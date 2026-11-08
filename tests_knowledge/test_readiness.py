"""Injected readiness observations never initialize real artifacts or model runtimes."""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from embedding_service.server import create_app
from embedding_service.settings import Settings
from knowledge.profile import PROFILE
from tests_knowledge.test_embedding import StubWorker
from tests_knowledge.test_sources import setup_api
from tests_knowledge.test_worker import worker_factory as _worker_factory

worker_factory = _worker_factory

KEY = "isolated-blob-ownership-secret-for-tests-only"


def heartbeat(root, **overrides):
    data = {
        "version": 1,
        "profile": PROFILE.fingerprint,
        "observed_at": time.time(),
        "worker": True,
        "embedding": True,
        "artifacts": True,
    } | overrides
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(
        KEY.encode(), b"knowledge-readiness-v1\0" + payload.encode(), hashlib.sha256
    ).hexdigest()
    path = root / ".worker-readiness.json"
    path.write_text(json.dumps({"observation": data, "signature": signature}))
    path.chmod(0o600)
    return path


def test_verified_worker_observation_enables_processing(owner, tmp_path):
    base = setup_api(owner, tmp_path)
    heartbeat(tmp_path)
    result = owner.get(base + "/capabilities")
    assert result.status_code == 200
    assert result.json()["processing_available"] is True
    assert result.json()["unavailable_reason"] is None
    from knowledge.readiness import processing_status

    assert processing_status(tmp_path, "wrong-key-" * 4, enabled=True) == "processing_unavailable"
    assert processing_status(tmp_path, "", enabled=True) == "processing_unavailable"
    assert processing_status(tmp_path, KEY, enabled=False) == "disabled"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "stale",
        "forged",
        "profile",
        "embedding",
        "artifacts",
        "worker",
        "future",
        "symlink",
    ],
)
def test_unverified_readiness_fails_closed(owner, tmp_path, change):
    base = setup_api(owner, tmp_path)
    modifications = {
        "stale": {"observed_at": time.time() - 31},
        "future": {"observed_at": time.time() + 10},
        "profile": {"profile": "0" * 64},
        "embedding": {"embedding": False},
        "artifacts": {"artifacts": False},
        "worker": {"worker": False},
        "forged": {"worker": False},
    }
    path = heartbeat(tmp_path, **modifications.get(change, {}))
    if change == "missing":
        path.unlink()
    if change == "forged":
        path.write_text(path.read_text().replace('"worker": false', '"worker": true'))
    if change == "symlink":
        target = tmp_path / "outside"
        path.rename(target)
        path.symlink_to(target)
    response = owner.get(base + "/capabilities").json()
    assert response["processing_available"] is False
    assert response["unavailable_reason"] is not None


def test_embedding_status_requires_auth_and_is_observation_only():
    worker = StubWorker()

    def forbidden(*args, **kwargs):
        pytest.fail("status performed loading/inference")

    worker.start = forbidden
    worker.embed = forbidden
    app = create_app(
        Settings(__import__("pathlib").Path("/unused"), "s" * 32, enabled=True), worker
    )
    # No lifespan: health itself may only inspect already observed readiness.
    client = TestClient(app)
    assert client.get("/internal/status").status_code == 401
    response = client.get("/internal/status", headers={"Authorization": "Bearer " + "s" * 32})
    assert response.status_code == 200
    assert response.json()["profile"] == PROFILE.fingerprint
    assert response.json()["ready"] is True
    worker.ready = False
    assert (
        client.get("/internal/status", headers={"Authorization": "Bearer " + "s" * 32}).json()[
            "ready"
        ]
        is False
    )


def test_worker_producer_checks_db_artifacts_and_authenticated_embedding(
    owner, tmp_path, worker_factory
):
    from pathlib import Path

    from knowledge.embedding import EmbeddingUnavailable
    from knowledge.readiness import processing_status
    from knowledge_worker.readiness import observe
    from knowledge_worker.settings import Settings as WorkerSettings
    from tests_knowledge.test_worker import Embeddings

    setup_api(owner, tmp_path)
    settings = WorkerSettings(
        enabled=True, blob_root=tmp_path, blob_secret=KEY, tokenizer=Path("/synthetic-tokenizer")
    )

    def valid(path):
        assert path == Path("/synthetic-tokenizer")

    observe(worker_factory, settings, Embeddings(), verify=valid)
    assert processing_status(tmp_path, KEY, enabled=True) is None

    def invalid(path):
        raise EmbeddingUnavailable("tokenizer_artifact")

    observe(worker_factory, settings, Embeddings(), verify=invalid)
    assert processing_status(tmp_path, KEY, enabled=True) == "artifacts_unavailable"
    observe(worker_factory, settings, Embeddings(ready=False), verify=valid)
    assert processing_status(tmp_path, KEY, enabled=True) == "embedding_unavailable"


def test_embedding_client_rejects_wrong_profile_configured_only_and_unauthenticated():
    import httpx2

    from knowledge.embedding import EmbeddingClient

    cases = [
        (200, {"ready": True}),
        (200, {"ready": True, "profile": "0" * 64}),
        (401, {"ready": True, "profile": PROFILE.fingerprint}),
        (200, {"ready": False, "profile": PROFILE.fingerprint}),
        (200, {"ready": True, "profile": PROFILE.fingerprint}),
    ]
    for index, (status, payload) in enumerate(cases):

        def handler(request):
            assert request.url.path == "/internal/status"
            assert request.headers["Authorization"] == "Bearer " + "s" * 32
            return httpx2.Response(status, json=payload)

        client = EmbeddingClient(
            "http://private", "s" * 32, transport=httpx2.MockTransport(handler)
        )
        assert client.available() is (index == 4)


def test_ready_corpus_query_availability_does_not_depend_on_worker(owner, tmp_path, database, seed):
    from uuid import UUID

    from tests_knowledge.test_retrieval import corpus

    base = setup_api(owner, tmp_path)
    corpus(database, seed["tenant_a"], UUID(base.split("/")[3]), ["hours"])

    class Available:
        def available(self):
            return True

    owner.app.state.knowledge_embeddings = Available()
    result = owner.get(base + "/capabilities").json()
    assert result["processing_available"] is False
    assert result["query_available"] is True
    assert result["query_unavailable_reason"] is None
    owner.app.state.knowledge_embeddings = None
    result = owner.get(base + "/capabilities").json()
    assert result["query_available"] is False
    assert result["query_unavailable_reason"] == "service_unavailable"


def test_status_deadline_and_single_outstanding_probe():
    import threading

    import httpx2

    from knowledge.embedding import EmbeddingClient

    release = threading.Event()
    entered = threading.Event()

    def handler(request):
        entered.set()
        release.wait(4)
        return httpx2.Response(200, json={"ready": True, "profile": PROFILE.fingerprint})

    client = EmbeddingClient("http://private", "s" * 32, transport=httpx2.MockTransport(handler))
    started = time.monotonic()
    try:
        assert client.available() is False
        assert entered.is_set()
        assert time.monotonic() - started < 2.8
        repeated = time.monotonic()
        assert client.available() is False
        assert time.monotonic() - repeated < 0.2
    finally:
        release.set()
