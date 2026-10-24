"""Privacy and ownership locks for real-provider presentation edit smoke."""

from __future__ import annotations

import importlib.util
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/source_presentation_object_edit_smoke.py"


def _load():
    spec = importlib.util.spec_from_file_location("presentation_edit_smoke", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("method", ["review", "review_slide", "review_slide_cancellable", "review_deck", "review_deck_cancellable"])
def test_critic_failure_observation_preserves_exception_and_never_exposes_response(method):
    module = _load()
    private_message = "synthetic private model response and credential"
    failure = ValueError(private_message)

    def fail(*args, **kwargs):
        raise failure

    critic = SimpleNamespace(**{method: fail})
    diagnostics = []
    module._observe_critic_errors(critic, diagnostics)
    with pytest.raises(ValueError) as raised:
        getattr(critic, method)(SimpleNamespace(slide_id="slide-005"))
    assert raised.value is failure
    assert diagnostics[0]["method"] == method
    assert diagnostics[0]["error_type"] == "ValueError"
    assert diagnostics[0]["error_sha256"] == hashlib.sha256(
        ("ValueError:" + private_message).encode(),
    ).hexdigest()
    assert private_message not in json.dumps(diagnostics)


def test_critic_diagnostic_allowlist_removes_arbitrary_fields_and_paths():
    module = _load()
    raw = [
        {"method": "review_slide", "slide_id": "slide-005", "error_type": "ValueError",
         "error_sha256": "a" * 64, "error_origin": {"file": "/private/credential.py", "function": "secret", "line": 1},
         "prompt": "private prompt", "response": "private model response", "credentials": "private credential"},
        {"method": "private_model_response", "error_type": "ValueError", "error_sha256": "b" * 64},
        {"method": ["review_slide"], "error_type": "ValueError", "error_sha256": "b" * 64},
    ]
    safe = module._safe_critic_failures(raw)
    assert len(safe) == 1
    assert safe[0]["error_origin"] is None
    assert safe[0]["error_sha256"] == "a" * 64
    assert "private" not in json.dumps(safe)
    assert module._safe_critic_failures("private response") == []


def test_schema_critic_diagnostics_keep_exception_identity_and_safe_locations_only():
    from lantai.app.structured_utility import StructuredTaskValidationError

    module = _load()
    private = "private_model_reply_and_credential"
    failure = StructuredTaskValidationError(private, validation_issues=(
        "missing@$.issues[0].code", "extra_forbidden@$.issues[0]." + private,
    ))

    def fail(*args, **kwargs):
        raise failure

    critic = SimpleNamespace(review_slide=fail)
    diagnostics = []
    module._observe_critic_errors(critic, diagnostics)
    with pytest.raises(StructuredTaskValidationError) as raised:
        critic.review_slide(SimpleNamespace(slide_id="slide-005"))
    assert raised.value is failure
    safe = module._safe_critic_failures(diagnostics)
    assert safe[0]["schema_issues"] == [
        {"type": "missing", "path": "$.issues[0].code"},
        {"type": "extra_forbidden", "path": "$.issues[0].<redacted>"},
    ]
    assert private not in json.dumps(safe)
    diagnostics[0]["error_type"] = "ValueError"
    assert module._safe_critic_failures(diagnostics)[0]["schema_issues"] == []


@pytest.mark.parametrize("method", ["review", "review_slide", "review_slide_cancellable", "review_deck", "review_deck_cancellable"])
def test_critic_timeout_keeps_typed_phase_and_exact_exception(method):
    from lantai.app.utility_model import UtilityTaskTimeoutError

    module = _load()
    failure = UtilityTaskTimeoutError(phase="idle", timeout_seconds=15)
    failure.args = ("synthetic_private_reply_and_credential",)

    def fail(*args, **kwargs):
        raise failure

    diagnostics = []
    critic = SimpleNamespace(**{method: fail})
    module._observe_critic_errors(critic, diagnostics)
    with pytest.raises(UtilityTaskTimeoutError) as raised:
        getattr(critic, method)(SimpleNamespace(slide_id="slide-002"))
    assert raised.value is failure
    evidence = module._safe_critic_failures(diagnostics)
    assert evidence[0]["utility_timeout"] == {"phase": "idle", "timeout_seconds": 15.0}
    assert "synthetic_private" not in json.dumps(evidence)
    diagnostics[0]["utility_timeout"]["phase"] = "synthetic_private_reply"
    assert module._safe_critic_failures(diagnostics)[0]["utility_timeout"] is None
    diagnostics[0]["utility_timeout"] = {"phase": "total", "timeout_seconds": 90}
    diagnostics[0]["error_type"] = "ValueError"
    assert module._safe_critic_failures(diagnostics)[0]["utility_timeout"] is None


def _target(tmp_path, module):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "private-jobs"
    job = root / "presentation-edit"
    output = job / "output/presentation.pptx"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"base-pptx")
    (job / "manifest.json").write_text(json.dumps({"status": "completed", "timeout_seconds": 30}))
    checkpoint = job / "checkpoints/deck-ir.json"
    checkpoint.parent.mkdir()
    checkpoint.write_text(json.dumps({"slides": [{"id": "slide-005"}]}))
    instruction = tmp_path / "private-instruction.txt"
    instruction.write_text("private user instruction")
    return workspace, root, output, instruction, module.full._sha256_file(output)


def test_edit_target_rejects_workspace_and_stale_base(tmp_path):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    with pytest.raises(ValueError, match="outside"):
        module._validate_edit_target(job_root=workspace, job_id="presentation-edit", workspace_root=workspace,
                                     instruction_file=instruction, base_sha256=sha)
    with pytest.raises(ValueError, match="base"):
        module._validate_edit_target(job_root=root, job_id="presentation-edit", workspace_root=workspace,
                                     instruction_file=instruction, base_sha256="a" * 64)
    instruction_in_repo = workspace / "instruction.txt"
    instruction_in_repo.write_text("private")
    with pytest.raises(ValueError, match="instruction"):
        module._validate_edit_target(job_root=root, job_id="presentation-edit", workspace_root=workspace,
                                     instruction_file=instruction_in_repo, base_sha256=sha)


