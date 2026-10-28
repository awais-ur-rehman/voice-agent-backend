"""Authenticated native media-worker boundary; never a browser capability."""

import json
import secrets
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import AwareDatetime, Field
from sqlalchemy import select

from scheduler.contracts import Contract
from scheduler.db import bearer
from scheduler.errors import AppError
from scheduler.models import AgentSession, AgentVersion, VoiceSession

from .routes import Event, Turn, repair, require_store, run_command
from .security import decode_runtime
from .voice_contracts import PlaybackAck
from .voice_playback import acknowledge, finish_delivery, publish_segment
from .voice_sessions import claim_worker, disconnect_voice, require_worker
from .voice_tokens import decode_worker

router = APIRouter(prefix="/voice/worker", tags=["Internal voice worker"])


class Readiness(Contract):
    en: bool = Field(strict=True)
    ur: bool = Field(strict=True)


class WorkerNext(Contract):
    worker_id: UUID


class WorkerClaim(WorkerNext):
    tenant_id: UUID
    voice_id: UUID


class AudioTurn(Turn):
    utterance_started_at: AwareDatetime
    language: Literal["en", "ur"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertain: bool = False


class Published(Contract):
    readback_revision: int = Field(ge=1)
    segment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    published_samples: int = Field(ge=1, le=1_440_000)
    sample_rate: int = Field(ge=16000, le=48000)


class Acknowledged(Contract):
    participant: str = Field(min_length=1, max_length=128)
    ack: PlaybackAck


class Finished(Contract):
    readback_revision: int = Field(ge=1)


def bootstrap(request: Request, credentials=Depends(bearer)):
    settings = request.app.state.settings
    secret = settings.voice_bootstrap_secret
    if secret is None or credentials is None:
        raise AppError("unauthorized", 401)
    value = secret.get_secret_value()
    if any(
        other is not None and secrets.compare_digest(value, other.get_secret_value())
        for other in (
            settings.jwt_secret,
            settings.runtime_secret,
            settings.voice_worker_secret,
            settings.livekit_api_secret,
        )
    ):
        raise AppError("voice_unavailable", 503)
    if not secrets.compare_digest(value, credentials.credentials):
        raise AppError("unauthorized", 401)


def worker(request, db, identity, credentials, runtime_token):
    if credentials is None or runtime_token is None:
        raise AppError("unauthorized", 401)
    authority, tenant = decode_worker(
        request.app.state.settings, credentials.credentials, request.app.state.clock()
    )
    actor = decode_runtime(
        db, runtime_token, request.app.state.settings, now=request.app.state.clock()
    )
    if actor.id != identity or actor.tenant_id != tenant:
        raise AppError("unauthorized", 401)
    # Uniform lock order across all worker transitions.
    db.scalar(
        select(AgentSession)
        .where(AgentSession.id == actor.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = require_worker(db, actor, authority, request.app.state.clock())
    return actor, authority, row


@router.post("/ready", dependencies=[Depends(bootstrap)])
def ready(body: Readiness, request: Request):
    require_store(request).client.set("voice:ready", body.model_dump_json(), ex=15)
    return {"ready": True}


def _claim(request, body):
    with request.app.state.session_factory() as db, db.begin():
        grant = claim_worker(
            db,
            body.voice_id,
            body.tenant_id,
            body.worker_id,
            request.app.state.clock(),
            request.app.state.settings,
        )
        parent = db.get(AgentSession, grant.session_id)
        version = db.get(AgentVersion, parent.version_id)
        voice = db.get(VoiceSession, grant.voice_id)
        return grant.model_dump(mode="json") | {
            "language_mode": version.config.get("language_mode", "bilingual"),
            "caller_participant": voice.participant,
        }


@router.post("/claim", dependencies=[Depends(bootstrap)])
def claim(body: WorkerClaim, request: Request):
    return _claim(request, body)


@router.post("/next", dependencies=[Depends(bootstrap)])
def next_call(body: WorkerNext, request: Request):
    client = require_store(request).client
    raw = client.lindex("voice:pending", 0)
    if raw is None:
        raw = client.rpoplpush("voice:queue", "voice:pending")
    if raw is None:
        return None
    try:
        if len(raw) > 1024:
            raise ValueError("queue_message")
        payload = json.loads(raw)
        claim_body = WorkerClaim(**payload, worker_id=body.worker_id)
    except (ValueError, TypeError):
        raise AppError("voice_unavailable", 503) from None
    try:
        return _claim(request, claim_body)
    except AppError as exc:
        if exc.code == "voice_busy":
            return None
        if exc.code in {"voice_unavailable", "not_found", "unauthorized"}:
            client.lrem("voice:pending", 1, raw)
            return None
        raise


@router.get("/sessions/{identity}")
def inspect(
    identity: UUID,
    request: Request,
    credentials=Depends(bearer),
    runtime_token: str | None = Header(default=None, alias="X-Runtime-Capability"),
):
    with request.app.state.session_factory() as db, db.begin():
        actor, authority, voice = worker(request, db, identity, credentials, runtime_token)
        parent = db.get(AgentSession, identity)
        version = db.get(AgentVersion, actor.version_id)
        return {
            "session_id": str(identity),
            "revision": parent.revision,
            "state": deepcopy(parent.checkpoint),
            "generation": voice.generation,
            "language_mode": version.config.get("language_mode", "bilingual"),
            "caller_participant": voice.participant,
        }


@router.post("/sessions/{identity}/heartbeat")
def heartbeat(
    identity: UUID,
    request: Request,
    credentials=Depends(bearer),
    runtime_token: str | None = Header(default=None, alias="X-Runtime-Capability"),
):
    with request.app.state.session_factory() as db, db.begin():
        actor, authority, voice = worker(request, db, identity, credentials, runtime_token)
        voice.worker_lease_until = min(
            request.app.state.clock() + timedelta(seconds=15), voice.call_deadline
        )
        client = require_store(request).client
        for pending in client.lrange("voice:pending", 0, 31):
            try:
                record = json.loads(pending)
                if record.get("voice_id") == str(voice.id):
                    client.lrem("voice:pending", 1, pending)
            except (ValueError, TypeError):
                client.lrem("voice:pending", 1, pending)
        return {"generation": voice.generation, "lease_until": voice.worker_lease_until.isoformat()}


def _turn_authority(identity, request, credentials, runtime_token):
    with request.app.state.session_factory() as db, db.begin():
        _, authority, _ = worker(request, db, identity, credentials, runtime_token)
    return authority


@router.post("/sessions/{identity}/turn")
def audio_turn(
    identity: UUID,
    body: AudioTurn,
    request: Request,
    credentials=Depends(bearer),
    runtime_token: str | None = Header(default=None, alias="X-Runtime-Capability"),
):
    authority = _turn_authority(identity, request, credentials, runtime_token)
    now = request.app.state.clock()
    if (
        not body.live
        or body.uncertain
        or body.confidence is not None
        and body.confidence < 0.5
        or not now - timedelta(seconds=90) <= body.utterance_started_at <= now
    ):
        raise AppError("uncertain_audio", 422)
    from fastapi.security import HTTPAuthorizationCredentials

    from .voice_input import normalize_phone_input

    normalized = normalize_phone_input(body.text)
    if normalized is not None:
        body = body.model_copy(update={"text": normalized})
    return run_command(
        identity,
        body,
        request,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=runtime_token),
        live=True,
        voice_authority=authority,
        voice_input=body,
    )


@router.post("/sessions/{identity}/events")
def event(
    identity: UUID,
    body: Event,
    request: Request,
    credentials=Depends(bearer),
    runtime_token: str | None = Header(default=None, alias="X-Runtime-Capability"),
):
    authority = _turn_authority(identity, request, credentials, runtime_token)
    # Browser-supplied delivered text is never a source of trusted voice history.
    if body.delivered_text:
        raise AppError("validation_error", 422)
    from fastapi.security import HTTPAuthorizationCredentials

    response = run_command(
        identity,
        body,
        request,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=runtime_token),
        event=True,
        voice_authority=authority,
    )
    if body.kind == "delivery_interrupted":
        with request.app.state.session_factory() as db, db.begin():
            _, _, voice = worker(request, db, identity, credentials, runtime_token)
            voice.delivery = None
    return response


@router.post("/sessions/{identity}/publish")
def publish(
    identity: UUID,
    body: Published,
    request: Request,
    credentials=Depends(bearer),
    runtime_token: str | None = Header(default=None, alias="X-Runtime-Capability"),
):
    with request.app.state.session_factory() as db, db.begin():
        _, _, voice = worker(request, db, identity, credentials, runtime_token)
        parent = db.get(AgentSession, identity)
        state = parent.checkpoint
        if not state.get("readback") or state["readback"]["revision"] != body.readback_revision:
            raise AppError("idempotency_conflict", 409)
        now = request.app.state.clock()
        deadline = min(voice.call_deadline, parent.expires_at, now + timedelta(seconds=120))
        hold = state.get("hold")
        if hold and hold.get("expires_at_ms"):
            from datetime import UTC

            deadline = min(deadline, datetime.fromtimestamp(hold["expires_at_ms"] / 1000, UTC))
        voice.delivery = publish_segment(
            deepcopy(voice.delivery),
            state,
            voice.generation,
            voice.participant,
            body.segment_id,
            body.published_samples,
            body.sample_rate,
            now,
            deadline,
        )
    return {"published": True}


@router.post("/sessions/{identity}/ack")
def ack(
    identity: UUID,
    body: Acknowledged,
    request: Request,
    credentials=Depends(bearer),
    runtime_token: str | None = Header(default=None, alias="X-Runtime-Capability"),
):
    with request.app.state.session_factory() as db, db.begin():
        _, _, voice = worker(request, db, identity, credentials, runtime_token)
        if body.ack.session_id != identity or voice.delivery is None:
            raise AppError("validation_error", 422)
        ledger = deepcopy(voice.delivery)
        acknowledge(ledger, body.ack, body.participant, request.app.state.clock())
        voice.delivery = ledger
    return {"acknowledged": True}


@router.post("/sessions/{identity}/finish")
def finish(
    identity: UUID,
    body: Finished,
    request: Request,
    credentials=Depends(bearer),
    runtime_token: str | None = Header(default=None, alias="X-Runtime-Capability"),
):
    with request.app.state.session_factory() as db, db.begin():
        _, _, voice = worker(request, db, identity, credentials, runtime_token)
        ledger = deepcopy(voice.delivery)
        if not ledger or ledger["readback_revision"] != body.readback_revision:
            raise AppError("idempotency_conflict", 409)
        finish_delivery(ledger, request.app.state.clock())
        voice.delivery = ledger
    return {"finished": True}


@router.post("/sessions/{identity}/disconnect")
def disconnect(
    identity: UUID,
    request: Request,
    credentials=Depends(bearer),
    runtime_token: str | None = Header(default=None, alias="X-Runtime-Capability"),
):
    with request.app.state.session_factory() as db, db.begin():
        actor, authority, voice = worker(request, db, identity, credentials, runtime_token)
        parent = db.get(AgentSession, identity)
        hold = deepcopy((parent.checkpoint or {}).get("hold"))
        voice = disconnect_voice(db, actor, authority, request.app.state.clock())
        state = deepcopy(parent.checkpoint)
        if hold:
            require_store(request).release_hold(
                str(actor.tenant_id), hold["provider"], str(identity), hold["hold_id"]
            )
        response = {"generation": voice.generation, "status": voice.status}
    repair(require_store(request), identity, state)
    return response


@router.post("/sessions/{identity}/end")
def end(
    identity: UUID,
    request: Request,
    credentials=Depends(bearer),
    runtime_token: str | None = Header(default=None, alias="X-Runtime-Capability"),
):
    from scheduler.db import Principal

    from .voice_routes import _terminate
    from .voice_sessions import _locked_pair

    if credentials is None:
        raise AppError("unauthorized", 401)
    authority, tenant = decode_worker(
        request.app.state.settings, credentials.credentials, request.app.state.clock(), cleanup=True
    )
    with request.app.state.session_factory() as db, db.begin():
        parent, voice = _locked_pair(db, tenant, voice_id=authority.voice_id)
        if (
            parent.id != identity
            or voice.generation != authority.generation
            or voice.worker_id != authority.worker_id
            or not voice.worker_nonce_hash
            or not secrets.compare_digest(voice.worker_nonce_hash, authority.nonce_hash)
        ):
            raise AppError("voice_worker_required", 403)
        # Ending is the only allowed operation after brain revocation/lease expiry.
        # This grants no inspection, renewal, planning or scheduling authority.
        user = Principal(voice.creator_id, voice.tenant_id, "", "worker")
        generation = voice.generation
    result = _terminate(request, user, identity, expected_generation=generation)
    if result is None:
        raise AppError("voice_worker_required", 403)
    return result


class Interrupted(Contract):
    command_id: str = Field(min_length=1, max_length=128)


@router.post("/sessions/{identity}/interrupt")
def interrupt(
    identity: UUID,
    body: Interrupted,
    request: Request,
    credentials=Depends(bearer),
    runtime_token: str | None = Header(default=None, alias="X-Runtime-Capability"),
):
    from scheduler.models import CommandReceipt

    from .security import runtime_secret
    from .sessions import command
    from .tools import ToolEngine

    store = require_store(request)
    with request.app.state.session_factory() as db, db.begin():
        actor, authority, voice = worker(request, db, identity, credentials, runtime_token)
        parent = db.get(AgentSession, identity)
        receipt = db.scalar(
            select(CommandReceipt).where(
                CommandReceipt.session_id == identity, CommandReceipt.command_id == body.command_id
            )
        )
        revision = receipt.revision - 1 if receipt is not None else parent.revision

        def apply(db, state):
            voice.delivery = None
            return ToolEngine(db, actor, store, request.app.state.clock()).trusted_event(
                state, "delivery_interrupted"
            )

        response, state = command(
            db,
            actor,
            body.command_id,
            revision,
            {"kind": "delivery_interrupted"},
            request.app.state.clock(),
            runtime_secret(request.app.state.settings),
            apply,
            clock=request.app.state.clock,
            voice_authority=authority,
        )
    return {
        "result": response,
        "revision": state["revision"],
        "state": state,
        "cache_ready": repair(store, identity, state),
        "provider_requests": 0,
    }
