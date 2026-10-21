import json
from pathlib import Path
from typing import Protocol


class SourceProtocol(Protocol):
    def poll(self) -> tuple[list, list]:
        """Read complete local events and explicit parse errors."""
        raise NotImplementedError


class JsonlSource:
    """Tails only complete lines; restart replay is handled by durable message IDs."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.identity = None

    def poll(self):
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if self.identity != identity or stat.st_size < self.offset:
            self.offset = 0
            self.identity = identity
        events, errors = [], []
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            while True:
                line = stream.readline(1_000_002)
                if not line:
                    break
                if len(line) > 1_000_000:
                    raise ValueError("输入单行超过1MB，停止监听")
                if not line.endswith(b"\n"):
                    break
                start = self.offset
                self.offset = stream.tell()
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except (ValueError, UnicodeDecodeError) as exc:
                    errors.append({"offset": start, "error": str(exc)})
        return events, errors
