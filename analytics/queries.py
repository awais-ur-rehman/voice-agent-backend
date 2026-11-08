"""Read projections on one caller-established repeatable-read tenant snapshot."""

import base64
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import get_args
from uuid import UUID

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from analytics.aggregation import COUNTERS
from analytics.contracts import Stage
from analytics.models import (
    AnalyticsAlert,
    AnalyticsSettings,
    CallRecord,
    DailyRollup,
    DirtyBucket,
    HourlyRollup,
    OperationEvent,
    StageMetric,
    TaskRecord,
    TurnEvent,
    UsageInvocation,
    WorkerState,
)
from analytics.views import CallFilters, Filters, Ratio
from scheduler.errors import AppError
from scheduler.models import Agent


def conditions(model, filters: Filters):
    result = [
        model.purpose == "production",
        model.local_date >= filters.start,
        model.local_date <= filters.end,
    ]
    if filters.channel != "all":
        result.append(model.channel == filters.channel)
    if filters.agent_id:
        result.append(model.agent_id == filters.agent_id)
    if filters.timezone:
        result.append(model.timezone == filters.timezone)
    return result


def validate_scope(db: Session, f: Filters, now: datetime):
    if f.start < (now - timedelta(days=365)).date() or f.end > now.date() + timedelta(days=1):
        raise AppError("analytics_date_range", 422)
    if f.agent_id and db.scalar(select(Agent.id).where(Agent.id == f.agent_id)) is None:
        raise AppError("not_found", 404)


def ratio(n: int, d: int) -> dict:
    return Ratio(numerator=n, denominator=d, value=n / d if d else None).model_dump()


def meta(db: Session, f: Filters, now: datetime, *, raw=False) -> dict:
    heartbeat = db.scalar(select(WorkerState.last_success_at))
    dirty_count = (
        db.scalar(select(func.count()).select_from(DirtyBucket).where(*conditions(DirtyBucket, f)))
        or 0
    )
    expired = (
        db.scalar(
            select(func.count())
            .select_from(CallRecord)
            .where(
                *conditions(CallRecord, f),
                CallRecord.ended_at.is_(None),
                CallRecord.expires_at <= now,
            )
        )
        or 0
    )
    zones = set(
        db.scalars(select(DailyRollup.timezone).where(*conditions(DailyRollup, f)).distinct())
    )
    zones.update(
        db.scalars(select(CallRecord.timezone).where(*conditions(CallRecord, f)).distinct())
    )
    return dict(
        filters=f,
        timezones=sorted(zones),
        snapshot_at=now,
        freshness=dict(
            as_of=now if raw else heartbeat,
            stale=heartbeat is None
            or heartbeat < now - timedelta(seconds=60)
            or dirty_count > 0
            or expired > 0,
            pending_buckets=dirty_count,
            pending_expirations=expired,
            source="raw_snapshot" if raw else "persisted_rollups",
        ),
    )


def totals(db: Session, f: Filters) -> dict:
    row = (
        db.execute(
            select(
                *[func.coalesce(func.sum(getattr(DailyRollup, k)), 0).label(k) for k in COUNTERS]
            ).where(*conditions(DailyRollup, f))
        )
        .mappings()
        .one()
    )
    return dict(row)


