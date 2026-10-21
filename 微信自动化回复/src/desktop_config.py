"""Configure the receive-only CLI and local dry-run pipeline independently of callbacks."""

from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

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
    wechat_ilink_base_url: str
    wechat_ilink_allowed_user: str
    wechat_ilink_token_path: str
    wechat_ilink_state_path: str
    wechat_ocr_contact: str
    wechat_ocr_template_name: str
    wx_cli_workdir: str
    wx_cli_account_verified: bool
    llm_base_url: str
    llm_api_key: SecretStr
    llm_model: str
    llm_template_name: str
    personal_llm_template_name: str
    wechat_llm_template_name: str
    db_url: str
    poll_interval_seconds: float
    dedup_bucket_seconds: int


class DesktopSettings(BaseSettings):
    """Load local MVP settings without requiring Redis or callback credentials."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    dry_run: bool = True
    inbox_backend: Literal["cli", "personal", "wechat_ilink", "wechat_ocr", "demo"] = (
        "cli"
    )
    inbox_chat_id: str = ""
    inbox_sender_id: str = ""
    inbox_verified_single_chat: bool = False
    inbox_timezone: str = "Asia/Shanghai"
    personal_contact_name: Literal["Ri"] = "Ri"
    allowed_user_id: str = "LiChengYun"
    wecom_agent_id: int = Field(default=1000002, gt=0)
    wechat_ilink_base_url: str = "https://ilinkai.weixin.qq.com"
    wechat_ilink_allowed_user: str = ""
    wechat_ilink_token_path: str = ".wechat_ilink.json"
    wechat_ilink_state_path: str = ".wechat_ilink_state.json"
    wechat_ocr_contact: str = ""
    wechat_ocr_template_name: str = "wechat_ocr_reply.txt"
    wx_cli_workdir: str = "."
    wx_cli_account_verified: bool = False
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_template_name: str = "desktop_reply.txt"
    personal_llm_template_name: str = "personal_reply.txt"
    wechat_llm_template_name: str = "wechat_reply.txt"
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

    @field_validator(
        "llm_template_name", "personal_llm_template_name", "wechat_ocr_template_name"
    )
    @classmethod
    def require_template_filename(cls, value: str, info: ValidationInfo) -> str:
        """Confine prompt configuration to one filename under prompts/."""
        if not value.endswith(".txt") or Path(value).name != value or "\\" in value:
            raise ValueError(
                f"{info.field_name.upper()} must be a .txt filename in prompts/"
            )
        return value

    @field_validator("wechat_ilink_token_path", "wechat_ilink_state_path")
    @classmethod
    def require_local_state_filename(cls, value: str, info: ValidationInfo) -> str:
        """Confine credential and cursor files to plain names in the project dir."""
        if not value or Path(value).name != value or "\\" in value or "/" in value:
            raise ValueError(
                f"{info.field_name.upper()} must be a plain filename in the project directory"
            )
        return value

    @field_validator("wechat_ilink_base_url")
    @classmethod
    def require_plain_ilink_origin(cls, value: str) -> str:
        """Require an HTTP(S) origin without userinfo, query or fragment."""
        failed = False
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                failed = True
        except ValueError:
            failed = True
        if failed:
            raise ValueError(
                "WECHAT_ILINK_BASE_URL must be an HTTP(S) origin without credentials"
            )
        return value.rstrip("/")

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