@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("additional", [[], ["slide-006", "slide-007"],
                                        [f"slide-{index:03}" for index in range(6, 25)]])
@pytest.mark.parametrize("required", [[], ["slide-005"]])
@pytest.mark.parametrize("notes_only,with_output_contract", [(False, False), (True, False), (True, True)])
def test_edit_run_exports_only_verified_pptx_and_removes_profile(tmp_path, monkeypatch, success, additional, required, notes_only, with_output_contract):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    (root / "presentation-edit/checkpoints/deck-ir.json").write_text(json.dumps({
        "slides": [{"id": slide} for slide in ["slide-005", *additional]],
    }))
    profile = tmp_path / "staged-profile"
    stage_calls = []
    output_contract = {"min_han_characters": 5, "max_han_characters": 12, "forbidden_characters": ["一"]}

    def stage(**kwargs):
        stage_calls.append(kwargs)
        working, secret = profile / "working", profile / "working.secret"
        working.mkdir(parents=True)
        secret.mkdir()
        (secret / "encrypted-provider.json").write_text("encrypted store")
        return working, secret, {"provider_id": "kimi-codingplan", "model": "kimi-for-coding"}

    def child(command, **kwargs):
        assert ("--notes-only" in command) is notes_only
        assert ("--notes-output-contract-json" in command) is with_output_contract
        if with_output_contract:
            assert json.loads(command[command.index("--notes-output-contract-json") + 1]) == output_contract
        assert command[command.index("--job-root") + 1] == str(root)
        assert command[command.index("--base-sha256") + 1] == sha
        assert [command[index + 1] for index, value in enumerate(command)
                if value == "--additional-slide-id"] == additional
        assert [command[index + 1] for index, value in enumerate(command)
                if value == "--required-slide-id"] == required
        assert "private user instruction" not in command
        assert kwargs["capture_output"] is True
        assert kwargs["env"]["LANTAI_SECRET_DIR"] == str(profile / "working.secret")
        job = root / "presentation-edit"
        quality = job / "quality/quality-report.json"
        deck = job / "checkpoints/deck-ir.json"
        current = job / "versions/current.json"
        for path in (quality, deck, current):
            path.parent.mkdir(parents=True, exist_ok=True)
        quality.write_text('{"passed":true,"delivery_status":"passed"}')
        deck.write_text('{"title":"private model response","slides":[]}')
        current.write_text('{}')
        result = {"ok": success, "status": "passed" if success else "failed", "job_id": "presentation-edit",
                  "output_path": str(output), "quality_path": str(quality), "deck_ir_path": str(deck),
                  "job_manifest_path": str(job / "manifest.json"), "current_version_path": str(current)}
        result["critic_failures"] = [{"method": "review_slide", "slide_id": "slide-005",
                                      "error_type": "ValueError", "error_sha256": "c" * 64,
                                      "response": "private model response"}]
        result["provider_measurement"] = {
            "observed": True, "model_invocation_count": 999,
            "calls": [{"ordinal": 1, "outcome": "completed", "duration_ms": 12,
                       "usage_origin": "wrapper_recorded", "response": "private model response",
                       "usage": {"input_tokens": 7, "output_tokens": 3,
                                 "total_tokens": 10, "total_tokens_source": "sum_reported_fields"}}],
        }
        if success:
            output.write_bytes(b"verified-edit")
        else:
            result.update({"error_type": "RuntimeError", "error_sha256": "a" * 64})
        return SimpleNamespace(returncode=0 if success else 1, stdout="private diagnostic\n" + json.dumps(result),
                               stderr="private stderr")

    monkeypatch.setattr(module.full, "_source_head", lambda: "b" * 40)
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: SimpleNamespace(_stage_profile=stage))
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **kwargs: str(profile))
    monkeypatch.setattr(module.subprocess, "run", child)
    exported = workspace / "exported"
    result = module.run(source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
                        job_root=root, job_id="presentation-edit", workspace_root=workspace,
                        instruction_file=instruction, slide_id="slide-005", base_sha256=sha,
                        additional_slide_ids=additional, required_slide_ids=required,
                        **({"notes_only": True} if notes_only else {}),
                        **({"notes_output_contract": output_contract} if with_output_contract else {}),
                        output_dir=exported, expected_provider_id="kimi-codingplan", expected_model="kimi-for-coding")

    assert result["ok"] is success
    assert result["run_kind"] == "full-production-presentation-edit"
    assert result["critic_failures"][0]["error_sha256"] == "c" * 64
    assert result["provider_measurement"]["model_invocation_count"] == 1
    assert result["provider_measurement"]["tokens"]["total"]["total"] == 10
    assert "response" not in result["critic_failures"][0]
    assert result["operation"]["base_output_sha256"] == sha
    assert result["operation"]["additional_slide_ids"] == additional
    assert result["operation"]["required_slide_ids"] == required
    assert result["operation"].get("planner_output_mode", "generic") == ("notes_only" if notes_only else "generic")
    if with_output_contract:
        canonical = json.dumps(output_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        assert result["operation"]["notes_output_contract"] == {
            "sha256": hashlib.sha256(canonical).hexdigest(), "bytes": len(canonical), "forbidden_character_count": 1,
        }
    else:
        assert "notes_output_contract" not in result["operation"]
    assert result["source_files"]["object_planner_sha256"] == module.full._sha256_file(
        module.PROJECT_ROOT / "src/lantai/presentation/intelligence.py",
    )
    assert result["source_files"]["object_edit_apply_sha256"] == module.full._sha256_file(
        module.PROJECT_ROOT / "src/lantai/presentation/natural_edit.py",
    )
    assert result["privacy"]["durable_job_store_persisted"] is True
    assert not profile.exists()
    assert root.exists()
    assert instruction.exists()
    rendered = json.dumps(result)
    for private in ("private user instruction", "private model response", "private diagnostic", "private stderr", "encrypted store"):
        assert private not in rendered
    if success:
        assert sorted(path.name for path in exported.iterdir()) == ["presentation.pptx"]
        assert (exported / "presentation.pptx").read_bytes() == b"verified-edit"
    else:
        assert not exported.exists()
        assert result["operation"]["base_preserved_on_failure"] is True
    assert stage_calls[0]["active_model_override"] == {"provider_id": "kimi-codingplan", "model": "kimi-for-coding"}


@pytest.mark.parametrize("additional", [
    ["slide-005"], ["unknown-slide"], ["slide-006", "slide-006"],
    [f"slide-{index:03}" for index in range(10)], ["../private"],
])
def test_edit_smoke_rejects_invalid_additional_scope_before_profile(tmp_path, monkeypatch, additional):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    calls = []

    def unexpected_profile():
        calls.append(True)
        raise AssertionError("invalid scope must not stage credentials or call a provider")

    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", unexpected_profile)
    with pytest.raises(ValueError, match="scope"):
        module.run(
            source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
            job_root=root, job_id="presentation-edit", workspace_root=workspace,
            instruction_file=instruction, slide_id="slide-005", additional_slide_ids=additional,
            base_sha256=sha, output_dir=workspace / "exported",
            expected_provider_id="kimi-codingplan", expected_model="kimi-for-coding",
        )
    assert calls == []
    assert module.full._sha256_file(output) == sha


@pytest.mark.parametrize("required", [["unknown-slide"], ["slide-005", "slide-005"], ["../private"], [""]])
def test_edit_smoke_rejects_invalid_required_scope_before_profile(tmp_path, monkeypatch, required):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    calls = []
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: calls.append(True))
    with pytest.raises(ValueError, match="required.*scope|scope.*required"):
        module.run(
            source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
            job_root=root, job_id="presentation-edit", workspace_root=workspace,
            instruction_file=instruction, slide_id="slide-005", required_slide_ids=required,
            base_sha256=sha, output_dir=workspace / "exported",
            expected_provider_id="kimi-codingplan", expected_model="kimi-for-coding",
        )
    assert calls == []
    assert module.full._sha256_file(output) == sha


