import math
import time
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from embedding_service.model import verify_assets
from embedding_service.server import create_app
from embedding_service.settings import Settings
from embedding_service.supervisor import Supervisor
from knowledge.embedding import EmbeddingClient, EmbeddingUnavailable, validate_vectors
from knowledge.profile import PROFILE


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[0.0] * 1024],
        [[1.0] * 1023],
        [[math.nan] * 1024],
        [[math.inf] * 1024],
        [[1.0] * 1024],
        [[True] + [0.0] * 1023],
    ],
)
def test_rejects_invalid_vectors(vectors):
    with pytest.raises(EmbeddingUnavailable):
        validate_vectors(vectors)


def test_accepts_unit_vector():
    assert validate_vectors([[1.0] + [0.0] * 1023]) == [[1.0] + [0.0] * 1023]


@pytest.mark.parametrize(
    "payload",
    [
        {"profile": "wrong", "vectors": [[1.0] + [0.0] * 1023]},
        {"profile": PROFILE.fingerprint, "vectors": [[0.0] * 1024]},
    ],
)
def test_client_rejects_incompatible_response(payload):
    client = EmbeddingClient(
        "http://private",
        "s" * 32,
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=payload)),
    )
    with pytest.raises(EmbeddingUnavailable):
        client.embed(["clinic"], deadline=time.monotonic() + 1)


def test_expired_client_never_sends():
    def forbidden(request):
        pytest.fail("expired request sent")

    client = EmbeddingClient("http://private", "s" * 32, transport=httpx2.MockTransport(forbidden))
    with pytest.raises(EmbeddingUnavailable, match="deadline"):
        client.embed(["clinic"], deadline=time.monotonic() - 1)


class StubWorker:
    ready = True

    def start(self):
        pass

    def close(self):
        pass

    def embed(self, texts, *, deadline):
        return [[1.0] + [0.0] * 1023]


@pytest.mark.parametrize("secret", [None, "bad"])
def test_private_route_rejects_missing_wrong_secret_before_body(secret):
    with TestClient(
        create_app(Settings(Path("/unused"), "s" * 32, enabled=True), StubWorker())
    ) as client:
        response = client.post(
            "/internal/embed",
            content="invalid json",
            headers={} if secret is None else {"Authorization": f"Bearer {secret}"},
        )
        assert response.status_code == 401


