import argparse
import fcntl
import json
import os
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Protocol

from src.core.assistant.engine import Engine
from src.core.assistant.media import Media
from src.core.assistant.model import FixtureModel, LocalModel
from src.core.assistant.source import JsonlSource
from src.core.assistant.store import Store, encoded


class CLIRuntimeProtocol(Protocol):
    def __call__(self, argv: list[str] | None = None) -> int | None:
        """Run only an offline source and simulated transport."""
        raise NotImplementedError


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线微信助手；不连接真实微信")
    parser.add_argument(
        "command",
        choices=["replay", "watch", "inspect", "resume", "clear", "resolve", "doctor"],
    )
    parser.add_argument("--config", default="assistant.example.json")
    parser.add_argument("--input", default="examples/assistant/messages.jsonl")
    parser.add_argument("--db", default="logs/assistant.sqlite3")
    parser.add_argument("--model", choices=["fixture", "local"], default="fixture")
    parser.add_argument("--conversation")
    parser.add_argument("--sender")
    parser.add_argument("--report")
    parser.add_argument("--message-id")
    parser.add_argument("--resolution", choices=["sent", "not_sent"])
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text())
    base = config_path.parent
    media_config = dict(config["media"])
    for name in ("ocr_binary", "asr_python", "asr_model"):
        if name in media_config:
            # Preserve a venv interpreter symlink: resolving it bypasses its packages.
            media_config[name] = os.path.abspath(base / media_config[name])
    model = (
        FixtureModel()
        if args.model == "fixture"
        else LocalModel(config["local_model"], template_name=config["prompt_template"])
    )
    if args.command == "doctor":
        result = {
            "mode": "offline",
            "wechat_access": False,
            "local_model": LocalModel(
                config["local_model"], template_name=config["prompt_template"]
            ).check(),
            "media_paths": {k: Path(v).exists() for k, v in media_config.items()},
        }
        print(encoded(result))
        return
    if args.model == "local":
        model.check()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        lockfile = stack.enter_context(open(str(args.db) + ".lock", "a"))
        try:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("同一数据库已有实例运行，请先停止该实例")
        store = Store(args.db)
        engine = Engine(
            store, config, model, Media(base / config["inbox"], media_config)
        )
        errors = []
        try:
            if args.command == "resolve":
                if not args.message_id or not args.resolution:
                    parser.error("resolve需要 --message-id 和 --resolution")
                print(engine.resolve_unknown(args.message_id, args.resolution))
            elif args.command in {"resume", "clear"}:
                if not args.conversation or not args.sender:
                    parser.error("需要 --conversation 和 --sender")
                scope = encoded([args.conversation, args.sender])
                if args.command == "resume":
                    print(engine.resume(scope))
                else:
                    # Clear content, retain message tombstones and delivery audit for dedupe.
                    for table in ("history", "memory", "documents"):
                        store.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
                    store.execute(
                        "UPDATE messages SET payload='{}',status='cleared' WHERE scope=?",
                        (scope,),
                    )
                    store.execute(
                        "UPDATE outbox SET text='',evidence='{}' WHERE scope=?",
                        (scope,),
                    )
                    store.execute(
                        "UPDATE receipts SET text='' WHERE id IN (SELECT id FROM messages WHERE scope=?)",
                        (scope,),
                    )
                    store.pause(scope, "会话内容已清除；保留去重ID，恢复后仅接收新消息")
                    print("已清除会话内容，保留去重与发送状态")
            elif args.command in {"replay", "watch"}:
                source = JsonlSource(args.input)

                def ingest():
                    events, parse_errors = source.poll()
                    errors.extend(parse_errors)
                    for event in events:
                        try:
                            engine.ingest(event)
                        except (ValueError, TypeError) as exc:
                            errors.append(
                                {
                                    "error": str(exc),
                                    "id": event.get("id")
                                    if isinstance(event, dict)
                                    else None,
                                }
                            )

                if args.command == "replay":
                    # Replay interleaves intake and processing to model normal conversation turns.
                    events, parse_errors = source.poll()
                    errors.extend(parse_errors)
                    for event in events:
                        try:
                            engine.ingest(event)
                            engine.drain()
                        except (ValueError, TypeError) as exc:
                            errors.append({"error": str(exc)})
                else:
                    stop = threading.Event()

                    def watcher():
                        while not stop.is_set():
                            try:
                                ingest()
                            except Exception as exc:  # noqa: BLE001 -- Stop the offline reader on unexpected local I/O failures.
                                errors.append({"error": str(exc)})
                                stop.set()
                            stop.wait(config.get("poll_seconds", 0.2))

                    thread = threading.Thread(target=watcher, daemon=True)
                    thread.start()
                    print(
                        "监听本地JSONL，自动回复只写入模拟发件箱。Ctrl+C停止。",
                        flush=True,
                    )
                    last_outbox = store.rows(
                        "SELECT COALESCE(MAX(rowid),0) AS n FROM outbox"
                    )[0]["n"]
                    try:
                        while not stop.is_set():
                            if not engine.process_one():
                                time.sleep(0.1)
                            else:
                                for item in store.rows(
                                    "SELECT rowid AS seq,* FROM outbox WHERE rowid>? ORDER BY rowid",
                                    (last_outbox,),
                                ):
                                    print(
                                        encoded({"event": "offline_outbox", **item}),
                                        flush=True,
                                    )
                                    last_outbox = item["seq"]
                    except KeyboardInterrupt:
                        stop.set()
                    finally:
                        stop.set()
                        thread.join(timeout=5)
            report = {
                "mode": "offline",
                "wechat_access": False,
                "backend": type(model).__name__,
                "errors": errors,
                **store.dump(),
            }
            if args.report:
                Path(args.report).parent.mkdir(parents=True, exist_ok=True)
                Path(args.report).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2)
                )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if args.command in {"replay", "watch"} and (
                errors
                or any(x["status"] in {"failed", "unknown"} for x in report["messages"])
            ):
                raise SystemExit(1)
        finally:
            store.close()


if __name__ == "__main__":
    main()
