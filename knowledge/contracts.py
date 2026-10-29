"""Public knowledge contracts; never expose storage or worker authority."""

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator

from scheduler.contracts import Contract

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000_000)]
FileFormat = Literal["pdf", "docx", "txt", "md"]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]


class TextSource(Contract):
    kind: Literal["text"]
    name: Name
    text: Text
    format: Literal["txt", "md"]


class FAQSource(Contract):
    kind: Literal["faq"]
    name: Name
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]
    answer: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100_000)
    ]


class URLSource(Contract):
    kind: Literal["url"]
    name: Name
    url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]

    @field_validator("url")
    @classmethod
    def https_only(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("A public HTTPS URL is required")
        return value


SourceCreate = Annotated[TextSource | FAQSource | URLSource, Field(discriminator="kind")]


class TextUpdate(TextSource):
    revision: int = Field(ge=1)


class FAQUpdate(FAQSource):
    revision: int = Field(ge=1)


class URLUpdate(URLSource):
    revision: int = Field(ge=1)


SourceUpdate = Annotated[TextUpdate | FAQUpdate | URLUpdate, Field(discriminator="kind")]


class Revision(Contract):
    revision: int = Field(ge=1)


class Upload(Contract):
    kind: Literal["file"] = "file"
    name: Name
    format: FileFormat
    checksum: Digest
    revision: int | None = Field(default=None, ge=1)


class RetrievalQuery(Contract):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class SourceOut(Contract):
    id: UUID
    agent_id: UUID
    kind: Literal["text", "faq", "url", "file"]
    name: str
    revision: int
    status: Literal["queued", "processing", "ready", "failed", "deleted"]
    available: bool
    active_generation_id: UUID | None
    chunk_count: int
    error_code: str | None
    error_at: datetime | None
    updated_at: datetime


class Capabilities(Contract):
    enabled: bool
    processing_available: Literal[False] = False
    unavailable_reason: Literal["disabled", "processing_unavailable"]
    upload_max_bytes: int = 10 * 1024 * 1024
    source_limit: int = 64


class Preview(Contract):
    source_id: UUID
    available: bool
    reason: Literal["ready", "processing_unavailable"]
    excerpts: list[str]
