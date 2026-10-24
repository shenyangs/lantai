# -*- coding: utf-8 -*-
"""Behavior locks for the system-owned artifact Completion Gate."""

from pathlib import Path
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

from lantai.app import completion_gate as completion_gate_module
from lantai.app.completion_gate import finalize_deliverable
from lantai.app.experience.runtime_runs import record_chat_runtime_run_started
from lantai.app.works import list_works
from lantai.runtime.deliverable_contract import bind_deliverable_contract
from lantai.runtime.artifact_effects import (
    RuntimeArtifactEffectJournal,
    RuntimeArtifactEffectRecord,
)
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from lantai.runtime.store import RuntimeRunStore
from lantai.runtime.runs import (
    RuntimeEffectRecord,
    RuntimeRunOwner,
    build_chat_task_contract,
)


async def _started_contract(
    workspace: Path,
    *,
    run_id: str,
    objective: str,
    acceptance: tuple[str, ...] = (),
    work_method_id: str = "standard",
) -> None:
    contract = build_chat_task_contract(
        objective=objective,
        client_turn_id=f"client-{run_id}",
        complexity="light",
        work_method_id=work_method_id,
        acceptance=acceptance,
    )
    await record_chat_runtime_run_started(
        workspace_dir=workspace,
        agent_id="default",
        chat_id="chat-1",
        session_id="console:chat-1",
        run_id=run_id,
        effective_workspace_dir=str(workspace),
        task_contract=contract,
    )


def _write_png(path: Path, *, color: tuple[int, int, int] = (30, 80, 120)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 36), color).save(path, format="PNG")


def test_unsupported_visual_evidence_is_not_run(tmp_path: Path) -> None:
    unsupported = tmp_path / "preview.txt"
    unsupported.write_text("not a render", encoding="utf-8")

    checks, receipts = completion_gate_module._visual_verification_observation(
        [unsupported],
        tmp_path,
    )

    render = next(item for item in checks if item["id"] == "visual_render_evidence")
    assert render["passed"] is False
    assert render["outcome"] == "not_run"
    assert receipts == ()


def _presentation_attestation_event(
    workspace: Path,
    *,
    run_id: str,
    attempt_id: str,
    deck: Path,
    previews: tuple[Path, ...],
) -> SimpleNamespace:
    render_rows: list[dict[str, object]] = []
    for slide_index, preview in enumerate(previews, start=1):
        with Image.open(preview) as image:
            width, height = image.size
        render_rows.append(
            {
                "slide_id": f"slide-{slide_index}",
                "slide_index": slide_index,
                "path": str(preview),
                "sha256": hashlib.sha256(preview.read_bytes()).hexdigest(),
                "width": width,
                "height": height,
            }
        )
    output_sha256 = hashlib.sha256(deck.read_bytes()).hexdigest()
    quality = {
        "schema_version": "1.0",
        "passed": True,
        "delivery_status": "passed",
        "content_complete": True,
        "editable": True,
        "render_verified": True,
        "vision_critique_required": True,
        "visual_verified": True,
        "render_evidence": "passed",
        "visual_evidence": "passed",
        "editability_evidence": "passed",
        "provenance_evidence": "passed",
        "blocking_gaps": [],
        "output_sha256": output_sha256,
        "rendered_slide_count": len(render_rows),
        "rendered_slides": render_rows,
        "visual_critic_id": "test-system-visual-critic",
    }
    receipt = {
        "job_id": f"job-{run_id}",
        "output_path": str(deck),
        "output_sha256": output_sha256,
        "quality": quality,
        "render_evidence": render_rows,
    }
    receipt_path = workspace / "presentation-tool-receipts" / f"{run_id}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_sha256 = "sha256:" + hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    full_scope = list(range(1, len(render_rows) + 1))
    return SimpleNamespace(
        type="tool.call.completed",
        status="completed",
        source="tool",
        run_id=run_id,
        turn_id=run_id,
        payload={
            "name": "create_presentation",
            "ok": True,
            "attempt_id": attempt_id,
            "canonical_tool": {
                "effect_attempt_id": attempt_id,
            },
            "metadata": {
                "verification_attestation": {
                    "schema_version": 1,
                    "kind": "presentation",
                    "primary_path": str(deck),
                    "primary_sha256": f"sha256:{output_sha256}",
                    "receipt_path": str(receipt_path),
                    "receipt_sha256": receipt_sha256,
                    "source_page_count": len(render_rows),
                    "observed_page_indexes": full_scope,
                    "validator_id": "test-system-visual-critic",
                    "ruleset_id": "presentation-quality",
                    "ruleset_version": "1.0",
                }
            },
        },
    )


def test_validation_error_is_not_recorded_as_quality_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "deck.pptx"
    package.write_bytes(b"not empty")

    class _UnreadableArchive:
        def __init__(self, *_args, **_kwargs) -> None:
            raise OSError("validator storage unavailable")

    monkeypatch.setattr(completion_gate_module.zipfile, "ZipFile", _UnreadableArchive)

    check = completion_gate_module._declared_file_openability_check(package)

    assert check["passed"] is False
    assert check["outcome"] == "error"


def test_validation_metadata_observes_only_checks_with_a_conclusion() -> None:
    checks = (
        completion_gate_module._result("passed", True, "ok"),
        completion_gate_module._result("failed", False, "bad"),
        completion_gate_module._result(
            "not-run",
            False,
            "unsupported",
            outcome="not_run",
        ),
        completion_gate_module._result(
            "error",
            False,
            "validator unavailable",
            outcome="error",
        ),
        completion_gate_module._result(
            "waived",
            True,
            "approved waiver",
            outcome="waived",
            evidence_refs=("approval:user:2026-09-21",),
        ),
    )

    metadata = completion_gate_module._validation_metadata(checks)

    assert metadata.required_scope == (
        "passed",
        "failed",
        "not-run",
        "error",
        "waived",
    )
    assert metadata.observed_scope == ("passed", "failed", "waived")


