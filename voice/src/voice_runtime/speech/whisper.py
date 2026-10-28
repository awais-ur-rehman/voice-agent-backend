"""Pinned turbo CPU int8 ASR with en/ur-only detection and honest metadata."""

import base64
import json

from voice_runtime.assets import snapshot
from voice_runtime.config import require_headroom
from voice_runtime.speech.adapter import SpeechAdapter
from voice_runtime.speech.contracts import ASRResult, PCMChunk


class WhisperAdapter(SpeechAdapter):
    engine = "whisper"
    streaming = "utterance"

    def transcribe(self, pcm: PCMChunk, language_mode="bilingual", public_vocab=(), generation=0):
        if language_mode not in ("en", "ur", "bilingual"):
            raise ValueError("unsupported language")
        if pcm.sample_rate != 16000:
            raise ValueError("ASR requires 16000Hz PCM")
        if (
            not isinstance(public_vocab, (tuple, list))
            or len(public_vocab) > 50
            or any(not isinstance(v, str) or len(v) > 80 for v in public_vocab)
        ):
            raise ValueError("invalid public vocabulary")
        import psutil

        require_headroom("whisper", available_bytes=psutil.virtual_memory().available)
        snapshot("whisper", self.settings)
        self.supervisor.memory_bytes = min(self.supervisor.memory_bytes, 1024 * 1024**2)
        request = {
            "engine": "whisper",
            "pcm": base64.b64encode(pcm.samples).decode(),
            "language_mode": language_mode,
            "public_vocab": public_vocab,
        }
        result = self.supervisor.run(
            [str(self.settings.inference_python), "-m", "voice_runtime.speech.child"],
            generation=generation,
            timeout=self.settings.timeout_seconds,
            input_bytes=json.dumps(request).encode(),
            env=self.settings.environment(),
        )
        data = json.loads(result.stdout)
        self.last_metrics = {
            **data.pop("metrics"),
            "elapsed_seconds": result.elapsed_seconds,
            "peak_rss_bytes": result.peak_rss_bytes,
        }
        return ASRResult(**data)
