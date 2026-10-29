"""Explicit pinned bootstrap/preflight. Never called by API/browser reads."""

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path

from knowledge.embedding import EmbeddingUnavailable
from knowledge.profile import PROFILE


def bootstrap(root: Path):
    from embedding_service.model import verify_assets

    root.parent.mkdir(parents=True, exist_ok=True)
    required = sum(a.size for a in PROFILE.artifacts) + 4 * 1024**3
    if shutil.disk_usage(root.parent).free < required:
        raise EmbeddingUnavailable("disk_reserve")
    if root.exists():
        verify_assets(root)
        return
    with tempfile.TemporaryDirectory(prefix="bge-stage-", dir=root.parent) as temporary:
        stage = Path(temporary) / "assets"
        stage.mkdir()
        for artifact in PROFILE.artifacts:
            destination = stage / artifact.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            url = f"https://huggingface.co/{PROFILE.repository}/resolve/{PROFILE.revision}/{artifact.name}"
            digest = hashlib.sha256()
            size = 0
            deadline = time.monotonic() + 300
            with (
                urllib.request.urlopen(url, timeout=30) as response,
                destination.open("wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > artifact.size or time.monotonic() > deadline:
                        raise EmbeddingUnavailable("download_bound")
                    if shutil.disk_usage(stage).free < 4 * 1024**3:
                        raise EmbeddingUnavailable("disk_reserve")
                    digest.update(chunk)
                    output.write(chunk)
            if size != artifact.size or digest.hexdigest() != artifact.sha256:
                raise EmbeddingUnavailable("asset_integrity")
        verify_assets(stage)
        os.rename(stage, root)


def smoke(root: Path):
    from embedding_service.supervisor import Supervisor, available_memory

    available = available_memory()
    metrics = {
        "fingerprint": PROFILE.fingerprint,
        "available_mib": round(available / 1024**2, 2),
        "required_available_mib": 1792,
    }
    worker = Supervisor(root)
    started = time.monotonic()
    try:
        worker.start()
        metrics["startup_seconds"] = round(time.monotonic() - started, 3)
        started = time.monotonic()
        vector = worker.embed(["Clinic opening hours"], deadline=time.monotonic() + 10)[0]
        metrics.update(
            dimensions=len(vector),
            norm=math.sqrt(sum(x * x for x in vector)),
            inference_seconds=round(time.monotonic() - started, 3),
            status="passed",
        )
        return 0
    except EmbeddingUnavailable as exc:
        metrics.update(status="unavailable", reason=str(exc))
        return 1
    finally:
        metrics["peak_child_rss_mib"] = round(worker.peak_rss / 1024**2, 2)
        worker.close()
        print(json.dumps(metrics, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["bootstrap", "verify", "smoke"])
    parser.add_argument("--assets", type=Path, default=Path(".local/knowledge-model"))
    args = parser.parse_args()
    try:
        if args.action == "bootstrap":
            bootstrap(args.assets)
        elif args.action == "verify":
            from embedding_service.model import verify_assets

            verify_assets(args.assets)
        else:
            return smoke(args.assets)
        print(json.dumps({"status": "verified", "fingerprint": PROFILE.fingerprint}))
        return 0
    except Exception:
        print(json.dumps({"status": "unavailable", "reason": "asset_preparation_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
