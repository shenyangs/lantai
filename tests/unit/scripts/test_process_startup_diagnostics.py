# -*- coding: utf-8 -*-
"""Crash fixtures must expose a child startup error without masking its
cause.
"""

import importlib
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "test_runtime_process_crash_recovery",
        "test_runtime_native_crash_resume",
        "test_steering_control_l3",
        "test_steering_control_recovery_l3",
    ],
)
def test_ready_wait_reports_child_startup_failure(tmp_path, module_name):
    module = importlib.import_module(f"tests.integration.{module_name}")
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; print('ready-service-failure', file=sys.stde"
                "rr); sys.exit(7)"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            with pytest.raises(AssertionError, match="ready-service-failure"):
                getattr(module, "_wait_for_json")(
                    tmp_path / "ready.json",
                    process=process,
                    timeout=2,
                )
            assert process.returncode == 7
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=2)
