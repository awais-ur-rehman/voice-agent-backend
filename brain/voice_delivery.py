"""Immutable playback evidence supplied only by the authenticated SQL voice boundary.

These are server contracts, never action arguments. The caller participant and
worker generation must be checked before building this snapshot. Sample offsets
are a conservative lower bound on playback, not evidence of human attention.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class OutputSegment:
    segment_id: str
    published_samples: int
    acknowledged_samples: int
    sample_rate: int
    published_at: datetime
    acknowledged_at: datetime | None


@dataclass(frozen=True)
class VoiceDelivery:
    generation: int
    readback_revision: int
    participant: str
    segments: tuple[OutputSegment, ...]
    completed_at: datetime | None
    deadline_at: datetime
    invalidated: bool = False


@dataclass(frozen=True)
class VoiceTurnContext:
    generation: int
    readback_revision: int
    utterance_started_at: datetime
    delivery: VoiceDelivery

    def permits_confirmation(self, readback_revision: int, now: datetime) -> bool:
        delivery = self.delivery
        if (
            not isinstance(delivery, VoiceDelivery)
            or not _positive_integer(self.generation)
            or not _positive_integer(self.readback_revision)
            or type(delivery.generation) is not int
            or type(delivery.readback_revision) is not int
            or self.generation != delivery.generation
            or self.readback_revision != delivery.readback_revision
            or self.readback_revision != readback_revision
            or delivery.invalidated is not False
            or not isinstance(delivery.participant, str)
            or not delivery.participant
            or not isinstance(delivery.segments, tuple)
            or not 0 < len(delivery.segments) <= 256
            or not all(
                _aware(value)
                for value in (
                    now,
                    self.utterance_started_at,
                    delivery.completed_at,
                    delivery.deadline_at,
                )
            )
        ):
            return False
        completed = delivery.completed_at
        assert completed is not None
        if not completed < self.utterance_started_at <= now < delivery.deadline_at:
            return False
        seen: set[str] = set()
        total_duration = timedelta()
        first_publish = completed
        for segment in delivery.segments:
            if (
                not isinstance(segment, OutputSegment)
                or not isinstance(segment.segment_id, str)
                or not segment.segment_id
                or segment.segment_id in seen
                or not _positive_integer(segment.published_samples)
                or type(segment.acknowledged_samples) is not int
                or segment.acknowledged_samples != segment.published_samples
                or not _positive_integer(segment.sample_rate)
                or segment.sample_rate > 192000
                or segment.published_samples > segment.sample_rate * 600
                or not _aware(segment.published_at)
                or not _aware(segment.acknowledged_at)
            ):
                return False
            acknowledged = segment.acknowledged_at
            assert acknowledged is not None
            duration = timedelta(seconds=segment.published_samples / segment.sample_rate)
            if acknowledged > completed or acknowledged - segment.published_at < duration:
                return False
            seen.add(segment.segment_id)
            first_publish = min(first_publish, segment.published_at)
            total_duration += duration
        return completed - first_publish >= total_duration


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _positive_integer(value: object) -> bool:
    return type(value) is int and value > 0
