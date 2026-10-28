import stat
from pathlib import Path

from scripts.dev_setup import create_env_file


def test_generates_private_separate_credentials(tmp_path: Path) -> None:
    path = tmp_path / ".env.api"
    assert create_env_file(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )
    secrets = [
        values[name] for name in ("DEV_ADMIN_PASSWORD", "DEV_APP_PASSWORD", "SCHEDULER_JWT_SECRET")
    ]
    assert min(map(len, secrets)) >= 64
    assert len(set(secrets)) == 3
    assert values["DEV_APP_PASSWORD"] in values["SCHEDULER_DATABASE_URL"]
    assert values["DEV_ADMIN_PASSWORD"] not in values["SCHEDULER_DATABASE_URL"]


def test_existing_file_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / ".env.api"
    path.write_text("existing-synthetic-content\n")
    path.chmod(0o640)
    before = path.stat()
    assert not create_env_file(path)
    assert path.read_text() == "existing-synthetic-content\n"
    assert path.stat().st_mode == before.st_mode
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_existing_symlink_is_preserved(tmp_path: Path) -> None:
    target = tmp_path / "synthetic-secret"
    target.write_text("synthetic-data")
    path = tmp_path / ".env.api"
    path.symlink_to(target)
    assert not create_env_file(path)
    assert path.is_symlink()
    assert target.read_text() == "synthetic-data"


def test_brain_setup_appends_missing_values_without_replacing_credentials(tmp_path: Path) -> None:
    from scripts.dev_setup import enable_brain

    path = tmp_path / ".env.api"
    original = "DEV_ADMIN_PASSWORD=synthetic-admin\nDEV_VALKEY_PASSWORD=kept-password\n"
    path.write_text(original)
    path.chmod(0o600)
    assert enable_brain(path)
    text = path.read_text()
    assert text.startswith(original)
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    assert values["DEV_VALKEY_PASSWORD"] == "kept-password"
    assert values["SCHEDULER_VALKEY_URL"] == "valkey://:kept-password@127.0.0.1:56380/0"
    assert len(values["SCHEDULER_RUNTIME_SECRET"]) >= 64
    before = path.stat()
    assert not enable_brain(path)
    assert path.read_text() == text
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_brain_setup_generates_distinct_private_secrets(tmp_path: Path) -> None:
    from scripts.dev_setup import enable_brain

    path = tmp_path / ".env.api"
    create_env_file(path)
    enable_brain(path)
    values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    assert len(values["DEV_VALKEY_PASSWORD"]) >= 64
    assert values["DEV_VALKEY_PASSWORD"] != values["SCHEDULER_RUNTIME_SECRET"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_brain_setup_refuses_symlink(tmp_path: Path) -> None:
    import pytest

    from scripts.dev_setup import enable_brain

    target = tmp_path / "target"
    target.write_text("DEV_ADMIN_PASSWORD=synthetic-admin\n")
    path = tmp_path / ".env.api"
    path.symlink_to(target)
    with pytest.raises(ValueError):
        enable_brain(path)
    assert target.read_text() == "DEV_ADMIN_PASSWORD=synthetic-admin\n"


def test_voice_setup_preserves_saved_secrets_and_stays_disabled(tmp_path):
    from scripts.dev_setup import enable_voice

    path = tmp_path / ".env.api"
    create_env_file(path)
    original = path.read_text()
    assert enable_voice(path)
    updated = path.read_text()
    assert updated.startswith(original)
    values = dict(line.split("=", 1) for line in updated.splitlines() if "=" in line)
    names = [
        "SCHEDULER_VOICE_BOOTSTRAP_SECRET",
        "SCHEDULER_VOICE_WORKER_SECRET",
        "SCHEDULER_LIVEKIT_API_SECRET",
    ]
    assert len({values[key] for key in names}) == 3
    assert all(len(values[key]) >= 64 for key in names)
    assert values["SCHEDULER_VOICE_ENABLED"] == "false"
    assert not enable_voice(path)
    assert path.read_text() == updated
