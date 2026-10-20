"""Follow append-only personal snapshots without turning older history into new input."""

import asyncio
from collections.abc import Callable
from typing import Protocol

from adapters.inbox import Inbox, InboxError, Msg

_ERRORS = {
    "EMPTY_BASELINE": "个人聊天启动快照为空；未建立消息基线。",
    "TAIL_DISCONTINUITY": "个人聊天记录不再是已确认快照的尾部追加；游标未推进。",
    "BASELINE_REQUIRED": "请先建立个人聊天消息基线。",
    "SOURCE_READ_FAILED": "个人聊天只读快照获取失败；游标未推进。",
    "INVALID_SNAPSHOT": "个人聊天快照格式无效；游标未推进。",
    "KEY_FAILED": "个人聊天消息指纹生成失败；游标未推进。",
}


class TailError(InboxError):
    """Expose a fixed allowlisted code without raw history or source diagnostics."""

    def __init__(self, code: str) -> None:
        """Discard unknown codes and keep private source values out of errors."""
        self.code: str = code if code in _ERRORS else "SOURCE_READ_FAILED"
        super().__init__(_ERRORS[self.code])


class TailProtocol(Inbox, Protocol):
    """Require an explicit baseline before receive-only append polling."""

    async def baseline(self) -> list[Msg]:
        """Return history to baseline once, retaining any suffix after a known anchor."""
        raise NotImplementedError


class PersonalTailInbox:
    """Serialize baseline and polling while accepting only an unchanged prefix.

    A successful baseline is idempotent. The first poll consumes its buffered
    suffix without rereading. Read failures and cancellation leave the last
    accepted snapshot unchanged. A polling discontinuity recovers by position:
    re-anchor at the desktop owner's latest outgoing message and refeed the
    non-own messages after it, letting the store's claim() deduplicate.
    """

    def __init__(
        self,
        source: Inbox,
        *,
        known_keys: frozenset[str] = frozenset(),
        key_of: Callable[[Msg], str],
    ) -> None:
        """Inject stable fingerprints without importing loop or storage modules."""
        self._source: Inbox = source
        self._known_keys: frozenset[str] = frozenset(known_keys)
        self._key_of: Callable[[Msg], str] = key_of
        self._baseline: tuple[Msg, ...] | None = None
        self._last_keys: tuple[str, ...] | None = None
        self._pending: tuple[Msg, ...] | None = None
        self._lock = asyncio.Lock()

    async def _read_snapshot(self) -> tuple[tuple[Msg, ...], tuple[str, ...]]:
        """Build a complete local candidate before changing any durable-tail state."""
        failed = False
        messages: list[Msg] | None = None
        try:
            messages = await self._source.poll_unread()
        except Exception:  # noqa: BLE001 -- Source exceptions can contain private history.
            failed = True
        if failed:
            raise TailError("SOURCE_READ_FAILED")
        if not isinstance(messages, list) or not all(
            isinstance(message, Msg) for message in messages
        ):
            raise TailError("INVALID_SNAPSHOT")
        snapshot = tuple(messages)
        keys: tuple[str, ...] = ()
        try:
            keys = tuple(self._key_of(message) for message in snapshot)
        except Exception:  # noqa: BLE001 -- Fingerprint failures must not expose inputs.
            failed = True
        if failed or any(not isinstance(key, str) or not key for key in keys):
            raise TailError("KEY_FAILED")
        # Disambiguate identical fingerprints inside one snapshot (for example
        # the same minute-precision message resent within the same minute):
        # the first occurrence stays untouched, later copies carry a suffixed
        # dedupe_key so the store's claim() cannot swallow them as duplicates.
        # Append-only snapshots keep each row's occurrence ordinal stable
        # across reads and restarts.
        seen: dict[str, int] = {}
        unique_messages: list[Msg] = []
        unique_keys: list[str] = []
        for message, key in zip(snapshot, keys, strict=True):
            occurrence = seen.get(key, 0) + 1
            seen[key] = occurrence
            if occurrence == 1:
                unique_messages.append(message)
                unique_keys.append(key)
                continue
            unique_key = f"{key}#{occurrence}"
            unique_messages.append(
                message.model_copy(update={"dedupe_key": unique_key})
            )
            unique_keys.append(unique_key)
        return tuple(unique_messages), tuple(unique_keys)

    async def baseline(self) -> list[Msg]:
        """Read once successfully and preserve new messages after the last known key."""
        async with self._lock:
            if self._baseline is not None:
                return list(self._baseline)
            snapshot, keys = await self._read_snapshot()
            if not snapshot:
                raise TailError("EMPTY_BASELINE")
            cut = len(snapshot)
            if self._known_keys:
                matches = [
                    index for index, key in enumerate(keys) if key in self._known_keys
                ]
                if not matches:
                    raise TailError("TAIL_DISCONTINUITY")
                cut = matches[-1] + 1
            prefix = snapshot[:cut]
            self._baseline = prefix
            self._last_keys = keys
            self._pending = snapshot[cut:]
            return list(prefix)

    @staticmethod
    def _recover_after_last_own(snapshot: tuple[Msg, ...]) -> tuple[Msg, ...]:
        """Re-anchor after a discontinuity at the owner's latest own message.

        Non-own messages after that point are unanswered candidates. The
        store's claim() already deduplicates anything handled before, so
        refeeding is safe and no incoming message is silently dropped.
        """
        anchor = -1
        for index, message in enumerate(snapshot):
            if message.is_self:
                anchor = index
        return snapshot[anchor + 1 :]

    async def poll_unread(self) -> list[Msg]:
        """Return buffered new input once, then strict tail appends.

        When the snapshot is no longer a strict append, recover by position
        instead of stalling until a manual restart.
        """
        async with self._lock:
            if self._last_keys is None:
                raise TailError("BASELINE_REQUIRED")
            if self._pending is not None:
                pending = self._pending
                self._pending = None
                return list(pending)
            snapshot, keys = await self._read_snapshot()
            if keys == self._last_keys:
                return []
            length = len(self._last_keys)
            if len(keys) > length and keys[:length] == self._last_keys:
                appended = snapshot[length:]
            else:
                appended = self._recover_after_last_own(snapshot)
            self._last_keys = keys
            return list(appended)
