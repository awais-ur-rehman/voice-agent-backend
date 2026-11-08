"""Request-local observations. Billing invocation identity outlives domain rollback."""

import sys
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import wraps
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from analytics.contracts import (
    Confidence,
    DurationMs,
    InternalContract,
    Language,
    StageObservation,
    UsageObservation,
)
from analytics.lifecycle import dirty
from analytics.models import CallRecord, StageMetric, TurnEvent, UsageInvocation
from scheduler.db import tenant_context
from scheduler.errors import AppError
from scheduler.models import CommandReceipt

current_sink: ContextVar["MeasurementSink | None"] = ContextVar("measurement_sink", default=None)


class MeasurementSink:
    def __init__(self, *, writer: Callable[[UsageObservation], None] | None = None):
        self.writer = writer
        self.requests = 0
        self.stages: list[StageObservation] = []
        self.trace_id: UUID | None = None
        self.command_span: AbstractContextManager | None = None
        self.lock = threading.Lock()
        self.closed = False

    def emit(self, value):
        try:
            if self.writer:
                self.writer(value)
        except Exception:
            # Observation availability never changes a clinical command; no exception/body logging.
            pass

    def start(self, model, input_rate=None, output_rate=None):
        with self.lock:
            self.requests += 1
        try:
            value = UsageObservation(
                invocation_id=uuid4(),
                model=model,
                status="started",
                started_at=datetime.now(UTC),
                input_rate=input_rate,
                output_rate=output_rate,
            )
        except ValidationError:
            return None
        self.emit(value)
        return value, time.monotonic()

    def finish(self, value, started, status, usage, *, completed_monotonic=None, completed_at=None):
        fields = {}
        if isinstance(usage, dict):
            for source, target in [
                ("prompt_tokens", "input_tokens"),
                ("completion_tokens", "output_tokens"),
            ]:
                count = usage.get(source)
                if type(count) is int and 0 <= count <= 1000000000000:
                    fields[target] = count
        duration = (
            (completed_monotonic if completed_monotonic is not None else time.monotonic()) - started
        ) * 1000
        completed = completed_at if completed_at is not None else datetime.now(UTC)
        if all(
            v is not None
            for v in (
                fields.get("input_tokens"),
                fields.get("output_tokens"),
                value.input_rate,
                value.output_rate,
            )
        ):
            cost = (
                Decimal(fields["input_tokens"]) * value.input_rate
                + Decimal(fields["output_tokens"]) * value.output_rate
            ) / Decimal(1000000)
            if cost <= Decimal(1000000000000):
                fields["cost"] = cost
        self.emit(
            value.model_copy(
                update=fields | {"status": status, "completed_at": max(completed, value.started_at)}
            )
        )
        with self.lock:
            if (
                not self.closed
                and status in {"success", "malformed"}
                and 0 <= duration <= 600000
                and len(self.stages) < 32
            ):
                self.stages.append(
                    StageObservation(
                        event_id=uuid4(),
                        stage="llm_completion",
                        language="unknown",
                        origin="server",
                        duration_ms=duration,
                        observed_at=completed,
                    )
                )


@contextmanager
def measurement_scope(sink):
    token = current_sink.set(sink)
    try:
        yield sink
    finally:
        with sink.lock:
            sink.closed = True
        if sink.command_span is not None:
            sink.command_span.__exit__(*sys.exc_info())
        current_sink.reset(token)


def measured_request(function):
    @wraps(function)
    def run(*args, **kwargs):
        with measurement_scope(MeasurementSink()):
            return function(*args, **kwargs)

    return run


def database_writer(factory, tenant_id, session_id):
    """Independent short ledger transactions; frozen server-authenticated ownership only."""

    def write(value):
        with factory() as db, db.begin():
            tenant_context(db, tenant_id)
            db.execute(text("SET LOCAL statement_timeout='5000ms'"))
            db.execute(text("SET LOCAL lock_timeout='1000ms'"))
            call = db.scalar(
                select(CallRecord).where(CallRecord.session_id == session_id).with_for_update()
            )
            if call is None or call.tenant_id != tenant_id:
                return
            fields = value.model_dump() | {"session_id": session_id, "tenant_id": tenant_id}
            stmt = insert(UsageInvocation).values(id=uuid4(), **fields)
            # Only this exact invocation may complete; no domain mutation or success inference.
            changed = db.scalar(
                stmt.on_conflict_do_update(
                    index_elements=["invocation_id"],
                    set_={
                        k: v
                        for k, v in fields.items()
                        if k
                        not in {
                            "session_id",
                            "tenant_id",
                            "invocation_id",
                            "model",
                            "provider",
                            "started_at",
                        }
                    },
                    where=(UsageInvocation.session_id == session_id)
                    & (UsageInvocation.tenant_id == tenant_id)
                    & (UsageInvocation.status == "started")
                    & (value.status != "started"),
                ).returning(UsageInvocation.id)
            )
            if changed is not None:
                from analytics.prometheus import committed_observation

                committed_observation(
                    db, call, stage="llm_completion", language=call.language, status=value.status
                )
            dirty(db, call, value.completed_at or value.started_at)

    return write


# The pinned runtime cannot prove publisher/frame association. These are the only
# current native timings; accepting arbitrary canonical keys would fabricate coverage.
class NativeMetadata(InternalContract):
    stt_confidence: Confidence | None = Field(default=None, strict=True)
    uncertain: bool | None = Field(default=None, strict=True)
    cache_hit: bool | None = Field(default=None, strict=True)
    output_state: Literal["generated", "queued", "interrupted"] | None = None


