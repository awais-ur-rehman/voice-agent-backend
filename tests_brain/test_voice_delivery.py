"""Pure core tests: playback evidence must precede a voice confirmation."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from brain.tools import ToolEngine

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def state(mode="voice"):
    return {
        "transport_mode": mode,
        "revision": 8,
        "readback": {"kind": "appointment", "revision": 8},
    }


def test_issued_voice_readback_is_not_confirmation():
    engine = ToolEngine(None, None, None, NOW)
    assert not engine._confirmed(state(), "yes", "appointment")


def test_text_confirmation_keeps_existing_behavior():
    engine = ToolEngine(None, None, None, NOW)
    assert engine._confirmed(state("text"), "yes", "appointment")


@pytest.fixture
def delivered():
    from brain.voice_delivery import OutputSegment, VoiceDelivery, VoiceTurnContext

    segment = OutputSegment(
        segment_id="segment-one",
        published_samples=24000,
        acknowledged_samples=24000,
        sample_rate=24000,
        published_at=NOW - timedelta(seconds=4),
        acknowledged_at=NOW - timedelta(seconds=2),
    )
    delivery = VoiceDelivery(
        generation=3,
        readback_revision=8,
        participant="opaque-caller",
        segments=(segment,),
        completed_at=NOW - timedelta(seconds=2),
        deadline_at=NOW + timedelta(seconds=10),
    )
    return VoiceTurnContext(
        generation=3,
        readback_revision=8,
        utterance_started_at=NOW - timedelta(seconds=1),
        delivery=delivery,
    )


def confirmed(context, text="yes"):
    engine = ToolEngine(None, None, None, NOW, voice_context=context)
    return engine._confirmed(state(), text, "appointment")


def test_later_affirmative_after_all_current_segments_are_delivered(delivered):
    assert confirmed(delivered)
    assert not confirmed(delivered, "yes but tomorrow")


@pytest.mark.parametrize("offset", [-1, 0])
def test_early_start_even_with_late_asr_is_rejected(delivered, offset):
    assert not confirmed(
        dataclasses.replace(
            delivered,
            utterance_started_at=delivered.delivery.completed_at + timedelta(seconds=offset),
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": 2},
        {"readback_revision": 7},
        {"utterance_started_at": NOW + timedelta(seconds=1)},
        {"utterance_started_at": NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_or_stale_turn_context_rejected(delivered, changes):
    assert not confirmed(dataclasses.replace(delivered, **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": 2},
        {"readback_revision": 7},
        {"invalidated": True},
        {"segments": ()},
        {"participant": ""},
        {"completed_at": None},
        {"completed_at": NOW + timedelta(seconds=1)},
        {"completed_at": NOW - timedelta(seconds=5)},
        {"deadline_at": NOW - timedelta(seconds=1)},
        {"deadline_at": NOW.replace(tzinfo=None)},
    ],
)
def test_incomplete_stale_or_expired_delivery_rejected(delivered, changes):
    assert not confirmed(
        dataclasses.replace(delivered, delivery=dataclasses.replace(delivered.delivery, **changes))
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"acknowledged_samples": 23999},
        {"acknowledged_samples": 24001},
        {"acknowledged_samples": -1},
        {"published_samples": 0},
        {"published_samples": True},
        {"acknowledged_samples": 24000.0},
        {"sample_rate": 0},
        {"sample_rate": -1},
        {"segment_id": ""},
        {"acknowledged_at": None},
        {"acknowledged_at": NOW - timedelta(seconds=3, microseconds=1)},
        {"acknowledged_at": NOW - timedelta(seconds=1)},
        {"published_at": NOW - timedelta(seconds=1)},
        {"published_at": NOW.replace(tzinfo=None)},
    ],
)
def test_bad_offsets_and_impossible_playback_timing_fail_closed(delivered, changes):
    segment = dataclasses.replace(delivered.delivery.segments[0], **changes)
    assert not confirmed(
        dataclasses.replace(
            delivered, delivery=dataclasses.replace(delivered.delivery, segments=(segment,))
        )
    )


def test_every_segment_must_be_acknowledged(delivered):
    segment = dataclasses.replace(
        delivered.delivery.segments[0],
        segment_id="segment-two",
        acknowledged_samples=0,
        acknowledged_at=None,
    )
    assert not confirmed(
        dataclasses.replace(
            delivered,
            delivery=dataclasses.replace(
                delivered.delivery, segments=delivered.delivery.segments + (segment,)
            ),
        )
    )


def test_duplicate_segment_ids_cannot_double_count_acknowledgment(delivered):
    segment = delivered.delivery.segments[0]
    assert not confirmed(
        dataclasses.replace(
            delivered, delivery=dataclasses.replace(delivered.delivery, segments=(segment, segment))
        )
    )


def test_parallel_fast_acks_cannot_skip_total_playback_duration(delivered):
    segment = dataclasses.replace(
        delivered.delivery.segments[0],
        published_samples=48000,
        acknowledged_samples=48000,
    )
    other = dataclasses.replace(segment, segment_id="segment-two")
    assert not confirmed(
        dataclasses.replace(
            delivered, delivery=dataclasses.replace(delivered.delivery, segments=(segment, other))
        )
    )


def test_context_and_ledger_are_immutable(delivered):
    with pytest.raises(dataclasses.FrozenInstanceError):
        delivered.generation = 9
    with pytest.raises(dataclasses.FrozenInstanceError):
        delivered.delivery.completed_at = NOW


def test_raw_mapping_cannot_be_used_as_trusted_context():
    assert not confirmed({"generation": 3, "readback_revision": 8, "delivered": True})
