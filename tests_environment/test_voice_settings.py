from scheduler.config import Settings


def test_empty_optional_compose_credentials_do_not_enable_or_break_voice(monkeypatch):
    for name in ("VOICE_BOOTSTRAP_SECRET", "VOICE_WORKER_SECRET", "LIVEKIT_API_SECRET"):
        monkeypatch.setenv("SCHEDULER_" + name, "")
    settings = Settings(
        database_url="postgresql://unused",
        jwt_secret="synthetic-settings-test-secret-at-least32chars",
        _env_file=None,
    )
    assert settings.voice_bootstrap_secret is None
    assert settings.voice_worker_secret is None
    assert settings.livekit_api_secret is None
    assert settings.voice_enabled is False
