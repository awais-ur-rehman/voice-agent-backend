"""Private worker configuration. No dotenv, owner credentials or model initialization."""

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx2

from knowledge.embedding import EmbeddingClient, EmbeddingUnavailable
from knowledge.profile import PROFILE


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    database_url: str = ""
    blob_root: Path = Path("/var/lib/knowledge")
    tokenizer: Path = Path("/tokenizer/tokenizer.json")
    embedding_url: str = "http://embedding:8001"
    embedding_secret: str = ""
    blob_secret: str = ""

    @classmethod
    def from_env(cls):
        return cls(
            enabled=os.getenv("KNOWLEDGE_WORKER_ENABLED", "false").lower() == "true",
            database_url=os.getenv("KNOWLEDGE_WORKER_DATABASE_URL", ""),
            blob_root=Path(os.getenv("KNOWLEDGE_WORKER_BLOB_ROOT", "/var/lib/knowledge")),
            tokenizer=Path(os.getenv("KNOWLEDGE_WORKER_TOKENIZER", "/tokenizer/tokenizer.json")),
            embedding_url=os.getenv("KNOWLEDGE_WORKER_EMBEDDING_URL", "http://embedding:8001"),
            embedding_secret=os.getenv("KNOWLEDGE_WORKER_EMBEDDING_SECRET", ""),
            blob_secret=os.getenv("KNOWLEDGE_WORKER_BLOB_SECRET", ""),
        )


def load_tokenizer(path: Path):
    artifact = next(a for a in PROFILE.artifacts if a.name == "tokenizer.json")
    try:
        if (
            any(p.is_symlink() for p in [path, *path.parents])
            or path.stat().st_size != artifact.size
        ):
            raise EmbeddingUnavailable("tokenizer_artifact")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != artifact.sha256:
                raise EmbeddingUnavailable("tokenizer_artifact")
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(path))
        tokenizer.no_padding()
        tokenizer.no_truncation()
        return tokenizer
    except (OSError, ValueError):
        raise EmbeddingUnavailable("tokenizer_artifact") from None


class PrivateEmbeddings(EmbeddingClient):
    def available(self):
        """Read readiness only. This route never starts the model or performs inference."""
        deadline = time.monotonic() + 2
        try:
            with httpx2.Client(transport=self.transport, trust_env=False, timeout=2) as client:
                with client.stream(
                    "GET", self.url.removesuffix("/internal/embed") + "/health/ready"
                ) as response:
                    if response.status_code != 200:
                        return False
                    body = bytearray()
                    for block in response.iter_bytes(chunk_size=128):
                        if time.monotonic() >= deadline or len(body) + len(block) > 256:
                            return False
                        body.extend(block)
                    return json.loads(body) == {"ready": True}
        except (httpx2.HTTPError, ValueError, TypeError):
            return False
