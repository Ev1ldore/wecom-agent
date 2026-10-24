"""Run the offline Phase smoke without credentials or external message sends."""

import asyncio
import importlib.util
from collections.abc import Awaitable
from typing import Protocol

from smoke.smoke_assistant import run_assistant_smoke
from smoke.smoke_desktop import run_desktop_smoke, run_wx2_smoke
from smoke.smoke_loop import run_offline_loop
from smoke.smoke_personal import run_personal_smoke, run_realtime_smoke
from smoke.smoke_wechat_native import run_native_smoke


class SmokeRunnerProtocol(Protocol):
    """Define the async Phase check invoked by this command-line entry point."""

    def __call__(self) -> Awaitable[None]:
        """Run the smoke check or raise on any failed assertion."""
        raise NotImplementedError


def main() -> int:
    """Exercise desktop and legacy callback flows with mocked external services."""

    async def run_all() -> None:
        """Keep both independent phase checks in one bounded manual entry point."""
        runner: SmokeRunnerProtocol = run_desktop_smoke
        await runner()
        runner = run_offline_loop
        await runner()
        await run_personal_smoke()
        await run_realtime_smoke()
        await run_wx2_smoke()
        await run_native_smoke()
        await run_assistant_smoke()
        if importlib.util.find_spec("mcp") is not None:
            from smoke.smoke_assistant import run_mcp_smoke

            await run_mcp_smoke()
        else:
            print("MCP smoke: SKIP; optional dependency not installed (install .[mcp])")

    asyncio.run(run_all())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
