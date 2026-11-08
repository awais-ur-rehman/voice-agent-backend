"""Bounded internal observations; ephemeral text fields are excluded from serialization."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Language = Literal["en", "ur", "mixed", "unknown"]
Channel = Literal["text", "voice"]
Purpose = Literal["production", "test"]
TaskKind = Literal["book", "cancel", "reschedule"]
TaskStatus = Literal["active", "parked", "completed", "abandoned"]
CallStatus = Literal["active", "disconnected", "ended", "expired", "failed"]
Outcome = Literal[
    "booked", "cancelled", "rescheduled", "info_only", "abandoned", "handoff", "error"
]
EndReason = Literal[
    "caller_end", "staff_end", "handoff", "timeout", "error", "provisioning_failed", "disconnect"
]
Consent = Literal["off", "pending", "accepted", "declined"]
EventKind = Literal[
    "session.started",
    "turn.user",
    "turn.assistant",
    "tool.called",
    "tool.result",
    "metric.turn",
    "lang.switched",
    "handoff.requested",
    "session.ended",
]
Stage = Literal[
    "command_total",
    "tool",
    "llm_completion",
    "llm_first_token",
    "stt_completion",
    "tts_first_byte",
    "end_of_speech_to_audio",
    "tts_buffered_generation",
    "audio_queue",
    "end_of_speech_to_audio_queue",
]
Origin = Literal["server", "native"]
PromptId = Literal[
    "ask_date",
    "ask_need",
    "ask_patient",
    "ask_verification",
    "ask_slot",
    "ask_appointment",
    "readback",
    "consent",
    "consent_clarification",
    "info",
    "greeting",
    "handoff",
    "end",
    "other",
]
Delivery = Literal["generated", "delivered", "interrupted"]
OutputState = Literal["generated", "queued", "delivered", "interrupted"]
ErrorCode = Literal[
    "provider_timeout",
    "provider_unavailable",
    "provider_malformed",
    "tool_failed",
    "validation_failed",
    "conflict",
    "not_found",
    "unauthorized",
]
QualityFlag = Literal["repeated_prompt", "interruptions", "low_stt_confidence", "handoff"]
InvocationStatus = Literal["started", "success", "malformed", "timeout", "failure"]
AlertKind = Literal["latency", "tool_failure", "provider_failure", "ended_error", "backlog"]
ToolName = Literal[
    "update_dialogue_state",
    "find_patient",
    "set_caller_contact",
    "verify_patient",
    "prepare_patient",
    "create_patient",
    "get_clinic_info",
    "search_knowledge",
    "get_available_slots",
    "hold_slot",
    "find_appointments",
    "select_appointment",
    "read_back",
    "book_appointment",
    "cancel_appointment",
    "reschedule_appointment",
    "confirm",
    "ask_caller",
    "resume_task",
    "request_handoff",
    "end_call",
]
DurationMs = Annotated[float, Field(ge=0, le=600000, allow_inf_nan=False)]
Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
TokenCount = Annotated[int, Field(ge=0, le=1000000000000)]
PriceRate = Annotated[Decimal, Field(ge=0, le=1000000, allow_inf_nan=False)]
Cost = Annotated[Decimal, Field(ge=0, le=1000000000000, allow_inf_nan=False)]


class InternalContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SafeMetadata(InternalContract):
    prompt_id: PromptId | None = None
    delivery: Delivery | None = None
    output_state: OutputState | None = None
    error_code: ErrorCode | None = None
    progressed: bool | None = None
    repeated_prompt: bool | None = None
    accepted: bool | None = None
    trusted: bool | None = None
    cached: bool | None = None
    cache_hit: bool | None = None
    uncertain: bool | None = None
    filler: bool | None = None
    stt_confidence: Confidence | None = None


class StageObservation(InternalContract):
    event_id: UUID
    stage: Stage
    language: Language
    origin: Origin
    duration_ms: DurationMs
    observed_at: AwareDatetime | None = None
    turn_event_id: UUID | None = None


class TurnObservation(InternalContract):
    """Owned by one command; unknown stage timing is represented by no sample."""

    completed_task_ids: list[UUID] = Field(
        default_factory=list, max_length=8, exclude=True, repr=False
    )
    started_monotonic: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, exclude=True, repr=False
    )
    input_text: str = Field(default="", max_length=8000, exclude=True, repr=False)
    identity_literals: tuple[str, ...] = Field(default=(), exclude=True, repr=False)
    suppress_text: bool = Field(default=False, exclude=True)
    control: Literal["delivery_interrupted", "upstream_timeout", "silence"] | None = None
    stages: list[StageObservation] = Field(default_factory=list, max_length=32)
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)
    language: Language = "unknown"


class UsageObservation(InternalContract):
    """One actual HTTP invocation, independently persisted after provider completion.

    Rates are configured currency units per million reported tokens; no estimates.
    A missing input is unknown, never an implicit zero.
    """

    invocation_id: UUID
    provider: Literal["nim"] = "nim"
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_./:-]+$")
    status: InvocationStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    input_rate: PriceRate | None = None
    output_rate: PriceRate | None = None
    cost: Cost | None = None

    @model_validator(mode="after")
    def observed_cost_requires_coverage(self) -> "UsageObservation":
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("Invocation completion precedes start")
        if self.cost is not None and any(
            value is None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.input_rate,
                self.output_rate,
            )
        ):
            raise ValueError("Known cost requires actual usage and configured rates")
        return self


class SettingsUpdate(InternalContract):
    revision: int = Field(ge=1)
    capture_enabled: bool
    retention_days: int = Field(ge=1, le=30)


class BacklogObservation(InternalContract):
    queued_count: int = Field(ge=0)
    oldest_age_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None


class Freshness(InternalContract):
    as_of: datetime | None
    stale: bool
