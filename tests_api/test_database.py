import pytest
from conftest import ADMIN_URL, APP_URL, SECRET
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from scheduler.config import Settings
from scheduler.main import create_app


def test_rls_without_context_and_cross_tenant_writes(seed):
    engine = create_engine(APP_URL)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM patients")).scalar_one() == 0
        conn.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(seed["tenant_a"])}
        )
        assert conn.execute(text("SELECT count(*) FROM patients")).scalar_one() == 1
        assert (
            conn.execute(
                text("SELECT count(*) FROM patients WHERE id=:id"), {"id": seed["patient_b"]}
            ).scalar_one()
            == 0
        )
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(seed["tenant_a"])}
        )
        conn.execute(
            text("UPDATE patients SET tenant_id=:other WHERE id=:id"),
            {"other": seed["tenant_b"], "id": seed["patient_a"]},
        )
    engine.dispose()


def test_application_refuses_superuser_database(database):
    app = create_app(Settings(database_url=ADMIN_URL, jwt_secret=SECRET, _env_file=None))
    with pytest.raises(RuntimeError, match="role"):
        with TestClient(app):
            pass
    app.state.engine.dispose()


def test_password_hash_and_database_roles(database, seed):
    with database.connect() as conn:
        value = conn.execute(
            text("SELECT password_hash FROM users WHERE id=:id"), {"id": seed["owner_a"]}
        ).scalar_one()
        assert value.startswith("$argon2id$")
        role = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='scheduler_app'")
        ).one()
        assert role == (False, False)


def test_migration_round_trip_and_forced_rls(database):
    from alembic import command
    from alembic.config import Config
    from conftest import ROOT

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", ADMIN_URL.replace("%", "%%"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    with database.connect() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace AND relrowsecurity AND relforcerowsecurity"
            )
        ).scalar_one()
        assert count == 12
