"""Authoritative opt-in text boundary with settings/call lock ordering.

Runtime lock order: existing session/domain locks -> call -> settings SHARE.
Manager settings mutation locks settings and may shorten transcript expiry,
never active session/call rows. Erasure locks call then deletes its text. No path
locks an existing call after settings; this prevents disable/delete/insert lock cycles.
"""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analytics.contracts import SettingsUpdate
from analytics.models import AnalyticsSettings, CallRecord, Transcript
from analytics.privacy import redact
from scheduler.db import Principal, tenant_context
from scheduler.errors import AppError


def settings_row(db: Session, tenant_id: UUID, *, shared: bool = False) -> AnalyticsSettings:
    db.execute(insert(AnalyticsSettings).values(tenant_id=tenant_id).on_conflict_do_nothing())
    query = select(AnalyticsSettings).where(AnalyticsSettings.tenant_id == tenant_id)
    if shared:
        query = query.with_for_update(read=True)
    return db.scalars(query.execution_options(populate_existing=True)).one()


def locked_call(db: Session, session_id: UUID) -> CallRecord | None:
    return db.scalar(
        select(CallRecord)
        .where(CallRecord.session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def append_text(
    db: Session,
    session_id: UUID,
    *,
    speaker: str,
    value: str,
    literals: tuple[str, ...],
    occurred_at: datetime,
    now: datetime,
    event_id: UUID,
) -> None:
    call = locked_call(db, session_id)
    if (
        call is None
        or not call.capture_enabled_at_start
        or call.consent != "accepted"
        or call.transcript_deleted_at is not None
    ):
        return
    settings = settings_row(db, call.tenant_id, shared=True)
    if not settings.capture_enabled:
        return
    expires = occurred_at + timedelta(days=settings.retention_days)
    if expires <= now:
        return
    clean = redact(value, literals)
    if clean:
        db.execute(
            insert(Transcript)
            .values(
                id=uuid4(),
                tenant_id=call.tenant_id,
                session_id=session_id,
                event_id=event_id,
                speaker=speaker,
                text=clean,
                occurred_at=occurred_at,
                expires_at=expires,
            )
            .on_conflict_do_nothing()
        )


def manager(user: Principal) -> None:
    if user.role not in {"owner", "admin"}:
        raise AppError("forbidden", 403)


def update_settings(
    db: Session, user: Principal, update: SettingsUpdate, now: datetime
) -> AnalyticsSettings:
    manager(user)
    tenant_context(db, user.tenant_id)
    settings_row(db, user.tenant_id)
    settings = db.scalars(
        select(AnalyticsSettings)
        .where(AnalyticsSettings.tenant_id == user.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if settings.revision != update.revision:
        raise AppError("revision_conflict", 409)
    if update.retention_days < settings.retention_days:
        # Expiry only moves earlier; a later policy expansion cannot resurrect
        # text that expired under a previously shortened retention policy.
        db.execute(
            sql_update(Transcript)
            .where(Transcript.tenant_id == user.tenant_id)
            .values(
                expires_at=func.least(
                    Transcript.expires_at,
                    Transcript.occurred_at + timedelta(days=update.retention_days),
                )
            )
        )
    settings.capture_enabled = update.capture_enabled
    settings.retention_days = update.retention_days
    settings.revision += 1
    settings.updated_at = now
    db.flush()
    return settings


def delete_transcripts(db: Session, user: Principal, session_id: UUID, now: datetime) -> None:
    manager(user)
    tenant_context(db, user.tenant_id)
    call = locked_call(db, session_id)
    if call is None or call.tenant_id != user.tenant_id:
        raise AppError("not_found", 404)
    call.transcript_deleted_at = call.transcript_deleted_at or now
    db.execute(delete(Transcript).where(Transcript.session_id == session_id))
    db.flush()


def read_transcripts(
    db: Session, user: Principal, session_id: UUID, now: datetime, *, limit: int = 100
) -> list[Transcript]:
    manager(user)
    tenant_context(db, user.tenant_id)
    # Same lock order as insertion, so erasure/retention cannot be read stale.
    call = locked_call(db, session_id)
    if call is None or call.tenant_id != user.tenant_id:
        raise AppError("not_found", 404)
    if call.transcript_deleted_at is not None:
        return []
    settings = settings_row(db, user.tenant_id, shared=True)
    return list(
        db.scalars(
            select(Transcript)
            .where(
                Transcript.session_id == session_id,
                Transcript.expires_at > now,
                Transcript.occurred_at > now - timedelta(days=settings.retention_days),
            )
            .order_by(Transcript.occurred_at, Transcript.id)
            .limit(max(1, min(limit, 100)))
        )
    )
