"""No dotenv, provider, parser, tokenizer or model configuration."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANALYTICS_WORKER_", extra="ignore")
    enabled: bool = False
    database_url: SecretStr = SecretStr("")
    poll_seconds: float = Field(default=10, ge=1, le=60)
    batch_size: int = Field(default=100, ge=1, le=100)
