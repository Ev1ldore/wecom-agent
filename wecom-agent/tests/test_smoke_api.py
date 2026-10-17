"""Test API smoke checks without credentials or external network requests."""

import asyncio
import json
import logging
import traceback
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote

import httpx
import pytest
from structlog.contextvars import bound_contextvars, get_contextvars

from smoke import smoke_api
from smoke.smoke_api import APISmokeProtocol, SecretRedactor, run_api_smoke
from src.config import Settings
from src.gateway.wecom_client import WeComAPIError

TOKEN_PATH = "/cgi-bin/gettoken"
SEND_PATH = "/cgi-bin/message/send"
PROBE_TOKEN = "private-probe-token"
SEND_TOKEN = "private-send-token"


@pytest.fixture
def settings() -> Settings:
    """Use non-production values without reading a local dotenv file."""
    return Settings(
        wecom_corpid="corp-test",
        wecom_secret="private corp/secret",
        wecom_token="private-callback-token",
        wecom_aes_key="private-aes-key",
        wecom_agent_id=1000001,
        _env_file=None,
    )


@pytest.mark.asyncio
async def test_smoke_protocol_supports_a_callable_without_external_io(
    settings: Settings,
) -> None:
    """The operation contract can be replaced independently of HTTP details."""
    calls: list[str] = []

    async def fake_smoke(
        settings: Settings,
        user_id: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Capture only the intended business recipient."""
        calls.append(user_id)

    operation: APISmokeProtocol = fake_smoke
    await operation(settings, "real-user")
    assert calls == ["real-user"]


@pytest.mark.asyncio
async def test_secret_redactor_captures_gettoken_and_preserves_ordinary_errors(
    settings: Settings,
) -> None:
    """Mask configured secrets, URL-encoded secrets and dynamically acquired tokens."""
    redactor = SecretRedactor(settings)
    response = httpx.Response(
        200,
        json={"access_token": SEND_TOKEN},
        request=httpx.Request("GET", smoke_api.TOKEN_URL),
    )
    await redactor.capture_token(response)
    secrets = (
        settings.wecom_secret,
        settings.wecom_token,
        settings.wecom_aes_key,
        quote(settings.wecom_secret, safe=""),
        SEND_TOKEN,
    )
    rendered = redactor.redact(" | ".join(secrets))
    assert all(value not in rendered for value in secrets)
    assert redactor.redact("invalid userid, hint: check application visibility") == (
        "invalid userid, hint: check application visibility"
    )


@pytest.mark.asyncio
async def test_success_probes_expiry_then_sends_fixed_message_without_claiming_receipt(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Use the actual HTTP client contract and print only safe acceptance evidence."""
    requests: list[httpx.Request] = []
    token_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Return separate tokens for the probe and the message sender."""
        nonlocal token_count
        requests.append(request)
        if request.url.path == TOKEN_PATH:
            token_count += 1
            return httpx.Response(
                200,
                json={
                    "access_token": PROBE_TOKEN if token_count == 1 else SEND_TOKEN,
                    "expires_in": 7200,
                },
            )
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        original_hooks = list(http.event_hooks["response"])
        assert await run_api_smoke(settings, "real-user", http_client=http) is None
        assert not http.is_closed
        assert http.event_hooks["response"] == original_hooks

    assert [request.url.path for request in requests] == [
        TOKEN_PATH,
        TOKEN_PATH,
        SEND_PATH,
    ]
    assert dict(requests[0].url.params) == {
        "corpid": settings.wecom_corpid,
        "corpsecret": settings.wecom_secret,
    }
    assert json.loads(requests[-1].content)["text"] == {"content": "smoke-test-1"}
    assert json.loads(requests[-1].content)["touser"] == "real-user"
    assert all(request.extensions["timeout"]["read"] == 10.0 for request in requests)
    output = capsys.readouterr().out
    first_stage = output.splitlines()[0]
    assert "expires_in=7200" in first_stage
    assert "API 已接受" in output
    assert "客户端实收未确认" in output
    for value in (
        settings.wecom_secret,
        settings.wecom_token,
        settings.wecom_aes_key,
        PROBE_TOKEN,
        SEND_TOKEN,
    ):
        assert value not in output


@pytest.mark.asyncio
async def test_token_business_error_keeps_original_fields_and_does_not_send(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Configuration errors must stop the smoke operation without business bypasses."""
    requests: list[httpx.Request] = []
    original_message = "invalid corpid, check your enterprise configuration"

    def handler(request: httpx.Request) -> httpx.Response:
        """Reject the first probe with a deterministic API error."""
        requests.append(request)
        return httpx.Response(200, json={"errcode": 40013, "errmsg": original_message})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(WeComAPIError) as caught:
            await run_api_smoke(settings, "real-user", http_client=http)

    assert len(requests) == 1
    assert caught.value.errcode == 40013
    assert caught.value.errmsg == original_message
    assert original_message in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("send_fails", [False, True])
async def test_smoke_and_client_logs_share_context_then_restore_the_caller(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
    send_fails: bool,
) -> None:
    """Correlate both stages and restore caller context after success or failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a token and a selectable send outcome without external I/O."""
        if request.url.path == TOKEN_PATH:
            return httpx.Response(
                200, json={"access_token": PROBE_TOKEN, "expires_in": 7200}
            )
        return httpx.Response(
            200,
            json={"errcode": 40003 if send_fails else 0, "errmsg": "test outcome"},
        )

    with (
        caplog.at_level(logging.INFO),
        bound_contextvars(msg_id="caller-message", smoke_context="preserved"),
    ):
        previous_context = get_contextvars()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            if send_fails:
                with pytest.raises(WeComAPIError):
                    await run_api_smoke(settings, "real-user", http_client=http)
            else:
                await run_api_smoke(settings, "real-user", http_client=http)
        assert get_contextvars() == previous_context

    entries = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name in {"smoke.smoke_api", "src.gateway.wecom_client"}
    ]
    assert len({entry["msg_id"] for entry in entries}) == 1
    assert entries[0]["msg_id"].startswith("smoke-api-")
    assert all(entry["smoke_context"] == "preserved" for entry in entries)
    events = {entry["event"] for entry in entries}
    assert "smoke_token_probe_accepted" in events
    assert "wecom_access_token_refreshed" in events
    assert (
        {"smoke_api_failed", "wecom_text_send_failed"}
        if send_fails
        else {"smoke_send_api_accepted", "wecom_text_sent"}
    ) <= events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("errcode", "hint"),
    [(81013, "应用可见范围"), (40001, "secret 无效"), (42001, "票据问题")],
)
async def test_user_supplied_configuration_hints_do_not_change_api_errors(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    errcode: int,
    hint: str,
) -> None:
    """Retain the actual provider response alongside the requested troubleshooting hint."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return the original business code without triggering any bypass."""
        assert request.url.host == "qyapi.weixin.qq.com"
        return httpx.Response(
            200, json={"errcode": errcode, "errmsg": "original error"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(WeComAPIError) as caught:
            await run_api_smoke(settings, "real-user", http_client=http)

    assert caught.value.errcode == errcode
    assert caught.value.errmsg == "original error"
    assert hint in capsys.readouterr().out


@pytest.mark.asyncio
async def test_60020_reports_mocked_current_public_ip(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Look up the outbound IP only for the explicitly identified trusted-IP error."""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve both the rejected token request and the public-IP lookup."""
        hosts.append(request.url.host)
        assert request.extensions["timeout"]["read"] == 10.0
        if request.url.host == "api.ipify.org":
            assert dict(request.url.params) == {"format": "json"}
            return httpx.Response(200, json={"ip": "203.0.113.24"})
        return httpx.Response(200, json={"errcode": 60020, "errmsg": "not trusted ip"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(WeComAPIError) as caught:
            await run_api_smoke(settings, "real-user", http_client=http)

    assert caught.value.errcode == 60020
    assert caught.value.errmsg == "not trusted ip"
    assert hosts == ["qyapi.weixin.qq.com", "api.ipify.org"]
    output = capsys.readouterr().out
    assert "203.0.113.24" in output
    assert "企业可信 IP" in output


@pytest.mark.asyncio
async def test_public_ip_failure_retries_then_explicitly_reports_unknown(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed diagnostic lookup must not replace the original 60020 error."""
    ip_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Keep IP lookup unavailable while preserving the WeCom business error."""
        nonlocal ip_calls
        if request.url.host == "api.ipify.org":
            ip_calls += 1
            return httpx.Response(503, content=b"unavailable")
        return httpx.Response(200, json={"errcode": 60020, "errmsg": "not trusted ip"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(WeComAPIError) as caught:
            await run_api_smoke(settings, "real-user", http_client=http)

    assert caught.value.errcode == 60020
    assert ip_calls == 2
    assert "当前出口公网 IP：unknown" in capsys.readouterr().out


def transport_failure(request: httpx.Request, kind: str) -> httpx.Response:
    """Return a temporary HTTP/JSON failure or raise a credential-bearing timeout."""
    if kind == "http":
        return httpx.Response(503)
    if kind == "json":
        return httpx.Response(200, content=b"broken JSON")
    if kind == "array":
        return httpx.Response(200, json=[])
    raise httpx.ReadTimeout(f"timeout at {request.url}", request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "json", "array", "timeout"])
async def test_probe_retries_temporary_failures_once(
    settings: Settings, kind: str
) -> None:
    """Recover a transient probe failure before proceeding to the real sender."""
    probes = 0
    sends = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail the first token request and acknowledge subsequent operations."""
        nonlocal probes, sends
        if request.url.path == TOKEN_PATH:
            probes += 1
            if probes == 1:
                return transport_failure(request, kind)
            return httpx.Response(
                200, json={"access_token": PROBE_TOKEN, "expires_in": 7200}
            )
        sends += 1
        return httpx.Response(200, json={"errcode": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await run_api_smoke(settings, "real-user", http_client=http)

    assert probes == 3
    assert sends == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "json", "array", "timeout"])
async def test_exhausted_probe_retry_is_safe_and_does_not_send(
    settings: Settings, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """Stop after two attempts without exposing HTTP exception URL credentials."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Keep the initial token probe unavailable."""
        nonlocal calls
        calls += 1
        return transport_failure(request, kind)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(WeComAPIError) as caught:
            await run_api_smoke(settings, "real-user", http_client=http)

    assert calls == 2
    assert caught.value.errcode == -1
    rendered = capsys.readouterr().out + "".join(
        traceback.format_exception(caught.value)
    )
    assert settings.wecom_secret not in rendered
    assert quote(settings.wecom_secret, safe="") not in rendered
    assert "https://" not in rendered
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_all_observed_tokens_and_configured_secrets_are_redacted(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep API errmsg useful while preventing stdout, log and traceback leaks."""
    tokens = iter([PROBE_TOKEN, SEND_TOKEN])
    sensitive_values = (
        settings.wecom_secret,
        settings.wecom_token,
        settings.wecom_aes_key,
        PROBE_TOKEN,
        SEND_TOKEN,
        quote(settings.wecom_secret, safe=""),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """Echo all secret values in a provider error to exercise final sanitization."""
        if request.url.path == TOKEN_PATH:
            return httpx.Response(
                200, json={"access_token": next(tokens), "expires_in": 7200}
            )
        return httpx.Response(
            200,
            json={
                "errcode": 40003,
                "errmsg": "ordinary prefix " + " | ".join(sensitive_values),
            },
        )

    previous_httpx_level = logging.getLogger("httpx").level
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(WeComAPIError) as caught:
                await run_api_smoke(settings, "real-user", http_client=http)
        rendered = (
            capsys.readouterr().out
            + caplog.text
            + "".join(traceback.format_exception(caught.value))
        )
        assert all(value not in rendered for value in sensitive_values)
        assert not any(record.name == "httpx" for record in caplog.records)
    assert logging.getLogger("httpx").level == previous_httpx_level
    assert caught.value.errcode == 40003
    assert caught.value.errmsg.startswith("ordinary prefix ")
    assert "[REDACTED]" in caught.value.errmsg
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_stalled_probe_obeys_overall_request_budget(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound each attempt even when an injected transport never responds."""
    calls = 0
    monkeypatch.setattr(smoke_api, "REQUEST_TIMEOUT_SECONDS", 0.02)

    async def handler(request: httpx.Request) -> httpx.Response:
        """Remain pending until the attempt is cancelled by its timeout."""
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with asyncio.timeout(1):
            with pytest.raises(WeComAPIError):
                await run_api_smoke(settings, "real-user", http_client=http)
    assert calls == 2


@pytest.mark.parametrize("user_id", ["", " ", "<USER_ID>", "USER_ID", "ACTUAL_ID"])
def test_cli_rejects_placeholder_before_loading_credentials_or_creating_http_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    user_id: str,
) -> None:
    """A template command cannot accidentally fetch credentials or send a message."""
    load = Mock()
    create_http = Mock()
    monkeypatch.setattr(smoke_api, "get_settings", load)
    monkeypatch.setattr(smoke_api.httpx, "AsyncClient", create_http)

    assert smoke_api.main(["--user-id", user_id]) == 2
    load.assert_not_called()
    create_http.assert_not_called()
    assert "实际企业微信成员 ID" in capsys.readouterr().err


def test_cli_reports_only_invalid_config_field_names_without_external_io(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pydantic input values must never leak through validation error rendering."""
    invalid_value = "private-config-value-not-an-agent-id"

    def invalid_settings() -> Settings:
        """Produce a real validation error whose default rendering includes input."""
        values = settings.model_dump()
        values["wecom_agent_id"] = invalid_value
        return Settings(**values, _env_file=None)

    create_http = Mock()
    monkeypatch.setattr(smoke_api, "get_settings", invalid_settings)
    monkeypatch.setattr(smoke_api.httpx, "AsyncClient", create_http)

    assert smoke_api.main(["--user-id", "real-user"]) == 2
    create_http.assert_not_called()
    output = capsys.readouterr().err
    assert "wecom_agent_id" in output
    assert invalid_value not in output
    assert settings.wecom_secret not in output


def test_cli_runs_validated_settings_and_reports_success_exit_code(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a configured recipient reaches the async smoke operation."""
    operation = AsyncMock(return_value=None)
    monkeypatch.setattr(smoke_api, "get_settings", lambda: settings)
    monkeypatch.setattr(smoke_api, "run_api_smoke", operation)

    assert smoke_api.main(["--user-id", "real-user"]) == 0
    operation.assert_awaited_once_with(settings, "real-user")