def overview(db: Session, f: Filters, now: datetime) -> dict:
    t = totals(db, f)
    channels = dict(
        db.execute(
            select(DailyRollup.channel, func.sum(DailyRollup.sessions))
            .where(*conditions(DailyRollup, f))
            .group_by(DailyRollup.channel)
        ).all()
    )
    hour_keys = [
        HourlyRollup.bucket_start,
        HourlyRollup.timezone,
        HourlyRollup.utc_offset_minutes,
        HourlyRollup.channel,
        HourlyRollup.local_date,
    ]
    hours = list(
        db.execute(
            select(
                *hour_keys,
                func.sum(HourlyRollup.sessions).label("sessions"),
                func.sum(HourlyRollup.ended_sessions).label("ended_sessions"),
                func.sum(HourlyRollup.booking_sessions).label("booking_sessions"),
            )
            .where(*conditions(HourlyRollup, f))
            .group_by(*hour_keys)
            .order_by(*hour_keys)
            .limit(2001)
        ).mappings()
    )
    days = (
        db.execute(
            select(
                DailyRollup.local_date,
                func.sum(DailyRollup.sessions).label("sessions"),
                func.sum(DailyRollup.ended_sessions).label("ended_sessions"),
                func.sum(DailyRollup.booking_sessions).label("booking_sessions"),
            )
            .where(*conditions(DailyRollup, f))
            .group_by(DailyRollup.local_date)
            .order_by(DailyRollup.local_date)
        )
        .mappings()
        .all()
    )
    return meta(db, f, now) | dict(
        sessions=t["sessions"],
        text_sessions=channels.get("text", 0),
        voice_sessions=channels.get("voice", 0),
        ended_sessions=t["ended_sessions"],
        booking_conversion=ratio(t["booking_sessions"], t["ended_sessions"]),
        handoff_rate=ratio(t["handoff_sessions"], t["ended_sessions"]),
        abandonment_rate=ratio(t["abandoned_sessions"], t["ended_sessions"]),
        book_operations=t["book_operations"],
        cancel_operations=t["cancel_operations"],
        reschedule_operations=t["reschedule_operations"],
        average_duration_ms=float(t["ended_duration_ms"] / t["ended_duration_samples"])
        if t["ended_duration_samples"]
        else None,
        duration_samples=t["ended_duration_samples"],
        usage=dict(
            invocations=t["usage_invocations"],
            usage_known=t["usage_known"],
            cost_known=t["cost_known"],
            input_tokens=t["input_tokens"] if t["usage_known"] else None,
            output_tokens=t["output_tokens"] if t["usage_known"] else None,
            estimated_usd=t["known_cost"] if t["cost_known"] else None,
        ),
        days=[dict(d) for d in days],
        hours=dict(
            items=[dict(h) for h in hours] if len(hours) <= 2000 else [],
            unavailable_reason="series_limit" if len(hours) > 2000 else None,
        ),
    )


def quality(db: Session, f: Filters, now: datetime) -> dict:
    t = totals(db, f)
    return meta(db, f, now) | dict(
        task_completion=ratio(t["completed_tasks"], t["attempted_tasks"]),
        tool_failure=ratio(t["tool_failures"], t["tool_results"]),
        repeated_question=ratio(t["repeated_prompts"], t["prompt_emissions"]),
        turns_per_task=ratio(t["caller_turns"], t["attempted_tasks"]),
    )


def raw_available(db: Session, f: Filters, now: datetime) -> bool:
    # Use historic cohort offsets, not the tenant's potentially changed timezone.
    offsets = list(
        db.scalars(
            select(CallRecord.utc_offset_minutes).where(*conditions(CallRecord, f)).distinct()
        )
    )
    # Sealed cohorts retain their original offsets after their Call rows expire.
    # Looking only at surviving western calls could conceal already-purged eastern
    # admissions on the same selected local date and falsely claim full coverage.
    offsets.extend(
        db.scalars(
            select(HourlyRollup.utc_offset_minutes).where(*conditions(HourlyRollup, f)).distinct()
        )
    )
    # Empty/old cohorts cannot prove a favorable offset: conservative +14h.
    maximum = max(offsets, default=840)
    earliest = datetime.combine(f.start, datetime.min.time(), UTC) - timedelta(minutes=maximum)
    return earliest >= now - timedelta(days=90)


def parameters(f: Filters) -> dict:
    return {
        "start": f.start,
        "end": f.end,
        "channel": f.channel,
        "agent": f.agent_id,
        "timezone": f.timezone,
    }


COHORT_SQL = """c.purpose='production' AND (:channel='all' OR c.channel=:channel) AND c.local_date BETWEEN :start AND :end
 AND (CAST(:agent AS uuid) IS NULL OR c.agent_id=:agent)
 AND (CAST(:timezone AS text) IS NULL OR c.timezone=:timezone)"""


