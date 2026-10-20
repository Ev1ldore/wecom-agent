"""Run local receive/filter/model/SQLite with opt-in personal-chat sending."""

import argparse
import asyncio
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from pydantic import ValidationError
from structlog.contextvars import bound_contextvars

from adapters.inbox import InboxError
from adapters.wecom_cli import IS_POSIX, stop_cli_process
from src.core.desktop_llm import HTTPReplyModel, LLMError
from src.core.desktop_loop import AgentLoop, LoopProtocol, ReplyPreviewProtocol
from src.core.desktop_store import SQLiteStore
from src.core.logging import get_logger
from src.desktop_config import DesktopSettings

logger = get_logger(__name__)


class LocalRuntime(Protocol):
    """Define a local CLI entry operation without requiring live service calls."""

    async def __call__(self, settings: DesktopSettings, *, once: bool) -> int:
        """Run the selected local inbox and return an exit code."""
        raise NotImplementedError


class SetupError(Exception):
    """Report fixed, credential-free instructions for incomplete local setup."""


class DisabledSender:
    """Guard this delivery against any accidental real send, including future bugs."""

    async def send_text(self, user_id: str, content: str) -> None:
        """Reject every call; the default dry-run loop must never invoke this."""
        raise RuntimeError("Real sending is disabled in this dry-run delivery")


async def _require_cli_authorized() -> None:
    """Inspect local CLI status with a timeout and one transport/process retry."""
    executable = shutil.which("wecom-cli")
    if executable is None:
        raise SetupError("未安装 wecom-cli；请先运行 npm install -g @wecom/cli")
    environment = os.environ.copy()
    environment.pop("WECOM_CLI_LOG_DIR", None)
    environment["WECOM_CLI_LOG_LEVEL"] = "warn"
    for attempt in range(2):
        process: asyncio.subprocess.Process | None = None
        try:
            async with asyncio.timeout(10):
                process = await asyncio.create_subprocess_exec(
                    executable,
                    "auth",
                    "show",
                    "--status",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    env=environment,
                    start_new_session=IS_POSIX,
                )
                stdout, _ = await process.communicate()
                status = stdout.decode("utf-8").strip()
                if process.returncode == 0 and status == "authorized":
                    return
                if process.returncode == 0 and status == "unauthorized":
                    raise SetupError(
                        "CLI 未授权；请本人运行 wecom-cli auth init 并扫码确认"
                    )
        except SetupError:
            raise
        except (OSError, TimeoutError, UnicodeError):
            pass
        finally:
            if process is not None:
                await stop_cli_process(process, isolated_session=IS_POSIX)
        if attempt == 1:
            raise SetupError("无法确认 CLI 授权状态；已重试 1 次，请检查本机 CLI")


async def _first_window_ready() -> None:
    """Allow a fresh second-resolution CLI window to have a later end time."""
    await asyncio.sleep(2)


