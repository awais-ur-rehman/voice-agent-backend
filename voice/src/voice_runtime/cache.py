"""Allowlisted public phrases only; dynamic patient speech never persists here."""

import hashlib
import json
from pathlib import Path

from voice_runtime.assets import ARTIFACTS
from voice_runtime.config import FROZEN_VOICES, LOCAL
from voice_runtime.speech.contracts import PCMChunk

PUBLIC = {
    "en": frozenset({"Hello. How can I help you today?", "One moment, please."}),
    "ur": frozenset({"السلام علیکم۔ آپ کیسے ہیں؟", "براہ کرم انتظار کریں۔"}),
}


class PublicClipCache:
    def __init__(self, directory: Path = LOCAL / "public-clips"):
        self.directory = directory

    def key(self, text, language):
        if text not in PUBLIC.get(language, frozenset()):
            raise ValueError("only exact public phrases may be cached")
        engine = "kokoro" if language == "en" else "parler"
        config = {
            "artifact": ARTIFACTS[engine].revision,
            "text": text,
            "lang": language,
            "voice_config": FROZEN_VOICES[language],
            "seed": 42,
            "speed": 1,
            "sample_rate": 24000,
            "encoding": "s16le",
            "channels": 1,
        }
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def put(self, text, language, pcm):
        key = self.key(text, language)
        if pcm.sample_rate != 24000:
            raise ValueError("public cache requires transport rate 24000Hz")
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / (key + ".tmp")
        temporary.write_bytes(pcm.samples)
        temporary.replace(self.directory / (key + ".pcm"))

    def get(self, text, language):
        path = self.directory / (self.key(text, language) + ".pcm")
        return PCMChunk(path.read_bytes(), 24000) if path.exists() else None


def prepare_public_cache(capabilities):
    """Called before readiness; synthesize only exact static phrases, never caller data."""
    import numpy as np
    import soxr

    from voice_runtime.speech.kokoro import KokoroAdapter
    from voice_runtime.speech.parler import ParlerAdapter

    cache = PublicClipCache()
    for language, enabled in capabilities.items():
        if not enabled:
            continue
        adapter = KokoroAdapter() if language == "en" else ParlerAdapter()
        for text in sorted(PUBLIC[language]):
            if cache.get(text, language) is not None:
                continue
            pcm = adapter.synthesize(text, language)
            samples = np.frombuffer(pcm.samples, dtype="<i2").astype(np.float32)
            output = (
                soxr.resample(samples, pcm.sample_rate, 24000).clip(-32768, 32767).astype("<i2")
            )
            cache.put(text, language, PCMChunk(output.tobytes(), 24000))
