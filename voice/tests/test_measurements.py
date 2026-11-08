"""Lightweight timing/control tests, no weights/RTC/provider requests."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_queue_bounds_nonblocking_epoch_revision_fences_and_failures():
    from voice_runtime.telemetry import MetricQueue

    async def scenario():
        block = asyncio.Event()
        submitted = []

        async def send(body):
            submitted.append(body)
            await block.wait()

        call = SimpleNamespace(epoch=3, generation=1, api=SimpleNamespace(revision=2, metrics=send))
        queue = MetricQueue(call)
        for i in range(100):
            queue.offer("stt_completion", i, "en", 3, metadata={"uncertain": True})
        assert len(queue.pending) <= 32
        await asyncio.sleep(0)
        call.epoch = 4
        call.api.revision = 3
        queue.offer("stt_completion", 4, "en", 3)
        block.set()
        await queue.flush()
        assert len(submitted) == 1 and submitted[0]["revision"] == 2
        assert len(submitted[0]["observations"]) <= 32
        assert all(v["output_kind"] == "input" for v in submitted[0]["observations"])

    asyncio.run(scenario())


def test_uncertain_and_low_confidence_asr_are_safe_observations_before_api():
    from voice_runtime.pipeline import NativeCall
    from voice_runtime.speech.contracts import ASRResult, PCMChunk
    from voice_runtime.telemetry import MetricQueue

    async def scenario():
        for result in [
            ASRResult("private", "en", 0.9, True),
            ASRResult("private", "en", 0.3, False),
        ]:
            call = NativeCall.__new__(NativeCall)
            call.epoch = 1
            call.generation = 1
            call.language_mode = "en"
            call.api = SimpleNamespace(revision=0, metrics=AsyncMock(), turn=AsyncMock())
            call.metrics = MetricQueue(call)
            call.message = AsyncMock()
            call.fail = AsyncMock()
            call.asr = SimpleNamespace(
                transcribe=lambda *args: result, supervisor=SimpleNamespace(generation=1)
            )
            await call.turn(PCMChunk(b"\0\0" * 160, 16000), datetime.now(UTC), 1)
            await call.metrics.flush()
            call.api.turn.assert_not_awaited()
            observation = call.api.metrics.call_args.args[0]["observations"][0]
            assert (
                observation["stage"] == "stt_completion"
                and observation["metadata"]["stt_confidence"] == result.confidence
            )
            assert "private" not in repr(observation) and observation["command_id"] is None

    asyncio.run(scenario())


def test_vad_anchor_is_last_voiced_boundary_not_start_or_silence(monkeypatch):
    from pipecat.audio.vad.vad_analyzer import VADState
    from pipecat.frames.frames import UserAudioRawFrame

    import voice_runtime.pipeline as module

    monkeypatch.setattr(module.FrameProcessor, "process_frame", AsyncMock())
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    call = SimpleNamespace(
        caller="caller",
        timers=SimpleNamespace(muted=False, activity=lambda _: None),
        interrupt=AsyncMock(),
        spawn_turn=lambda *args, **kw: anchors.append(kw["end_of_speech"]),
    )
    anchors = []
    processor = module.CallerProcessor.__new__(module.CallerProcessor)
    processor.call = call
    processor.buffer = bytearray()
    processor.preroll = b""
    processor.speaking = False
    processor.started = None
    processor.last_voiced = None
    processor.vad = SimpleNamespace(
        analyze_audio=AsyncMock(side_effect=[VADState.SPEAKING, VADState.STOPPING, VADState.QUIET])
    )

    async def scenario():
        for tick in [100.0, 100.2, 100.6]:
            now[0] = tick
            await processor.process_frame(
                UserAudioRawFrame(
                    audio=b"\0\0" * 160, sample_rate=16000, num_channels=1, user_id="caller"
                ),
                None,
            )

    asyncio.run(scenario())
    assert anchors == [100.0]


def test_buffered_and_cached_output_never_become_first_byte_or_published_audio():
    import time

    from voice_runtime.pipeline import NativeCall
    from voice_runtime.speech.contracts import PCMChunk
    from voice_runtime.telemetry import MetricQueue, output_measurement

    async def scenario():
        for cached, filler in [(False, False), (True, False), (True, True)]:
            submitted = []

            async def send(body):
                submitted.extend(body["observations"])

            call = NativeCall.__new__(NativeCall)
            call.session_id = "synthetic"
            call.caller = "caller"
            call.generation = 1
            call.epoch = 2
            call.ending = False
            call.api = SimpleNamespace(revision=3, metrics=send)
            call.metrics = MetricQueue(call)
            call.message = AsyncMock()
            pcm = PCMChunk(b"\0\0" * 960, 24000)
            call.cache = SimpleNamespace(
                get=lambda *args: pcm if cached else None, put=lambda *args: None
            )
            call.english = call.urdu = SimpleNamespace(
                synthesize=lambda *args: pcm, supervisor=SimpleNamespace(generation=1)
            )
            call.worker = SimpleNamespace(queue_frame=AsyncMock())
            call.timers = SimpleNamespace(activity=lambda _: None)
            token = output_measurement.set(
                {
                    "command_id": "f2ca0fae-1111-4111-8111-88c58e1db6cc",
                    "revision": 3,
                    "output_kind": "response",
                    "end_of_speech": time.monotonic() - 0.1,
                }
            )
            try:
                await call.speak("One moment, please.", "en", None, 2, status_only=filler)
            finally:
                output_measurement.reset(token)
            await call.metrics.flush()
            stages = {value["stage"] for value in submitted}
            assert not stages & {"tts_first_byte", "end_of_speech_to_audio", "llm_first_token"}
            assert "audio_queue" in stages
            assert ("tts_buffered_generation" in stages) is not cached
            assert ("end_of_speech_to_audio_queue" in stages) is not filler
            assert all(v["metadata"]["cache_hit"] == cached for v in submitted)
            assert all(v["output_kind"] == ("filler" if filler else "response") for v in submitted)

    asyncio.run(scenario())


def test_stale_epoch_and_failed_submission_remain_missing_without_breaking_control():
    from voice_runtime.telemetry import MetricQueue

    async def scenario():
        call = SimpleNamespace(
            epoch=1,
            generation=2,
            api=SimpleNamespace(
                revision=3, metrics=AsyncMock(side_effect=OSError("private failure"))
            ),
        )
        queue = MetricQueue(call)
        queue.offer("stt_completion", 1, "en", 0)
        assert not queue.pending and queue.task is None
        queue.offer("stt_completion", 1, "en", 1)
        await queue.flush()
        assert call.api.metrics.call_count == 1
        assert not queue.pending

    asyncio.run(scenario())


def disconnect_fixture(events, send):
    from voice_runtime.delivery import DeliveryLedger
    from voice_runtime.pipeline import NativeCall
    from voice_runtime.telemetry import MetricQueue

    call = NativeCall.__new__(NativeCall)
    call.epoch = 4
    call.generation = 1
    call.caller = "caller"
    call.session_id = "sid"
    call.input_sequence = 0
    call.ending = False
    call.timers = SimpleNamespace(started=0, tick=lambda now: "end")
    call.pending = None
    call.invalidation = None
    call.invalidation_jobs = set()
    call.current_output = None
    call.message = AsyncMock()

    class Adapter:
        def __init__(self, name):
            self.name = name

        def cancel(self):
            events.append("cancel-" + self.name)

    call.asr = Adapter("asr")
    call.english = Adapter("en")
    call.urdu = Adapter("ur")

    call.audio_fenced = asyncio.Event()

    async def queue(frame):
        events.append("audio-fenced")
        call.audio_fenced.set()

    async def cancel():
        events.append("worker-cancelled")

    call.worker = SimpleNamespace(queue_frame=queue, cancel=cancel)
    call.ledger = DeliveryLedger("sid", generation=1, participant="caller")
    call.ledger.publish("segment", 160, 16000)

    async def interrupt():
        events.append("server-interrupt")
        call.api.revision += 1
        return {"revision": call.api.revision}

    async def request(route, body):
        events.append(route)
        await asyncio.sleep(0)

    call.api = SimpleNamespace(revision=7, metrics=send, interrupt=interrupt, request=request)
    call.metrics = MetricQueue(call)
    return call


async def disconnect_callback(call, kind):
    if kind == "participant_left":
        await call.participant_left(None, "caller")
    elif kind == "disconnected":
        await call.disconnected(None)
    elif kind == "caller_end":
        import json

        await call.data_received(
            None,
            json.dumps(
                {
                    "version": 1,
                    "session_id": "sid",
                    "generation": 1,
                    "sequence": 1,
                    "kind": "end",
                }
            ),
            "caller",
        )
    elif kind == "deadline":
        await call.heartbeat()
    else:
        call.speak = AsyncMock()
        await call.complete_response(
            {"state": {"ended": True}, "result": {"say": {"en": "Goodbye."}}}, "en", 4
        )


def terminal_route(kind):
    return "/disconnect" if kind in {"participant_left", "disconnected"} else "/end"


@pytest.mark.parametrize(
    "kind", ["participant_left", "disconnected", "caller_end", "deadline", "terminal_response"]
)
def test_disconnect_callbacks_submit_pending_completed_observation_before_server_revocation(kind):
    async def scenario(kind):
        events = []
        submitted = []

        async def send(body):
            events.append("metrics")
            submitted.append(body)

        call = disconnect_fixture(events, send)
        # Do not yield: this accepted observation is pending when the actual callback begins.
        call.metrics.offer("stt_completion", 12, "en", 4)
        event_id = call.metrics.pending[0][2]["event_id"]
        await disconnect_callback(call, kind)
        await call.metrics.flush()
        assert len(submitted) == 1, events
        assert submitted[0]["revision"] == 7 and submitted[0]["generation"] == 1
        assert submitted[0]["observations"][0]["event_id"] == event_id
        assert events.index("metrics") < events.index(terminal_route(kind))
        if "server-interrupt" in events:
            assert events.index("metrics") < events.index("server-interrupt")
        assert call.epoch == 5 and call.ledger.invalidated
        assert events[-1] == "worker-cancelled"
        if call.invalidation_jobs:
            await asyncio.gather(*call.invalidation_jobs)

    asyncio.run(scenario(kind))


@pytest.mark.parametrize(
    "kind", ["participant_left", "disconnected", "caller_end", "deadline", "terminal_response"]
)
def test_disconnect_freezes_old_observations_and_cancels_locally_while_submission_waits(kind):
    async def scenario(kind):
        events = []
        submitted = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def send(body):
            events.append("metrics")
            submitted.append(body)
            entered.set()
            await release.wait()

        call = disconnect_fixture(events, send)
        call.metrics.offer("stt_completion", 12, "en", 4)
        closing = asyncio.create_task(disconnect_callback(call, kind))
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            # A blocked metrics request must not hold up local ASR/TTS/audio cancellation.
            await asyncio.wait_for(call.audio_fenced.wait(), 0.5)
            assert all("cancel-" + name in events for name in ["asr", "en", "ur"])
            assert terminal_route(kind) not in events and "server-interrupt" not in events
            call.metrics.offer("stt_completion", 99, "en", 4)
            call.metrics.offer("stt_completion", 100, "en", call.epoch)
            assert not call.metrics.pending
            release.set()
            await asyncio.wait_for(closing, 0.5)
            assert len(submitted) == 1
            assert [v["duration_ms"] for v in submitted[0]["observations"]] == [12]
            assert events.index("metrics") < events.index(terminal_route(kind))
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)
            if call.invalidation_jobs:
                await asyncio.gather(*call.invalidation_jobs)

    asyncio.run(scenario(kind))


@pytest.mark.parametrize(
    "kind", ["participant_left", "disconnected", "caller_end", "deadline", "terminal_response"]
)
def test_disconnect_hung_metrics_cannot_hold_shutdown_or_send_frozen_work_after_deadline(kind):
    async def scenario():
        events = []
        submitted = []
        entered = asyncio.Event()
        never = asyncio.Event()

        async def send(body):
            submitted.append(body)
            events.append("metrics")
            entered.set()
            try:
                await never.wait()
            finally:
                events.append("metrics-cancelled")

        call = disconnect_fixture(events, send)
        call.metrics.offer("stt_completion", 11, "en", 4)
        await entered.wait()
        # One already in flight plus a completed observation still queued at shutdown.
        call.metrics.offer("stt_completion", 12, "en", 4)
        await asyncio.wait_for(disconnect_callback(call, kind), 2)
        await asyncio.sleep(0)
        assert call.metrics.task.done()
        # Re-entrant disconnect/cleanup must not inherit cancellation from its sender.
        await call.metrics.flush()
        assert not call.metrics.pending
        assert events.index("audio-fenced") < events.index(terminal_route(kind))
        assert events.index("metrics-cancelled") < events.index("worker-cancelled")
        assert len(submitted) == 1
        call.metrics.offer("stt_completion", 99, "en", call.epoch)
        assert not call.metrics.pending
        assert "worker-cancelled" in events
        if call.invalidation_jobs:
            await asyncio.gather(*call.invalidation_jobs)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"session_id": "other"},
        {"generation": 2},
        {"sequence": 0},
        {"sequence": True},
        {"sequence": "1"},
        {"kind": "unknown"},
    ],
)
def test_caller_end_does_not_bypass_packet_authority(change):
    import json

    async def scenario():
        events = []
        call = disconnect_fixture(events, AsyncMock())
        packet = {"version": 1, "session_id": "sid", "generation": 1, "sequence": 1, "kind": "end"}
        packet.update(change)
        await call.data_received(None, json.dumps(packet), "caller")
        assert events == []
        assert not call.metrics.sealed and call.epoch == 4 and call.input_sequence == 0

    asyncio.run(scenario())


def test_caller_end_ignores_wrong_participant_and_oversized_packet():
    import json

    async def scenario():
        events = []
        call = disconnect_fixture(events, AsyncMock())
        packet = {"version": 1, "session_id": "sid", "generation": 1, "sequence": 1, "kind": "end"}
        await call.data_received(None, json.dumps(packet), "other")
        packet["padding"] = "x" * 4096
        await call.data_received(None, json.dumps(packet), "caller")
        assert events == []
        assert not call.metrics.sealed and call.epoch == 4 and call.input_sequence == 0

    asyncio.run(scenario())