async def run_cycles(
    loop: LoopProtocol,
    *,
    once: bool,
    interval: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Keep polling on safe inbox failures, with bounded pacing and Ctrl-C exit."""
    while True:
        try:
            stats = await loop.run_once()
        except InboxError as exc:
            code = getattr(exc, "code", None)
            logger.error(
                "local_inbox_unavailable",
                msg_id=None,
                error=type(exc).__name__,
                code=code,
            )
            print("收消息失败：请检查所选接入及已验证的单聊窗口；未发送消息。")
            if once:
                return 1
        else:
            logger.info("local_poll_done", msg_id=None, **stats)
            if once:
                return int(
                    any(
                        stats.get(field, 0)
                        for field in ("llm_failed", "send_failed", "emit_failed")
                    )
                )
        await sleep(interval)


async def run_cli(settings: DesktopSettings, *, once: bool) -> int:
    """Poll only an explicitly mapped CLI single chat and record dry-run results."""
    if not settings.dry_run:
        raise SetupError("本次交付仅支持 DRY_RUN=true；真实发送尚未启用")
    if settings.inbox_backend != "cli":
        raise SetupError("演示请运行 --demo；真实收消息当前使用 INBOX_BACKEND=cli")
    if not (
        settings.inbox_chat_id
        and settings.inbox_sender_id
        and settings.inbox_verified_single_chat
    ):
        raise SetupError(
            "需填写 INBOX_CHAT_ID、INBOX_SENDER_ID，并核实后设 INBOX_VERIFIED_SINGLE_CHAT=true；禁止猜测 ID 或将群聊标为单聊"
        )
    await _require_cli_authorized()
    from adapters.wecom_cli import WecomCLIInbox

    async with HTTPReplyModel(
        settings.llm_base_url,
        settings.llm_api_key.get_secret_value(),
        settings.llm_model,
        template_name=settings.llm_template_name,
    ) as model:
        store = SQLiteStore(settings.sqlite_path)
        try:
            records = await store.records()
            previous = [
                row["ts"]
                for row in records
                if row["chat"] == settings.allowed_chat
                and row["sender"] == settings.allowed_user_id
                and type(row["ts"]) is int
            ]
            start_at = None
            if previous:
                oldest_supported = time.time() - 7 * 86400 + 60
                start_at = max(
                    max(previous) - settings.dedup_bucket_seconds, oldest_supported
                )
                if max(previous) - settings.dedup_bucket_seconds < oldest_supported:
                    logger.warning("cli_history_limited_to_seven_days", msg_id=None)
            inbox = WecomCLIInbox(
                chat_id=settings.inbox_chat_id,
                sender_id=settings.inbox_sender_id,
                normalized_chat=settings.allowed_chat,
                normalized_sender=settings.allowed_user_id,
                verified_single_chat=settings.inbox_verified_single_chat,
                timezone_name=settings.inbox_timezone,
                start_at=start_at,
            )
            loop = AgentLoop(
                inbox,
                model,
                DisabledSender(),
                store,
                allowed_chat=settings.allowed_chat,
                allowed_sender=settings.allowed_user_id,
                self_senders=frozenset({f"app:{settings.wecom_agent_id}"}),
                dry_run=True,
                bucket_seconds=settings.dedup_bucket_seconds,
            )
            print("DRY-RUN 已开启：仅处理已映射的指定单聊；真实发送被入口禁用。")
            if once and start_at is None:
                await _first_window_ready()
            return await run_cycles(
                loop, once=once, interval=settings.poll_interval_seconds
            )
        finally:
            await store.aclose()


async def run_personal(settings: DesktopSettings, *, once: bool) -> int:
    """Reply to Ri appends, baselining all visible history before live sending."""
    if settings.personal_contact_name != "Ri":
        raise SetupError("当前仅验证了 Ri 的外部微信单聊")
    from adapters.personal_refresh import PersonalRefreshingReader
    from adapters.personal_reply import PersonalDesktopReplyChannel
    from adapters.personal_tail import PersonalTailInbox
    from adapters.wecom_personal import PersonalHistoryInbox
    from src.core.desktop_loop import message_key

    store = SQLiteStore(settings.sqlite_path)
    preview = PersonalDesktopReplyChannel(live_enabled=not settings.dry_run)
    try:
        known: frozenset[str] = frozenset()
        if settings.dry_run:
            await _recover_personal_drafts(settings, store, preview)
            known = frozenset(
                row["key"]
                for row in await store.records()
                if row["chat"] == settings.personal_chat
                and row["sender"] == settings.personal_chat
                and not row["is_self"]
            )
        inbox = PersonalTailInbox(
            PersonalHistoryInbox(PersonalRefreshingReader()),
            known_keys=known,
            key_of=lambda msg: message_key(msg, settings.dedup_bucket_seconds),
        )
        initial = await inbox.baseline()
        baseline_count = 0
        for msg in initial:
            if (
                msg.chat == settings.personal_chat
                and msg.sender == settings.personal_chat
                and msg.chat_type == "single"
                and not msg.is_self
            ):
                key = msg.dedupe_key or message_key(msg, settings.dedup_bucket_seconds)
                if await store.claim(msg, key):
                    await store.finish(key, "baseline")
                    baseline_count += 1
        mode = "dry-run" if settings.dry_run else "真实自动回复"
        behavior = (
            "从已保存记录后接收新增消息，真实发送禁用。"
            if settings.dry_run
            else "仅回复启动后新增消息；旧草稿不补发，发送使用本人 Ri 原会话。"
        )
        print(
            f"Ri {mode}已建立启动基线（新增记录 {baseline_count} 条）；"
            f"{behavior}原始时间保留分钟精度。",
            flush=True,
        )
        async with HTTPReplyModel(
            settings.llm_base_url,
            settings.llm_api_key.get_secret_value(),
            settings.llm_model,
            template_name=settings.personal_llm_template_name,
        ) as model:
            loop = AgentLoop(
                inbox,
                model,
                DisabledSender() if settings.dry_run else preview,
                store,
                allowed_chat=settings.personal_chat,
                allowed_sender=settings.personal_chat,
                self_senders=frozenset({"desktop:self"}),
                dry_run=settings.dry_run,
                bucket_seconds=settings.dedup_bucket_seconds,
                reply_preview=preview if settings.dry_run else None,
            )
            return await run_cycles(
                loop, once=once, interval=settings.poll_interval_seconds
            )
    finally:
        await store.aclose()


async def _recover_personal_drafts(
    settings: DesktopSettings,
    store: SQLiteStore,
    preview: ReplyPreviewProtocol,
) -> None:
    """Recheck saved drafts that failed before printing, without another model call."""
    for row in await store.records():
        if not (
            row["chat"] == row["sender"] == settings.personal_chat
            and not row["is_self"]
            and row["status"] == "emit_failed"
            and row["error"] == "PersonalReplyError"
            and row["time_precision"] == "minute"
            and row["time_display"]
            and isinstance(row["reply"], str)
            and row["reply"].strip()
        ):
            continue
        with bound_contextvars(msg_id=row["key"]):
            await preview.prepare_reply(row["sender"], row["reply"])
            print(
                f"[DRY-RUN] 将发送至 {row['sender']}"
                f"（企微原始显示时间：{row['time_display']}，分钟精度）：{row['reply']}",
                flush=True,
            )
            await store.finish(row["key"], "dry_run", reply=row["reply"])
            logger.info(
                "personal_draft_recovered", prior_status="emit_failed", real_sends=0
            )


def main(argv: Sequence[str] | None = None) -> int:
    """Offer offline/demo polling and explicitly enabled Ri member-account sends."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="等待首个时间窗口后轮询一轮并退出"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="固定 fake inbox + mock LLM；不读取私信或 .env",
    )
    parser.add_argument(
        "--personal",
        action="store_true",
        help="读取当前 Ri 外部单聊；默认 dry-run，配合 --live 开启本人回复",
    )
    parser.add_argument(
        "--live", action="store_true", help="只在个人 Ri 原会话启用真实自动回复"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.live and args.demo:
            raise SetupError("--live 仅支持 Ri 个人会话，不能用于 --demo")
        if args.demo:
            from smoke.smoke_desktop import run_desktop_smoke

            asyncio.run(run_desktop_smoke())
            return 0
        settings = DesktopSettings()
        personal_selected = args.personal or settings.inbox_backend == "personal"
        if args.live:
            if not personal_selected:
                raise SetupError("--live 必须选择 --personal 或 INBOX_BACKEND=personal")
            settings = settings.model_copy(update={"dry_run": False})
        runtime: LocalRuntime = run_personal if personal_selected else run_cli
        return asyncio.run(runtime(settings, once=args.once))
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(map(str, error["loc"]))
                for error in exc.errors(include_input=False, include_context=False)
            }
        )
        print("配置缺失或无效字段：" + ", ".join(fields))
        return 2
    except (SetupError, LLMError) as exc:
        print(str(exc))
        return 2
    except KeyboardInterrupt:
        print("本地消息循环已停止。")
        return 0
    except Exception as exc:  # noqa: BLE001 - Never emit credentials from raw external exceptions.
        print(f"本地消息循环失败：{type(exc).__name__}；请查看安全错误日志。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
