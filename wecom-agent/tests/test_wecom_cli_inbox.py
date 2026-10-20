"""Verify receive-only CLI polling with fake runners and fake child processes."""

import asyncio
import json
from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest

from adapters import wecom_cli as module
from adapters.inbox import Inbox, application_chat
from adapters.wecom_cli import (
    CLIInboxError,
    SubprocessCLIRunner,
    WecomCLIInbox,
    stop_cli_process,
)

ZONE = ZoneInfo("Asia/Shanghai")
START = int(datetime(2026, 10, 17, 12, tzinfo=ZONE).timestamp())
CHAT = application_chat(1000001, "actual-user")
RAW_CHAT = "opaque-chat-with-exact-case"
RAW_SENDER = "opaque-encrypted-user"


class FakeRunner:
    """Record argv and return explicitly supplied public-schema fixture pages."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> dict[str, Any]:
        self.calls.append(argv)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def message(second: int = 10, **updates: Any) -> dict[str, Any]:
    """Build a synthetic message using the actual discovery field names."""
    result = {
        "userid": RAW_SENDER,
        "user_name": "Fixture user",
        "msg_type": "text",
        "send_time": datetime.fromtimestamp(START + second, ZONE).strftime(
            module.TIME_FORMAT
        ),
        "text": {"content": "hello CLI"},
    }
    result.update(updates)
    return result


def page(rows: list[Any], *, more: bool = False, cursor: str = "") -> dict[str, Any]:
    """Build one complete, synthetic GetMessagesRsp page."""
    return {
        "messages": rows,
        "messages_count": len(rows),
        "has_more": more,
        "next_cursor": cursor,
    }


def inbox(runner: FakeRunner, **updates: Any) -> WecomCLIInbox:
    """Use verified fixture mappings, never a real account or chat."""
    options = {
        "chat_id": RAW_CHAT,
        "sender_id": RAW_SENDER,
        "normalized_chat": CHAT,
        "verified_single_chat": True,
        "start_at": START,
        "clock": lambda: START + 20,
        "runner": runner,
    }
    options.update(updates)
    return WecomCLIInbox(**options)


@pytest.mark.asyncio
async def test_exact_schema_mapping_and_receive_only_argv() -> None:
    runner = FakeRunner([page([message()])])
    source: Inbox = inbox(runner)
    result = await source.poll_unread()
    assert len(result) == 1
    assert result[0].model_dump() == {
        "chat": CHAT,
        "sender": "LiChengYun",
        "content": "hello CLI",
        "ts": START + 10,
        "time_precision": "second",
        "time_display": None,
        "chat_type": "single",
        "is_self": False,
        "dedupe_key": None,
    }
    argv = runner.calls[0]
    assert argv[:5] == ["wecom-cli", "chat", "messages", "list", "--json"]
    assert len(argv) == 6
    assert json.loads(argv[5]) == {
        "chat_id": RAW_CHAT,
        "begin_time": "2026-10-17 12:00:00",
        "end_time": "2026-10-17 12:00:20",
    }
    assert RAW_SENDER not in (result[0].sender, result[0].chat)


@pytest.mark.parametrize(
    "updates",
    [
        {"verified_single_chat": False},
        {"verified_single_chat": 1},
        {"chat_id": ""},
        {"sender_id": " "},
        {"normalized_chat": ""},
        {"normalized_sender": ""},
        {"timezone_name": "Invalid/Timezone"},
    ],
)
def test_constructor_requires_verified_mapping(updates: dict[str, Any]) -> None:
    runner = FakeRunner([])
    with pytest.raises(ValueError):
        inbox(runner, **updates)
    assert not runner.calls


@pytest.mark.asyncio
async def test_filters_exact_sender_non_text_and_history() -> None:
    runner = FakeRunner(
        [
            page(
                [
                    message(userid=RAW_SENDER.upper()),
                    message(msg_type="image", text=None),
                    message(-1),
                    message(30),
                    message(),
                ]
            )
        ]
    )
    assert len(await inbox(runner).poll_unread()) == 1


@pytest.mark.asyncio
async def test_default_start_rounds_up_and_same_second_does_not_poll() -> None:
    now = [START + 0.25]
    runner = FakeRunner([page([message(0), message(1)])])
    source = inbox(runner, start_at=None, clock=lambda: now[0])
    assert await source.poll_unread() == []
    assert not runner.calls
    now[0] = START + 1.75
    assert await source.poll_unread() == []
    now[0] = START + 2
    assert [item.ts for item in await source.poll_unread()] == [START + 1]
    assert json.loads(runner.calls[0][5])["begin_time"] == "2026-10-17 12:00:01"


@pytest.mark.asyncio
async def test_explicit_timezone_controls_request_and_response_conversion() -> None:
    runner = FakeRunner([page([message(send_time="2026-10-17 04:00:10")])])
    result = await inbox(runner, timezone_name="UTC").poll_unread()
    assert result[0].ts == START + 10
    assert json.loads(runner.calls[0][5])["begin_time"] == "2026-10-17 04:00:00"


@pytest.mark.asyncio
async def test_success_overlaps_five_seconds_and_failure_keeps_watermark() -> None:
    now = [START + 20]
    runner = FakeRunner([page([]), RuntimeError("sensitive failure body"), page([])])
    source = inbox(runner, clock=lambda: now[0])
    assert await source.poll_unread() == []
    now[0] = START + 25
    with pytest.raises(CLIInboxError, match="RuntimeError") as exc:
        await source.poll_unread()
    assert "sensitive" not in str(exc.value)
    assert await source.poll_unread() == []
    failed, retried = (json.loads(call[5]) for call in runner.calls[1:])
    assert failed == retried
    assert retried["begin_time"] == "2026-10-17 12:00:15"


@pytest.mark.asyncio
async def test_full_pagination_preserves_query_and_sorts_reversed_pages() -> None:
    runner = FakeRunner(
        [
            page([message(18)], more=True, cursor="next-page"),
            page([message(2)]),
        ]
    )
    result = await inbox(runner).poll_unread()
    assert [item.ts for item in result] == [START + 2, START + 18]
    first, second = (json.loads(call[5]) for call in runner.calls)
    assert second == {**first, "cursor": "next-page"}


@pytest.mark.asyncio
async def test_observed_zero_message_shape_allows_omitted_empty_fields() -> None:
    """An explicit empty terminal response may omit messages and next_cursor."""
    runner = FakeRunner(
        [
            {
                "extra_identity_context": {},
                "has_more": False,
                "messages_count": 0,
                "security_notice": "Synthetic public-schema fixture",
            },
            page([]),
        ]
    )
    now = [START + 20]
    source = inbox(runner, clock=lambda: now[0])
    assert await source.poll_unread() == []
    now[0] = START + 25
    assert await source.poll_unread() == []
    assert json.loads(runner.calls[1][5])["begin_time"] == "2026-10-17 12:00:15"


@pytest.mark.asyncio
async def test_terminal_nonempty_page_may_omit_next_cursor() -> None:
    response = page([message()])
    del response["next_cursor"]
    assert len(await inbox(FakeRunner([response])).poll_unread()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"messages_count": 1, "has_more": False},
        {"messages_count": 0, "has_more": True, "next_cursor": "next"},
        {"messages_count": 0, "has_more": True, "messages": []},
        {"messages_count": 0, "has_more": False, "messages": None},
        {"messages_count": 0, "has_more": False, "next_cursor": None},
        {"messages_count": False, "has_more": False},
        {"messages_count": "0", "has_more": False},
        {"has_more": False},
        {"messages_count": 0},
        {"messages_count": 0, "has_more": 0},
    ],
)
async def test_unobserved_or_ambiguous_omissions_remain_invalid(
    response: dict[str, Any],
) -> None:
    with pytest.raises(CLIInboxError):
        await inbox(FakeRunner([response])).poll_unread()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        [page([message()], more=True)],
        [page([], more=True, cursor="same"), page([], more=True, cursor="same")],
        [page([], more=True, cursor=str(index)) for index in range(10)],
        [{"messages": [], "has_more": "false", "next_cursor": ""}],
        [page([message(text={"wrong": "field"})])],
        [page([message(send_time="1700000000")])],
    ],
)
async def test_invalid_or_incomplete_windows_fail_closed(responses: list[Any]) -> None:
    runner = FakeRunner(responses)
    with pytest.raises(CLIInboxError):
        await inbox(runner).poll_unread()


@pytest.mark.asyncio
async def test_page_failure_restarts_entire_window() -> None:
    runner = FakeRunner(
        [
            page([message()], more=True, cursor="second"),
            CLIInboxError("failed", code=853006),
            page([message()], more=True, cursor="second"),
            page([message(11)]),
        ]
    )
    source = inbox(runner)
    with pytest.raises(CLIInboxError) as exc:
        await source.poll_unread()
    assert exc.value.code == 853006
    assert len(await source.poll_unread()) == 2
    assert runner.calls[0] == runner.calls[2]


class FakeChild:
    """Mimic communicate/kill without spawning or reading a real CLI process."""

    registry: ClassVar[dict[int, "FakeChild"]] = {}

    def __init__(
        self, *, block: bool = False, output: bytes = b"{}", status: int = 0
    ) -> None:
        self.block = block
        self.pid = 900000 + len(self.registry)
        self.registry[self.pid] = self
        self.output = output
        self.returncode: int | None = None
        self.status = status
        self.killed = False
        self.calls = 0
        self.started = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.calls += 1
        self.started.set()
        if self.block and not self.killed:
            await asyncio.Event().wait()
        self.returncode = -9 if self.killed else self.status
        return self.output, b"secret stderr must never be logged"

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


@pytest.fixture(autouse=True)
def fake_process_groups(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Intercept every group signal; these tests must never kill a real process."""
    FakeChild.registry = {}
    signals: list[tuple[int, int]] = []

    def kill_group(pid: int, sig: int) -> None:
        assert pid in FakeChild.registry, (
            "Only this test's fake children may be signalled"
        )
        signals.append((pid, sig))
        FakeChild.registry[pid].kill()

    monkeypatch.setattr(module.os, "killpg", kill_group, raising=False)
    return signals


