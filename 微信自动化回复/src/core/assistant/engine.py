import hashlib
import json
import re
import time
from typing import Protocol

from structlog.contextvars import bound_contextvars, get_contextvars

from src.core.logging import get_logger

from .management import whitelist
from .media import retrieve
from .model import validate
from .store import encoded

logger = get_logger(__name__)


class EngineProtocol(Protocol):
    def update_whitelist(self, config: dict) -> None:
        """Revoke queued work and invalidate drafts when an identity is removed."""
        raise NotImplementedError

    def ingest(self, event: dict) -> str:
        """Authorize and persist a local event before processing."""
        raise NotImplementedError

    def process_one(self) -> bool:
        """Process one event into an offline-only receipt."""
        raise NotImplementedError


class SenderProtocol(Protocol):
    def send(self, message_id: str, scope: str, text: str) -> None:
        """Record a simulated send; this package exposes no live transport."""
        raise NotImplementedError


class OfflineSender:
    """A durable simulation receipt. Cannot access WeChat or the network."""

    def __init__(self, store):
        self.store = store

    def send(self, message_id, scope, text):
        self.store.execute(
            "INSERT INTO receipts VALUES(?,?,?)", (message_id, text, time.time())
        )


class Engine:
    def __init__(self, store, config, model, media, sender=None):
        self.store, self.config, self.model, self.media = store, config, model, media
        self.sender = sender or OfflineSender(store)
        self.allowed = set(whitelist(config))
        self.store.recover()

    def update_whitelist(self, config):
        allowed = set(whitelist(config))
        with self.store.lock:
            removed = self.allowed - allowed
            self.allowed = allowed
            self.config["whitelist"] = config["whitelist"]
            for conversation, sender in removed:
                scope = encoded([conversation, sender])
                self.store.pause(scope, "已从白名单移除；重新添加后需手动恢复")
                self.store.execute(
                    "UPDATE sessions SET revision=revision+1 WHERE scope=?", (scope,)
                )
                self.store.execute(
                    "UPDATE messages SET status='cancelled',detail='白名单已移除' "
                    "WHERE scope=? AND status='pending'",
                    (scope,),
                )

    @staticmethod
    def scope(event):
        return encoded([event["conversation"], event["sender"]])

    @staticmethod
    def validate_event(event):
        if not isinstance(event, dict):
            raise TypeError("消息必须为JSON对象")
        for key in ("id", "conversation", "sender", "kind"):
            if not isinstance(event.get(key), str) or not 0 < len(event[key]) <= 200:
                raise ValueError("缺少或无效字段：" + key)
        if event["kind"] not in {"text", "voice", "file", "image", "video", "system"}:
            raise ValueError("不支持的消息类型")
        if event.get("direction", "incoming") not in {"incoming", "self"}:
            raise ValueError("无效direction")
        if (
            not isinstance(event.get("text", ""), str)
            or len(event.get("text", "")) > 4000
        ):
            raise ValueError("text必须为不超过4000字符的字符串；长内容请作为附件导入")
        if event["kind"] in {"voice", "file", "image", "video"} and not isinstance(
            event.get("attachment"), str
        ):
            raise ValueError("附件消息缺少attachment相对路径")
        if event["kind"] == "text" and not event.get("text", "").strip():
            raise ValueError("空文本")
        if "group" in event and not isinstance(event["group"], bool):
            raise ValueError("group必须为布尔值")

    def ingest(self, event):
        self.validate_event(event)
        scope = self.scope(event)
        # Native IDs are scoped, not globally assumed unique.
        key = hashlib.sha256(encoded([scope, event["id"]]).encode()).hexdigest()
        logger.info(
            "assistant_event_received",
            msg_id=get_contextvars().get("msg_id") or key,
            kind=event["kind"],
        )
        payload = encoded(event)
        with self.store.lock:
            existing = self.store.rows(
                "SELECT payload,status FROM messages WHERE id=?", (key,)
            )
            if existing:
                stored = existing[0]["payload"]
                redacted = encoded(
                    {"hash": hashlib.sha256(payload.encode()).hexdigest()}
                )
                if existing[0]["status"] != "cleared" and stored not in {
                    payload,
                    redacted,
                }:
                    raise ValueError("同一消息ID对应不同内容，拒绝覆盖")
                return "duplicate"
            reason = ""
            if (event["conversation"], event["sender"]) not in self.allowed:
                reason = "not_whitelisted"
            elif event.get("group", False):
                reason = "groups_disabled"
            elif event["kind"] == "system":
                reason = "system_message"
            elif event.get("direction") != "self" and any(
                term in event.get("text", "")
                for term in self.config.get("spam_terms", [])
            ):
                reason = "spam_rule"
            if reason:
                # Minimize persistence of messages outside scope.
                self.store.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?)",
                    (
                        key,
                        scope,
                        payload
                        if reason != "not_whitelisted"
                        else encoded(
                            {"hash": hashlib.sha256(payload.encode()).hexdigest()}
                        ),
                        "filtered",
                        reason,
                        time.time(),
                    ),
                )
                return reason
            self.store.execute(
                "INSERT OR IGNORE INTO sessions(scope) VALUES(?)", (scope,)
            )
            session = self.store.rows("SELECT * FROM sessions WHERE scope=?", (scope,))[
                0
            ]
            status = "paused" if session["paused"] else "pending"
            if event.get("direction") == "self":
                status = "manual"
                self.store.pause(scope, "人工接管")
                self.store.execute(
                    "UPDATE messages SET status='cancelled',detail='人工接管' "
                    "WHERE scope=? AND status='pending'",
                    (scope,),
                )
            self.store.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?)",
                (key, scope, payload, status, "", time.time()),
            )
            self.store.execute(
                "UPDATE sessions SET revision=revision+1 WHERE scope=?", (scope,)
            )
            self.store.execute(
                "INSERT INTO history(scope,role,text,source,created) VALUES(?,?,?,?,?)",
                (
                    scope,
                    "assistant" if status == "manual" else "user",
                    event.get("text", "") or "[" + event["kind"] + "附件]",
                    key,
                    time.time(),
                ),
            )
            return status

    def resume(self, scope):
        with self.store.lock:
            if self.store.rows(
                "SELECT id FROM outbox WHERE scope=? AND status='unknown'", (scope,)
            ):
                raise ValueError("存在unknown发送，先人工核查；禁止直接恢复")
            self.store.execute(
                "UPDATE sessions SET paused=0,reason='',revision=revision+1 WHERE scope=?",
                (scope,),
            )
            # Old paused events are never replayed on resume.
            return "仅处理之后的新消息"

    def resolve_unknown(self, message_id, resolution):
        if resolution not in {"sent", "not_sent"}:
            raise ValueError("resolution必须为sent或not_sent")
        with self.store.lock:
            rows = self.store.rows(
                "SELECT scope FROM outbox WHERE id=? AND status='unknown'",
                (message_id,),
            )
            if not rows:
                raise ValueError("不存在该unknown发送记录")
            state = "reviewed_" + resolution
            self.store.execute(
                "UPDATE outbox SET status=? WHERE id=?", (state, message_id)
            )
            self.store.execute(
                "UPDATE messages SET status=? WHERE id=?", (state, message_id)
            )
            return "核查结果已记录；会话仍暂停，旧消息不会重发"

    def process_one(self):
        pending = self.store.rows(
            "SELECT id FROM messages WHERE status='pending' ORDER BY created LIMIT 1"
        )
        if not pending:
            return False
        key = pending[0]["id"]
        with bound_contextvars(msg_id=get_contextvars().get("msg_id") or key):
            result = self._process_one()
            state = self.store.rows("SELECT status FROM messages WHERE id=?", (key,))[
                0
            ]["status"]
            logger.info("assistant_event_processed", state=state, live_send=False)
            return result

    def _process_one(self):
        with self.store.lock:
            pending = self.store.rows(
                "SELECT * FROM messages WHERE status='pending' ORDER BY created LIMIT 1"
            )
            if not pending:
                return False
            row = pending[0]
            scope, key = row["scope"], row["id"]
            event = json.loads(row["payload"])
            if (event["conversation"], event["sender"]) not in self.allowed:
                self.store.execute(
                    "UPDATE messages SET status='cancelled',detail='白名单已移除' WHERE id=?",
                    (key,),
                )
                return True
            newer = self.store.rows(
                "SELECT payload FROM messages WHERE scope=? AND status='pending' AND created>? ORDER BY created LIMIT 1",
                (scope, row["created"]),
            )
            # Coalesce a burst of ordinary text, retaining every input in history.
            if (
                event["kind"] == "text"
                and not re.match(r"^(记住|忘记)[：:]", event.get("text", ""))
                and newer
                and json.loads(newer[0]["payload"])["kind"] == "text"
                and not re.match(
                    r"^(记住|忘记)[：:]",
                    json.loads(newer[0]["payload"]).get("text", ""),
                )
            ):
                self.store.execute(
                    "UPDATE messages SET status='coalesced',detail='合并到后续文本回复' WHERE id=?",
                    (key,),
                )
                return True
            session = self.store.rows("SELECT * FROM sessions WHERE scope=?", (scope,))[
                0
            ]
            if session["paused"]:
                self.store.execute(
                    "UPDATE messages SET status='paused' WHERE id=?", (key,)
                )
                return True
            self.store.execute(
                "UPDATE messages SET status='processing' WHERE id=?", (key,)
            )
            revision = session["revision"]
            event = json.loads(row["payload"])
        try:
            result, evidence = self.respond(event, key, scope)
            with self.store.lock:
                current = self.store.rows(
                    "SELECT * FROM sessions WHERE scope=?", (scope,)
                )[0]
                if (
                    current["paused"]
                    or current["revision"] != revision
                    or (event["conversation"], event["sender"]) not in self.allowed
                ):
                    self.store.execute(
                        "UPDATE messages SET status='stale',detail='新消息或人工接管使回复失效' WHERE id=?",
                        (key,),
                    )
                    return True
                count = self.store.rows(
                    "SELECT COUNT(*) AS n FROM outbox WHERE scope=? AND created>?",
                    (scope, time.time() - 60),
                )[0]["n"]
                if count >= self.config.get("max_replies_per_minute", 10):
                    self.store.execute(
                        "UPDATE messages SET status='rate_limited' WHERE id=?", (key,)
                    )
                    return True
                if result["intent"] in {"handoff", "task"}:
                    self.store.pause(scope, "需要人工处理的任务")
                text = result["reply"]
                if result["citations"]:
                    text += "\n来源：" + "；".join(result["citations"])
                # Reserve durably before the side effect. Crash recovery becomes unknown.
                self.store.execute(
                    "INSERT INTO outbox VALUES(?,?,?,?,?,?,?)",
                    (
                        key,
                        scope,
                        text,
                        result["intent"],
                        encoded(evidence),
                        "sending",
                        time.time(),
                    ),
                )
                try:
                    self.sender.send(key, scope, text)
                except Exception as exc:  # noqa: BLE001 -- Persist a safe terminal state at the adapter boundary.
                    self.store.execute(
                        "UPDATE outbox SET status='unknown' WHERE id=?", (key,)
                    )
                    self.store.execute(
                        "UPDATE messages SET status='unknown',detail=? WHERE id=?",
                        (type(exc).__name__, key),
                    )
                    self.store.pause(scope, "发送结果未知，禁止自动重试")
                    return True
                self.store.execute(
                    "UPDATE outbox SET status='simulated' WHERE id=?", (key,)
                )
                self.store.execute(
                    "UPDATE messages SET status='simulated' WHERE id=?", (key,)
                )
                self.store.execute(
                    "INSERT INTO history(scope,role,text,source,created) VALUES(?,?,?,?,?)",
                    (scope, "assistant", text, key, time.time()),
                )
        except Exception as exc:  # noqa: BLE001 -- Persist a safe terminal state at the adapter boundary.
            self.store.execute(
                "UPDATE messages SET status='failed',detail=? WHERE id=?",
                (type(exc).__name__, key),
            )
        return True

    def respond(self, event, key, scope):
        text = event.get("text", "")
        evidence = {
            "transport": "offline_simulator",
            "model": type(self.model).__name__,
        }
        if event["kind"] == "text":
            match = re.fullmatch(r"记住[：:]\s*([^=\n]{1,40})=(.{1,300})", text)
            if match:
                name, value = (x.strip() for x in match.groups())
                if not name or not value:
                    raise ValueError("记忆键和值不能为空")
                self.store.execute(
                    "INSERT OR REPLACE INTO memory VALUES(?,?,?,?,?)",
                    (scope, name, value, key, None),
                )
                return {
                    "intent": "chat",
                    "reply": f"已记住：{name}={value}。",
                    "citations": [],
                }, evidence
            if re.match(r"^忘记[：:]", text):
                self.store.execute(
                    "DELETE FROM memory WHERE scope=? AND key=?",
                    (scope, text[3:].strip()),
                )
                return {
                    "intent": "chat",
                    "reply": "已删除该长期记忆。历史消息仍保留，可通过本地清除命令删除。",
                    "citations": [],
                }, evidence
            if re.search(r"(我的|我之前的).*(偏好|习惯)", text):
                memory = self.store.context(scope)["memory"]
                reply = "目前还没有记录你的长期偏好。你可以告诉我，例如“记住：回复风格=简短中文”。"
                if memory:
                    reply = (
                        "你已确认的记录是："
                        + "；".join(x["key"] + "=" + x["value"] for x in memory)
                        + "。"
                    )
                return {"intent": "question", "reply": reply, "citations": []}, evidence
        if event["kind"] in {"file", "voice", "image", "video"}:
            chunks, media_evidence = self.media.extract(
                event["attachment"], event["kind"]
            )
            self.store.execute(
                "INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)",
                (key, scope, event["attachment"], encoded(chunks), media_evidence),
            )
            evidence["media"] = media_evidence
            if event["kind"] == "voice":
                text = "\n".join(x["text"] for x in chunks)
                self.store.execute(
                    "UPDATE history SET text=? WHERE source=? AND role=?",
                    (text, key, "user"),
                )
            elif not text.strip():
                return {
                    "intent": "clarify",
                    "reply": "附件已在本地读取。你希望我做摘要、回答问题，还是检查指定内容？",
                    "citations": [],
                }, evidence
        context = self.store.context(
            scope,
            self.config.get("history_window", 12),
            ttl_seconds=self.config.get("short_term_ttl_seconds", 1800),
            max_chars=self.config.get("short_term_max_chars", 12000),
        )
        # Older original messages remain auditable, but explicit memory deletion must not
        # be undone by refeeding old preference commands to the model.
        context["recent"] = [
            x
            for x in context["recent"]
            if not re.match(r"^(记住|已记住|忘记)[：:]", x["text"])
        ]
        context["older_excerpts"] = [
            x
            for x in context["older_excerpts"]
            if not re.match(r"^(记住|已记住|忘记)[：:]", x["excerpt"])
        ]
        for item in context["recent"]:
            if len(item["text"]) > 1000:
                item["text"] = (
                    item["text"][:1000] + "[历史消息仅保留前1000字符；原文仍在本地记录]"
                )
        if event["kind"] in {"file", "image", "video"}:
            documents = self.store.rows(
                "SELECT * FROM documents WHERE scope=? AND id=?", (scope, key)
            )
            context["recent"] = []
            context["older_excerpts"] = []
            context["current_attachment"] = True
        else:
            documents = self.store.rows(
                "SELECT * FROM documents WHERE scope=? ORDER BY rowid DESC LIMIT 10",
                (scope,),
            )
        context.update(
            text=text,
            documents=retrieve(documents, text),
            coverage=(
                "当前附件中最多6个片段；不能声称已分析完整文档"
                if context.get("current_attachment")
                else "最多最近10份附件中的6个相关片段；不能声称已分析完整文档"
            ),
        )
        result = validate(self.model.generate(context), context)
        evidence["context_sources"] = [x["source"] for x in context["documents"]]
        return result, evidence

    def drain(self):
        while self.process_one():
            pass
