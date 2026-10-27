import json
import stat
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from scheduler.bootstrap import seed_demo
from scheduler.models import ProviderService, Schedule, Tenant, User
from scheduler.security import passwords


def test_demo_private_idempotent_clinics(database, seed, tmp_path: Path):
    path = tmp_path / "results" / "credentials.json"
    admin_url = database.url.render_as_string(hide_password=False)
    assert seed_demo(admin_url, path) == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    original = path.read_bytes()
    original_stat = path.stat()
    clinics = json.loads(original)["clinics"]
    assert len(clinics) == 2
    assert clinics[0]["owner_password"] != clinics[1]["owner_password"]
    with Session(database) as db:
        for clinic in clinics:
            user = db.scalar(select(User).where(User.email == clinic["owner_email"]))
            assert user.role == "owner"
            assert passwords.verify(clinic["owner_password"], user.password_hash)
            assert str(user.tenant_id) == clinic["tenant_id"]
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(Schedule)
                    .where(Schedule.tenant_id == user.tenant_id)
                )
                == 7
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(ProviderService)
                    .where(ProviderService.tenant_id == user.tenant_id)
                )
                == 1
            )
        tenant_count = db.scalar(select(func.count()).select_from(Tenant))
    seed_demo(admin_url, path)
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(Tenant)) == tenant_count


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://postgres:synthetic@remote.example/voice_agent",
        "postgresql+psycopg://postgres:synthetic@127.0.0.1/unrelated",
        "postgresql+psycopg://postgres:synthetic@127.0.0.1/voice_agent?host=remote.example",
        "sqlite:///voice_agent",
    ],
)
def test_refuses_other_database_targets(url, tmp_path: Path):
    path = tmp_path / "credentials.json"
    with pytest.raises(ValueError):
        seed_demo(url, path)
    assert not path.exists()


def test_demo_symlink_refused(database, seed, tmp_path: Path):
    target = tmp_path / "target"
    target.write_text("synthetic sentinel")
    path = tmp_path / "credentials.json"
    path.symlink_to(target)
    with pytest.raises(ValueError):
        seed_demo(database.url.render_as_string(hide_password=False), path)
    assert path.is_symlink()
    assert target.read_text() == "synthetic sentinel"


def test_demo_database_failure_rolls_back_partial_clinic_and_removes_file(
    database, seed, tmp_path: Path
):
    admin_url = database.url.render_as_string(hide_password=False)
    with Session(database) as db, db.begin():
        user = db.scalar(select(User).where(User.email == "owner-b@example.com"))
        user.email = "demo-owner-b@example.com"
        initial_tenants = db.scalar(select(func.count()).select_from(Tenant))
    path = tmp_path / "credentials.json"
    with pytest.raises(ValueError):
        seed_demo(admin_url, path)
    assert not path.exists()
    with Session(database) as db:
        assert db.scalar(select(User).where(User.email == "demo-owner-a@example.com")) is None
        assert db.scalar(select(func.count()).select_from(Tenant)) == initial_tenants
