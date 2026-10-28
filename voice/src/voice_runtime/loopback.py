"""Explicit real synthetic Kokoro -> pinned turbo CPU ASR; no patient audio."""

import argparse
import hashlib
import json
from datetime import UTC, datetime

import numpy as np
import psutil
import soxr

from voice_runtime.assets import ARTIFACTS
from voice_runtime.cache import PublicClipCache
from voice_runtime.config import LOCAL
from voice_runtime.speech.contracts import PCMChunk
from voice_runtime.speech.kokoro import KokoroAdapter
from voice_runtime.speech.parler import ParlerAdapter
from voice_runtime.speech.whisper import WhisperAdapter
from voice_runtime.worker import gate_fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("en", "ur"), default="en")
    language = parser.parse_args().language
    tts = KokoroAdapter() if language == "en" else ParlerAdapter()
    text = "Hello. How can I help you today?" if language == "en" else "السلام علیکم۔ آپ کیسے ہیں؟"
    baseline = {
        "available_bytes": psutil.virtual_memory().available,
        "swap_used_bytes": psutil.swap_memory().used,
        "at": datetime.now(UTC).isoformat(),
    }
    try:
        cache = PublicClipCache()
        pcm = cache.get(text, language)
        cached = pcm is not None
        if pcm is None:
            pcm = tts.synthesize(text, language)
            cached_samples = np.frombuffer(pcm.samples, dtype="<i2").astype(np.float32)
            cached_pcm = (
                soxr.resample(cached_samples, pcm.sample_rate, 24000)
                .clip(-32768, 32767)
                .astype("<i2")
            )
            cache.put(text, language, PCMChunk(cached_pcm.tobytes(), 24000))
        samples = np.frombuffer(pcm.samples, dtype="<i2").astype(np.float32)
        resampled = soxr.resample(samples, pcm.sample_rate, 16000).clip(-32768, 32767).astype("<i2")
        normalized = PCMChunk(resampled.tobytes(), 16000)
        asr = WhisperAdapter()
        recognized = asr.transcribe(normalized, language_mode="bilingual")
        result = {
            "status": "passed",
            "at": datetime.now(UTC).isoformat(),
            "fixture_sha256": hashlib.sha256(normalized.samples).hexdigest(),
            "tts": tts.last_metrics,
            "tts_cached": cached,
            "asr": asr.last_metrics,
            "recognized_text": recognized.text,
            "language": recognized.language,
            "uncertain": recognized.uncertain,
            "confidence": recognized.confidence,
        }
        if (
            recognized.language != language
            or recognized.uncertain
            or not any(
                word in recognized.text.lower()
                for word in (("help",) if language == "en" else ("کیسے", "علیکم"))
            )
        ):
            result["status"] = "failed"
            result["reason"] = "synthetic recognition mismatch"
    except Exception as error:
        result = {
            "status": "failed",
            "error_type": type(error).__name__,
            "reason": str(error) if type(error).__name__ == "JobFailure" else None,
            "peak_rss_bytes": getattr(error, "peak_rss_bytes", None),
            "elapsed_seconds": getattr(error, "elapsed_seconds", None),
            "tts": tts.last_metrics,
        }
    result["fingerprint"] = gate_fingerprint(language)
    result["baseline"] = baseline
    result["artifacts"] = {
        name: ARTIFACTS[name].revision
        for name in (("kokoro" if language == "en" else "parler"), "whisper")
    }
    result["asr_profile"] = {
        "device": "cpu",
        "compute_type": "int8",
        "threads": 2,
        "memory_bytes": 1024 * 1024**2,
        "system_available_floor_bytes": 256 * 1024**2,
    }
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / ("english-loopback.json" if language == "en" else "urdu-loopback.json")).write_text(
        json.dumps(result, indent=2)
    )
    print(json.dumps(result))
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
