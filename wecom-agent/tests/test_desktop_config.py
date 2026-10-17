"""Check safe local defaults without callback, Redis or model credentials."""

import json
from importlib import resources
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from src.core.desktop_llm import HTTPReplyModel
from src.desktop_config import DesktopSettings, DesktopSettingsProtocol


def test_defaults_are_dry_run_with_one_application_and_user() -> None:
    """An unconfigured desktop runtime must never widen its whitelist."""
    settings: DesktopSettingsProtocol = DesktopSettings(_env_file=None)
    assert settings.dry_run is True
    assert settings.inbox_backend == "cli"
    assert settings.allowed_user_id == "LiChengYun"
    assert settings.inbox_chat_id == settings.inbox_sender_id == ""


def test_personal_target_is_a_local_namespace_and_cannot_widen_the_whitelist() -> None:
    """Keep the verified external Ri chat distinct from application user IDs."""
    settings = DesktopSettings(inbox_backend="personal", _env_file=None)
    assert settings.personal_chat == "desktop:wechat:Ri"
    assert settings.personal_contact_name == "Ri"
    assert settings.dry_run is True
    with pytest.raises(ValidationError):
        DesktopSettings(personal_contact_name="another-contact", _env_file=None)


def test_personal_prompt_has_an_independent_default_from_the_application_prompt() -> (
    None
):
    """Personal drafting must not silently inherit the application-assistant role."""
    settings: DesktopSettingsProtocol = DesktopSettings(_env_file=None)
    assert settings.personal_llm_template_name == "personal_reply.txt"
    assert settings.llm_template_name == "desktop_reply.txt"
    overridden = DesktopSettings(
        personal_llm_template_name="custom_personal.txt",
        _env_file=None,
    )
    assert overridden.personal_llm_template_name == "custom_personal.txt"
    assert overridden.llm_template_name == "desktop_reply.txt"


@pytest.mark.parametrize(
    "name",
    [
        "../outside.txt",
        "/outside.txt",
        "folder\\outside.txt",
        "folder/outside.txt",
        "personal.json",
        "",
    ],
)
def test_personal_prompt_name_cannot_escape_packaged_text_templates(name: str) -> None:
    """Apply the same filename boundary to the separately selected personal prompt."""
    with pytest.raises(ValidationError) as error:
        DesktopSettings(personal_llm_template_name=name, _env_file=None)
    assert error.value.errors()[0]["loc"] == ("personal_llm_template_name",)


@pytest.mark.asyncio
async def test_personal_prompt_loads_through_the_existing_real_model_adapter() -> None:
    """Exercise packaged template loading and request assembly with mocked HTTP only."""
    settings = DesktopSettings(_env_file=None)
    template = (
        resources.files("prompts")
        .joinpath(settings.personal_llm_template_name)
        .read_text(encoding="utf-8")
    )
    assert "个人聊天" in template
    assert "只输出" in template
    assert "不得编造" in template
    assert "身份" in template
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Capture the system role without calling a model service or real endpoint."""
        requests.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "你好！"}}]}
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        HTTPReplyModel(
            "https://personal-template.invalid/v1",
            "fake-template-key",
            "fake-model",
            template_name=settings.personal_llm_template_name,
            http_client=http,
        ) as model,
    ):
        assert await model.reply("你好") == "你好！"
    assert len(requests) == 1
    assert json.loads(requests[0].content)["messages"] == [
        {"role": "system", "content": template.strip()},
        {"role": "user", "content": "你好"},
    ]


def test_local_config_accepts_empty_model_fields_and_ignores_legacy_whitelist_off(
    tmp_path: Path,
) -> None:
    """The previous WHITELIST_MODE=off must not disable the MVP whitelist."""
    env_file = tmp_path / ".env"
    env_file.write_text("WHITELIST_MODE=off\nLLM_BASE_URL=\nLLM_API_KEY=\nLLM_MODEL=\n")
    settings = DesktopSettings(_env_file=env_file)
    assert settings.allowed_chat == "app:1000002:LiChengYun"
    assert settings.dry_run
    assert settings.llm_model == ""
    assert not hasattr(settings, "whitelist_mode")


def test_secret_is_masked_and_prompt_cannot_escape_directory() -> None:
    """Model keys must not appear in repr and templates cannot name external files."""
    settings = DesktopSettings(llm_api_key="dummy-private-model-key", _env_file=None)
    assert "dummy-private-model-key" not in repr(settings)
    for name in ("../outside.txt", "/outside.txt", "folder\\outside.txt"):
        with pytest.raises(ValidationError):
            DesktopSettings(llm_template_name=name, _env_file=None)


def test_sqlite_path_rejects_network_schemes_and_preserves_local_path() -> None:
    """Do not interpret a remote DB URL as an arbitrary filesystem name."""
    assert DesktopSettings(
        db_url="sqlite:///./agent.db", _env_file=None
    ).sqlite_path == Path("agent.db")
    with pytest.raises(ValueError, match="sqlite"):
        _ = DesktopSettings(
            db_url="postgresql://invalid/db", _env_file=None
        ).sqlite_path
