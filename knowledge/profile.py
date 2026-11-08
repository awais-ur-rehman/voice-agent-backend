"""Immutable, deterministic metadata. No runtime/model dependencies."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Artifact:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class EmbeddingProfile:
    fingerprint: str
    repository: str
    revision: str
    artifacts: tuple[Artifact, ...]
    dimensions: int = 1024
    max_tokens: int = 512


def load_profile() -> EmbeddingProfile:
    data = json.loads(Path(__file__).with_name("profile.json").read_text())
    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return EmbeddingProfile(
        digest,
        data["repository"],
        data["revision"],
        tuple(
            Artifact(name, item["bytes"], item["sha256"]) for name, item in data["files"].items()
        ),
    )


PROFILE = load_profile()
