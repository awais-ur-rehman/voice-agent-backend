"""Observed 15-minute alert state; no notifications or free-text labels."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from analytics.contracts import Language
from analytics.models import AnalyticsAlert


def observe(
    db: Session,
    tenant: UUID,
    agent: UUID,
    channel: str | None,
    kind: str,
    now: datetime,
    *,
    firing: bool,
    numerator: int = 0,
    denominator: int = 0,
    value: float | None = None,
    language: Language = "unknown",
):
    alert = db.scalar(
        select(AnalyticsAlert).where(
            AnalyticsAlert.agent_id == agent,
            AnalyticsAlert.channel == channel,
            AnalyticsAlert.kind == kind,
            AnalyticsAlert.state == "open",
        )
    )
    if firing:
        if alert is None:
            alert = AnalyticsAlert(
                tenant_id=tenant,
                agent_id=agent,
                channel=channel,
                kind=kind,
                state="open",
                opened_at=now,
            )
            db.add(alert)
        alert.window_start = now - timedelta(minutes=15)
        alert.window_end = now
        alert.last_seen_at = now
        alert.numerator = numerator
        alert.denominator = denominator
        alert.observed_value = value
        alert.language = language
    elif alert is not None:
        alert.state = "resolved"
        alert.resolved_at = now
        alert.last_seen_at = now


def evaluate(
    db: Session, tenant: UUID, now: datetime, limit: int, after: UUID | None = None
) -> tuple[UUID | None, bool]:
    backlog = {
        r.agent_id: r
        for r in db.execute(
            text("SELECT * FROM analytics_agent_backlog(:now,:after,:limit)"),
            {"now": now, "after": after, "limit": limit},
        )
    }
    agents = set(
        db.scalars(
            text("""SELECT agent_id FROM (
      SELECT agent_id FROM analytics_calls WHERE purpose='production'
      UNION SELECT agent_id FROM analytics_alerts WHERE state='open') a
      WHERE CAST(:after AS uuid) IS NULL OR agent_id>:after ORDER BY agent_id LIMIT :limit"""),
            {"after": after, "limit": limit},
        )
    )
    agents.update(backlog)
    selected = sorted(agents)[:limit]
    complete = after is None and len(selected) < limit
    for agent in selected:
        queue = backlog.get(agent)
        observe(
            db,
            tenant,
            agent,
            None,
            "backlog",
            now,
            firing=bool(queue and queue.oldest_age_seconds > 900),
            numerator=queue.queued_count if queue else 0,
            denominator=queue.queued_count if queue else 0,
            value=queue.oldest_age_seconds if queue else None,
        )
        for channel in ["text", "voice"]:
            params = {
                "agent": agent,
                "channel": channel,
                "start": now - timedelta(minutes=15),
                "end": now,
            }
            # Cap raw samples before ordered-set aggregate; never use a truncated p95.
            samples = db.scalar(
                text("""SELECT count(*) FROM (SELECT 1 FROM analytics_stage_metrics m
              JOIN analytics_calls c USING(tenant_id,session_id) WHERE c.agent_id=:agent AND c.channel=:channel
              AND c.purpose='production' AND m.stage='end_of_speech_to_audio' AND m.language IN ('en','ur')
              AND m.observed_at>:start AND m.observed_at<=:end LIMIT 100001) s"""),
                params,
            )
            if samples <= 100000:
                latencies = (
                    db.execute(
                        text("""SELECT m.language,count(*) AS n,percentile_cont(0.95) WITHIN GROUP(ORDER BY duration_ms) AS p95
                  FROM analytics_stage_metrics m JOIN analytics_calls c USING(tenant_id,session_id)
                  WHERE c.agent_id=:agent AND c.channel=:channel AND c.purpose='production'
                  AND m.stage='end_of_speech_to_audio' AND m.language IN ('en','ur') AND m.observed_at>:start AND m.observed_at<=:end
                  GROUP BY m.language HAVING count(*)>=5"""),
                        params,
                    ).all()
                    if channel == "voice"
                    else []
                )
                breached = [r for r in latencies if r.p95 > ({"en": 1500, "ur": 2000}[r.language])]
                worst = max(
                    breached,
                    key=lambda r: r.p95 / ({"en": 1500, "ur": 2000}[r.language]),
                    default=None,
                )
                observe(
                    db,
                    tenant,
                    agent,
                    channel,
                    "latency",
                    now,
                    firing=worst is not None,
                    numerator=worst.n if worst else 0,
                    denominator=worst.n if worst else 0,
                    value=worst.p95 if worst else None,
                    language=worst.language if worst else "unknown",
                )
            if samples > 100000:
                complete = False
            tool = db.execute(
                text("""SELECT count(*) AS n,count(*) FILTER(WHERE tool_status='failure') AS failures
              FROM analytics_turns e JOIN analytics_calls c USING(tenant_id,session_id) WHERE c.agent_id=:agent AND c.channel=:channel
              AND c.purpose='production' AND e.kind='tool.result' AND e.occurred_at>:start AND e.occurred_at<=:end"""),
                params,
            ).one()
            observe(
                db,
                tenant,
                agent,
                channel,
                "tool_failure",
                now,
                firing=tool.n >= 5 and tool.failures / tool.n > 0.1,
                numerator=tool.failures,
                denominator=tool.n,
                value=tool.failures / tool.n if tool.n else None,
            )
            provider = db.execute(
                text("""SELECT count(*) AS n,count(*) FILTER(WHERE u.status IN ('malformed','timeout','failure')) AS failures
              FROM analytics_usage_invocations u JOIN analytics_calls c USING(tenant_id,session_id)
              WHERE c.agent_id=:agent AND c.channel=:channel AND c.purpose='production' AND u.completed_at>:start AND u.completed_at<=:end"""),
                params,
            ).one()
            observe(
                db,
                tenant,
                agent,
                channel,
                "provider_failure",
                now,
                firing=provider.failures > 0,
                numerator=provider.failures,
                denominator=provider.n,
                value=float(provider.failures),
            )
            ended = db.execute(
                text("""SELECT count(*) AS n,count(*) FILTER(WHERE outcome='error') AS failures FROM analytics_calls
              WHERE agent_id=:agent AND channel=:channel AND purpose='production' AND ended_at>:start AND ended_at<=:end"""),
                params,
            ).one()
            observe(
                db,
                tenant,
                agent,
                channel,
                "ended_error",
                now,
                firing=ended.failures > 0,
                numerator=ended.failures,
                denominator=ended.n,
                value=float(ended.failures),
            )
    db.flush()
    return (selected[-1] if len(selected) == limit else None), complete