def _append_committed_finalize_evidence(
    workspace: Path,
    *,
    run_id: str,
    attempt_id: str,
    primary_path: Path,
    effect_step_tool_call_id: str = "call-finalize-visual",
) -> RuntimeArtifactEffectRecord:
    now = datetime.now(timezone.utc)
    effect_id = "effect-finalize-visual"
    journal = RuntimeEffectJournal(workspace)
    effect = journal.append_effect(
        RuntimeEffectRecord(
            run_id=run_id,
            attempt_id=attempt_id,
            step_id=f"tool-call:{effect_step_tool_call_id}",
            effect_id=effect_id,
            source="chat",
            owner=RuntimeRunOwner(
                agent_id="default",
                session_id="console:chat-1",
                workspace_id=str(workspace),
            ),
            status="committed",
            tool_name="finalize_deliverable",
            args_hash="sha256:args",
            policy_snapshot_hash="sha256:policy",
            idempotency_key=f"{run_id}:{attempt_id}:{effect_id}",
            settled_at=now,
            result_hash="sha256:result",
            release_edition="official",
        )
    )
    journal.confirm_effect_receipt(effect)
    artifact = RuntimeArtifactEffectRecord(
        run_id=run_id,
        attempt_id=attempt_id,
        source_effect_id=effect_id,
        tool_call_id="call-finalize-visual",
        tool_name="finalize_deliverable",
        artifact_effect_id="artifact-effect:visual-primary",
        artifact_id="artifact:visual-primary",
        artifact_role="primary",
        status="committed",
        revision=1,
        relative_path=primary_path.relative_to(workspace).as_posix(),
        delivery_idempotency_key="artifact-delivery:visual-primary",
        created_at=now,
        settled_at=now,
        size_bytes=primary_path.stat().st_size,
        sha256=f"sha256:{hashlib.sha256(primary_path.read_bytes()).hexdigest()}",
        media_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
    )
    RuntimeArtifactEffectJournal(workspace).append(artifact)
    return artifact


@pytest.mark.asyncio
async def test_primary_artifact_path_io_failure_is_persisted_as_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = "run-primary-path-io-error"
    await _started_contract(
        tmp_path,
        run_id=run_id,
        objective="Create a report",
    )
    report = tmp_path / "report.md"
    report.write_text("# Report\n", encoding="utf-8")

    def fail_path_validation(_path: str | Path, _root: Path) -> Path:
        raise OSError("artifact storage unavailable")

    monkeypatch.setattr(
        completion_gate_module,
        "_validate_explicit_path",
        fail_path_validation,
    )
    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="Report created.",
        final_response_id="response-primary-path-io-error",
        primary_path=report,
        enforce_effect_settlement=False,
    )

    primary = next(
        item for item in result.acceptance_results
        if item["id"] == "primary_artifact"
    )
    assert primary["passed"] is False
    assert primary["outcome"] == "error"
    assert result.allow_success is False


@pytest.mark.asyncio
async def test_primary_artifact_hash_io_failure_is_persisted_as_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id = "run-primary-hash-io-error"
    await _started_contract(
        tmp_path,
        run_id=run_id,
        objective="Create a report",
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    report = tmp_path / "report.md"
    report.write_text("# Report\n", encoding="utf-8")
    _append_committed_finalize_evidence(
        tmp_path,
        run_id=run_id,
        attempt_id=contract.attempt_id,
        primary_path=report,
    )

    def fail_hash(_path: Path) -> str:
        raise OSError("artifact read failed")

    monkeypatch.setattr(completion_gate_module, "_sha256_file", fail_hash)
    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="Report created.",
        final_response_id="response-primary-hash-io-error",
    )

    identity = next(
        item for item in result.acceptance_results
        if item["id"] == "artifact_content_identity"
    )
    assert identity["passed"] is False
    assert identity["outcome"] == "error"
    assert result.allow_success is False


def _finalize_receipt_event(
    *,
    run_id: str,
    attempt_id: str,
    primary: RuntimeArtifactEffectRecord,
    verification_receipt: dict[str, object],
) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool.call.completed",
        status="completed",
        source="tool",
        run_id=run_id,
        turn_id=run_id,
        item_id="call-finalize-visual",
        payload={
            "id": "call-finalize-visual",
            "name": "finalize_deliverable",
            "tool_name": "finalize_deliverable",
            "ok": True,
            "effect_id": primary.source_effect_id,
            "attempt_id": attempt_id,
            "output": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "primary_path": verification_receipt["primary_path"],
                            "source_run_id": run_id,
                            "delivery_status": "verified",
                            "verification_receipt": verification_receipt,
                        }
                    ),
                }
            ],
            "canonical_tool": {
                "effect_id": primary.source_effect_id,
                "effect_attempt_id": attempt_id,
                "effect_status": "committed",
            },
            "artifact_effects": [
                {
                    "artifact_effect_id": primary.artifact_effect_id,
                    "artifact_id": primary.artifact_id,
                    "artifact_role": "primary",
                    "status": "committed",
                    "relative_path": primary.relative_path,
                    "sha256": primary.sha256,
                    "size_bytes": primary.size_bytes,
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_html_artifact_is_verified_and_automatically_added_to_works(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-html",
        objective="做一个可离线打开的日运网页产品",
    )
    product = tmp_path / "daily-fortune" / "index.html"
    product.parent.mkdir()
    product.write_text(
        "<!doctype html><html><head><title>日运</title></head><body>ok</body></html>",
        encoding="utf-8",
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-html",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="产品已完成。",
        final_response_id="response-1",
        primary_path=product,
    )

    assert result.allow_success is True
    assert result.delivery_status == "verified"
    assert result.primary_path == str(product)
    assert result.work_item is not None
    assert result.evidence.primary_artifact_ids == (result.work_item.artifact_id,)
    assert result.evidence.works_item_ids == (result.work_item.id,)
    assert result.evidence.validation_work_item_ids == (result.work_item.id,)
    assert result.evidence.validation is not None
    assert result.evidence.validation.validator_id == "runtime-completion-gate"
    assert result.evidence.validation.validator_version == "2"
    assert result.evidence.validation.ruleset_version == "2"
    assert len(list_works(tmp_path)) == 1


@pytest.mark.asyncio
async def test_changed_bytes_at_same_run_path_create_a_new_work_candidate(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-html-rewrite",
        objective="生成并反复修订一个 HTML 成果",
    )
    product = tmp_path / "index.html"
    product.write_text(
        "<!doctype html><html><body>version one</body></html>",
        encoding="utf-8",
    )

    first = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-html-rewrite",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="第一版已完成。",
        final_response_id="response-html-v1",
        primary_path=product,
    )
    assert first.candidate_work_item is not None

    product.write_text(
        "<!doctype html><html><body>version two</body></html>",
        encoding="utf-8",
    )
    second = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-html-rewrite",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="第二版已完成。",
        final_response_id="response-html-v2",
        primary_path=product,
    )

    assert second.candidate_work_item is not None
    assert second.candidate_work_item.id != first.candidate_work_item.id
    assert second.candidate_work_item.source_digest != (
        first.candidate_work_item.source_digest
    )
    assert Path(second.candidate_work_item.path).read_text(encoding="utf-8") == (
        "<!doctype html><html><body>version two</body></html>"
    )
    assert second.evidence.validation_work_item_ids == (
        second.candidate_work_item.id,
    )
    assert len(list_works(tmp_path)) == 2


