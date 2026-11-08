"""Small lifecycle transactions, then repeatable-read replace/recompute with revision CAS.

No raw patient, recovery, source, or transcript text is read. A day is sealed
before its first raw admission expires; sealed counts never read partial raw data.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID
from weakref import WeakKeyDictionary

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from analytics.lifecycle import classify_outcome, dirty
from analytics.models import (
    CallRecord,
    DailyRollup,
    DirtyBucket,
    HourlyRollup,
    TaskRecord,
    TurnEvent,
    WorkerState,
)
from scheduler.db import tenant_context

KEYS = ["tenant_id", "agent_id", "purpose", "channel", "timezone", "local_date"]
COUNTERS = [
    "sessions",
    "ended_sessions",
    "booking_sessions",
    "handoff_sessions",
    "abandoned_sessions",
    "book_operations",
    "cancel_operations",
    "reschedule_operations",
    "attempted_tasks",
    "completed_tasks",
    "caller_turns",
    "tool_results",
    "tool_failures",
    "prompt_emissions",
    "repeated_prompts",
    "ended_duration_samples",
    "ended_duration_ms",
    "usage_invocations",
    "usage_known",
    "cost_known",
    "input_tokens",
    "output_tokens",
    "known_cost",
]
_CURSORS: WeakKeyDictionary = WeakKeyDictionary()


def configure(db: Session, tenant: UUID | None = None, *, snapshot: bool = False):
    # Must be the first statement, including before setting tenant context.
    if snapshot:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    db.execute(text("SET LOCAL statement_timeout='5s'"))
    db.execute(text("SET LOCAL lock_timeout='1s'"))
    if tenant is not None:
        tenant_context(db, tenant)


# Each child is aggregated once per call before joins, avoiding task x event inflation.
FACTS_SQL = """
SELECT c.*,
 (date_trunc('hour',c.started_at AT TIME ZONE 'UTC'+make_interval(mins=>c.utc_offset_minutes))
    -make_interval(mins=>c.utc_offset_minutes)) AT TIME ZONE 'UTC' AS bucket_start,
 COALESCE(o.book_operations,0) AS book_operations, COALESCE(o.cancel_operations,0) AS cancel_operations,
 COALESCE(o.reschedule_operations,0) AS reschedule_operations,
 COALESCE(t.attempted_tasks,0) AS attempted_tasks,COALESCE(t.completed_tasks,0) AS completed_tasks,
 COALESCE(t.caller_turns,0) AS caller_turns,
 COALESCE(e.tool_results,0) AS tool_results,COALESCE(e.tool_failures,0) AS tool_failures,
 COALESCE(e.handoff,false) AS committed_handoff,
 COALESCE(e.prompt_emissions,0) AS prompt_emissions,COALESCE(e.repeated_prompts,0) AS repeated_prompts,
 COALESCE(u.usage_invocations,0) AS usage_invocations,COALESCE(u.usage_known,0) AS usage_known,
 COALESCE(u.cost_known,0) AS cost_known,COALESCE(u.input_tokens,0) AS input_tokens,
 COALESCE(u.output_tokens,0) AS output_tokens,COALESCE(u.known_cost,0) AS known_cost
FROM analytics_calls c
LEFT JOIN LATERAL (SELECT count(*) FILTER(WHERE operation='book') AS book_operations,
 count(*) FILTER(WHERE operation='cancel') AS cancel_operations,
 count(*) FILTER(WHERE operation='reschedule') AS reschedule_operations
 FROM analytics_operations WHERE session_id=c.session_id AND tenant_id=c.tenant_id) o ON true
LEFT JOIN LATERAL (SELECT count(*) AS attempted_tasks,count(*) FILTER(WHERE status='completed') AS completed_tasks,
 sum(caller_turns) AS caller_turns FROM analytics_tasks WHERE session_id=c.session_id AND tenant_id=c.tenant_id) t ON true
