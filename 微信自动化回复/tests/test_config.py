"""Verify dotenv compatibility without weakening required WeCom configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import Settings


def test_dotenv_ignores_unrelated_future_configuration(tmp_path: Path) -> None:
    """Read current fields when a shared dotenv also contains future settings."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WECOM_CORPID=test-corp\n"
        "WECOM_SECRET=test-secret\n"
        "WECOM_TOKEN=test-callback-token\n"
        "WECOM_AES_KEY=test-aes-key\n"
        "WECOM_AGENT_ID=1000001\n"
        "LLM_MODEL=test-model\n"
        "DB_URL=sqlite:///test.db\n"
    )
    settings = Settings(_env_file=env_file)
    assert settings.wecom_corpid == "test-corp"
    assert settings.wecom_agent_id == 1000001
    assert settings.redis_url == "redis://localhost:6379/0"
    assert not hasattr(settings, "llm_model")
    assert not hasattr(settings, "db_url")


def test_required_settings_are_still_validated() -> None:
    """Ignoring unknown dotenv keys must retain required field type checks."""
    with pytest.raises(ValidationError) as captured:
        Settings(
            _env_file=None,
            wecom_corpid="test-corp",
            wecom_secret="test-secret",
            wecom_token="test-callback-token",
            wecom_aes_key="test-aes-key",
            wecom_agent_id="invalid-integer",
        )
    errors = captured.value.errors(include_input=False)
    assert any(error["loc"] == ("wecom_agent_id",) for error in errors)
