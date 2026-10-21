import json
import urllib.request
from typing import Protocol

from structlog.contextvars import get_contextvars

from src.core.desktop_llm import _load_template
from src.core.logging import get_logger

logger = get_logger(__name__)


class ModelProtocol(Protocol):
    def generate(self, context: dict) -> dict:
        """Produce a structured reply without performing any chat send."""
        raise NotImplementedError


INTENTS = {"chat", "question", "task", "clarify", "handoff", "document"}


class LocalModel:
    """Only a fixed loopback endpoint and an installed non-cloud Ollama model."""

    def __init__(self, name, timeout=90, template_name="assistant_reply.txt"):
        self.template = _load_template(template_name)
        if "cloud" in name.lower():
            raise ValueError("离线模式拒绝 cloud 模型")
        self.name, self.timeout = name, timeout

    def _request(self, route, data=None):
        payload = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:11434" + route,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        # Do not route localhost requests through any configured external proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(2):
            try:
                with opener.open(req, timeout=self.timeout) as response:
                    return json.loads(response.read(2_000_000))
            except (OSError, ValueError) as exc:
                logger.warning(
                    "assistant_model_request_failed",
                    msg_id=get_contextvars().get("msg_id"),
                    attempt=attempt + 1,
                    retry=attempt == 0,
                    error=type(exc).__name__,
                )
                if attempt:
                    raise ValueError("本地模型请求失败，已重试一次") from None
        raise AssertionError("unreachable")

    def check(self):
        data = self._request("/api/show", {"model": self.name})
        if data.get("remote_host") or data.get("remote_model"):
            raise ValueError("模型指向远程服务，离线模式拒绝运行")
        return {"model": self.name, "details": data.get("details", {})}

    def generate(self, context):
        allowed = [x["source"] for x in context.get("documents", [])]
        citations = (
            {
                "type": "array",
                "maxItems": 6,
                "uniqueItems": True,
                "items": {"type": "string", "enum": allowed},
            }
            if allowed
            else {"type": "array", "maxItems": 0, "items": {"type": "string"}}
        )
        schema = {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": sorted(INTENTS)},
                "reply": {"type": "string", "minLength": 1, "maxLength": 2000},
                "citations": citations,
            },
            "required": ["intent", "reply", "citations"],
            "additionalProperties": False,
        }
        messages = [
            {"role": "system", "content": self.template},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        result = self._request(
            "/api/chat",
            {
                "model": self.name,
                "stream": False,
                "format": schema,
                "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 600},
                "messages": messages,
            },
        )
        return validate(json.loads(result["message"]["content"]), context)


def validate(answer, context):
    if not isinstance(answer, dict) or answer.get("intent") not in INTENTS:
        raise ValueError("模型意图格式错误")
    reply = answer.get("reply")
    if not isinstance(reply, str) or not reply.strip() or len(reply) > 2000:
        raise ValueError("模型回复为空或超长")
    citations = answer.get("citations", [])
    allowed = {x["source"] for x in context.get("documents", [])}
    if not isinstance(citations, list) or any(
        not isinstance(x, str) or x not in allowed for x in citations
    ):
        raise ValueError("模型返回了不存在的引用")
    return {
        "intent": answer["intent"],
        "reply": reply.strip(),
        "citations": list(dict.fromkeys(citations)),
    }


class FixtureModel:
    """Explicit deterministic test double, never labelled a real LLM."""

    def generate(self, context):
        docs = context.get("documents", [])
        if docs:
            return {
                "intent": "document",
                "reply": "资料摘录：" + docs[0]["text"][:300],
                "citations": [docs[0]["source"]],
            }
        memory = context.get("memory", [])
        suffix = "；已确认偏好：" + memory[0]["value"] if memory else ""
        return {
            "intent": "chat",
            "reply": "离线测试回复：" + context["text"][:160] + suffix,
            "citations": [],
        }
