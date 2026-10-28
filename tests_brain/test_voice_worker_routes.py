from types import SimpleNamespace

from fastapi.testclient import TestClient

from scheduler.config import Settings
from scheduler.main import create_app
from tests_api.conftest import SECRET


def test_readiness_requires_separate_internal_secret_and_bounded_capabilities():
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        jwt_secret=SECRET,
        voice_bootstrap_secret="bootstrap-only-test-distinct-secret-2026",
        _env_file=None,
    )
    app = create_app(settings)
    values = []
    app.state.brain_store = SimpleNamespace(
        client=SimpleNamespace(set=lambda key, value, ex: values.append((key, value, ex)))
    )
    client = TestClient(app)
    assert client.post("/voice/worker/ready", json={"en": True, "ur": False}).status_code == 401
    response = client.post(
        "/voice/worker/ready",
        headers={"Authorization": "Bearer bootstrap-only-test-distinct-secret-2026"},
        json={"en": True, "ur": False},
    )
    assert response.status_code == 200
    assert values[0][0] == "voice:ready" and values[0][2] == 15
    assert SECRET not in values[0][1]
    assert (
        client.post(
            "/voice/worker/ready",
            headers={"Authorization": "Bearer bootstrap-only-test-distinct-secret-2026"},
            json={"en": True, "ur": False, "verified": True},
        ).status_code
        == 422
    )


def test_claim_remains_recoverable_until_first_heartbeat(monkeypatch):
    import json
    from uuid import uuid4

    from brain import voice_worker_routes as routes

    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        jwt_secret=SECRET,
        voice_bootstrap_secret="bootstrap-only-test-distinct-secret-2026",
        _env_file=None,
    )
    app = create_app(settings)
    payload = json.dumps({"tenant_id": str(uuid4()), "voice_id": str(uuid4())})

    class Queue:
        def __init__(self):
            self.queued = [payload]
            self.pending = []

        def lpop(self, key):
            return self.queued.pop(0) if self.queued else None

        def lindex(self, key, index):
            return self.pending[index] if len(self.pending) > index else None

        def rpoplpush(self, source, destination):
            if not self.queued:
                return None
            item = self.queued.pop()
            self.pending.insert(0, item)
            return item

    queue = Queue()
    app.state.brain_store = SimpleNamespace(client=queue)
    monkeypatch.setattr(routes, "_claim", lambda request, body: {"voice_id": str(body.voice_id)})
    client = TestClient(app)
    headers = {"Authorization": "Bearer bootstrap-only-test-distinct-secret-2026"}
    worker = {"worker_id": str(uuid4())}
    first = client.post("/voice/worker/next", headers=headers, json=worker)
    second = client.post("/voice/worker/next", headers=headers, json=worker)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"voice_id": json.loads(payload)["voice_id"]}
