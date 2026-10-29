"""Staff-only browser grants; external room provisioning runs outside SQL locks."""

import json
import math
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from valkey.exceptions import ValkeyError

from scheduler.contracts import Contract
from scheduler.db import Principal, principal, session, tenant_context
from scheduler.errors import AppError
from scheduler.models import Agent, AgentSession, AgentVersion

from .voice_contracts import MediaGrant, VoiceCreate, VoiceSnapshot
from .voice_rooms import LiveKitRooms
from .voice_sessions import _locked_pair, create_voice, resume_voice
from .voice_tokens import media_token

router = APIRouter(prefix="/voice", tags=["Browser voice"])

_RESERVE_CAPACITY = """
local owner = redis.call('GET', KEYS[1])
if owner == ARGV[1] then return 2 end
if owner then return 0 end
return redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX') and 1 or 0
"""
_RELEASE_CAPACITY = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _store(request):
    store = getattr(request.app.state, "brain_store", None)
    if store is None:
        raise AppError("voice_unavailable", 503)
    return store


def _ready(request, language_mode=None):
    store = _store(request)
    try:
        if (
            not getattr(request.app.state.settings, "voice_enabled", False)
            or not 0 < store.client.ttl("voice:ready") <= 15
        ):
            raise AppError("voice_unavailable", 503)
        capabilities = json.loads(store.client.get("voice:ready"))
        if (
            not isinstance(capabilities, dict)
            or type(capabilities.get("en")) is not bool
            or type(capabilities.get("ur")) is not bool
            or not (capabilities["en"] or capabilities["ur"])
        ):
            raise AppError("voice_unavailable", 503)
        if isinstance(language_mode, dict):
            from .agent_config import AgentConfig

            config = AgentConfig.model_validate(language_mode)
            required = (
                ("en", "ur") if config.language_mode == "bilingual" else (config.language_mode,)
            )
            fingerprints = capabilities.get("profiles", [])
            if not isinstance(fingerprints, list) or any(
                not capabilities[language]
                or config.voice_profiles[language]["config_fingerprint"] not in fingerprints
                for language in required
            ):
                raise AppError("voice_unavailable", 503)
            language_mode = config.language_mode
        if language_mode == "ur" and not capabilities["ur"]:
            raise AppError("voice_urdu_unavailable", 503)
        if language_mode in {"en", "bilingual"} and not capabilities["en"]:
            raise AppError("voice_unavailable", 503)
    except (ValkeyError, ValueError, TypeError):
        raise AppError("voice_unavailable", 503) from None
    return store


def _language_mode(db, parent):
    version = db.get(AgentVersion, parent.version_id)
    if version is None:
        raise AppError("voice_unavailable", 503)
    return (
        version.config
        if version.config.get("schema_version") == 2
        else version.config.get("language_mode", "bilingual")
    )


def _expired(parent, voice, now):
    if now >= min(parent.expires_at, voice.call_deadline):
        return True
    if (
        voice.status == "active"
        and voice.worker_id is not None
        and voice.worker_lease_until is not None
        and now >= voice.worker_lease_until + timedelta(seconds=30)
    ):
        return True
    if voice.status == "disconnected":
        return voice.disconnected_at is None or now >= voice.disconnected_at + timedelta(seconds=30)
    if parent.status == "ended" and voice.status == "active":
        started = (voice.delivery or {}).get("terminal_started_at")
        return started is None or now >= datetime.fromisoformat(started) + timedelta(seconds=30)
    if voice.status == "provisioning":
        try:
            started = (voice.delivery or {}).get("provisioning_started_at")
            started = (
                datetime.fromisoformat(started) if started else getattr(voice, "created_at", now)
            )
            return now >= started + timedelta(seconds=30)
        except (TypeError, ValueError):
            return True
    return False


def _owned_pair(db, user, identity):
    parent, voice = _locked_pair(db, user.tenant_id, session_id=identity)
    if (
        parent.id != identity
        or voice.session_id != identity
        or voice.creator_id != user.id
        or voice.tenant_id != user.tenant_id
        or parent.tenant_id != user.tenant_id
    ):
        raise AppError("not_found", 404)
    return parent, voice