class NativeObservation(InternalContract):
    event_id: UUID
    command_id: UUID | None = None
    stage: Literal[
        "stt_completion", "tts_buffered_generation", "audio_queue", "end_of_speech_to_audio_queue"
    ]
    language: Language
    duration_ms: DurationMs = Field(strict=True)
    observed_at: AwareDatetime
    output_kind: Literal["input", "response", "greeting", "filler", "resumed", "control"]
    metadata: NativeMetadata = Field(default_factory=NativeMetadata)

    @model_validator(mode="after")
    def provenance(self):
        if (self.stage == "stt_completion") != (self.output_kind == "input"):
            raise ValueError("stage_provenance")
        if self.stage == "end_of_speech_to_audio_queue" and self.output_kind != "response":
            raise ValueError("stage_provenance")
        if self.output_kind == "response" and self.command_id is None:
            raise ValueError("command_required")
        if self.output_kind == "input" and (
            self.metadata.cache_hit is not None
            or self.metadata.output_state is not None
            or self.command_id is not None
        ):
            raise ValueError("stage_provenance")
        if self.stage == "tts_buffered_generation" and (
            self.metadata.cache_hit is not False
            or self.metadata.output_state not in {"generated", "interrupted"}
        ):
            raise ValueError("stage_provenance")
        if self.stage in {
            "audio_queue",
            "end_of_speech_to_audio_queue",
        } and self.metadata.output_state not in {"queued", "interrupted"}:
            raise ValueError("stage_provenance")
        if self.output_kind != "input" and (
            self.metadata.stt_confidence is not None or self.metadata.uncertain is not None
        ):
            raise ValueError("stage_provenance")
        return self


class NativeBatch(InternalContract):
    generation: int = Field(strict=True, ge=1)
    revision: int = Field(strict=True, ge=0)
    observations: list[NativeObservation] = Field(min_length=1, max_length=32)


def record_native(db, row, batch, now):
    if row.transport_mode != "voice" or batch.revision != row.revision:
        raise AppError("conflict", 409)
    call = db.scalar(select(CallRecord).where(CallRecord.session_id == row.id).with_for_update())
    if (
        call is None
        or call.channel != "voice"
        or call.ended_at is not None
        or now >= call.expires_at
    ):
        raise AppError("unauthorized", 401)
    for value in batch.observations:
        if (
            not max(call.started_at, now - timedelta(minutes=10))
            <= value.observed_at
            <= min(call.expires_at, now + timedelta(seconds=5))
        ):
            raise AppError("validation_failed", 422)
        turn_id = None
        if value.command_id is not None:
            receipt = db.scalar(
                select(CommandReceipt).where(
                    CommandReceipt.session_id == row.id,
                    CommandReceipt.command_id == str(value.command_id),
                    CommandReceipt.revision == batch.revision,
                )
            )
            if receipt is None:
                raise AppError("conflict", 409)
            turn_id = db.scalar(
                select(TurnEvent.event_id).where(
                    TurnEvent.session_id == row.id,
                    TurnEvent.command_id == UUID(hex=receipt.request_digest[:32]),
                    TurnEvent.kind == "turn.assistant",
                )
            )
            if turn_id is None:
                raise AppError("conflict", 409)
        metadata = value.metadata.model_dump(exclude_none=True) | {
            "trusted": True,
            "accepted": False,
            "filler": value.output_kind == "filler",
        }
        if value.metadata.cache_hit is not None:
            metadata["cached"] = value.metadata.cache_hit
        # Safe code-owned categories retain classification without prose or a caller turn.
        if value.output_kind == "greeting":
            metadata["prompt_id"] = "greeting"
        if value.output_kind in {"filler", "resumed", "control"}:
            metadata["prompt_id"] = "other"
        fields = {
            "session_id": row.id,
            "tenant_id": row.tenant_id,
            "event_id": value.event_id,
            "turn_event_id": turn_id,
            "stage": value.stage,
            "language": value.language,
            "origin": "native",
            "duration_ms": value.duration_ms,
            "observed_at": value.observed_at,
        }
        previous = db.scalar(
            select(StageMetric).where(
                StageMetric.session_id == row.id, StageMetric.event_id == value.event_id
            )
        )
        previous_event = db.scalar(
            select(TurnEvent).where(
                TurnEvent.session_id == row.id, TurnEvent.event_id == value.event_id
            )
        )
        if previous is not None or previous_event is not None:
            if (
                previous is None
                or previous_event is None
                or any(getattr(previous, key) != item for key, item in fields.items())
                or previous_event.safe_metadata != metadata
            ):
                raise AppError("idempotency_conflict", 409)
            continue
        db.add(StageMetric(**fields))
        db.add(
            TurnEvent(
                session_id=row.id,
                tenant_id=row.tenant_id,
                event_id=value.event_id,
                kind="metric.turn",
                language=value.language,
                safe_metadata=metadata,
                occurred_at=value.observed_at,
            )
        )
        if value.metadata.stt_confidence is not None and value.metadata.stt_confidence < 0.5:
            call.quality_flags = sorted(set(call.quality_flags) | {"low_stt_confidence"})
        db.flush()
        from analytics.prometheus import committed_observation

        committed_observation(
            db, call, stage=value.stage, language=value.language, duration_ms=value.duration_ms
        )
    dirty(db, call, now)
