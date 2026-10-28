"""Explicit synthetic gate tooling; no patient audio is recorded."""

import argparse
import json

from voice_runtime.assets import download


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["download", "gate"])
    parser.add_argument("engine", choices=["whisper", "kokoro", "parler", "description"])
    args = parser.parse_args()
    try:
        if args.command == "download":
            print(json.dumps(download(args.engine)))
        else:
            gate(args.engine)
    except Exception as error:
        # Upstream exceptions can contain signed URLs or authorization headers.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1) from None


def gate(engine):
    import hashlib
    import time

    from voice_runtime.config import LOCAL, require_headroom
    from voice_runtime.speech.kokoro import KokoroAdapter
    from voice_runtime.speech.parler import ParlerAdapter

    if engine not in ("kokoro", "parler"):
        raise ValueError("gate requires synthesis engine; ASR uses generated PCM in-memory")
    adapter = KokoroAdapter() if engine == "kokoro" else ParlerAdapter()
    text = (
        "Hello. How can I help you today?" if engine == "kokoro" else "السلام علیکم۔ آپ کیسے ہیں؟"
    )
    from datetime import UTC, datetime

    import psutil

    baseline = {
        "available_bytes": psutil.virtual_memory().available,
        "swap_used_bytes": psutil.swap_memory().used,
        "at": datetime.now(UTC).isoformat(),
    }
    started = time.monotonic()
    try:
        require_headroom(engine, available_bytes=baseline["available_bytes"])
        pcm = adapter.synthesize(text, adapter.language)
        result = {
            "status": "passed",
            "engine": engine,
            "metrics": adapter.last_metrics,
            "samples": len(pcm.samples) // 2,
            "sample_rate": pcm.sample_rate,
            "sha256": hashlib.sha256(pcm.samples).hexdigest(),
        }
    except Exception as error:
        result = {
            "status": "failed",
            "engine": engine,
            "error_type": type(error).__name__,
            "elapsed_seconds": time.monotonic() - started,
            "reason": str(error) if type(error).__name__ == "JobFailure" else None,
            "peak_rss_bytes": getattr(error, "peak_rss_bytes", 0),
        }
    result["baseline"] = baseline
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / f"{engine}-gate.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
