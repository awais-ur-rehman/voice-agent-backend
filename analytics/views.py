"""Versioned, bounded staff projection contracts. No hidden transcript properties."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from analytics.contracts import (
    AlertKind,
    CallStatus,
    Channel,
    EndReason,
    EventKind,
    Language,
    Outcome,
    QualityFlag,
    Stage,
    ToolName,
)


class View(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


ChannelFilter = Literal["all", "text", "voice"]


class Filters(View):
    start: date
    end: date
    channel: ChannelFilter = "all"
    agent_id: UUID | None = None
    timezone: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def bounded(self):
        if not 0 <= (self.end - self.start).days < 31:
            raise ValueError("range_must_be_1_to_31_inclusive_days")
        return self


class CallFilters(Filters):
    outcome: Outcome | None = None
    review_flag: QualityFlag | None = None


class FreshnessView(View):
    as_of: datetime | None
    stale: bool
    pending_buckets: int
    pending_expirations: int
    source: Literal["persisted_rollups", "raw_snapshot"] = "persisted_rollups"


class Ratio(View):
    numerator: int
    denominator: int
    value: float | None


class UsageView(View):
    invocations: int
    usage_known: int
    cost_known: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_usd: Decimal | None
    currency: Literal["USD"] = "USD"
    basis: Literal["reported_tokens_configured_usd_per_million"] = (
        "reported_tokens_configured_usd_per_million"
    )


class Cohort(View):
    filters: Filters
    timezones: list[str]
    snapshot_at: datetime
    freshness: FreshnessView
    purpose: Literal["production"] = "production"
    date_basis: Literal["stored_admission_local_date"] = "stored_admission_local_date"


class DayCount(View):
    local_date: date
    sessions: int
    ended_sessions: int
    booking_sessions: int


class HourCount(DayCount):
    bucket_start: datetime
    timezone: str
    utc_offset_minutes: int
    channel: Channel


class HourSeries(View):
    items: list[HourCount]
    unavailable_reason: Literal["series_limit"] | None
    limit: int = 2000


class Overview(Cohort):
    sessions: int
    text_sessions: int
    voice_sessions: int
    ended_sessions: int
    booking_conversion: Ratio
    handoff_rate: Ratio
    abandonment_rate: Ratio
    book_operations: int
    cancel_operations: int
    reschedule_operations: int
    average_duration_ms: float | None
    duration_samples: int
    usage: UsageView
    days: list[DayCount]
    hours: HourSeries


class Quality(Cohort):
    task_completion: Ratio
    tool_failure: Ratio
    repeated_question: Ratio
    turns_per_task: Ratio


class StageView(View):
    stage: Stage
    diagnostic: bool
    samples: int | None
    sampled_sessions: int | None
    eligible_sessions: int
    p50_ms: float | None
    p95_ms: float | None


class Latency(Cohort):
    stages: list[StageView]
    unavailable_reason: Literal["raw_retention", "sample_limit"] | None
    sample_limit: int = 100000


class IntentRow(View):
    intent: Literal["book", "cancel", "reschedule", "information", "handoff"]
    sessions: int
    share: Ratio


class Intents(Cohort):
    items: list[IntentRow]
    unavailable_reason: Literal["raw_retention"] | None
    unit: Literal["distinct_sessions_per_intent"] = "distinct_sessions_per_intent"
    overlapping: Literal[True] = True


class GapRow(View):
    fingerprint: str
    decision: Literal[
        "ready",
        "no_sources",
        "processing_pending",
        "below_threshold",
        "service_unavailable",
        "rejected",
        "stale",
    ]
    occurrences: int


class KnowledgeGaps(Cohort):
    items: list[GapRow]
    total: int
    has_more: bool
    next_cursor: str | None
    unavailable: int
    irrelevant: int
    unavailable_reason: Literal["raw_retention"] | None
    unattributed_observations: int
    unattributed_date_basis: Literal["observed_utc_date"] = "observed_utc_date"
    channel_attribution: Literal["server_session"] = "server_session"


class AlertView(View):
    id: UUID
    agent_id: UUID
    channel: Channel | None
    kind: AlertKind
    language: Language
    state: Literal["open", "resolved"]
    numerator: int
    denominator: int
    observed_value: float | None
    window_start: datetime
    window_end: datetime
    opened_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None


class AlertPage(View):
    freshness: FreshnessView
    date_basis: Literal["last_seen_utc_date"] = "last_seen_utc_date"
    items: list[AlertView]
    total: int
    has_more: bool
    next_cursor: str | None
    snapshot_at: datetime


class CallView(View):
    session_id: UUID
    agent_id: UUID
    channel: Channel
    language: Language
    timezone: str
    local_date: date
    utc_offset_minutes: int
    started_at: datetime
    ended_at: datetime | None
    expires_at: datetime
    status: CallStatus
    outcome: Outcome | None
    end_reason: EndReason | None
    quality_flags: list[
        Literal["repeated_prompt", "interruptions", "low_stt_confidence", "handoff"]
    ]
    pending_expiry: bool


class CallPage(View):
    items: list[CallView]
    total: int
    has_more: bool
    next_cursor: str | None
    snapshot_at: datetime


class SafeEvent(View):
    ordinal: int
    kind: EventKind
    occurred_at: datetime
    tool_name: ToolName | None
    tool_status: Literal["success", "failure"] | None
    language: Language


class EventPage(View):
    items: list[SafeEvent]
    total: int
    has_more: bool
    next_cursor: int | None


PrivacyReason = Literal[
    "off",
    "pending",
    "declined",
    "live_disabled",
    "deleted",
    "expired",
    "no_captured",
    "captured",
    "available",
]


class PrivacyView(View):
    reason: PrivacyReason
    availability: Literal["unavailable", "unavailable_to_role", "available"]
    text_access: Literal["restricted", "manager"]


class MeasurementView(View):
    id: UUID
    stage: Stage
    language: Language
    origin: Literal["server", "native"]
    duration_ms: float
    observed_at: datetime
    turn_ordinal: int | None


class MeasurementPage(View):
    items: list[MeasurementView]
    total: int
    has_more: bool
    next_cursor: str | None


class CallDetail(View):
    call: CallView
    trace_id: UUID
    privacy: PrivacyView
    usage: UsageView
    measurements: MeasurementPage
    summary: str
    operations: dict[Literal["book", "cancel", "reschedule"], int]
    attempted_tasks: int
    completed_tasks: int
    events: EventPage


class TextItem(View):
    ordinal: int
    speaker: Literal["user", "assistant"]
    occurred_at: datetime
    text: str


class TranscriptPage(View):
    items: list[TextItem]
    total: int
    has_more: bool
    next_cursor: int | None
    privacy: PrivacyView
    residual_sensitive_content: Literal[True] = True


class SettingsView(View):
    revision: int
    capture_enabled: bool
    retention_days: int
    updated_at: datetime
