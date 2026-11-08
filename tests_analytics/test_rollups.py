"""Real SQL reconciliation and snapshot/retention boundaries, synthetic fixtures only."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from analytics.models import (
    CallRecord,
    DailyRollup,
    DirtyBucket,
    OperationEvent,
    TaskRecord,
    TurnEvent,
)
from tests_analytics.conftest import insert_call

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def worker_factory(database):
    from sqlalchemy import event

    factory = sessionmaker(database, expire_on_commit=False)

    @event.listens_for(factory, "after_begin")
    def role(session, transaction, connection):
        connection.execute(text("SET LOCAL ROLE analytics_worker"))

    return factory


def populate(database, ids, **overrides):
    with database.begin() as conn:
        insert_call(
            conn,
            ids,
            **(
                dict(started_at=NOW - timedelta(minutes=20), expires_at=NOW + timedelta(hours=1))
                | overrides
            ),
        )
    with Session(database) as db, db.begin():
        from analytics.lifecycle import dirty

        call = db.get(CallRecord, ids["session_a"])
        dirty(db, call, NOW)


def test_rollups_reconcile_repeat_and_late_task_operations(database, analytics_seed):
    import analytics.aggregation as aggregation

    populate(database, analytics_seed, ended_at=NOW, status="ended", outcome="rescheduled")
    with Session(database) as db, db.begin():
        for kind in ["book", "reschedule", "cancel"]:
            task = TaskRecord(
                tenant_id=analytics_seed["tenant_a"],
                session_id=analytics_seed["session_a"],
                kind=kind,
                status="completed",
                started_at=NOW - timedelta(minutes=10),
                ended_at=NOW,
                caller_turns=2,
            )
            db.add(task)
            db.flush()
            db.add(
                OperationEvent(
                    tenant_id=task.tenant_id,
                    session_id=task.session_id,
                    task_id=task.id,
                    operation=kind,
                    event_id=uuid4(),
                    occurred_at=NOW,
                )
            )
    factory = worker_factory(database)
    aggregation.run_once(factory, clock=lambda: NOW)
    aggregation.run_once(factory, clock=lambda: NOW)
    with Session(database) as db:
        row = db.scalar(select(DailyRollup))
        assert (
            row.sessions,
            row.ended_sessions,
            row.booking_sessions,
            row.book_operations,
            row.reschedule_operations,
            row.cancel_operations,
        ) == (1, 1, 1, 1, 1, 1)
        assert (row.attempted_tasks, row.completed_tasks, row.caller_turns) == (3, 3, 6)
        assert row.usage_invocations == row.usage_known == row.cost_known == 0
        assert db.scalar(select(DirtyBucket)) is None
    with Session(database) as db, db.begin():
        from analytics.lifecycle import dirty

        call = db.get(CallRecord, analytics_seed["session_a"])
        db.add(
            TurnEvent(
                tenant_id=call.tenant_id,
                session_id=call.session_id,
                event_id=uuid4(),
                kind="tool.result",
                language="en",
                tool_name="ask_caller",
                tool_status="failure",
                safe_metadata={},
                occurred_at=NOW,
            )
        )
        dirty(db, call, NOW)
    aggregation.run_once(factory, clock=lambda: NOW)
    with Session(database) as db:
        row = db.scalar(select(DailyRollup))
        assert (row.sessions, row.tool_results, row.tool_failures) == (1, 1, 1)


def test_worker_expires_info_only_and_never_uses_recovery(database, analytics_seed):
    import analytics.aggregation as aggregation

    populate(database, analytics_seed)
    with Session(database) as db, db.begin():
        call = db.get(CallRecord, analytics_seed["session_a"])
        call.expires_at = NOW - timedelta(seconds=1)
        db.add(
            TurnEvent(
                tenant_id=call.tenant_id,
                session_id=call.session_id,
                event_id=uuid4(),
                kind="tool.result",
                language="en",
                tool_name="get_clinic_info",
                tool_status="success",
                safe_metadata={},
                occurred_at=NOW - timedelta(minutes=1),
            )
        )
    aggregation.run_once(worker_factory(database), clock=lambda: NOW)
    with Session(database) as db:
        call = db.get(CallRecord, analytics_seed["session_a"])
        assert call.status == "expired" and call.outcome == "info_only"
        row = db.scalar(select(DailyRollup))
        assert row.attempted_tasks == 0 and row.ended_sessions == 1


@pytest.mark.parametrize(
    "timezone,offset,stamp,expected",
    [
        ("Asia/Kathmandu", 345, "2026-11-08T06:38:00+00:00", "2026-11-08T06:15:00+00:00"),
        ("America/New_York", -240, "2026-11-01T05:30:00+00:00", "2026-11-01T05:00:00+00:00"),
        ("America/New_York", -300, "2026-11-01T06:30:00+00:00", "2026-11-01T06:00:00+00:00"),
    ],
)
def test_hour_bucket_uses_historical_offset_including_quarter_hour_and_fold(
    database, analytics_seed, timezone, offset, stamp, expected
):
    from analytics.lifecycle import dirty

    populate(database, analytics_seed, timezone=timezone, utc_offset_minutes=offset)
    with Session(database) as db, db.begin():
        db.query(DirtyBucket).delete()
        call = db.get(CallRecord, analytics_seed["session_a"])
        call.started_at = datetime.fromisoformat(stamp)
        dirty(db, call, NOW)
        assert db.scalar(select(DirtyBucket)).bucket_start == datetime.fromisoformat(expected)


def test_concurrent_capture_revision_survives_mvcc_recompute_without_live_call_lock(
    database, analytics_seed
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event

    from analytics.aggregation import run_once
    from analytics.lifecycle import dirty

    populate(database, analytics_seed)
    snapshot_read, proceed = Event(), Event()
    blocked = False

    def pause(conn, cursor, statement, parameters, context, executemany):
        nonlocal blocked
        if statement.startswith("WITH facts AS") and not blocked:
            blocked = True
            snapshot_read.set()
            assert proceed.wait(5)

    event.listen(database, "after_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(run_once, worker_factory(database), clock=lambda: NOW)
            assert snapshot_read.wait(5)
            try:
                # Same Call -> Dirty order as committed runtime capture, while the
                # worker has already read its old snapshot and is deliberately slow.
                with Session(database) as db, db.begin():
                    db.execute(text("SET LOCAL lock_timeout='500ms'"))
                    call = db.scalar(select(CallRecord).with_for_update())
                    db.add(
                        TurnEvent(
                            tenant_id=call.tenant_id,
                            session_id=call.session_id,
                            event_id=uuid4(),
                            kind="tool.result",
                            language="en",
                            tool_name="ask_caller",
                            tool_status="failure",
                            safe_metadata={},
                            occurred_at=NOW,
                        )
                    )
                    dirty(db, call, NOW)
            finally:
                proceed.set()
            assert task.result(timeout=10)["failed"] == 0
    finally:
        event.remove(database, "after_cursor_execute", pause)
        proceed.set()
    with Session(database) as db:
        assert db.scalar(select(DailyRollup)).tool_failures == 1
        assert db.scalar(select(DirtyBucket)) is None


def test_failure_rolls_back_replacement_and_acknowledgement(database, analytics_seed, monkeypatch):
    import analytics.aggregation as aggregation

    populate(database, analytics_seed)
    original = aggregation.recompute_day

    def fail(db, *args):
        original(db, *args)
        db.execute(text("SELECT 1/0"))

    monkeypatch.setattr(aggregation, "recompute_day", fail)
    assert aggregation.run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 1
    with Session(database) as db:
        assert db.scalar(select(DailyRollup)) is None
        assert db.scalar(select(DirtyBucket)).revision == 1


def test_expiry_batch_does_not_hold_dirty_while_waiting_for_second_call(database, analytics_seed):
    from concurrent.futures import ThreadPoolExecutor

    from analytics.aggregation import run_once
    from analytics.lifecycle import dirty

    populate(database, analytics_seed, expires_at=NOW - timedelta(minutes=1))
    with database.begin() as db:
        sid = uuid4()
        db.execute(
            text(
                "INSERT INTO agent_sessions(id,tenant_id,agent_id,version_id,token_jti_hash,expires_at) SELECT :sid,tenant_id,agent_id,version_id,'second',expires_at FROM agent_sessions WHERE id=:first"
            ),
            {"sid": sid, "first": analytics_seed["session_a"]},
        )
        insert_call(
            db,
            analytics_seed,
            session_id=sid,
            started_at=NOW - timedelta(minutes=10),
            expires_at=NOW - timedelta(seconds=1),
        )
    with Session(database) as held, held.begin():
        second = held.scalar(
            select(CallRecord).where(CallRecord.session_id == sid).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(run_once, worker_factory(database), clock=lambda: NOW).result(
                timeout=8
            )
            assert result["expired"] == 1 and result["failed"] == 0
        second.expires_at = NOW + timedelta(hours=1)
        dirty(held, second, NOW)
    with Session(database) as db:
        assert db.get(CallRecord, sid).ended_at is None
        assert db.scalar(select(DailyRollup)).ended_sessions == 1


def test_query_snapshot_is_consistent_across_independent_counter_and_freshness_reads(
    database, analytics_seed
):
    from analytics.aggregation import configure, run_once
    from analytics.queries import overview
    from analytics.views import Filters

    populate(database, analytics_seed)
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    with Session(database) as snapshot, snapshot.begin():
        configure(snapshot, analytics_seed["tenant_a"], snapshot=True)
        assert snapshot.scalar(select(DailyRollup.sessions)) == 1
        with database.begin() as conn:
            conn.execute(text("UPDATE analytics_daily_rollups SET sessions=2"))
            conn.execute(
                text(
                    "INSERT INTO analytics_dirty_buckets(tenant_id,agent_id,purpose,channel,timezone,local_date,bucket_start,utc_offset_minutes) SELECT tenant_id,agent_id,purpose,channel,timezone,local_date,started_at,utc_offset_minutes FROM analytics_calls"
                )
            )
        response = overview(
            snapshot, Filters(start=NOW.date(), end=NOW.date(), channel="text"), NOW
        )
        assert response["sessions"] == 1 and response["freshness"]["pending_buckets"] == 0


def test_quality_denominator_only_eligible_completed_prompts_and_handoff_fact(
    database, analytics_seed
):
    from analytics.aggregation import run_once

    populate(
        database, analytics_seed, ended_at=NOW, status="failed", outcome="error", end_reason="error"
    )
    with Session(database) as db, db.begin():
        call = db.get(CallRecord, analytics_seed["session_a"])
        for prompt, delivery, repeated in [
            ("greeting", "generated", False),
            ("info", "generated", False),
            ("ask_date", "generated", False),
            ("ask_date", "generated", True),
            ("ask_date", "interrupted", True),
        ]:
            db.add(
                TurnEvent(
                    tenant_id=call.tenant_id,
                    session_id=call.session_id,
                    event_id=uuid4(),
                    kind="turn.assistant",
                    language="en",
                    safe_metadata={
                        "prompt_id": prompt,
                        "delivery": delivery,
                        "repeated_prompt": repeated,
                    },
                    occurred_at=NOW,
                )
            )
        db.add(
            TurnEvent(
                tenant_id=call.tenant_id,
                session_id=call.session_id,
                event_id=uuid4(),
                kind="handoff.requested",
                language="en",
                safe_metadata={},
                occurred_at=NOW,
            )
        )
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    with Session(database) as db:
        row = db.scalar(select(DailyRollup))
        assert (row.prompt_emissions, row.repeated_prompts) == (2, 1)
        assert row.handoff_sessions == 1


@pytest.mark.parametrize(
    "reason,outcome,unfinished,expected",
    [
        ("caller_end", "abandoned", False, 1),
        ("caller_end", "abandoned", True, 1),
        ("timeout", "abandoned", True, 1),
        ("timeout", "booked", True, 0),
        ("timeout", "info_only", False, 0),
        ("handoff", "handoff", True, 0),
        ("error", "error", True, 0),
    ],
)
def test_abandonment_kpi_follows_canonical_ended_outcome_not_timeout_only(
    database, analytics_seed, reason, outcome, unfinished, expected
):
    from analytics.aggregation import run_once

    populate(
        database, analytics_seed, ended_at=NOW, status="ended", outcome=outcome, end_reason=reason
    )
    if unfinished:
        with Session(database) as db, db.begin():
            db.add(
                TaskRecord(
                    tenant_id=analytics_seed["tenant_a"],
                    session_id=analytics_seed["session_a"],
                    kind="book",
                    status="abandoned",
                    started_at=NOW - timedelta(minutes=10),
                    ended_at=NOW,
                    caller_turns=1,
                )
            )
    assert run_once(worker_factory(database), clock=lambda: NOW)["failed"] == 0
    with Session(database) as db:
        row = db.scalar(select(DailyRollup))
        assert row.ended_sessions == 1 and row.abandoned_sessions == expected
        assert db.get(CallRecord, analytics_seed["session_a"]).end_reason == reason
