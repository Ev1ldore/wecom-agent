"""Speech-worker tests never load a real speech model."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.assistant.transcribe import TranscriberProtocol, transcribe


def test_local_weights_are_required_before_import(tmp_path):
    with pytest.raises(ValueError, match="download is disabled"):
        transcribe("voice.wav", str(tmp_path))


def test_worker_uses_local_chinese_model(tmp_path, monkeypatch):
    (tmp_path / "weights.safetensors").touch()
    call = Mock(return_value={"text": "转写测试"})
    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=call))
    worker: TranscriberProtocol = transcribe
    assert worker("voice.wav", str(tmp_path)) == {"text": "转写测试"}
    assert call.call_args.kwargs["path_or_hf_repo"] == str(tmp_path)
    assert call.call_args.kwargs["language"] == "zh"
