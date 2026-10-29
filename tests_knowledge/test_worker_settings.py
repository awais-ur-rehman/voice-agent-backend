import os
import subprocess
import sys


def test_disabled_entrypoint_never_connects_or_loads_parser_model(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "knowledge_worker.main", "--once"],
        env={
            "PATH": os.defpath,
            "KNOWLEDGE_WORKER_DATABASE_URL": "invalid",
            "KNOWLEDGE_WORKER_TOKENIZER": "/does/not/exist",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '{"status": "disabled"}'
    assert not result.stderr


def test_unverified_tokenizer_fails_before_tokenizer_load(tmp_path, monkeypatch):
    import pytest

    from knowledge.embedding import EmbeddingUnavailable
    from knowledge_worker.settings import load_tokenizer

    path = tmp_path / "tokenizer.json"
    path.write_text("{}")
    with pytest.raises(EmbeddingUnavailable, match="tokenizer_artifact"):
        load_tokenizer(path)


def test_readiness_requires_actual_service_response():
    import httpx2

    from knowledge_worker.settings import PrivateEmbeddings

    for response, expected in [
        (httpx2.Response(503, json={"ready": False}), False),
        (httpx2.Response(200, json={"ready": False}), False),
        (httpx2.Response(200, json={"ready": True}), True),
        (httpx2.Response(200, content=b"x" * 1024), False),
    ]:
        client = PrivateEmbeddings(
            "http://embedding:8001", "x" * 32, transport=httpx2.MockTransport(lambda r: response)
        )
        assert client.available() is expected
