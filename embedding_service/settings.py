import os
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
RSS_LIMIT = 1536 * MIB
AVAILABLE_FLOOR = 1792 * MIB
STARTUP_SECONDS = 120.0
INFERENCE_SECONDS = 10.0


@dataclass(frozen=True)
class Settings:
    assets: Path
    secret: str
    enabled: bool = False

    def __post_init__(self):
        if len(self.secret) < 32:
            raise ValueError("embedding secret required")

    @classmethod
    def from_environment(cls):
        return cls(
            Path(os.environ.get("EMBEDDING_ASSETS", "/models/bge-m3")),
            os.environ.get("EMBEDDING_SECRET", ""),
            os.environ.get("EMBEDDING_ENABLED", "false") == "true",
        )
