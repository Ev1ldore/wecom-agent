"""Probe configured WeCom credentials and send one explicitly addressed message."""

import argparse
import asyncio
import ipaddress
import logging
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol
from urllib.parse import quote, quote_plus
from uuid import uuid4

import httpx
from pydantic import ValidationError
from structlog.contextvars import bound_contextvars

from src.config import Settings, get_settings
from src.core.logging import get_logger
from src.gateway.wecom_client import WeComAPIError, WecomClient

TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
PUBLIC_IP_URL = "https://api.ipify.org"
REQUEST_TIMEOUT_SECONDS = 10.0
logger = get_logger(__name__)


class APISmokeProtocol(Protocol):
    """Define the API smoke operation without prescribing its HTTP transport."""

    async def __call__(
        self,
        settings: Settings,
        user_id: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Probe credentials and ask the API to send one smoke-test message."""
        raise NotImplementedError


class SecretRedactor:
    """Mask configured credentials and every observed gettoken access token."""

    def __init__(self, settings: Settings) -> None:
        """Retain only the values needed for exact-value output redaction."""
        self._secrets: set[str] = set()
        for value in (
            settings.wecom_secret,
            settings.wecom_token,
            settings.wecom_aes_key,
        ):
            self._remember(value)

    def _remember(self, value: str) -> None:
        """Cover direct values and their forms inside URL-encoded query strings."""
        if value:
            self._secrets.update((value, quote(value, safe=""), quote_plus(value)))

    async def capture_token(self, response: httpx.Response) -> None:
        """Observe token response bodies as an HTTPX response hook without logging."""
        url = response.request.url
        if url.host != "qyapi.weixin.qq.com" or url.path != "/cgi-bin/gettoken":
            return
        await response.aread()
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            return
        if isinstance(payload, dict):
            token = payload.get("access_token")
            if isinstance(token, str):
                self._remember(token)

    def redact(self, text: str) -> str:
        """Preserve ordinary API messages while masking known sensitive values."""
        for value in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text


def validate_user_id(user_id: str) -> str:
    """Reject absent or template recipients before loading settings or sending."""
    value = user_id.strip()
    if (
        not value
        or value.upper() in {"USER_ID", "ACTUAL_ID", "YOUR_USER_ID_HERE"}
        or (value.startswith("<") and value.endswith(">"))
    ):
        raise ValueError("--user-id 必须填写实际企业微信成员 ID，不能使用占位符")
    return value


@contextmanager
def quiet_http_logs() -> Iterator[None]:
    """Suppress query-bearing HTTPX/HTTPCore INFO and DEBUG request logs."""
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    previous = [item.level for item in loggers]
    for item in loggers:
        item.setLevel(max(item.getEffectiveLevel(), logging.WARNING))
    try:
        yield
    finally:
        for item, level in zip(loggers, previous, strict=True):
            item.setLevel(level)


async def _get_json(
    http: httpx.AsyncClient, url: str, *, params: dict[str, str], label: str
) -> dict[str, Any]:
    """Try a JSON GET twice without retaining credential-bearing HTTP exceptions."""
    for attempt in range(2):
        error_type = "unknown"
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                response = await http.get(
                    url, params=params, timeout=REQUEST_TIMEOUT_SECONDS
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError("JSON response must be an object")
                return payload
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError) as exc:
            error_type = type(exc).__name__
        if attempt == 1:
            # Raise outside except so the public error has no raw HTTP context.
            raise WeComAPIError(
                -1, f"{label} request failed after 2 attempts ({error_type})"
            ) from None
    raise AssertionError("GET retry loop finished without a result")


async def _probe_token(http: httpx.AsyncClient, settings: Settings) -> int:
    """Validate gettoken and return only its public expiry value."""
    payload = await _get_json(
        http,
        TOKEN_URL,
        params={"corpid": settings.wecom_corpid, "corpsecret": settings.wecom_secret},
        label="gettoken",
    )
    errcode = payload.get("errcode", 0)
    errmsg = payload.get("errmsg", "")
    if type(errcode) is not int or not isinstance(errmsg, str):
        raise WeComAPIError(-1, "gettoken response has invalid error fields")
    if errcode != 0:
        raise WeComAPIError(errcode, errmsg)
    token = payload.get("access_token")
    expiry = payload.get("expires_in", 7200)
    if not isinstance(token, str) or not token:
        raise WeComAPIError(-1, "gettoken response is missing access_token")
    if type(expiry) is not int or expiry <= 0:
        raise WeComAPIError(-1, "gettoken response has invalid expires_in")
    return expiry


async def _current_public_ip(http: httpx.AsyncClient) -> str:
    """Return the current outbound IP or an explicit unknown result."""
    try:
        payload = await _get_json(
            http, PUBLIC_IP_URL, params={"format": "json"}, label="public IP"
        )
        value = payload.get("ip")
        if isinstance(value, str):
            return str(ipaddress.ip_address(value))
    except (WeComAPIError, ValueError):
        return "unknown"
    return "unknown"


async def run_api_smoke(
    settings: Settings,
    user_id: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Probe configured credentials and send smoke-test-1 with safe diagnostics.

    Success confirms API acceptance only. The recipient must separately confirm
    that the enterprise WeChat client actually received the message.
    """
    recipient = validate_user_id(user_id)
    redactor = SecretRedactor(settings)
    owns_client = http_client is None
    http = http_client if http_client is not None else httpx.AsyncClient(timeout=10.0)
    hook = redactor.capture_token
    http.event_hooks["response"].append(hook)
    failure: WeComAPIError | None = None
    msg_id = f"smoke-api-{uuid4().hex}"
    log = logger.bind(msg_id=msg_id)
    with quiet_http_logs():
        try:
            try:
                expiry = await _probe_token(http, settings)
                print(f"阶段1：gettoken 成功；expires_in={expiry}")
                log.info("smoke_token_probe_accepted", expires_in=expiry)
                async with WecomClient(
                    settings.wecom_corpid,
                    settings.wecom_secret,
                    settings.wecom_agent_id,
                    http_client=http,
                ) as client:
                    with bound_contextvars(msg_id=msg_id):
                        await client.send_text(recipient, "smoke-test-1")
                print(
                    "阶段2：发送 API 已接受 smoke-test-1；客户端实收未确认，请人工确认。"
                )
                log.info("smoke_send_api_accepted", received_confirmed=False)
            except WeComAPIError as exc:
                failure = WeComAPIError(exc.errcode, redactor.redact(exc.errmsg))
            except Exception as exc:  # noqa: BLE001 - Never expose raw secret-bearing exceptions.
                failure = WeComAPIError(-1, f"API smoke failed ({type(exc).__name__})")

            if failure is not None:
                print(f"失败：errcode={failure.errcode}；errmsg={failure.errmsg}")
                log.error("smoke_api_failed", errcode=failure.errcode)
                if failure.errcode == 60020:
                    print("排查项：服务器出口 IP 未加入应用「企业可信 IP」。")
                    public_ip = await _current_public_ip(http)
                    print(f"当前出口公网 IP：{public_ip}")
                elif failure.errcode == 81013:
                    print("排查项：user 不在应用可见范围，或账号大小写不符。")
                elif failure.errcode in {40001, 42001}:
                    print("排查项：secret 无效或票据问题。")
        finally:
            http.event_hooks["response"].remove(hook)
            if owns_client:
                await http.aclose()
    if failure is not None:
        # Do not retain the original API exception in the public exception chain.
        raise failure from None


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI only after recipient and required configuration validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="实际企业微信成员 ID")
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        user_id = validate_user_id(arguments.user_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(str(part) for part in error["loc"])
                for error in exc.errors(include_input=False, include_context=False)
            }
        )
        print(f"配置缺失或无效字段：{', '.join(fields)}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - Configuration errors can embed secret input values.
        print(f"配置读取失败：{type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        asyncio.run(run_api_smoke(settings, user_id))
    except WeComAPIError:
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI failures must not print raw exception tracebacks.
        print(f"API smoke 失败：{type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
