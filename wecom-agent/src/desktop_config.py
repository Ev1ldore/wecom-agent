"""Configure the receive-only CLI and local dry-run pipeline independently of callbacks."""

from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from adapters.inbox import application_chat


class DesktopSettingsProtocol(Protocol):
    """Expose only configuration used by the local desktop runtime."""

    dry_run: bool
    inbox_backend: str
    inbox_chat_id: str
    inbox_sender_id: str
    inbox_verified_single_chat: bool
    inbox_timezone: str
    personal_contact_name: str
    allowed_user_id: str
    wecom_agent_id: int
    llm_base_url: str
    llm_api_key: SecretStr
    llm_model: str
    llm_template_name: str
    personal_llm_template_name: str
    db_url: str
    poll_interval_seconds: float
    dedup_bucket_seconds: int


class DesktopSettings(BaseSettings):
    """Load local MVP settings without requiring Redis or callback credentials."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    dry_run: bool = True
    inbox_backend: Literal["cli", "personal", "demo"] = "cli"
    inbox_chat_id: str = ""
    inbox_sender_id: str = ""
    inbox_verified_single_chat: bool = False
    inbox_timezone: str = "Asia/Shanghai"
    personal_contact_name: Literal["Ri"] = "Ri"
    allowed_user_id: str = "LiChengYun"
    wecom_agent_id: int = Field(default=1000002, gt=0)
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_template_name: str = "desktop_reply.txt"
    personal_llm_template_name: str = "personal_reply.txt"
    db_url: str = "sqlite:///./agent.db"
    poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=60)
    dedup_bucket_seconds: int = Field(default=5, ge=1, le=300)

    @field_validator("allowed_user_id")
    @classmethod
    def require_user_id(cls, value: str) -> str:
        """Retain case-sensitive identities while refusing a blank whitelist."""
        if not value.strip():
            raise ValueError("ALLOWED_USER_ID must not be empty")
        return value

    @field_validator("llm_template_name", "personal_llm_template_name")
    @classmethod
    def require_template_filename(cls, value: str, info: ValidationInfo) -> str:
        """Confine prompt configuration to one filename under prompts/."""
        if not value.endswith(".txt") or Path(value).name != value or "\\" in value:
            raise ValueError(
                f"{info.field_name.upper()} must be a .txt filename in prompts/"
            )
        return value

    @property
    def allowed_chat(self) -> str:
        """Return the canonical application/user single-chat identity."""
        return application_chat(self.wecom_agent_id, self.allowed_user_id)

    @property
    def personal_chat(self) -> str:
        """Name the verified local external chat without inventing a server ID."""
        return f"desktop:wechat:{self.personal_contact_name}"

    @property
    def sqlite_path(self) -> Path:
        """Resolve a local SQLite URL without supporting remote database schemes."""
        prefix = "sqlite:///"
        if not self.db_url.startswith(prefix) or not self.db_url[len(prefix) :]:
            raise ValueError("DB_URL must be a nonempty sqlite:/// local path")
        return Path(self.db_url[len(prefix) :]).expanduser()
