"""Control boundaries only; these tests never load speech weights or open RTC."""

import asyncio

import pytest

from voice_runtime.delivery import DeliveryLedger
from voice_runtime.pipeline import NativeCall


class NoInference:
    def cancel(self):
        pass


class LocalWorker:
    def __init__(self):
        self.cancelled = False

    async def queue_frame(self, frame):
        pass

    async def cancel(self):
        self.cancelled = True


def call_fixture():
    call = NativeCall.__new__(NativeCall)
    call.epoch = 0
    call.pending = None
    call.asr = call.english = call.urdu = NoInference()
    call.worker = LocalWorker()
    call.ledger = DeliveryLedger("sid", generation=1, participant="caller")
    call.ledger.publish("s", 160, 16000)
    call.control_lock = asyncio.Lock()

    async def message(*args, **kwargs):
        pass

    call.message = message
    return call


def test_fresh_speech_preserves_fully_acknowledged_readback():
    async def scenario():
        call = call_fixture()
        call.ledger.ack(
            {
                "version": 1,
                "session_id": "sid",
                "generation": 1,
                "sequence": 1,
                "segment_id": "s",
                "played_samples": 160,
            },
            participant="caller",
        )
        # No API is installed: any invalidation-network branch would fail this test.
        await call.interrupt()
        assert call.epoch == 1
        assert call.ledger.complete

    asyncio.run(scenario())


def test_media_disconnect_fences_output_even_when_api_is_unreachable():
    class UnreachableAPI:
        async def request(self, *args):
            raise OSError("network unavailable")

    async def scenario():
        call = call_fixture()
        call.api = UnreachableAPI()
        await call.disconnected(None)
        assert call.epoch == 1
        assert call.ledger.invalidated
        assert call.worker.cancelled

    asyncio.run(scenario())


def test_barge_in_never_waits_for_the_planner_control_lock():
    async def scenario():
        from unittest.mock import AsyncMock

        call = call_fixture()
        call.invalidation_jobs = set()
        call.invalidation = None
        call.api = type("API", (), {"interrupt": AsyncMock(return_value={"revision": 1})})()
        await call.control_lock.acquire()
        try:
            await asyncio.wait_for(call.interrupt(), 0.1)
            await call.invalidation
            call.api.interrupt.assert_awaited_once()
        finally:
            call.control_lock.release()

    asyncio.run(scenario())


