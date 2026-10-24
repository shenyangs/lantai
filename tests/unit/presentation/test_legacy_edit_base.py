"""Public edits may repair owned legacy evidence, never publish it as passed."""

from __future__ import annotations

from pathlib import Path

import pytest

from lantai.presentation.models import (
    CompositionEngine, DeckIR, DeckSlide, EvidenceState, InputMaterial,
    JobStage, JobStatus, MoveResizeAction, ObjectFrame, PlanningMode,
    PresentationJobRequest, PresentationObjectEditPlan, QualityIssue, QualityReport,
    ShapeObject, TextObject, VisualCriticResult, VisualIssue,
)
from lantai.presentation.jobs import PresentationJobInterruptedError
from lantai.presentation.quality import file_sha256, inspect_pptx, is_verified_delivery
from lantai.presentation.render import slide_image_paths
from lantai.presentation.rendered_geometry import GLYPH_DIVIDER_RULESET
from lantai.presentation.runtime import PresentationJobRuntime
from lantai.presentation.versioning import PresentationVersionStore, StalePresentationVersionError


class _MoveDividerPlanner:
    """Fixture at the external model boundary; production QA stays real."""

    calls = 0
    divider_x = 8.0

    def plan(self, deck, slide_id, instruction, *, deck_plan=None):
        self.calls += 1
        divider = next(obj for obj in deck.slides[0].objects if obj.id == "divider")
        return PresentationObjectEditPlan(
            slide_id=slide_id,
            actions=[MoveResizeAction(
                object_id="divider", frame=divider.frame.model_copy(update={"x": self.divider_x}),
            )],
        )


class _PassingCritic:
    def review(self, slides):
        return VisualCriticResult(critic_id="synthetic-legacy-edit-critic/1", issues=[])


def _legacy_runtime(tmp_path: Path, *, collision_outside_scope=False):
    planner = _MoveDividerPlanner()
    runtime = PresentationJobRuntime(
        tmp_path / "jobs", object_edit_planner=planner, visual_critic=_PassingCritic(),
    )
    deck = DeckIR(
        id="synthetic-legacy-deck", title="合成旧版本修复验证",
        required_facts=["MMMMMMMMMMMMMMMMMMMMMMMMMMMM", "保留未指定页"],
        slides=[
            DeckSlide(id="repair-page", layout_id="freeform", objects=[
                TextObject(id="title", role="title", text="合成旧版本修复验证", font_size=32,
                           frame=ObjectFrame(x=0.6, y=0.4, w=12, h=0.8)),
                TextObject(id="body", role="body", text="MMMMMMMMMMMMMMMMMMMMMMMMMMMM",
                           color="1565C0", font_size=24,
                           frame=ObjectFrame(x=0.78, y=1.52, w=4.95, h=1.6)),
                ShapeObject(id="divider", shape_type="rect", fill_color="D4DDE1",
                            line_color="D4DDE1", line_width=0,
                            frame=ObjectFrame(x=3.3, y=1.55, w=0.02, h=4.95)),
            ]),
            DeckSlide(id="keep-page", layout_id="freeform", objects=[
                TextObject(id="keep", role="title", text="保留未指定页", font_size=32,
                           frame=ObjectFrame(x=0.6, y=0.4, w=12, h=0.8)),
            ]),
        ],
    )
    if collision_outside_scope:
        deck.slides[0].id, deck.slides[1].id = "keep-page", "repair-page"
    record = runtime.service.create_job(PresentationJobRequest(
        conversation_id="synthetic-legacy-conversation",
        user_request="只移动分隔线，保留全部文字及未指定页",
        slide_count=2, composition_engine=CompositionEngine.NATIVE_PPTX,
        planning_mode=PlanningMode.DETERMINISTIC_FIXTURE, vision_critique_required=True,
        materials=[InputMaterial(id="synthetic-source", name="synthetic.txt",
                                 media_type="text/plain",
                                 content="合成旧版本修复验证\nMMMMMMMMMMMMMMMMMMMMMMMMMMMM\n保留未指定页")],
    ))
    job_dir = runtime.service.store.job_dir(record.job_id)
    runtime.service.store.write_checkpoint(record.job_id, "deck-ir.json", deck.model_dump_json())
    output = job_dir / "output/presentation.pptx"
    runtime.service.compiler_for(CompositionEngine.NATIVE_PPTX).compile(deck, output)
    current_report, images = runtime.service.verify_artifact(
        deck, output, job_dir / "renders/legacy-baseline", render_set_id="legacy-baseline",
        vision_critique_required=True,
    )
    assert {issue.code for issue in current_report.issues if issue.severity == "error"} == {
        "layout.glyph_divider_collision",
    }
    # Reconstruct the historical pre-glyph report from the real structural
    # inspector and real images. This fixture is NOT a current-policy pass.
    legacy = inspect_pptx(deck, output, rendered_slide_count=len(images)).model_copy(update={
        "renderer_id": current_report.renderer_id,
        "renderer_version": current_report.renderer_version,
        "font_manifest": current_report.font_manifest,
        "rendered_slides": current_report.rendered_slides,
        "render_set_id": "legacy-baseline",
        "vision_critique_required": True,
        "visual_verified": True, "visual_evidence": EvidenceState.PASSED,
        "visual_critic_id": current_report.visual_critic_id,
        "visual_review_evidence": current_report.visual_review_evidence,
    })
    assert legacy.passed and legacy.rendered_glyph_check_version is None
    assert not is_verified_delivery(deck, legacy, output, images)
    quality = job_dir / "quality/quality-report.json"
    quality.parent.mkdir(parents=True, exist_ok=True)
    quality.write_text(legacy.model_dump_json(), encoding="utf-8")
    versions = PresentationVersionStore(job_dir)
    version = versions.bootstrap(
        output, deck, writer=CompositionEngine.NATIVE_PPTX.value,
        quality_report_path=quality, render_dir=job_dir / "renders/legacy-baseline",
    )
    runtime.service.store.append_event(
        record.job_id, stage=JobStage.COMPLETED, status=JobStatus.COMPLETED,
        message="synthetic historical baseline, not current-policy delivery",
    )
    return runtime, record.job_id, deck, output, versions, version, planner


