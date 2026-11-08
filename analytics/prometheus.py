"""Operator-private committed stage/status and projection metrics; process-local.

Fixed allowlist labels only. No global usage claim, exporter, or network socket.
"""

import hmac
import math
import threading
import time
from dataclasses import dataclass, field
from typing import get_args

from fastapi import APIRouter, Request, Response
from sqlalchemy import event

from analytics.contracts import Channel, Language, Stage
from scheduler.errors import AppError

PATHS = {
    "/dashboard/analytics/overview": "overview",
    "/dashboard/analytics/quality": "quality",
    "/dashboard/analytics/latency": "latency",
    "/dashboard/analytics/intents": "intents",
    "/dashboard/analytics/knowledge-gaps": "knowledge_gaps",
    "/dashboard/analytics/alerts": "alerts",
    "/dashboard/calls": "calls",
    "/dashboard/calls/{session_id}": "call_detail",
}
BUCKETS = (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)
router = APIRouter(include_in_schema=False)


@dataclass
class Histogram:
    total: float = 0.0
    count: int = 0
    buckets: list[int] = field(default_factory=lambda: [0] * len(BUCKETS))


class Registry:
    def __init__(self):
        self.started = time.time()
        self.lock = threading.Lock()
        self.values = {
            (name, result): Histogram() for name in PATHS.values() for result in ["ok", "error"]
        }
        self.stages: dict[tuple[str, str, str, str], Histogram] = {}
        self.statuses: dict[tuple[str, str, str, str], int] = {}

    def observe_stage(self, stage, language, channel, status, duration_ms=None):
        if (
            stage not in get_args(Stage)
            or language not in get_args(Language)
            or channel not in get_args(Channel)
            or status not in {"observed", "started", "success", "malformed", "timeout", "failure"}
        ):
            return
        if duration_ms is not None and (
            not math.isfinite(duration_ms) or not 0 <= duration_ms <= 600000
        ):
            return
        key = (stage, language, channel, status)
        with self.lock:
            self.statuses[key] = self.statuses.get(key, 0) + 1
            if duration_ms is not None:
                duration = duration_ms / 1000
                row = self.stages.setdefault(key, Histogram())
                row.total += duration
                row.count += 1
                for i, bound in enumerate(BUCKETS):
                    if duration <= bound:
                        row.buckets[i] += 1

    def observe(self, path: str, duration: float, status: int):
        if path not in PATHS:
            return
        with self.lock:
            row = self.values[(PATHS[path], "ok" if status < 400 else "error")]
            row.total += max(0, duration)
            row.count += 1
            for i, bound in enumerate(BUCKETS):
                if duration <= bound:
                    row.buckets[i] += 1

    def render(self) -> str:
        lines = [
            "# HELP analytics_process_start_time_seconds Process-local reset boundary.",
            "# TYPE analytics_process_start_time_seconds gauge",
            f"analytics_process_start_time_seconds {self.started}",
            "# HELP analytics_projection_seconds API projection duration; process-local, not voice latency.",
            "# TYPE analytics_projection_seconds histogram",
        ]
        with self.lock:
            lines.extend(
                [
                    "# HELP analytics_stage_events_total Committed stage observations and provider invocation transitions; process-local.",
                    "# TYPE analytics_stage_events_total counter",
                    "# HELP analytics_stage_seconds Committed measured durations; absent stages have no fabricated samples.",
                    "# TYPE analytics_stage_seconds histogram",
                ]
            )
            for key, n in sorted(self.statuses.items()):
                labels = ",".join(
                    f'{name}="{value}"'
                    for name, value in zip(["stage", "language", "channel", "status"], key)
                )
                lines.append(f"analytics_stage_events_total{{{labels}}} {n}")
                row = self.stages.get(key)
                if row is not None:
                    for bound, count in zip(BUCKETS, row.buckets):
                        lines.append(
                            f'analytics_stage_seconds_bucket{{{labels},le="{bound}"}} {count}'
                        )
                    lines.extend(
                        [
                            f'analytics_stage_seconds_bucket{{{labels},le="+Inf"}} {row.count}',
                            f"analytics_stage_seconds_sum{{{labels}}} {row.total}",
                            f"analytics_stage_seconds_count{{{labels}}} {row.count}",
                        ]
                    )
            for (endpoint, result), row in sorted(self.values.items()):
                labels = f'endpoint="{endpoint}",result="{result}"'
                for bound, n in zip(BUCKETS, row.buckets):
                    lines.append(
                        f'analytics_projection_seconds_bucket{{{labels},le="{bound}"}} {n}'
                    )
                lines.extend(
                    [
                        f'analytics_projection_seconds_bucket{{{labels},le="+Inf"}} {row.count}',
                        f"analytics_projection_seconds_sum{{{labels}}} {row.total}",
                        f"analytics_projection_seconds_count{{{labels}}} {row.count}",
                    ]
                )
        return "\n".join(lines) + "\n"


def committed_observation(db, call, *, stage, language, status="observed", duration_ms=None):
    """Per-transaction events, emitted only after outer commit. Never affect domain success.

    Savepoint commits retain events for the outer commit; savepoint rollbacks discard
    that savepoint and its descendants. SQL identities, not process counters, dedupe.
    """
    try:
        registry = db.info.get("analytics_metrics")
        if registry is None or call.purpose != "production":
            return
        if "analytics_pending" not in db.info:
            db.info["analytics_pending"] = []

            def commit(session):
                if session.in_nested_transaction():
                    return
                pending = session.info["analytics_pending"]
                session.info["analytics_pending"] = []
                for _, observation in pending:
                    try:
                        registry.observe_stage(*observation)
                    except Exception:
                        pass

            def rollback(session, previous):
                def retained(item):
                    transaction = item[0]
                    while transaction is not None:
                        if transaction is previous:
                            return False
                        transaction = transaction.parent
                    return True

                session.info["analytics_pending"] = list(
                    filter(retained, session.info["analytics_pending"])
                )

            event.listen(db, "after_commit", commit)
            event.listen(db, "after_soft_rollback", rollback)

            def ended(session, transaction):
                # Session.close() rolls back without after_soft_rollback. A reused
                # Session must never publish the abandoned transaction's events.
                if transaction.parent is None:
                    session.info["analytics_pending"] = []

            event.listen(db, "after_transaction_end", ended)
        transaction = db.get_nested_transaction() or db.get_transaction()
        db.info["analytics_pending"].append(
            (transaction, (stage, language, call.channel, status, duration_ms))
        )
    except Exception:
        pass


@router.get("/private/analytics/metrics")
def metrics(request: Request):
    secret = request.app.state.settings.analytics_metrics_secret
    provided = request.headers.get("Authorization", "")
    if secret is None or not hmac.compare_digest(provided, "Bearer " + secret.get_secret_value()):
        raise AppError("forbidden", 403)
    return Response(
        request.app.state.analytics_metrics.render(),
        media_type="text/plain; version=0.0.4",
        headers={"Cache-Control": "no-store"},
    )
