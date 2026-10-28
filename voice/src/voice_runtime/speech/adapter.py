"""CPU subprocess adapter. Patient audio exists only in in-memory pipe buffers."""

import base64
import json

from voice_runtime.assets import require_manifest_file, snapshot
from voice_runtime.config import Settings, require_headroom
from voice_runtime.speech.contracts import PCMChunk
from voice_runtime.speech.process import Supervisor


class SpeechAdapter:
    engine: str
    language: str
    sample_rate: int
    streaming: str

    def __init__(self, supervisor=None, settings=None):
        self.settings = settings or Settings()
        self.supervisor = supervisor or Supervisor(self.settings.memory_bytes)
        self.last_metrics = None

    def cancel(self):
        self.supervisor.cancel()

    def synthesize(self, text: str, language: str, voice_config=None, generation=0):
        if language != self.language:
            raise ValueError("unsupported language")
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ValueError("synthesis requires 1-300 characters")
        from voice_runtime.profiles import selected_config

        config = selected_config(language, voice_config)
        import psutil

        require_headroom(self.engine, available_bytes=psutil.virtual_memory().available)
        snapshot(self.engine, self.settings)
        if voice_config and self.engine == "kokoro":
            require_manifest_file("kokoro", "voices/" + config["name"] + ".pt", self.settings)
        request = {"engine": self.engine, "text": text, "voice_config": voice_config or {}}
        result = self.supervisor.run(
            [str(self.settings.inference_python), "-m", "voice_runtime.speech.child"],
            generation=generation,
            timeout=self.settings.timeout_seconds,
            input_bytes=json.dumps(request).encode(),
            env=self.settings.environment(),
        )
        data = json.loads(result.stdout)
        self.last_metrics = {
            **data["metrics"],
            "elapsed_seconds": result.elapsed_seconds,
            "peak_rss_bytes": result.peak_rss_bytes,
        }
        return PCMChunk(
            base64.b64decode(data["pcm"], validate=True), data["sample_rate"], 1, (0, len(text))
        )
