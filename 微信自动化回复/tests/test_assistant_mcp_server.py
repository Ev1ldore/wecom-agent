"""Official MCP client/server round trip and bounded child cleanup."""

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from jsonschema import validate
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.core.assistant.contact_book import contact_id
from src.core.assistant.mcp_server import OUTPUT_SCHEMA, ProcessTools, create_server
from src.core.assistant.mcp_tools import DEFINITIONS, ToolFailure

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_official_client_stdio_roundtrip_and_restart(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"whitelist": [{"conversation": "a", "sender": "alice"}]})
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "src.core.assistant.mcp_server",
            "--config",
            str(path),
            "--db",
            str(tmp_path / "state.db"),
        ],
        cwd=ROOT,
    )
    cid = contact_id("a", "alice")
    with (tmp_path / "stderr.log").open("w") as errors:
        for iteration in range(2):
            async with (
                stdio_client(params, errlog=errors) as (read, write),
                ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=15)
                ) as client,
            ):
                initialized = await client.initialize()
                assert initialized.protocolVersion == "2025-11-25"
                tools = (await client.list_tools()).tools
                assert [t.name for t in tools] == list(DEFINITIONS)
                assert all(
                    t.inputSchema["additionalProperties"] is False for t in tools
                )
                assert all(t.annotations.openWorldHint is False for t in tools)
                if iteration == 0:
                    result = await client.call_tool(
                        "memory_set",
                        {
                            "contact_id": cid,
                            "key": "style",
                            "value": "private-value",
                        },
                    )
                    assert not result.isError
                    validate(result.structuredContent, OUTPUT_SCHEMA)
                    assert (
                        json.loads(result.content[0].text) == result.structuredContent
                    )
                result = await client.call_tool("memory_list", {"contact_id": cid})
                assert (
                    result.structuredContent["data"]["memory"][0]["value"]
                    == "private-value"
                )
                result = await client.call_tool(
                    "memory_set",
                    {
                        "contact_id": cid,
                        "key": "x",
                        "value": "v",
                        "ttl_seconds": True,
                    },
                )
                assert (
                    result.isError
                    and result.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
                )
                validate(result.structuredContent, OUTPUT_SCHEMA)
                data = json.loads(path.read_text())
                data["contacts"] = [
                    {"name": "a", "conversation": "a", "sender": "alice"}
                ]
                data["whitelist"] = []
                path.write_text(json.dumps(data))
                result = await client.call_tool("memory_list", {"contact_id": cid})
                assert (
                    result.isError
                    and result.structuredContent["error"]["code"] == "NOT_ALLOWED"
                )
                data["whitelist"] = [{"conversation": "a", "sender": "alice"}]
                path.write_text(json.dumps(data))
    logs = (tmp_path / "stderr.log").read_text()
    assert "mcp_tool_done" in logs
    assert "private-value" not in logs
    assert "msg_id" in logs


@pytest.mark.asyncio
async def test_worker_timeout_kills_child_once(monkeypatch, tmp_path):
    class Child:
        returncode = None
        killed = False

        async def communicate(self, data=None):
            if not self.killed:
                await asyncio.sleep(10)
            self.returncode = -9
            return b"", b""

        def kill(self):
            self.killed = True

    child = Child()
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    backend = ProcessTools(tmp_path / "config", tmp_path / "db", timeout=0.01)
    with pytest.raises(ToolFailure) as exc:
        await backend.call("contacts_list", {})
    assert exc.value.code == "TIMEOUT"
    assert child.killed and len(calls) == 1


@pytest.mark.asyncio
async def test_worker_cancel_kills_child_without_retry(monkeypatch, tmp_path):
    ready = asyncio.Event()

    class Child:
        returncode = None
        killed = False

        async def communicate(self, data=None):
            ready.set()
            if not self.killed:
                await asyncio.Event().wait()
            self.returncode = -9
            return b"", b""

        def kill(self):
            self.killed = True

    child = Child()

    async def spawn(*args, **kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(
        ProcessTools(tmp_path / "config", tmp_path / "db").call("contacts_list", {})
    )
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.killed


@pytest.mark.asyncio
async def test_stdio_stdout_contains_only_protocol_messages(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"whitelist":[]}')
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.core.assistant.mcp_server",
        "--config",
        str(path),
        "--db",
        str(tmp_path / "db"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=ROOT,
    )
    try:

        async def send(payload):
            process.stdin.write((json.dumps(payload) + "\n").encode())
            await process.stdin.drain()

        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "raw-check", "version": "1"},
                },
            }
        )
        first = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert first["id"] == 1 and "result" in first
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "contacts_list", "arguments": {}},
            }
        )
        result = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert result["id"] == 2 and result["result"]["structuredContent"]["data"] == {
            "contacts": []
        }
        process.stdin.close()
        remaining, logs = await asyncio.wait_for(process.communicate(), 10)
        assert not remaining
        assert b"mcp_tool_done" in logs
        assert process.returncode == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


def test_server_has_only_management_handlers():
    class Backend:
        async def call(self, name, arguments):
            raise AssertionError("not invoked")

    server = create_server(Backend())
    assert server.name == "wecom-assistant-management"