LEFT JOIN LATERAL (SELECT count(*) FILTER(WHERE kind='tool.result') AS tool_results,
 count(*) FILTER(WHERE kind='tool.result' AND tool_status='failure') AS tool_failures,
 bool_or(kind='handoff.requested') AS handoff,
 count(*) FILTER(WHERE kind='turn.assistant' AND safe_metadata->>'prompt_id' IN ('ask_date','ask_need','ask_patient','ask_verification','ask_slot','ask_appointment','readback') AND safe_metadata->>'delivery' IN ('generated','delivered')) AS prompt_emissions,
 count(*) FILTER(WHERE kind='turn.assistant' AND safe_metadata->>'prompt_id' IN ('ask_date','ask_need','ask_patient','ask_verification','ask_slot','ask_appointment','readback') AND safe_metadata->>'delivery' IN ('generated','delivered') AND safe_metadata->>'repeated_prompt'='true') AS repeated_prompts
 FROM analytics_turns WHERE session_id=c.session_id AND tenant_id=c.tenant_id) e ON true
LEFT JOIN LATERAL (SELECT count(*) AS usage_invocations,
 count(*) FILTER(WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL) AS usage_known,
 count(cost) AS cost_known,
 sum(input_tokens) FILTER(WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL) AS input_tokens,
 sum(output_tokens) FILTER(WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL) AS output_tokens,
 sum(cost) AS known_cost
 FROM analytics_usage_invocations WHERE session_id=c.session_id AND tenant_id=c.tenant_id) u ON true
WHERE c.tenant_id=:tenant_id AND c.agent_id=:agent_id AND c.purpose=:purpose
 AND c.channel=:channel AND c.timezone=:timezone AND c.local_date=:local_date
"""
SUMS_SQL = ",".join(
    "sum(" + k + ") AS " + k
    for k in COUNTERS[5:]
    if k not in ["ended_duration_samples", "ended_duration_ms"]
)
ROLLUP_SQL = (
    """WITH facts AS ("""
    + FACTS_SQL
    + """)
