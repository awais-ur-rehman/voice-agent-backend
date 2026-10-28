"""SQL session guards serialize authorization, domain effects, and retry receipts.

The caller owns the database transaction and repairs Valkey only after commit.
"""

import hashlib
import hmac
import json
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from scheduler.db import Principal, tenant_context
from scheduler.errors import AppError
from scheduler.models import Agent, AgentSession, AgentVersion, CommandReceipt

from .security import RuntimeActor, active_guard, issue_runtime
from .state import initial_state, recovery_checkpoint

INACTIVITY = timedelta(minutes=30)
RETENTION = timedelta(hours=24)


def create_session(
    db: Session, staff: Principal, agent_id: UUID, now: datetime, settings, *, transport_mode="text"
) -> dict:
    tenant_context(db, staff.tenant_id)
    agent = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == staff.tenant_id))
    if agent is None or not agent.enabled or agent.active_version_id is None:
        raise AppError("not_found", 404)
    version = db.scalar(
        select(AgentVersion).where(
            AgentVersion.id == agent.active_version_id,
            AgentVersion.agent_id == agent.id,
            AgentVersion.tenant_id == staff.tenant_id,
        )
    )
    if version is None:
        raise AppError("not_found", 404)
    if transport_mode not in {"text", "voice"}:
        raise AppError("validation_error", 422)
    session_id = uuid4()
    state = initial_state(session_id, agent.id, version.id)
    if version.config.get("language_mode") == "ur":
        state["lang"] = "ur"
    state["read_digits_in_english"] = bool(version.config.get("read_digits_in_english", False))
    row = AgentSession(
        id=session_id,
        tenant_id=staff.tenant_id,
        agent_id=agent.id,
        version_id=version.id,
        revision=0,
        transport_mode=transport_mode,
        checkpoint=recovery_checkpoint(state),
        expires_at=now + INACTIVITY,
        updated_at=now,
    )
    token = issue_runtime(row, settings)
    db.add(row)
    db.flush()
    return {
        "session_id": str(row.id),
        "token": token,
        "revision": 0,
        "state": state,
        "expires_at": row.expires_at.isoformat(),
    }


def command(
    db: Session,
    actor: RuntimeActor,
    command_id: str,
    expected_revision: int,
    payload: dict,
    now: datetime,
    secret: str,
    execute,
    *,
    clock=None,
    voice_authority=None,
) -> tuple[dict, dict]:
    if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
        raise AppError("validation_error", 422)
    tenant_context(db, actor.tenant_id)
    db.execute(text("SET LOCAL lock_timeout='1500ms'"))
    db.execute(text("SET LOCAL statement_timeout='2000ms'"))
    row = db.scalar(
        select(AgentSession)
        .where(AgentSession.id == actor.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = clock() if clock else now
    row = active_guard(row, actor, now)
    if row.transport_mode == "voice":
        if voice_authority is None:
            raise AppError("voice_worker_required", 403)
        from .voice_sessions import require_worker

        require_worker(db, actor, voice_authority, now)
    agent = db.get(Agent, actor.agent_id)
    if agent is None or not agent.enabled:
        raise AppError("unauthorized", 401)
    key = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    encoded = json.dumps(
        {"session": str(actor.id), "revision": expected_revision, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    digest = hmac.new(key.encode(), encoded, hashlib.sha256).hexdigest()
    receipt = db.scalar(
        select(CommandReceipt).where(
            CommandReceipt.session_id == actor.id,
            CommandReceipt.tenant_id == actor.tenant_id,
            CommandReceipt.command_id == command_id,
        )
    )
    if receipt is not None:
        if not hmac.compare_digest(receipt.request_digest, digest):
            raise AppError("idempotency_conflict", 409)
        if receipt.expires_at <= now or receipt.response is None or receipt.checkpoint is None:
            raise AppError("unauthorized", 401)
        # A delayed retry must never roll the live cache back to an earlier revision.
        checkpoint = row.checkpoint if row.revision > receipt.revision else receipt.checkpoint
        return deepcopy(receipt.response), deepcopy(checkpoint or {})
    if row.revision != expected_revision:
        raise AppError("idempotency_conflict", 409)
    if row.checkpoint is None:
        raise AppError("unauthorized", 401)
    state = deepcopy(row.checkpoint)
    state["transport_mode"] = row.transport_mode
    previous_readback = deepcopy(state.get("readback"))
    result = execute(db, state)
    if isinstance(result, tuple):
        response, state = result
    else:
        response = result
    now = clock() if clock else now
    active_guard(row, actor, now)
    if row.transport_mode == "voice":
        voice = require_worker(db, actor, voice_authority, now)
        if previous_readback != state.get("readback"):
            voice.delivery = None
    state["revision"] = row.revision + 1
    checkpoint = recovery_checkpoint(state)
    # Code-generated envelopes must not echo identifying verification factors.
    response = recovery_checkpoint({"result": response})["result"]
    row.revision += 1
    row.checkpoint = checkpoint
    row.updated_at = now
    row.expires_at = now + INACTIVITY
    if state.get("stage") == "ended" or state.get("ended"):
        row.status = "ended"
        row.revoked_at = now
        if row.transport_mode == "voice":
            voice.delivery = {"terminal_started_at": now.isoformat()}
    db.add(
        CommandReceipt(
            tenant_id=actor.tenant_id,
            session_id=actor.id,
            command_id=command_id,
            request_digest=digest,
            revision=row.revision,
            response=response,
            checkpoint=checkpoint,
            created_at=now,
            expires_at=now + RETENTION,
        )
    )
    db.flush()
    return response, state


def purge_recovery(db: Session, now: datetime, limit: int = 500) -> dict:
    """Bounded, tenant-RLS-scoped deletion of expired recovery payloads."""
    limit = max(1, min(limit, 1000))
    receipts = list(
        db.scalars(
            select(CommandReceipt)
            .where(CommandReceipt.expires_at <= now, CommandReceipt.response.is_not(None))
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for receipt in receipts:
        receipt.response = None
        receipt.checkpoint = None
    sessions = list(
        db.scalars(
            select(AgentSession)
            .where(AgentSession.expires_at <= now - RETENTION, AgentSession.checkpoint.is_not(None))
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for row in sessions:
        row.checkpoint = None
        row.status = "expired"
        row.revoked_at = row.revoked_at or now
    db.flush()
    return {"receipts": len(receipts), "sessions": len(sessions)}


def renew_session(db: Session, actor: RuntimeActor, now: datetime, settings, *, clock=None) -> dict:
    """Rotate a still-live scoped capability; an expired session cannot be revived."""
    tenant_context(db, actor.tenant_id)
    db.execute(text("SET LOCAL lock_timeout='1500ms'"))
    db.execute(text("SET LOCAL statement_timeout='2000ms'"))
    row = db.scalar(
        select(AgentSession)
        .where(AgentSession.id == actor.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = clock() if clock else now
    row = active_guard(row, actor, now)
    if row.transport_mode == "voice":
        raise AppError("voice_worker_required", 403)
    agent = db.get(Agent, actor.agent_id)
    if agent is None or not agent.enabled:
        raise AppError("unauthorized", 401)
    token = issue_runtime(row, settings)
    row.expires_at = now + INACTIVITY
    row.updated_at = now
    db.flush()
    return {
        "session_id": str(row.id),
        "token": token,
        "revision": row.revision,
        "state": deepcopy(row.checkpoint),
        "expires_at": row.expires_at.isoformat(),
    }
