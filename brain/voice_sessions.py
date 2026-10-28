"""SQL authority for media workers. Media grants cannot authorize brain commands."""

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from scheduler.errors import AppError
from scheduler.models import VoiceSession

from .security import RuntimeActor


@dataclass(frozen=True)
class WorkerAuthority:
    voice_id: UUID
    worker_id: UUID
    generation: int
    nonce_hash: str = field(repr=False)


def require_worker(
    db: Session, actor: RuntimeActor, authority: WorkerAuthority, now: datetime
) -> VoiceSession:
    # Lock order is always agent_session then voice_session (command owns the first).
    row = db.scalar(
        select(VoiceSession)
        .where(VoiceSession.id == authority.voice_id, VoiceSession.tenant_id == actor.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        row is None
        or row.session_id != actor.id
        or row.status != "active"
        or row.generation != authority.generation
        or row.worker_id != authority.worker_id
        or row.worker_nonce_hash is None
        or not secrets.compare_digest(row.worker_nonce_hash, authority.nonce_hash)
        or row.worker_lease_until is None
        or row.worker_lease_until <= now
        or row.call_deadline <= now
    ):
        raise AppError("voice_worker_required", 403)
    return row


def create_voice(db, staff, agent_id, request_key, now, settings):
    from datetime import timedelta
    from uuid import uuid4

    from scheduler.db import tenant_context
    from scheduler.models import AgentSession

    from .sessions import create_session

    tenant_context(db, staff.tenant_id)
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
        raise AppError("validation_error", 422)
    # Serialize retries by actor and request key; no external operation under this lock.
    from sqlalchemy import text

    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"voice-create:{staff.tenant_id}:{staff.id}:{request_key}"},
    )
    existing = db.scalar(
        select(VoiceSession).where(
            VoiceSession.creator_id == staff.id, VoiceSession.request_key == request_key
        )
    )
    if existing is not None:
        parent = db.get(AgentSession, existing.session_id)
        if parent.agent_id != agent_id:
            raise AppError("idempotency_conflict", 409)
        return existing
    created = create_session(db, staff, agent_id, now, settings, transport_mode="voice")
    voice = VoiceSession(
        tenant_id=staff.tenant_id,
        session_id=UUID(created["session_id"]),
        creator_id=staff.id,
        request_key=request_key,
        room="voice-" + uuid4().hex,
        participant="caller-" + uuid4().hex,
        call_deadline=now + timedelta(minutes=10),
    )
    db.add(voice)
    db.flush()
    return voice


def _locked_pair(db, tenant_id, voice_id=None, session_id=None):
    from scheduler.db import tenant_context
    from scheduler.models import AgentSession

    tenant_context(db, tenant_id)
    query = select(VoiceSession).where(VoiceSession.tenant_id == tenant_id)
    query = (
        query.where(VoiceSession.id == voice_id)
        if voice_id
        else query.where(VoiceSession.session_id == session_id)
    )
    voice = db.scalar(query)
    if voice is None:
        raise AppError("not_found", 404)
    parent = db.scalar(
        select(AgentSession)
        .where(AgentSession.id == voice.session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    voice = db.scalar(
        select(VoiceSession)
        .where(VoiceSession.id == voice.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if parent is None or parent.transport_mode != "voice":
        raise AppError("unauthorized", 401)
    return parent, voice


def claim_worker(db, voice_id, tenant_id, worker_id, now, settings):
    from datetime import timedelta

    from .security import issue_runtime
    from .voice_contracts import MediaGrant, WorkerGrant
    from .voice_tokens import decode_worker, media_token, worker_token

    parent, voice = _locked_pair(db, tenant_id, voice_id=voice_id)
    if (
        voice.status != "active"
        or voice.call_deadline <= now
        or parent.status != "active"
        or parent.expires_at <= now
        or parent.revoked_at is not None
    ):
        raise AppError("voice_unavailable", 409)
    live_lease = voice.worker_lease_until and voice.worker_lease_until > now
    if live_lease and voice.worker_id != worker_id:
        raise AppError("voice_busy", 409)
    # A retry by the same worker rotates capabilities, fencing any delayed old
    # response without unnecessarily changing the room/playback generation.
    # Reclaiming a dead worker fences all its pending synthesis and delivery.
    if voice.worker_id is not None and not live_lease:
        voice.generation += 1
        _invalidate(parent, voice)
    authority = WorkerAuthority(voice.id, worker_id, voice.generation, "")
    token = worker_token(settings, authority, tenant_id, now)
    authority, _ = decode_worker(settings, token, now)
    voice.worker_id = worker_id
    voice.worker_nonce_hash = authority.nonce_hash
    voice.worker_lease_until = min(now + timedelta(seconds=15), voice.call_deadline)
    runtime = issue_runtime(parent, settings)
    db.flush()
    return WorkerGrant(
        session_id=parent.id,
        voice_id=voice.id,
        worker_id=worker_id,
        generation=voice.generation,
        token=token,
        runtime_token=runtime,
        lease_until=voice.worker_lease_until,
        media=MediaGrant(
            url=settings.livekit_url,
            room=voice.room,
            participant="worker",
            token=media_token(settings, voice.room, "worker", now),
            expires_at=now + timedelta(seconds=120),
            session_id=parent.id,
            generation=voice.generation,
        ),
    )


def _invalidate(parent, voice):
    from copy import deepcopy

    voice.delivery = None
    parent.token_jti_hash = ""
    state = deepcopy(parent.checkpoint or {})
    state["readback"] = None
    state.pop("hold", None)
    state["history"] = []
    state["summary"] = ""
    state["revision"] = parent.revision + 1
    parent.revision += 1
    parent.checkpoint = state


def disconnect_voice(db, actor, authority, now):
    from scheduler.models import AgentSession

    parent = db.scalar(
        select(AgentSession)
        .where(AgentSession.id == actor.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    from .security import active_guard

    active_guard(parent, actor, now)
    voice = require_worker(db, actor, authority, now)
    _invalidate(parent, voice)
    voice.generation += 1
    voice.status = "disconnected"
    voice.disconnected_at = now
    voice.worker_lease_until = now
    db.flush()
    return voice


def resume_voice(db, staff, session_id, now):
    from datetime import timedelta

    parent, voice = _locked_pair(db, staff.tenant_id, session_id=session_id)
    if voice.creator_id != staff.id:
        raise AppError("voice_unavailable", 409)
    if (
        voice.status == "active"
        and voice.worker_id is not None
        and voice.worker_lease_until is not None
        and voice.worker_lease_until <= now
    ):
        if now >= voice.worker_lease_until + timedelta(seconds=30):
            raise AppError("voice_unavailable", 409)
        disconnected_at = voice.worker_lease_until
        _invalidate(parent, voice)
        voice.generation += 1
        voice.status = "disconnected"
        voice.disconnected_at = disconnected_at
    if (
        voice.creator_id != staff.id
        or voice.status != "disconnected"
        or voice.disconnected_at is None
        or now >= voice.disconnected_at + timedelta(seconds=30)
        or voice.call_deadline <= now
        or parent.expires_at <= now
        or parent.status != "active"
        or parent.revoked_at is not None
    ):
        raise AppError("voice_unavailable", 409)
    voice.status = "active"
    voice.disconnected_at = None
    voice.worker_id = None
    voice.worker_nonce_hash = None
    voice.worker_lease_until = None
    db.flush()
    return voice