def test_private_route_strict_profile_batch_and_readiness():
    with TestClient(
        create_app(Settings(Path("/unused"), "s" * 32, enabled=True), StubWorker())
    ) as client:
        headers = {"Authorization": "Bearer " + "s" * 32}
        assert (
            client.post(
                "/internal/embed", headers=headers, json={"texts": ["a"], "profile": "wrong"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/internal/embed",
                headers=headers,
                json={"texts": ["a", "b"], "profile": PROFILE.fingerprint},
            ).status_code
            == 503
        )
        assert (
            client.post(
                "/internal/embed",
                headers=headers,
                json={"texts": ["a"], "profile": PROFILE.fingerprint, "extra": True},
            ).status_code
            == 422
        )
        response = client.post(
            "/internal/embed",
            headers=headers,
            json={"texts": ["a"], "profile": PROFILE.fingerprint},
        )
        assert response.status_code == 200
        assert response.json()["vectors"][0][0] == 1.0
        assert client.get("/health/ready").json() == {"ready": True}
        assert client.get("/openapi.json").status_code == 404


def test_missing_and_unmanifested_assets_fail_closed(tmp_path):
    with pytest.raises(EmbeddingUnavailable):
        verify_assets(tmp_path / "missing")
    (tmp_path / "evil.py").write_text("pass")
    with pytest.raises(EmbeddingUnavailable, match="unmanifested"):
        verify_assets(tmp_path)


def test_altered_hash_rejected_even_when_size_matches(tmp_path, monkeypatch):
    import hashlib

    import embedding_service.model as module
    from knowledge.profile import Artifact, EmbeddingProfile

    monkeypatch.setattr(
        module,
        "PROFILE",
        EmbeddingProfile("x", "x", "x", (Artifact("file", 3, hashlib.sha256(b"yes").hexdigest()),)),
    )
    (tmp_path / "file").write_bytes(b"bad")
    with pytest.raises(EmbeddingUnavailable, match="asset_hash"):
        verify_assets(tmp_path)


def test_resource_floor_prevents_child_start(tmp_path, monkeypatch):
    monkeypatch.setattr("embedding_service.supervisor.available_memory", lambda: 1792 * 1024**2 - 1)
    worker = Supervisor(tmp_path)
    try:
        with pytest.raises(EmbeddingUnavailable, match="memory_floor"):
            worker.start()
        assert worker.process is None
        assert not worker.ready
    finally:
        worker.close()


def test_queue_full_and_expired_requests_do_not_run(tmp_path):
    worker = Supervisor(tmp_path)
    try:
        for _ in range(5):
            assert worker._slots.acquire(False)
        with pytest.raises(EmbeddingUnavailable, match="queue_full"):
            worker.embed(["a"], deadline=time.monotonic() + 1)
        for _ in range(5):
            worker._slots.release()
        with pytest.raises(EmbeddingUnavailable, match="deadline"):
            worker.embed(["a"], deadline=time.monotonic() - 1)
        assert worker.process is None
    finally:
        worker.close()


def test_watchdog_kills_hung_child_independently(tmp_path):
    import subprocess
    import sys

    worker = Supervisor(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    worker.process = process
    worker.ready = True
    worker._deadline = time.monotonic() + 0.1
    try:
        process.wait(timeout=3)
        assert not worker.ready
        assert worker.reason == "deadline"
    finally:
        worker.close()


def test_watchdog_kills_excess_rss(tmp_path, monkeypatch):
    import subprocess
    import sys

    monkeypatch.setattr("embedding_service.supervisor.RSS_LIMIT", 1)
    worker = Supervisor(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    worker.process = process
    worker.ready = True
    try:
        process.wait(timeout=3)
        assert not worker.ready
        assert worker.reason == "rss_limit"
    finally:
        worker.close()


def test_lightweight_import_does_not_load_model_or_dotenv():
    import subprocess
    import sys

    code = """import sys
import knowledge.embedding
assert not any(x in sys.modules for x in ['onnxruntime','tokenizers','numpy','dotenv','embedding_service.model','scheduler.config'])
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_token_bound_and_cls_pooling():
    np = pytest.importorskip("numpy")
    tokenizers = pytest.importorskip("tokenizers")
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from tokenizers.processors import TemplateProcessing

    from embedding_service.model import Model

    model = object.__new__(Model)
    model.tokenizer = tokenizers.Tokenizer(
        WordLevel({"[UNK]": 0, "<s>": 1, "</s>": 2, "a": 3}, unk_token="[UNK]")
    )
    model.tokenizer.pre_tokenizer = Whitespace()
    model.tokenizer.post_processor = TemplateProcessing(
        single="<s> $A </s>", special_tokens=[("<s>", 1), ("</s>", 2)]
    )

    class Session:
        def run(self, outputs, inputs):
            assert outputs == ["last_hidden_state"]
            assert inputs["input_ids"].shape == (1, 512)
            hidden = np.zeros((1, 512, 1024))
            hidden[0, 0, 0] = 3
            hidden[0, 0, 1] = 4
            hidden[0, 1, 2] = 100
            return [hidden]

    model.session = Session()
    vector = model.embed([" ".join(["a"] * 510)])[0]
    assert vector[:3] == [0.6, 0.8, 0.0]
    with pytest.raises(EmbeddingUnavailable, match="token_limit"):
        model.embed([" ".join(["a"] * 511)])


def test_child_crash_invalidates_readiness_and_explicit_restart(tmp_path, monkeypatch):
    import subprocess
    import sys

    import embedding_service.supervisor as module

    real_popen = subprocess.Popen

    def child(command, **kwargs):
        assert not any(key.startswith(("SCHEDULER", "LLM", "DATABASE")) for key in kwargs["env"])
        return real_popen(
            [sys.executable, "-u", "-c", "import time; print('{\"ready\": true}'); time.sleep(30)"],
            **kwargs,
        )

    monkeypatch.setattr(module.subprocess, "Popen", child)
    monkeypatch.setattr(module, "available_memory", lambda: 2 * 1024**3)
    worker = Supervisor(tmp_path)
    try:
        worker.start()
        first = worker.process
        first.kill()
        first.wait()
        with pytest.raises(EmbeddingUnavailable):
            worker.embed(["a"], deadline=time.monotonic() + 1)
        worker.start()
        assert worker.ready and worker.process.pid != first.pid
        assert first.poll() is not None
    finally:
        worker.close()


def test_deadline_kills_inference_and_does_not_deliver_late_vector(tmp_path, monkeypatch):
    import subprocess
    import sys

    import embedding_service.supervisor as module

    real_popen = subprocess.Popen
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda command, **kwargs: real_popen(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('{\"ready\": true}'); input(); time.sleep(30)",
            ],
            **kwargs,
        ),
    )
    monkeypatch.setattr(module, "available_memory", lambda: 2 * 1024**3)
    worker = Supervisor(tmp_path)
    try:
        worker.start()
        with pytest.raises(EmbeddingUnavailable, match="deadline"):
            worker.embed(["a"], deadline=time.monotonic() + 0.1)
        worker.process.wait(timeout=2)
        assert not worker.ready
    finally:
        worker.close()


def test_huge_integer_vector_is_bounded_unavailable():
    with pytest.raises(EmbeddingUnavailable):
        validate_vectors([[10**1000] + [0.0] * 1023])


def test_service_requires_explicit_model_opt_in(tmp_path):
    class ForbiddenWorker(StubWorker):
        ready = False

        def start(self):
            pytest.fail("model started without opt-in")

        def embed(self, *args, **kwargs):
            pytest.fail("inference without opt-in")

    with TestClient(create_app(Settings(tmp_path, "s" * 32), ForbiddenWorker())) as client:
        assert client.get("/health/ready").status_code == 503
        response = client.post(
            "/internal/embed",
            headers={"Authorization": "Bearer " + "s" * 32},
            json={"profile": PROFILE.fingerprint, "texts": ["a"]},
        )
        assert response.status_code == 503


def test_startup_timeout_kills_child(tmp_path, monkeypatch):
    import subprocess
    import sys

    import embedding_service.supervisor as module

    real_popen = subprocess.Popen
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda command, **kwargs: real_popen(
            [sys.executable, "-u", "-c", "import time; time.sleep(30)"], **kwargs
        ),
    )
    monkeypatch.setattr(module, "available_memory", lambda: 2 * 1024**3)
    monkeypatch.setattr(module, "STARTUP_SECONDS", 0.1)
    worker = Supervisor(tmp_path)
    try:
        with pytest.raises(EmbeddingUnavailable, match="deadline"):
            worker.start()
        assert not worker.ready
        worker.process.wait(timeout=2)
    finally:
        worker.close()


def test_asset_symlink_rejected(tmp_path):
    (tmp_path / "tokenizer.json").symlink_to("/etc/hosts")
    with pytest.raises(EmbeddingUnavailable, match="unmanifested"):
        verify_assets(tmp_path)


def test_bootstrap_checks_disk_before_network(tmp_path, monkeypatch):
    import shutil

    from scripts.prepare_knowledge_assets import bootstrap

    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10, 9, 1))
    with pytest.raises(EmbeddingUnavailable, match="disk_reserve"):
        bootstrap(tmp_path / "model")
    assert not (tmp_path / "model").exists()


def test_rpc_total_deadline_bounds_slow_response():
    import threading

    def delayed(request):
        time.sleep(0.2)
        return httpx2.Response(
            200, json={"profile": PROFILE.fingerprint, "vectors": [[1.0] + [0.0] * 1023]}
        )

    client = EmbeddingClient("http://private", "s" * 32, transport=httpx2.MockTransport(delayed))
    completed = threading.Event()

    def call():
        try:
            with pytest.raises(EmbeddingUnavailable):
                client.embed(["a"], deadline=time.monotonic() + 0.03)
        finally:
            completed.set()

    thread = threading.Thread(target=call)
    thread.start()
    try:
        assert completed.wait(0.12), "RPC exceeded absolute deadline"
    finally:
        thread.join(timeout=1)


@pytest.mark.parametrize("ready", [1, 1.0, "true", None, [], {}])
def test_numeric_or_nonboolean_readiness_never_enables_embedding(ready):
    client = EmbeddingClient(
        "http://private",
        "s" * 32,
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json={"ready": ready, "profile": PROFILE.fingerprint}
            )
        ),
    )
    assert client.available() is False