SELECT bucket_start,utc_offset_minutes,count(*) AS sessions,
 count(*) FILTER(WHERE ended_at IS NOT NULL) AS ended_sessions,
 count(*) FILTER(WHERE ended_at IS NOT NULL AND book_operations>0) AS booking_sessions,
 count(*) FILTER(WHERE ended_at IS NOT NULL AND committed_handoff) AS handoff_sessions,
 count(*) FILTER(WHERE ended_at IS NOT NULL AND outcome='abandoned') AS abandoned_sessions,
 count(*) FILTER(WHERE ended_at IS NOT NULL) AS ended_duration_samples,
 COALESCE(sum(GREATEST(0,extract(epoch FROM ended_at-started_at)*1000)) FILTER(WHERE ended_at IS NOT NULL),0) AS ended_duration_ms,
 """
    + SUMS_SQL
    + " FROM facts GROUP BY bucket_start,utc_offset_minutes ORDER BY bucket_start"
)


def recompute_day(db: Session, key: dict, now: datetime) -> bool:
    filters = [getattr(DailyRollup, k) == v for k, v in key.items()]
    existing = db.scalar(select(DailyRollup).where(*filters))
    if existing and existing.sealed_at is not None:
        return False
    rows = db.execute(text(ROLLUP_SQL), key).mappings().all()
    seal = bool(
        db.scalar(
            select(func.count())
            .select_from(CallRecord)
            .where(
                *[getattr(CallRecord, k) == v for k, v in key.items()],
                CallRecord.started_at <= now - timedelta(days=90),
            )
        )
    )
    unfinished = bool(
        db.scalar(
            select(func.count())
            .select_from(CallRecord)
            .where(
                *[getattr(CallRecord, k) == v for k, v in key.items()],
                CallRecord.ended_at.is_(None),
            )
        )
    )
    sealed = now if seal and not unfinished else None
    totals = {k: 0 for k in COUNTERS}
    for row in rows:
        values = dict(row)
        for k in COUNTERS:
            totals[k] += values[k]
        stmt = insert(HourlyRollup).values(**key, **values, as_of=now, sealed_at=sealed)
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=KEYS + ["bucket_start"],
                set_=values | {"as_of": now, "sealed_at": sealed},
            )
        )
    stmt = insert(DailyRollup).values(**key, **totals, as_of=now, sealed_at=sealed)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=KEYS, set_=totals | {"as_of": now, "sealed_at": sealed}
        )
    )
    return True


def rollup_batch(db: Session, tenant: UUID, now: datetime, limit: int) -> int:
    # Plain MVCC reads: no dirty lock while computing or waiting for live call rows.
    buckets = list(
        db.scalars(select(DirtyBucket).order_by(DirtyBucket.dirty_at, DirtyBucket.id).limit(limit))
    )
    keys = {tuple(getattr(row, k) for k in KEYS) for row in buckets}
    # Seal a complete day before any 90-day deletion, including clean days.
    due = db.execute(
        select(*[getattr(CallRecord, k) for k in KEYS])
        .where(CallRecord.started_at <= now - timedelta(days=90))
        .distinct()
        .order_by(CallRecord.local_date)
        .limit(limit)
    ).all()
    keys.update(tuple(row) for row in due)
    for values in sorted(keys, key=str):
        recompute_day(db, dict(zip(KEYS, values)), now)
    for bucket in buckets:
        db.execute(
            delete(DirtyBucket).where(
                DirtyBucket.id == bucket.id, DirtyBucket.revision == bucket.revision
            )
        )
    return len(buckets)


def expire_call(db: Session, call: CallRecord, now: datetime):
    tasks = list(db.scalars(select(TaskRecord).where(TaskRecord.session_id == call.session_id)))
    unfinished = any(t.status != "completed" for t in tasks)
    for task in tasks:
        if task.status in {"active", "parked"}:
            task.status, task.ended_at = "abandoned", call.expires_at
    info = (
        db.scalar(
            select(TurnEvent.id)
            .where(
                TurnEvent.session_id == call.session_id,
                TurnEvent.kind == "tool.result",
                TurnEvent.tool_status == "success",
                TurnEvent.tool_name.in_(["get_clinic_info", "search_knowledge"]),
            )
            .limit(1)
        )
        is not None
    )
    call.outcome = classify_outcome(
        call.outcome, reason="timeout", unfinished=unfinished, answered_info=info
    )
    call.status, call.end_reason = "expired", "timeout"
    call.ended_at = call.expires_at
    call.updated_at = now
    call.active_task_id = None
    call.parked_task_ids = []
    dirty(db, call, now)


def prune(db: Session, now: datetime, limit: int) -> dict:
    # Only analytics tables. Text expiry is also enforced synchronously on reads/writes.
    result = {}
    result["transcripts"] = cast(
        CursorResult,
        db.execute(
            text("""DELETE FROM analytics_transcripts WHERE id IN (
      SELECT t.id FROM analytics_transcripts t LEFT JOIN analytics_settings s USING(tenant_id)
      WHERE t.expires_at<=:now OR t.occurred_at<=:now-make_interval(days=>COALESCE(s.retention_days,7))
      ORDER BY t.expires_at,t.id LIMIT :limit)"""),
            {"now": now, "limit": limit},
        ),
    ).rowcount
    result["calls"] = cast(
        CursorResult,
        db.execute(
            text("""DELETE FROM analytics_calls c WHERE c.session_id IN (
      SELECT c.session_id FROM analytics_calls c JOIN analytics_daily_rollups d
      USING(tenant_id,agent_id,purpose,channel,timezone,local_date)
      WHERE c.started_at<=:cutoff AND c.ended_at IS NOT NULL AND d.sealed_at IS NOT NULL
      ORDER BY c.started_at,c.session_id LIMIT :limit FOR UPDATE OF c SKIP LOCKED)"""),
            {"cutoff": now - timedelta(days=90), "limit": limit},
        ),
    ).rowcount
    for table in ["analytics_hourly_rollups", "analytics_daily_rollups"]:
        result[table] = cast(
            CursorResult,
            db.execute(
                text(
                    f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE local_date<:cutoff ORDER BY local_date,id LIMIT :limit)"
                ),
                {"cutoff": (now - timedelta(days=365)).date(), "limit": limit},
            ),
        ).rowcount
    result["alerts"] = cast(
        CursorResult,
        db.execute(
            text(
                "DELETE FROM analytics_alerts WHERE id IN (SELECT id FROM analytics_alerts WHERE state='resolved' AND last_seen_at<:cutoff ORDER BY last_seen_at,id LIMIT :limit)"
            ),
            {"cutoff": now - timedelta(days=90), "limit": limit},
        ),
    ).rowcount
    return result


def run_once(factory: sessionmaker, *, clock: Callable[[], datetime], limit: int = 100) -> dict:
    limit = max(1, min(limit, 100))
    now = clock()
    if now.tzinfo is None:
        raise ValueError("aware_clock_required")
    state = _CURSORS.setdefault(factory, {"tenant": None, "agents": {}})
    with factory() as db, db.begin():
        configure(db)
        tenants = list(
            db.scalars(
                text("SELECT * FROM analytics_discover_tenants(:after,:limit)"),
                {"after": state["tenant"], "limit": min(limit, 50)},
            )
        )
        if not tenants and state["tenant"] is not None:
            tenants = list(
                db.scalars(
                    text("SELECT * FROM analytics_discover_tenants(NULL,:limit)"),
                    {"limit": min(limit, 50)},
                )
            )
    counts = {"tenants": 0, "expired": 0, "buckets": 0, "failed": 0, "skipped": 0}
    for tenant in tenants:
        state["tenant"] = tenant
        # Dedicated connection/session lock spans short transactions, NOT row locks.
        with factory.kw["bind"].connect() as conn:
            lock = int.from_bytes(tenant.bytes[:8], "big", signed=True)
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": lock}
            ).scalar()
            conn.commit()
            if not acquired:
                counts["skipped"] += 1
                continue
            try:

                def scoped(*, snapshot=False):
                    db = factory(bind=conn)
                    db.begin()
                    configure(db, snapshot=snapshot)
                    tenant_context(db, tenant)
                    return db

                with scoped() as db:
                    ids = list(
                        db.scalars(
                            select(CallRecord.session_id)
                            .where(CallRecord.ended_at.is_(None), CallRecord.expires_at <= now)
                            .order_by(CallRecord.expires_at, CallRecord.session_id)
                            .limit(limit)
                        )
                    )
                    db.commit()
                for sid in ids:
                    # Never Call A -> Dirty -> Call B; one lifecycle row per transaction.
                    with scoped() as db:
                        call = db.scalar(
                            select(CallRecord)
                            .where(
                                CallRecord.session_id == sid,
                                CallRecord.ended_at.is_(None),
                                CallRecord.expires_at <= now,
                            )
                            .with_for_update(skip_locked=True)
                        )
                        if call:
                            expire_call(db, call, now)
                        db.commit()
                        if call:
                            counts["expired"] += 1
                for attempt in range(3):
                    try:
                        with scoped(snapshot=True) as db:
                            processed = rollup_batch(db, tenant, now, limit)
                            db.commit()
                            counts["buckets"] += processed
                        break
                    except DBAPIError as exc:
                        if (
                            getattr(exc.orig, "sqlstate", None) not in {"40001", "40P01"}
                            or attempt == 2
                        ):
                            raise
                from analytics.alerts import evaluate

                with scoped(snapshot=True) as db:
                    cursor, complete = evaluate(db, tenant, now, limit, state["agents"].get(tenant))
                    db.commit()
                    state["agents"][tenant] = cursor
                with scoped() as db:
                    prune(db, now, limit)
                    if complete:
                        stmt = insert(WorkerState).values(tenant_id=tenant, last_success_at=now)
                        db.execute(
                            stmt.on_conflict_do_update(
                                index_elements=["tenant_id"], set_={"last_success_at": now}
                            )
                        )
                    else:
                        db.execute(delete(WorkerState).where(WorkerState.tenant_id == tenant))
                    db.commit()
                counts["tenants"] += 1
            except DBAPIError:
                counts["failed"] += 1
            finally:
                conn.rollback()
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})
                conn.commit()
    return counts