@pytest.mark.asyncio
async def test_game_artifact_prefers_html_over_a_newer_validation_screenshot(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-game",
        objective="帮我做一个模仿超级马里奥的小游戏吧",
    )
    game = tmp_path / "mario-game.html"
    game.write_text(
        "<!doctype html><html><head><title>像素闯关</title></head><body>ok</body></html>",
        encoding="utf-8",
    )
    screenshot = tmp_path / "start.png"
    screenshot.write_bytes(b"validation screenshot")

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-game",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="小游戏已完成。",
        final_response_id="response-game",
        primary_path=game,
    )

    assert result.allow_success is True
    assert result.primary_path == str(game)
    assert result.work_item is not None
    assert result.work_item.source_path == str(game)


@pytest.mark.asyncio
async def test_completion_gate_registers_work_in_the_agent_home_not_topic_state(
    tmp_path: Path,
) -> None:
    agent_home = tmp_path / "agent-home"
    topic_state = agent_home / "topic-workspaces" / "topic-1"
    topic_state.mkdir(parents=True)
    await _started_contract(
        topic_state,
        run_id="run-topic-game",
        objective="做一个小游戏",
    )
    game = topic_state / "game.html"
    game.write_text(
        "<!doctype html><html><body>game</body></html>",
        encoding="utf-8",
    )

    result = await finalize_deliverable(
        state_workspace_dir=topic_state,
        effective_workspace_dir=topic_state,
        works_workspace_dir=agent_home,
        run_id="run-topic-game",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="小游戏已完成。",
        final_response_id="response-topic-game",
        primary_path=game,
    )

    assert result.allow_success is True
    assert len(list_works(agent_home)) == 1
    assert list_works(topic_state) == []


@pytest.mark.asyncio
async def test_artifact_completion_claim_without_finalize_evidence_is_rejected(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-missing",
        objective="创建一个 HTML 网站",
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-missing",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="我已完成。",
        final_response_id="response-2",
    )

    assert result.allow_success is False
    assert result.delivery_status == "not_delivered"
    assert result.failure_summary == (
        "The response claims a requested durable artifact is complete, but no "
        "successful finalize_deliverable evidence exists."
    )
    assert result.evidence.works_item_ids == ()
    assert result.candidate_work_item is None
    assert list_works(tmp_path) == []


@pytest.mark.asyncio
async def test_honest_artifact_blocker_is_not_mislabeled_as_verified_delivery(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-honest-blocker",
        objective="创建一个 HTML 网站",
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-honest-blocker",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="我没能创建网站：写入权限被系统拒绝了。",
        final_response_id="response-honest-blocker",
        durable_artifact_required=True,
    )

    assert result.allow_success is False
    assert result.delivery_status == "not_delivered"
    assert result.evidence.works_item_ids == ()


@pytest.mark.asyncio
async def test_requested_ppt_without_a_finalized_file_is_not_verified(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-missing-ppt",
        objective="根据这份内容做PPT。",
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-missing-ppt",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="附件已收到，现在继续。",
        final_response_id="response-missing-ppt",
        durable_artifact_required=True,
    )

    assert result.allow_success is False
    assert result.delivery_status == "not_delivered"
    assert result.failure_summary == (
        "A durable artifact was requested, but no successful "
        "finalize_deliverable evidence exists."
    )


@pytest.mark.asyncio
async def test_casual_ppt_discussion_remains_a_valid_text_answer(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-ppt-discussion",
        objective="聊聊怎么做PPT，先不用生成文件。",
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-ppt-discussion",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="分析已完成：可以先确定受众、结论和页面节奏。",
        final_response_id="response-ppt-discussion",
        durable_artifact_required=False,
    )

    assert result.allow_success is True
    assert result.delivery_status == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt", ["missing", "confirmed", "stale", "corrupt"]
)
async def test_committed_tool_completion_requires_its_durable_receipt(
    tmp_path: Path,
    receipt: str,
) -> None:
    run_id = "run-tool-receipt"
    await _started_contract(tmp_path, run_id=run_id, objective="完成操作并报告结果。")
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    journal = RuntimeEffectJournal(tmp_path)
    effect = RuntimeEffectRecord(
        run_id=run_id,
        attempt_id=contract.attempt_id,
        step_id="tool-call:write-once",
        effect_id="effect:write-once",
        source="chat",
        owner=contract.owner,
        status="committed",
        tool_name="external_write",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="write-once",
        result_hash="sha256:result",
        settled_at=datetime.now(timezone.utc),
        release_edition="official",
    )
    journal.append_effect(effect)
    if receipt != "missing":
        journal.confirm_effect_receipt(effect)
    if receipt == "stale":
        journal.append_effect(effect.model_copy(update={"revision": 1}))
    elif receipt == "corrupt":
        journal._receipt_path(effect).write_text("not json", encoding="utf-8")

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="操作已完成。",
        final_response_id="response-tool-receipt",
        durable_artifact_required=False,
    )

    settlement = next(
        item
        for item in result.acceptance_results
        if item["id"] == "effects_settled"
    )
    assert result.allow_success is (receipt == "confirmed")
    assert settlement["passed"] is (receipt == "confirmed")
    if receipt != "confirmed":
        assert result.delivery_status == "not_delivered"
        assert effect.effect_id not in result.evidence.settled_effect_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("event_run", ["run-restarted", "another-run"])
