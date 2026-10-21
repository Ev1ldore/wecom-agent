import json
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.db = self.path / "state.db"
        self.events = self.path / "events.jsonl"
        self.events.touch()
        self.event = {
            "id": "cli1",
            "conversation": "demo-alice",
            "sender": "alice",
            "kind": "text",
            "text": "你好",
        }

    def tearDown(self):
        self.temp.cleanup()

    def command(self, name):
        return [
            sys.executable,
            str(ROOT / "agent_loop.py"),
            "--offline-assistant",
            name,
            "--config",
            str(ROOT / "assistant.example.json"),
            "--input",
            str(self.events),
            "--db",
            str(self.db),
        ]

    def append(self, event):
        with self.events.open("a") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def wait_receipts(self, count):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if self.db.exists():
                try:
                    with sqlite3.connect(self.db) as db:
                        result = db.execute("SELECT COUNT(*) FROM receipts").fetchone()[
                            0
                        ]
                    if result == count:
                        return
                except sqlite3.OperationalError:
                    pass
            time.sleep(0.05)
        self.fail(f"watch未产生{count}条模拟回执")

    def test_live_file_watch_and_restart_dedupe(self):
        with (self.path / "watch.log").open("w") as log:
            process = subprocess.Popen(
                self.command("watch"), cwd=ROOT, stdout=log, stderr=log
            )
            try:
                self.append(self.event)
                self.wait_receipts(1)
                other = subprocess.run(
                    self.command("inspect"),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertNotEqual(other.returncode, 0)
                self.assertIn("已有实例", other.stderr)
            finally:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=10)
        self.assertEqual(process.returncode, 0)
        result = subprocess.run(
            self.command("replay"),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(len(json.loads(result.stdout)["receipts"]), 1)

    def test_clear_and_resume_keep_tombstones(self):
        self.append(self.event)
        subprocess.run(self.command("replay"), capture_output=True, check=True)
        args = ["--conversation", "demo-alice", "--sender", "alice"]
        subprocess.run(self.command("clear") + args, capture_output=True, check=True)
        subprocess.run(self.command("resume") + args, capture_output=True, check=True)
        result = subprocess.run(
            self.command("replay"), capture_output=True, text=True, check=True
        )
        report = json.loads(result.stdout)
        self.assertEqual(len(report["receipts"]), 1)
        self.assertEqual(report["receipts"][0]["text"], "")
        self.assertEqual(report["messages"][0]["payload"], "{}")
        self.assertFalse(report["errors"])


if __name__ == "__main__":
    unittest.main()
