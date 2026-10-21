"""Keep the complete offline smoke bounded, including consumer shutdown."""

import subprocess
import sys
from pathlib import Path


def test_offline_smoke_completes_and_exits_without_real_sends() -> None:
    """Catch hangs that unit tests of the worker's finite fake stream cannot see."""
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(project / "smoke.py")],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    assert "smoke.py: PASS" in result.stdout
    assert "no real sends" in result.stdout
    assert "personal smoke: PASS" in result.stdout
    assert "realtime smoke: PASS" in result.stdout
    assert "wx2 smoke: PASS" in result.stdout
    assert "precision=minute" in result.stdout
    assert "real sends=0" in result.stdout
    output = result.stdout + result.stderr
    assert "offline-secret" not in output
    assert "offline-callback-token" not in output
    assert "offline-access-" not in output
    assert "personal-smoke-fake-key" not in output