async def test_completion_checks_rejected_effects_from_previous_attempts(
    tmp_path: Path,
    event_run: str,
) -> None:
    run_id = "run-restarted"
    await _started_contract(tmp_path, run_id=run_id, objective="检查上一次操作的结果。")
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    blocked = SimpleNamespace(
        type="tool.call.completed",
        turn_id=event_run,
        payload={
            "ok": False,
            "metadata": {
                "invocation_status": "not_started",
                "side_effect_outcome": "committed",
                "automatic_retry_allowed": False,
                "requires_human": True,
            },
            "canonical_tool": {
                "effect_id": "effect:previous-write",
                "effect_attempt_id": "previous-run:chat-attempt-1",
                "effect_status": "committed",
                "effect_revision": 2,
            },
        },
    )
    assert not RuntimeEffectJournal(tmp_path).list_effects(
        run_id, contract.attempt_id
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="检查结束。",
        final_response_id="response-restarted",
        runtime_events=[blocked],
        durable_artifact_required=False,
    )

    assert result.allow_success is (event_run != run_id)
    if event_run == run_id:
        settlement = next(
            item
            for item in result.acceptance_results
            if item["id"] == "effects_settled"
        )
        assert "effect:previous-write" in settlement["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["probe_committed", "human_resolve"])
async def test_verified_recovery_supersedes_an_earlier_unresolved_effect_event(
    tmp_path: Path,
    resolution: str,
) -> None:
    run_id = "run-verified-effect-recovery"
    await _started_contract(tmp_path, run_id=run_id, objective="核验操作结果后结束。")
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    journal = RuntimeEffectJournal(tmp_path)
    ambiguous = RuntimeEffectRecord(
        run_id=run_id,
        attempt_id=contract.attempt_id,
        step_id="tool-call:write-once",
        effect_id="effect:write-once",
        source="chat",
        owner=contract.owner,
        status="ambiguous",
        revision=1,
        tool_name="external_write",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="write-once",
        ambiguity_reason="Receipt needs independent verification.",
        release_edition="official",
    )
    journal.append_effect(ambiguous)
    resolved = reduce_runtime_effect(
        ambiguous,
        RuntimeEffectReducerAction(
            type=resolution, result_hash="sha256:verification"
        ),
    ).effect
    journal.append_effect(resolved)
    earlier = SimpleNamespace(
        type="tool.call.completed",
        turn_id=run_id,
        payload={
            "ok": False,
            "metadata": {
                "side_effect_outcome": "unknown",
                "automatic_retry_allowed": False,
                "requires_human": True,
            },
            "canonical_tool": {
                "effect_id": ambiguous.effect_id,
                "effect_attempt_id": ambiguous.attempt_id,
                "effect_revision": ambiguous.revision,
            },
        },
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="操作结果已核验。",
        final_response_id="response-verified-recovery",
        runtime_events=[earlier],
        durable_artifact_required=False,
    )

    assert result.allow_success is True
    assert result.evidence.settled_effect_ids == (resolved.effect_id,)


@pytest.mark.asyncio
async def test_advisory_effect_settlement_records_an_honest_non_required_failure(
    tmp_path: Path,
) -> None:
    run_id = "run-advisory-effect-settlement"
    await _started_contract(
        tmp_path,
        run_id=run_id,
        objective="给出一段无需文件交付的分析。",
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    RuntimeEffectJournal(tmp_path).append_effect(
        RuntimeEffectRecord(
            run_id=run_id,
            attempt_id=contract.attempt_id,
            step_id="tool-call:call-advisory",
            effect_id="effect-advisory",
            source="chat",
            owner=contract.owner,
            status="running",
            tool_name="read_only_probe",
            args_hash="sha256:args",
            policy_snapshot_hash="sha256:policy",
            idempotency_key=f"{run_id}:effect-advisory",
            release_edition="official",
        )
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="分析已完成。",
        final_response_id="response-advisory-effect-settlement",
        enforce_effect_settlement=False,
        durable_artifact_required=False,
    )

    settlement = next(
        item for item in result.acceptance_results if item["id"] == "effects_settled"
    )
    assert result.allow_success is True
    assert settlement == {
        "id": "effects_settled",
        "passed": False,
        "outcome": "failed",
        "required": False,
        "detail": (
            "Effect settlement is advisory for this run; pending effects are "
            "recorded for recovery: effect-advisory"
        ),
    }


@pytest.mark.asyncio
async def test_successful_finalize_event_recovers_primary_path_for_terminal_gate(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-recovered-finalize",
        objective="创建一份 Markdown 报告",
    )
    report = tmp_path / "report.md"
    report.write_text("# 报告\n", encoding="utf-8")
    runtime_event = SimpleNamespace(
        type="tool.call.completed",
        status="completed",
        payload={
            "name": "finalize_deliverable",
            "ok": True,
            "output": [
                {
                    "type": "text",
                    "text": (
                        '{"primary_path": "'
                        + str(report)
                        + '", "delivery_status": "verified"}'
                    ),
                }
            ],
        },
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-recovered-finalize",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="报告已完成并附上。",
        final_response_id="response-recovered-finalize",
        runtime_events=[runtime_event],
    )

    assert result.allow_success is True
    assert result.delivery_status == "verified"
    assert result.primary_path == str(report)
    assert result.work_item is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("rework_status", ["committed", "failed"])
async def test_terminal_gate_prefers_committed_primary_effect_over_legacy_event(
    tmp_path: Path,
    rework_status: str,
) -> None:
    run_id = "run-artifact-effect-primary"
    await _started_contract(
        tmp_path,
        run_id=run_id,
        objective="创建一份 Markdown 报告",
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    report = tmp_path / "report.md"
    report.write_text("# 报告\n", encoding="utf-8")
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    now = datetime.now(timezone.utc)
    RuntimeArtifactEffectJournal(tmp_path).append(
        RuntimeArtifactEffectRecord(
            run_id=run_id,
            attempt_id=contract.attempt_id,
            source_effect_id="effect-finalize",
            tool_call_id="call-finalize",
            tool_name="finalize_deliverable",
            artifact_effect_id="artifact-effect:primary",
            artifact_id="artifact:primary",
            artifact_role="primary",
            status="committed",
            revision=1,
            relative_path="report.md",
            delivery_idempotency_key="artifact-delivery:primary",
            created_at=now,
            settled_at=now,
            size_bytes=report.stat().st_size,
            sha256=f"sha256:{digest}",
            media_type="text/markdown",
        )
    )
    revised = tmp_path / "report-revised.md"
    revised.write_text("# Revised report\n", encoding="utf-8")
    journal = RuntimeArtifactEffectJournal(tmp_path)
    prior = journal.list_records(run_id, contract.attempt_id)[0]
    journal.append(prior.model_copy(update={
        "artifact_effect_id": "artifact-effect:revised",
        "artifact_id": "artifact:revised",
        "source_effect_id": "effect-finalize-revised",
        "tool_call_id": "call-finalize-revised",
        "status": rework_status,
        "relative_path": revised.name,
        "delivery_idempotency_key": "artifact-delivery:revised",
        "created_at": now + timedelta(seconds=1),
        "settled_at": now + timedelta(seconds=1),
        "sha256": "sha256:" + hashlib.sha256(revised.read_bytes()).hexdigest(),
        "size_bytes": revised.stat().st_size,
    }))
    misleading_event = SimpleNamespace(
        type="tool.call.completed",
        status="completed",
        payload={
            "name": "finalize_deliverable",
            "ok": True,
            "output": [
                {
                    "type": "text",
                    "text": '{"primary_path":"missing-legacy.md"}',
                }
            ],
        },
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="报告已完成并附上。",
        final_response_id="response-artifact-effect",
        runtime_events=[misleading_event],
    )

    assert result.allow_success is True
    expected = revised if rework_status == "committed" else report
    assert result.primary_path == str(expected)
    assert report.read_text(encoding="utf-8") == "# 报告\n"
    assert result.work_item is not None

    if rework_status == "failed":
        failed_turn = await finalize_deliverable(
            state_workspace_dir=tmp_path,
            effective_workspace_dir=tmp_path,
            run_id=run_id,
            chat_id="chat-1",
            response_status="failed",
            assistant_text="新版未完成，保留此前已验证的版本。",
            final_response_id="response-failed-rework",
            runtime_events=[misleading_event],
        )
        assert failed_turn.allow_success is False
        assert failed_turn.delivery_status != "verified"
        assert failed_turn.primary_path == str(report)


@pytest.mark.asyncio
async def test_promised_true_solar_time_cannot_verify_when_artifact_disclaims_it(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-contradiction",
        objective="做一个支持农历和真太阳时的八字网页产品",
    )
    product = tmp_path / "index.html"
    product.write_text(
        "<!doctype html><html><body>暂不支持真太阳时</body></html>",
        encoding="utf-8",
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-contradiction",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="通用完整产品已完成。",
        final_response_id="response-3",
        primary_path=product,
    )

    consistency = next(
        item
        for item in result.acceptance_results
        if item["id"] == "spec_implementation_consistency"
    )
    assert consistency["passed"] is False
    assert result.allow_success is False
    assert result.delivery_status == "partial"
    assert result.candidate_work_item is not None
    assert [item.id for item in list_works(tmp_path)] == [
        result.candidate_work_item.id
    ]


@pytest.mark.asyncio
async def test_print_only_script_is_not_accepted_as_a_passing_test(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-fake-test",
        objective="创建一个 HTML 应用并运行测试",
    )
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body>app</body></html>",
        encoding="utf-8",
    )
    (tmp_path / "app.test.js").write_text(
        "console.log('all tests passed')",
        encoding="utf-8",
    )
    runtime_event = SimpleNamespace(
        type="tool.call.completed",
        status="completed",
        payload={"ok": True, "output": {"exit_code": 0}},
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-fake-test",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="测试通过。",
        final_response_id="response-4",
        primary_path=tmp_path / "index.html",
        runtime_events=[runtime_event],
    )

    test_check = next(
        item
        for item in result.acceptance_results
        if item["id"] == "real_test_execution"
    )
    assert test_check["passed"] is False
    assert result.allow_success is False
    assert result.candidate_work_item is not None
    assert [item.id for item in list_works(tmp_path)] == [
        result.candidate_work_item.id
    ]


@pytest.mark.asyncio
async def test_assertion_suite_with_real_test_exit_code_is_verified(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-real-test",
        objective="创建一个 HTML 应用并运行测试",
    )
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body>app</body></html>",
        encoding="utf-8",
    )
    (tmp_path / "test_app.py").write_text(
        "def test_app():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    runtime_event = SimpleNamespace(
        type="tool.call.completed",
        status="completed",
        payload={
            "ok": True,
            "command": "pytest -q test_app.py",
            "output": {"exit_code": 0},
        },
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-real-test",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="真实测试通过。",
        final_response_id="response-real-test",
        primary_path=tmp_path / "index.html",
        runtime_events=[runtime_event],
    )

    assert result.allow_success is True
    assert result.delivery_status == "verified"
    assert result.work_item is not None


@pytest.mark.asyncio
async def test_plain_text_response_needs_no_artifact(tmp_path: Path) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-answer",
        objective="解释这个错误的原因",
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-answer",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="根因是状态投影混入了旧 Run。",
        final_response_id="response-5",
    )

    assert result.allow_success is True
    assert result.delivery_status == "verified"
    assert result.evidence.works_item_ids == ()


