"""Pure HTTP lifecycle tests; SQL/Valkey isolation still needs integration tests."""

import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from scheduler.db import Principal, principal, session
from scheduler.errors import AppError

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


class Redis:
    def __init__(self):
        self.values = {"voice:ready": json.dumps({"en": True, "ur": True})}
        self.queue = []
        self.queue_failure = False

    def set(self, key, value, ex):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)

    def scan_iter(self, match, count):
        return iter([key for key in self.values if key.startswith(match[:-1])])

    def get(self, key):
        return self.values.get(key)

    def exists(self, key):
        return key in self.values

    def ttl(self, key):
        return 15 if key in self.values else -2

    def eval(self, script, count, key, owner, *args):
        if args:
            if self.values.get(key) == owner:
                return 2
            if self.values.get(key) not in (None, owner):
                return 0
            self.values[key] = owner
            return 1
        if self.values.get(key) == owner:
            del self.values[key]
            return 1
        return 0

    def rpush(self, key, value):
        assert key == "voice:queue"
        if self.queue_failure:
            raise ValkeyConnectionError("synthetic queue failure")
        self.queue.append(json.loads(value))


class Database:
    def __init__(self, harness):
        self.harness = harness
        self.locked = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.locked = False

    @contextmanager
    def begin(self):
        self.locked = True
        saved = deepcopy(vars(self.harness.voice))
        try:
            yield self
        except Exception:
            vars(self.harness.voice).update(saved)
            raise
        finally:
            self.locked = False

    def commit(self):
        self.locked = False

    def get(self, _model, identity):
        if identity == self.harness.version.id:
            return self.harness.version
        assert identity == self.harness.parent.id
        return self.harness.parent

    def flush(self):
        pass

    def scalars(self, query):
        self.harness.agent_query = query
        return []


@pytest.fixture
def harness(monkeypatch):
    import brain.voice_routes as routes

    staff = Principal(uuid4(), uuid4(), "synthetic@example.com", "staff")
    parent = SimpleNamespace(
        id=uuid4(),
        tenant_id=staff.tenant_id,
        agent_id=uuid4(),
        revision=0,
        checkpoint={
            "revision": 0,
            "transport_mode": "voice",
            "hold": {
                "provider": str(uuid4()),
                "hold_id": "synthetic-hold",
            },
        },
        status="active",
        revoked_at=None,
        expires_at=NOW + timedelta(minutes=30),
        token_jti_hash="private-runtime-hash",
        lease_until=None,
    )
    voice = SimpleNamespace(
        id=uuid4(),
        session_id=parent.id,
        tenant_id=staff.tenant_id,
        creator_id=staff.id,
        status="provisioning",
        generation=1,
        participant="caller-opaque",
        room="voice-opaque",
        call_deadline=NOW + timedelta(minutes=10),
        worker_id=None,
        worker_nonce_hash=None,
        worker_lease_until=None,
        delivery={"private": "patient"},
        disconnected_at=None,
    )
    version = SimpleNamespace(id=uuid4(), config={"language_mode": "bilingual"})
    parent.version_id = version.id
    h = SimpleNamespace(
        staff=staff, parent=parent, voice=voice, version=version, calls=[], databases=[]
    )
    redis = Redis()
    released = []
    store = SimpleNamespace(
        client=redis,
        release_hold=lambda *args: released.append(args),
        repair=lambda *args: None,
    )
    h.redis, h.released = redis, released

    def factory():
        db = Database(h)
        h.databases.append(db)
        return db

    class Rooms:
        failure = False

        def create(self, room):
            assert not any(db.locked for db in h.databases)
            h.calls.append(("create", room))
            if self.failure:
                raise AppError("voice_unavailable", 503)

        def delete(self, room):
            assert not any(db.locked for db in h.databases)
            h.calls.append(("delete", room))

    h.rooms = Rooms()
    monkeypatch.setattr(routes, "LiveKitRooms", lambda *args, **kwargs: h.rooms)
    monkeypatch.setattr(routes, "create_voice", lambda *args: voice)
    monkeypatch.setattr(routes, "_locked_pair", lambda *args, **kwargs: (parent, voice))
    monkeypatch.setattr(routes, "tenant_context", lambda *args: None)

    def resume(*args):
        if voice.creator_id != staff.id or voice.status != "disconnected":
            raise AppError("voice_unavailable", 409)
        voice.status = "active"
        voice.disconnected_at = None
        return voice

    monkeypatch.setattr(routes, "resume_voice", resume)
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        voice_enabled=True,
        livekit_api_key="synthetic-key",
        livekit_api_secret=SecretStr("synthetic-livekit-secret-000000000000"),
        livekit_url="ws://127.0.0.1:7880",
    )
    app.state.brain_store = store
    app.state.clock = lambda: NOW
    app.state.session_factory = factory
    app.dependency_overrides[principal] = lambda: staff
    app.dependency_overrides[session] = lambda: Database(h)

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(exc.body(), status_code=exc.status)

    app.include_router(routes.router)
    h.app, h.client = app, TestClient(app)
    return h


