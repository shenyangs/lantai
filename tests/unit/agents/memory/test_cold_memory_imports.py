"""Public tool and persona modules must work in a fresh Python process."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", [
    "lantai.agents.tools.shell",
    "lantai.agents.persona.user_understanding",
])
def test_memory_fence_does_not_require_app_import_order(module):
    result = subprocess.run(
        [sys.executable, "-c", "import importlib, sys; importlib.import_module(sys.argv[1])", module],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
