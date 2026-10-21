"""Check actual small file parsers and mock all subprocess boundaries."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.assistant.media import Media, MediaError, MediaProtocol


def test_local_subprocess_timeout_retry_and_no_network_env(tmp_path, monkeypatch):
    runner = Mock(
        side_effect=[
            subprocess.TimeoutExpired("parser", 3),
            subprocess.CompletedProcess(["parser"], 0, stdout="text"),
        ]
    )
    monkeypatch.setattr(subprocess, "run", runner)
    assert Media(tmp_path, {}).run(["parser"], timeout=3) == "text"
    assert runner.call_count == 2
    assert runner.call_args.kwargs["timeout"] == 3
    assert runner.call_args.kwargs["env"]["HF_HUB_OFFLINE"] == "1"


def test_exhausted_parser_failure_has_no_private_path(tmp_path, monkeypatch):
    runner = Mock(side_effect=OSError("/private/sensitive-file"))
    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(MediaError) as error:
        Media(tmp_path, {}).run(["parser"])
    assert "sensitive-file" not in str(error.value)
    assert runner.call_count == 2


def test_xlsx_keeps_label_and_value_in_same_chunk():
    root = Path(__file__).resolve().parents[1] / "examples/assistant/inbox"
    media: MediaProtocol = Media(root, {})
    chunks, _ = media.extract("budget.xlsx", "file")
    assert any("设备" in x["text"] and "100" in x["text"] for x in chunks)


def test_pdf_actual_page_source():
    root = Path(__file__).resolve().parents[1] / "examples/assistant/inbox"
    chunks, evidence = Media(root, {}).extract("meeting.pdf", "file")
    assert chunks[0]["source"].endswith("页1")
    assert "15:00" in chunks[0]["text"]
    assert "未做" in evidence


def test_missing_speech_model_never_starts_download(tmp_path, monkeypatch):
    media = Media(tmp_path, {"asr_model": str(tmp_path / "missing")})
    monkeypatch.setattr(media, "duration", lambda path: 1)
    with pytest.raises(MediaError, match="不会自动联网下载"):
        media.audio(tmp_path / "a.wav")
