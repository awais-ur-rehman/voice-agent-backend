"""Real Pipecat/LiveKit caller-track pipeline with native Silero CPU VAD.

Heavy ASR/TTS children never execute on the VAD/control event loop. There is no
browser speech substitution and no synthetic transcript shortcut in this path.
"""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState
from pipecat.frames.frames import InterruptionFrame, OutputAudioRawFrame, UserAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
from pipecat.workers.runner import WorkerRunner

from voice_runtime.api import WorkerAPI
from voice_runtime.cache import PUBLIC, PublicClipCache
from voice_runtime.delivery import DeliveryLedger
from voice_runtime.session import ActivityTimers
from voice_runtime.speech.contracts import PCMChunk
from voice_runtime.speech.kokoro import KokoroAdapter
from voice_runtime.speech.parler import ParlerAdapter
from voice_runtime.speech.whisper import WhisperAdapter


class CallerProcessor(FrameProcessor):
    def __init__(self, call):
        super().__init__()
        self.call = call
        self.vad = SileroVADAnalyzer(
            sample_rate=16000, params=VADParams(confidence=0.65, start_secs=0.15, stop_secs=0.6)
        )
        self.vad.set_sample_rate(16000)
        self.buffer = bytearray()
        self.speaking = False
        self.started = None
        self.preroll = b""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if not isinstance(frame, UserAudioRawFrame):
            await self.push_frame(frame, direction)
            return
        # Filter identity before VAD so other participants and bot audio cannot interrupt.
        if frame.user_id != self.call.caller or self.call.timers.muted:
            return
        if frame.sample_rate != 16000 or frame.num_channels != 1 or len(frame.audio) % 2:
            await self.call.fail("invalid_audio_format")
            return
        state = await self.vad.analyze_audio(frame.audio)
        self.preroll = (self.preroll + frame.audio)[-8000:]
        if state == VADState.STARTING and self.started is None:
            self.started = datetime.now(UTC) - timedelta(seconds=len(self.preroll) / 32000)
        if state == VADState.SPEAKING and not self.speaking:
            self.started = self.started or (
                datetime.now(UTC) - timedelta(seconds=len(self.preroll) / 32000)
            )
            self.speaking = True
            self.buffer = bytearray(self.preroll)
            await self.call.interrupt()
        if self.speaking:
            self.call.timers.activity(time.monotonic())
            self.buffer.extend(frame.audio)
            if len(self.buffer) > 16000 * 2 * 30:
                self.buffer.clear()
                self.speaking = False
                self.started = None
                await self.call.fail("utterance_too_long")
                return
        if state == VADState.QUIET:
            if self.speaking:
                pcm = PCMChunk(bytes(self.buffer), 16000)
                self.buffer.clear()
                self.speaking = False
                self.call.spawn_turn(pcm, self.started)
            self.started = None


