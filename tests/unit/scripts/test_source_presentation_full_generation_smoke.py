"""Behavior locks for the full real-provider presentation smoke harness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "source_presentation_full_generation_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "source_presentation_full_generation_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("encoding", ["records", "summary"])
@pytest.mark.parametrize(
    ("billing", "expected_status", "expected_amount"),
    [
        ({"provider_cost_status": "unknown", "provider_cost_micro_cny": None}, "unknown", None),
        ({"provider_cost_status": "unknown", "provider_cost_micro_cny": 0}, "unknown", None),
        ({"provider_cost_status": "exact", "provider_cost_micro_cny": 0}, "exact", 0),
        ({"provider_cost_status": "exact", "provider_cost_micro_cny": 125}, "exact", 125),
        ({"provider_cost_status": "exact", "provider_cost_micro_cny": None}, "unknown", None),
        ({"provider_cost_status": "exact", "provider_cost_micro_cny": True}, "unknown", None),
        ({"provider_cost_status": "exact", "provider_cost_micro_cny": -1}, "unknown", None),
        ({"provider_cost_status": "not_applicable", "provider_cost_micro_cny": None}, "not_applicable", None),
        ({"provider_cost_status": "not_applicable", "provider_cost_micro_cny": 0}, "unknown", None),
        ({"provider_cost_micro_cny": 0}, "unknown", None),
        ({"provider_cost_status": "private model reply", "provider_cost_micro_cny": 125}, "unknown", None),
    ],
)
def test_stage_evidence_requires_explicit_valid_bill_for_zero_cost(
    encoding, billing, expected_status, expected_amount,
):
    module = _load_module()
    metric = {"duration_ms": 12.5, "cost_units": 47, **billing}
    value = [{"stage": "plan", **metric}] if encoding == "records" else {"plan": metric}

    evidence = module._summarize_stage_metrics(value)["plan"]

    assert evidence["provider_cost_status"] == expected_status
    assert evidence["provider_cost_micro_cny"] == expected_amount
    assert evidence["cost_units"] == 47
    assert "private model reply" not in json.dumps(evidence)


def test_stage_evidence_preserves_unknown_bill_and_known_partial_cost():
    module = _load_module()
    evidence = module._summarize_stage_metrics([
        {"stage": "plan", "provider_cost_status": "exact", "provider_cost_micro_cny": 125,
         "outcome": "passed", "cost_units": 10, "duration_ms": 2.5},
        {"stage": "plan", "provider_cost_status": "unknown", "provider_cost_micro_cny": None,
         "outcome": "failed", "cost_units": 7, "duration_ms": 3.25},
        {"stage": "plan", "provider_cost_status": "not_applicable", "provider_cost_micro_cny": None,
         "outcome": "passed", "cost_units": 1, "duration_ms": 0.5},
    ])["plan"]

    assert evidence == {
        "call_count": 3, "passed_count": 2, "failed_count": 1,
        "duration_ms": 6.25, "cost_units": 18,
        "provider_cost_status": "unknown", "provider_cost_micro_cny": None,
        "provider_cost_known_micro_cny": 125,
        "provider_cost_exact_call_count": 1, "provider_cost_unknown_call_count": 1,
        "provider_cost_not_applicable_call_count": 1,
    }
    assert module._summarize_stage_metrics({"plan": evidence}) == {"plan": evidence}


def test_stage_evidence_sums_exact_bills_without_charging_non_provider_stages():
    module = _load_module()
    evidence = module._summarize_stage_metrics([
        {"stage": "quality", "provider_cost_status": "exact", "provider_cost_micro_cny": 125},
        {"stage": "quality", "provider_cost_status": "exact", "provider_cost_micro_cny": 0},
        {"stage": "quality", "provider_cost_status": "not_applicable", "provider_cost_micro_cny": None},
        {"stage": "render", "provider_cost_status": "not_applicable", "provider_cost_micro_cny": None},
    ])

    assert evidence["quality"]["provider_cost_status"] == "exact"
    assert evidence["quality"]["provider_cost_micro_cny"] == 125
    assert evidence["quality"]["provider_cost_exact_call_count"] == 2
    assert evidence["quality"]["provider_cost_not_applicable_call_count"] == 1
    assert evidence["render"]["provider_cost_status"] == "not_applicable"
    assert evidence["render"]["provider_cost_micro_cny"] is None


def test_legacy_stage_record_has_unknown_cost_and_discards_private_text():
    module = _load_module()
    evidence = module._summarize_stage_metrics([
        {"stage": "plan", "outcome": "passed", "cost_units": 47,
         "response": "private model reply", "prompt": "private user prompt"},
    ])["plan"]

    assert evidence["provider_cost_status"] == "unknown"
    assert evidence["provider_cost_micro_cny"] is None
    assert evidence["provider_cost_unknown_call_count"] == 1
    assert "private" not in json.dumps(evidence)


@pytest.mark.parametrize("target_kind", ["request", "material", "secret", "job", "resume", "recovery", "output", "hardlink", "symlink"])
def test_full_cli_rejects_evidence_input_alias_before_provider(tmp_path, monkeypatch, target_kind):
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = workspace / "request.json"
    material = workspace / "material.json"
    material.write_text('{"synthetic":"source"}')
    request.write_text(json.dumps({"materials": [{"path": "material.json"}]}))
    directories = {name: tmp_path / name for name in ["secret", "job", "resume", "recovery", "output"]}
    for path in directories.values():
        path.mkdir()
    targets = {name: path / "evidence.json" for name, path in directories.items()}
    targets.update({"request": request, "material": material})
    hardlink = tmp_path / "hardlink.json"
    hardlink.hardlink_to(request)
    targets["hardlink"] = hardlink
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(request)
    targets["symlink"] = symlink
    monkeypatch.setattr(module, "run", lambda **kwargs: pytest.fail("must validate before staging provider"))
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT_PATH),
        "--request", str(request), "--job-root", str(directories["job"]), "--output-dir", str(directories["output"]),
        "--workspace-root", str(workspace), "--source-working-dir", str(tmp_path / "working"),
        "--source-secret-dir", str(directories["secret"]), "--resume-job-root", str(directories["resume"]),
        "--failure-recovery-root", str(directories["recovery"]), "--evidence-output", str(targets[target_kind])])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert json.loads(request.read_text()) == {"materials": [{"path": "material.json"}]}
    assert material.read_text() == '{"synthetic":"source"}'


def test_full_cli_rejects_evidence_inside_completed_retention_before_provider(tmp_path, monkeypatch, capsys):
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = workspace / "request.json"
    request.write_text('{"materials":[]}')
    retained = tmp_path / "private-retained"
    retained.mkdir()
    monkeypatch.setattr(module, "run", lambda **_kwargs: pytest.fail("must reject evidence alias before Provider"))
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT_PATH),
        "--request", str(request), "--job-root", str(workspace / "jobs"),
        "--output-dir", str(workspace / "output"), "--workspace-root", str(workspace),
        "--source-working-dir", str(tmp_path / "working"),
        "--source-secret-dir", str(tmp_path / "secret"),
        "--completed-job-retention-root", str(retained),
        "--evidence-output", str(retained / "evidence.json"),
    ])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert "evidence output collides with protected presentation data" in capsys.readouterr().err
    assert not (retained / "evidence.json").exists()


def test_safe_child_failure_preserves_valid_source_origin_but_not_untrusted_text():
    module = _load_module()
    kwargs = dict(source_head="a" * 40, provider_id="provider", model="model", request_summary={},
                  material_summaries=[], elapsed_ms=1, returncode=1, stdout="", stderr="")
    origin = {"file": "scripts/source_presentation_full_generation_smoke.py", "function": "_inner_run", "line": 1}
    evidence = module._safe_child_failure(**kwargs, child={"error_type": "ValueError", "error_origin": origin})
    assert evidence["child"]["error_origin"] == origin
    for bad in [dict(origin, file="../../private.json"), dict(origin, function="private_model_response"),
                dict(origin, line=True), dict(origin, line=999999), "private model response"]:
        evidence = module._safe_child_failure(**kwargs, child={"error_type": "ValueError", "error_origin": bad})
        assert evidence["child"]["error_origin"] is None
        assert "private_model_response" not in json.dumps(evidence)


@pytest.mark.parametrize("phase", ["first_output", "idle", "total"])
def test_safe_exception_preserves_typed_timeout_phase_without_private_text(phase):
    from lantai.app.utility_model import UtilityTaskTimeoutError

    module = _load_module()
    error = UtilityTaskTimeoutError(phase=phase, timeout_seconds=90)
    error.args = ("synthetic_private_model_reply_and_credential",)
    evidence = module._safe_exception(error)
    assert evidence["utility_timeout"] == {"phase": phase, "timeout_seconds": 90.0}
    assert "synthetic_private" not in json.dumps(evidence)


@pytest.mark.parametrize("payload", [
    None, "synthetic_private_reply", [], {},
    {"phase": "synthetic_private_reply", "timeout_seconds": 90},
    {"phase": ["total"], "timeout_seconds": 90},
    {"phase": "total", "timeout_seconds": "90"},
    {"phase": "total", "timeout_seconds": True},
    {"phase": "total", "timeout_seconds": 0},
    {"phase": "total", "timeout_seconds": -1},
    {"phase": "total", "timeout_seconds": float("nan")},
    {"phase": "total", "timeout_seconds": float("inf")},
    {"phase": "total", "timeout_seconds": 10**400},
])
def test_timeout_child_boundary_rejects_unknown_or_nonfinite_payload(payload):
    module = _load_module()
    kwargs = dict(source_head="a" * 40, provider_id="provider", model="model", request_summary={},
                  material_summaries=[], elapsed_ms=1, returncode=1, stdout="", stderr="")
    evidence = module._safe_child_failure(**kwargs, child={
        "error_type": "UtilityTaskTimeoutError", "utility_timeout": payload,
    })
    assert evidence["child"]["utility_timeout"] is None
    assert "synthetic_private" not in json.dumps(evidence)


def test_timeout_metadata_is_revalidated_and_cannot_be_spoofed_by_other_exceptions():
    from lantai.app.utility_model import UtilityTaskTimeoutError

    module = _load_module()
    kwargs = dict(source_head="a" * 40, provider_id="provider", model="model", request_summary={},
                  material_summaries=[], elapsed_ms=1, returncode=1, stdout="", stderr="")
    payload = {"phase": "total", "timeout_seconds": 90, "reply": "synthetic_private_reply"}
    for error_type, expected in (("UtilityTaskTimeoutError", {"phase": "total", "timeout_seconds": 90.0}),
                                 ("ValueError", None)):
        evidence = module._safe_child_failure(**kwargs, child={"error_type": error_type, "utility_timeout": payload})
        assert evidence["child"]["utility_timeout"] == expected
        assert "synthetic_private" not in json.dumps(evidence)
    shadow_type = type("UtilityTaskTimeoutError", (TimeoutError,), {})
    for error in (ValueError("synthetic_private_reply"), shadow_type("synthetic_private_reply")):
        error.phase, error.timeout_seconds = "total", 90
        assert "utility_timeout" not in module._safe_exception(error)
    malformed = UtilityTaskTimeoutError(phase="total", timeout_seconds=90)
    malformed.phase = "synthetic_private_reply"
    assert "utility_timeout" not in module._safe_exception(malformed)


def test_safe_exception_keeps_only_schema_owned_failure_locations():
    from lantai.app.structured_utility import StructuredTaskValidationError

    module = _load_module()
    private = "private_model_reply_and_credential"
    exc = StructuredTaskValidationError(private, validation_issues=(
        "missing@$.actions[0].update_chart.unit",
        "extra_forbidden@$.actions[0].replace_text." + private,
        "private_error@$.slide_id",
        "missing@$." + "x" * 1024,
        "missing@$.actions[-1]",
        "missing@$.actions[100000000]",
        "missing@$",
    ))
    safe = module._safe_exception(exc)
    assert safe["schema_issues"] == [
        {"type": "missing", "path": "$.actions[0].update_chart.unit"},
        {"type": "extra_forbidden", "path": "$.actions[0].replace_text.<redacted>"},
        {"type": "missing", "path": "$"},
    ]
    assert private not in json.dumps(safe)
    ordinary = ValueError(private)
    ordinary.validation_issues = ("missing@$.slide_id",)
    assert module._safe_exception(ordinary)["schema_issues"] == []


def test_schema_failure_is_revalidated_at_child_boundary_and_bounded():
    module = _load_module()
    private = "private_model_reply_and_credential"
    kwargs = dict(source_head="a" * 40, provider_id="provider", model="model", request_summary={},
                  material_summaries=[], elapsed_ms=1, returncode=1, stdout="", stderr="")
    raw = [
        {"type": "missing", "path": "$.actions[0].update_chart.unit", "response": private},
        {"type": private, "path": "$.slide_id"},
        {"type": "extra_forbidden", "path": "$." + private},
        {"type": "missing", "path": "$.actions[-1]"},
        {"type": "missing", "path": "$.actions[999999]"},
        {"type": "missing", "path": "$." + "x" * 1024},
        {"type": "missing", "path": "$..slide_id"},
        {"type": "missing", "path": "$", "credentials": private},
    ]
    safe = module._safe_child_failure(**kwargs, child={"error_type": "StructuredTaskValidationError", "schema_issues": raw})
    assert safe["child"]["schema_issues"] == [
        {"type": "missing", "path": "$.actions[0].update_chart.unit"},
        {"type": "extra_forbidden", "path": "$.<redacted>"},
        {"type": "missing", "path": "$"},
    ]
    assert private not in json.dumps(safe)
    assert module._safe_child_failure(**kwargs, child={"error_type": "ValueError", "schema_issues": raw})["child"]["schema_issues"] == []
    assert module._safe_schema_issues(private) == []
    assert module._safe_schema_issues(None) == []
    assert len(module._safe_schema_issues([{"type": "missing", "path": f"$.actions[{i}]"} for i in range(40)])) == 24
    assert module._safe_schema_issues([{"type": "missing", "path": "$"}] * 40) == [{"type": "missing", "path": "$"}]


@pytest.mark.parametrize("case", ["new-success", "new-failure", "resume-success"])
@pytest.mark.parametrize("cleanup_raises", [False, True])
def test_staged_provider_cleanup_is_fail_closed_and_reported(tmp_path, monkeypatch, case, cleanup_raises):
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = workspace / "request.json"
    request.write_text('{"timeout_seconds":30,"materials":[]}')
    profile = tmp_path / "staged-profile"
    root = tmp_path / "private-jobs"
    job_id = "presentation-synthetic"
    job = root / job_id
    job.mkdir(parents=True)
    (job / "manifest.json").write_text('{"status":"failed"}')

    def stage(**kwargs):
        working, secret = profile / "working", profile / "working.secret"
        working.mkdir(parents=True)
        secret.mkdir()
        (secret / "encrypted-provider.json").write_text("synthetic encrypted store")
        return working, secret, {"provider_id": "provider", "model": "model"}

    def cleanup(*args, **kwargs):
        if cleanup_raises:
            raise OSError("private cleanup diagnostic")

    monkeypatch.setattr(module, "_source_head", lambda: "a" * 40)
    monkeypatch.setattr(module, "_load_configured_provider_smoke", lambda: SimpleNamespace(_stage_profile=stage))
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **kwargs: str(profile))
    monkeypatch.setattr(module.shutil, "rmtree", cleanup)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1 if case == "new-failure" else 0,
        stdout=json.dumps({"ok": case != "new-failure", "job_id": job_id}), stderr=""))
    monkeypatch.setattr(module, "_export_passing_pptx", lambda *args: workspace / "exported/presentation.pptx")
    monkeypatch.setattr(module, "_build_safe_evidence", lambda **kwargs: {
        "ok": True, "privacy": module._privacy_summary(durable_job_store_persisted=case == "resume-success")})
    result = module.run(
        source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
        request_path=request, job_root=root, output_dir=workspace / "exported", workspace_root=workspace,
        expected_provider_id="provider", expected_model="model", agent_id="default",
        **({"resume_job_root": root, "resume_job_id": job_id} if case == "resume-success" else {}))
    assert result["ok"] is False
    assert result["cleanup"]["ok"] is False
    assert result["privacy"]["credentials_persisted"] is True
    assert result["privacy"]["staged_provider_store_removed"] is False
    assert result["privacy"]["plaintext_credentials_persisted"] is False
    assert "private cleanup diagnostic" not in json.dumps(result)


def test_safe_evidence_keeps_metrics_but_not_request_or_model_text(tmp_path: Path) -> None:
    module = _load_module()
    output = tmp_path / "presentation.pptx"
    output.write_bytes(b"pptx-bytes")
    quality = tmp_path / "quality-report.json"
    quality.write_text(
        json.dumps(
            {
                "passed": True,
                "delivery_status": "passed",
                "render_verified": True,
                "visual_verified": True,
                "editable": True,
                "rendered_slide_count": 12,
                "repair_rounds": 2,
                "issues": [{"message": "model-authored issue text"}],
                "visual_issues": [{"message": "model-authored visual text"}],
                "renderer_id": "libreoffice",
                "renderer_version": "25.2",
                "visual_critic_id": "kimi-for-coding",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    job_manifest = tmp_path / "manifest.json"
    job_manifest.write_text(
        json.dumps(
            {
                "status": "completed",
                "stage": "completed",
                "generation": 7,
                "attempt_count": 9,
                "recovery_attempt_count": 2,
                "external_recovery_attempt_count": 1,
                "implementation_recovery_attempt_count": 1,
                "cost_budget_units": 10_000,
                "cost_used_units": 420,
                "stage_metrics": {
                    "planning": {
                        "duration_ms": 1234,
                        "attempt_count": 2,
                        "private_text": "never persist me",
                    }
                },
                "events": [{"message": "model-authored event text"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    deck_ir = tmp_path / "deck-ir.json"
    deck_ir.write_text(
        json.dumps(
            {
                "title": "model-authored deck title",
                "slides": [
                    {"objects": [{"type": "text"}, {"type": "chart"}]},
                    {"objects": [{"type": "image"}, {"type": "text"}]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    current = tmp_path / "current.json"
    current.write_text(
        json.dumps(
            {
                "version_id": "version-123",
                "pptx_sha256": "a" * 64,
                "writer": "presentation-runtime",
            }
        ),
        encoding="utf-8",
    )

    evidence = module._build_safe_evidence(
        source_head="b" * 40,
        provider_id="kimi-codingplan",
        model="kimi-for-coding",
        request_summary={"name": "request.json", "sha256": "c" * 64, "bytes": 999},
        material_summaries=[
            {"name": "D02_launch_notes.md", "sha256": "d" * 64, "bytes": 123}
        ],
        elapsed_ms=4321,
        child={
            "ok": True,
            "job_id": "presentation-123",
            "status": "passed",
            "output_path": str(output),
            "quality_path": str(quality),
            "job_manifest_path": str(job_manifest),
            "deck_ir_path": str(deck_ir),
            "current_version_path": str(current),
        },
        exported_output=output,
    )

    rendered = json.dumps(evidence, ensure_ascii=False)
    assert evidence["ok"] is True
    assert evidence["output"]["sha256"] == module._sha256_file(output)
    assert evidence["quality"]["issue_count"] == 1
    assert evidence["quality"]["visual_issue_count"] == 1
    assert evidence["deck"]["slide_count"] == 2
    assert evidence["deck"]["object_type_counts"] == {
        "chart": 1,
        "image": 1,
        "text": 2,
    }
    assert evidence["job"]["stage_metrics"] == {
        "planning": {"attempt_count": 2, "duration_ms": 1234}
    }
    assert evidence["privacy"] == {
        "credentials_persisted": False,
        "provider_store_staged_temporarily": True,
        "durable_job_store_persisted": False,
        "private_job_store_removed": True,
        "captured_stdout_persisted": False,
        "captured_stderr_persisted": False,
    }
    assert "model-authored" not in rendered
    assert "never persist me" not in rendered


def test_safe_failure_records_hashes_not_captured_output() -> None:
    module = _load_module()

    evidence = module._safe_child_failure(
        source_head="a" * 40,
        provider_id="kimi-codingplan",
        model="kimi-for-coding",
        request_summary={"name": "request.json", "sha256": "b" * 64, "bytes": 2},
        material_summaries=[],
        elapsed_ms=250,
        returncode=1,
        stdout="private model response",
        stderr="credential-looking private diagnostic",
        child={"error_type": "RuntimeError", "error_code": "provider_failure"},
    )

    rendered = json.dumps(evidence, ensure_ascii=False)
    assert evidence["ok"] is False
    assert evidence["child"]["returncode"] == 1
    assert evidence["child"]["stdout_bytes"] == len("private model response".encode())
    assert evidence["child"]["stderr_bytes"] == len(
        "credential-looking private diagnostic".encode()
    )
    assert "private model response" not in rendered
    assert "credential-looking" not in rendered


def test_safe_failure_rejects_untrusted_child_strings() -> None:
    module = _load_module()
    private = "private model response must not persist"

    evidence = module._safe_child_failure(
        source_head="a" * 40,
        provider_id="kimi-codingplan",
        model="kimi-for-coding",
        request_summary={"name": "request.json", "sha256": "b" * 64, "bytes": 2},
        material_summaries=[],
        elapsed_ms=250,
        returncode=1,
        stdout="",
        stderr="",
        child={
            "error_type": private,
            "error_code": private,
            "error_sha256": private,
            "job_id": private,
            "status": private,
            "failure_stage": private,
            "failure_routes": [{
                "category": private,
                "recovery_action": private,
                "issue_codes": [private, "content.safe_code"],
                "retry_stage": private,
            }],
        },
    )

    rendered = json.dumps(evidence, ensure_ascii=False)
    assert private not in rendered
    assert evidence["child"]["error_type"] == ""
    assert evidence["child"]["error_code"] == ""
    assert evidence["child"]["error_sha256"] == ""
    assert evidence["child"]["failure_routes"] == [{
        "category": "unknown",
        "recovery_action": "inspect_failure",
        "issue_codes": ["content.safe_code"],
        "retry_stage": "",
        "automatic_retry_allowed": False,
        "requires_human": False,
    }]


def test_failed_job_exposes_structured_route_without_event_message(
    tmp_path: Path,
) -> None:
    module = _load_module()
    manifest = tmp_path / "manifest.json"
    private_message = "当前步骤失败：模型生成的私密诊断不得进入证据"
    manifest.write_text(
        json.dumps(
            {
                "status": "failed",
                "stage": "failed",
                "events": [
                    {
                        "sequence": 41,
                        "stage": "failed",
                        "status": "failed",
                        "message": private_message,
                        "recoverable": True,
                        "failure_routes": [
                            {
                                "category": "content",
                                "recovery_action": "revise_content",
                                "issue_codes": ["content.required_claim_missing"],
                                "slide_ids": ["slide-private-title"],
                                "retry_stage": "compose",
                                "automatic_retry_allowed": False,
                                "requires_human": False,
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = module._failed_job_summary(manifest)

    rendered = json.dumps(summary, ensure_ascii=False)
    assert summary == {
        "error_type": "PresentationJobFailed",
        "error_code": "content.required_claim_missing",
        "error_sha256": module._sha256_bytes(private_message.encode("utf-8")),
        "failure_stage": "failed",
        "event_sequence": 41,
        "recoverable": True,
        "failure_routes": [
            {
                "category": "content",
                "recovery_action": "revise_content",
                "issue_codes": ["content.required_claim_missing"],
                "retry_stage": "compose",
                "automatic_retry_allowed": False,
                "requires_human": False,
            }
        ],
    }
    assert private_message not in rendered
    assert "slide-private-title" not in rendered


def test_passing_export_copies_only_pptx_to_durable_output(tmp_path: Path) -> None:
    module = _load_module()
    private = tmp_path / "private"
    private.mkdir()
    pptx = private / "presentation.pptx"
    pptx.write_bytes(b"native-pptx")
    (private / "manifest.json").write_text(
        '{"request":"must stay private"}',
        encoding="utf-8",
    )
    exported_root = tmp_path / "exported"

    exported = module._export_passing_pptx(
        {"output_path": str(pptx)},
        exported_root,
    )

    assert exported == exported_root / "presentation.pptx"
    assert exported.read_bytes() == b"native-pptx"
    assert sorted(path.name for path in exported_root.iterdir()) == [
        "presentation.pptx",
    ]


def test_full_run_keeps_job_state_private_and_removes_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    material = workspace / "brief.md"
    material.write_text("synthetic", encoding="utf-8")
    request = workspace / "request.json"
    request.write_text(
        json.dumps({
            "timeout_seconds": 30,
            "materials": [{"path": "brief.md"}],
        }),
        encoding="utf-8",
    )
    smoke_root = tmp_path / "private-smoke"
    staged: dict[str, object] = {}

    def stage_profile(**kwargs):
        staged.update(kwargs)
        working = smoke_root / "working"
        secret = smoke_root / "working.secret"
        working.mkdir(parents=True)
        secret.mkdir(parents=True)
        return working, secret, {
            "provider_id": "kimi-codingplan",
            "model": "kimi-for-coding",
        }

    def fake_subprocess(command, **_kwargs):
        private_job_root = Path(command[command.index("--job-root") + 1])
        private_output = Path(command[command.index("--output-dir") + 1])
        assert private_job_root.is_relative_to(smoke_root)
        assert private_output.is_relative_to(smoke_root)
        job_id = "presentation-private"
        job = private_job_root / job_id
        checkpoints = job / "checkpoints"
        versions = job / "versions"
        output = job / "output" / "presentation.pptx"
        checkpoints.mkdir(parents=True)
        versions.mkdir(parents=True)
        output.parent.mkdir(parents=True)
        output.write_bytes(b"native-pptx")
        (job / "manifest.json").write_text(
            json.dumps({
                "status": "completed",
                "stage": "completed",
                "request": "private prompt",
                "stage_metrics": {},
            }),
            encoding="utf-8",
        )
        (checkpoints / "deck-ir.json").write_text(
            '{"title":"private model text","slides":[]}',
            encoding="utf-8",
        )
        (versions / "current.json").write_text("{}", encoding="utf-8")
        quality = private_output / "quality-report.json"
        quality.parent.mkdir(parents=True)
        quality.write_text(
            '{"passed":true,"delivery_status":"passed"}',
            encoding="utf-8",
        )
        child = {
            "ok": True,
            "job_id": job_id,
            "status": "passed",
            "output_path": str(output),
            "quality_path": str(quality),
            "job_manifest_path": str(job / "manifest.json"),
            "deck_ir_path": str(checkpoints / "deck-ir.json"),
            "current_version_path": str(versions / "current.json"),
        }
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(child),
            stderr="",
        )

    monkeypatch.setattr(module, "_source_head", lambda: "a" * 40)
    monkeypatch.setattr(
        module,
        "_load_configured_provider_smoke",
        lambda: SimpleNamespace(_stage_profile=stage_profile),
    )
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(smoke_root))
    monkeypatch.setattr(module.subprocess, "run", fake_subprocess)
    requested_job_root = workspace / "must-not-persist-jobs"
    exported_root = workspace / "exported"

    evidence = module.run(
        source_working_dir=tmp_path / "source-working",
        source_secret_dir=tmp_path / "source-secret",
        request_path=request,
        job_root=requested_job_root,
        output_dir=exported_root,
        workspace_root=workspace,
        expected_provider_id="kimi-codingplan",
        expected_model="kimi-for-coding",
        agent_id="default",
    )

    rendered = json.dumps(evidence, ensure_ascii=False)
    assert evidence["ok"] is True
    assert (exported_root / "presentation.pptx").read_bytes() == b"native-pptx"
    assert not requested_job_root.exists()
    assert not smoke_root.exists()
    assert "private prompt" not in rendered
    assert "private model text" not in rendered
    assert "private-smoke" not in rendered
    assert staged["active_model_override"] == {
        "provider_id": "kimi-codingplan",
        "model": "kimi-for-coding",
    }


def test_passing_full_run_can_retain_one_private_job_for_office_roundtrip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = workspace / "request.json"
    request.write_text('{"timeout_seconds":30,"materials":[]}', encoding="utf-8")
    smoke_root = tmp_path / "private-smoke"

    def stage_profile(**_kwargs):
        working = smoke_root / "working"
        secret = smoke_root / "working.secret"
        working.mkdir(parents=True)
        secret.mkdir()
        (secret / "provider.json").write_text("synthetic encrypted store")
        return working, secret, {"provider_id": "provider", "model": "model"}

    def fake_subprocess(command, **_kwargs):
        job_root = Path(command[command.index("--job-root") + 1])
        job = job_root / "presentation-office-fixture"
        checkpoints = job / "checkpoints"
        checkpoints.mkdir(parents=True)
        output = job / "output" / "presentation.pptx"
        output.parent.mkdir()
        output.write_bytes(b"native-pptx-fixture")
        manifest = job / "manifest.json"
        manifest.write_text('{"status":"completed","request":"private fixture prompt","stage_metrics":{}}')
        deck = checkpoints / "deck-ir.json"
        deck.write_text('{"slides":[],"title":"private fixture response"}')
        current = job / "versions" / "current.json"
        current.parent.mkdir()
        current.write_text('{"version_id":"version-fixture","writer":"presentation-runtime"}')
        quality = job / "output" / "quality-report.json"
        quality.write_text('{"passed":true,"delivery_status":"passed"}')
        unrelated = job_root / "presentation-unrelated"
        unrelated.mkdir()
        (unrelated / "must-not-retain.json").write_text("unrelated fixture")
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "ok": True, "job_id": job.name, "status": "passed",
            "output_path": str(output), "quality_path": str(quality),
            "job_manifest_path": str(manifest), "deck_ir_path": str(deck),
            "current_version_path": str(current),
        }))

    monkeypatch.setattr(module, "_source_head", lambda: "a" * 40)
    monkeypatch.setattr(module, "_load_configured_provider_smoke", lambda: SimpleNamespace(_stage_profile=stage_profile))
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(smoke_root))
    monkeypatch.setattr(module.subprocess, "run", fake_subprocess)
    retained = tmp_path / "private-retained"
    exported = workspace / "exported"
    evidence = module.run(
        source_working_dir=tmp_path / "source-working", source_secret_dir=tmp_path / "source-secret",
        request_path=request, job_root=workspace / "must-not-persist-jobs", output_dir=exported,
        workspace_root=workspace, expected_provider_id="provider", expected_model="model",
        agent_id="default", completed_job_retention_root=retained,
    )

    assert evidence["ok"] is True
    assert evidence["completed_job_retention"] == {
        "retained": True, "job_id": "presentation-office-fixture", "checkpoint_count": 1,
    }
    assert evidence["privacy"]["durable_job_store_persisted"] is True
    assert evidence["privacy"]["private_job_store_removed"] is False
    assert evidence["privacy"]["staged_provider_store_removed"] is True
    assert sorted(path.name for path in retained.iterdir()) == ["presentation-office-fixture"]
    assert (retained / "presentation-office-fixture" / "checkpoints" / "deck-ir.json").is_file()
    assert (exported / "presentation.pptx").read_bytes() == b"native-pptx-fixture"
    assert retained.stat().st_mode & 0o777 == 0o700
    assert not smoke_root.exists()
    rendered = json.dumps(evidence)
    assert "private fixture" not in rendered
    assert "synthetic encrypted store" not in rendered
    assert "private-retained" not in rendered


@pytest.mark.parametrize("target_kind", ["workspace", "provider-working", "provider-secret", "workspace-symlink"])
def test_completed_job_retention_rejects_protected_roots_before_provider(
    tmp_path: Path, monkeypatch, target_kind: str,
) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    working = tmp_path / "provider-working"
    secret = tmp_path / "provider-secret"
    working.mkdir()
    secret.mkdir()
    request = workspace / "request.json"
    request.write_text('{"timeout_seconds":30,"materials":[]}')
    link = tmp_path / "workspace-link"
    link.symlink_to(workspace, target_is_directory=True)
    roots = {"workspace": workspace, "provider-working": working, "provider-secret": secret, "workspace-symlink": link}
    monkeypatch.setattr(module, "_source_head", lambda: "a" * 40)
    monkeypatch.setattr(module, "_load_configured_provider_smoke", lambda: SimpleNamespace(
        _stage_profile=lambda **_kwargs: pytest.fail("must reject retention target before Provider staging")))

    with pytest.raises(ValueError, match="retention root"):
        module.run(
            source_working_dir=working, source_secret_dir=secret, request_path=request,
            job_root=workspace / "jobs", output_dir=workspace / "exported", workspace_root=workspace,
            expected_provider_id="provider", expected_model="model", agent_id="default",
            completed_job_retention_root=roots[target_kind] / "retained",
        )


def test_failed_full_run_retains_only_job_state_in_explicit_private_recovery_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    material = workspace / "brief.md"
    material.write_text("synthetic", encoding="utf-8")
    request = workspace / "request.json"
    request.write_text(
        json.dumps({
            "timeout_seconds": 30,
            "materials": [{"path": "brief.md"}],
        }),
        encoding="utf-8",
    )
    smoke_root = tmp_path / "private-smoke"

    def stage_profile(**_kwargs):
        working = smoke_root / "working"
        secret = smoke_root / "working.secret"
        working.mkdir(parents=True)
        secret.mkdir(parents=True)
        (secret / "provider-secret.json").write_text("encrypted-secret")
        return working, secret, {
            "provider_id": "kimi-codingplan",
            "model": "kimi-for-coding",
        }

    def fake_subprocess(command, **_kwargs):
        private_job_root = Path(command[command.index("--job-root") + 1])
        job_id = "presentation-deadbeef"
        job = private_job_root / job_id
        checkpoints = job / "checkpoints"
        checkpoints.mkdir(parents=True)
        private_message = "private model-authored failure text"
        (job / "manifest.json").write_text(
            json.dumps({
                "status": "failed",
                "stage": "failed",
                "request": "private prompt",
                "events": [{
                    "sequence": 9,
                    "stage": "failed",
                    "status": "failed",
                    "message": private_message,
                    "recoverable": True,
                    "failure_routes": [{
                        "category": "content",
                        "recovery_action": "revise_content",
                        "issue_codes": ["content.required_claim_missing"],
                        "retry_stage": "compose",
                    }],
                }],
            }),
            encoding="utf-8",
        )
        (checkpoints / "page-scene-001.json").write_text(
            '{"slide":{"private":"model text"}}',
            encoding="utf-8",
        )
        child = {
            "ok": False,
            "job_id": job_id,
            "status": "failed",
            "error_type": "PresentationJobFailed",
            "error_code": "content.required_claim_missing",
            "error_sha256": module._sha256_bytes(private_message.encode()),
            "failure_stage": "failed",
            "event_sequence": 9,
            "recoverable": True,
            "failure_routes": [{
                "category": "content",
                "recovery_action": "revise_content",
                "issue_codes": ["content.required_claim_missing"],
                "retry_stage": "compose",
                "automatic_retry_allowed": False,
                "requires_human": False,
            }],
            "job_manifest_path": str(job / "manifest.json"),
        }
        return SimpleNamespace(returncode=1, stdout=json.dumps(child), stderr="")

    monkeypatch.setattr(module, "_source_head", lambda: "a" * 40)
    monkeypatch.setattr(
        module,
        "_load_configured_provider_smoke",
        lambda: SimpleNamespace(_stage_profile=stage_profile),
    )
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(smoke_root))
    monkeypatch.setattr(module.subprocess, "run", fake_subprocess)
    recovery_root = tmp_path / "private-recovery"

    evidence = module.run(
        source_working_dir=tmp_path / "source-working",
        source_secret_dir=tmp_path / "source-secret",
        request_path=request,
        job_root=workspace / "must-not-persist-jobs",
        output_dir=workspace / "exported",
        workspace_root=workspace,
        expected_provider_id="kimi-codingplan",
        expected_model="kimi-for-coding",
        agent_id="default",
        failure_recovery_root=recovery_root,
    )

    recovered_job = recovery_root / "presentation-deadbeef"
    rendered = json.dumps(evidence, ensure_ascii=False)
    assert evidence["ok"] is False
    assert evidence["recovery"] == {
        "retained": True,
        "job_id": "presentation-deadbeef",
        "checkpoint_count": 1,
    }
    assert evidence["privacy"]["durable_job_store_persisted"] is True
    assert evidence["privacy"]["private_job_store_removed"] is False
    assert recovered_job.is_dir()
    assert (recovered_job / "checkpoints" / "page-scene-001.json").is_file()
    assert not (recovery_root / "working.secret").exists()
    assert not smoke_root.exists()
    assert "private prompt" not in rendered
    assert "private model-authored" not in rendered
    assert "provider-secret" not in rendered


def test_resume_run_reuses_retained_job_and_removes_staged_provider_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    material = workspace / "brief.md"
    material.write_text("synthetic", encoding="utf-8")
    request = workspace / "request.json"
    request.write_text(
        json.dumps({
            "timeout_seconds": 30,
            "materials": [{"path": "brief.md"}],
        }),
        encoding="utf-8",
    )
    recovery_root = tmp_path / "private-recovery"
    job_id = "presentation-existing"
    job = recovery_root / job_id
    checkpoints = job / "checkpoints"
    versions = job / "versions"
    output = job / "output" / "presentation.pptx"
    checkpoints.mkdir(parents=True)
    versions.mkdir(parents=True)
    output.parent.mkdir(parents=True)
    (job / "manifest.json").write_text(
        '{"status":"failed","private":"must stay private"}',
        encoding="utf-8",
    )
    smoke_root = tmp_path / "staged-provider"

    def stage_profile(**_kwargs):
        working = smoke_root / "working"
        secret = smoke_root / "working.secret"
        working.mkdir(parents=True)
        secret.mkdir(parents=True)
        (secret / "provider-secret.json").write_text("encrypted-secret")
        return working, secret, {
            "provider_id": "kimi-codingplan",
            "model": "kimi-for-coding",
        }

    def fake_subprocess(command, **_kwargs):
        assert Path(command[command.index("--job-root") + 1]) == recovery_root
        assert command[command.index("--resume-job-id") + 1] == job_id
        assert command[command.index("--resume-strategy") + 1] == "implementation-fix"
        assert command[command.index("--implementation-recovery-basis") + 1] == (
            "d01.deck_critic_media_limit.v1"
        )
        assert command[command.index("--implementation-fix-identity") + 1] == (
            "deck_critic_contact_sheets.v1"
        )
        assert command[command.index("--implementation-failure-category") + 1] == (
            "file"
        )
        assert command[command.index("--implementation-recovery-action") + 1] == (
            "retry_file_stage"
        )
        assert command[command.index("--implementation-issue-code") + 1] == (
            "file.media_block_limit"
        )
        assert command[command.index("--implementation-retry-stage") + 1] == (
            "quality"
        )
        assert command[
            command.index("--implementation-recovery-max-attempts") + 1
        ] == "3"
        assert Path(_kwargs["env"]["LANTAI_PPTXGENJS_MODULE"]).name == "pptxgenjs"
        output.write_bytes(b"resumed-native-pptx")
        (job / "manifest.json").write_text(
            '{"status":"completed","stage":"completed","stage_metrics":{}}',
            encoding="utf-8",
        )
        (checkpoints / "deck-ir.json").write_text(
            '{"title":"private model text","slides":[]}',
            encoding="utf-8",
        )
        (versions / "current.json").write_text("{}", encoding="utf-8")
        quality = job / "quality" / "quality-report.json"
        quality.parent.mkdir(parents=True)
        quality.write_text(
            '{"passed":true,"delivery_status":"passed"}',
            encoding="utf-8",
        )
        child = {
            "ok": True,
            "job_id": job_id,
            "status": "passed",
            "output_path": str(output),
            "quality_path": str(quality),
            "job_manifest_path": str(job / "manifest.json"),
            "deck_ir_path": str(checkpoints / "deck-ir.json"),
            "current_version_path": str(versions / "current.json"),
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(child), stderr="")

    monkeypatch.setattr(module, "_source_head", lambda: "a" * 40)
    monkeypatch.setattr(
        module,
        "_load_configured_provider_smoke",
        lambda: SimpleNamespace(_stage_profile=stage_profile),
    )
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(smoke_root))
    monkeypatch.setattr(module.subprocess, "run", fake_subprocess)
    exported_root = workspace / "exported"

    evidence = module.run(
        source_working_dir=tmp_path / "source-working",
        source_secret_dir=tmp_path / "source-secret",
        request_path=request,
        job_root=workspace / "unused-new-job-root",
        output_dir=exported_root,
        workspace_root=workspace,
        expected_provider_id="kimi-codingplan",
        expected_model="kimi-for-coding",
        agent_id="default",
        resume_job_root=recovery_root,
        resume_job_id=job_id,
        resume_strategy="implementation-fix",
        implementation_recovery_basis="d01.deck_critic_media_limit.v1",
        implementation_fix_identity="deck_critic_contact_sheets.v1",
        implementation_failure_category="file",
        implementation_recovery_action="retry_file_stage",
        implementation_issue_code="file.media_block_limit",
        implementation_retry_stage="quality",
        implementation_recovery_max_attempts=3,
    )

    rendered = json.dumps(evidence, ensure_ascii=False)
    assert evidence["ok"] is True
    assert evidence["job_id"] == job_id
    assert evidence["privacy"]["durable_job_store_persisted"] is True
    assert evidence["privacy"]["private_job_store_removed"] is False
    assert output.read_bytes() == b"resumed-native-pptx"
    assert (exported_root / "presentation.pptx").read_bytes() == b"resumed-native-pptx"
    assert job.is_dir()
    assert not smoke_root.exists()
    assert "must stay private" not in rendered
    assert "private model text" not in rendered
    assert "provider-secret" not in rendered


def test_resume_target_must_be_an_existing_private_job(tmp_path: Path) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_root = tmp_path / "private-recovery"
    job = job_root / "presentation-existing"
    job.mkdir(parents=True)
    (job / "manifest.json").write_text("{}", encoding="utf-8")

    assert module._validate_resume_target(
        resume_job_root=job_root,
        resume_job_id="presentation-existing",
        workspace_root=workspace,
    ) == job_root.resolve()

    try:
        module._validate_resume_target(
            resume_job_root=workspace,
            resume_job_id="presentation-existing",
            workspace_root=workspace,
        )
    except ValueError as exc:
        assert "outside the workspace" in str(exc)
    else:
        raise AssertionError("workspace-owned recovery roots must be rejected")


def test_external_recovery_routes_preserve_safe_failure_identity() -> None:
    module = _load_module()
    model_service_route = SimpleNamespace(
        category=SimpleNamespace(value="model_service"),
        recovery_action=SimpleNamespace(value="retry_model_service"),
        issue_codes=["access_terminated_error"],
        retry_stage=SimpleNamespace(value="compose"),
    )
    failed_event = SimpleNamespace(
        status=SimpleNamespace(value="failed"),
        failure_routes=[model_service_route],
    )
    record = SimpleNamespace(events=[failed_event])

    assert module._external_recovery_route_payloads(record) == [{
        "category": "model_service",
        "recovery_action": "retry_model_service",
        "issue_codes": ["access_terminated_error"],
        "retry_stage": "compose",
        "automatic_retry_allowed": True,
        "requires_human": False,
    }]


def test_interrupted_recovery_reuses_runtime_future_without_retry_mutation() -> None:
    module = _load_module()
    recovered_future = object()

    class _RecoveredRuntime:
        _futures = {"presentation-running": recovered_future}

        def retry_failed(self, *_args, **_kwargs) -> None:
            raise AssertionError("interrupted recovery must not retry a failed job")

        def retry_failed_after_external_recovery(self, *_args, **_kwargs) -> None:
            raise AssertionError("interrupted recovery must not spend external recovery")

        def retry_failed_after_implementation_fix(self, *_args, **_kwargs) -> None:
            raise AssertionError(
                "interrupted recovery must not spend implementation recovery",
            )

    record = SimpleNamespace(
        job_id="presentation-running",
        conversation_id="conversation-a",
        status=SimpleNamespace(value="running"),
    )

    module._dispatch_resume_strategy(
        runtime=_RecoveredRuntime(),
        record=record,
        resume_strategy="interrupted-recovery",
        external_recovery_basis=None,
        external_recovery_max_attempts=None,
        implementation_recovery_basis="unused",
        implementation_fix_identity="unused",
        implementation_failure_category="content",
        implementation_recovery_action="revise_content",
        implementation_issue_code="content.unused",
        implementation_retry_stage="compose",
        implementation_recovery_max_attempts=None,
    )

    assert _RecoveredRuntime._futures[record.job_id] is recovered_future


def test_external_recovery_routes_reject_authorization_failure() -> None:
    module = _load_module()
    authorization_route = SimpleNamespace(
        category=SimpleNamespace(value="authorization"),
        recovery_action=SimpleNamespace(value="request_authorization"),
        issue_codes=["access_terminated_error.client_policy"],
        retry_stage=SimpleNamespace(value="compose"),
    )
    failed_event = SimpleNamespace(
        status=SimpleNamespace(value="failed"),
        failure_routes=[authorization_route],
    )

    try:
        module._external_recovery_route_payloads(
            SimpleNamespace(events=[failed_event]),
        )
    except ValueError as exc:
        assert "model-service" in str(exc)
    else:
        raise AssertionError("authorization failures must not enter external recovery")


def test_smoke_only_external_recovery_override_is_bounded() -> None:
    module = _load_module()

    assert module._normalize_external_recovery_max_attempts(None) is None
    assert module._normalize_external_recovery_max_attempts(4) == 4
    for invalid in (0, 5):
        try:
            module._normalize_external_recovery_max_attempts(invalid)
        except ValueError as exc:
            assert "between 1 and 4" in str(exc)
        else:
            raise AssertionError("smoke recovery override must remain bounded")


def test_smoke_only_implementation_recovery_override_allows_one_extra_attempt() -> None:
    module = _load_module()

    assert module._normalize_implementation_recovery_max_attempts(None) is None
    assert module._normalize_implementation_recovery_max_attempts(3) == 3
    for invalid in (0, 4):
        try:
            module._normalize_implementation_recovery_max_attempts(invalid)
        except ValueError as exc:
            assert "between 1 and 3" in str(exc)
        else:
            raise AssertionError("implementation smoke override must remain bounded")


def test_material_summaries_resolve_only_inside_workspace(tmp_path: Path) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    material = workspace / "brief.md"
    material.write_text("synthetic brief", encoding="utf-8")
    request = workspace / "request.json"
    request.write_text(
        json.dumps(
            {
                "user_request": "full brief",
                "materials": [{"id": "brief", "name": "brief.md", "path": "brief.md"}],
            }
        ),
        encoding="utf-8",
    )

    summaries = module._material_summaries(request, workspace)

    assert summaries == [
        {
            "name": "brief.md",
            "sha256": module._sha256_file(material),
            "bytes": len("synthetic brief".encode()),
        }
    ]

    request.write_text(
        json.dumps(
            {
                "user_request": "full brief",
                "materials": [{"id": "brief", "name": "brief.md", "path": "../outside.md"}],
            }
        ),
        encoding="utf-8",
    )
    try:
        module._material_summaries(request, workspace)
    except ValueError as exc:
        assert "inside workspace" in str(exc)
    else:
        raise AssertionError("out-of-workspace material must be rejected")
