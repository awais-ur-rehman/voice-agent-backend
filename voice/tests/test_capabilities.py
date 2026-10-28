import json


def test_readiness_rejects_evidence_without_current_artifact_and_voice_fingerprint(
    tmp_path, monkeypatch
):
    import voice_runtime.worker as module

    monkeypatch.setattr(module, "LOCAL", tmp_path)
    monkeypatch.setattr(module, "snapshot", lambda _: tmp_path)
    (tmp_path / "english-loopback.json").write_text(json.dumps({"status": "passed"}))
    assert module.capabilities() == {"en": False, "ur": False}


def test_readiness_accepts_only_matching_evidence_for_each_language(tmp_path, monkeypatch):
    import voice_runtime.worker as module

    monkeypatch.setattr(module, "LOCAL", tmp_path)
    monkeypatch.setattr(module, "snapshot", lambda _: tmp_path)
    fingerprint = module.gate_fingerprint("en")
    (tmp_path / "english-loopback.json").write_text(
        json.dumps({"status": "passed", "fingerprint": fingerprint})
    )
    assert module.capabilities() == {"en": True, "ur": False}
    record = json.loads((tmp_path / "english-loopback.json").read_text())
    record["fingerprint"]["artifacts"]["whisper"] = "stale-revision"
    (tmp_path / "english-loopback.json").write_text(json.dumps(record))
    assert module.capabilities() == {"en": False, "ur": False}
