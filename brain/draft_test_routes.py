"""Staff-authenticated test transport; no runtime credentials cross this boundary."""

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, Field

from scheduler.appointments import IdempotencyKey
from scheduler.contracts import Contract
from scheduler.db import Principal, manager, session, tenant_context
from scheduler.errors import AppError
from scheduler.models import AgentSession, AgentVersion

from . import draft_tests as scopes
from .routes import Turn, require_store, run_command
from .voice_contracts import VoiceSnapshot

router = APIRouter(prefix="/dashboard", tags=["Draft tests"])


class TestCreate(Contract):
    draft_revision: int = Field(ge=1)
    replace: bool = False


class TestTurn(Contract):
    command_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=2000)


class Capability(Contract):
    available: bool
    reason: str | None = None


class Capabilities(Contract):
    text: Capability
    voice: Capability


class FixturePatient(Contract):
    id: UUID
    name: str
    phone: str
    dob: date


class FixtureAppointment(Contract):
    id: UUID
    patient_id: UUID
    provider_id: UUID
    service_id: UUID
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class Fixtures(Contract):
    patients: list[FixturePatient]
    appointment: FixtureAppointment | None
    appointment_unavailable_reason: str | None


class Message(Contract):
    role: Literal["assistant"] = "assistant"
    text: str


class Conversation(Contract):
    revision: int
    messages: list[Message]
    transport: Literal["text", "voice"]
    text_confirmation_reset: bool


class TestSnapshot(Contract):
    id: UUID
    draft_revision: int
    expires_at: AwareDatetime
    status: Literal["active", "ended", "expired"]
    transport_capabilities: Capabilities
    conversation: Conversation
    fixtures: Fixtures


class ToolTrace(Contract):
    action: str | None
    ok: bool
    code: str | None


class TurnResult(Contract):
    id: UUID
    revision: int
    status: Literal["active", "ended", "expired"]
    messages: list[Message]
    tool_trace: list[ToolTrace]


class TestVoiceCreate(Contract):
    request_key: str = Field(min_length=1, max_length=128)


def snapshot(db, row, request):
    from .voice_routes import _ready

    tenant_context(db, row.child_tenant_id)
    runtime = db.get(AgentSession, row.session_id)
    version = db.get(AgentVersion, runtime.version_id)
    voice = Capability(available=False, reason="voice_unavailable")
    if row.status == "active":
        try:
            _ready(
                request,
                version.config
                if version.config.get("schema_version") == 2
                else version.config.get("language_mode", "bilingual"),
            )
            voice = Capability(available=True)
        except AppError as exc:
            voice.reason = exc.code
    checkpoint = runtime.checkpoint or {}
    result = TestSnapshot(
        id=row.id,
        draft_revision=row.draft_revision,
        expires_at=row.expires_at,
        status=row.status,
        transport_capabilities=Capabilities(
            text=Capability(
                available=row.status == "active",
                reason=None if row.status == "active" else "test_expired",
            ),
            voice=voice,
        ),
        conversation=Conversation(
            revision=runtime.revision,
            messages=[Message(text=checkpoint["greeting"].get(checkpoint.get("lang", "en"), ""))]
            if checkpoint.get("greeting")
            else [],
            transport="voice" if row.voice_session_id else "text",
            text_confirmation_reset=row.voice_session_id is not None,
        ),
        fixtures=Fixtures.model_validate(row.fixtures),
    )
    tenant_context(db, row.tenant_id)
    return result


def expire(request, user, identity):
    scopes.finish(request, user, identity, expired_only=True)


@router.post("/agents/{agent_id}/tests", response_model=TestSnapshot, status_code=201)
def start(
    agent_id: UUID,
    body: TestCreate,
    request: Request,
    key: IdempotencyKey,
    user: Principal = Depends(manager),
    db=Depends(session),
):
    require_store(request)
    row, rooms = scopes.provision(
        db, user, agent_id, body.draft_revision, key, body.replace, request
    )
    result = snapshot(db, row, request)
    db.commit()
    scopes.release_rooms(request, rooms)
    return result


@router.get("/tests/{identity}", response_model=TestSnapshot)
def inspect(
    identity: UUID, request: Request, user: Principal = Depends(manager), auth_db=Depends(session)
):
    auth_db.commit()
    expire(request, user, identity)
    with request.app.state.session_factory() as db, db.begin():
        return snapshot(
            db, scopes.owned(db, user, identity, request.app.state.clock(), active=False), request
        )


@router.post("/tests/{identity}/end", response_model=TestSnapshot)
def end(
    identity: UUID, request: Request, user: Principal = Depends(manager), auth_db=Depends(session)
):
    auth_db.commit()
    scopes.finish(request, user, identity)
    with request.app.state.session_factory() as db, db.begin():
        return snapshot(
            db, scopes.owned(db, user, identity, request.app.state.clock(), active=False), request
        )


@router.post("/tests/{identity}/turns", response_model=TurnResult)
def turn(
    identity: UUID,
    body: TestTurn,
    request: Request,
    user: Principal = Depends(manager),
    auth_db=Depends(session),
):
    auth_db.commit()
    expire(request, user, identity)
    with request.app.state.session_factory() as db, db.begin():
        row = scopes.owned(db, user, identity, request.app.state.clock())
        if row.voice_session_id:
            raise AppError("voice_worker_required", 403)
        sid = row.session_id
    result = run_command(
        sid,
        Turn(command_id=body.command_id, expected_revision=body.revision, text=body.text),
        request,
        None,
        live=True,
        internal_auth=lambda db: scopes.internal_actor(db, request, user, identity, sid),
    )
    envelope = result["result"]
    language = result["state"].get("lang", "en")
    return TurnResult(
        id=identity,
        revision=result["revision"],
        status="ended" if result["state"].get("ended") else "active",
        messages=[Message(text=envelope["say"][language])],
        tool_trace=[
            ToolTrace(
                action=result["executed_action"],
                ok=envelope["ok"],
                code=(envelope.get("error") or {}).get("code"),
            )
        ],
    )


