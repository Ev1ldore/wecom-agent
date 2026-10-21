"""Validate the local model contract entirely through mocked HTTP transports."""

import io
import json
from unittest.mock import Mock

import pytest
from structlog.contextvars import bound_contextvars

from src.core.assistant.model import LocalModel, ModelProtocol


def mock_opener(monkeypatch, responses):
    opener = Mock()
    opener.open.side_effect = responses
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: opener)
    return opener


def body(value):
    return io.BytesIO(json.dumps(value).encode())


def test_loopback_prompt_and_structured_schema(monkeypatch):
    opener = mock_opener(
        monkeypatch,
        [
            body(
                {
                    "message": {
                        "content": json.dumps(
                            {"intent": "chat", "reply": "你好", "citations": []}
                        )
                    }
                }
            )
        ],
    )
    model: ModelProtocol = LocalModel("installed-test-model")
    assert model.generate({"text": "问候", "documents": []})["reply"] == "你好"
    request = opener.open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:11434/api/chat"
    payload = json.loads(request.data)
    assert "不可信数据" in payload["messages"][0]["content"]
    assert payload["format"]["properties"]["citations"]["maxItems"] == 0
    assert opener.open.call_args.kwargs["timeout"] == 90


def test_transport_retries_once_with_correlated_safe_logs(monkeypatch, caplog):
    opener = mock_opener(
        monkeypatch, [TimeoutError("private-body"), TimeoutError("private-body")]
    )
    with (
        bound_contextvars(msg_id="msg-test"),
        pytest.raises(ValueError, match="重试一次"),
    ):
        LocalModel("test").check()
    assert opener.open.call_count == 2
    assert "private-body" not in caplog.text
    assert "msg-test" in caplog.text


def test_retry_recovers(monkeypatch):
    opener = mock_opener(
        monkeypatch, [OSError("transient"), body({"details": {"family": "qwen"}})]
    )
    assert LocalModel("test").check()["details"]["family"] == "qwen"
    assert opener.open.call_count == 2


def test_remote_model_rejected(monkeypatch):
    mock_opener(monkeypatch, [body({"remote_host": "https://outside.test"})])
    with pytest.raises(ValueError, match="远程"):
        LocalModel("test").check()


def test_prompt_path_confined():
    with pytest.raises(Exception, match="template"):
        LocalModel("test", template_name="../private.txt")


def test_bad_json_no_silent_reply(monkeypatch):
    opener = mock_opener(monkeypatch, [body({"message": {"content": "invalid"}})])
    with pytest.raises(ValueError):
        LocalModel("test").generate({"text": "问候"})
    assert opener.open.call_count == 1