CLI_ARGV = ["wecom-cli", "chat", "messages", "list", "--json", "{}"]


@pytest.mark.asyncio
async def test_subprocess_timeout_retries_once_and_reaps_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children: list[FakeChild] = []

    async def spawn(*argv: str, **kwargs: Any) -> FakeChild:
        assert list(argv) == CLI_ARGV
        assert kwargs["stdout"] == kwargs["stderr"] == asyncio.subprocess.PIPE
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        assert "WECOM_CLI_LOG_DIR" not in kwargs["env"]
        assert kwargs["env"]["WECOM_CLI_LOG_LEVEL"] == "warn"
        assert kwargs["start_new_session"] is module.IS_POSIX
        child = FakeChild(block=True)
        children.append(child)
        return child

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "CLI_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(CLIInboxError, match="TimeoutError"):
        await SubprocessCLIRunner()(CLI_ARGV)
    assert len(children) == 2
    assert all(child.killed and child.calls == 2 for child in children)


@pytest.mark.asyncio
async def test_cancellation_cleans_up_child_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = FakeChild(block=True)
    count = 0

    async def spawn(*argv: str, **kwargs: Any) -> FakeChild:
        nonlocal count
        count += 1
        return child

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(SubprocessCLIRunner()(CLI_ARGV))
    await child.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert count == 1 and child.killed and child.calls == 2


