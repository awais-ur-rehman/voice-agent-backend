"""Manual API-only spans: no SDK/exporter/auto instrumentation or exception text."""

from contextlib import contextmanager
from secrets import randbits

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags

NAMES = {"runtime.command": "runtime", "provider.invoke": "nim", "native.metrics": "native"}


@contextmanager
def safe_span(name, trace_id, *, tracer=None):
    if name not in NAMES:
        raise ValueError("span_name")
    span = None
    try:
        parent = (
            trace.set_span_in_context(
                NonRecordingSpan(
                    SpanContext(
                        trace_id=trace_id.int,
                        span_id=randbits(64) or 1,
                        is_remote=False,
                        trace_flags=TraceFlags(0),
                    )
                )
            )
            if trace_id
            else None
        )
        span = (tracer or trace.get_tracer("clinic.analytics", "1")).start_span(
            name,
            context=parent,
            attributes={"component": NAMES[name]},
            record_exception=False,
            set_status_on_exception=False,
        )
    except Exception:
        pass
    try:
        yield
    except BaseException:
        if span:
            try:
                span.set_status(Status(StatusCode.ERROR))
            except Exception:
                pass
        raise
    finally:
        if span:
            try:
                span.end()
            except Exception:
                pass
