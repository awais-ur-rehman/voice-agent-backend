"""Private worker configuration. No dotenv, owner credentials or model initialization."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

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


def verify_tokenizer(path: Path):
    """Verify immutable artifact bytes without importing/loading the tokenizer."""
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
    except OSError:
        raise EmbeddingUnavailable("tokenizer_artifact") from None


def load_tokenizer(path: Path):
    verify_tokenizer(path)
    try:
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(path))
        tokenizer.no_padding()
        tokenizer.no_truncation()
        return tokenizer
    except (OSError, ValueError):
        raise EmbeddingUnavailable("tokenizer_artifact") from None


class PrivateEmbeddings(EmbeddingClient):
    """Worker uses the same authenticated observation protocol as the API client."""