@pytest.mark.parametrize("target_kind", ["output", "manifest", "instruction", "secret", "export", "symlink",
                                        "hardlink-output", "hardlink-instruction", "hardlink-secret"])
def test_cli_evidence_collision_is_rejected_before_provider(tmp_path, monkeypatch, target_kind):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    secret = tmp_path / "desktop-secret"
    exported = workspace / "exported"
    targets = {
        "output": output, "manifest": root / "presentation-edit/manifest.json",
        "instruction": instruction, "secret": secret / "evidence.json",
        "export": exported / "evidence.json",
    }
    alias = tmp_path / "evidence.json"
    alias.symlink_to(output)
    targets["symlink"] = alias
    secret.mkdir()
    store = secret / "encrypted-provider.json"
    store.write_text("encrypted store")
    for name, source in (("output", output), ("instruction", instruction), ("secret", store)):
        hardlink = tmp_path / f"hardlink-{name}.json"
        hardlink.hardlink_to(source)
        targets[f"hardlink-{name}"] = hardlink
    monkeypatch.setattr(module, "run", lambda **kwargs: pytest.fail("must not stage or run provider"))
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT),
        "--source-working-dir", str(tmp_path / "desktop-working"), "--source-secret-dir", str(secret),
        "--job-root", str(root), "--job-id", "presentation-edit", "--workspace-root", str(workspace),
        "--instruction-file", str(instruction), "--slide-id", "slide-005", "--base-sha256", sha,
        "--output-dir", str(exported), "--evidence-output", str(targets[target_kind])])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert output.read_bytes() == b"base-pptx"
    assert instruction.read_text() == "private user instruction"
    assert store.read_text() == "encrypted store"


def test_edit_run_rejects_unknown_slide_before_provider(tmp_path, monkeypatch):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: pytest.fail("must not stage provider"))
    with pytest.raises(ValueError, match="slide"):
        module.run(source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
                   job_root=root, job_id="presentation-edit", workspace_root=workspace,
                   instruction_file=instruction, slide_id="slide-05", base_sha256=sha,
                   output_dir=workspace / "exported", expected_provider_id="kimi-codingplan", expected_model="kimi-for-coding")


@pytest.mark.parametrize("raises", [False, True])
def test_cleanup_failure_is_fail_closed_and_privacy_is_truthful(tmp_path, monkeypatch, raises):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    profile = tmp_path / "staged-profile"

    def stage(**kwargs):
        working, secret = profile / "working", profile / "working.secret"
        working.mkdir(parents=True)
        secret.mkdir()
        (secret / "encrypted-provider.json").write_text("encrypted store")
        return working, secret, {"provider_id": "kimi-codingplan", "model": "kimi-for-coding"}

    def failed_cleanup(*args, **kwargs):
        if raises:
            raise OSError("private cleanup diagnostic")

    monkeypatch.setattr(module.full, "_source_head", lambda: "b" * 40)
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: SimpleNamespace(_stage_profile=stage))
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **kwargs: str(profile))
    monkeypatch.setattr(module.shutil, "rmtree", failed_cleanup)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout=json.dumps({"ok": False, "error_type": "RuntimeError"}), stderr=""))
    result = module.run(source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
                        job_root=root, job_id="presentation-edit", workspace_root=workspace,
                        instruction_file=instruction, slide_id="slide-005", base_sha256=sha,
                        output_dir=workspace / "exported", expected_provider_id="kimi-codingplan", expected_model="kimi-for-coding")
    assert result["ok"] is False
    assert result["cleanup"]["ok"] is False
    assert result["privacy"]["credentials_persisted"] is True
    assert result["privacy"]["staged_provider_store_removed"] is False
    assert result["privacy"]["plaintext_credentials_persisted"] is False
    assert result["operation"]["base_preserved_on_failure"] is True
    assert "private cleanup diagnostic" not in json.dumps(result)


def test_edit_run_never_calls_provider_on_wrong_base(tmp_path, monkeypatch):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: pytest.fail("must not stage provider"))
    with pytest.raises(ValueError, match="base"):
        module.run(source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
                   job_root=root, job_id="presentation-edit", workspace_root=workspace, instruction_file=instruction,
                   slide_id="slide-005", base_sha256="a" * 64, output_dir=workspace / "out",
                   expected_provider_id="kimi-codingplan", expected_model="kimi-for-coding")


