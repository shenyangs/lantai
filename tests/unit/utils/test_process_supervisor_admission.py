# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="inherited POSIX ownership pipes",
)


@pytest.mark.parametrize("entry", ["module", "packaged_entry"])
def test_owner_loss_before_command_admission_confirms_settlement(
    entry: str,
) -> None:
    config_read, config_write = os.pipe()
    life_read, life_write = os.pipe()
    receipt_read, receipt_write = os.pipe()
    descriptors = [
        config_read,
        config_write,
        life_read,
        life_write,
        receipt_read,
        receipt_write,
    ]
    process = None
    try:
        # The owner disappears before a command exists. The private child must
        # positively settle the empty admission instead of leaving a stale
        # fence.
        os.close(life_write)
        descriptors.remove(life_write)
        route = (
            ["-B", "-m", "lantai.utils.posix_process_supervisor"]
            if entry == "module"
            else [
                str(PROJECT_ROOT / "electron" / "entry.py"),
                "--owned-process-supervisor",
            ]
        )
        environment = dict(os.environ)
        environment["LANTAI_RUN_SKILL_SCRIPT_PATH"] = str(
            PROJECT_ROOT
            / "this-private-entry-must-not-initialize-the-skill-bridge.py",
        )
        # The explicit owner settles this process in cleanup.
        # pylint: disable-next=consider-using-with
        process = subprocess.Popen(
            [
                sys.executable,
                *route,
                str(config_read),
                str(life_read),
                str(receipt_write),
                "0",
            ],
            pass_fds=(config_read, life_read, receipt_write),
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        for descriptor in (config_read, life_read, receipt_write):
            os.close(descriptor)
            descriptors.remove(descriptor)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr.decode(
            "utf-8",
            errors="replace",
        )
        assert stdout == b""
        receipt = json.loads(os.read(receipt_read, 65536) or b"{}")
        assert receipt.get("settled") is True
        assert receipt.get("returncode") == 0
        assert "error" not in receipt
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        for descriptor in descriptors:
            os.close(descriptor)
