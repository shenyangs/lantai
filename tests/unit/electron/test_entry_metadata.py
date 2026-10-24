from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lantai.__version__ import __version__


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_entry_version_is_a_fast_side_effect_free_command() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "electron" / "entry.py"), "--version"],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == f"澜台, version {__version__}"
    assert completed.stderr == ""
