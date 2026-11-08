"""Analytics-only session mirror. Never reconstruct pre-deployment telemetry."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analytics.contracts import EndReason, Outcome
from analytics.models import CallRecord, DirtyBucket, TaskRecord, TurnEvent
from analytics.privacy import NOTICE_VERSION
from analytics.transcripts import locked_call, settings_row
from scheduler.errors import AppError
from scheduler.models import AgentSession, AgentVersion, Tenant, VoiceSession


def dirty(db: Session, call: CallRecord, now: datetime) -> None:
    bucket = call.started_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    stmt = insert(DirtyBucket).values(
        id=uuid4(),
        tenant_id=call.tenant_id,
        agent_id=call.agent_id,
        purpose=call.purpose,
        channel=call.channel,
        timezone=call.timezone,
        local_date=call.local_date,
        bucket_start=bucket,
        utc_offset_minutes=call.utc_offset_minutes,
        revision=1,
        dirty_at=now,
    )
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                "tenant_id",
                "agent_id",
                "purpose",
                "channel",
                "timezone",
                "local_date",
                "bucket_start",
            ],
            set_={"revision": DirtyBucket.revision + 1, "dirty_at": now},
        )
    )
    call.summary_revision += 1


def begin_session(db: Session, row: AgentSession, now: datetime) -> None:
    settings = settings_row(db, row.tenant_id, shared=True)
    tenant = db.get(Tenant, row.tenant_id)
    version = db.get(AgentVersion, row.version_id)
    assert tenant is not None and version is not None
    local = now.astimezone(ZoneInfo(tenant.timezone))
    offset = local.utcoffset()
    call = CallRecord(
        session_id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        trace_id=uuid4(),
        purpose=version.purpose,
        channel=row.transport_mode,
        timezone=tenant.timezone,
        local_date=local.date(),
        utc_offset_minutes=int(offset.total_seconds() / 60) if offset else 0,
        started_at=now,
        expires_at=row.expires_at,
        updated_at=now,
        capture_enabled_at_start=settings.capture_enabled,
        consent="pending" if settings.capture_enabled else "off",
        consent_notice_version=NOTICE_VERSION if settings.capture_enabled else None,
        summary_revision=0,
        language=(row.checkpoint or {}).get("lang", "unknown"),
    )
    db.add(call)
    db.flush()
    db.add(
        TurnEvent(
            tenant_id=row.tenant_id,
            session_id=row.id,
            event_id=uuid4(),
            kind="session.started",
            language=call.language,
            safe_metadata={},
            occurred_at=now,
        )
    )
    dirty(db, call, now)


def finish_session(db: Session, row: AgentSession, *, reason: EndReason, now: datetime) -> None:
    call = locked_call(db, row.id)
    if call is None or call.ended_at is not None:
        return
    if reason == "timeout":
        now = min(now, call.expires_at)
    tasks = list(db.scalars(select(TaskRecord).where(TaskRecord.session_id == row.id)))
    for task in tasks:
        if task.status in {"active", "parked"}:
            task.status, task.ended_at = "abandoned", now
    call.active_task_id = None
    call.parked_task_ids = []
    answered_info = (
        db.scalar(
            select(TurnEvent.id)
            .where(
                TurnEvent.session_id == row.id,
                TurnEvent.kind == "tool.result",
                TurnEvent.tool_status == "success",
                TurnEvent.tool_name.in_(["get_clinic_info", "search_knowledge"]),
            )
            .limit(1)
        )
        is not None
    )
    call.outcome = classify_outcome(
        call.outcome,
        reason=reason,
        unfinished=any(task.status != "completed" for task in tasks),
        answered_info=answered_info,
    )
    if reason == "handoff":
        call.quality_flags = sorted(set(call.quality_flags) | {"handoff"})
    call.status = (
        "expired"
        if reason == "timeout"
        else "failed"
        if reason in {"error", "provisioning_failed"}
        else "ended"
    )
    call.end_reason, call.ended_at, call.updated_at = reason, now, now
    db.add(
        TurnEvent(
            tenant_id=call.tenant_id,
            session_id=call.session_id,
            event_id=uuid4(),
            kind="session.ended",
            language=call.language,
            safe_metadata={},
            occurred_at=now,
        )
    )
    dirty(db, call, now)


def classify_outcome(
    prior: Outcome | None, *, reason: EndReason, unfinished: bool, answered_info: bool
) -> Outcome:
    """Shared facts-only classifier for API and analytics-worker terminalization."""
    if reason == "handoff":
        return "handoff"
    if reason in {"error", "provisioning_failed"}:
        return "error"
    if prior in {"booked", "cancelled", "rescheduled", "handoff", "error"}:
        return prior
    return "info_only" if answered_info and not unfinished else "abandoned"


def guard_session(
    db: Session,
    row: AgentSession,
    now: datetime,
    *,
    clock: Callable[[], datetime] | None = None,
    error: str = "unauthorized",
    allow_handoff: bool = False,
) -> datetime:
    """Acquire mirror after core locks, then refresh time before any extension.

    Analytics workers lock only the mirror. A committed worker expiry must win
    over a runtime waiting to extend its lease, even with an older entry clock.
    Handoff commands may still perform their existing end-call cleanup.
    """
    call = locked_call(db, row.id)
    now = clock() if clock else now
    if call is not None and (
        call.ended_at is not None
        and not (allow_handoff and call.end_reason == "handoff")
        or now >= call.expires_at
    ):
        raise AppError(error, 409 if error == "voice_unavailable" else 401)
    return now


def sync_session(
    db: Session, row: AgentSession, now: datetime, voice: VoiceSession | None = None
) -> None:
    call = locked_call(db, row.id)
    if call is None or call.ended_at is not None:
        return
    deadline = row.expires_at
    if row.transport_mode == "voice":
        voice = voice or db.scalar(select(VoiceSession).where(VoiceSession.session_id == row.id))
        if voice:
            deadline = min(deadline, voice.call_deadline)
            if voice.status == "disconnected" and voice.disconnected_at:
                deadline = min(deadline, voice.disconnected_at + timedelta(seconds=30))
            elif voice.status == "active" and voice.worker_id and voice.worker_lease_until:
                deadline = min(deadline, voice.worker_lease_until + timedelta(seconds=30))
            elif voice.status == "provisioning":
                started = (voice.delivery or {}).get("provisioning_started_at")
                # Admission uses app time, not the database clock default.
                deadline = min(
                    deadline,
                    (datetime.fromisoformat(started) if started else call.started_at)
                    + timedelta(seconds=30),
                )
            call.status = "disconnected" if voice.status == "disconnected" else "active"
    call.expires_at, call.updated_at = deadline, now
    dirty(db, call, now)
    if now >= deadline:
        finish_session(db, row, reason="timeout", now=deadline)