def start(h):
    return h.client.post(
        "/voice/sessions",
        json={
            "agent_id": str(h.parent.agent_id),
            "request_key": "synthetic-request",
        },
    )


def test_create_provisions_outside_locks_and_exposes_media_only(harness):
    h = harness
    response = start(h)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["media"]["participant"] == "caller-opaque"
    assert set(body) == {"session_id", "status", "generation", "revision", "call_deadline", "media"}
    assert "runtime" not in response.text and "worker" not in response.text
    assert h.calls == [("create", "voice-opaque")]
    assert h.redis.queue == [{"tenant_id": str(h.staff.tenant_id), "voice_id": str(h.voice.id)}]
    assert h.redis.values["voice:capacity"] == str(h.parent.id)


def test_idempotent_active_creation_does_not_require_another_capacity_slot(harness):
    h = harness
    first = start(h)
    assert first.status_code == 201
    replay = start(h)
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert len(h.calls) == 1
    assert len(h.redis.queue) == 1


@pytest.mark.parametrize("disable", ["configuration", "heartbeat", "persistent-key"])
def test_unready_pipeline_never_provisions_or_advertises_working_grants(harness, disable):
    h = harness
    if disable == "configuration":
        h.app.state.settings.voice_enabled = False
    elif disable == "heartbeat":
        h.redis.values.clear()
    else:
        h.redis.ttl = lambda _key: -1
    response = start(h)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "voice_unavailable"
    assert not h.calls
    assert not h.redis.queue


def test_global_capacity_busy_is_generic_and_preserves_other_owner(harness):
    h = harness
    h.redis.values["voice:capacity"] = "unrelated-session"
    response = start(h)
    assert response.status_code == 503
    assert "unrelated-session" not in response.text
    assert h.redis.values["voice:capacity"] == "unrelated-session"
    assert not h.calls


@pytest.mark.parametrize("failure", ["room", "queue"])
def test_failed_provisioning_revokes_and_compensates_without_usable_grant(harness, failure):
    h = harness
    h.rooms.failure = failure == "room"
    h.redis.queue_failure = failure == "queue"
    response = start(h)
    assert response.status_code == 503
    assert h.voice.status == "failed"
    assert h.parent.revoked_at == NOW
    assert h.voice.worker_nonce_hash is None
    assert h.voice.delivery is None
    assert "voice:capacity" not in h.redis.values
    assert h.calls[-1] == ("delete", "voice-opaque")
    assert len(h.released) == 1


def test_end_releases_hold_fences_worker_and_keeps_other_capacity_owner(harness):
    h = harness
    h.voice.status = "active"
    h.redis.values["voice:capacity"] = "another-session"
    h.redis.values.pop("voice:ready")
    response = h.client.post(f"/voice/sessions/{h.parent.id}/end")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ended"
    assert response.json()["media"] is None
    assert h.parent.revoked_at == NOW
    assert h.parent.checkpoint["ended"] is True
    assert h.voice.generation == 2
    assert h.voice.worker_nonce_hash is None
    assert h.redis.values["voice:capacity"] == "another-session"
    assert h.released == [
        (str(h.staff.tenant_id), h.released[0][1], str(h.parent.id), "synthetic-hold")
    ]
    assert h.calls == [("delete", "voice-opaque")]


@pytest.mark.parametrize("kind", ["creator", "tenant"])
@pytest.mark.parametrize("suffix", ["", "/end", "/resume"])
def test_other_creator_or_tenant_cannot_read_end_or_resume(harness, kind, suffix):
    h = harness
    if kind == "creator":
        h.voice.creator_id = uuid4()
    else:
        h.voice.tenant_id = uuid4()
    path = f"/voice/sessions/{h.parent.id}" + suffix
    response = h.client.get(path) if not suffix else h.client.post(path)
    assert response.status_code == 404
    assert not h.calls


