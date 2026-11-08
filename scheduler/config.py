from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCHEDULER_", extra="ignore", env_ignore_empty=True
    )
    database_url: SecretStr
    jwt_secret: SecretStr = Field(min_length=32)
    runtime_secret: SecretStr | None = Field(default=None, min_length=32)
    valkey_url: SecretStr | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str = "nvidia/nemotron-3.5-bolt-e4b"
    llm_timeout_seconds: float = Field(default=30, ge=1, le=60)
    llm_request_budget: int = Field(default=100, ge=1, le=1000)
    registration_enabled: bool = False
    knowledge_enabled: bool = False
    knowledge_embedding_url: str = "http://127.0.0.1:8001"
    knowledge_embedding_secret: SecretStr | None = Field(default=None, min_length=32)
    knowledge_blob_secret: SecretStr | None = Field(default=None, min_length=32)
    knowledge_blob_root: Path = ROOT / ".local/knowledge-blobs"
    voice_enabled: bool = False
    livekit_url: str = "ws://127.0.0.1:7880"
    livekit_api_url: str = "http://127.0.0.1:7880"
    livekit_api_key: str | None = None
    livekit_api_secret: SecretStr | None = Field(default=None, min_length=32)
    voice_bootstrap_secret: SecretStr | None = Field(default=None, min_length=32)
    voice_worker_secret: SecretStr | None = Field(default=None, min_length=32)
    jwt_issuer: str = "voice-agent-api"
    jwt_audience: str = "voice-agent-staff"
    access_minutes: int = Field(default=15, ge=1, le=60)
    refresh_days: int = Field(default=7, ge=1, le=30)
    cors_origins: list[str] = ["http://localhost:5173"]

    @model_validator(mode="after")
    def separate_blob_secret(self):
        if self.knowledge_blob_secret is not None:
            value = self.knowledge_blob_secret.get_secret_value()
            others = (
                self.jwt_secret,
                self.knowledge_embedding_secret,
                self.runtime_secret,
                self.llm_api_key,
                self.voice_bootstrap_secret,
                self.voice_worker_secret,
                self.livekit_api_secret,
            )
            if any(secret is not None and value == secret.get_secret_value() for secret in others):
                raise ValueError("A distinct knowledge blob ownership secret is required")
        return self


def load_settings() -> Settings:
    # Intentionally separate from the existing Phase 0 NVIDIA credential file.
    return Settings(_env_file=ROOT / ".env.api")
