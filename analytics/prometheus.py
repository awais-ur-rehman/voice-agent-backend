"""Operator-private process-local API projection timing; resets on process restart.

This registry does not claim global call/voice latency or cross-tenant usage.
Only fixed endpoint/result labels exist. No telemetry dependency or export socket.
"""

import hmac
import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Request, Response

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
