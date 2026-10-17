"""Exercise compatible LLM replies using only mocked HTTP transports."""

import asyncio
import json
import logging
from importlib import resources
from typing import Any

import httpx
import pytest
from structlog.contextvars import bound_contextvars, get_contextvars

from src.core import desktop_llm
from src.core.desktop_llm import HTTPReplyModel, LLMError, ReplyModel

API_KEY = "test-private-api-key"
BASE_URL = "https://llm.test/v1"
MODEL = "test-model"


def success_response(text: str = "测试回复") -> httpx.Response:
    """Return one valid assistant response without contacting a model."""
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def log_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Read the model's safe structured events through stdlib logging."""
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "src.core.desktop_llm"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "path"),
    [
        ("https://llm.test", "/chat/completions"),
        ("https://llm.test/", "/chat/completions"),
        (BASE_URL, "/v1/chat/completions"),
        (BASE_URL + "/", "/v1/chat/completions"),
        (BASE_URL + "/chat/completions", "/v1/chat/completions"),
        (BASE_URL + "/chat/completions/", "/v1/chat/completions"),
        ("http://localhost:11434/v1", "/v1/chat/completions"),
    ],
)
async def test_reply_uses_configured_endpoint_template_and_user_message(
    base_url: str, path: str
) -> None:
    """Preserve configured prefixes, avoid duplicate paths and send role-separated input."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Observe the exact compatible request and return a fixed model reply."""
        requests.append(request)
        return success_response("  保留模型原文  ")

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        HTTPReplyModel(base_url, API_KEY, MODEL, http_client=http) as model,
    ):
        reply_model: ReplyModel = model
        assert await reply_model.reply("用户问题 & 内容") == "  保留模型原文  "

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == path
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.extensions["timeout"] == {
        "connect": 10.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
    template = (
        resources.files("prompts")
        .joinpath("desktop_reply.txt")
        .read_text(encoding="utf-8")
    )
    assert json.loads(request.content) == {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": template.strip()},
            {"role": "user", "content": "用户问题 & 内容"},
        ],
    }


@pytest.mark.asyncio
async def test_timeout_retries_once_then_succeeds() -> None:
    """Retry a transport timeout once with the same payload and timeout budget."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Timeout the first attempt without making a network connection."""
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("private transport details", request=request)
        return success_response()

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        HTTPReplyModel(BASE_URL, API_KEY, MODEL, http_client=http) as model,
    ):
        assert await model.reply("问题") == "测试回复"

    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].extensions["timeout"] == requests[1].extensions["timeout"]


@pytest.mark.asyncio
async def test_async_timeout_bounds_each_of_two_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound a hung async transport even if it does not honor HTTPX timeouts."""
    calls = 0

    async def blocked(request: httpx.Request) -> httpx.Response:
        """Wait indefinitely until the model's own timeout cancels this attempt."""
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        raise AssertionError("Blocked transport must be cancelled")

    monkeypatch.setattr(desktop_llm, "REQUEST_TIMEOUT_SECONDS", 0.01)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as http,
        HTTPReplyModel(BASE_URL, API_KEY, MODEL, http_client=http) as model,
    ):
        async with asyncio.timeout(1):
            with pytest.raises(LLMError, match=r"2 attempts \(TimeoutError\)"):
                await model.reply("问题")

    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 429, 500])
async def test_http_failures_exhaust_exactly_one_retry(status_code: int) -> None:
    """Treat HTTP failures as bounded model errors, without returning error bodies."""
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        """Return one persistent upstream HTTP failure."""
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, json={"error": {"message": API_KEY}})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        HTTPReplyModel(BASE_URL, API_KEY, MODEL, http_client=http) as model,
    ):
        with pytest.raises(LLMError, match=r"2 attempts \(HTTPStatusError\)") as caught:
            await model.reply("问题")

    assert calls == 2
    assert caught.value.__context__ is None
    assert API_KEY not in str(caught.value)
    assert BASE_URL not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"error": {"message": "model rejected request"}},
        {"choices": None},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": " \n "}}]},
        {"choices": [{"message": {"content": ["not-text"]}}]},
    ],
)
async def test_malformed_success_payloads_are_retried_and_rejected(
    payload: Any,
) -> None:
    """Reject missing or empty textual choices after exactly two attempts."""
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        """Return a valid JSON document that violates the assistant reply contract."""
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        HTTPReplyModel(BASE_URL, API_KEY, MODEL, http_client=http) as model,
    ):
        with pytest.raises(LLMError, match=r"2 attempts \(ValueError\)"):
            await model.reply("问题")

    assert calls == 2