@pytest.mark.asyncio
async def test_bound_pptx_contract_rejects_html_substitute(tmp_path: Path) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-pptx-contract",
        objective="根据附件做一份咖啡馆 PPT",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-pptx-contract",
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )
    substitute = tmp_path / "deck.html"
    substitute.write_text("<!doctype html><title>不是 PPTX</title>", encoding="utf-8")

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-pptx-contract",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="演示文稿已经完成。",
        final_response_id="response-pptx-contract",
        primary_path=substitute,
    )

    extension_check = next(
        item for item in result.acceptance_results
        if item["id"] == "deliverable_extension"
    )
    assert extension_check["passed"] is False
    assert result.allow_success is False
    assert result.delivery_status == "partial"
    assert result.work_item is None


@pytest.mark.asyncio
async def test_bound_file_contract_cannot_complete_with_prose_only(tmp_path: Path) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-file-contract-no-artifact",
        objective="生成一份 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-file-contract-no-artifact",
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-file-contract-no-artifact",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="我已经规划好全部页面。",
        final_response_id="response-no-file",
    )

    assert result.allow_success is False
    assert result.delivery_status == "not_delivered"
    assert result.work_item is None


@pytest.mark.asyncio
async def test_bound_pptx_contract_rejects_corrupt_package(tmp_path: Path) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-corrupt-pptx",
        objective="生成一份 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-corrupt-pptx",
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=False,
    )
    corrupt = tmp_path / "deck.pptx"
    corrupt.write_text("这只是改了扩展名的文本", encoding="utf-8")

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-corrupt-pptx",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="演示文稿已经完成。",
        final_response_id="response-corrupt-pptx",
        primary_path=corrupt,
    )

    check = next(
        item for item in result.acceptance_results
        if item["id"] == "deliverable_openable"
    )
    assert check["passed"] is False
    assert result.allow_success is False
    assert result.work_item is None
    assert result.candidate_work_item is not None
    assert result.candidate_work_item.source_run_id == "run-corrupt-pptx"
    assert result.evidence.validation_work_item_ids == (
        result.candidate_work_item.id,
    )
    assert result.evidence.works_item_ids == ()
    assert result.evidence.validation is not None
    assert "deliverable_openable" in result.evidence.validation.required_scope
    assert [item.id for item in list_works(tmp_path)] == [
        result.candidate_work_item.id
    ]


@pytest.mark.asyncio
async def test_bound_pptx_contract_rejects_default_after_override(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-invalid-opc-order",
        objective="生成一份 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-invalid-opc-order",
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=False,
    )
    malformed = tmp_path / "deck.pptx"
    manifest = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/ppt/presentation.xml" '
        'ContentType="application/presentation+xml"/>'
        '<Default Extension="xlsx" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet"/>'
        "</Types>"
    )
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr("[Content_Types].xml", manifest)
        archive.writestr("ppt/presentation.xml", "<xml />")

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-invalid-opc-order",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="演示文稿已经完成。",
        final_response_id="response-invalid-opc-order",
        primary_path=malformed,
    )

    check = next(
        item
        for item in result.acceptance_results
        if item["id"] == "deliverable_openable"
    )
    assert check["passed"] is False
    assert "Default(xlsx) appears after Override" in check["detail"]
    assert result.allow_success is False
    assert result.work_item is None


