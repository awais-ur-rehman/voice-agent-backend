"""Opt-in real local RTC PCM transport smoke. This does not prove ASR or booking."""

import asyncio
import json
import math
import struct
import uuid
from datetime import UTC, datetime

from dotenv import dotenv_values
from livekit import api, rtc
from pipecat.frames.frames import OutputAudioRawFrame, UserAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
from pipecat.workers.runner import WorkerRunner

from voice_runtime.config import BACKEND, LOCAL


async def main():
    from loguru import logger

    logger.remove()
    env = dotenv_values(BACKEND / ".env.api")
    url = env.get("SCHEDULER_LIVEKIT_URL", "ws://127.0.0.1:7880")
    key = env["SCHEDULER_LIVEKIT_API_KEY"]
    secret = env["SCHEDULER_LIVEKIT_API_SECRET"]
    room_name = "voice-smoke-" + uuid.uuid4().hex
    caller_identity = "synthetic-caller"

    def token(identity):
        return (
            api.AccessToken(key, secret)
            .with_identity(identity)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )

    server = api.LiveKitAPI(url=url, api_key=key, api_secret=secret)
    caller = rtc.Room()
    observed = {"input_samples": 0, "output_samples": 0, "output_nonzero": False}
    got_audio = asyncio.Event()

    class EchoCaller(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, UserAudioRawFrame):
                if frame.user_id != caller_identity:
                    return
                observed["input_samples"] += len(frame.audio) // 2
                await self.push_frame(
                    OutputAudioRawFrame(
                        audio=frame.audio,
                        sample_rate=frame.sample_rate,
                        num_channels=frame.num_channels,
                    )
                )
            else:
                await self.push_frame(frame, direction)

    transport = LiveKitTransport(
        url,
        token("synthetic-worker"),
        room_name,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_in_user_tracks=True,
            audio_in_sample_rate=16000,
            audio_in_passthrough=True,
            audio_out_enabled=True,
            audio_out_sample_rate=24000,
            audio_out_queue_size_ms=200,
        ),
    )
    worker = PipelineWorker(
        Pipeline([transport.input(), EchoCaller(), transport.output()]),
        enable_rtvi=False,
        enable_turn_tracking=False,
        idle_timeout_secs=None,
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
    )
    ready = asyncio.Event()

    @transport.event_handler("on_connected")
    async def connected(transport):
        ready.set()

    streams = []
    readers = []

    async def consume(track):
        stream = rtc.AudioStream(track, sample_rate=24000, num_channels=1)
        streams.append(stream)
        async for event in stream:
            raw = bytes(event.frame.data)
            observed["output_samples"] += len(raw) // 2
            observed["output_nonzero"] |= any(raw)
            if observed["output_samples"] >= 24000 and observed["output_nonzero"]:
                got_audio.set()

    @caller.on("track_subscribed")
    def subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO and participant.identity == "synthetic-worker":
            readers.append(asyncio.create_task(consume(track)))

    run = None
    stage = "create_room"
    try:
        await server.room.create_room(api.CreateRoomRequest(name=room_name))
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        run = asyncio.create_task(runner.run())
        stage = "worker_connected"
        await asyncio.wait_for(ready.wait(), 15)
        stage = "caller_connected"
        await caller.connect(url, token(caller_identity))
        source = rtc.AudioSource(16000, 1, queue_size_ms=100)
        track = rtc.LocalAudioTrack.create_audio_track("synthetic-microphone", source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await caller.local_participant.publish_track(track, options)
        stage = "publish_frames"
        for chunk in range(100):
            data = b"".join(
                struct.pack(
                    "<h", int(6000 * math.sin(2 * math.pi * 440 * (chunk * 320 + i) / 16000))
                )
                for i in range(320)
            )
            await source.capture_frame(
                rtc.AudioFrame(
                    data=data, sample_rate=16000, num_channels=1, samples_per_channel=320
                )
            )
        stage = "receive_frames"
        await asyncio.wait_for(got_audio.wait(), 15)
        result = {
            "status": "passed",
            "at": datetime.now(UTC).isoformat(),
            "kind": "real LiveKit/Pipecat PCM transport only",
            "metrics": observed,
            "speech_acceptance": False,
        }
        LOCAL.mkdir(parents=True, exist_ok=True)
        (LOCAL / "rtc-transport-smoke.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
    except Exception as error:
        failure = {
            "status": "failed",
            "stage": stage,
            "error_type": type(error).__name__,
            "metrics": observed,
            "worker_done": run.done() if run else None,
            "speech_acceptance": False,
            "at": datetime.now(UTC).isoformat(),
        }
        LOCAL.mkdir(parents=True, exist_ok=True)
        (LOCAL / "rtc-transport-smoke.json").write_text(json.dumps(failure, indent=2))
        print(json.dumps(failure))
        raise
    finally:
        await worker.cancel()
        if run:
            await asyncio.gather(run, return_exceptions=True)
        for reader in readers:
            reader.cancel()
        for stream in streams:
            await stream.aclose()
        await caller.disconnect()
        await server.room.delete_room(api.DeleteRoomRequest(room=room_name))
        await server.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        raise SystemExit(type(error).__name__) from None
