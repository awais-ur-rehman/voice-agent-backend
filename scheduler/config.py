from decimal import Decimal
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCHEDULER_", extra="ignore", env_ignore_empty=True, hide_input_in_errors=True
    )
    database_url: SecretStr
    jwt_secret: SecretStr = Field(min_length=32)
    runtime_secret: SecretStr | None = Field(default=None, min_length=32)
    valkey_url: SecretStr | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str = Field(
        default="nvidia/nemotron-3.5-bolt-e4b",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_./:-]+$",
    )
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_secondary_api_key: SecretStr | None = None
    llm_secondary_base_url: str | None = None
    llm_secondary_model: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_./:-]+$"
    )
    llm_secondary_input_rate_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=1000000, allow_inf_nan=False
    )
    llm_secondary_output_rate_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=1000000, allow_inf_nan=False
    )
    llm_endpoint_rpm: float = Field(default=10, gt=0, le=10000, allow_inf_nan=False)
    llm_endpoint_burst: int = Field(default=1, ge=1, le=1000)
    llm_max_outstanding: int = Field(default=4, ge=1, le=20)
    llm_timeout_seconds: float = Field(default=3, gt=0, le=3, allow_inf_nan=False)
    llm_request_budget: int = Field(default=100, ge=1, le=1000)
    llm_input_rate_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=1000000, allow_inf_nan=False
    )
    llm_output_rate_usd_per_million: Decimal | None = Field(
        default=None, ge=0, le=1000000, allow_inf_nan=False
    )
    analytics_metrics_secret: SecretStr | None = Field(default=None, min_length=32)
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

    @field_validator("llm_base_url", "llm_secondary_base_url")
    @classmethod
    def provider_url(cls, value):
        if value is None:
            return value
        try:
            url = urlsplit(value)
            loopback = False
            if url.hostname:
                try:
                    loopback = ip_address(url.hostname).is_loopback
                except ValueError:
                    pass
            if (
                not url.hostname
                or url.username is not None
                or url.password is not None
                or "?" in value
                or "#" in value
                or "\\" in value
                or any(ch.isspace() for ch in value)
                or not (url.scheme == "https" or url.scheme == "http" and loopback)
            ):
                raise ValueError
            _ = url.port
        except ValueError:
            raise ValueError(
                "Provider base URL must be HTTPS or literal loopback HTTP without credentials, query or fragment"
            ) from None
        return value.rstrip("/")

    @model_validator(mode="after")
    def secondary_configuration(self):
        values = (self.llm_secondary_base_url, self.llm_secondary_model, self.llm_secondary_api_key)
        if any(value is not None for value in values) and not all(values):
            raise ValueError("Secondary provider requires explicit base URL, model and key")
        if (
            self.llm_secondary_api_key is not None
            and not self.llm_secondary_api_key.get_secret_value()
        ):
            raise ValueError("Secondary provider requires a nonempty key")
        return self

    @model_validator(mode="after")
    def separate_blob_secret(self):
        if self.knowledge_blob_secret is not None:
            value = self.knowledge_blob_secret.get_secret_value()
            others = (
                self.jwt_secret,
                self.knowledge_embedding_secret,
                self.runtime_secret,
                self.llm_api_key,
                self.llm_secondary_api_key,
                self.voice_bootstrap_secret,
                self.voice_worker_secret,
                self.livekit_api_secret,
            )
            if any(secret is not None and value == secret.get_secret_value() for secret in others):
                raise ValueError("A distinct knowledge blob ownership secret is required")
        if self.analytics_metrics_secret is not None:
            value = self.analytics_metrics_secret.get_secret_value()
            scrape_others = (
                self.jwt_secret,
                self.runtime_secret,
                self.voice_worker_secret,
                self.voice_bootstrap_secret,
                self.knowledge_embedding_secret,
                self.knowledge_blob_secret,
                self.livekit_api_secret,
                self.llm_api_key,
                self.llm_secondary_api_key,
            )
            if any(
                secret is not None and value == secret.get_secret_value()
                for secret in scrape_others
            ):
                raise ValueError("A distinct analytics scrape secret is required")
        return self


def load_settings() -> Settings:
    # Intentionally separate from the existing Phase 0 NVIDIA credential file.
    return Settings(_env_file=ROOT / ".env.api")