@pytest.mark.parametrize("page_count", [20, 21])
def test_smoke_page_limit_matches_native_runtime_before_profile(tmp_path, monkeypatch, page_count):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    pages = [f"slide-{index:03}" for index in range(1, page_count + 1)]
    checkpoint = root / "presentation-edit/checkpoints/deck-ir.json"
    checkpoint.write_text(json.dumps({"slides": [{"id": page} for page in pages]}))
    calls = []

    def boundary():
        calls.append(True)
        raise RuntimeError("synthetic profile boundary, no provider call")

    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", boundary)
    arguments = dict(source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
        job_root=root, job_id="presentation-edit", workspace_root=workspace,
        instruction_file=instruction, slide_id=pages[0], additional_slide_ids=pages[1:],
        required_slide_ids=pages, base_sha256=sha, output_dir=workspace / "exported",
        expected_provider_id="kimi-codingplan", expected_model="kimi-for-coding")
    if page_count == 21:
        with pytest.raises(ValueError, match="scope"):
            module.run(**arguments)
        assert calls == []
    else:
        result = module.run(**arguments)
        assert calls == [True]
        assert result["ok"] is False
        assert result["operation"]["additional_slide_ids"] == pages[1:]
        assert result["operation"]["required_slide_ids"] == pages
    assert module.full._sha256_file(output) == sha


def _correction_target(tmp_path, module):
    from lantai.presentation.models import (
        DeckIR, DeckSlide, FactRecord, PresentationBrief, PresentationSourceCoverage,
        PresentationSourceSection, TextObject,
    )

    workspace, root, output, instruction, sha = _target(tmp_path, module)
    old, new = "synthetic inferred approval", "synthetic original-source preference without approval"
    body = new + "\nPrivate original workshop material."
    pin = hashlib.sha256(body.encode()).hexdigest()
    coverage = PresentationSourceCoverage(material_id="workshop", source_name="Synthetic workshop",
        source_sha256=pin, method="inline", status="supported_content_extracted", extracted_chars=len(body))
    deck = DeckIR(id="synthetic-correction", title="Source correction", source_refs=["workshop"],
        source_coverage=[coverage], required_facts=[old],
        facts=[FactRecord(fact_id="approval", text=old, source_refs=["workshop"])],
        slides=[DeckSlide(id="slide-005", intent_id="intent-005", layout_id="freeform",
            source_fact_ids=["approval"], objects=[TextObject(id="title", role="title", text=old)])])
    brief = PresentationBrief(objective="Synthetic evidence", audience="Team", narrative="Source audit",
        source_refs=["workshop"], source_coverage=[coverage],
        source_sections=[PresentationSourceSection(title="Workshop", body=body, source_ref="workshop")])
    (root / "presentation-edit/checkpoints/deck-ir.json").write_text(deck.model_dump_json())
    (root / "presentation-edit/checkpoints/brief.json").write_text(brief.model_dump_json())
    request = dict(original_fact=old, corrected_fact=new, source_ref="workshop",
        source_sha256=pin, source_section_sha256=pin)
    correction_file = tmp_path / "private-source-corrections.json"
    correction_file.write_text(json.dumps([request]))
    return workspace, root, output, instruction, sha, correction_file, request


@pytest.mark.parametrize("invalid", ["invented", "pin", "section", "unknown", "eleven", "empty", "malformed", "private-schema"])
def test_source_correction_smoke_fails_before_profile_without_excerpts(tmp_path, monkeypatch, invalid):
    module = _load()
    workspace, root, output, instruction, sha, corrections, request = _correction_target(tmp_path, module)
    payload = [request.copy()]
    if invalid == "invented":
        payload[0]["corrected_fact"] = "synthetic private invented model response"
    elif invalid == "pin":
        payload[0]["source_sha256"] = "b" * 64
    elif invalid == "section":
        payload[0]["source_section_sha256"] = "c" * 64
    elif invalid == "unknown":
        payload[0]["original_fact"] = "synthetic private unknown lock"
    elif invalid == "eleven":
        payload *= 11
    elif invalid == "empty":
        payload = []
    elif invalid == "private-schema":
        payload[0]["private_model_response"] = "synthetic private model response"
    corrections.write_text("private malformed JSON" if invalid == "malformed" else json.dumps(payload))
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: pytest.fail("must not stage provider"))
    with pytest.raises(ValueError, match="source.fact.correction") as failure:
        module.run(source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
            job_root=root, job_id="presentation-edit", workspace_root=workspace,
            instruction_file=instruction, source_fact_corrections_file=corrections,
            slide_id="slide-005", base_sha256=sha, output_dir=workspace / "exported",
            expected_provider_id="kimi-codingplan", expected_model="kimi-for-coding")
    assert "private" not in str(failure.value) and request["original_fact"] not in str(failure.value)
    assert before == {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("protected", ["workspace", "job", "secret", "instruction", "hardlink", "symlink-secret"])
def test_source_correction_file_cannot_alias_protected_inputs(tmp_path, monkeypatch, protected):
    module = _load()
    workspace, root, output, instruction, sha, corrections, request = _correction_target(tmp_path, module)
    secret = tmp_path / "desktop-secret"
    secret.mkdir()
    encrypted = secret / "encrypted-provider.json"
    encrypted.write_text("synthetic encrypted store")
    target = corrections
    if protected in {"workspace", "job", "secret"}:
        target = {"workspace": workspace, "job": root, "secret": secret}[protected] / "corrections.json"
        target.write_text(corrections.read_text())
    elif protected == "instruction":
        target = instruction
    elif protected == "hardlink":
        target = tmp_path / "linked-corrections.json"
        target.hardlink_to(corrections)
    else:
        target = tmp_path / "linked-secret.json"
        target.symlink_to(encrypted)
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: pytest.fail("must not stage provider"))
    with pytest.raises(ValueError, match="source.fact.correction"):
        module.run(source_working_dir=tmp_path / "desktop-working", source_secret_dir=secret,
            job_root=root, job_id="presentation-edit", workspace_root=workspace,
            instruction_file=instruction, source_fact_corrections_file=target,
            slide_id="slide-005", base_sha256=sha, output_dir=workspace / "exported",
            expected_provider_id="kimi-codingplan", expected_model="kimi-for-coding")
    assert module.full._sha256_file(output) == sha and encrypted.read_text() == "synthetic encrypted store"


