"""Real isolated SQL/Valkey worker fencing; no inference or real media claims."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from brain.tools import ToolEngine
from brain.voice_playback import turn_context
from brain.voice_sessions import create_voice
from scheduler.config import Settings
from scheduler.db import Principal
from scheduler.main import create_app
from scheduler.models import AgentSession, VoiceSession
from tests_api.conftest import APP_URL, NOW, PASSWORD, SECRET


def test_worker_http_claim_playback_and_generation_fencing(seed, database, monkeypatch):
    import brain.voice_routes as staff_routes

    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="runtime-worker-route-distinct-secret-2026",
        voice_worker_secret="worker-route-distinct-secret-only-2026",
        voice_bootstrap_secret="bootstrap-worker-route-secret-only-2026",
        livekit_api_key="synthetic-key",
        livekit_api_secret="livekit-worker-route-distinct-secret-2026",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        voice_enabled=True,
        _env_file=None,
    )
    now = [NOW]
    staff = Principal(seed["owner_a"], seed["tenant_a"], "owner-a@example.com", "owner")
    with Session(database) as db:
        voice = create_voice(db, staff, seed["agent_a"], "worker-http-call", NOW, settings)
        voice.status = "active"
        voice_id, session_id, caller = voice.id, voice.session_id, voice.participant
        db.commit()

    app = create_app(settings, clock=lambda: now[0])
    room_deletes = []
    monkeypatch.setattr(
        staff_routes,
        "LiveKitRooms",
        lambda *_args: type(
            "Rooms",
            (),
            {
                "delete": lambda _self, room: room_deletes.append(room),
            },
        )(),
    )
    redis = app.state.brain_store.client
    redis.delete("voice:capacity", "voice:queue", "voice:ready")
    bootstrap = {"Authorization": "Bearer " + settings.voice_bootstrap_secret.get_secret_value()}
    base = f"/voice/worker/sessions/{session_id}"
    try:
        with TestClient(app) as client:
            login = client.post("/auth/login", json={"email": staff.email, "password": PASSWORD})
            staff_headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            claim_body = {
                "tenant_id": str(staff.tenant_id),
                "voice_id": str(voice_id),
                "worker_id": str(uuid4()),
            }
            assert (
                client.post(
                    "/voice/worker/claim", headers=staff_headers, json=claim_body
                ).status_code
                == 401
            )
            claim = client.post("/voice/worker/claim", headers=bootstrap, json=claim_body)
            assert claim.status_code == 200, claim.text
            grant = claim.json()
            assert grant["voice_id"] == str(voice_id)
            assert grant["runtime_token"] and grant["token"]
            assert grant["caller_participant"] == caller
            headers = {
                "Authorization": "Bearer " + grant["token"],
                "X-Runtime-Capability": grant["runtime_token"],
            }
            assert client.get(base, headers=headers).status_code == 200
            assert client.post(base + "/heartbeat", headers=headers).status_code == 200
            interrupted = client.post(
                base + "/interrupt",
                headers=headers,
                json={"command_id": "interrupt-no-planner-lock"},
            )
            assert interrupted.status_code == 200, interrupted.text
            replayed = client.post(
                base + "/interrupt",
                headers=headers,
                json={"command_id": "interrupt-no-planner-lock"},
            )
            assert replayed.status_code == 200
            assert replayed.json()["revision"] == interrupted.json()["revision"]
            assert client.get(base, headers=staff_headers).status_code == 401
            assert (
                client.get(
                    "/voice/agents", headers={"Authorization": "Bearer " + grant["token"]}
                ).status_code
                == 401
            )
            bypass = client.post(
                f"/runtime/sessions/{session_id}/commands",
                headers={
                    "Authorization": "Bearer " + grant["runtime_token"],
                },
                json={
                    "command_id": "raw-text",
                    "expected_revision": 0,
                    "action": {"name": "ask_caller", "arguments": {}},
                    "text": "yes",
                },
            )
            assert bypass.status_code == 403, bypass.text
            import threading
            from concurrent.futures import ThreadPoolExecutor

            from brain.contracts import Action

            started, release = threading.Event(), threading.Event()
            original_planner = app.state.brain_planner.plan

            def slow_planner(*args):
                started.set()
                assert release.wait(3)
                return Action(name="ask_caller", arguments={})

            app.state.brain_planner.plan = slow_planner
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(
                    client.post,
                    base + "/turn",
                    headers=headers,
                    json={
                        "command_id": "planner-before-barge-in",
                        "expected_revision": 1,
                        "text": "Please check the clinic.",
                        "utterance_started_at": NOW.isoformat(),
                        "language": "en",
                        "confidence": 0.99,
                        "uncertain": False,
                    },
                )
                try:
                    assert started.wait(2)
                    fence = client.post(
                        base + "/interrupt",
                        headers=headers,
                        json={"command_id": "barge-in-during-inference"},
                    )
                    assert fence.status_code == 200, fence.text
                    assert not pending.done()
                finally:
                    release.set()
                assert pending.result(timeout=3).status_code == 409
            app.state.brain_planner.plan = original_planner
            readback_revision = client.get(base, headers=headers).json()["revision"] + 1
            with Session(database) as db, db.begin():
                parent = db.get(AgentSession, session_id)
                state = deepcopy(parent.checkpoint)
                state.update(
                    revision=readback_revision,
                    readback={"kind": "appointment", "revision": readback_revision},
                )
                parent.revision, parent.checkpoint = readback_revision, state
            published = {
                "readback_revision": readback_revision,
                "segment_id": "synthetic-one",
                "published_samples": 24000,
                "sample_rate": 24000,
            }
            assert (
                client.post(base + "/publish", headers=headers, json=published).status_code == 200
            )
            ack = {
                "participant": caller,
                "ack": {
                    "version": 1,
                    "session_id": str(session_id),
                    "generation": grant["generation"],
                    "sequence": 1,
                    "segment_id": "synthetic-one",
                    "played_samples": 24000,
                },
            }
            assert client.post(base + "/ack", headers=headers, json=ack).status_code == 422
            now[0] += timedelta(seconds=1)
            wrong_participant = deepcopy(ack)
            wrong_participant["participant"] = "unrelated-caller"
            assert (
                client.post(base + "/ack", headers=headers, json=wrong_participant).status_code
                == 422
            )
            wrong_generation = deepcopy(ack)
            wrong_generation["ack"]["generation"] += 1
            assert (
                client.post(base + "/ack", headers=headers, json=wrong_generation).status_code
                == 422
            )
            assert client.post(base + "/ack", headers=headers, json=ack).status_code == 200
            assert (
                client.post(
                    base + "/finish", headers=headers, json={"readback_revision": readback_revision}
                ).status_code
                == 200
            )
            with Session(database) as db:
                voice = db.get(VoiceSession, voice_id)
                ledger = deepcopy(voice.delivery)
                assert ledger["completed_at"] is not None
                context = turn_context(ledger, voice.generation, NOW)
                assert not ToolEngine(None, None, None, now[0], voice_context=context)._confirmed(
                    state | {"transport_mode": "voice"}, "yes", "appointment"
                )
                later = turn_context(ledger, voice.generation, NOW + timedelta(seconds=2))
                assert ToolEngine(
                    None, None, None, NOW + timedelta(seconds=2), voice_context=later
                )._confirmed(state | {"transport_mode": "voice"}, "yes", "appointment")
            now[0] += timedelta(seconds=1)
            assert client.post(base + "/heartbeat", headers=headers).status_code == 200
            now[0] += timedelta(seconds=15)
            assert client.post(base + "/disconnect", headers=headers).status_code == 403
            claim_body["worker_id"] = str(uuid4())
            reclaimed = client.post("/voice/worker/claim", headers=bootstrap, json=claim_body)
            assert reclaimed.status_code == 200, reclaimed.text
            newer = reclaimed.json()
            assert newer["generation"] == grant["generation"] + 1
            assert client.get(base, headers=headers).status_code in {401, 403}
            fresh_headers = {
                "Authorization": "Bearer " + newer["token"],
                "X-Runtime-Capability": newer["runtime_token"],
            }
            response = client.post(base + "/end", headers=fresh_headers)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "ended"
            assert len(room_deletes) == 1
            with Session(database) as db:
                assert db.get(VoiceSession, voice_id).status == "ended"
                assert db.get(AgentSession, session_id).revoked_at is not None
    finally:
        from valkey import Valkey

        cleanup = Valkey.from_url(settings.valkey_url.get_secret_value())
        cleanup.delete("voice:capacity", "voice:queue", "voice:ready")
        cleanup.close()


def test_staff_created_queue_message_is_claimed_and_cleared_only_after_heartbeat(seed, monkeypatch):
    import json

    import brain.voice_routes as staff_routes

    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="runtime-queue-route-distinct-secret-2026",
        voice_worker_secret="worker-queue-route-distinct-secret-2026",
        voice_bootstrap_secret="bootstrap-queue-route-distinct-secret-2026",
        livekit_api_key="synthetic-key",
        livekit_api_secret="livekit-queue-route-distinct-secret-2026",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        voice_enabled=True,
        _env_file=None,
    )
    app = create_app(settings, clock=lambda: NOW)
    monkeypatch.setattr(
        staff_routes,
        "LiveKitRooms",
        lambda *_: type(
            "Rooms", (), {"create": lambda self, room: None, "delete": lambda self, room: None}
        )(),
    )
    redis = app.state.brain_store.client
    redis.delete("voice:queue", "voice:pending", "voice:capacity", "voice:ready")
    redis.set("voice:ready", json.dumps({"en": True, "ur": False}), ex=15)
    try:
        with TestClient(app) as client:
            login = client.post(
                "/auth/login", json={"email": "owner-a@example.com", "password": PASSWORD}
            )
            staff = {"Authorization": "Bearer " + login.json()["access_token"]}
            response = client.post(
                "/voice/sessions",
                headers=staff,
                json={"agent_id": str(seed["agent_a"]), "request_key": "roundtrip-queue"},
            )
            assert response.status_code == 201, response.text
            bootstrap = {
                "Authorization": "Bearer " + settings.voice_bootstrap_secret.get_secret_value()
            }
            claimed = client.post(
                "/voice/worker/next", headers=bootstrap, json={"worker_id": str(uuid4())}
            )
            assert claimed.status_code == 200, claimed.text
            grant = claimed.json()
            assert grant["session_id"] == response.json()["session_id"]
            assert redis.llen("voice:pending") == 1
            worker = {
                "Authorization": "Bearer " + grant["token"],
                "X-Runtime-Capability": grant["runtime_token"],
            }
            base = "/voice/worker/sessions/" + grant["session_id"]
            assert client.post(base + "/heartbeat", headers=worker).status_code == 200
            assert redis.llen("voice:pending") == 0
            assert client.post(base + "/end", headers=worker).status_code == 200
    finally:
        redis.delete("voice:queue", "voice:pending", "voice:capacity", "voice:ready")


def test_worker_can_cleanup_after_brain_has_revoked_runtime(seed, database, monkeypatch):
    import brain.voice_routes as staff_routes
    from brain.contracts import Action

    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="runtime-terminal-test-distinct-secret-2026",
        voice_worker_secret="worker-terminal-test-distinct-secret-2026",
        voice_bootstrap_secret="bootstrap-terminal-test-distinct-secret-2026",
        livekit_api_key="synthetic",
        livekit_api_secret="livekit-terminal-test-distinct-secret-2026",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        _env_file=None,
    )
    staff = Principal(seed["owner_a"], seed["tenant_a"], "owner-a@example.com", "owner")
    from brain.voice_sessions import claim_worker

    with Session(database) as db:
        voice = create_voice(db, staff, seed["agent_a"], "terminal-cleanup", NOW, settings)
        voice.status = "active"
        db.flush()
        grant = claim_worker(db, voice.id, staff.tenant_id, uuid4(), NOW, settings)
        db.commit()
    app = create_app(settings, clock=lambda: NOW)
    app.state.brain_planner.plan = lambda *_: Action(name="end_call", arguments={})
    deleted = []
    monkeypatch.setattr(
        staff_routes,
        "LiveKitRooms",
        lambda *_: type("Rooms", (), {"delete": lambda self, room: deleted.append(room)})(),
    )
    redis = app.state.brain_store.client
    redis.set("voice:capacity", str(grant.session_id), ex=600)
    headers = {
        "Authorization": "Bearer " + grant.token,
        "X-Runtime-Capability": grant.runtime_token,
    }
    base = "/voice/worker/sessions/" + str(grant.session_id)
    try:
        with TestClient(app) as client:
            response = client.post(
                base + "/turn",
                headers=headers,
                json={
                    "command_id": "spoken-end",
                    "expected_revision": 0,
                    "text": "End this call.",
                    "utterance_started_at": NOW.isoformat(),
                    "language": "en",
                    "confidence": 0.99,
                    "uncertain": False,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["state"]["ended"]
            ended = client.post(base + "/end", headers=headers)
            assert ended.status_code == 200, ended.text
            assert ended.json()["status"] == "ended"
            assert redis.get("voice:capacity") is None
            assert len(deleted) == 1
    finally:
        redis.delete("voice:capacity")