def _core_paths(runtime, job_id, output, versions, base):
    job_dir = runtime.service.store.job_dir(job_id)
    return [output, job_dir / "checkpoints/deck-ir.json", job_dir / "quality/quality-report.json",
            versions.current_path, versions.history_path, job_dir / base.quality_snapshot]


def _assert_settled(runtime, job_id):
    record = runtime.service.store.get(job_id)
    assert record.active_operation is None and record.active_lease_id is None
    assert not record.resource_settlement_pending
    return record


def test_public_edit_revalidates_legacy_geometry_and_requires_current_candidate(tmp_path):
    runtime, job_id, deck, output, versions, base, planner = _legacy_runtime(tmp_path)
    historical_quality_path = runtime.service.store.job_dir(job_id) / base.quality_snapshot
    historical_quality = historical_quality_path.read_bytes()
    old_hash = file_sha256(output)
    try:
        updated = runtime.redo_slide(
            job_id, "synthetic-legacy-conversation", "repair-page",
            "移动分隔线，保留全部文字", old_hash,
        )
        assert updated.status == JobStatus.COMPLETED
        assert planner.calls == 1
        assert file_sha256(output) != old_hash
        job_dir = runtime.service.store.job_dir(job_id)
        assessments = list((job_dir / "checkpoints").glob("edit-base-quality-report-*.json"))
        assert len(assessments) == 1
        assessment = QualityReport.model_validate_json(assessments[0].read_bytes())
        assert not assessment.passed and assessment.delivery_status == "failed"
        assert assessment.output_sha256 == old_hash
        assert assessment.rendered_glyph_check_version == GLYPH_DIVIDER_RULESET
        assert historical_quality_path.read_bytes() == historical_quality
        assert versions.current().parent_version_id == base.version_id
        changed = DeckIR.model_validate_json((job_dir / "checkpoints/deck-ir.json").read_bytes())
        report = QualityReport.model_validate_json((job_dir / "quality/quality-report.json").read_bytes())
        assert is_verified_delivery(changed, report, output, slide_image_paths(
            job_dir / "renders" / report.render_set_id,
        ))
        assert report.rendered_glyph_check_version == GLYPH_DIVIDER_RULESET
        assert report.visual_verified and report.vision_critique_required
        assert changed.required_facts == deck.required_facts
        _assert_settled(runtime, job_id)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("bad_state", [
    {"passed": False}, {"delivery_status": "failed"}, {"render_verified": False},
    {"content_evidence": EvidenceState.FAILED}, {"structure_evidence": EvidenceState.FAILED},
    {"render_evidence": EvidenceState.FAILED}, {"editability_evidence": EvidenceState.FAILED},
    {"provenance_evidence": EvidenceState.FAILED}, {"visual_verified": False},
    {"visual_evidence": EvidenceState.FAILED}, {"blocking_gaps": ["content evidence"]},
    {"rendered_glyph_check_version": "native-foreground-glyph-dividers/v0"},
    {"rendered_glyph_check_version": "future-unknown"},
    {"issues": [QualityIssue(code="content.test_failure", severity="error", message="fixture failure")]},
])
def test_public_legacy_edit_does_not_upgrade_failed_or_unknown_historical_qa(tmp_path, bad_state):
    runtime, job_id, _deck, output, versions, base, planner = _legacy_runtime(tmp_path)
    paths = _core_paths(runtime, job_id, output, versions, base)
    report = QualityReport.model_validate_json(paths[2].read_bytes()).model_copy(update=bad_state)
    # A genuine historical failure, not just a changed current copy, must
    # remain a failure. No fresh model or render is used to hide it.
    for path in (paths[2], paths[-1]):
        path.write_text(report.model_dump_json(), encoding="utf-8")
    before = {path: path.read_bytes() for path in paths}
    try:
        with pytest.raises(ValueError, match="完整的渲染证据"):
            runtime.redo_slide(job_id, "synthetic-legacy-conversation", "repair-page",
                               "移动分隔线", file_sha256(output))
        assert planner.calls == 0
        assert {path: path.read_bytes() for path in paths} == before
        assert not list((runtime.service.store.job_dir(job_id) / "checkpoints").glob(
            "edit-base-quality-report-*.json",
        ))
        settled = _assert_settled(runtime, job_id)
        assert settled.operation_history[-1].cost_used_units == 0
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("corruption", ["preview", "report_copy", "renderer", "font_manifest"])
def test_public_legacy_edit_preserves_hash_and_snapshot_binding(tmp_path, corruption):
    runtime, job_id, _deck, output, versions, base, planner = _legacy_runtime(tmp_path)
    paths = _core_paths(runtime, job_id, output, versions, base)
    job_dir = runtime.service.store.job_dir(job_id)
    if corruption == "preview":
        image = slide_image_paths(job_dir / "renders/legacy-baseline")[0]
        image.write_bytes(b"corrupted synthetic preview")
    else:
        report = QualityReport.model_validate_json(paths[2].read_bytes())
        updates = {"renderer_id": None} if corruption == "renderer" else (
            {"font_manifest": []} if corruption == "font_manifest" else {"repair_rounds": 1}
        )
        paths[2].write_text(report.model_copy(update=updates).model_dump_json(), encoding="utf-8")
    before = {path: path.read_bytes() for path in paths}
    try:
        with pytest.raises(ValueError, match="完整的渲染证据"):
            runtime.redo_slide(job_id, "synthetic-legacy-conversation", "repair-page",
                               "移动分隔线", file_sha256(output))
        assert planner.calls == 0
        assert {path: path.read_bytes() for path in paths} == before
        _assert_settled(runtime, job_id)
    finally:
        runtime.shutdown()


