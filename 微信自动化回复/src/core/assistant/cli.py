import argparse
import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Protocol

from src.core.assistant.engine import Engine
from src.core.assistant.management import Management, WhitelistFile
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
        choices=[
            "replay",
            "watch",
            "inspect",
            "resume",
            "clear",
            "resolve",
            "doctor",
            "whitelist-list",
            "whitelist-add",
            "whitelist-remove",
            "session-show",
            "session-reset",
            "memory-list",
            "memory-set",
            "memory-forget",
            "attachment-add",
            "attachment-list",
            "attachment-forget",
        ],
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
    parser.add_argument("--key")
    parser.add_argument("--value")
    parser.add_argument("--ttl-seconds", type=int)
    parser.add_argument("--attachment", help="相对配置 inbox 的本地附件路径")
    parser.add_argument(
        "--kind", choices=["file", "voice", "image", "video"], default="file"
    )
    parser.add_argument("--text", default="")
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    whitelist_file = WhitelistFile(config_path)
    config = whitelist_file.read()
    if args.command.startswith("whitelist-"):
        if args.command == "whitelist-list":
            result = config["whitelist"]
        else:
            if not args.conversation or not args.sender:
                parser.error("需要 --conversation 和 --sender")
            result = whitelist_file.change(
                args.conversation,
                args.sender,
                remove=args.command == "whitelist-remove",
            )
        print(encoded({"whitelist": result, "wechat_access": False}))
        return
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
            if args.command.startswith(("memory-", "attachment-", "session-")):
                if not args.conversation or not args.sender:
                    parser.error("需要 --conversation 和 --sender")
                scope = encoded([args.conversation, args.sender])
                manager = Management(engine)
                manager.authorize(scope)
                if args.command == "session-show":
                    print(encoded({"short_term": manager.short_term(scope)}))
                elif args.command == "session-reset":
                    manager.reset_short_term(scope)
                    print(
                        encoded(
                            {"short_term": manager.short_term(scope), "reset": True}
                        )
                    )
                elif args.command == "memory-list":
                    print(encoded({"memory": manager.memories(scope)}))
                elif args.command == "memory-set":
                    manager.set_memory(scope, args.key, args.value, args.ttl_seconds)
                    print(encoded({"memory": manager.memories(scope)}))
                elif args.command == "memory-forget":
                    print(encoded({"removed": manager.forget_memory(scope, args.key)}))
                elif args.command == "attachment-list":
                    print(encoded({"documents": manager.documents(scope)}))
                elif args.command == "attachment-forget":
                    if not args.message_id:
                        parser.error(
                            "需要 --message-id（attachment-list 返回的文档ID）"
                        )
                    print(
                        encoded(
                            {"removed": manager.forget_document(scope, args.message_id)}
                        )
                    )
                else:
                    if not args.attachment:
                        parser.error("需要 --attachment，相对配置 inbox 的文件路径")
                    event = {
                        "id": args.message_id or str(uuid.uuid4()),
                        "conversation": args.conversation,
                        "sender": args.sender,
                        "kind": args.kind,
                        "attachment": args.attachment,
                        "text": args.text,
                    }
                    intake = engine.ingest(event)
                    engine.drain()
                    key = hashlib.sha256(
                        encoded([scope, event["id"]]).encode()
                    ).hexdigest()
                    row = store.rows(
                        "SELECT status,detail FROM messages WHERE id=?", (key,)
                    )[0]
                    print(
                        encoded(
                            {
                                "message_id": key,
                                "intake": intake,
                                **row,
                                "outbox": store.rows(
                                    "SELECT text,status,evidence FROM outbox WHERE id=?",
                                    (key,),
                                ),
                                "wechat_access": False,
                            }
                        )
                    )
                    if row["status"] in {"failed", "unknown"}:
                        raise SystemExit(1)
                return
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
                    for table in ("history", "memory", "documents", "context_resets"):
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

                def refresh_whitelist():
                    # Serialize reads with application so two threads cannot apply
                    # an older file snapshot after a newer revocation.
                    with store.lock:
                        engine.update_whitelist(whitelist_file.read())

                def ingest():
                    refresh_whitelist()
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
                                engine.update_whitelist({"whitelist": []})
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
                            refresh_whitelist()
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