def _snapshot(parent, voice, request):
    now = request.app.state.clock()
    media = None
    if (
        voice.status == "active"
        and parent.status == "active"
        and parent.revoked_at is None
        and now < min(parent.expires_at, voice.call_deadline)
        and (
            voice.worker_id is None
            or voice.worker_lease_until is not None
            and now < voice.worker_lease_until
        )
    ):
        settings = request.app.state.settings
        media = MediaGrant(
            url=settings.livekit_url,
            room=voice.room,
            participant=voice.participant,
            token=media_token(settings, voice.room, voice.participant, now),
            expires_at=min(now + timedelta(seconds=120), voice.call_deadline),
            session_id=parent.id,
            generation=voice.generation,
        )
    return VoiceSnapshot(
        session_id=parent.id,
        status=voice.status,
        generation=voice.generation,
        revision=parent.revision,
        call_deadline=voice.call_deadline,
        media=media,
    )


def _reserve(store, parent, voice, now):
    remaining = min(600, math.ceil((voice.call_deadline - now).total_seconds()))
    if remaining <= 0:
        raise AppError("voice_unavailable", 503)
    try:
        reserved = store.client.eval(
            _RESERVE_CAPACITY, 1, "voice:capacity", str(parent.id), remaining
        )
        if not reserved:
            raise AppError("voice_unavailable", 503)
        from .voice_cleanup import track

        track(store, parent.tenant_id, parent.id, voice.creator_id)
        return reserved == 1
    except ValkeyError:
        raise AppError("voice_unavailable", 503) from None


def _release_capacity(store, identity):
    store.client.eval(_RELEASE_CAPACITY, 1, "voice:capacity", str(identity))


def _end_locked(parent, voice, store, now, status, *, release_hold=True):
    cleanup_failed = False
    if voice.status not in {"ended", "failed"}:
        state = deepcopy(parent.checkpoint or {})
        hold = state.pop("hold", None)
        if hold and release_hold:
            try:
                store.release_hold(
                    str(parent.tenant_id), hold["provider"], str(parent.id), hold["hold_id"]
                )
            except ValkeyError:
                cleanup_failed = True
        parent.revision += 1
        parent.checkpoint = {
            "revision": parent.revision,
            "ended": True,
            "transport_mode": "voice",
        }
        parent.status = "ended"
        parent.revoked_at = now
        parent.token_jti_hash = ""
        parent.lease_until = now
        voice.generation += 1
        voice.status = status
        voice.worker_id = None
        voice.worker_nonce_hash = None
        voice.worker_lease_until = now
        voice.delivery = None
    return cleanup_failed


def _terminate(request, user, identity, *, status="ended", expected_generation=None):
    store = _store(request)
    with request.app.state.session_factory() as db, db.begin():
        parent, voice = _owned_pair(db, user, identity)
        # A late failed provisioner cannot revoke/delete a newer live generation.
        if expected_generation is not None and voice.generation != expected_generation:
            return None
        failed = _end_locked(parent, voice, store, request.app.state.clock(), status)
        room = voice.room
        snapshot = _snapshot(parent, voice, request)
    try:
        LiveKitRooms(request.app.state.settings).delete(room)
    except AppError:
        failed = True
    try:
        _release_capacity(store, identity)
        store.repair(
            str(identity),
            {
                "revision": snapshot.revision,
                "ended": True,
            },
        )
    except ValkeyError:
        failed = True
    if failed:
        raise AppError("voice_unavailable", 503)
    from .voice_cleanup import PREFIX

    store.client.delete(PREFIX + str(identity))
    return snapshot


def _provision(request, user, identity, room, generation, *, internal_auth=None):
    try:
        LiveKitRooms(request.app.state.settings).create(room)
        with request.app.state.session_factory() as db, db.begin():
            if internal_auth:
                internal_auth(db)
            parent, voice = _owned_pair(db, user, identity)
            now = request.app.state.clock()
            if (
                voice.status != "provisioning"
                or voice.generation != generation
                or parent.status != "active"
                or parent.revoked_at is not None
                or now >= min(parent.expires_at, voice.call_deadline)
            ):
                raise AppError("voice_unavailable", 503)
            _ready(request, _language_mode(db, parent))
            voice.status = "active"
            voice.delivery = None
            snapshot = _snapshot(parent, voice, request)
            notification = json.dumps({"tenant_id": str(user.tenant_id), "voice_id": str(voice.id)})
        _store(request).client.rpush("voice:queue", notification)
        return snapshot
    except Exception:
        # SQL revocation commits before room/capacity cleanup. Never expose upstream details.
        try:
            _terminate(request, user, identity, status="failed", expected_generation=generation)
        except Exception:
            pass
        raise AppError("voice_unavailable", 503) from None