def test_public_legacy_edit_rejects_collision_outside_explicit_scope(tmp_path):
    runtime, job_id, _deck, output, versions, base, planner = _legacy_runtime(
        tmp_path, collision_outside_scope=True,
    )
    paths = _core_paths(runtime, job_id, output, versions, base)
    before = {path: path.read_bytes() for path in paths}
    try:
        with pytest.raises(ValueError, match="完整的渲染证据"):
            runtime.redo_slide(job_id, "synthetic-legacy-conversation", "repair-page",
                               "移动分隔线", file_sha256(output))
        assert planner.calls == 0
        assert {path: path.read_bytes() for path in paths} == before
        reports = list((runtime.service.store.job_dir(job_id) / "checkpoints").glob(
            "edit-base-quality-report-*.json",
        ))
        assert len(reports) == 1
        assessment = QualityReport.model_validate_json(reports[0].read_bytes())
        assert not assessment.passed
        assert {issue.slide_id for issue in assessment.issues if issue.severity == "error"} == {"keep-page"}
        _assert_settled(runtime, job_id)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("failure", ["still_colliding", "visual_failure"])
def test_public_legacy_repair_candidate_must_pass_current_full_qa(tmp_path, failure):
    runtime, job_id, _deck, output, versions, base, planner = _legacy_runtime(tmp_path)
    paths = _core_paths(runtime, job_id, output, versions, base)
    before = {path: path.read_bytes() for path in paths}
    if failure == "still_colliding":
        planner.divider_x = 3.31
    else:
        class FailingCritic:
            def review(self, slides):
                return VisualCriticResult(critic_id="synthetic-failing-critic/1", issues=[
                    VisualIssue(slide_id="repair-page", issue_type="hierarchy", severity="error",
                                description="synthetic visual failure",
                                proposed_action="repair the synthetic page hierarchy"),
                ])
        runtime.service.visual_critic = FailingCritic()
    try:
        with pytest.raises(ValueError, match="候选修改未通过"):
            runtime.redo_slide(job_id, "synthetic-legacy-conversation", "repair-page",
                               "移动分隔线", file_sha256(output))
        assert planner.calls == 1
        assert {path: path.read_bytes() for path in paths} == before
        reports = list((runtime.service.store.job_dir(job_id) / "checkpoints").glob("edit-quality-report-*.json"))
        assert len(reports) == 1
        assessment = QualityReport.model_validate_json(reports[0].read_bytes())
        assert not assessment.passed and assessment.delivery_status == "failed"
        assert assessment.rendered_glyph_check_version == GLYPH_DIVIDER_RULESET
        expected_code = "layout.glyph_divider_collision" if failure == "still_colliding" else "vision.hierarchy"
        assert expected_code in {issue.code for issue in assessment.issues if issue.severity == "error"}
        _assert_settled(runtime, job_id)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("interruption", ["cancel", "external_pptx", "external_ledger", "external_report"])