def test_source_correction_file_is_snapshotted_privately_and_only_hashes_exported(tmp_path, monkeypatch):
    module = _load()
    workspace, root, output, instruction, sha, corrections, request = _correction_target(tmp_path, module)
    original = corrections.read_bytes()
    profile = tmp_path / "staged-profile"

    def stage(**kwargs):
        working, secret = profile / "working", profile / "working.secret"
        working.mkdir(parents=True)
        secret.mkdir()
        # Parent has already validated the exact caller input. Later mutation
        # must not alter the request sent to the original Runtime.
        corrections.write_text("synthetic later malformed change")
        return working, secret, {"provider_id": "kimi-codingplan", "model": "kimi-for-coding"}

    def child(command, **kwargs):
        snapshot = Path(command[command.index("--source-fact-corrections-file") + 1])
        assert snapshot != corrections and profile in snapshot.parents
        assert json.loads(snapshot.read_text()) == [request]
        assert request["original_fact"] not in command and request["corrected_fact"] not in command
        return SimpleNamespace(returncode=1, stdout=json.dumps({"ok": False, "error_type": "RuntimeError"}), stderr="")

    monkeypatch.setattr(module.full, "_source_head", lambda: "b" * 40)
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: SimpleNamespace(_stage_profile=stage))
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **kwargs: str(profile))
    monkeypatch.setattr(module.subprocess, "run", child)
    result = module.run(source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
        job_root=root, job_id="presentation-edit", workspace_root=workspace,
        instruction_file=instruction, source_fact_corrections_file=corrections,
        slide_id="slide-005", base_sha256=sha, output_dir=workspace / "exported",
        expected_provider_id="kimi-codingplan", expected_model="kimi-for-coding")
    assert result["operation"]["source_fact_corrections"] == {
        "count": 1, "sha256": hashlib.sha256(original).hexdigest(), "bytes": len(original)}
    assert request["original_fact"] not in json.dumps(result) and request["corrected_fact"] not in json.dumps(result)
    assert not profile.exists() and module.full._sha256_file(output) == sha


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_cli_evidence_cannot_overwrite_caller_source_corrections(tmp_path, monkeypatch, alias):
    module = _load()
    workspace, root, output, instruction, sha, corrections, request = _correction_target(tmp_path, module)
    before = corrections.read_bytes()
    evidence = corrections
    if alias != "same":
        evidence = tmp_path / "evidence.json"
        if alias == "symlink":
            evidence.symlink_to(corrections)
        else:
            evidence.hardlink_to(corrections)
    monkeypatch.setattr(module, "run", lambda **kwargs: pytest.fail("must not stage provider"))
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT),
        "--source-working-dir", str(tmp_path / "desktop-working"), "--source-secret-dir", str(tmp_path / "desktop-secret"),
        "--job-root", str(root), "--job-id", "presentation-edit", "--workspace-root", str(workspace),
        "--instruction-file", str(instruction), "--source-fact-corrections-file", str(corrections),
        "--slide-id", "slide-005", "--base-sha256", sha,
        "--output-dir", str(workspace / "exported"), "--evidence-output", str(evidence)])
    with pytest.raises(SystemExit) as failure:
        module.main()
    assert failure.value.code == 2 and corrections.read_bytes() == before


@pytest.mark.parametrize("case", ["pass", "verify-fails", "job-incomplete", "redo-raises",
                                 "wrong-route", "unselectable", "bad-render-directory"])
