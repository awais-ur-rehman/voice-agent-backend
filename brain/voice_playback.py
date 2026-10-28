"""Protected playback ledger; the authenticated worker boundary owns persistence.

Timestamps originate at the API. RTC participant acknowledgments are bounded by
published samples and elapsed playback time, never accepted as state patches.
"""

from datetime import datetime, timedelta

from scheduler.errors import AppError

from .voice_contracts import PlaybackAck
from .voice_delivery import OutputSegment, VoiceDelivery, VoiceTurnContext


def publish_segment(
    ledger, state, generation, participant, segment_id, samples, sample_rate, now, deadline
):
    readback = state.get("readback")
    if (
        not readback
        or type(samples) is not int
        or type(sample_rate) is not int
        or not 16000 <= sample_rate <= 48000
        or not 0 < samples <= sample_rate * 30
        or not now < deadline
        or not isinstance(segment_id, str)
        or not 1 <= len(segment_id) <= 64
    ):
        raise AppError("validation_error", 422)
    revision = readback["revision"]
    if ledger is None:
        ledger = {
            "generation": generation,
            "readback_revision": revision,
            "participant": participant,
            "deadline_at": deadline.isoformat(),
            "completed_at": None,
            "finished_at": None,
            "sequence": 0,
            "segments": [],
        }
    if (
        ledger["generation"] != generation
        or ledger["readback_revision"] != revision
        or ledger["participant"] != participant
        or ledger["finished_at"] is not None
        or ledger.get("invalidated")
        or len(ledger["segments"]) >= 64
    ):
        raise AppError("idempotency_conflict", 409)
    existing = next((s for s in ledger["segments"] if s["segment_id"] == segment_id), None)
    if existing:
        if existing["published_samples"] != samples or existing["sample_rate"] != sample_rate:
            raise AppError("idempotency_conflict", 409)
        return ledger
    ledger["segments"].append(
        {
            "segment_id": segment_id,
            "published_samples": samples,
            "acknowledged_samples": 0,
            "sample_rate": sample_rate,
            "published_at": now.isoformat(),
            "acknowledged_at": None,
        }
    )
    return ledger


def acknowledge(ledger: dict, ack: PlaybackAck, participant: str, now: datetime) -> None:
    if (
        ledger["participant"] != participant
        or ledger["generation"] != ack.generation
        or ledger.get("invalidated")
        or now >= datetime.fromisoformat(ledger["deadline_at"])
        or ack.sequence <= ledger["sequence"]
    ):
        raise AppError("validation_error", 422)
    segment = next((s for s in ledger["segments"] if s["segment_id"] == ack.segment_id), None)
    if (
        segment is None
        or ack.played_samples != segment["published_samples"]
        or now - datetime.fromisoformat(segment["published_at"])
        < timedelta(seconds=segment["published_samples"] / segment["sample_rate"])
    ):
        raise AppError("validation_error", 422)
    segment["acknowledged_samples"] = ack.played_samples
    segment["acknowledged_at"] = now.isoformat()
    ledger["sequence"] = ack.sequence
    _complete(ledger, now)


def finish_delivery(ledger: dict, now: datetime) -> None:
    if ledger.get("invalidated") or not ledger["segments"]:
        raise AppError("validation_error", 422)
    if now >= datetime.fromisoformat(ledger["deadline_at"]):
        raise AppError("validation_error", 422)
    ledger["finished_at"] = now.isoformat()
    _complete(ledger, now)


def _complete(ledger, now):
    if ledger["finished_at"] and all(
        s["acknowledged_at"] and s["acknowledged_samples"] == s["published_samples"]
        for s in ledger["segments"]
    ):
        ledger["completed_at"] = now.isoformat()


def turn_context(ledger: dict | None, generation: int, utterance_started_at: datetime):
    if ledger is None:
        return None

    def timestamp(value):
        return datetime.fromisoformat(value) if value else None

    delivery = VoiceDelivery(
        generation=ledger["generation"],
        readback_revision=ledger["readback_revision"],
        participant=ledger["participant"],
        segments=tuple(
            OutputSegment(
                **(
                    segment
                    | {
                        "published_at": timestamp(segment["published_at"]),
                        "acknowledged_at": timestamp(segment["acknowledged_at"]),
                    }
                )
            )
            for segment in ledger["segments"]
        ),
        completed_at=timestamp(ledger["completed_at"]),
        deadline_at=datetime.fromisoformat(ledger["deadline_at"]),
        invalidated=ledger.get("invalidated", False),
    )
    return VoiceTurnContext(generation, ledger["readback_revision"], utterance_started_at, delivery)
