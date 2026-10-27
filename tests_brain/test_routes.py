from uuid import uuid4

from fastapi.testclient import TestClient

from scheduler.config import Settings
from scheduler.main import create_app
from tests_api.conftest import APP_URL, NOW, PASSWORD, SECRET


def test_runtime_http_separation_cache_loss_and_confirmation(seed):
    settings = Settings(
        database_url=APP_URL,
        jwt_secret=SECRET,
        runtime_secret="runtime-route-test-secret-different-40chars",
        valkey_url="redis://:phase2_valkey_test_only@127.0.0.1:56379/0",
        _env_file=None,
    )
    app = create_app(settings, clock=lambda: NOW)
    with TestClient(app) as client:
        staff = client.post(
            "/auth/login", json={"email": "owner-a@example.com", "password": PASSWORD}
        ).json()["access_token"]
        response = client.post(
            "/agent-sessions",
            headers={"Authorization": "Bearer " + staff},
            json={"agent_id": str(seed["agent_a"])},
        )
        assert response.status_code == 201, response.text
        created = response.json()
        sid = created["session_id"]
        token = created["token"]
        revision = 0
        client.headers["Authorization"] = "Bearer " + token
        assert client.get("/patients").status_code == 401
        assert client.get("/runtime/sessions/" + sid).status_code == 200

        def command(name, args=None, text="", command_id=None, rev=None):
            nonlocal revision
            response = client.post(
                "/runtime/sessions/" + sid + "/commands",
                json={
                    "command_id": command_id or str(uuid4()),
                    "expected_revision": revision if rev is None else rev,
                    "action": {"name": name, "arguments": args or {}},
                    "text": text,
                },
            )
            assert response.status_code == 200, response.text
            revision = response.json()["revision"]
            return response.json()

        command("find_patient", {"phone": "03001234567"})
        key = str(uuid4())
        old = revision
        response = command("verify_patient", {"factor": "Synthetic Patient"}, command_id=key)
        assert response["result"]["ok"]
        app.state.brain_store.delete(sid)
        replay = command("verify_patient", {"factor": "Synthetic Patient"}, command_id=key, rev=old)
        assert replay["revision"] == revision and replay["result"] == response["result"]
        assert "Synthetic Patient" not in str(client.get("/runtime/sessions/" + sid).json())
        family = client.post(
            "/runtime/sessions/" + sid + "/turn",
            json={
                "command_id": str(uuid4()),
                "expected_revision": revision,
                "text": "Book for my child instead of myself.",
            },
        ).json()
        revision = family["revision"]
        assert family["result"]["ok"] and not family["state"]["verified"]
        assert not family["state"].get("subject") and family["provider_requests"] == 0
        assert client.get("/runtime/sessions/" + str(uuid4())).status_code == 401
        command("end_call")
        assert client.get("/runtime/sessions/" + sid).status_code == 401


def test_delayed_pre_end_cache_repair_cannot_restore_history():
    from valkey import Valkey

    from brain.routes import repair
    from brain.store import StateStore

    store = StateStore(
        Valkey.from_url(
            "redis://:phase2_valkey_test_only@127.0.0.1:56379/0", decode_responses=True
        ),
        prefix="end-race-" + str(uuid4()),
    )
    sid = str(uuid4())
    repair(store, sid, {"revision": 6, "ended": True, "history": ["private"]})
    repair(store, sid, {"revision": 5, "history": ["stale private"]})
    assert store.load(sid) == {"revision": 6, "ended": True}
    store.delete(sid)
