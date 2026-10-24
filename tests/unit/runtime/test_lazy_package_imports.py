"""Regression tests for lightweight package and CLI leaf imports."""
from __future__ import annotations

import os
import subprocess
import sys


def _loaded_modules_after(statement: str) -> set[str]:
    script = f"{statement}\nimport sys\nprint('\\n'.join(sorted(sys.modules)))\n"
    env = os.environ.copy()
    env["LANTAI_WORKING_DIR"] = os.devnull
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
        env=env,
    )
    return set(result.stdout.splitlines())


def test_agent_management_leaf_does_not_import_unrelated_tool_surfaces() -> None:
    loaded = _loaded_modules_after("import lantai.agents.tools.agent_management")

    assert "lantai.agents.tools.browser_workbench" not in loaded
    assert "lantai.agents.tools.presentation" not in loaded
    assert "lantai.agents.tools.web" not in loaded


def test_daemon_commands_leaf_does_not_import_full_runner() -> None:
    loaded = _loaded_modules_after("import lantai.app.runner.daemon_commands")

    assert "lantai.app.runner.runner" not in loaded
    assert "lantai.app.runner.api" not in loaded


def test_public_package_exports_remain_compatible() -> None:
    from lantai.agents import tools
    from lantai.app import runner

    assert callable(tools.read_file)
    assert runner.ChatSpec.__name__ == "ChatSpec"
