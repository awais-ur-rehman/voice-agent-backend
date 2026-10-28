from datetime import timedelta
from uuid import uuid4

import pytest

from scheduler.errors import AppError
from tests_api.conftest import NOW


def test_delivery_requires_published_drained_and_acknowledged_audio():
    from brain.voice_contracts import PlaybackAck
    from brain.voice_playback import acknowledge, finish_delivery, publish_segment, turn_context

    sid = uuid4()
    state = {"readback": {"revision": 4}, "revision": 4}
    ledger = publish_segment(
        None, state, 2, "caller", "segment-1", 16000, 16000, NOW, NOW + timedelta(seconds=120)
    )

    def ack(samples, sequence=1):
        return PlaybackAck(
            version=1,
            session_id=sid,
            generation=2,
            sequence=sequence,
            segment_id="segment-1",
            played_samples=samples,
        )

    with pytest.raises(AppError):
        acknowledge(ledger, ack(16000), "caller", NOW + timedelta(milliseconds=500))
    with pytest.raises(AppError):
        acknowledge(ledger, ack(16000), "other", NOW + timedelta(seconds=1))
    acknowledge(ledger, ack(16000), "caller", NOW + timedelta(seconds=1))
    ctx = turn_context(ledger, 2, NOW + timedelta(seconds=2))
    assert not ctx.permits_confirmation(4, NOW + timedelta(seconds=3))
    finish_delivery(ledger, NOW + timedelta(seconds=2))
    assert turn_context(ledger, 2, NOW + timedelta(seconds=3)).permits_confirmation(
        4, NOW + timedelta(seconds=4)
    )
    assert not turn_context(ledger, 2, NOW + timedelta(seconds=1)).permits_confirmation(
        4, NOW + timedelta(seconds=4)
    )
    with pytest.raises(AppError):
        acknowledge(ledger, ack(16000), "caller", NOW + timedelta(seconds=3))


def test_stale_readback_cannot_append_to_delivery():
    from brain.voice_playback import publish_segment

    state = {"readback": {"revision": 4}, "revision": 4}
    ledger = publish_segment(
        None, state, 2, "caller", "segment-1", 16000, 16000, NOW, NOW + timedelta(seconds=120)
    )
    state["readback"]["revision"] = 5
    with pytest.raises(AppError):
        publish_segment(
            ledger, state, 2, "caller", "segment-2", 16000, 16000, NOW, NOW + timedelta(seconds=120)
        )