def test_resume_reprovisions_before_new_grant(harness):
    h = harness
    h.voice.status = "disconnected"
    h.voice.disconnected_at = NOW - timedelta(seconds=1)
    response = h.client.post(f"/voice/sessions/{h.parent.id}/resume")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert h.calls == [("create", "voice-opaque")]
    assert len(h.redis.queue) == 1


def test_duplicate_pending_provision_does_not_race_or_compensate_owner(harness):
    h = harness
    h.redis.values["voice:capacity"] = str(h.parent.id)
    response = start(h)
    assert response.status_code == 201
    assert response.json()["status"] == "provisioning"
    assert response.json()["media"] is None
    assert not h.calls
    assert not h.redis.queue
    assert h.voice.status == "provisioning"


def test_newer_live_generation_is_not_revoked_by_late_provision_result(harness):
    h = harness

    def newer_generation(_room):
        h.voice.generation += 1
        h.voice.status = "active"

    h.rooms.create = newer_generation
    response = start(h)
    assert response.status_code == 503
    assert h.voice.status == "active"
    assert h.voice.generation == 2
    assert h.parent.revoked_at is None
    assert h.redis.values["voice:capacity"] == str(h.parent.id)
    assert not h.calls


def test_readiness_loss_during_provision_revokes_before_grant(harness):
    h = harness
    h.rooms.create = lambda _room: h.redis.values.pop("voice:ready")
    response = start(h)
    assert response.status_code == 503
    assert h.voice.status == "failed"
    assert h.parent.revoked_at == NOW
    assert "voice:capacity" not in h.redis.values
    assert not h.redis.queue


def test_agent_list_requires_readiness_and_returns_tenant_catalog_envelope(harness):
    h = harness
    response = h.client.get("/voice/agents")
    assert response.status_code == 200
    assert response.json() == {"agents": []}
    assert h.agent_query.compile().params["tenant_id_1"] == h.staff.tenant_id
    h.redis.values.pop("voice:ready")
    assert h.client.get("/voice/agents").status_code == 200


@pytest.mark.parametrize("operation", ["start", "resume"])
def test_urdu_only_agent_fails_honestly_when_worker_has_no_urdu(harness, operation):
    h = harness
    h.version.config["language_mode"] = "ur"
    h.redis.values["voice:ready"] = json.dumps({"en": True, "ur": False})
    if operation == "resume":
        h.voice.status = "disconnected"
        response = h.client.post(f"/voice/sessions/{h.parent.id}/resume")
    else:
        response = start(h)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "voice_urdu_unavailable"
    assert not h.calls


def test_bilingual_can_start_with_english_ready_and_urdu_unavailable(harness):
    h = harness
    h.redis.values["voice:ready"] = json.dumps({"en": True, "ur": False})
    assert start(h).status_code == 201


def test_inspect_cleans_orphaned_provisioning_after_30_seconds(harness):
    h = harness
    h.voice.delivery = {"provisioning_started_at": (NOW - timedelta(seconds=30)).isoformat()}
    h.redis.values["voice:capacity"] = str(h.parent.id)
    response = h.client.get(f"/voice/sessions/{h.parent.id}")
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert h.parent.revoked_at == NOW
    assert "voice:capacity" not in h.redis.values
    assert h.calls == [("delete", "voice-opaque")]


