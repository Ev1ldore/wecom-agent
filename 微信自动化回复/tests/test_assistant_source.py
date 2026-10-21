"""A partial final line never becomes a half-message."""

from src.core.assistant.source import JsonlSource, SourceProtocol


def test_complete_lines_only_with_offset_errors(tmp_path):
    path = tmp_path / "inbox.jsonl"
    path.write_text('bad\n{"id":"1"}\n{"id":')
    source: SourceProtocol = JsonlSource(path)
    events, errors = source.poll()
    assert events == [{"id": "1"}]
    assert errors[0]["offset"] == 0
    with path.open("a") as handle:
        handle.write('"2"}\n')
    assert source.poll() == ([{"id": "2"}], [])