def test_real_legacy_revalidation_keeps_writer_cancellation_and_external_authority(
    tmp_path, monkeypatch, interruption,
):
    runtime, job_id, _deck, output, versions, base, planner = _legacy_runtime(tmp_path)
    paths = _core_paths(runtime, job_id, output, versions, base)
    before = {path: path.read_bytes() for path in paths}
    from lantai.presentation.render import render_pptx as real_render

    def interrupted_renderer(source, output_dir, *, timeout=120):
        result = real_render(source, output_dir, timeout=timeout)
        if interruption == "cancel":
            runtime.cancel(job_id, "synthetic-legacy-conversation")
        elif interruption == "external_pptx":
            output.write_bytes(b"synthetic newer external PPTX")
        elif interruption == "external_ledger":
            newer = versions.current().model_copy(update={"version_id": "version-newer-external"})
            versions.current_path.write_text(newer.model_dump_json(), encoding="utf-8")
        else:
            paths[2].write_bytes(b"synthetic newer external report")
        return result

    monkeypatch.setattr("lantai.presentation.service.render_pptx", interrupted_renderer)
    try:
        expected_error = StalePresentationVersionError if interruption == "external_pptx" else (
            PresentationJobInterruptedError if interruption == "cancel" else ValueError
        )
        with pytest.raises(expected_error):
            runtime.redo_slide(job_id, "synthetic-legacy-conversation", "repair-page",
                               "移动分隔线", file_sha256(output))
        assert planner.calls == 0
        untouched = [path for path in paths if path != (
            output if interruption == "external_pptx" else versions.current_path
            if interruption == "external_ledger" else paths[2]
            if interruption == "external_report" else None
        )]
        assert {path: path.read_bytes() for path in untouched} == {path: before[path] for path in untouched}
        if interruption == "external_pptx":
            assert output.read_bytes() == b"synthetic newer external PPTX"
        elif interruption == "external_report":
            assert paths[2].read_bytes() == b"synthetic newer external report"
        elif interruption == "external_ledger":
            assert versions.current().version_id == "version-newer-external"
        _assert_settled(runtime, job_id)
    finally:
        runtime.shutdown()
