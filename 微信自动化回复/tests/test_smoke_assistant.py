"""The management smoke uses only a temporary store and fake model/sender."""

import pytest

from smoke.smoke_assistant import run_assistant_smoke


@pytest.mark.asyncio
async def test_assistant_management_smoke(capsys):
    await run_assistant_smoke()
    assert "whitelist revocation" in capsys.readouterr().out
