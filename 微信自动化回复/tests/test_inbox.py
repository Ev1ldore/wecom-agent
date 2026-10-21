"""Check normalized message identity and the receive-only adapter contract."""

import pytest
from pydantic import ValidationError

from adapters.inbox import Inbox, Msg, application_chat


@pytest.mark.asyncio
async def test_fake_inbox_can_return_a_human_request_from_the_desktop_owner() -> None:
    """A human's outgoing desktop bubble is still a request to the application."""
    message = Msg(
        chat=application_chat(1000002, "LiChengYun"),
        sender="LiChengYun",
        content="你好",
        ts=1700000000,
    )

    class FakeInbox:
        """Return a stable snapshot without external I/O."""

        async def poll_unread(self) -> list[Msg]:
            """Return one normalized human request."""
            return [message]

    inbox: Inbox = FakeInbox()
    assert await inbox.poll_unread() == [message]
    assert not message.is_self


def test_agent_reply_is_explicitly_distinct_from_requester() -> None:
    """Retain the application identity needed to prevent a reply loop."""
    message = Msg(
        chat="app:1000002:LiChengYun",
        sender="app:1000002",
        content="你好",
        ts=1700000000,
        is_self=True,
    )
    assert message.is_self and message.sender != "LiChengYun"


def test_unknown_message_types_and_invalid_timestamps_are_rejected() -> None:
    """Do not let malformed adapter output silently enter the single-chat loop."""
    with pytest.raises(ValidationError):
        Msg(chat="chat", sender="user", content="text", ts=0)
    with pytest.raises(ValidationError):
        Msg(
            chat="chat",
            sender="user",
            content="text",
            ts=1700000000,
            chat_type="unknown",
        )


def test_second_precision_remains_the_default() -> None:
    """Existing callers retain an explicit original second-level timestamp."""
    message = Msg(chat="chat", sender="user", content="text", ts=1700000000)
    assert message.ts == 1700000000
    assert message.time_precision == "second"
    assert message.time_display is None


@pytest.mark.parametrize("timestamp", [None, True, False, 1.0, 1.5, "1700000000", -1])
def test_second_precision_rejects_missing_or_coerced_timestamps(
    timestamp: object,
) -> None:
    """A valid timestamp must already be a positive integer, excluding bools."""
    with pytest.raises(ValidationError):
        Msg(chat="chat", sender="user", content="text", ts=timestamp)


@pytest.mark.parametrize("display", ["昨天12:11", "11:43", "  10月16日 12:11  "])
def test_minute_precision_preserves_the_complete_display_label(display: str) -> None:
    """Retain observed text without adding a date, seconds or an epoch value."""
    message = Msg(
        chat="Ri",
        sender="Ri",
        content="你好",
        ts=None,
        time_precision="minute",
        time_display=display,
    )
    assert message.ts is None
    assert message.time_precision == "minute"
    assert message.time_display == display
    assert Msg.model_validate_json(message.model_dump_json()) == message


@pytest.mark.parametrize("display", [None, "", " \t\n"])
def test_minute_precision_requires_a_nonempty_display_label(
    display: str | None,
) -> None:
    """Missing source time text cannot be replaced with an observation clock."""
    with pytest.raises(ValidationError):
        Msg(
            chat="Ri",
            sender="Ri",
            content="你好",
            ts=None,
            time_precision="minute",
            time_display=display,
        )


def test_minute_precision_rejects_an_invented_epoch_timestamp() -> None:
    """A minute label must never coexist with fabricated second-level precision."""
    with pytest.raises(ValidationError):
        Msg(
            chat="Ri",
            sender="Ri",
            content="你好",
            ts=1700000000,
            time_precision="minute",
            time_display="昨天12:11",
        )
