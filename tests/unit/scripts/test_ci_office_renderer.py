# -*- coding: utf-8 -*-
"""Final pixel contracts must have a renderer in every CI execution job."""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "job_id",
    ["unit-tests", "integrated-tests", "coverage-report"],
)
def test_pixel_contract_jobs_install_renderer_before_running_tests(job_id):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"),
    )
    steps = workflow["jobs"][job_id]["steps"]
    renderer_steps = [
        index
        for index, step in enumerate(steps)
        if step.get("uses") == "./.github/actions/setup-office-renderer"
    ]
    test_steps = [
        index
        for index, step in enumerate(steps)
        if "python -m pytest" in step.get("run", "")
        or "python scripts/run_tests.py" in step.get("run", "")
    ]
    assert len(renderer_steps) == 1
    assert test_steps
    assert renderer_steps[0] < min(test_steps)
