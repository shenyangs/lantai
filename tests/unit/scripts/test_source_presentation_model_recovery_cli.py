"""CLI must expose the existing bounded model-service recovery API."""
from __future__ import annotations

import json

import pytest

from tests.unit.scripts.test_source_presentation_full_generation_smoke import (
    SCRIPT_PATH,
    _load_module,
)


def _argv(tmp_path, *, inner):
    options = [
        str(SCRIPT_PATH), "--request", str(tmp_path / "request.json"),
        "--job-root", str(tmp_path / "jobs"),
        "--output-dir", str(tmp_path / "outputs"),
        "--workspace-root", str(tmp_path / "workspace"),
        "--resume-job-id", "presentation-synthetic-recovery",
        "--resume-strategy", "implementation-fix",
    ]
    if inner:
        options.append("--inner")
    else:
        options.extend([
            "--source-working-dir", str(tmp_path / "working"),
            "--source-secret-dir", str(tmp_path / "working.secret"),
            "--resume-job-root", str(tmp_path / "jobs"),
        ])
    return options


@pytest.mark.parametrize("inner", [False, True])
def test_model_service_recovery_reaches_existing_boundary_without_overrides(
    tmp_path, monkeypatch, capsys, inner,
):
    module = _load_module()
    calls = []

    def boundary(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(module, "_inner_resume" if inner else "run", boundary)
    monkeypatch.setattr(module.sys, "argv", [*_argv(tmp_path, inner=inner),
        "--implementation-failure-category", "model_service",
        "--implementation-recovery-action", "retry_model_service",
        "--implementation-issue-code", "model_output_truncated",
        "--implementation-retry-stage", "plan",
        "--implementation-recovery-basis", "synthetic-bounded-capacity-evidence",
        "--implementation-fix-identity", "synthetic-verified-source",
    ])
    module.main()
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert len(calls) == 1
    forwarded = calls[0]
    assert forwarded["resume_strategy"] == "implementation-fix"
    assert forwarded["implementation_failure_category"] == "model_service"
    assert forwarded["implementation_recovery_action"] == "retry_model_service"
    assert forwarded["implementation_issue_code"] == "model_output_truncated"
    assert forwarded["implementation_retry_stage"] == "plan"
    assert forwarded["implementation_recovery_basis"] == "synthetic-bounded-capacity-evidence"
    assert forwarded["implementation_fix_identity"] == "synthetic-verified-source"
    assert forwarded["implementation_recovery_max_attempts"] is None
    assert forwarded["external_recovery_max_attempts"] is None
    assert forwarded["external_recovery_basis"] is None


@pytest.mark.parametrize("inner", [False, True])
@pytest.mark.parametrize("option,value", [
    ("--implementation-failure-category", "authorization"),
    ("--implementation-recovery-action", "ignore_errors"),
])
def test_unapproved_recovery_options_still_rejected_before_boundary(
    tmp_path, monkeypatch, inner, option, value,
):
    module = _load_module()

    def forbidden(**_kwargs):
        pytest.fail("invalid recovery options must not reach production boundary")

    monkeypatch.setattr(module, "run", forbidden)
    monkeypatch.setattr(module, "_inner_resume", forbidden)
    monkeypatch.setattr(module.sys, "argv", [*_argv(tmp_path, inner=inner), option, value])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
