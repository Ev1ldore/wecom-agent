"""Run local receive/filter/model/SQLite with opt-in personal-chat sending."""

import argparse
import asyncio
import fcntl
import logging
import os
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError
from structlog.contextvars import bound_contextvars

from adapters.inbox import InboxError
from adapters.wechat_ilink import (
    FileILinkCursorStore,
    ILinkHTTPClient,
    WeChatILinkInbox,
    WeChatILinkSender,
)
from adapters.wechat_ilink_auth import (
    FileILinkCredentialStore,
    ILinkLoginFlow,
    WeChatILinkAuthError,
)
from adapters.wecom_cli import IS_POSIX, stop_cli_process
from src.core.desktop_llm import HTTPReplyModel, LLMError
from src.core.desktop_loop import AgentLoop, LoopProtocol, ReplyPreviewProtocol
from src.core.desktop_store import SQLiteStore
from src.core.logging import get_logger
from src.desktop_config import DesktopSettings

WECOM_ILINK_QR_FILENAME = "wechat_ilink_qr.png"

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
    stop_on_error: bool = False,
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
            if once or stop_on_error:
                return 1
        else:
            logger.info("local_poll_done", msg_id=None, **stats)
            failed = any(
                stats.get(field, 0)
                for field in ("llm_failed", "send_failed", "emit_failed")
            )
            if once or (stop_on_error and failed):
                return int(failed)
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


async def run_wechat_login(settings: DesktopSettings) -> int:
    """Run the official QR login once and persist bot credentials locally."""
    store = FileILinkCredentialStore(Path(settings.wechat_ilink_token_path))
    if store.load() is not None:
        print(
            "已登录 iLink 机器人；如需重新登录请先删除 "
            f"{settings.wechat_ilink_token_path} 再运行本命令。",
            flush=True,
        )
        return 0
    async with ILinkHTTPClient(settings.wechat_ilink_base_url) as api:
        flow = ILinkLoginFlow(api, store, qr_sink=_write_wechat_qr)
        try:
            await flow.ensure_credentials()
        except WeChatILinkAuthError as exc:
            raise SetupError(str(exc)) from None
    print(
        f"登录成功，凭据已保存到 {settings.wechat_ilink_token_path}（仅本人可读）。",
        flush=True,
    )
    return 0


async def _write_wechat_qr(image: bytes) -> Path:
    """Persist the login QR image and tell the owner to scan it before polling."""
    path = Path(WECOM_ILINK_QR_FILENAME)
    path.write_bytes(image)
    print(
        f"请用微信扫描 {path.resolve()} 中的二维码完成机器人登录；"
        "确认后自动继续，二维码过期需重新运行。",
        flush=True,
    )
    return path


async def run_wechat_ilink(settings: DesktopSettings, *, once: bool) -> int:
    """Poll the official personal-WeChat bot stream for one allowed peer.

    The reply identity is the iLink bot itself, never the owner's personal
    account. An empty allowed user starts a read-only discovery mode that only
    logs peer IDs so the owner can fill WECHAT_ILINK_ALLOWED_USER afterwards.
    """
    credential_store = FileILinkCredentialStore(Path(settings.wechat_ilink_token_path))
    cursor_store = FileILinkCursorStore(Path(settings.wechat_ilink_state_path))
    async with ILinkHTTPClient(settings.wechat_ilink_base_url) as login_api:
        login_flow = ILinkLoginFlow(
            login_api, credential_store, qr_sink=_write_wechat_qr
        )
        try:
            credentials = await login_flow.ensure_credentials()
        except WeChatILinkAuthError as exc:
            raise SetupError(str(exc)) from None
    allowed_user = settings.wechat_ilink_allowed_user.strip()
    guard_user = allowed_user or "discover"
    async with ILinkHTTPClient(credentials.base_url, credentials.bot_token) as api:
        inbox = WeChatILinkInbox(api, cursor_store, allowed_user=guard_user)
        store = SQLiteStore(settings.sqlite_path)
        try:
            async with HTTPReplyModel(
                settings.llm_base_url,
                settings.llm_api_key.get_secret_value(),
                settings.llm_model,
                template_name=settings.wechat_llm_template_name,
            ) as model:
                loop = AgentLoop(
                    inbox,
                    model,
                    DisabledSender()
                    if settings.dry_run
                    else WeChatILinkSender(api, inbox),
                    store,
                    allowed_chat=f"wxilink:{guard_user}",
                    allowed_sender=guard_user,
                    self_senders=frozenset(),
                    dry_run=settings.dry_run,
                    bucket_seconds=settings.dedup_bucket_seconds,
                )
                mode = (
                    "dry-run"
                    if settings.dry_run
                    else "真实自动回复（官方 iLink 机器人身份）"
                )
                target = allowed_user or "未设置（发现模式：只记录来信人 ID，不回复）"
                print(
                    f"个人微信 iLink {mode}已启动；目标联系人：{target}。",
                    flush=True,
                )
                return await run_cycles(
                    loop, once=once, interval=settings.poll_interval_seconds
                )
        finally:
            await store.aclose()