def latency(db: Session, f: Filters, now: datetime) -> dict:
    result = meta(db, f, now, raw=True)
    reason = None if raw_available(db, f, now) else "raw_retention"
    params = parameters(f)
    eligible = (
        db.scalar(select(func.count()).select_from(CallRecord).where(*conditions(CallRecord, f)))
        or 0
    )
    rows: Sequence[RowMapping] = []
    voice_eligible = (
        db.scalar(
            select(func.count())
            .select_from(CallRecord)
            .where(*conditions(CallRecord, f), CallRecord.channel == "voice")
        )
        or 0
    )
    if reason is None:
        n = db.scalar(
            text(
                "SELECT count(*) FROM (SELECT 1 FROM analytics_stage_metrics m JOIN analytics_calls c USING(tenant_id,session_id) WHERE "
                + COHORT_SQL
                + " LIMIT 100001) s"
            ),
            params,
        )
        if n > 100000:
            reason = "sample_limit"
        else:
            rows = (
                db.execute(
                    text(
                        "SELECT m.stage,count(*) AS samples,count(DISTINCT m.session_id) AS sampled_sessions,percentile_cont(0.5) WITHIN GROUP(ORDER BY duration_ms) AS p50_ms,percentile_cont(0.95) WITHIN GROUP(ORDER BY duration_ms) AS p95_ms FROM analytics_stage_metrics m JOIN analytics_calls c USING(tenant_id,session_id) WHERE "
                        + COHORT_SQL
                        + " GROUP BY m.stage"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
    values = {r["stage"]: dict(r) for r in rows}
    stages = []
    for stage in get_args(Stage):
        voice_stage = stage in {
            "stt_completion",
            "tts_first_byte",
            "end_of_speech_to_audio",
            "tts_buffered_generation",
            "audio_queue",
            "end_of_speech_to_audio_queue",
        }
        stages.append(
            dict(
                stage=stage,
                diagnostic=stage
                in {"tts_buffered_generation", "audio_queue", "end_of_speech_to_audio_queue"},
                samples=None if reason else 0,
                sampled_sessions=None if reason else 0,
                eligible_sessions=voice_eligible if voice_stage else eligible,
                p50_ms=None,
                p95_ms=None,
            )
            | values.get(stage, {})
        )
    return result | dict(stages=stages, unavailable_reason=reason)


def intents(db: Session, f: Filters, now: datetime) -> dict:
    available = raw_available(db, f, now)
    items = []
    if available:
        n = (
            db.scalar(
                select(func.count()).select_from(CallRecord).where(*conditions(CallRecord, f))
            )
            or 0
        )
        sql = (
            """WITH cohort AS (SELECT c.session_id FROM analytics_calls c WHERE """
            + COHORT_SQL
            + """)
          SELECT intent,count(DISTINCT session_id) AS n FROM (
           SELECT t.kind AS intent,t.session_id FROM analytics_tasks t JOIN cohort USING(session_id)
           UNION ALL SELECT 'information',e.session_id FROM analytics_turns e JOIN cohort USING(session_id)
             WHERE e.kind='tool.result' AND e.tool_status='success' AND e.tool_name IN ('get_clinic_info','search_knowledge')
           UNION ALL SELECT 'handoff',e.session_id FROM analytics_turns e JOIN cohort USING(session_id)
             WHERE e.kind='handoff.requested') x GROUP BY intent"""
        )
        counts = {r.intent: r.n for r in db.execute(text(sql), parameters(f))}
        items = [
            dict(intent=k, sessions=counts.get(k, 0), share=ratio(counts.get(k, 0), n))
            for k in ["book", "cancel", "reschedule", "information", "handoff"]
        ]
    return meta(db, f, now, raw=True) | dict(
        items=items, unavailable_reason=None if available else "raw_retention"
    )


def encode_cursor(values: list) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps([str(v) for v in values]).encode()).decode().rstrip("=")
    )


def decode_cursor(value: str | None) -> list[str] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (
            not isinstance(parsed, list)
            or len(parsed) != 2
            or not all(isinstance(v, str) for v in parsed)
        ):
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise AppError("invalid_cursor", 422) from None


def page(rows, total, limit, cursor):
    has_more = len(rows) > limit
    items = rows[:limit]
    return dict(
        items=items,
        total=total,
        has_more=has_more,
        next_cursor=cursor(items[-1]) if has_more else None,
    )


def call_view(call: CallRecord, now: datetime) -> dict:
    keys = [
        "session_id",
        "agent_id",
        "channel",
        "language",
        "timezone",
        "local_date",
        "utc_offset_minutes",
        "started_at",
        "ended_at",
        "expires_at",
        "status",
        "outcome",
        "end_reason",
        "quality_flags",
    ]
    return {k: getattr(call, k) for k in keys} | {
        "pending_expiry": call.ended_at is None and call.expires_at <= now
    }


def calls(db: Session, f: CallFilters, now: datetime, limit: int, after: str | None):
    query = select(CallRecord).where(
        *conditions(CallRecord, f), CallRecord.started_at > now - timedelta(days=90)
    )
    if f.outcome is not None:
        query = query.where(CallRecord.outcome == f.outcome)
    if f.review_flag is not None:
        query = query.where(CallRecord.quality_flags.contains([f.review_flag]))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    cursor = decode_cursor(after)
    if cursor:
        try:
            stamp = datetime.fromisoformat(cursor[0])
            sid = UUID(cursor[1])
        except ValueError:
            raise AppError("invalid_cursor", 422) from None
        if stamp.tzinfo is None:
            raise AppError("invalid_cursor", 422)
        query = query.where(
            tuple_(CallRecord.started_at, CallRecord.session_id) < tuple_(stamp, sid)
        )
    rows = [
        call_view(c, now)
        for c in db.scalars(
            query.order_by(CallRecord.started_at.desc(), CallRecord.session_id.desc()).limit(
                limit + 1
            )
        )
    ]
    return page(rows, total, limit, lambda r: encode_cursor([r["started_at"], r["session_id"]])) | {
        "snapshot_at": now
    }


def detail(
    db: Session,
    sid: UUID,
    now: datetime,
    limit: int,
    after: int,
    measurement_after: str | None = None,
):
    call = db.scalar(
        select(CallRecord).where(
            CallRecord.session_id == sid,
            CallRecord.purpose == "production",
            CallRecord.started_at > now - timedelta(days=90),
        )
    )
    if call is None:
        raise AppError("not_found", 404)
    from analytics.transcripts import privacy_state

    settings = db.get(AnalyticsSettings, call.tenant_id)
    usage_known = (UsageInvocation.input_tokens.is_not(None)) & (
        UsageInvocation.output_tokens.is_not(None)
    )
    usage = (
        db.execute(
            select(
                func.count().label("invocations"),
                func.count().filter(usage_known).label("usage_known"),
                func.count(UsageInvocation.cost).label("cost_known"),
                func.sum(UsageInvocation.input_tokens).filter(usage_known).label("input_tokens"),
                func.sum(UsageInvocation.output_tokens).filter(usage_known).label("output_tokens"),
                func.sum(UsageInvocation.cost).label("estimated_usd"),
            ).where(UsageInvocation.session_id == sid)
        )
        .mappings()
        .one()
    )
    measurements = (
        select(
            StageMetric.id,
            StageMetric.stage,
            StageMetric.language,
            StageMetric.origin,
            StageMetric.duration_ms,
            StageMetric.observed_at,
            TurnEvent.ordinal.label("turn_ordinal"),
        )
        .outerjoin(
            TurnEvent,
            (TurnEvent.session_id == StageMetric.session_id)
            & (TurnEvent.event_id == StageMetric.turn_event_id)
            & (TurnEvent.tenant_id == StageMetric.tenant_id),
        )
        .where(StageMetric.session_id == sid)
    )
    measurement_total = db.scalar(select(func.count()).select_from(measurements.subquery())) or 0
    cursor = decode_cursor(measurement_after)
    if cursor:
        try:
            stamp, ident = datetime.fromisoformat(cursor[0]), UUID(cursor[1])
        except ValueError:
            raise AppError("invalid_cursor", 422) from None
        if stamp.tzinfo is None:
            raise AppError("invalid_cursor", 422)
        measurements = measurements.where(
            tuple_(StageMetric.observed_at, StageMetric.id) > tuple_(stamp, ident)
        )
    samples = [
        dict(r)
        for r in db.execute(
            measurements.order_by(StageMetric.observed_at, StageMetric.id).limit(limit + 1)
        ).mappings()
    ]
    counts = {
        r.operation: r.n
        for r in db.execute(
            select(OperationEvent.operation, func.count().label("n"))
            .where(OperationEvent.session_id == sid)
            .group_by(OperationEvent.operation)
        )
    }
    tasks = db.execute(
        select(
            func.count().label("n"),
            func.count().filter(TaskRecord.status == "completed").label("completed"),
        ).where(TaskRecord.session_id == sid)
    ).one()
    # Enumerated safe fields only: no text table is selected for staff detail.
    query = select(
        TurnEvent.ordinal,
        TurnEvent.kind,
        TurnEvent.occurred_at,
        TurnEvent.tool_name,
        TurnEvent.tool_status,
        TurnEvent.language,
    ).where(TurnEvent.session_id == sid)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    events = [
        dict(r)
        for r in db.execute(
            query.where(TurnEvent.ordinal > after).order_by(TurnEvent.ordinal).limit(limit + 1)
        ).mappings()
    ]
    return dict(
        call=call_view(call, now),
        trace_id=call.trace_id,
        privacy=privacy_state(call, settings),
        usage=dict(usage),
        measurements=page(
            samples, measurement_total, limit, lambda r: encode_cursor([r["observed_at"], r["id"]])
        ),
        summary=f"{counts.get('book', 0)} bookings, {counts.get('cancel', 0)} cancellations, {counts.get('reschedule', 0)} reschedules; {tasks.completed}/{tasks.n} scheduling tasks completed.",
        operations={k: counts.get(k, 0) for k in ["book", "cancel", "reschedule"]},
        attempted_tasks=tasks.n,
        completed_tasks=tasks.completed,
        events=page(events, total, limit, lambda r: r["ordinal"]),
    )


def alerts(db: Session, f: Filters, now: datetime, limit: int, after: str | None):
    query = select(AnalyticsAlert).where(
        AnalyticsAlert.last_seen_at >= datetime.combine(f.start, datetime.min.time(), UTC),
        AnalyticsAlert.last_seen_at
        < datetime.combine(f.end + timedelta(days=1), datetime.min.time(), UTC),
    )
    if f.channel != "all":
        query = query.where(
            (AnalyticsAlert.channel == f.channel) | (AnalyticsAlert.kind == "backlog")
        )
    if f.agent_id:
        query = query.where(AnalyticsAlert.agent_id == f.agent_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    cursor = decode_cursor(after)
    if cursor:
        try:
            stamp = datetime.fromisoformat(cursor[0])
            ident = UUID(cursor[1])
        except ValueError:
            raise AppError("invalid_cursor", 422) from None
        if stamp.tzinfo is None:
            raise AppError("invalid_cursor", 422)
        query = query.where(
            tuple_(AnalyticsAlert.last_seen_at, AnalyticsAlert.id) < tuple_(stamp, ident)
        )
    rows = list(
        db.scalars(
            query.order_by(AnalyticsAlert.last_seen_at.desc(), AnalyticsAlert.id.desc()).limit(
                limit + 1
            )
        )
    )
    return page(rows, total, limit, lambda r: encode_cursor([r.last_seen_at, r.id])) | {
        "snapshot_at": now,
        "freshness": meta(db, f, now)["freshness"],
    }


def knowledge_gaps(db: Session, f: Filters, now: datetime, limit: int, after: str | None):
    result = meta(db, f, now, raw=True)
    params = parameters(f) | {
        "observed_start": datetime.combine(f.start, datetime.min.time(), UTC),
        "observed_end": datetime.combine(f.end + timedelta(days=1), datetime.min.time(), UTC),
    }
    raw = raw_available(db, f, now)
    result.update(
        unavailable_reason=None if raw else "raw_retention",
        unavailable=0,
        irrelevant=0,
        unattributed_observations=0,
    )
    if not raw:
        return result | page([], 0, limit, lambda r: None)
    # Legacy and preview counts remain separate, observation-date based, without
    # inventing session/channel attribution or using incompatible command UUIDs.
    result["unattributed_observations"] = db.scalar(
        text("""SELECT count(*) FROM knowledge_query_events q
      LEFT JOIN analytics_calls c USING(tenant_id,session_id)
      WHERE q.purpose IN ('runtime','preview') AND c.session_id IS NULL
      AND q.created_at>=:observed_start AND q.created_at<:observed_end
      AND (CAST(:agent AS uuid) IS NULL OR q.agent_id=:agent)"""),
        params,
    )
    source = (
        " FROM knowledge_query_events q JOIN analytics_calls c USING(tenant_id,session_id) WHERE q.purpose='runtime' AND "
        + COHORT_SQL
    )
    grouped = (
        "SELECT q.query_hash AS fingerprint,q.decision,count(*) AS occurrences"
        + source
        + " GROUP BY q.query_hash,q.decision"
    )
    total = db.scalar(text("SELECT count(*) FROM (" + grouped + ") q"), params)
    for decision, n in db.execute(
        text("SELECT q.decision,count(*) AS n" + source + " GROUP BY q.decision"), params
    ):
        if decision == "below_threshold":
            result["irrelevant"] += n
        elif decision in {"service_unavailable", "no_sources", "processing_pending", "stale"}:
            result["unavailable"] += n
    cursor = decode_cursor(after)
    extra = ""
    if cursor:
        extra = " WHERE (fingerprint,decision)>(:fingerprint,:decision)"
        params.update(fingerprint=cursor[0], decision=cursor[1])
    params["limit"] = limit + 1
    rows = [
        dict(r)
        for r in db.execute(
            text(
                "SELECT * FROM ("
                + grouped
                + ") q"
                + extra
                + " ORDER BY fingerprint,decision LIMIT :limit"
            ),
            params,
        ).mappings()
    ]
    return result | page(
        rows, total, limit, lambda r: encode_cursor([r["fingerprint"], r["decision"]])
    )
