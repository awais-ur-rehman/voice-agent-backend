"""Retention seals complete counter cohorts before bounded analytics-only deletion."""

from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from analytics.models import CallRecord, DailyRollup, DirtyBucket
from tests_analytics.test_rollups import NOW, populate, worker_factory


def test_sealed_counts_survive_raw_purge_and_late_dirty_without_source_deletion(
    database, analytics_seed
):
    from analytics.aggregation import run_once

    start = NOW - timedelta(days=91)
    populate(
        database,
        analytics_seed,
        started_at=start,
        expires_at=start + timedelta(hours=1),
        ended_at=start + timedelta(minutes=10),
        status="ended",
        outcome="info_only",
        local_date=start.date(),
    )
    factory = worker_factory(database)
    result = run_once(factory, clock=lambda: NOW)
    assert result["failed"] == 0
    with Session(database) as db, db.begin():
        row = db.scalar(select(DailyRollup))
        assert row.sessions == 1 and row.sealed_at == NOW
        assert db.scalar(select(CallRecord)) is None
        assert db.scalar(text("SELECT count(*) FROM agent_sessions")) == 2
        assert db.scalar(text("SELECT count(*) FROM patients")) == 2
        db.add(
            DirtyBucket(
                tenant_id=row.tenant_id,
                agent_id=row.agent_id,
                purpose=row.purpose,
                channel=row.channel,
                timezone=row.timezone,
                local_date=row.local_date,
                bucket_start=start,
                utc_offset_minutes=300,
                dirty_at=NOW,
            )
        )
    assert run_once(factory, clock=lambda: NOW)["failed"] == 0
    with Session(database) as db:
        assert db.scalar(select(DailyRollup)).sessions == 1
    assert run_once(factory, clock=lambda: NOW + timedelta(days=366))["failed"] == 0
    with Session(database) as db:
        assert db.scalar(select(DailyRollup)) is None


def test_disabled_worker_does_not_open_database(monkeypatch, capsys):
    import sqlalchemy

    from analytics_worker import main

    monkeypatch.setenv("ANALYTICS_WORKER_ENABLED", "false")
    monkeypatch.setenv("ANALYTICS_WORKER_DATABASE_URL", "not-a-url")
    monkeypatch.setattr("sys.argv", ["worker", "--once"])
    monkeypatch.setattr(
        sqlalchemy,
        "create_engine",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("disabled connected")),
    )
    main.main()
    assert "disabled" in capsys.readouterr().out


def test_private_metrics_rejects_staff_and_has_bounded_labels(owner):  # noqa: F811
    response = owner.get("/private/analytics/metrics")
    assert response.status_code == 403, response.text
    settings = owner.app.state.settings.model_copy(
        update={
            "analytics_metrics_secret": __import__("pydantic").SecretStr(
                "synthetic-operator-secret-01234567890123"
            )
        }
    )
    owner.app.state.settings = settings
    response = owner.get(
        "/private/analytics/metrics",
        headers={"Authorization": "Bearer synthetic-operator-secret-01234567890123"},
    )
    assert response.status_code == 200, response.text
    assert "analytics_projection_seconds_bucket" in response.text
    assert all(
        word not in response.text for word in ["tenant_id", "session_id", "agent_id", "email"]
    )
    assert response.headers["cache-control"] == "no-store"


from tests_api.conftest import client, owner  # noqa: E402,F401


def test_worker_qualification_rejects_owner_and_overgranted_role(database):
    import pytest
    from sqlalchemy import create_engine, event

    from analytics_worker.main import qualify
    from tests_api.conftest import ADMIN_URL

    with pytest.raises(RuntimeError, match="role_required"):
        qualify(database)
    engine = create_engine(ADMIN_URL)

    @event.listens_for(engine, "connect")
    def role(connection, record):
        connection.execute("SET ROLE analytics_worker")
        connection.commit()

    try:
        qualify(engine)
        with database.begin() as db:
            db.execute(text("GRANT SELECT ON patients TO analytics_worker"))
        with pytest.raises(RuntimeError, match="grants_unsafe"):
            qualify(engine)
    finally:
        with database.begin() as db:
            db.execute(text("REVOKE SELECT ON patients FROM analytics_worker"))
        engine.dispose()


def test_worker_tenant_cursor_is_fair_with_bounded_calls(database, analytics_seed):
    from analytics.aggregation import run_once

    populate(database, analytics_seed)
    with database.begin() as db:
        from tests_analytics.conftest import insert_call

        insert_call(
            db,
            analytics_seed,
            "b",
            started_at=NOW - timedelta(minutes=5),
            expires_at=NOW - timedelta(minutes=1),
        )
        db.execute(
            text("UPDATE analytics_calls SET expires_at=:expire"),
            {"expire": NOW - timedelta(minutes=1)},
        )
    factory = worker_factory(database)
    for _ in range(2):
        result = run_once(factory, clock=lambda: NOW, limit=1)
        assert result["tenants"] == 1 and result["failed"] == 0
    with Session(database) as db:
        assert (
            db.scalar(
                select(func.count()).select_from(CallRecord).where(CallRecord.ended_at.is_not(None))
            )
            == 2
        )


from sqlalchemy import func  # noqa: E402


def test_chronology_migration_refuses_to_invent_order_for_old_events(
    database, analytics_seed, monkeypatch
):
    from importlib import import_module
    from types import SimpleNamespace
    from uuid import uuid4

    import pytest
    from sqlalchemy.exc import DBAPIError

    populate(database, analytics_seed)
    migration = import_module("migrations.versions.0013_analytics_projection")
    with database.connect() as db:
        transaction = db.begin()
        try:
            db.execute(
                text(
                    "INSERT INTO analytics_turns(tenant_id,session_id,event_id,kind,language,occurred_at) VALUES(:t,:s,:e,'turn.user','en',:now)"
                ),
                {
                    "t": analytics_seed["tenant_a"],
                    "s": analytics_seed["session_a"],
                    "e": uuid4(),
                    "now": NOW,
                },
            )
            monkeypatch.setattr(
                migration, "op", SimpleNamespace(execute=lambda sql: db.execute(text(sql)))
            )
            with pytest.raises(
                DBAPIError, match="analytics_chronology_requires_empty_capture_history"
            ):
                migration.upgrade()
        finally:
            transaction.rollback()
