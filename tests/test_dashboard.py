"""Run production dashboard action and rendering regressions in Node's VM."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_dashboard_actions_and_live_catalog():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for dashboard behavior tests")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("dashboard.test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
