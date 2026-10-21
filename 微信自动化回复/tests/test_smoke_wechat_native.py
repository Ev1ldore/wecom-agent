"""Keep the native smoke offline while exercising its full receive/send chain."""

import pytest

from smoke.smoke_wechat_native import run_native_smoke


@pytest.mark.asyncio
async def test_native_smoke_completes_without_real_services(capsys):
    await run_native_smoke()
    output = capsys.readouterr().out
    assert "native wx2 smoke: PASS" in output
    assert "real sends=0" in output
