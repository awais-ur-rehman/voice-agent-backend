"""Staff session issuance and capability-scoped text simulator transport."""

import re
from copy import deepcopy
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import select
from valkey.exceptions import ValkeyError

from scheduler.contracts import Contract
from scheduler.db import Principal, bearer, principal, session
from scheduler.errors import AppError
from scheduler.models import (
    AgentSession,
    AgentVersion,
    CommandReceipt,
    Provider,
    Schedule,
    Service,
    Tenant,
)

from .contracts import Action
from .dialogue import affirmative, subject_change
from .language import language_transition
from .provider import ProviderError
from .security import RuntimeActor, decode_runtime, runtime_secret
from .sessions import command, create_session, renew_session
from .tools import ToolEngine

router = APIRouter(tags=["Text agent"])


class SessionCreate(Contract):
    agent_id: UUID


class Command(Contract):
    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    action: Action
    text: str = Field(default="", max_length=2000)


class Turn(Contract):
    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=2000)
    live: bool = True


class Event(Contract):
    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    kind: str = Field(pattern="^(delivery_interrupted|upstream_timeout|silence)$")
    delivered_text: str = Field(default="", max_length=2000)


def require_store(request):
    store = getattr(request.app.state, "brain_store", None)
    if store is None:
        raise AppError("service_unavailable", 503)
    return store


def auth_runtime(request, db, credentials, identity):
    if credentials is None:
        raise AppError("unauthorized", 401)
    actor = decode_runtime(
        db, credentials.credentials, request.app.state.settings, request.app.state.clock()
    )
    if actor.id != identity:
        raise AppError("unauthorized", 401)
    return actor


def repair(store, identity, state):
    try:
        if state.get("ended"):
            store.repair(str(identity), {"revision": state["revision"], "ended": True})
        else:
            store.repair(str(identity), state)
        return True
    except ValkeyError:
        return False


@router.post("/agent-sessions", status_code=201)
def start(
    body: SessionCreate, request: Request, user: Principal = Depends(principal), db=Depends(session)
):
    store = require_store(request)
    created = create_session(
        db, user, body.agent_id, request.app.state.clock(), request.app.state.settings
    )
    db.commit()
    created["cache_ready"] = repair(store, created["session_id"], created["state"])
    return created


@router.post("/agent-sessions/{identity}/resume")
def staff_resume(
    identity: UUID, request: Request, user: Principal = Depends(principal), db=Depends(session)
):
    row = db.scalar(select(AgentSession).where(AgentSession.id == identity).with_for_update())
    if row is None:
        raise AppError("not_found", 404)
    actor = RuntimeActor(row.id, row.tenant_id, row.agent_id, row.version_id)
    created = renew_session(
        db,
        actor,
        request.app.state.clock(),
        request.app.state.settings,
        clock=request.app.state.clock,
    )
    db.commit()
    repair(require_store(request), identity, created["state"])
    return created


@router.get("/runtime/sessions/{identity}")
def inspect_session(identity: UUID, request: Request, credentials=Depends(bearer)):
    store = require_store(request)
    with request.app.state.session_factory() as db, db.begin():
        actor = auth_runtime(request, db, credentials, identity)
        row = db.get(AgentSession, actor.id)
        state = deepcopy(row.checkpoint)
    repair(store, identity, state)
    return {"session_id": str(identity), "revision": state["revision"], "state": state}


@router.post("/runtime/sessions/{identity}/renew")
def renew(identity: UUID, request: Request, credentials=Depends(bearer)):
    with request.app.state.session_factory() as db, db.begin():
        actor = auth_runtime(request, db, credentials, identity)
        created = renew_session(
            db,
            actor,
            request.app.state.clock(),
            request.app.state.settings,
            clock=request.app.state.clock,
        )
    repair(require_store(request), identity, created["state"])
    return created


