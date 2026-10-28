import pytest


def test_artifact_pin_and_cache_cannot_escape_backend(tmp_path):
    from voice_runtime.assets import ARTIFACTS
    from voice_runtime.config import Settings

    assert len(ARTIFACTS["whisper"].revision) == 40
    with pytest.raises(ValueError):
        Settings(cache_dir=tmp_path)


def test_adapter_rejects_wrong_language_before_inference():
    from voice_runtime.speech.kokoro import KokoroAdapter
    from voice_runtime.speech.parler import ParlerAdapter

    for adapter, language in [(KokoroAdapter(), "ur"), (ParlerAdapter(), "en")]:
        with pytest.raises(ValueError, match="language"):
            adapter.synthesize("test", language=language, generation=0)


def test_parler_gate_refuses_insufficient_physical_headroom():
    from voice_runtime.config import require_headroom
    from voice_runtime.speech.process import JobFailure

    with pytest.raises(JobFailure, match="headroom"):
        require_headroom("parler", available_bytes=1902 * 1024**2)


def test_inference_child_never_inherits_database_or_arbitrary_credentials(monkeypatch):
    from voice_runtime.config import Settings

    monkeypatch.setenv("SCHEDULER_DATABASE_URL", "postgresql://private:credential@localhost/db")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-private")
    env = Settings().environment()
    assert env.get("SCHEDULER_DATABASE_URL") is None
    assert env.get("AWS_ACCESS_KEY_ID") is None
    assert env["HF_HUB_OFFLINE"] == "1"


def test_snapshot_refuses_incomplete_or_wrong_revision_manifest(tmp_path, monkeypatch):
    import json

    import pytest

    import voice_runtime.assets as module
    from voice_runtime.config import Settings

    monkeypatch.setattr(module, "LOCAL", tmp_path)
    settings = Settings()
    # Use a workspace cache path but no model bytes; only metadata is under test.
    from pathlib import Path

    pin = module.ARTIFACTS["kokoro"]
    manifest = tmp_path / "kokoro-manifest.json"
    manifest.write_text(json.dumps({"repo": pin.repo, "revision": "wrong", "files": []}))
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    with pytest.raises((ValueError, FileNotFoundError)):
        module.snapshot("kokoro", settings)
