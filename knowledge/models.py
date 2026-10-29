"""ORM mappings for tenant qualified knowledge rows."""

from datetime import datetime
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from scheduler.models import Base, Row


class Source(Row, Base):
    __tablename__ = "knowledge_sources"
    agent_id: Mapped[UUID]
    kind: Mapped[str]
    name: Mapped[str]
    revision: Mapped[int] = mapped_column(default=1)
    requested_revision: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="queued")
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    blob_key: Mapped[str | None]
    checksum: Mapped[str | None]
    active_generation_id: Mapped[UUID | None]
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None]
    error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Generation(Row, Base):
    __tablename__ = "knowledge_generations"
    agent_id: Mapped[UUID]
    source_id: Mapped[UUID]
    source_revision: Mapped[int]
    profile: Mapped[str]
    chunk_count: Mapped[int]
    sealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default="NULL"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chunk(Row, Base):
    __tablename__ = "knowledge_chunks"
    agent_id: Mapped[UUID]
    source_id: Mapped[UUID]
    generation_id: Mapped[UUID]
    ordinal: Mapped[int]
    heading: Mapped[str] = mapped_column(default="")
    language: Mapped[str]
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int]
    embedding: Mapped[list[float]] = mapped_column(Vector(1024))
    search: Mapped[str] = mapped_column(TSVECTOR, server_default="")


class Job(Row, Base):
    __tablename__ = "knowledge_jobs"
    agent_id: Mapped[UUID]
    source_id: Mapped[UUID]
    source_revision: Mapped[int]
    status: Mapped[str] = mapped_column(default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QueryEvent(Row, Base):
    __tablename__ = "knowledge_query_events"
    agent_id: Mapped[UUID]
    command_id: Mapped[UUID]
    query_hash: Mapped[str]
    language: Mapped[str]
    purpose: Mapped[str]
    decision: Mapped[str]
    redacted_question: Mapped[str | None] = mapped_column(String(1000))
    refs: Mapped[list] = mapped_column(JSONB, default=list)
    duration_ms: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