def test_real_sql_staff_voice_isolation_receipts_and_compensation(seed, database, monkeypatch):
    """Dedicated stores: real SQL/RLS and real atomic Valkey operations, fake media HTTP only."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    import brain.voice_routes as routes
    from scheduler.config import Settings
    from scheduler.main import create_app
    from scheduler.models import AgentSession, VoiceSession
    from tests_api.conftest import APP_URL, PASSWORD, SECRET
    from tests_api.conftest import NOW as SQL_NOW

    configuration = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="synthetic-distinct-runtime-test-secret-2026",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        livekit_api_key="synthetic-key",
        livekit_api_secret="synthetic-distinct-livekit-test-secret-2026",
        voice_enabled=True,
        _env_file=None,
    )
    app = create_app(configuration, clock=lambda: SQL_NOW)
    calls = []

    class Rooms:
        failure = False

        def create(self, room):
            # A fresh SQL transaction must be able to lock the row immediately.
            with Session(database) as probe, probe.begin():
                probe.scalar(
                    select(VoiceSession)
                    .where(VoiceSession.room == room)
                    .with_for_update(nowait=True)
                )
            calls.append(("create", room))
            if self.failure:
                raise AppError("voice_unavailable", 503)

        def delete(self, room):
            calls.append(("delete", room))

    rooms = Rooms()
    monkeypatch.setattr(routes, "LiveKitRooms", lambda *_args: rooms)
    redis = app.state.brain_store.client
    redis.delete("voice:capacity", "voice:queue", "voice:ready")
    redis.set("voice:ready", json.dumps({"en": True, "ur": False}), ex=15)
    try:
        with TestClient(app) as client:

            def login(email):
                response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
                assert response.status_code == 200, response.text
                return {"Authorization": "Bearer " + response.json()["access_token"]}

            owner = login("owner-a@example.com")
            stranger = login("staff-a@example.com")
            other_tenant = login("owner-b@example.com")
            catalog = client.get("/voice/agents", headers=owner)
            assert catalog.status_code == 200, catalog.text
            assert [row["id"] for row in catalog.json()["agents"]] == [str(seed["agent_a"])]
            assert (
                client.post(
                    "/voice/sessions",
                    headers=owner,
                    json={
                        "agent_id": str(seed["agent_b"]),
                        "request_key": "cross-tenant",
                    },
                ).status_code
                == 404
            )
            payload = {"agent_id": str(seed["agent_a"]), "request_key": "real-sql-call"}
            created = client.post("/voice/sessions", headers=owner, json=payload)
            assert created.status_code == 201, created.text
            body = created.json()
            identity = body["session_id"]
            assert body["status"] == "active"
            assert "runtime" not in created.text
            assert client.post("/voice/sessions", headers=owner, json=payload).json() == body
            assert [kind for kind, _ in calls] == ["create"]
            for headers in (stranger, other_tenant):
                assert client.get(f"/voice/sessions/{identity}", headers=headers).status_code == 404
                assert (
                    client.post(f"/voice/sessions/{identity}/end", headers=headers).status_code
                    == 404
                )
            media_headers = {"Authorization": "Bearer " + body["media"]["token"]}
            assert client.get("/voice/agents", headers=media_headers).status_code == 401
            busy = client.post(
                "/voice/sessions",
                headers=other_tenant,
                json={
                    "agent_id": str(seed["agent_b"]),
                    "request_key": "busy",
                },
            )
            assert busy.status_code == 503, busy.text
            assert client.post(f"/voice/sessions/{identity}/end", headers=owner).status_code == 200
            assert not redis.exists("voice:capacity")
            rooms.failure = True
            failed = client.post(
                "/voice/sessions",
                headers=owner,
                json={
                    "agent_id": str(seed["agent_a"]),
                    "request_key": "room-failure",
                },
            )
            assert failed.status_code == 503, failed.text
            with Session(database) as db:
                voices = list(
                    db.scalars(
                        select(VoiceSession).where(VoiceSession.tenant_id == seed["tenant_a"])
                    )
                )
                assert sorted(row.status for row in voices) == ["ended", "failed"]
                assert all(db.get(AgentSession, row.session_id).revoked_at for row in voices)
            assert not redis.exists("voice:capacity")
    finally:
        # Only dedicated test keys; never operate on development Redis.
        from valkey import Valkey

        cleanup = Valkey.from_url(configuration.valkey_url.get_secret_value())
        cleanup.delete("voice:capacity", "voice:queue", "voice:ready")
        cleanup.close()


def test_disconnect_grace_expiry_is_a_cleanup_deadline(harness):
    h = harness
    h.voice.status = "disconnected"
    h.voice.disconnected_at = NOW - timedelta(seconds=31)
    h.redis.values["voice:capacity"] = str(h.parent.id)
    response = h.client.get(f"/voice/sessions/{h.parent.id}")
    assert response.status_code == 200
    assert response.json()["status"] == "ended"
    assert h.parent.revoked_at == NOW
    assert "voice:capacity" not in h.redis.values


def test_cleanup_sweeper_reclaims_grace_expiry_without_a_browser(harness):
    from brain.voice_cleanup import sweep, track

    h = harness
    h.voice.status = "disconnected"
    h.voice.disconnected_at = NOW - timedelta(seconds=31)
    h.redis.values["voice:capacity"] = str(h.parent.id)
    track(h.app.state.brain_store, h.staff.tenant_id, h.parent.id, h.staff.id)
    assert sweep(h.app) == 1
    assert h.voice.status == "ended"
    assert "voice:capacity" not in h.redis.values