@pytest.mark.asyncio
async def test_invalid_json_retries_once_before_valid_response() -> None:
    """Recover from malformed JSON while keeping model data out of diagnostics."""
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        """Produce invalid JSON once and a valid assistant message next."""
        nonlocal calls
        calls += 1
        return (
            httpx.Response(200, content=b"not-json")
            if calls == 1
            else success_response()
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        HTTPReplyModel(BASE_URL, API_KEY, MODEL, http_client=http) as model,
    ):
        assert await model.reply("问题") == "测试回复"

    assert calls == 2


@pytest.mark.asyncio
async def test_logs_and_public_error_hide_private_values_and_keep_msg_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Suppress raw transport diagnostics and expose only safe correlated events."""
    private_content = "private-user-message"
    private_response = "private-model-response"
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        """Simulate unsafe transport logging and a failure carrying private data."""
        nonlocal calls
        calls += 1
        raw = f"{API_KEY} {BASE_URL} {private_content} {private_response}"
        logging.getLogger("httpcore.connection").debug(raw)
        logging.getLogger("httpx").warning(raw)
        raise RuntimeError(raw)

    initial_context = get_contextvars()
    httpx_backend = logging.getLogger("httpx")
    previous_level = httpx_backend.level
    previous_filters = list(httpx_backend.filters)
    with caplog.at_level(logging.DEBUG):
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
            HTTPReplyModel(BASE_URL, API_KEY, MODEL, http_client=http) as model,
        ):
            with bound_contextvars(msg_id="inbound-message-1"):
                with pytest.raises(LLMError) as caught:
                    await model.reply(private_content)
                assert get_contextvars()["msg_id"] == "inbound-message-1"
            logging.getLogger("httpx").warning("unrelated-http-log-after-call")

    assert calls == 2
    combined = str(caught.value) + caplog.text
    for private in (API_KEY, BASE_URL, private_content, private_response):
        assert private not in combined
    assert caught.value.__context__ is None
    assert "unrelated-http-log-after-call" in caplog.text
    assert httpx_backend.level == previous_level
    assert httpx_backend.filters == previous_filters
    assert get_contextvars() == initial_context
    events = log_events(caplog)
    assert len(events) == 3
    assert all(event["msg_id"] == "inbound-message-1" for event in events)
    attempts = [event for event in events if event["event"] == "llm_request_failed"]
    assert [(event["attempt"], event["retry"]) for event in attempts] == [
        (1, True),
        (2, False),
    ]


@pytest.mark.asyncio
async def test_transport_logging_in_other_tasks_is_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep unrelated HTTP diagnostics even while an LLM request is in progress."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        """Hold one mocked request open while another task emits a safe log."""
        entered.set()
        await release.wait()
        return success_response()

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        HTTPReplyModel(BASE_URL, API_KEY, MODEL, http_client=http) as model,
    ):
        with caplog.at_level(logging.INFO):
            task = asyncio.create_task(model.reply("问题"))
            await asyncio.wait_for(entered.wait(), timeout=1)
            logging.getLogger("httpx").warning("unrelated-task-http-log")
            release.set()
            assert await asyncio.wait_for(task, timeout=1) == "测试回复"

    assert "unrelated-task-http-log" in caplog.text
    assert BASE_URL not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry() -> None:
    """Preserve cancellation and restore transport logging context on exit."""
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        """Cancel from inside the mocked transport rather than return a reply."""
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    backend = logging.getLogger("httpx")
    previous_filters = list(backend.filters)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        HTTPReplyModel(BASE_URL, API_KEY, MODEL, http_client=http) as model,
    ):
        with pytest.raises(asyncio.CancelledError):
            await model.reply("问题")

    assert calls == 1
    assert backend.filters == previous_filters


@pytest.mark.parametrize("field", ["base_url", "api_key", "model"])
@pytest.mark.parametrize("value", ["", " \n "])
def test_empty_configuration_fields_fail_without_http(field: str, value: str) -> None:
    """Reject each missing configuration field with its name and no value echo."""
    arguments = {"base_url": BASE_URL, "api_key": API_KEY, "model": MODEL}
    arguments[field] = value

    with pytest.raises(LLMError, match=f"LLM {field} must not be empty"):
        HTTPReplyModel(**arguments)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:private@llm.test/v1",
        "https://@llm.test/v1",
        "https://llm.test/v1?api_key=private",
        "https://llm.test/v1?",
        "https://llm.test/v1#private",
        "ftp://llm.test/v1",
        "not-an-http-url",
    ],
)
def test_unsafe_base_urls_fail_without_echoing_configuration(base_url: str) -> None:
    """Disallow URL credentials, query strings, fragments and invalid schemes."""
    with pytest.raises(LLMError) as caught:
        HTTPReplyModel(base_url, API_KEY, MODEL)

    assert base_url not in str(caught.value)
    assert "private" not in str(caught.value)
    assert API_KEY not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "template_name",
    [
        "../desktop_reply.txt",
        "/tmp/desktop_reply.txt",
        "..\\desktop_reply.txt",
        "sub/desktop_reply.txt",
        "desktop_reply.txt/..",
        "desktop_reply.yaml",
        "",
    ],
)
def test_template_names_cannot_escape_packaged_prompts(template_name: str) -> None:
    """Reject every non-filename template before any resource or HTTP access."""
    with pytest.raises(LLMError, match="plain .txt filename"):
        HTTPReplyModel(BASE_URL, API_KEY, MODEL, template_name=template_name)


def test_missing_template_fails_with_safe_error() -> None:
    """Explain missing packaged resources without exposing paths or a cause."""
    with pytest.raises(LLMError, match="missing, empty or unreadable") as caught:
        HTTPReplyModel(BASE_URL, API_KEY, MODEL, template_name="missing-template.txt")

    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_configured_template_name_is_loaded_through_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Honor the caller-selected template name through the prompts package API."""
    template = resources.files("prompts").joinpath("desktop_reply.txt")
    loaded_names: list[str] = []
    packages: list[str] = []

    class PromptResources:
        """Observe resource selection while returning an existing packaged prompt."""

        def joinpath(self, name: str) -> Any:
            """Record the configured filename without adding another prompt source."""
            loaded_names.append(name)
            return template

    def prompt_resources(package: str) -> PromptResources:
        """Require the actual prompts package as the template source."""
        packages.append(package)
        return PromptResources()

    monkeypatch.setattr(desktop_llm.resources, "files", prompt_resources)
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: success_response())
        ) as http,
        HTTPReplyModel(
            BASE_URL,
            API_KEY,
            MODEL,
            template_name="configured-template.txt",
            http_client=http,
        ) as model,
    ):
        assert await model.reply("问题") == "测试回复"

    assert packages == ["prompts"]
    assert loaded_names == ["configured-template.txt"]


@pytest.mark.asyncio
async def test_injected_http_client_remains_open_after_model_scope() -> None:
    """Close the model operation without taking ownership of an injected client."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: success_response())
    ) as http:
        async with HTTPReplyModel(BASE_URL, API_KEY, MODEL, http_client=http) as model:
            assert await model.reply("问题") == "测试回复"
        assert not http.is_closed
        with pytest.raises(LLMError, match="closed"):
            await model.reply("问题")
        with pytest.raises(LLMError, match="closed"):
            async with model:
                raise AssertionError("Closed model must not enter again")


@pytest.mark.asyncio
async def test_owned_http_client_is_closed_after_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close an internally created client even when the caller's scope raises."""
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: success_response())
    )

    def constructor(**kwargs: Any) -> httpx.AsyncClient:
        """Replace only client construction so owned requests stay fully mocked."""
        assert kwargs == {"timeout": 10.0}
        return http

    monkeypatch.setattr(desktop_llm.httpx, "AsyncClient", constructor)
    with pytest.raises(RuntimeError, match="caller failure"):
        async with HTTPReplyModel(BASE_URL, API_KEY, MODEL) as model:
            assert await model.reply("问题") == "测试回复"
            raise RuntimeError("caller failure")

    assert http.is_closed
