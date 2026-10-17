"""Define the normalized inbox contract shared by CLI, desktop and test sources."""

from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Msg(BaseModel):
    """Represent a message directed to the agent, independent of desktop direction.

    ``is_self`` identifies the replying identity's own messages. In application
    mode the desktop owner can be the requester; in personal mode the desktop
    owner's outgoing messages must be ignored.

    Second precision requires the original positive integer timestamp. Minute
    precision retains the complete source display label and has no epoch value.
    """

    model_config = ConfigDict(frozen=True)

    chat: str = Field(min_length=1)
    sender: str = Field(min_length=1)
    content: str
    ts: int | None = Field(gt=0, strict=True)
    time_precision: Literal["second", "minute"] = "second"
    time_display: str | None = None
    chat_type: Literal["single", "group"] = "single"
    is_self: bool = False

    @model_validator(mode="after")
    def validate_time_precision(self) -> Self:
        """Reject missing or fabricated time without changing observed labels."""
        if self.time_precision == "second":
            if self.ts is None:
                raise ValueError("Second precision requires an original timestamp")
        elif self.ts is not None or not (
            self.time_display is not None and self.time_display.strip()
        ):
            raise ValueError(
                "Minute precision requires ts=None and source time_display"
            )
        return self


class Inbox(Protocol):
    """Expose only bounded, receive-only polling of normalized messages."""

    async def poll_unread(self) -> list[Msg]:
        """Return observable messages; callers must still filter and deduplicate."""
        raise NotImplementedError


class InboxError(Exception):
    """Report unavailable permissions, unsupported layouts or CLI failures safely."""


def application_chat(agent_id: int, user_id: str) -> str:
    """Identify one user's single conversation with one official application."""
    if agent_id <= 0 or not user_id.strip():
        raise ValueError("A positive agent_id and an actual user_id are required")
    return f"app:{agent_id}:{user_id}"
