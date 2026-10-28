import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from scheduler.config import Settings
from scheduler.main import create_app
from tests_api.conftest import ADMIN_URL, APP_URL, SECRET


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

    from tests_api.conftest import ROOT

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
        assert count == 18


def test_postgres_itself_serializes_competing_slot_inserts(seed):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    engine = create_engine(APP_URL)
    barrier = Barrier(2)
    sql = text("""INSERT INTO appointments(id,tenant_id,provider_id,patient_id,service_id,starts_at,ends_at,duration_min,buffer_before_min,buffer_after_min)
    VALUES(:id,:tenant,:provider,:patient,:service,'2026-11-03T05:00:00Z','2026-11-03T05:30:00Z',30,0,0)""")

    def insert(_):
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("SELECT set_config('app.tenant_id',:tenant,true)"),
                    {"tenant": str(seed["tenant_a"])},
                )
                barrier.wait(timeout=10)
                conn.execute(
                    sql,
                    {
                        "id": uuid4(),
                        "tenant": seed["tenant_a"],
                        "provider": seed["provider_a"],
                        "patient": seed["patient_a"],
                        "service": seed["service_a"],
                    },
                )
            return "booked"
        except DBAPIError as error:
            return error.orig.sqlstate

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(insert, range(2)))
        assert results.count("booked") == 1
        assert next(value for value in results if value != "booked") in {"23P01", "40P01"}
    with engine.connect() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id',:tenant,true)"),
            {"tenant": str(seed["tenant_a"])},
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM appointments WHERE status='booked'")
            ).scalar_one()
            == 1
        )
    engine.dispose()


def test_database_rejects_cross_tenant_relationship_and_forces_buffers(seed):
    from uuid import uuid4

    from sqlalchemy.exc import IntegrityError

    engine = create_engine(APP_URL)
    stmt = text("""INSERT INTO appointments(id,tenant_id,provider_id,patient_id,service_id,starts_at,ends_at,duration_min,buffer_before_min,buffer_after_min)
    VALUES(:id,:tenant,:provider,:patient,:service,:start,:end,30,:before,:after)""")
    values = {
        "id": uuid4(),
        "tenant": seed["tenant_a"],
        "provider": seed["provider_b"],
        "patient": seed["patient_a"],
        "service": seed["service_a"],
        "start": "2026-11-03T05:00:00Z",
        "end": "2026-11-03T05:30:00Z",
        "before": 15,
        "after": 15,
    }
    with pytest.raises(IntegrityError) as cross, engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id',:tenant,true)"),
            {"tenant": str(seed["tenant_a"])},
        )
        conn.execute(stmt, values)
    assert cross.value.orig.sqlstate == "23503"
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id',:tenant,true)"),
            {"tenant": str(seed["tenant_a"])},
        )
        conn.execute(stmt, values | {"provider": seed["provider_a"]})
        assert (
            conn.execute(
                text("SELECT extract(minute FROM occupied_starts_at) FROM appointments")
            ).scalar_one()
            == 45
        )
    with pytest.raises(IntegrityError) as collision, engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id',:tenant,true)"),
            {"tenant": str(seed["tenant_a"])},
        )
        conn.execute(
            stmt,
            values
            | {
                "id": uuid4(),
                "provider": seed["provider_a"],
                "start": "2026-11-03T05:30:00Z",
                "end": "2026-11-03T06:00:00Z",
                "before": 0,
                "after": 0,
            },
        )
    assert collision.value.orig.sqlstate == "23P01"
    engine.dispose()


def test_application_refuses_ownership_of_any_public_table(database):
    with database.begin() as conn:
        conn.execute(text("CREATE TABLE unsafe_owner_check(id integer)"))
        conn.execute(text("ALTER TABLE unsafe_owner_check OWNER TO scheduler_app"))
    app = create_app(Settings(database_url=APP_URL, jwt_secret=SECRET, _env_file=None))
    try:
        with pytest.raises(RuntimeError, match="role"):
            with TestClient(app):
                pass
    finally:
        app.state.engine.dispose()
        with database.begin() as conn:
            conn.execute(text("DROP TABLE unsafe_owner_check"))