class VoiceAgent(Contract):
    id: UUID
    name: str
    language_mode: str


class VoiceAgents(Contract):
    agents: list[VoiceAgent]


@router.get("/agents", response_model=VoiceAgents)
def agents(request: Request, user: Principal = Depends(principal), auth_db=Depends(session)):
    auth_db.commit()
    with request.app.state.session_factory() as db, db.begin():
        tenant_context(db, user.tenant_id)
        rows = db.scalars(
            select(Agent)
            .where(
                Agent.tenant_id == user.tenant_id,
                Agent.enabled.is_(True),
                Agent.active_version_id.is_not(None),
            )
            .order_by(Agent.name, Agent.id)
        )
        output = []
        for agent in rows:
            version = db.get(AgentVersion, agent.active_version_id)
            if (
                version is not None
                and version.tenant_id == user.tenant_id
                and version.agent_id == agent.id
            ):
                output.append(
                    {
                        "id": str(agent.id),
                        "name": agent.name,
                        "language_mode": version.config.get("language_mode", "bilingual"),
                    }
                )
    return {"agents": output}


@router.post("/sessions", response_model=VoiceSnapshot, status_code=201)
def start(
    body: VoiceCreate,
    request: Request,
    user: Principal = Depends(principal),
    auth_db=Depends(session),
):
    auth_db.commit()
    store = _ready(request)
    with request.app.state.session_factory() as db, db.begin():
        voice = create_voice(
            db,
            user,
            body.agent_id,
            body.request_key,
            request.app.state.clock(),
            request.app.state.settings,
        )
        parent = db.get(AgentSession, voice.session_id)
        _ready(request, _language_mode(db, parent))
        if voice.status != "provisioning":
            return _snapshot(parent, voice, request)
        if not _reserve(store, parent, voice, request.app.state.clock()):
            # Another request already owns this in-flight provision. Do not race
            # its room creation or compensate its eventual successful result.
            return _snapshot(parent, voice, request)
        voice.delivery = {"provisioning_started_at": request.app.state.clock().isoformat()}
        identity, room, generation = parent.id, voice.room, voice.generation
    return _provision(request, user, identity, room, generation)


@router.get("/sessions/{identity}", response_model=VoiceSnapshot)
def inspect(
    identity: UUID,
    request: Request,
    user: Principal = Depends(principal),
    auth_db=Depends(session),
):
    auth_db.commit()
    with request.app.state.session_factory() as db, db.begin():
        parent, voice = _owned_pair(db, user, identity)
        if not _expired(parent, voice, request.app.state.clock()):
            if voice.status == "active":
                _ready(request, _language_mode(db, parent))
            return _snapshot(parent, voice, request)
        generation = voice.generation
        status = "failed" if voice.status == "provisioning" else "ended"
    return _terminate(request, user, identity, status=status, expected_generation=generation)


@router.post("/sessions/{identity}/end", response_model=VoiceSnapshot)
def end(
    identity: UUID,
    request: Request,
    user: Principal = Depends(principal),
    auth_db=Depends(session),
):
    auth_db.commit()
    return _terminate(request, user, identity)


@router.post("/sessions/{identity}/resume", response_model=VoiceSnapshot)
def resume(
    identity: UUID,
    request: Request,
    user: Principal = Depends(principal),
    auth_db=Depends(session),
):
    auth_db.commit()
    store = _ready(request)
    with request.app.state.session_factory() as db, db.begin():
        parent, _voice = _owned_pair(db, user, identity)
        _ready(request, _language_mode(db, parent))
        old_hold = deepcopy((parent.checkpoint or {}).get("hold"))
        voice = resume_voice(db, user, identity, request.app.state.clock())
        if old_hold:
            store.release_hold(
                str(parent.tenant_id), old_hold["provider"], str(identity), old_hold["hold_id"]
            )
        _reserve(store, parent, voice, request.app.state.clock())
        voice.status = "provisioning"
        voice.delivery = {"provisioning_started_at": request.app.state.clock().isoformat()}
        room, generation = voice.room, voice.generation
    return _provision(request, user, identity, room, generation)
