from uuid import uuid4

import pytest
from pydantic import ValidationError


def test_control_rejects_unbounded_or_state_patch_messages():
    from brain.voice_contracts import PlaybackAck

    valid = dict(
        version=1,
        session_id=str(uuid4()),
        generation=1,
        sequence=1,
        segment_id="segment-1",
        played_samples=16000,
    )
    assert PlaybackAck(**valid).played_samples == 16000
    for patch in (
        {"played_samples": -1},
        {"played_samples": 100_000_000},
        {"verified": True},
        {"generation": 0},
        {"version": 2},
    ):
        with pytest.raises(ValidationError):
            PlaybackAck(**(valid | patch))


def test_media_response_has_no_worker_or_runtime_capability():
    from brain.voice_contracts import MediaGrant

    assert set(MediaGrant.model_fields) == {
        "url",
        "room",
        "participant",
        "token",
        "expires_at",
        "session_id",
        "generation",
    }
