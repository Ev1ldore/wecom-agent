"""Official SDK stdio server for isolated local assistant management."""

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Protocol

from jsonschema import validate as validate_schema
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from structlog.contextvars import bound_contextvars

from src.core.logging import get_logger

from .mcp_tools import DEFINITIONS, ERRORS, ToolFailure, validate_arguments

logger = get_logger(__name__)
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "msg_id": {"type": "string"},
        "ok": {"type": "boolean"},
        "data": {"type": "object"},
        "error": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "code": {"type": "string", "enum": list(ERRORS)},
                "message": {"type": "string"},
            },
            "required": ["code", "message"],
        },
    },
    "required": ["msg_id", "ok"],
    "oneOf": [
        {
            "properties": {"ok": {"const": True}},
            "required": ["data"],
            "not": {"required": ["error"]},
        },
        {
            "properties": {"ok": {"const": False}},
            "required": ["error"],
            "not": {"required": ["data"]},
        },
    ],
}


class AsyncToolBackendProtocol(Protocol):
    async def call(self, name: str, arguments: dict) -> dict:
        """Return data or a fixed failure after bounded worker cleanup."""
        raise NotImplementedError


class ProcessTools:
    def __init__(self, config: Path, database: Path, *, timeout: float = 10):
        self.config, self.database, self.timeout = config, database, timeout

    async def call(self, name, arguments):
        args = validate_arguments(name, arguments)
        process = None
        try:
            async with asyncio.timeout(self.timeout):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "src.core.assistant.mcp_tools",
                    str(self.config),
                    str(self.database),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await process.communicate(
                    json.dumps({"name": name, "arguments": args}).encode()
                )
                if process.returncode != 0 or len(stdout) > 1_000_001:
                    raise ToolFailure("STORE_UNAVAILABLE")
                result = json.loads(stdout)
                if result.get("ok") is not True:
                    raise ToolFailure(result.get("error", {}).get("code"))
                if not isinstance(result.get("data"), dict):
                    raise ToolFailure("STORE_UNAVAILABLE")
                return result["data"]
        except TimeoutError:
            raise ToolFailure("TIMEOUT") from None
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            if isinstance(exc, ToolFailure):
                raise
            raise ToolFailure("STORE_UNAVAILABLE") from None
        finally:
            if process is not None and process.returncode is None:
                # No retry: a cancelled or timed-out write may have committed.
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.shield(process.communicate())


def create_server(backend: AsyncToolBackendProtocol) -> Server:
    server = Server("wecom-assistant-management", version="0.1.0")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=name,
                description=description,
                inputSchema=model.model_json_schema(),
                outputSchema=OUTPUT_SCHEMA,
                annotations=types.ToolAnnotations(
                    readOnlyHint=readonly, openWorldHint=False
                ),
            )
            for name, (model, readonly, description) in DEFINITIONS.items()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        msg_id = uuid.uuid4().hex
        started = time.monotonic()
        with bound_contextvars(msg_id=msg_id):
            try:
                args = validate_arguments(name, arguments)
                data = await backend.call(name, args)
                envelope = {"msg_id": msg_id, "ok": True, "data": data}
                code = None
            except ToolFailure as exc:
                code = exc.code
                envelope = {
                    "msg_id": msg_id,
                    "ok": False,
                    "error": {"code": code, "message": str(exc)},
                }
            except Exception:  # noqa: BLE001 -- Fixed error at the protocol boundary.
                code = "STORE_UNAVAILABLE"
                envelope = {
                    "msg_id": msg_id,
                    "ok": False,
                    "error": {"code": code, "message": ERRORS[code]},
                }
            validate_schema(envelope, OUTPUT_SCHEMA)
            logger.info(
                "mcp_tool_done",
                tool=name if name in DEFINITIONS else "unknown",
                code=code,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=json.dumps(envelope, ensure_ascii=False)
                    )
                ],
                structuredContent=envelope,
                isError=not envelope["ok"],
            )

    return server


async def serve(config: Path, database: Path):
    server = create_server(ProcessTools(config, database))
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="本地助手 MCP 管理服务；stdio，不连接微信"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(message)s")
    logging.getLogger(__name__).setLevel(logging.INFO)
    try:
        asyncio.run(serve(args.config.absolute(), args.db.absolute()))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
