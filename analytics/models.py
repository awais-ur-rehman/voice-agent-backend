"""Typed analytics persistence. Migrations own constraints and tenant-qualified keys."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from analytics.contracts import (
    AlertKind,
    CallStatus,
    Channel,
    Consent,
    EndReason,
    EventKind,
    InvocationStatus,
    Language,
    Origin,
    Outcome,
    Purpose,
    Stage,
    TaskKind,
    TaskStatus,
    ToolName,
)
from scheduler.models import Base, Row


class AnalyticsSettings(Base):
    __tablename__ = "analytics_settings"
    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    capture_enabled: Mapped[bool] = mapped_column(default=False)
    retention_days: Mapped[int] = mapped_column(default=7)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CallRecord(Base):
    __tablename__ = "analytics_calls"
    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    agent_id: Mapped[UUID]
    trace_id: Mapped[UUID]
    purpose: Mapped[Purpose] = mapped_column(String)
    channel: Mapped[Channel] = mapped_column(String)
    timezone: Mapped[str]
    local_date: Mapped[date]
    utc_offset_minutes: Mapped[int]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[CallStatus] = mapped_column(String, default="active")
    outcome: Mapped[Outcome | None] = mapped_column(String)
    end_reason: Mapped[EndReason | None] = mapped_column(String)
    language: Mapped[Language] = mapped_column(String, default="unknown")
    capture_enabled_at_start: Mapped[bool] = mapped_column(default=False)
    consent: Mapped[Consent] = mapped_column(String, default="off")
    consent_notice_version: Mapped[str | None] = mapped_column(String)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transcript_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_task_id: Mapped[UUID | None]
    parked_task_ids: Mapped[list[str | None]] = mapped_column(JSONB, default=list)
    quality_flags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    summary_revision: Mapped[int] = mapped_column(default=0)
    summarized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionEvent(Row):
    session_id: Mapped[UUID]
    event_id: Mapped[UUID]


class TaskRecord(Row, Base):
    __tablename__ = "analytics_tasks"
    session_id: Mapped[UUID]
    kind: Mapped[TaskKind] = mapped_column(String)
    status: Mapped[TaskStatus] = mapped_column(String, default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    caller_turns: Mapped[int] = mapped_column(default=0)


class OperationEvent(SessionEvent, Base):
    __tablename__ = "analytics_operations"
    task_id: Mapped[UUID | None]
    operation: Mapped[TaskKind] = mapped_column(String)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TurnEvent(SessionEvent, Base):
    __tablename__ = "analytics_turns"
    command_id: Mapped[UUID | None]
    task_id: Mapped[UUID | None]
    kind: Mapped[EventKind] = mapped_column(String)
    language: Mapped[Language] = mapped_column(String)
    tool_name: Mapped[ToolName | None] = mapped_column(String)
    tool_status: Mapped[str | None]
    safe_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Transcript(SessionEvent, Base):
    __tablename__ = "analytics_transcripts"
    speaker: Mapped[str]
    text: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StageMetric(SessionEvent, Base):
    __tablename__ = "analytics_stage_metrics"
    turn_event_id: Mapped[UUID | None]
    stage: Mapped[Stage] = mapped_column(String)
    language: Mapped[Language] = mapped_column(String)
    origin: Mapped[Origin] = mapped_column(String)
    duration_ms: Mapped[float]
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsageInvocation(Row, Base):
    __tablename__ = "analytics_usage_invocations"
    session_id: Mapped[UUID]
    invocation_id: Mapped[UUID]
    provider: Mapped[str]
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[InvocationStatus] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    input_rate: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    output_rate: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    cost: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))


class Bucket(Row):
    agent_id: Mapped[UUID]
    purpose: Mapped[Purpose] = mapped_column(String)
    channel: Mapped[Channel] = mapped_column(String)
    timezone: Mapped[str]
    local_date: Mapped[date]


class Rollup(Bucket):
    sessions: Mapped[int] = mapped_column(BigInteger, default=0)
    ended_sessions: Mapped[int] = mapped_column(BigInteger, default=0)
    booking_sessions: Mapped[int] = mapped_column(BigInteger, default=0)
    handoff_sessions: Mapped[int] = mapped_column(BigInteger, default=0)
    abandoned_sessions: Mapped[int] = mapped_column(BigInteger, default=0)
    book_operations: Mapped[int] = mapped_column(BigInteger, default=0)
    cancel_operations: Mapped[int] = mapped_column(BigInteger, default=0)
    reschedule_operations: Mapped[int] = mapped_column(BigInteger, default=0)
    attempted_tasks: Mapped[int] = mapped_column(BigInteger, default=0)
    completed_tasks: Mapped[int] = mapped_column(BigInteger, default=0)
    caller_turns: Mapped[int] = mapped_column(BigInteger, default=0)
    tool_results: Mapped[int] = mapped_column(BigInteger, default=0)
    tool_failures: Mapped[int] = mapped_column(BigInteger, default=0)
    prompt_emissions: Mapped[int] = mapped_column(BigInteger, default=0)
    repeated_prompts: Mapped[int] = mapped_column(BigInteger, default=0)
    ended_duration_samples: Mapped[int] = mapped_column(BigInteger, default=0)
    ended_duration_ms: Mapped[Decimal] = mapped_column(Numeric(30, 3), default=0)
    usage_invocations: Mapped[int] = mapped_column(BigInteger, default=0)
    usage_known: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_known: Mapped[int] = mapped_column(BigInteger, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    known_cost: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HourlyRollup(Rollup, Base):
    __tablename__ = "analytics_hourly_rollups"
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    utc_offset_minutes: Mapped[int]


class DailyRollup(Rollup, Base):
    __tablename__ = "analytics_daily_rollups"


class DirtyBucket(Bucket, Base):
    __tablename__ = "analytics_dirty_buckets"
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    utc_offset_minutes: Mapped[int]
    revision: Mapped[int] = mapped_column(BigInteger, default=1)
    dirty_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalyticsAlert(Row, Base):
    __tablename__ = "analytics_alerts"
    agent_id: Mapped[UUID]
    channel: Mapped[Channel] = mapped_column(String)
    kind: Mapped[AlertKind] = mapped_column(String)
    state: Mapped[str]
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    numerator: Mapped[int] = mapped_column(BigInteger)
    denominator: Mapped[int] = mapped_column(BigInteger)
    observed_value: Mapped[float | None]
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