def voice_scope(db, request, user, identity):
    row = scopes.owned(db, user, identity, request.app.state.clock())
    tenant_context(db, row.child_tenant_id)
    return row


@router.post("/tests/{identity}/voice", response_model=VoiceSnapshot, status_code=201)
def voice_start(
    identity: UUID,
    body: TestVoiceCreate,
    request: Request,
    user: Principal = Depends(manager),
    auth_db=Depends(session),
):
    from .voice_routes import _language_mode, _provision, _ready, _reserve, _snapshot
    from .voice_sessions import create_voice

    auth_db.commit()
    expire(request, user, identity)
    with request.app.state.session_factory() as db, db.begin():
        row = voice_scope(db, request, user, identity)
        text_session = db.get(AgentSession, row.session_id)
        store = _ready(request, _language_mode(db, text_session))
        voice = create_voice(
            db,
            scopes.child_user(row),
            row.child_agent_id,
            body.request_key,
            request.app.state.clock(),
            request.app.state.settings,
        )
        parent = db.get(AgentSession, voice.session_id)
        voice.call_deadline = min(voice.call_deadline, row.expires_at)
        parent.expires_at = min(parent.expires_at, row.expires_at)
        # Text confirmations cannot authorize a spoken action.
        if text_session.transport_mode == "text" and text_session.status == "active":
            hold = (text_session.checkpoint or {}).get("hold")
            if hold:
                store.release_hold(
                    str(row.child_tenant_id),
                    hold["provider"],
                    str(text_session.id),
                    hold["hold_id"],
                )
            text_session.revision += 1
            text_session.status = "ended"
            text_session.revoked_at = request.app.state.clock()
            text_session.token_jti_hash = ""
            text_session.checkpoint = {"revision": text_session.revision, "ended": True}
            store.repair(str(text_session.id), text_session.checkpoint)
        db.flush()
        tenant_context(db, row.tenant_id)
        row.voice_session_id = parent.id
        db.flush()
        tenant_context(db, row.child_tenant_id)
        if voice.status != "provisioning" or not _reserve(
            store, parent, voice, request.app.state.clock()
        ):
            return _snapshot(parent, voice, request)
        voice.delivery = {"provisioning_started_at": request.app.state.clock().isoformat()}
        sid, room, generation, child = (
            parent.id,
            voice.room,
            voice.generation,
            scopes.child_user(row),
        )
    return _provision(
        request,
        child,
        sid,
        room,
        generation,
        internal_auth=lambda db: voice_scope(db, request, user, identity),
    )


@router.get("/tests/{identity}/voice", response_model=VoiceSnapshot)
def voice_inspect(
    identity: UUID, request: Request, user: Principal = Depends(manager), auth_db=Depends(session)
):
    from .voice_routes import _expired, _language_mode, _owned_pair, _ready, _snapshot, _terminate

    auth_db.commit()
    expire(request, user, identity)
    with request.app.state.session_factory() as db, db.begin():
        row = voice_scope(db, request, user, identity)
        if row.voice_session_id is None:
            raise AppError("not_found", 404)
        child, sid = scopes.child_user(row), row.voice_session_id
        parent, voice = _owned_pair(db, child, sid)
        if not _expired(parent, voice, request.app.state.clock()):
            if voice.status == "active":
                _ready(request, _language_mode(db, parent))
            return _snapshot(parent, voice, request)
    return _terminate(request, child, sid)


@router.post("/tests/{identity}/voice/end", response_model=VoiceSnapshot)
def voice_end(
    identity: UUID, request: Request, user: Principal = Depends(manager), auth_db=Depends(session)
):
    from .voice_routes import _owned_pair, _snapshot

    auth_db.commit()
    scopes.finish(request, user, identity)
    with request.app.state.session_factory() as db, db.begin():
        row = scopes.owned(db, user, identity, request.app.state.clock(), active=False)
        if row.voice_session_id is None:
            raise AppError("not_found", 404)
        parent, voice = _owned_pair(db, scopes.child_user(row), row.voice_session_id)
        return _snapshot(parent, voice, request)


@router.post("/tests/{identity}/voice/resume", response_model=VoiceSnapshot)
def voice_resume(
    identity: UUID, request: Request, user: Principal = Depends(manager), auth_db=Depends(session)
):
    from .voice_routes import _language_mode, _owned_pair, _provision, _ready, _reserve
    from .voice_sessions import resume_voice

    auth_db.commit()
    expire(request, user, identity)
    with request.app.state.session_factory() as db, db.begin():
        row = voice_scope(db, request, user, identity)
        if row.voice_session_id is None:
            raise AppError("not_found", 404)
        child, sid = scopes.child_user(row), row.voice_session_id
        parent, _ = _owned_pair(db, child, sid)
        store = _ready(request, _language_mode(db, parent))
        hold = (parent.checkpoint or {}).get("hold")
        voice = resume_voice(db, child, sid, request.app.state.clock())
        if hold:
            store.release_hold(
                str(row.child_tenant_id), hold["provider"], str(sid), hold["hold_id"]
            )
        _reserve(store, parent, voice, request.app.state.clock())
        voice.status = "provisioning"
        voice.delivery = {"provisioning_started_at": request.app.state.clock().isoformat()}
        room, generation = voice.room, voice.generation
    return _provision(
        request,
        child,
        sid,
        room,
        generation,
        internal_auth=lambda db: voice_scope(db, request, user, identity),
    )
