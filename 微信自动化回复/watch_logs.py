"""Display Ri's durable receive/reply records and live errors without sending."""

import argparse
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Protocol

from dotenv import dotenv_values

from src.desktop_config import DesktopSettings

PERSONAL_CHAT = "desktop:wechat:Ri"


class TranscriptProtocol(Protocol):
    """Render changes to persisted message records without changing their state."""

    def update(self, rows: list[dict[str, Any]]) -> list[str]:
        """Return only newly visible receive/reply lines."""
        raise NotImplementedError


def redact_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Mask configured credentials and escape terminal control characters."""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(\b(?:access_token|api_key|secret|aes_key|token|password)\b\s*[:=]\s*)"
        r"""(?:"[^"]*"|'[^']*'|[^\s,;}]+)""",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    return "".join(
        f"\\x{ord(char):02x}"
        if (ord(char) < 32 and char not in "\n\t") or ord(char) == 127
        else char
        for char in text
    )


def format_record(
    row: dict[str, Any],
    *,
    include_received: bool,
    secrets: tuple[str, ...] = (),
) -> list[str]:
    """Label original time and distinguish submission, drafts and uncertainty."""
    status = row.get("status", "")
    if status == "baseline":
        return []
    label = row.get("time_display") or str(row.get("ts", "未知"))
    precision = "，分钟精度" if row.get("time_precision") == "minute" else ""
    stamp = f"{label}{precision}"
    lines: list[str] = []
    if include_received:
        lines.append(f"[收到][{stamp}] Ri：{row.get('content', '')}")
    reply = row.get("reply") or ""
    if status == "sent":
        lines.append(f"[已提交] 本人 → Ri：{reply}")
    elif status == "dry_run":
        lines.append(f"[DRY-RUN 草稿] 本人 → Ri：{reply}")
    elif status == "send_unknown":
        lines.append(f"[发送结果未知，不自动重发] 草稿：{reply}")
    elif status.endswith("failed"):
        lines.append(f"[处理失败] {status}：{row.get('error', '')}")
        if reply:
            lines.append(f"[未确认发送的草稿] {reply}")
    return [redact_text(line, secrets) for line in lines]


class TranscriptView:
    """Show one recent historical record, then only new receipts/status changes."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        """Retain display fingerprints independently of the sender's claims."""
        self._secrets = secrets
        self._seen: dict[str, tuple[Any, Any, Any]] = {}
        self._initialized = False

    def update(self, rows: list[dict[str, Any]]) -> list[str]:
        """Never replay messages, regenerate replies or modify the database."""
        visible = [row for row in rows if row.get("status") != "baseline"]
        lines: list[str] = []
        if not self._initialized:
            self._initialized = True
            for row in visible:
                self._seen[row["key"]] = self._signature(row)
            if visible:
                lines.append("[历史展示：最近一条记录，不会重发]")
                lines.extend(
                    format_record(
                        visible[-1], include_received=True, secrets=self._secrets
                    )
                )
            return lines
        for row in visible:
            key = row["key"]
            signature = self._signature(row)
            previous = self._seen.get(key)
            if previous != signature:
                lines.extend(
                    format_record(
                        row, include_received=previous is None, secrets=self._secrets
                    )
                )
                self._seen[key] = signature
        return lines

    @staticmethod
    def _signature(row: dict[str, Any]) -> tuple[Any, Any, Any]:
        """Track only visible outcome fields, keeping unchanged polls quiet."""
        return row.get("status"), row.get("reply"), row.get("error")


def read_records(db_path: Path) -> list[dict[str, Any]]:
    """Open SQLite in enforced read-only mode and select only verified Ri input."""
    connection = sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1
    )
    try:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM desktop_messages WHERE chat = ? AND sender = ? "
                "AND is_self = 0 AND chat_type = 'single' ORDER BY rowid",
                (PERSONAL_CHAT, PERSONAL_CHAT),
            )
        ]
    finally:
        connection.close()


def format_event(line: str, secrets: tuple[str, ...] = ()) -> str | None:
    """Render safe warning/error fields while hiding empty polling chatter."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or event.get("level") not in {"warning", "error"}:
        return None
    fields = " ".join(
        f"{key}={event[key]}"
        for key in ("event", "code", "error", "retry", "outcome_unknown")
        if key in event
    )
    return redact_text(f"[运行警告] {fields}", secrets)


def _configured_secrets() -> tuple[str, ...]:
    """Read redaction values privately without printing environment data."""
    values = {**dotenv_values(".env"), **os.environ}
    return tuple(
        value
        for name, value in values.items()
        if value
        and re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)$", name, re.IGNORECASE)
    )


def main() -> int:
    """Follow persisted messages and new runtime errors until Ctrl-C."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="只展示最近记录后退出")
    args = parser.parse_args()
    try:
        settings = DesktopSettings()
        secrets = _configured_secrets()
        view: TranscriptProtocol = TranscriptView(secrets)
        path = Path("logs/personal-live.log")
        offset = path.stat().st_size if path.exists() else 0
        print("企业微信 Ri 实时消息与回复（只读查看，不发送消息）", flush=True)
        print("已提交 = 本机出站气泡已确认；不代表对方已读。", flush=True)
        print("Ctrl-C 只关闭此日志窗口，自动回复程序继续运行。\n", flush=True)
        last_error = ""
        while True:
            try:
                lines = view.update(read_records(settings.sqlite_path))
                if path.exists():
                    if path.stat().st_size < offset:
                        offset = 0
                    with path.open(encoding="utf-8") as stream:
                        stream.seek(offset)
                        for line in stream:
                            rendered = format_event(line, secrets)
                            if rendered:
                                lines.append(rendered)
                        offset = stream.tell()
                for line in lines:
                    print(line, flush=True)
                last_error = ""
            except (sqlite3.Error, OSError) as exc:
                error = type(exc).__name__
                if error != last_error:
                    print(f"[日志暂不可读] {error}，稍后自动重试。", flush=True)
                last_error = error
            if args.once:
                return int(bool(last_error))
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n日志查看已停止；自动回复程序不受影响。", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - Configuration errors must not expose credentials.
        print(f"日志查看启动失败：{type(exc).__name__}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