@pytest.mark.parametrize("with_corrections", [False, True])
@pytest.mark.parametrize("notes_only,with_output_contract", [(False, False), (True, False), (True, True)])
def test_inner_edit_dispatch_preserves_owner_budget_and_checks_delivery(tmp_path, monkeypatch, case, with_corrections, notes_only, with_output_contract):
    module = _load()
    import lantai.config.config as config
    import lantai.presentation.intelligence as intelligence
    import lantai.presentation.models as models
    import lantai.presentation.quality as quality
    import lantai.presentation.render as render
    import lantai.presentation.runtime as runtime_module
    import lantai.providers.provider_manager as providers

    job_id = "presentation-synthetic"
    root = tmp_path / "jobs"
    job = root / job_id
    for child in ["checkpoints", "quality", "output"]:
        (job / child).mkdir(parents=True)
    (job / "checkpoints/deck-ir.json").write_text("{}")
    (job / "quality/quality-report.json").write_text("{}")
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("synthetic instruction")
    correction_request = dict(original_fact="synthetic inferred approval",
        corrected_fact="synthetic original-source preference", source_ref="workshop",
        source_sha256="b" * 64, source_section_sha256="c" * 64)
    correction_file = tmp_path / "caller-corrections.json"
    correction_file.write_text(json.dumps([correction_request]))
    active = SimpleNamespace(provider_id="wrong" if case == "wrong-route" else "provider", model="model")
    manager = SimpleNamespace(get_active_model=lambda: active,
                              is_chat_model_slot_selectable=lambda _: case != "unselectable")
    monkeypatch.setattr(providers.ProviderManager, "get_instance", classmethod(lambda cls: manager))
    monkeypatch.setattr(config, "load_agent_config", lambda agent: SimpleNamespace(active_model=active))
    adapters, adapter_options = {}, {}
    for name in ["UtilityPresentationObjectEditPlanner", "UtilityPresentationPageAuthor",
                 "UtilityPresentationVisualCritic", "UtilityPresentationRepairDesigner"]:
        def adapter(agent_id, _name=name, **options):
            adapters[_name] = agent_id
            adapter_options[_name] = options
            return (_name, agent_id)
        monkeypatch.setattr(intelligence, name, adapter)

    owner = SimpleNamespace(conversation_id="original-owner", cost_budget_units=100,
                            cost_used_units=42, timeout_seconds=30)
    observed = {"constructor": [], "redo": [], "redo_kwargs": [], "shutdown": 0, "verify": [], "images": []}

    class FakeRuntime:
        def __init__(self, job_root, **kwargs):
            observed["constructor"].append((job_root, kwargs))
            self.service = SimpleNamespace(store=SimpleNamespace(get=lambda target: owner if target == job_id else None))

        def redo_slide(self, *args, **kwargs):
            observed["redo"].append(args)
            observed["redo_kwargs"].append(kwargs)
            if case == "redo-raises":
                raise RuntimeError("synthetic runtime failure")
            return SimpleNamespace(status=models.JobStatus.FAILED if case == "job-incomplete" else models.JobStatus.COMPLETED)

        def shutdown(self):
            observed["shutdown"] += 1

    monkeypatch.setattr(runtime_module, "PresentationJobRuntime", FakeRuntime)
    deck = SimpleNamespace()
    report = SimpleNamespace(render_set_id="../../outside" if case == "bad-render-directory" else "edit-4", repair_rounds=4)
    monkeypatch.setattr(models.DeckIR, "model_validate_json", classmethod(lambda cls, raw: deck))
    monkeypatch.setattr(models.QualityReport, "model_validate_json", classmethod(lambda cls, raw: report))

    def images(path):
        observed["images"].append(path)
        return [path / "slide-1.png"]

    def verify(*args):
        observed["verify"].append(args)
        return case != "verify-fails"

    monkeypatch.setattr(render, "slide_image_paths", images)
    monkeypatch.setattr(quality, "is_verified_delivery", verify)
    kwargs = dict(job_root=root, job_id=job_id, slide_id="slide-005", instruction_file=instruction,
                  base_sha256="a" * 64, agent_id="original-agent", expected_provider_id="provider", expected_model="model")
    if with_corrections:
        kwargs["source_fact_corrections_file"] = correction_file
    if notes_only:
        kwargs["notes_only"] = True
    output_contract = {"min_han_characters": 5, "max_han_characters": 12, "forbidden_characters": ["一"]}
    if with_output_contract:
        kwargs["notes_output_contract"] = output_contract
    if notes_only and with_corrections:
        with pytest.raises(ValueError, match="notes-only.*correction"):
            module._inner_edit(**kwargs)
        assert observed["constructor"] == observed["redo"] == []
        assert adapters == {}
        return
    if case in ["redo-raises", "wrong-route", "unselectable", "bad-render-directory"]:
        with pytest.raises((RuntimeError, ValueError)):
            module._inner_edit(**kwargs)
    else:
        result = module._inner_edit(**kwargs)
        assert result["ok"] is (case == "pass")
        assert result["output_path"] == (str(job / "output/presentation.pptx") if case == "pass" else "")
    if case in ["wrong-route", "unselectable"]:
        assert observed["constructor"] == observed["redo"] == []
        assert observed["shutdown"] == 0
    else:
        assert observed["constructor"][0][0] == root
        assert set(observed["constructor"][0][1]) == {"object_edit_planner", "page_author", "visual_critic", "repair_designer"}
        assert len(adapters) == 4 and set(adapters.values()) == {"original-agent"}
        planner_options = adapter_options["UtilityPresentationObjectEditPlanner"].copy()
        if with_output_contract:
            assert isinstance(planner_options["notes_output_contract"], models.SpeakerNotesOutputContract)
            assert planner_options.pop("notes_output_contract").model_dump(mode="json") == output_contract
        assert planner_options == ({"notes_only": True} if notes_only else {})
        assert all(not options for name, options in adapter_options.items()
                   if name != "UtilityPresentationObjectEditPlanner")
        assert observed["redo"] == [(job_id, "original-owner", "slide-005", "synthetic instruction", "a" * 64)]
        assert observed["redo_kwargs"] == [{"source_fact_corrections": [correction_request]} if with_corrections else {}]
        assert observed["shutdown"] == 1
        assert (owner.cost_budget_units, owner.cost_used_units, owner.timeout_seconds) == (100, 42, 30)
    if case in ["pass", "verify-fails"]:
        assert observed["images"] == [job / "renders/edit-4"]
        assert observed["verify"] == [(deck, report, job / "output/presentation.pptx", [job / "renders/edit-4/slide-1.png"])]
    else:
        assert observed["verify"] == []