@contextmanager
def _wx2_runtime_lock():
    """Permit one desktop actor across databases and processes for this macOS user."""
    lock_path = Path.home() / ".wx-cli" / "wx2-ri.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("wx2→Ri 已有一个桌面执行器在运行。") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def run_wechat_ocr(settings: DesktopSettings, *, once: bool) -> int:
    """Hold the cross-process wx2 desktop lock throughout this runtime."""
    with _wx2_runtime_lock():
        return await _run_wechat_ocr_locked(settings, once=once)


async def run_wechat_native(settings: DesktopSettings, *, once: bool) -> int:
    """Hold the same cross-process wx2 lock for the built-in screenshot path."""
    with _wx2_runtime_lock():
        return await _run_wechat_native_locked(settings, once=once)


async def _run_wechat_native_locked(settings: DesktopSettings, *, once: bool) -> int:
    """Baseline current Ri history, then reply only to strict new tail appends."""
    from adapters.personal_tail import PersonalTailInbox
    from adapters.wechat_native import IDENTITY, NativeRiChat
    from src.core.desktop_loop import message_key

    if settings.wechat_ocr_contact.strip() not in {"", "Ri"}:
        raise SetupError("内置截图路径只允许微信 2 的 Ri 独立单聊窗口。")
    if settings.assistant_enabled and not settings.dry_run:
        raise SetupError("增强处理链只支持 dry-run；尚未实机验收，未操作微信。")
    chat = NativeRiChat(live=not settings.dry_run, recover_history=False)
    await chat.activate()
    # Validate model credentials/template before opening the durable receive cursor.
    async with HTTPReplyModel(
        settings.llm_base_url,
        settings.llm_api_key.get_secret_value(),
        settings.llm_model,
        template_name=settings.assistant_template_name
        if settings.assistant_enabled
        else settings.wechat_ocr_template_name,
    ) as model:
        store = SQLiteStore(settings.sqlite_path)
        assistant = None
        try:
            if settings.assistant_enabled:
                from src.core.assistant.desktop import DesktopAssistant

                assistant = DesktopAssistant(
                    model,
                    settings.sqlite_path.with_suffix(".assistant.db"),
                    chat=IDENTITY,
                    sender=IDENTITY,
                )
            known = frozenset()
            if settings.dry_run:
                known = frozenset(
                    r["key"]
                    for r in await store.records()
                    if r["chat"] == IDENTITY and not r["is_self"]
                )
            inbox = PersonalTailInbox(
                chat,
                known_keys=known,
                key_of=lambda m: message_key(m, settings.dedup_bucket_seconds),
            )
            history = await inbox.baseline()
            for msg in history:
                if not msg.is_self:
                    key = message_key(msg, settings.dedup_bucket_seconds)
                    if await store.claim(msg, key):
                        await store.finish(key, "baseline")
            mode = (
                "dry-run，不发送" if settings.dry_run else "live，仅回复启动后新增消息"
            )
            print(
                f"wx2 Ri 内置截图自动回复已就绪：{mode}；基线 {len(history)} 条。"
                f"轮询间隔 {settings.wechat_native_poll_seconds:g} 秒；截图不重试。"
                "请保持 Ri 独立窗口在前台；异常后停止本次运行，不自动重启。",
                flush=True,
            )
            loop = AgentLoop(
                inbox,
                model,
                DisabledSender() if settings.dry_run else chat,
                store,
                allowed_chat=IDENTITY,
                allowed_sender=IDENTITY,
                self_senders=frozenset({"desktop:self"}),
                dry_run=settings.dry_run,
                bucket_seconds=settings.dedup_bucket_seconds,
                reply_preview=chat if settings.dry_run else None,
                message_processor=assistant,
            )
            return await run_cycles(
                loop,
                once=once,
                interval=settings.wechat_native_poll_seconds,
                stop_on_error=True,
            )
        finally:
            if assistant is not None:
                await assistant.aclose()
            await store.aclose()