def run_command(identity, body, request, credentials, *, live=False, event=False):
    store = require_store(request)
    factory = request.app.state.session_factory
    settings = request.app.state.settings
    now = request.app.state.clock()
    payload = body.model_dump(mode="json")
    # Complete snapshot transaction before network inference. Never hold SQL locks across NIM.
    with factory() as db, db.begin():
        actor = auth_runtime(request, db, credentials, identity)
        row = db.get(AgentSession, identity)
        state = deepcopy(row.checkpoint)
        version = db.get(AgentVersion, actor.version_id)
        config = version.config
        existing = db.scalar(
            select(CommandReceipt).where(
                CommandReceipt.session_id == identity, CommandReceipt.command_id == body.command_id
            )
        )
        if not existing and row.revision != body.expected_revision:
            raise AppError("idempotency_conflict", 409)
        tenant = db.get(Tenant, actor.tenant_id)
        facts = {
            "clinic": {
                "name": tenant.name,
                "timezone": tenant.timezone,
                "notice_min": tenant.min_notice_min,
                "cancellation_min": tenant.cancellation_min,
                "profile": config.get("clinic_profile", {}),
            },
            "hours": [
                {
                    "weekday": r.weekday,
                    "start": r.start_time.isoformat(),
                    "end": r.end_time.isoformat(),
                }
                for r in db.scalars(select(Schedule))
            ],
            "providers": [
                {"id": str(p.id), "name": p.name, "name_ur": p.name_ur}
                for p in db.scalars(select(Provider).where(Provider.active.is_(True)))
            ],
            "services": [
                {"id": str(p.id), "name": p.name, "name_ur": p.name_ur}
                for p in db.scalars(select(Service).where(Service.active.is_(True)))
            ],
        }
    action = None
    failed = False
    provider_requests = 0
    if not existing and live:
        text = body.text.strip()
        # Identity factors are consumed directly and never sent to an external model.
        if any(
            word in text.casefold()
            for word in (
                "emergency",
                "chest pain",
                "cannot breathe",
                "can't breathe",
                "difficulty breathing",
                "سانس نہیں",
                "سینے میں درد",
                "ہنگامی",
            )
        ):
            action = {"name": "request_handoff", "arguments": {"reason": "emergency"}}
        elif subject_change(text):
            action = {
                "name": "update_dialogue_state",
                "arguments": {"subject": subject_change(text)},
            }
        elif any(word in text.casefold() for word in ("fee", "fees", "فیس")):
            action = {"name": "get_clinic_info", "arguments": {"topic": "fees"}}
        elif any(
            word in text.casefold()
            for word in ("opening hours", "clinic hours", "کلینک کے اوقات", "کھلنے کے اوقات")
        ):
            action = {"name": "get_clinic_info", "arguments": {"topic": "hours"}}
        elif (
            state.get("candidates")
            and not state.get("verified")
            and not any(
                word in text.casefold() for word in ("cancel", "reschedule", "منسوخ", "تبدیل")
            )
        ):
            action = {"name": "verify_patient", "arguments": {"factor": text}}
        elif re.fullmatch(r"[+\d ()-]{9,40}", text) and len(re.sub(r"\D", "", text)) >= 10:
            action = {"name": "find_patient", "arguments": {"phone": text}}
        elif state.get("lookup_empty") and state.get("lookup_phone") and not state.get("readback"):
            pending = state.get("pending_patient", {})
            args = {"name": pending.get("name", text), "phone": state["lookup_phone"]}
            if re.search(r"[\u0600-\u06ff]", text):
                args["name_ur"] = text
            action = {"name": "prepare_patient", "arguments": args}
        elif state.get("readback") and affirmative(text):
            action = {"name": "confirm", "arguments": {}}
        else:
            try:
                provider_requests = 1
                action = request.app.state.brain_planner.plan(
                    state, text, config, facts
                ).model_dump(mode="json")
                # Date spans are literal extraction, not planner-computed values.
                phrase = action["arguments"].get("date_phrase")
                if phrase and phrase not in text:
                    raise ProviderError("nonliteral_date", requests=1)
            except ProviderError as exc:
                provider_requests = getattr(exc, "requests", provider_requests)
                failed = True
    elif not live and not event:
        action = body.action.model_dump(mode="json")
    with factory() as db, db.begin():
        actor = auth_runtime(request, db, credentials, identity)

        def execute(db, s):
            try:
                cached = store.load(str(identity))
            except ValkeyError:
                cached = None
            if cached and cached["revision"] == s["revision"]:
                s["history"] = cached.get("history", [])
                s["summary"] = cached.get("summary", {})
            text = getattr(body, "text", "")
            s["lang"], s["lang_pending"], s["lang_streak"] = language_transition(
                s.get("lang", "en"),
                s.get("lang_pending"),
                s.get("lang_streak", 0),
                text,
                config.get("language_mode", "bilingual"),
            )
            engine = ToolEngine(
                db, actor, store, request.app.state.clock(), clock=request.app.state.clock
            )
            if event:
                return engine.trusted_event(s, body.kind, delivered_text=body.delivered_text)
            if failed:
                return engine.trusted_event(s, "upstream_timeout")
            return engine.execute(s, action, text)

        response, next_state = command(
            db,
            actor,
            body.command_id,
            body.expected_revision,
            payload,
            now,
            runtime_secret(settings),
            execute,
            clock=request.app.state.clock,
        )
    cache_ready = repair(store, identity, next_state)
    return {
        "result": response,
        "revision": next_state["revision"],
        "state": next_state,
        "cache_ready": cache_ready,
        "provider_requests": provider_requests,
        "executed_action": action["name"] if action else None,
    }


@router.post("/runtime/sessions/{identity}/commands")
def commands(identity: UUID, body: Command, request: Request, credentials=Depends(bearer)):
    return run_command(identity, body, request, credentials)


@router.post("/runtime/sessions/{identity}/turn")
def turn(identity: UUID, body: Turn, request: Request, credentials=Depends(bearer)):
    if not body.live:
        raise AppError("validation_error", 422)
    return run_command(identity, body, request, credentials, live=True)


@router.post("/runtime/sessions/{identity}/events")
def events(identity: UUID, body: Event, request: Request, credentials=Depends(bearer)):
    return run_command(identity, body, request, credentials, event=True)