@pytest.mark.asyncio
async def test_visual_contract_requires_real_render_evidence(tmp_path: Path) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-pptx-no-render",
        objective="生成并检查一份 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-pptx-no-render",
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )
    deck = tmp_path / "deck.pptx"
    with zipfile.ZipFile(deck, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-pptx-no-render",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="演示文稿已经完成。",
        final_response_id="response-pptx-no-render",
        primary_path=deck,
    )

    check = next(
        item for item in result.acceptance_results
        if item["id"] == "visual_verification_evidence"
    )
    assert check["passed"] is False
    assert result.allow_success is False
    assert result.work_item is None


@pytest.mark.asyncio
async def test_visual_contract_rejects_raw_render_without_visual_review(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-pptx-rendered",
        objective="生成并检查一份 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-pptx-rendered",
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )
    deck = tmp_path / "deck.pptx"
    with zipfile.ZipFile(deck, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")
    preview = tmp_path / "deck-preview.png"
    _write_png(preview)

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-pptx-rendered",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="演示文稿已经完成。",
        final_response_id="response-pptx-rendered",
        primary_path=deck,
        verification_paths=[preview],
    )

    render_check = next(
        item
        for item in result.acceptance_results
        if item["id"] == "visual_render_evidence"
    )
    visual_check = next(
        item
        for item in result.acceptance_results
        if item["id"] == "visual_verification_evidence"
    )
    assert render_check["passed"] is True
    assert visual_check["outcome"] == "not_run"
    assert result.allow_success is False
    assert result.delivery_status == "partial"
    assert result.work_item is None


@pytest.mark.asyncio
async def test_reported_acceptance_notes_cannot_replace_system_visual_review(
    tmp_path: Path,
) -> None:
    """Model-authored notes are context, not system-owned pass/fail verdicts."""

    await _started_contract(
        tmp_path,
        run_id="run-pptx-reported-notes",
        objective="生成并检查一份 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-pptx-reported-notes",
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )
    deck = tmp_path / "lingxi-training-deck.pptx"
    with zipfile.ZipFile(deck, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")
    preview = tmp_path / "thumbnails-1.jpg"
    Image.new("RGB", (64, 36), (120, 80, 30)).save(preview, format="JPEG")

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-pptx-reported-notes",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="演示文稿已经完成并发送。",
        final_response_id="response-pptx-reported-notes",
        primary_path=deck,
        verification_paths=[preview],
        acceptance_evidence=[
            {
                "check": "PPTX generated via pptx skill render wrapper",
                "result": "23 slides, status ok, file size 90.9 KB",
            },
            {
                "check": "Visual verification",
                "result": "Rendered thumbnail montage inspected",
            },
        ],
    )

    reported_notes = [
        item
        for item in result.acceptance_results
        if item["id"].startswith("reported_acceptance:")
    ]
    assert result.allow_success is False
    assert result.delivery_status == "partial"
    assert result.work_item is None
    assert len(reported_notes) == 2
    assert all(item["required"] is False for item in reported_notes)


@pytest.mark.asyncio
async def test_terminal_gate_revalidates_bound_visual_receipt_from_runtime_finalize(
    tmp_path: Path,
) -> None:
    run_id = "run-terminal-visual-receipt"
    await _started_contract(
        tmp_path,
        run_id=run_id,
        objective="生成并检查一份 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id=run_id,
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    deck = tmp_path / "deck.pptx"
    with zipfile.ZipFile(deck, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")
    preview = tmp_path / "renders" / "deck-preview.png"
    _write_png(preview)
    attestation_event = _presentation_attestation_event(
        tmp_path,
        run_id=run_id,
        attempt_id=contract.attempt_id,
        deck=deck,
        previews=(preview,),
    )

    initial = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="deliverable finalization in progress",
        final_response_id=f"pending:{run_id}",
        primary_path=deck,
        runtime_events=[attestation_event],
        enforce_effect_settlement=False,
    )
    assert initial.allow_success is True
    assert initial.verification_receipt is not None
    primary = _append_committed_finalize_evidence(
        tmp_path,
        run_id=run_id,
        attempt_id=contract.attempt_id,
        primary_path=deck,
    )
    event = _finalize_receipt_event(
        run_id=run_id,
        attempt_id=contract.attempt_id,
        primary=primary,
        verification_receipt=initial.verification_receipt,
    )

    terminal = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="PPTX 已生成、验证并附在对话中。",
        final_response_id="response-terminal-visual-receipt",
        runtime_events=[event],
    )

    assert terminal.allow_success is True
    assert terminal.delivery_status == "verified"
    identity = next(
        item
        for item in terminal.acceptance_results
        if item["id"] == "visual_evidence_identity"
    )
    assert identity["passed"] is True