class NativeCall:
    def __init__(self, base_url, grant):
        self.grant = grant
        self.voice_profiles = grant.get("voice_profiles", {})
        self.greetings = grant.get("greetings", {})
        self.caller = grant["caller_participant"]
        self.api = WorkerAPI(base_url, grant)
        self.session_id = str(grant["session_id"])
        self.generation = grant["generation"]
        self.language_mode = grant["language_mode"]
        self.epoch = 0
        self.sequence = 0
        self.input_sequence = 0
        self.ledger = None
        self.critical_revision = None
        self.cache = PublicClipCache()
        self.asr = WhisperAdapter()
        self.english = KokoroAdapter()
        self.urdu = ParlerAdapter()
        self.pending = None
        self.ending = False
        self.invalidation = None
        self.invalidation_jobs = set()
        self.current_output = None
        self.resume_output = None
        self.control_lock = asyncio.Lock()
        self.timers = ActivityTimers(started=time.monotonic())
        media = grant["media"]
        self.transport = LiveKitTransport(
            media["url"],
            media["token"],
            media["room"],
            params=LiveKitParams(
                audio_in_enabled=True,
                audio_in_user_tracks=True,
                audio_in_sample_rate=16000,
                audio_in_channels=1,
                audio_in_passthrough=True,
                audio_out_enabled=True,
                audio_out_sample_rate=24000,
                audio_out_channels=1,
                audio_out_queue_size_ms=200,
                video_in_enabled=False,
                video_out_enabled=False,
            ),
        )
        self.processor = CallerProcessor(self)
        self.worker = PipelineWorker(
            Pipeline([self.transport.input(), self.processor, self.transport.output()]),
            enable_rtvi=False,
            enable_turn_tracking=False,
            idle_timeout_secs=None,
            params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
        )
        self.transport.event_handler("on_connected")(self.connected)
        self.transport.event_handler("on_participant_connected")(self.caller_joined)
        self.transport.event_handler("on_data_received")(self.data_received)
        self.transport.event_handler("on_participant_disconnected")(self.participant_left)
        self.transport.event_handler("on_disconnected")(self.disconnected)

    async def message(self, kind, **fields):
        self.sequence += 1
        payload = {
            "version": 1,
            "session_id": self.session_id,
            "generation": self.generation,
            "sequence": self.sequence,
            "kind": kind,
            **fields,
        }
        # Pinned Pipecat transport exposes no data-topic parameter; use its owned
        # native room for the versioned reliable channel, preserving sender identity.
        await self.transport._client.room.local_participant.publish_data(
            json.dumps(payload, ensure_ascii=False).encode(),
            reliable=True,
            topic="voice.v1",
            destination_identities=[self.caller],
        )

    async def connected(self, transport):
        await self.api.request("")
        await self.message("state", state="listening")

    async def caller_joined(self, transport, participant):
        if participant != self.caller:
            return
        language = "ur" if self.language_mode == "ur" else "en"
        greeting = {"en": "Hello. How can I help you today?", "ur": "السلام علیکم۔ آپ کیسے ہیں؟"}[
            language
        ]
        greeting = getattr(self, "greetings", {}).get(language, greeting)
        self.pending = asyncio.create_task(self.speak(greeting, language, None, self.epoch))

    async def status_filler(self, language, epoch, done, *, delay=0.8):
        if done.is_set():
            return
        try:
            await asyncio.wait_for(done.wait(), timeout=delay)
            return
        except TimeoutError:
            pass
        text = {"en": "One moment, please.", "ur": "براہ کرم انتظار کریں۔"}[language]
        if (
            not done.is_set()
            and self.cache.get(text, language, getattr(self, "voice_profiles", {}).get(language))
            is not None
        ):
            self.filler_started = True
            await self.speak(text, language, None, epoch, status_only=True)

    async def plan(self, result, started, epoch):
        done = asyncio.Event()
        self.filler_started = False
        filler = asyncio.create_task(self.status_filler(result.language, epoch, done))
        try:
            return await self.api.turn(
                result.text,
                started_at=started,
                language=result.language,
                confidence=result.confidence,
                uncertain=result.uncertain,
            )
        finally:
            done.set()
            filler.cancel()
            await asyncio.gather(filler, return_exceptions=True)
            if self.filler_started:
                await self.worker.queue_frame(InterruptionFrame())
                await self.message("invalidate")
                self.current_output = None
                self.ledger = None

    async def fail(self, code):
        await self.message("state", state="error", code=code)

    async def interrupt(self):
        self.epoch += 1
        in_flight = self.pending is not None and not self.pending.done()
        incomplete = self.ledger is not None and not self.ledger.complete
        await asyncio.to_thread(self.asr.cancel)
        await asyncio.to_thread(self.english.cancel)
        await asyncio.to_thread(self.urdu.cancel)
        await self.worker.queue_frame(InterruptionFrame())
        # A fresh affirmative after fully acknowledged playback must preserve
        # the completed SQL readback. Only in-flight/incomplete delivery is invalidated.
        if in_flight or incomplete:
            output = getattr(self, "current_output", None)
            self.resume_output = None
            if output and output["readback_revision"] is None and not output.get("status_only"):
                index = output["index"]
                segment_id = output["ids"].get(index)
                segment = self.ledger.segments.get(segment_id) if self.ledger else None
                if segment and segment["played"] == segment["samples"]:
                    index += 1
                remaining = " ".join(output["segments"][index:])
                if remaining:
                    self.resume_output = (remaining, output["language"])
            self.current_output = None
            if self.ledger:
                self.ledger.invalidate()
            await self.message("invalidate")
            # SQL invalidation must overtake a slow planner without blocking VAD.
            self.invalidation = asyncio.create_task(self.api.interrupt())
            self.invalidation_jobs.add(self.invalidation)

            def consumed(job):
                self.invalidation_jobs.discard(job)
                if not job.cancelled():
                    job.exception()

            self.invalidation.add_done_callback(consumed)
        await self.message("state", state="listening")

    def spawn_turn(self, pcm, started):
        # At most one owned task: interrupted work is fenced before replacement.
        if self.pending and not self.pending.done():
            self.pending.cancel()
        self.pending = asyncio.create_task(self.turn(pcm, started, self.epoch))

    async def turn(self, pcm, started, epoch):
        try:
            await self.message("state", state="thinking")
            result = await asyncio.to_thread(
                self.asr.transcribe, pcm, self.language_mode, (), self.asr.supervisor.generation
            )
            if epoch != self.epoch:
                return
            if result.uncertain or not result.text:
                await self.fail("asr_uncertain")
                return
            if self.invalidation is not None:
                await self.invalidation
            resume = getattr(self, "resume_output", None)
            self.resume_output = None
            if resume and result.text.strip().casefold().strip(".؟!،") in {
                "okay",
                "ok",
                "yes",
                "جی",
                "ہاں",
                "ji",
                "hmm",
            }:
                await self.speak(resume[0], resume[1], None, epoch)
                return
            async with self.control_lock:
                await self.api.request("")
                response = await self.plan(result, started, epoch)
            if epoch != self.epoch:
                return
            await self.complete_response(response, result.language, epoch)
        except asyncio.CancelledError:
            raise
        except Exception:
            if epoch == self.epoch:
                await self.fail("voice_pipeline_failed")

    async def complete_response(self, response, default_language, epoch):
        state = response["state"]
        terminal = bool(state.get("ended") or state.get("stage") == "ended")
        self.ending = terminal
        try:
            language = state.get("lang", default_language)
            say = response["result"].get("say", {}).get(language)
            if not say:
                await self.fail("missing_code_speech")
                return
            readback = state.get("readback")
            revision = (
                readback["revision"]
                if readback and say == readback.get("say", {}).get(language)
                else None
            )
            await self.speak(say, language, revision, epoch)
        finally:
            if terminal:
                try:
                    await self.api.request("/end", {})
                finally:
                    await self.worker.cancel()

    async def speak(self, text, language, readback_revision, epoch, *, status_only=False):
        adapter = self.english if language == "en" else self.urdu
        self.ledger = DeliveryLedger(
            self.session_id, generation=self.generation, participant=self.caller
        )
        self.critical_revision = readback_revision
        if not status_only:
            await self.message("caption", text=text[:2000], lang=language)
        # Small completed model segments; this is deliberately not labelled native streaming.
        words = text.split()
        segments = []
        segment = ""
        for word in words:
            if len(segment) + len(word) + 1 > 220:
                segments.append(segment)
                segment = ""
            segment = (segment + " " + word).strip()
        if segment:
            segments.append(segment)
        self.current_output = {
            "segments": segments,
            "index": 0,
            "ids": {},
            "language": language,
            "readback_revision": readback_revision,
            "status_only": status_only,
        }
        for index, sentence in enumerate(segments):
            self.current_output["index"] = index
            is_public = sentence in PUBLIC.get(language, frozenset()) and readback_revision is None
            pcm = (
                self.cache.get(
                    sentence, language, getattr(self, "voice_profiles", {}).get(language)
                )
                if is_public
                else None
            )
            if pcm is None:
                pcm = await asyncio.to_thread(
                    adapter.synthesize,
                    sentence,
                    language,
                    getattr(self, "voice_profiles", {}).get(language),
                    adapter.supervisor.generation,
                )
            if epoch != self.epoch:
                return
            import numpy as np
            import soxr

            samples = np.frombuffer(pcm.samples, dtype="<i2").astype(np.float32)
            output = (
                soxr.resample(samples, pcm.sample_rate, 24000)
                .clip(-32768, 32767)
                .astype("<i2")
                .tobytes()
            )
            if is_public:
                self.cache.put(
                    sentence,
                    language,
                    PCMChunk(output, 24000),
                    getattr(self, "voice_profiles", {}).get(language),
                )
            identity = uuid.uuid4().hex
            count = len(output) // 2
            self.current_output["ids"][index] = identity
            self.ledger.publish(identity, count, 24000)
            if readback_revision is not None:
                await self.api.request(
                    "/publish",
                    {
                        "readback_revision": readback_revision,
                        "segment_id": identity,
                        "published_samples": count,
                        "sample_rate": 24000,
                    },
                )
            await self.message(
                "segment",
                segment_id=identity,
                sample_rate=24000,
                total_samples=count,
                critical=readback_revision is not None,
            )
            await self.message("state", state="speaking")
            for offset in range(0, len(output), 960):
                if epoch != self.epoch:
                    return
                await self.worker.queue_frame(
                    OutputAudioRawFrame(
                        audio=output[offset : offset + 960], sample_rate=24000, num_channels=1
                    )
                )
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
            if epoch != self.epoch:
                return
            await self.message("segment_end", segment_id=identity)
        if readback_revision is not None:
            await self.api.request("/finish", {"readback_revision": readback_revision})
        self.current_output = None
        if not self.ending:
            await self.message("state", state="thinking" if status_only else "listening")
            if not status_only:
                self.timers.activity(time.monotonic())

    async def data_received(self, transport, data, participant):
        if participant != self.caller or len(data) > 4096:
            return
        try:
            message = json.loads(data)
            if (
                message.get("version") != 1
                or message.get("session_id") != self.session_id
                or message.get("generation") != self.generation
                or type(message.get("sequence")) is not int
                or message["sequence"] <= self.input_sequence
            ):
                return
            if "played_samples" in message:
                if self.ledger is None:
                    return
                self.ledger.ack(message, participant=participant)
                if self.critical_revision is not None:
                    await self.api.request("/ack", {"ack": message, "participant": participant})
            elif message.get("kind") in ("mute", "unmute"):
                self.timers.muted = message["kind"] == "mute"
                self.timers.activity(time.monotonic())
                if self.timers.muted:
                    await self.interrupt()
            elif message.get("kind") == "end":
                await self.api.request("/end", {})
                await self.worker.cancel()
            else:
                return
            self.input_sequence = message["sequence"]
        except (ValueError, TypeError, KeyError):
            return

    async def participant_left(self, transport, participant):
        if participant != self.caller:
            return
        await self.interrupt()
        await self.api.request("/disconnect", {})
        await self.worker.cancel()

    async def disconnected(self, transport):
        self.epoch += 1
        for adapter in (self.asr, self.english, self.urdu):
            await asyncio.to_thread(adapter.cancel)
        if self.ledger:
            self.ledger.invalidate()
        try:
            await self.api.request("/disconnect", {})
        except Exception:
            # If the server is unreachable its lease fence still rejects late work.
            pass
        finally:
            await self.worker.cancel()

    async def heartbeat(self):
        last_heartbeat = time.monotonic()
        while True:
            await asyncio.sleep(0.25)
            try:
                if self.ending:
                    continue
                now = time.monotonic()
                if now - last_heartbeat >= 4:
                    lease = await self.api.request("/heartbeat", {})
                    if lease["generation"] != self.generation:
                        raise ValueError("worker fenced")
                    last_heartbeat = now
                busy = self.pending is not None and not self.pending.done()
                action = (
                    ("end" if now - self.timers.started >= 600 else None)
                    if busy
                    else self.timers.tick(now)
                )
                if action == "end" and now - self.timers.started >= 600:
                    await self.api.request("/end", {})
                    await self.worker.cancel()
                    return
                if action in ("reprompt", "end") and not busy:
                    async with self.control_lock:
                        response = await self.api.event("silence")
                    language = "ur" if self.language_mode == "ur" else "en"
                    text = response.get("result", {}).get("say", {}).get(language)
                    if text:
                        self.pending = asyncio.create_task(
                            self.complete_response(response, language, self.epoch)
                        )
            except Exception:
                if self.ending:
                    continue
                self.epoch += 1
                for adapter in (self.asr, self.english, self.urdu):
                    await asyncio.to_thread(adapter.cancel)
                if self.ledger:
                    self.ledger.invalidate()
                await self.worker.queue_frame(InterruptionFrame())
                await self.worker.cancel()
                return

    async def run(self):
        heartbeat = asyncio.create_task(self.heartbeat())
        try:
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(self.worker)
            await runner.run()
        finally:
            heartbeat.cancel()
            for invalidation in self.invalidation_jobs:
                invalidation.cancel()
            if self.invalidation_jobs:
                await asyncio.gather(*self.invalidation_jobs, return_exceptions=True)
            if self.pending:
                self.pending.cancel()
            for adapter in (self.asr, self.english, self.urdu):
                await asyncio.to_thread(adapter.cancel)
            if self.ending:
                try:
                    await self.api.request("/end", {})
                except Exception:
                    pass
            await self.api.close()
