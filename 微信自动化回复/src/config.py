"""Load the current service settings from a shared dotenv file or environment."""

from functools import lru_cache
from typing import Protocol

from pydantic_settings import BaseSettings


class SettingsProtocol(Protocol):
    """Define configuration consumed by the gateway and smoke runners."""

    wecom_corpid: str
    wecom_secret: str
    wecom_token: str
    wecom_aes_key: str
    wecom_agent_id: int
    redis_url: str
    queue_key: str
    app_port: int


class Settings(BaseSettings):
    """Validate required service fields and ignore unrelated dotenv settings."""

    wecom_corpid: str
    wecom_secret: str
    wecom_token: str
    wecom_aes_key: str
    wecom_agent_id: int
    redis_url: str = "redis://localhost:6379/0"
    queue_key: str = "wecom:msg:stream"
    app_port: int = 8000

    class Config:
        """Retain the requested dotenv source while allowing a shared file."""

        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    """Return one cached, validated settings instance per process."""
    return Settings()