async def _run_wechat_ocr_locked(settings: DesktopSettings, *, once: bool) -> int:
    """Read the verified WeChat window via OCR; opt-in GUI sends with OCR receipts."""
    contact = settings.wechat_ocr_contact.strip()
    if not contact:
        raise SetupError(
            "请先填写 WECHAT_OCR_CONTACT（当前已在微信桌面端打开并核实的单聊显示名）"
        )
    if contact != "Ri":
        raise SetupError("本次个人微信路径仅支持 wx2 客服与 Ri 用户的单聊。")
    from adapters.personal_tail import PersonalTailInbox, TailError
    from adapters.wechat_ocr import (
        DesktopWeChatOCRReader,
        WeChatOCRInbox,
        WxCLIChangeProbe,
        chat_identity,
    )
    from adapters.wechat_ocr_send import WeChatOCRReplyChannel
    from src.core.desktop_loop import message_key

    identity = chat_identity(contact)
    probe = WxCLIChangeProbe(
        contact=contact,
        workdir=settings.wx_cli_workdir,
        account_verified=settings.wx_cli_account_verified,
    )
    store = SQLiteStore(settings.sqlite_path)
    channel = WeChatOCRReplyChannel(
        contact=contact,
        live_enabled=not settings.dry_run,
        reader=DesktopWeChatOCRReader(),
    )
    reader = DesktopWeChatOCRReader(
        probe=probe,
        select_contact=channel.select_contact if not settings.dry_run else None,
    )
    try:
        known: frozenset[str] = frozenset()
        if settings.dry_run:
            known = frozenset(
                row["key"]
                for row in await store.records()
                if row["chat"] == identity
                and row["sender"] == identity
                and not row["is_self"]
            )
        inbox = PersonalTailInbox(
            WeChatOCRInbox(contact=contact, reader=reader),
            known_keys=known,
            key_of=lambda msg: message_key(msg, settings.dedup_bucket_seconds),
        )
        try:
            initial = await inbox.baseline()
        except TailError as exc:
            raise SetupError(str(exc)) from None
        baseline_count = 0
        for msg in initial:
            if (
                msg.chat == identity
                and msg.sender == identity
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
            else "仅回复启动后新增消息；旧草稿不补发，发送使用本人微信原会话。"
        )
        print(
            f"微信 OCR {mode}已建立启动基线（新增记录 {baseline_count} 条）；"
            f"{behavior}时间取自聊天内可见的原始分隔标签。",
            flush=True,
        )
        async with HTTPReplyModel(
            settings.llm_base_url,
            settings.llm_api_key.get_secret_value(),
            settings.llm_model,
            template_name=settings.wechat_ocr_template_name,
        ) as model:
            loop = AgentLoop(
                inbox,
                model,
                DisabledSender() if settings.dry_run else channel,
                store,
                allowed_chat=identity,
                allowed_sender=identity,
                self_senders=frozenset({"desktop:self"}),
                dry_run=settings.dry_run,
                bucket_seconds=settings.dedup_bucket_seconds,
                reply_preview=channel if settings.dry_run else None,
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
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--offline-assistant" in arguments:
        if any(
            flag in arguments
            for flag in (
                "--live",
                "--wechat-native",
                "--personal",
                "--wechat",
                "--wechat-ocr",
                "--wechat-login",
                "--demo",
                "--assistant",
            )
        ):
            print("离线增强入口不能与真实客户端或其他入口组合；未读取 .env。")
            return 2
        from src.core.assistant.cli import main as assistant_main

        arguments.remove("--offline-assistant")
        return assistant_main(arguments) or 0
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
        "--wechat",
        action="store_true",
        help="通过官方 iLink Bot API 接入个人微信；默认 dry-run，配合 --live 开启机器人回复",
    )
    parser.add_argument(
        "--wechat-ocr",
        action="store_true",
        help="OCR 读取微信桌面窗口并走本人原会话回复；默认 dry-run，配合 --live 开启真实发送",
    )
    parser.add_argument(
        "--wechat-native",
        action="store_true",
        help="使用微信 2 内置截图读取 Ri 独立窗口；--live 启用本人自动回复，无需 wx-cli",
    )
    parser.add_argument(
        "--wechat-login",
        action="store_true",
        help="仅执行 iLink 扫码登录并保存凭据，不启动消息循环",
    )
    parser.add_argument(
        "--live", action="store_true", help="只在个人 Ri/微信原会话启用真实自动回复"
    )
    parser.add_argument(
        "--assistant",
        action="store_true",
        help="为 --wechat-native dry-run 启用记忆和意图处理；不支持 --live",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.assistant and (not args.wechat_native or args.live):
            raise SetupError(
                "--assistant 仅支持 --wechat-native 的 dry-run，新增处理链尚未实机验收"
            )
        if args.wechat_native and any(
            (args.demo, args.personal, args.wechat, args.wechat_ocr, args.wechat_login)
        ):
            raise SetupError("--wechat-native 不能与其他接入模式组合")
        if args.live and args.demo:
            raise SetupError("--live 仅支持个人会话或微信入口，不能用于 --demo")
        if args.wechat_login:
            if args.demo or args.personal or args.wechat or args.live:
                raise SetupError("--wechat-login 不能与其他运行模式组合")
            settings = DesktopSettings()
            return asyncio.run(run_wechat_login(settings))
        if args.demo:
            from smoke.smoke_desktop import run_desktop_smoke

            asyncio.run(run_desktop_smoke())
            return 0
        settings = DesktopSettings()
        if args.assistant:
            settings = settings.model_copy(update={"assistant_enabled": True})
        personal_selected = args.personal or settings.inbox_backend == "personal"
        wechat_selected = args.wechat or settings.inbox_backend == "wechat_ilink"
        wechat_ocr_selected = args.wechat_ocr or settings.inbox_backend == "wechat_ocr"
        if args.live:
            if not (
                personal_selected
                or wechat_selected
                or wechat_ocr_selected
                or args.wechat_native
            ):
                raise SetupError(
                    "--live 必须选择 --personal、--wechat、--wechat-ocr 或对应的 INBOX_BACKEND"
                )
            settings = settings.model_copy(update={"dry_run": False})
        if args.wechat_native:
            settings = settings.model_copy(update={"dry_run": not args.live})
            runtime: LocalRuntime = run_wechat_native
        elif wechat_ocr_selected and not (args.personal or args.wechat):
            runtime = run_wechat_ocr
        elif personal_selected and not args.wechat:
            runtime = run_personal
        elif wechat_selected:
            runtime = run_wechat_ilink
        else:
            runtime = run_cli
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
    except InboxError as exc:
        code = getattr(exc, "code", None)
        safe_code = (
            code
            if code
            in {
                "CLI_SETUP",
                "CLI_FAILED",
                "CLI_STALE",
                "NO_WINDOW",
                "WINDOW_CHANGED",
                "CAPTURE_DENIED",
                "CAPTURE_EMPTY",
                "OCR_SETUP",
            }
            else "INBOX_UNAVAILABLE"
        )
        print(f"接入未就绪：{safe_code}；未启动自动回复。")
        return 2
    except KeyboardInterrupt:
        print("本地消息循环已停止。")
        return 0
    except Exception as exc:  # noqa: BLE001 - Never emit credentials from raw external exceptions.
        print(f"本地消息循环失败：{type(exc).__name__}；请查看安全错误日志。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