@pytest.mark.asyncio
async def test_subprocess_error_code_is_retained_but_body_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    children: list[FakeChild] = []

    async def spawn(*argv: str, **kwargs: Any) -> FakeChild:
        child = FakeChild(
            output=b'{"error":{"code":893201,"message":"secret response"}}', status=1
        )
        children.append(child)
        return child

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(CLIInboxError) as exc:
        await SubprocessCLIRunner()(CLI_ARGV)
    assert exc.value.code == 893201
    assert len(children) == 2
    assert "secret" not in str(exc.value) + caplog.text


@pytest.mark.asyncio
async def test_subprocess_runner_refuses_auth_and_send_commands() -> None:
    for command in (
        ["wecom-cli", "auth", "init"],
        ["wecom-cli", "message", "aibot", "send", "--json", "{}"],
    ):
        with pytest.raises(CLIInboxError, match="Unsupported"):
            await SubprocessCLIRunner()(command)


@pytest.mark.asyncio
async def test_group_cleanup_includes_descendants_after_parent_exit(
    fake_process_groups: list[tuple[int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exited Node wrapper must not leave its Rust child's pipes open."""
    monkeypatch.setattr(module, "IS_POSIX", True)
    child = FakeChild()
    child.returncode = 0
    await stop_cli_process(child, isolated_session=True)
    assert fake_process_groups == [(child.pid, module.signal.SIGKILL)]
    assert child.killed


@pytest.mark.asyncio
async def test_cleanup_refuses_host_process_group(
    monkeypatch: pytest.MonkeyPatch,
    fake_process_groups: list[tuple[int, int]],
) -> None:
    monkeypatch.setattr(module, "IS_POSIX", True)
    child = FakeChild()
    monkeypatch.setattr(module.os, "getpgrp", lambda: child.pid)
    with pytest.raises(CLIInboxError, match="unsafe"):
        await stop_cli_process(child, isolated_session=True)
    assert not fake_process_groups
    assert not child.killed


@pytest.mark.asyncio
async def test_windows_cleanup_uses_only_child_kill(
    monkeypatch: pytest.MonkeyPatch,
    fake_process_groups: list[tuple[int, int]],
) -> None:
    monkeypatch.setattr(module, "IS_POSIX", False)
    child = FakeChild()
    await stop_cli_process(child, isolated_session=True)
    assert child.killed
    assert not fake_process_groups


@pytest.mark.asyncio
async def test_cleanup_has_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even an unresponsive drain cannot hang timeout or cancellation cleanup."""

    class StuckChild(FakeChild):
        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b"", b""

    child = StuckChild()
    monkeypatch.setattr(module, "CLI_CLEANUP_SECONDS", 0.01)
    async with asyncio.timeout(1):
        with pytest.raises(CLIInboxError, match="Timed out reaping"):
            await stop_cli_process(child, isolated_session=module.IS_POSIX)
    assert child.killed