def test_inner_correction_revalidation_rejects_private_file_before_provider(tmp_path, monkeypatch):
    import lantai.providers.provider_manager as providers

    module = _load()
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("synthetic instruction")
    corrections = tmp_path / "caller-corrections.json"
    corrections.write_text("private invalid JSON")
    monkeypatch.setattr(providers.ProviderManager, "get_instance",
        classmethod(lambda cls: pytest.fail("invalid caller file must precede Provider selection")))
    with pytest.raises(ValueError, match="source.fact.correction") as failure:
        module._inner_edit(job_root=tmp_path / "jobs", job_id="presentation-synthetic",
            slide_id="slide-005", instruction_file=instruction, base_sha256="a" * 64,
            agent_id="default", expected_provider_id="provider", expected_model="model",
            source_fact_corrections_file=corrections)
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("case", ["pass", "planner-timeout", "critic-failure", "wrong-route"])
def test_inner_edit_measures_real_wrapper_boundary_without_changing_calls(tmp_path, monkeypatch, case):
    from lantai.app.utility_model import UtilityTaskTimeoutError
    from lantai.config import config
    from lantai.presentation import intelligence, models, quality, render
    from lantai.presentation import runtime as runtime_module
    from lantai.providers.provider_manager import ProviderManager
    from lantai.token_usage import TokenRecordingModelWrapper

    module = _load()
    private = "synthetic_private_prompt_reply_and_credential"
    stale = {"observed": True, "calls": [{"outcome": "completed"}]}
    module.full._LAST_PROVIDER_MEASUREMENT = stale
    job_id = "presentation-measured"
    job = tmp_path / "jobs" / job_id
    for path in (job / "checkpoints/deck-ir.json", job / "quality/quality-report.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    instruction = tmp_path / "instruction.txt"
    instruction.write_text(private)
    active = SimpleNamespace(provider_id="wrong" if case == "wrong-route" else "provider", model="model")
    manager = SimpleNamespace(get_active_model=lambda: active, is_chat_model_slot_selectable=lambda _: True)
    monkeypatch.setattr(ProviderManager, "get_instance", classmethod(lambda cls: manager))
    monkeypatch.setattr(config, "load_agent_config", lambda _: SimpleNamespace(active_model=active))
    failure = (UtilityTaskTimeoutError(phase="total", timeout_seconds=90)
               if case == "planner-timeout" else ValueError(private))
    calls, recorded = [], []

    class Transport:
        model_name, stream = "model", False

        async def __call__(self, **kwargs):
            calls.append(kwargs)
            if case == "planner-timeout" or (case == "critic-failure" and len(calls) == 2):
                raise failure
            return SimpleNamespace(content=private, usage={"input_tokens": 7, "output_tokens": 3})

    monkeypatch.setattr(TokenRecordingModelWrapper, "_record_usage", lambda self, usage: recorded.append(usage))
    original_call, original_record = TokenRecordingModelWrapper.__call__, TokenRecordingModelWrapper._record_usage
    wrapper = TokenRecordingModelWrapper("provider", Transport())

    class Critic:
        def review_slide(self, _request):
            return asyncio.run(wrapper(messages=[{"role": "user", "content": private}]))

    monkeypatch.setattr(intelligence, "UtilityPresentationVisualCritic", lambda _: Critic())
    for name in ("UtilityPresentationObjectEditPlanner", "UtilityPresentationPageAuthor", "UtilityPresentationRepairDesigner"):
        monkeypatch.setattr(intelligence, name, lambda _: object())
    owner = SimpleNamespace(conversation_id="retained-owner", cost_budget_units=10000,
                            cost_used_units=1966, timeout_seconds=7200)
    shutdown = []

    class Runtime:
        def __init__(self, _root, **kwargs):
            self.critic = kwargs["visual_critic"]
            self.service = SimpleNamespace(store=SimpleNamespace(get=lambda _: owner))

        def redo_slide(self, *args, **kwargs):
            assert args == (job_id, "retained-owner", "slide-002", private, "a" * 64)
            assert kwargs == {"additional_slide_ids": ["slide-008", "slide-010"],
                              "required_slide_ids": ["slide-002", "slide-008", "slide-010"]}
            asyncio.run(wrapper(messages=[{"role": "user", "content": private}]))
            if case == "critic-failure":
                self.critic.review_slide(SimpleNamespace(slide_id="slide-002"))
            return SimpleNamespace(status=models.JobStatus.COMPLETED)

        def shutdown(self):
            shutdown.append(True)

    monkeypatch.setattr(runtime_module, "PresentationJobRuntime", Runtime)
    monkeypatch.setattr(models.DeckIR, "model_validate_json", classmethod(lambda cls, raw: object()))
    monkeypatch.setattr(models.QualityReport, "model_validate_json", classmethod(
        lambda cls, raw: SimpleNamespace(render_set_id="edit-measured", repair_rounds=0)))
    monkeypatch.setattr(render, "slide_image_paths", lambda _: [])
    monkeypatch.setattr(quality, "is_verified_delivery", lambda *args: True)
    kwargs = dict(job_root=job.parent, job_id=job_id, slide_id="slide-002", instruction_file=instruction,
                  base_sha256="a" * 64, agent_id="original-agent", expected_provider_id="provider",
                  expected_model="model", additional_slide_ids=["slide-008", "slide-010"],
                  required_slide_ids=["slide-002", "slide-008", "slide-010"])
    if case == "pass":
        evidence = module._inner_edit(**kwargs)
        assert evidence["ok"] is True
    else:
        with pytest.raises((ValueError, RuntimeError, UtilityTaskTimeoutError)) as raised:
            module._inner_edit(**kwargs)
        if case != "wrong-route":
            assert raised.value is failure
        evidence = module.full._safe_exception(raised.value)
        if case == "critic-failure":
            assert module._safe_critic_failures(raised.value.presentation_critic_failures)[0]["error_sha256"]
    assert TokenRecordingModelWrapper.__call__ is original_call
    assert TokenRecordingModelWrapper._record_usage is original_record
    assert (owner.cost_budget_units, owner.cost_used_units, owner.timeout_seconds) == (10000, 1966, 7200)
    assert private not in json.dumps(evidence)
    if case == "wrong-route":
        assert calls == recorded == shutdown == []
        assert "provider_measurement" not in evidence
    else:
        expected_calls = 2 if case == "critic-failure" else 1
        report = evidence["provider_measurement"]
        assert report["observed"] is True
        assert report["model_invocation_count"] == len(calls) == expected_calls
        assert report["settled_invocation_count"] == expected_calls
        assert report["duration_ms"]["sample_count"] == expected_calls
        assert report["calls"][-1]["outcome"] == ("completed" if case == "pass" else "failed")
        assert report["tokens"]["input"]["known_sum"] == (0 if case == "planner-timeout" else 7)
        assert report["billing"]["provider_cost_status"] == "unknown"
        assert shutdown == [True]


@pytest.mark.parametrize("inner", [False, True])
@pytest.mark.parametrize("notes_only,with_output_contract", [(False, False), (True, False), (True, True)])
def test_cli_notes_selector_is_explicit_and_preserves_original_arguments(tmp_path, monkeypatch, capsys, inner, notes_only, with_output_contract):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    observed = []

    def dispatch(**kwargs):
        observed.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(module, "_inner_edit" if inner else "run", dispatch)
    args = [str(SCRIPT), "--job-root", str(root), "--job-id", "presentation-edit",
            "--slide-id", "slide-005", "--instruction-file", str(instruction),
            "--base-sha256", sha, "--additional-slide-id", "slide-006",
            "--required-slide-id", "slide-005", "--agent-id", "original-agent",
            "--expected-provider-id", "original-provider", "--expected-model", "original-model"]
    if inner:
        args.append("--inner")
    else:
        args += ["--source-working-dir", str(tmp_path / "desktop-working"),
                 "--source-secret-dir", str(tmp_path / "desktop-secret"),
                 "--workspace-root", str(workspace), "--output-dir", str(workspace / "exported")]
    if notes_only:
        args.append("--notes-only")
    output_contract = {"min_han_characters": 5, "max_han_characters": 12, "forbidden_characters": ["一"]}
    if with_output_contract:
        args += ["--notes-output-contract-json", json.dumps(output_contract)]
    monkeypatch.setattr(module.sys, "argv", args)
    module.main()
    assert len(observed) == 1
    call = observed[0]
    assert call.get("notes_only", False) is notes_only
    if with_output_contract:
        assert call["notes_output_contract"] == output_contract
    else:
        assert "notes_output_contract" not in call
    assert call["instruction_file"] == instruction and call["base_sha256"] == sha
    assert call["agent_id"] == "original-agent"
    assert (call["expected_provider_id"], call["expected_model"]) == ("original-provider", "original-model")
    assert call["additional_slide_ids"] == ["slide-006"] and call["required_slide_ids"] == ["slide-005"]
    assert "private user instruction" not in capsys.readouterr().out
    assert module.full._sha256_file(output) == sha


@pytest.mark.parametrize("flag", ["notes", 1, None])
@pytest.mark.parametrize("inner", [False, True])
def test_smoke_notes_selector_rejects_coercion_before_profile_or_runtime(tmp_path, monkeypatch, flag, inner):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    calls = []
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: calls.append(True))
    kwargs = dict(job_root=root, job_id="presentation-edit", slide_id="slide-005",
                  instruction_file=instruction, base_sha256=sha, agent_id="default",
                  expected_provider_id="provider", expected_model="model", notes_only=flag)
    if not inner:
        kwargs.update(source_working_dir=tmp_path / "desktop-working",
                      source_secret_dir=tmp_path / "desktop-secret", workspace_root=workspace,
                      output_dir=workspace / "exported")
    with pytest.raises(TypeError, match="notes_only"):
        (module._inner_edit if inner else module.run)(**kwargs)
    assert calls == [] and module.full._sha256_file(output) == sha


