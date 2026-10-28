"""Version-one voice transport messages. Browser messages carry no state authority."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from scheduler.contracts import Contract


class VoiceCreate(Contract):
    agent_id: UUID
    request_key: str = Field(min_length=1, max_length=128)


class MediaGrant(Contract):
    url: str
    room: str
    participant: str
    token: str
    expires_at: datetime
    session_id: UUID
    generation: int = Field(ge=1)


class VoiceSnapshot(Contract):
    session_id: UUID
    status: Literal["provisioning", "active", "disconnected", "ended", "failed"]
    generation: int = Field(ge=1)
    revision: int = Field(ge=0)
    call_deadline: datetime
    media: MediaGrant | None = None


class PlaybackAck(Contract):
    version: Literal[1]
    session_id: UUID
    generation: int = Field(ge=1)
    sequence: int = Field(ge=1, le=2_147_483_647)
    segment_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    played_samples: int = Field(ge=0, le=4_800_000)


class CallControl(Contract):
    version: Literal[1]
    session_id: UUID
    generation: int = Field(ge=1)
    sequence: int = Field(ge=1, le=2_147_483_647)
    kind: Literal["mute", "unmute", "end"]


class WorkerGrant(Contract):
    """Internal only; never nested in a browser response."""

    session_id: UUID
    voice_id: UUID
    worker_id: UUID
    generation: int = Field(ge=1)
    token: str
    runtime_token: str
    media: MediaGrant
    lease_until: datetime


class WorkerMessage(Contract):
    version: Literal[1]
    session_id: UUID
    generation: int = Field(ge=1)
    sequence: int = Field(ge=1, le=2_147_483_647)


class VoiceStateMessage(WorkerMessage):
    kind: Literal["state"]
    state: Literal["listening", "thinking", "speaking", "error"]
    code: str | None = Field(default=None, max_length=64)


class CaptionMessage(WorkerMessage):
    kind: Literal["caption"]
    text: str = Field(max_length=2000)
    lang: Literal["en", "ur"]


class SegmentMessage(WorkerMessage):
    kind: Literal["segment"]
    segment_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    sample_rate: int = Field(ge=16000, le=48000)
    total_samples: int = Field(ge=1, le=4_800_000)
    critical: bool


class SegmentEndMessage(WorkerMessage):
    kind: Literal["segment_end"]
    segment_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class InvalidateMessage(WorkerMessage):
    kind: Literal["invalidate"]