def test_utterance_start_is_a_conservative_preroll_bound(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pipecat.audio.vad.vad_analyzer import VADState
    from pipecat.frames.frames import UserAudioRawFrame

    import voice_runtime.pipeline as module

    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(module.FrameProcessor, "process_frame", AsyncMock())
    processor = module.CallerProcessor.__new__(module.CallerProcessor)
    processor.call = SimpleNamespace(caller="caller", timers=SimpleNamespace(muted=False))
    processor.vad = SimpleNamespace(analyze_audio=AsyncMock(return_value=VADState.STARTING))
    processor.buffer = bytearray()
    processor.preroll = b"\0" * 8000
    processor.speaking = False
    processor.started = None
    asyncio.run(
        processor.process_frame(
            UserAudioRawFrame(
                audio=b"\0" * 320, sample_rate=16000, num_channels=1, user_id="caller"
            ),
            None,
        )
    )
    assert processor.started <= now - timedelta(seconds=0.25)


def test_noncritical_backchannel_resumes_code_speech_without_a_new_plan():
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from voice_runtime.speech.contracts import ASRResult, PCMChunk

    async def scenario():
        call = call_fixture()
        call.resume_output = ("The remaining clinic answer.", "en")
        call.invalidation = None
        call.api = SimpleNamespace(turn=AsyncMock(), request=AsyncMock())
        call.asr = SimpleNamespace(
            supervisor=SimpleNamespace(generation=0),
            transcribe=lambda *args: ASRResult("okay", "en", None, False),
        )
        call.language_mode = "en"
        call.speak = AsyncMock()
        call.fail = AsyncMock()
        await call.turn(PCMChunk(b"\0\0" * 160, 16000), datetime.now(UTC), 0)
        call.api.turn.assert_not_awaited()
        call.speak.assert_awaited_once_with("The remaining clinic answer.", "en", None, 0)

    asyncio.run(scenario())


def test_caller_join_uses_cached_public_greeting_as_owned_output():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    async def scenario():
        call = NativeCall.__new__(NativeCall)
        call.caller = "caller"
        call.language_mode = "en"
        call.epoch = 0
        call.pending = None
        call.cache = SimpleNamespace(get=lambda *_: object())
        call.speak = AsyncMock()
        await call.caller_joined(None, "caller")
        await call.pending
        call.speak.assert_awaited_once_with("Hello. How can I help you today?", "en", None, 0)

    asyncio.run(scenario())


def test_status_filler_never_starts_a_model_or_speaks_after_plan_finishes():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    async def scenario():
        call = NativeCall.__new__(NativeCall)
        call.cache = SimpleNamespace(get=lambda *_: None)
        call.speak = AsyncMock()
        done = asyncio.Event()
        await call.status_filler("en", 0, done, delay=0)
        call.speak.assert_not_awaited()
        call.cache.get = lambda *_: object()
        done.set()
        await call.status_filler("en", 0, done, delay=0)
        call.speak.assert_not_awaited()

    asyncio.run(scenario())


def test_interrupted_filler_is_never_resumed_as_a_task_answer():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    async def scenario():
        call = call_fixture()
        call.invalidation_jobs = set()
        call.invalidation = None
        call.api = SimpleNamespace(interrupt=AsyncMock())
        call.current_output = {
            "segments": ["One moment, please."],
            "index": 0,
            "ids": {},
            "language": "en",
            "readback_revision": None,
            "status_only": True,
        }
        await call.interrupt()
        await call.invalidation
        assert call.resume_output is None

    asyncio.run(scenario())


def test_selected_profile_reaches_native_child_and_cache(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    from voice_runtime.cache import PublicClipCache
    from voice_runtime.config import BACKEND
    from voice_runtime.speech.kokoro import KokoroAdapter
    from voice_runtime.worker import gate_fingerprint

    records = json.loads((BACKEND / "voice/catalog.json").read_text())
    selected = next(row for row in records if row["id"] == "kokoro-am-adam-v1")
    requests = []

    class Supervisor:
        def run(self, *args, **kwargs):
            requests.append(json.loads(kwargs["input_bytes"]))
            return SimpleNamespace(
                stdout=json.dumps({"pcm": "AQACAA==", "sample_rate": 24000, "metrics": {}}),
                elapsed_seconds=0,
                peak_rss_bytes=0,
            )

    monkeypatch.setattr(
        "voice_runtime.speech.adapter.require_headroom", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("voice_runtime.speech.adapter.snapshot", lambda *args: tmp_path)
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices" / "am_adam.pt").write_bytes(b"synthetic-test-placeholder")
    # This wire/config unit test assumes adoption; real unapproved files are rejected in test_assets.
    approved_file = tmp_path / "voices" / "am_adam.pt"
    monkeypatch.setattr(
        "voice_runtime.speech.adapter.require_manifest_file", lambda *args: approved_file
    )
    monkeypatch.setattr("voice_runtime.worker.require_manifest_file", lambda *args: approved_file)
    KokoroAdapter(supervisor=Supervisor()).synthesize("hello", "en", selected)
    assert requests[0]["voice_config"] == selected
    cache = PublicClipCache(tmp_path)
    phrase = "One moment, please."
    assert cache.key(phrase, "en", selected) != cache.key(phrase, "en")
    assert gate_fingerprint("en", selected) != gate_fingerprint("en")
    tampered = selected | {"config": selected["config"] | {"name": "unknown"}}
    with pytest.raises(ValueError):
        KokoroAdapter(supervisor=Supervisor()).synthesize("hello", "en", tampered)


def test_missing_selected_tensor_never_starts_inference(monkeypatch, tmp_path):
    import json

    from voice_runtime.config import BACKEND
    from voice_runtime.speech.kokoro import KokoroAdapter

    selected = next(
        row
        for row in json.loads((BACKEND / "voice/catalog.json").read_text())
        if row["id"] == "kokoro-am-adam-v1"
    )

    class Supervisor:
        def run(self, *args, **kwargs):
            raise AssertionError("Missing profile must fail before inference")

    monkeypatch.setattr(
        "voice_runtime.speech.adapter.require_headroom", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("voice_runtime.speech.adapter.snapshot", lambda *args: tmp_path)
    with pytest.raises((FileNotFoundError, ValueError)):
        KokoroAdapter(supervisor=Supervisor()).synthesize("hello", "en", selected)