@pytest.mark.asyncio
async def test_visual_attestation_rejects_receipt_changed_after_tool_event(
    tmp_path: Path,
) -> None:
    run_id = "run-reject-mutated-tool-receipt"
    await _started_contract(
        tmp_path,
        run_id=run_id,
        objective="生成并检查一份 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id=run_id,
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    deck = tmp_path / "deck.pptx"
    with zipfile.ZipFile(deck, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")
    preview = tmp_path / "renders" / "deck-preview.png"
    _write_png(preview)
    event = _presentation_attestation_event(
        tmp_path,
        run_id=run_id,
        attempt_id=contract.attempt_id,
        deck=deck,
        previews=(preview,),
    )
    attestation = event.payload["metadata"]["verification_attestation"]
    receipt_path = Path(attestation["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["quality"]["visual_critic_id"] = "forged-after-tool-return"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="PPTX 已生成、验证并附在对话中。",
        final_response_id="response-mutated-tool-receipt",
        primary_path=deck,
        runtime_events=[event],
        enforce_effect_settlement=False,
    )

    assert result.allow_success is False
    assert result.delivery_status == "partial"
    check = next(
        item
        for item in result.acceptance_results
        if item["id"] == "visual_verification_attestation"
    )
    assert check["outcome"] == "not_run"


@pytest.mark.asyncio
async def test_visual_attestation_rejects_prior_attempt_with_same_path_and_bytes(
    tmp_path: Path,
) -> None:
    run_id = "run-reject-prior-attempt-attestation"
    await _started_contract(
        tmp_path,
        run_id=run_id,
        objective="生成并检查一份 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id=run_id,
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    deck = tmp_path / "deck.pptx"
    with zipfile.ZipFile(deck, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")
    preview = tmp_path / "renders" / "deck-preview.png"
    _write_png(preview)
    event = _presentation_attestation_event(
        tmp_path,
        run_id=run_id,
        attempt_id=f"prior-{contract.attempt_id}",
        deck=deck,
        previews=(preview,),
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="PPTX 已生成、验证并附在对话中。",
        final_response_id="response-prior-attempt-attestation",
        primary_path=deck,
        runtime_events=[event],
        enforce_effect_settlement=False,
    )

    assert result.allow_success is False
    assert result.delivery_status == "partial"
    check = next(
        item
        for item in result.acceptance_results
        if item["id"] == "visual_verification_attestation"
    )
    assert check["outcome"] == "not_run"


@pytest.mark.parametrize(
    "tamper",
    [
        "event_source",
        "failed_event",
        "cross_run",
        "cross_attempt",
        "missing_canonical",
        "wrongtype_canonical",
        "canonical_effect",
        "effect_tool_call",
        "primary_hash",
        "artifact_identity",
        "artifact_row",
        "changed_visual",
        "missing_visual",
        "outside_visual",
    ],
)
@pytest.mark.asyncio
async def test_terminal_gate_rejects_unbound_or_stale_visual_receipt(
    tmp_path: Path,
    tamper: str,
) -> None:
    run_id = f"run-reject-visual-receipt-{tamper}"
    await _started_contract(
        tmp_path,
        run_id=run_id,
        objective="生成并检查一份 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id=run_id,
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    deck = tmp_path / "deck.pptx"
    with zipfile.ZipFile(deck, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")
    preview = tmp_path / "renders" / "deck-preview.png"
    _write_png(preview)
    attestation_event = _presentation_attestation_event(
        tmp_path,
        run_id=run_id,
        attempt_id=contract.attempt_id,
        deck=deck,
        previews=(preview,),
    )
    initial = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="deliverable finalization in progress",
        final_response_id=f"pending:{run_id}",
        primary_path=deck,
        runtime_events=[attestation_event],
        enforce_effect_settlement=False,
    )
    assert initial.verification_receipt is not None
    primary = _append_committed_finalize_evidence(
        tmp_path,
        run_id=run_id,
        attempt_id=contract.attempt_id,
        primary_path=deck,
        effect_step_tool_call_id=(
            "other-call" if tamper == "effect_tool_call" else "call-finalize-visual"
        ),
    )
    receipt = copy.deepcopy(initial.verification_receipt)
    if tamper == "cross_attempt":
        receipt["attempt_id"] = "other-attempt"
    elif tamper == "primary_hash":
        receipt["primary_sha256"] = "sha256:wrong"
    elif tamper == "outside_visual":
        outside = tmp_path.parent / "outside-preview.png"
        _write_png(outside, color=(200, 30, 30))
        receipt["visual_evidence"][0]["relative_path"] = "../outside-preview.png"
    event = _finalize_receipt_event(
        run_id=run_id,
        attempt_id=contract.attempt_id,
        primary=primary,
        verification_receipt=receipt,
    )
    if tamper == "event_source":
        event.source = "assistant"
    elif tamper == "failed_event":
        event.status = "failed"
        event.payload["ok"] = False
    elif tamper == "cross_run":
        event.run_id = "other-run"
        event.turn_id = "other-run"
    elif tamper == "missing_canonical":
        event.payload.pop("canonical_tool")
    elif tamper == "wrongtype_canonical":
        event.payload["canonical_tool"] = "not-a-canonical-envelope"
    elif tamper == "canonical_effect":
        event.payload["canonical_tool"]["effect_id"] = "other-effect"
    elif tamper == "artifact_identity":
        event.payload["artifact_effects"][0]["artifact_id"] = "other-artifact"
    elif tamper == "artifact_row":
        event.payload["artifact_effects"][0]["sha256"] = "sha256:wrong"
    elif tamper == "changed_visual":
        _write_png(preview, color=(200, 30, 30))
    elif tamper == "missing_visual":
        preview.unlink()

    terminal = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="PPTX 已生成、验证并附在对话中。",
        final_response_id=f"response-reject-{tamper}",
        runtime_events=[event],
    )

    assert terminal.allow_success is False
    assert terminal.delivery_status == "partial"
    assert not terminal.evidence.works_item_ids


@pytest.mark.asyncio
async def test_legacy_passed_acceptance_note_stays_informational(
    tmp_path: Path,
) -> None:
    """A success sentence must never be promoted into a gate failure."""

    await _started_contract(
        tmp_path,
        run_id="run-pptx-legacy-acceptance-note",
        objective="生成一份 12 页 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id="run-pptx-legacy-acceptance-note",
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=False,
    )
    deck = tmp_path / "training-deck.pptx"
    with zipfile.ZipFile(deck, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")

    success_detail = (
        "12-slide PowerPoint generated successfully by python-pptx script"
    )
    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-pptx-legacy-acceptance-note",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="演示文稿已经完成。",
        final_response_id="response-pptx-legacy-acceptance-note",
        primary_path=deck,
        acceptance_evidence=[
            {
                "check": "File created",
                "status": "passed",
                "detail": success_detail,
            },
        ],
    )

    reported_note = next(
        item
        for item in result.acceptance_results
        if item["id"] == "reported_acceptance:1"
    )
    assert result.allow_success is True
    assert result.failure_summary is None
    assert reported_note["required"] is False
    assert reported_note["passed"] is True
    assert success_detail in str(reported_note["detail"])


@pytest.mark.asyncio
async def test_delivered_presentation_survives_failed_finalize_evidence(
    tmp_path: Path,
) -> None:
    """A sent file remains the terminal artifact even when QA is incomplete."""

    run_id = "run-delivered-pptx-after-failed-finalize"
    await _started_contract(
        tmp_path,
        run_id=run_id,
        objective="生成一份汇报 PPTX",
    )
    await bind_deliverable_contract(
        state_workspace_dir=tmp_path,
        run_id=run_id,
        kind="presentation",
        accepted_extensions=[".pptx"],
        visual_verification_required=True,
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    deck = (
        tmp_path
        / "presentation-jobs"
        / "job-1"
        / "candidates"
        / "round-1"
        / "presentation.pptx"
    )
    deck.parent.mkdir(parents=True)
    with zipfile.ZipFile(deck, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")
    now = datetime.now(timezone.utc)
    RuntimeArtifactEffectJournal(tmp_path).append(
        RuntimeArtifactEffectRecord(
            run_id=run_id,
            attempt_id=contract.attempt_id,
            source_effect_id="effect-send-file",
            tool_call_id="call-send-file",
            tool_name="send_file_to_user",
            artifact_effect_id="artifact-effect:delivered-presentation",
            artifact_id="artifact:delivered-presentation",
            artifact_role="delivery",
            status="delivered",
            revision=1,
            relative_path=deck.relative_to(tmp_path).as_posix(),
            delivery_idempotency_key="artifact-delivery:delivered-presentation",
            created_at=now,
            settled_at=now,
            size_bytes=deck.stat().st_size,
            sha256="sha256:" + hashlib.sha256(deck.read_bytes()).hexdigest(),
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        )
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id="chat-1",
        response_status="completed",
        assistant_text="PPTX 已生成并发送。",
        final_response_id="response-delivered-pptx",
    )

    assert result.primary_path == str(deck)
    assert result.delivery_status == "partial"
    assert result.allow_success is False
    assert any(
        row["id"] == "visual_verification_attestation"
        and row["outcome"] == "not_run"
        for row in result.acceptance_results
    )
    assert "no successful finalize_deliverable evidence" not in str(
        result.failure_summary
    )


@pytest.mark.asyncio
async def test_collaboration_cannot_verify_without_a_closed_hosted_room(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-collaboration-without-room",
        objective="用会议厅讨论后给出结论",
        work_method_id="collaboration",
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-collaboration-without-room",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="我模拟了两位专家的观点。",
        final_response_id="response-fake-room",
    )

    collaboration_check = next(
        item
        for item in result.acceptance_results
        if item["id"] == "hosted_collaboration_closed"
    )
    assert collaboration_check["passed"] is False
    assert result.allow_success is False


@pytest.mark.asyncio
async def test_closed_hosted_collaboration_is_valid_completion_evidence(
    tmp_path: Path,
) -> None:
    await _started_contract(
        tmp_path,
        run_id="run-collaboration-closed",
        objective="由两位成员讨论后给出结论",
        work_method_id="collaboration",
    )
    collaboration_event = SimpleNamespace(
        type="tool.call.completed",
        status="completed",
        payload={
            "name": "collaborate_with_agents",
            "ok": True,
            "collaboration": {
                "batch_id": "batch-closed",
                "phase": "closed",
                "status": "completed",
                "members": [
                    {"member_id": "one", "status": "completed"},
                    {"member_id": "two", "status": "completed"},
                ],
            },
        },
    )

    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id="run-collaboration-closed",
        chat_id="chat-1",
        response_status="completed",
        assistant_text="协作室已关闭，由沈行知统一交付。",
        final_response_id="response-closed-room",
        runtime_events=[collaboration_event],
    )

    assert result.allow_success is True


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['valid', 'valid_docx', 'valid_pdf', 'other_agent', 'tampered', 'wrong_digest', 'forged_tool', 'missing_receipt', 'wrong_path', 'raw_path', 'visual_required'])
async def test_research_delivery_resolves_only_verified_current_agent_export(tmp_path, case):
    from lantai.config.context import WorkspaceContext, current_workspace_context
    from lantai.research.projects import ResearchService
    from lantai.research.exports import export_report, export_file

    agent_home, topic, state = (tmp_path / name for name in ('agent', 'topic', 'state'))
    topic.mkdir()
    service = ResearchService(agent_home)
    project = service.create_project('Q', [], idempotency_key='p')
    report = service.save_report(project['project_id'], 1, 'Draft research with disclosed limitations.', [], idempotency_key='r')
    format = {'valid_docx': 'docx', 'valid_pdf': 'pdf'}.get(case, 'markdown')
    receipt = export_report(service, project['project_id'], 1, report['id'], format, 'apa', 'export')
    artifact = export_file(service, receipt['id'])
    run_id = 'run-research-managed'
    await _started_contract(state, run_id=run_id, objective='Export the research draft.')
    if case == 'visual_required':
        await bind_deliverable_contract(state_workspace_dir=state, run_id=run_id,
            kind='document', accepted_extensions=['.md'], visual_verification_required=True)
    contract = await RuntimeRunStore(state).load_contract(run_id)
    now = datetime.now(timezone.utc)
    record = RuntimeArtifactEffectRecord(
        run_id=run_id, attempt_id=contract.attempt_id, source_effect_id='effect-export',
        tool_call_id='call-export', tool_name='research_export',
        artifact_effect_id='artifact-effect:research', artifact_id='artifact:research',
        artifact_role='delivery', status='delivered', revision=1,
        relative_path=artifact.relative_to(agent_home).as_posix(),
        research_export_id=receipt['id'], delivery_idempotency_key='delivery:research',
        created_at=now, settled_at=now, size_bytes=artifact.stat().st_size,
        sha256='sha256:' + hashlib.sha256(artifact.read_bytes()).hexdigest(), media_type='text/markdown')
    if case == 'wrong_digest':
        record = record.model_copy(update={'sha256': 'sha256:' + '0' * 64})
    if case == 'missing_receipt':
        record = record.model_copy(update={'research_export_id': 'missing-export'})
    if case == 'wrong_path':
        record = record.model_copy(update={'relative_path': '.lantai/research/exports/forged.md'})
    if case == 'forged_tool':
        record = record.model_copy(update={'tool_name': 'plugin_export'})
    if case == 'tampered':
        artifact.write_text('Untrusted replacement.', encoding='utf-8')
    if case != 'raw_path':
        RuntimeArtifactEffectJournal(state).append(record)
    active_home = tmp_path / 'other-agent' if case == 'other_agent' else agent_home
    token = current_workspace_context.set(WorkspaceContext(active_home, state, topic))
    try:
        result = await finalize_deliverable(
            state_workspace_dir=state, effective_workspace_dir=topic, works_workspace_dir=active_home,
            run_id=run_id, chat_id='chat-1', response_status='completed',
            assistant_text='The research draft was exported; review limitations remain.',
            final_response_id='response-research', durable_artifact_required=True,
            **({'primary_path': artifact} if case == 'raw_path' else {}))
    finally:
        current_workspace_context.reset(token)
    if case in {'valid', 'valid_docx', 'valid_pdf'}:
        assert result.primary_path == str(artifact)
        assert result.delivery_status == 'verified'
        assert result.allow_success
        assert result.work_item is not None
        assert list_works(active_home)
    elif case == 'visual_required':
        assert result.primary_path == str(artifact)
        assert result.delivery_status == 'partial'
        assert not result.allow_success
        assert any(row['id'] == 'visual_verification_attestation' and not row['passed'] for row in result.acceptance_results)
    else:
        assert not result.allow_success
        if case != 'wrong_digest':
            assert result.primary_path is None
            assert result.candidate_work_item is None
    assert not (topic / '.lantai/research').exists()
    assert not (state / '.lantai/research').exists()
    if case == 'other_agent':
        assert not (active_home / '.lantai/research').exists()
