# -*- coding: utf-8 -*-
"""Real completed-deck edit admission and Stop, with an offline model
coroutine.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image
from pptx import Presentation

from lantai.presentation import intelligence as intelligence_module
from lantai.presentation import service as service_module
from lantai.presentation.intelligence import (
    UtilityPresentationObjectEditPlanner,
)
from lantai.presentation.jobs import (
    cancel_active_job_for_runtime_run,
    cancel_active_jobs_for_runtime_run,
)
from lantai.presentation.models import (
    DeckIR,
    DeckSlide,
    JobStage,
    JobStatus,
    TextObject,
)
from lantai.presentation.quality import file_sha256, is_verified_delivery
from lantai.presentation.render import RenderResult
from lantai.presentation.runtime import PresentationJobRuntime
from lantai.presentation.versioning import PresentationVersionStore

from . import test_whole_job_lifecycle as base
from .test_real_process_settlement import record_evidence


def completed_fixture(tmp_path, monkeypatch):
    def render(source, output_dir, *, timeout=120):
        del timeout
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        pdf = destination / "presentation.pdf"
        pdf.write_bytes(b"synthetic-renderer-pdf")
        images = []
        for index in range(1, len(Presentation(str(source)).slides) + 1):
            path = destination / f"slide-{index}.png"
            image = Image.new("RGB", (640, 360), "white")
            image.paste("black", (0, 0, 280 + index, 360))
            image.save(path)
            images.append(path)
        return RenderResult(
            pdf_path=pdf,
            slide_images=tuple(images),
            renderer_id="synthetic-renderer",
            renderer_version="1.0",
        )

    monkeypatch.setattr(service_module, "render_pptx", render)
    runtime = PresentationJobRuntime(
        tmp_path / "presentation-jobs",
        object_edit_planner=UtilityPresentationObjectEditPlanner(
            "synthetic-agent",
        ),
    )
    store = runtime.service.store
    record = runtime.service.create_job(base.request())
    deck = DeckIR(
        id="synthetic-edit-deck",
        title="合成显式修改",
        slides=[
            DeckSlide(
                id="edit-first",
                layout_id="cover",
                objects=[
                    TextObject(
                        id="edit-first-title",
                        role="title",
                        slot="title",
                        text="原始合成标题",
                    ),
                ],
            ),
            DeckSlide(
                id="edit-second",
                layout_id="closing",
                objects=[
                    TextObject(
                        id="edit-second-title",
                        role="title",
                        slot="title",
                        text="保持原页内容",
                    ),
                ],
            ),
        ],
    )
    store.write_checkpoint(
        record.job_id,
        "deck-ir.json",
        deck.model_dump_json(indent=2),
    )
    output = store.job_dir(record.job_id) / "output" / "presentation.pptx"
    runtime.service.compiler.compile(deck, output)
    report, images = runtime.service.verify_artifact(
        deck,
        output,
        store.job_dir(record.job_id) / "renders" / "round-0",
        render_set_id="round-0",
    )
    assert is_verified_delivery(
        deck,
        report,
        output,
        images,
    ), "fixture lacks genuine delivery evidence"
    quality = store.job_dir(record.job_id) / "quality" / "quality-report.json"
    quality.parent.mkdir(parents=True, exist_ok=True)
    quality.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    versions = PresentationVersionStore(store.job_dir(record.job_id))
    initial = versions.bootstrap(
        output,
        deck,
        writer="legacy_python_pptx",
        quality_report_path=quality,
        render_dir=store.job_dir(record.job_id) / "renders" / "round-0",
    )
    store.append_event(
        record.job_id,
        stage=JobStage.COMPLETED,
        status=JobStatus.COMPLETED,
        message="synthetic verified complete",
        checkpoint=str(output),
    )
    state = store.get(record.job_id)
    state.deadline_at = state.created_at - timedelta(days=1)
    state.cost_used_units = state.cost_budget_units
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    store._write(
        state,
    )  # Temporary synthetic legacy record, never real user state.
    return runtime, store.get(record.job_id), output, initial, versions


@pytest.mark.parametrize(
    "entry",
    ["presentation", "native-exact", "native-run"],
)
# One fault scenario includes admission and settlement assertions.
# pylint: disable-next=too-many-statements
def test_stop_cancels_active_explicit_edit_of_completed_deck(
    tmp_path,
    monkeypatch,
    entry,
):
    runtime, original, output, initial, versions = completed_fixture(
        tmp_path,
        monkeypatch,
    )
    entered, release_work, cancelled, release_cleanup = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    async def offline_model(**kwargs):
        del kwargs
        entered.set()
        try:
            while not release_work.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.set()
            while not release_cleanup.is_set():
                await asyncio.sleep(0.01)
            raise
        # Only used by safety cleanup when the broken Stop never arrived.
        return None

    monkeypatch.setattr(
        intelligence_module,
        "run_structured_utility_task",
        offline_model,
    )
    worker = base.Worker(
        lambda: runtime.redo_slide(
            original.job_id,
            original.conversation_id,
            "edit-first",
            "把本页标题改为：显式修改后的合成标题",
            initial.pptx_sha256,
        ),
    )

    def stop():
        if entry == "presentation":
            return runtime.cancel(original.job_id, original.conversation_id)
        if entry == "native-exact":
            return cancel_active_job_for_runtime_run(
                tmp_path,
                runtime.runtime_run_id,
                original.job_id,
                conversation_id=original.conversation_id,
            )
        return cancel_active_jobs_for_runtime_run(
            tmp_path,
            runtime.runtime_run_id,
        )

    try:
        assert entered.wait(4), worker.errors
        store = runtime.service.store
        active = store.get(original.job_id)
        assert (
            active.status == JobStatus.COMPLETED
            and active.active_operation is not None
        )
        assert (
            active.active_lease_id
            and active.runtime_run_id == runtime.runtime_run_id
        )
        error, result = None, None
        try:
            result = stop()
        except Exception as exc:
            error = exc
        record_evidence(
            "completed-explicit-edit-stop",
            {
                "entry": entry,
                "status": active.status.value,
                "active_operation_id": active.active_operation.operation_id,
                "original_lease": active.active_lease_id,
                "stop_error": repr(error),
                "stop_result": str(result),
                "model_cancelled": cancelled.is_set(),
                "output_original": file_sha256(output) == initial.pptx_sha256,
            },
        )
        assert error is None, (
            "Stop rejected a live explicit edit on a completed root job: "
            + repr(error)
        )
        if entry == "native-exact":
            assert (
                result is True
            ), "Native exact-job Stop skipped a live completed-deck edit"
        elif entry == "native-run":
            assert (
                original.job_id in result
            ), "Native Run Stop skipped a live completed-deck edit"
        assert cancelled.wait(
            1.5,
        ), "Stop did not cancel the actual edit model coroutine"
        assert (
            not worker.finished.is_set()
        ), "writer settled before model cleanup acknowledgement"
        assert (
            store.get(original.job_id).active_lease_id
            == active.active_lease_id
        )
        # Repeated Stop must not abandon this cleanup or create another
        # generation.
        stop()
        assert (
            store.get(original.job_id).active_lease_id
            == active.active_lease_id
        )
        assert file_sha256(output) == initial.pptx_sha256
        release_cleanup.set()
        worker.settle()
        final = store.get(original.job_id)
        assert final.status == JobStatus.COMPLETED
        assert final.active_lease_id is None and final.active_operation is None
        assert final.operation_history[-1].status == JobStatus.CANCELLED
        assert final.deadline_at == original.deadline_at
        assert final.cost_used_units == original.cost_used_units
        assert (
            runtime.output_path(original.job_id, original.conversation_id)
            == output
        )
        assert versions.current().version_id == initial.version_id
        assert file_sha256(output) == initial.pptx_sha256
    finally:
        release_work.set()
        release_cleanup.set()
        worker.settle()
        runtime.shutdown()


def test_native_stop_does_not_cancel_completed_history_without_an_active_edit(
    tmp_path,
    monkeypatch,
):
    runtime, original, output, initial, versions = completed_fixture(
        tmp_path,
        monkeypatch,
    )
    try:
        assert not cancel_active_job_for_runtime_run(
            tmp_path,
            original.runtime_run_id,
            original.job_id,
            conversation_id=original.conversation_id,
        )
        assert (
            # Keep the exact empty-list result required by this behavior lock.
            # pylint: disable-next=use-implicit-booleaness-not-comparison
            cancel_active_jobs_for_runtime_run(
                tmp_path,
                original.runtime_run_id,
            )
            == []
        )
        final = runtime.service.store.get(original.job_id)
        assert (
            final.status == JobStatus.COMPLETED
            and final.active_operation is None
        )
        assert final.last_sequence == original.last_sequence
        assert (
            runtime.output_path(original.job_id, original.conversation_id)
            == output
        )
        assert file_sha256(output) == initial.pptx_sha256
        assert versions.current().version_id == initial.version_id
    finally:
        runtime.shutdown()
