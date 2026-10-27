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
