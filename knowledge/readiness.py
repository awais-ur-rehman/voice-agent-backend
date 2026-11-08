"""Purpose-separated authenticated worker observations over the private blob volume.

Reading health never loads a tokenizer/model or performs inference. A fresh heartbeat
proves observed worker DB connectivity, tokenizer integrity and authenticated model
readiness; configuration by itself proves none of those.
"""

import hashlib
import hmac
import json
import os
import stat
import tempfile
import time
from pathlib import Path

from knowledge.profile import PROFILE

NAME = ".worker-readiness.json"
DOMAIN = b"knowledge-readiness-v1\0"
MAX_AGE = 30


def _canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def publish_observation(
    root: Path, secret: str, *, worker: bool, embedding: bool, artifacts: bool, observed_at: float
):
    if len(secret) < 32:
        return
    if any(path.is_symlink() for path in [root, *root.parents]):
        raise OSError("unsafe_readiness_root")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    observation = {
        "version": 1,
        "profile": PROFILE.fingerprint,
        "observed_at": observed_at,
        "worker": worker,
        "embedding": embedding,
        "artifacts": artifacts,
    }
    data = _canonical(
        {
            "observation": observation,
            "signature": hmac.new(
                secret.encode(), DOMAIN + _canonical(observation), hashlib.sha256
            ).hexdigest(),
        }
    )
    fd, temporary = tempfile.mkstemp(prefix=".readiness-", dir=root)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / NAME)
    finally:
        Path(temporary).unlink(missing_ok=True)


def processing_status(root: Path, secret: str, *, enabled: bool, now=None):
    if not enabled:
        return "disabled"
    if len(secret) < 32:
        return "processing_unavailable"
    try:
        if any(path.is_symlink() for path in [root, *root.parents]):
            return "processing_unavailable"
        fd = os.open(root / NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_mode & 0o077:
                return "processing_unavailable"
            envelope = json.loads(stream.read(4097))
        if set(envelope) != {"observation", "signature"}:
            return "processing_unavailable"
        data = envelope["observation"]
        expected = hmac.new(secret.encode(), DOMAIN + _canonical(data), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, envelope["signature"]):
            return "processing_unavailable"
        if (
            set(data) != {"version", "profile", "observed_at", "worker", "embedding", "artifacts"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or data["profile"] != PROFILE.fingerprint
            or type(data["observed_at"]) not in {int, float}
            or any(type(data[key]) is not bool for key in ["worker", "embedding", "artifacts"])
        ):
            return "processing_unavailable"
        age = (time.time() if now is None else now) - data["observed_at"]
        if not 0 <= age <= MAX_AGE:
            return "worker_stale"
        if not data["worker"]:
            return "processing_unavailable"
        if not data["embedding"]:
            return "embedding_unavailable"
        if not data["artifacts"]:
            return "artifacts_unavailable"
        return None
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return "processing_unavailable"
