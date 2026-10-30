"""Settings page (tabs) logic: the node unit suite for static/js/views/settings-model.js. The tabbed page itself needs a real browser
(tools/e2e/polish_settings.py)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TEST = Path(__file__).resolve().parent / "js" / "settings_model.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_settings_model_node_suite():
    r = subprocess.run(["node", str(JS_TEST)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 failed" in r.stdout
