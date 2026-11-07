"""Analytics worker can aggregate safe analytics, never clinical/recovery/text."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests_analytics.conftest import insert_call
from tests_api.conftest import APP_URL


def test_worker_created_disabled_and_without_privilege_escalation(database):
    with database.connect() as db:
        role = db.execute(
            text(
                "SELECT rolcanlogin,rolsuper,rolbypassrls,rolinherit,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname='analytics_worker'"
            )
        ).one_or_none()
        assert role == (False, False, False, False, False, False, False)
        for table in (
            "patients",
            "appointments",
            "users",
            "refresh_tokens",
            "agent_sessions",
            "voice_sessions",
            "command_receipts",
            "knowledge_sources",
            "knowledge_chunks",
            "knowledge_jobs",
            "knowledge_query_events",
        ):
            assert not db.execute(
                text("SELECT has_table_privilege('analytics_worker',:table,'SELECT')"),
                dict(table=table),
            ).scalar_one(), table
        assert not db.execute(
            text(
                "SELECT has_column_privilege('analytics_worker','analytics_transcripts','text','SELECT')"
            )
        ).scalar_one()
        assert not db.execute(
            text("SELECT has_table_privilege('analytics_worker','analytics_transcripts','INSERT')")
        ).scalar_one()


def test_empty_optional_secret_rejected_before_changing_existing_roles(database):
    from scripts.init_db import init_database
    from tests_api.conftest import ADMIN_URL

    with database.connect() as db:
        before = db.execute(
            text("SELECT rolpassword FROM pg_authid WHERE rolname='scheduler_app'")
        ).scalar_one()
    with pytest.raises(ValueError):
        init_database(ADMIN_URL, "phase1_app_test_only", analytics_worker_password="")
    with database.connect() as db:
        after = db.execute(
            text("SELECT rolpassword FROM pg_authid WHERE rolname='scheduler_app'")
        ).scalar_one()
    unchanged = before == after
    assert unchanged, "invalid optional configuration must be side-effect-free"


def test_rls_hides_other_clinic_without_context_and_blocks_foreign_writes(database, analytics_seed):
    with database.begin() as db:
        insert_call(db, analytics_seed)
        insert_call(db, analytics_seed, "b")
    for role in ("analytics_worker", "scheduler_app"):
        with database.begin() as db:
            db.execute(text(f"SET LOCAL ROLE {role}"))
            assert db.execute(text("SELECT count(*) FROM analytics_calls")).scalar_one() == 0
            db.execute(
                text("SELECT set_config('app.tenant_id',:tenant,true)"),
                dict(tenant=str(analytics_seed["tenant_a"])),
            )
            assert db.execute(text("SELECT session_id FROM analytics_calls")).scalars().all() == [
                analytics_seed["session_a"]
            ]
        with pytest.raises(DBAPIError), database.begin() as db:
            db.execute(text(f"SET LOCAL ROLE {role}"))
            db.execute(
                text("SELECT set_config('app.tenant_id',:tenant,true)"),
                dict(tenant=str(analytics_seed["tenant_a"])),
            )
            db.execute(
                text("UPDATE analytics_calls SET tenant_id=:other"),
                dict(other=analytics_seed["tenant_b"]),
            )


def test_discovery_is_worker_only_bounded_and_backlog_is_tenant_scoped(database, analytics_seed):
    with database.begin() as db:
        insert_call(db, analytics_seed)
        insert_call(db, analytics_seed, "b")
        db.execute(text("SET LOCAL ROLE analytics_worker"))
        assert db.execute(text("SELECT * FROM analytics_backlog()")).one() == (0, None)
        rows = db.execute(text("SELECT * FROM analytics_discover_tenants(NULL,1)")).scalars().all()
        assert len(rows) == 1
        tail = (
            db.execute(
                text("SELECT * FROM analytics_discover_tenants(:after,50)"), dict(after=rows[0])
            )
            .scalars()
            .all()
        )
        assert len(tail) == 1 and rows[0] not in tail
        db.execute(
            text("SELECT set_config('app.tenant_id',:tenant,true)"),
            dict(tenant=str(analytics_seed["tenant_a"])),
        )
        assert db.execute(text("SELECT * FROM analytics_backlog()")).one() == (0, None)
    engine = create_engine(APP_URL)
    try:
        for sql in (
            "SELECT * FROM analytics_discover_tenants(NULL,50)",
            "SELECT * FROM analytics_backlog()",
        ):
            with pytest.raises(DBAPIError), engine.begin() as db:
                db.execute(text(sql))
    finally:
        engine.dispose()


def test_worker_backlog_observes_only_current_tenant_counts_and_age(database, analytics_seed):
    from uuid import uuid4

    with database.begin() as db:
        for suffix, minutes in [("a", 20), ("b", 60)]:
            values = dict(
                tenant=analytics_seed[f"tenant_{suffix}"],
                agent=analytics_seed[f"agent_{suffix}"],
                source=uuid4(),
                minutes=minutes,
            )
            db.execute(
                text(
                    "INSERT INTO knowledge_sources(id,tenant_id,agent_id,kind,name) VALUES(:source,:tenant,:agent,'text','Synthetic private source')"
                ),
                values,
            )
            db.execute(
                text(
                    "INSERT INTO knowledge_jobs(tenant_id,agent_id,source_id,source_revision,created_at) VALUES(:tenant,:agent,:source,1,clock_timestamp()-make_interval(mins=>:minutes))"
                ),
                values,
            )
        db.execute(text("SET LOCAL ROLE analytics_worker"))
        db.execute(
            text("SELECT set_config('app.tenant_id',:tenant,true)"),
            dict(tenant=str(analytics_seed["tenant_a"])),
        )
        count, age = db.execute(text("SELECT * FROM analytics_backlog()")).one()
        assert count == 1 and 1199 <= age <= 1205
        assert list(
            db.execute(text("SELECT * FROM analytics_discover_tenants(NULL,100000)")).keys()
        ) == ["tenant_id"]
    with pytest.raises(DBAPIError) as error, database.begin() as db:
        db.execute(text("SET LOCAL ROLE analytics_worker"))
        db.execute(text("SELECT text FROM analytics_transcripts"))
    assert error.value.orig.sqlstate == "42501"


def test_optional_worker_provisioning_preserves_existing_call_signature_and_secrets(database):
    from scripts.init_db import init_database
    from tests_api.conftest import ADMIN_URL

    def role_snapshot():
        with database.connect() as db:
            return db.execute(
                text(
                    "SELECT rolname,rolpassword,rolcanlogin FROM pg_authid WHERE rolname IN ('scheduler_app','knowledge_worker','analytics_worker') ORDER BY rolname"
                )
            ).all()

    before = role_snapshot()
    try:
        init_database(
            ADMIN_URL,
            "phase1_app_test_only",
            analytics_worker_password="isolated-analytics-test-only",
        )
        provisioned = role_snapshot()
        assert bool(provisioned[0].rolcanlogin)
        # Avoid including credential hashes in assertion diagnostics.
        assert bool(before[1] == provisioned[1])
        init_database(ADMIN_URL, "phase1_app_test_only")
        preserved = role_snapshot()
        assert bool(provisioned[0] == preserved[0])
        assert bool(provisioned[1] == preserved[1])
    finally:
        with database.begin() as db:
            db.execute(text("ALTER ROLE analytics_worker NOLOGIN PASSWORD NULL"))
