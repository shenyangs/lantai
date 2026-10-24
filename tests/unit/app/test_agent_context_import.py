# -*- coding: utf-8 -*-
"""Regression tests for packaged app startup import chains."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_importing_app_module_does_not_hit_agent_context_cycle() -> None:
    """The desktop startup import chain must not fail on agent context import."""

    project_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    pythonpath = str(project_root / "src")
    env["PYTHONPATH"] = (
        f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
        if env.get("PYTHONPATH")
        else pythonpath
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import lantai.app._app; print('import-ok')",
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "import-ok" in result.stdout
