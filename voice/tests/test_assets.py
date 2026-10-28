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


@pytest.fixture
def baseline_with_unlisted_candidate(tmp_path, monkeypatch):
    """Real snapshot validation over synthetic bytes: only af_heart is approved/listed."""
    import hashlib
    import json
    from types import SimpleNamespace

    import voice_runtime.assets as assets
    from voice_runtime.config import BACKEND

    settings = SimpleNamespace(
        cache_dir=tmp_path / "cache",
        memory_bytes=1024**3,
        inference_python=tmp_path / "never-executed",
        timeout_seconds=1,
        environment=lambda: {},
    )
    pin = assets.ARTIFACTS["kokoro"]
    root = (
        settings.cache_dir
        / "hub"
        / ("models--" + pin.repo.replace("/", "--"))
        / "snapshots"
        / pin.revision
    )
    (root / "voices").mkdir(parents=True)
    baseline = root / "voices/af_heart.pt"
    baseline.write_bytes(b"synthetic approved tensor")
    (root / "voices/am_adam.pt").write_bytes(b"synthetic unlisted candidate")
    local = tmp_path / "manifests"
    local.mkdir()
    record = {
        "file": "voices/af_heart.pt",
        "bytes": baseline.stat().st_size,
        "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
    }
    (local / "kokoro-manifest.json").write_text(
        json.dumps({"repo": pin.repo, "revision": pin.revision, "files": [record]})
    )
    monkeypatch.setattr(assets, "LOCAL", local)
    monkeypatch.setattr(assets, "Settings", lambda: settings)
    assert assets.snapshot("kokoro", settings) == root
    candidate = next(
        row
        for row in json.loads((BACKEND / "voice/catalog.json").read_text())
        if row["id"] == "kokoro-am-adam-v1"
    )
    return settings, root, local, candidate


def test_unlisted_candidate_tensor_never_starts_synthesis(
    baseline_with_unlisted_candidate, monkeypatch
):
    from voice_runtime.speech.kokoro import KokoroAdapter

    settings, root, local, candidate = baseline_with_unlisted_candidate

    class Supervisor:
        def run(self, *args, **kwargs):
            raise AssertionError("Unlisted candidate reached inference")

    monkeypatch.setattr(
        "voice_runtime.speech.adapter.require_headroom", lambda *args, **kwargs: None
    )
    with pytest.raises(ValueError, match="manifest|approved"):
        KokoroAdapter(supervisor=Supervisor(), settings=settings).synthesize(
            "hello", "en", candidate
        )


def test_unlisted_candidate_tensor_never_qualifies_readiness(
    baseline_with_unlisted_candidate, monkeypatch
):
    import json
    import voice_runtime.assets as assets
    import voice_runtime.worker as worker

    settings, root, local, candidate = baseline_with_unlisted_candidate
    expected = {"synthetic": "otherwise-matching-evidence"}
    (local / ("profile-" + candidate["config_fingerprint"] + "-loopback.json")).write_text(
        json.dumps({"status": "passed", "fingerprint": expected})
    )
    monkeypatch.setattr(worker, "LOCAL", local)
    monkeypatch.setattr(
        worker,
        "snapshot",
        lambda name: assets.snapshot(name, settings) if name == "kokoro" else root,
    )
    monkeypatch.setattr(worker, "gate_fingerprint", lambda *args: expected)
    assert worker.qualified_profiles() == []


def test_approved_tensor_requires_manifest_membership_and_digest(baseline_with_unlisted_candidate):
    import hashlib
    import json

    from voice_runtime.assets import require_manifest_file

    settings, root, local, candidate = baseline_with_unlisted_candidate
    baseline = root / "voices/af_heart.pt"
    assert require_manifest_file("kokoro", "voices/af_heart.pt", settings) == baseline
    manifest_path = local / "kokoro-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    extra = root / "voices/am_adam.pt"
    manifest["files"].append(
        {
            "file": "voices/am_adam.pt",
            "bytes": extra.stat().st_size,
            "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(),
        }
    )
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="approved"):
        require_manifest_file("kokoro", "voices/am_adam.pt", settings)
    manifest["files"] = [row for row in manifest["files"] if row["file"] != "voices/af_heart.pt"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest member"):
        require_manifest_file("kokoro", "voices/af_heart.pt", settings)
    manifest["files"].append(
        {
            "file": "voices/af_heart.pt",
            "bytes": baseline.stat().st_size,
            "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        }
    )
    manifest_path.write_text(json.dumps(manifest))
    baseline.write_bytes(b"X" + baseline.read_bytes()[1:])
    with pytest.raises(ValueError, match="bytes"):
        require_manifest_file("kokoro", "voices/af_heart.pt", settings)


def test_selected_profile_gate_includes_approved_tensor_digest(baseline_with_unlisted_candidate):
    import hashlib
    import json

    from voice_runtime.config import BACKEND
    from voice_runtime.worker import gate_fingerprint

    settings, root, local, candidate = baseline_with_unlisted_candidate
    profile = next(
        row
        for row in json.loads((BACKEND / "voice/catalog.json").read_text())
        if row["id"] == "kokoro-af-heart-v1"
    )
    before = gate_fingerprint("en", profile)
    baseline = root / "voices/af_heart.pt"
    assert before["selected_tensor"]["sha256"] == hashlib.sha256(baseline.read_bytes()).hexdigest()
    manifest_path = local / "kokoro-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    baseline.write_bytes(b"X" + baseline.read_bytes()[1:])
    manifest["files"][0]["sha256"] = hashlib.sha256(baseline.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    assert gate_fingerprint("en", profile) != before