def test_outer_notes_mode_cannot_change_source_fact_contracts(tmp_path, monkeypatch):
    module = _load()
    workspace, root, output, instruction, sha, corrections, request = _correction_target(tmp_path, module)
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: pytest.fail("no provider staging"))
    with pytest.raises(ValueError, match="notes-only.*correction"):
        module.run(source_working_dir=tmp_path / "desktop-working", source_secret_dir=tmp_path / "desktop-secret",
                   job_root=root, job_id="presentation-edit", workspace_root=workspace,
                   instruction_file=instruction, source_fact_corrections_file=corrections,
                   slide_id="slide-005", base_sha256=sha, output_dir=workspace / "exported",
                   expected_provider_id="provider", expected_model="model", notes_only=True)
    assert module.full._sha256_file(output) == sha


@pytest.mark.parametrize("inner", [False, True])
@pytest.mark.parametrize("notes_only,contract", [
    (False, {"min_han_characters": 5}),
    (True, {"min_han_characters": -1}),
    (True, {"min_han_characters": True}),
    (True, {"max_han_characters": "8"}),
    (True, {"min_han_characters": 9, "max_han_characters": 8}),
    (True, {"forbidden_characters": ["PRIVATE_CONTRACT_VALUE"]}),
    (True, {"PRIVATE_CONTRACT_FIELD": "PRIVATE_CONTRACT_VALUE"}),
    (True, "PRIVATE_CONTRACT_VALUE"),
])
def test_invalid_output_contract_fails_before_provider_without_private_diagnostics(tmp_path, monkeypatch, inner, notes_only, contract):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    monkeypatch.setattr(module.full, "_load_configured_provider_smoke", lambda: pytest.fail("no provider staging"))
    kwargs = dict(job_root=root, job_id="presentation-edit", slide_id="slide-005", instruction_file=instruction,
                  base_sha256=sha, agent_id="default", expected_provider_id="provider", expected_model="model",
                  notes_only=notes_only, notes_output_contract=contract)
    if not inner:
        kwargs.update(source_working_dir=tmp_path / "working", source_secret_dir=tmp_path / "secret",
                      workspace_root=workspace, output_dir=workspace / "exported")
    with pytest.raises(ValueError, match="notes_output_contract") as failure:
        (module._inner_edit if inner else module.run)(**kwargs)
    assert "PRIVATE_CONTRACT" not in str(failure.value)
    assert module.full._sha256_file(output) == sha


@pytest.mark.parametrize("raw", ["PRIVATE_INVALID_JSON", '{"PRIVATE_FIELD":"PRIVATE_VALUE"}',
                                 '{"min_han_characters":true}', '{"forbidden_characters":["PRIVATE_VALUE"]}'])
@pytest.mark.parametrize("inner", [False, True])
def test_cli_output_contract_rejects_invalid_json_privately_before_dispatch(tmp_path, monkeypatch, capsys, raw, inner):
    module = _load()
    workspace, root, output, instruction, sha = _target(tmp_path, module)
    monkeypatch.setattr(module, "_inner_edit", lambda **kwargs: pytest.fail("no inner dispatch"))
    monkeypatch.setattr(module, "run", lambda **kwargs: pytest.fail("no outer dispatch"))
    args = [str(SCRIPT), "--job-root", str(root), "--job-id", "presentation-edit", "--slide-id", "slide-005",
            "--instruction-file", str(instruction), "--base-sha256", sha, "--notes-only",
            "--notes-output-contract-json", raw]
    if inner:
        args.append("--inner")
    else:
        args += ["--source-working-dir", str(tmp_path / "working"), "--source-secret-dir", str(tmp_path / "secret"),
                 "--workspace-root", str(workspace), "--output-dir", str(workspace / "exported")]
    monkeypatch.setattr(module.sys, "argv", args)
    with pytest.raises(SystemExit) as failure:
        module.main()
    assert failure.value.code == 2
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    assert module.full._sha256_file(output) == sha
