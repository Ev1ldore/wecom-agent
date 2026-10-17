"""Validated messages exchanged between the WeCom gateway and agent."""

from typing import Any, Self

from pydantic import BaseModel, Field, model_validator


class WecomMessage(BaseModel):
    """A decrypted WeCom message normalized for this phase's single chats."""

    msg_id: str = Field(
        default="",
        description="Deduplication key; events without an ID use from_user:ts.",
    )
    msg_type: str = Field(
        description="Message type: text, voice, image, file or event."
    )
    from_user: str = Field(description="WeCom user ID of the sender.")
    chat_type: str = Field(default="single", description="Always single in this phase.")
    content: str | None = Field(
        default=None,
        description="Normalized text content; non-text messages use None.",
    )
    raw: dict[str, Any] = Field(
        description="The original decrypted message dictionary."
    )
    ts: int = Field(description="Message creation time as a Unix timestamp.")

    @model_validator(mode="after")
    def normalize_message(self) -> Self:
        """Generate event IDs and apply this phase's chat and content rules."""
        if not self.msg_id:
            if self.msg_type != "event":
                raise ValueError("msg_id is required for non-event messages")
            self.msg_id = f"{self.from_user}:{self.ts}"
        self.chat_type = "single"
        if self.msg_type != "text":
            self.content = None
        return self


class AgentReply(BaseModel):
    """An agent's textual reply and whether a human handoff is required."""

    text: str = Field(description="Reply text to send to the user.")
    need_human: bool = Field(default=False, description="Request human intervention.")
