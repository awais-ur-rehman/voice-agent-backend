import asyncio
import threading

import pytest

from voice_runtime.speech.contracts import PCMChunk


def test_interruption_discards_pending_tts_and_invalidates_before_late_audio():
    from voice_runtime.session import VoiceController

    started = threading.Event()
    release = threading.Event()

    class SlowSpeech:
        def synthesize(self, *args, **kwargs):
            started.set()
            release.wait(2)
            return PCMChunk(b"\1\0" * 160, 16000)

        def cancel(self):
            release.set()

    async def scenario():
        events = []

        async def emit(kind, **fields):
            events.append(kind)

        controller = VoiceController(emit=emit, speech=SlowSpeech())
        job = asyncio.create_task(controller.speak("hello", "en"))
        await asyncio.to_thread(started.wait, 1)
        await controller.interrupt()
        await job
        assert events == ["thinking", "invalidate", "listening"]

    asyncio.run(scenario())


def test_silence_mute_and_call_deadline_are_distinct():
    from voice_runtime.session import ActivityTimers

    timers = ActivityTimers(started=0)
    assert timers.tick(6) == "reprompt"
    assert timers.tick(12) == "reprompt"
    assert timers.tick(18) == "end"
    timers = ActivityTimers(started=0)
    timers.muted = True
    assert timers.tick(19) is None
    assert timers.tick(600) == "end"


def test_playback_ack_never_exceeds_published_and_stale_fails():
    from voice_runtime.delivery import DeliveryLedger

    ledger = DeliveryLedger("sid", generation=3, participant="caller")
    ledger.publish("segment", 160, 16000)
    with pytest.raises(ValueError):
        ledger.ack(
            {
                "version": 1,
                "session_id": "sid",
                "generation": 3,
                "sequence": 1,
                "segment_id": "segment",
                "played_samples": 161,
            },
            participant="caller",
        )
    assert not ledger.complete
    ledger.ack(
        {
            "version": 1,
            "session_id": "sid",
            "generation": 3,
            "sequence": 2,
            "segment_id": "segment",
            "played_samples": 160,
        },
        participant="caller",
    )
    assert ledger.complete
    ledger.invalidate()
    with pytest.raises(ValueError):
        ledger.ack(
            {
                "version": 1,
                "session_id": "sid",
                "generation": 3,
                "sequence": 3,
                "segment_id": "segment",
                "played_samples": 160,
            },
            participant="caller",
        )
