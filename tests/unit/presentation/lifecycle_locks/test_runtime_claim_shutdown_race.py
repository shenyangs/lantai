# -*- coding: utf-8 -*-
"""Actual public redo/shutdown races, with bounded offline preflight/model
work.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager

from lantai.presentation import intelligence as intelligence_module
from lantai.presentation.models import JobStatus
from lantai.presentation.quality import file_sha256

from . import test_whole_job_lifecycle as base
from .test_completed_edit_stop import completed_fixture
from .test_real_process_settlement import record_evidence, wait_until


def assert_original(runtime, original, output, initial, versions):
    final = runtime.service.store.get(original.job_id)
    assert final.status == JobStatus.COMPLETED
    assert final.active_lease_id is None and final.active_operation is None
    assert not final.resource_settlement_pending
    assert final.deadline_at == original.deadline_at
    assert final.cost_used_units == original.cost_used_units
    assert file_sha256(output) == initial.pptx_sha256
    assert versions.current().version_id == initial.version_id
    return final


def test_shutdown_retires_registered_edit_before_writer_claim(
    tmp_path,
    monkeypatch,
):
    runtime, original, output, initial, versions = completed_fixture(
        tmp_path,
        monkeypatch,
    )
    preflight, release_preflight, returned = (
        threading.Event() for _ in range(3)
    )
    actual_get = runtime.get
    dispatches = []

    def paused_get(*args, **kwargs):
        record = actual_get(*args, **kwargs)
        preflight.set()
        assert release_preflight.wait(
            4,
        ), "synthetic preflight barrier timed out"
        return record

    async def offline_model(**kwargs):
        del kwargs
        dispatches.append("synthetic-model")
        raise RuntimeError("synthetic model must not be admitted")

    monkeypatch.setattr(runtime, "get", paused_get)
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
            "合成准入屏障",
            initial.pptx_sha256,
        ),
    )
    shutdown_errors = []

    def shutdown():
        try:
            runtime.shutdown()
        except BaseException as exc:
            shutdown_errors.append(exc)
        finally:
            returned.set()

    closer = threading.Thread(target=shutdown, daemon=True)
    try:
        assert preflight.wait(3), worker.errors
        assert (
            runtime.service.store.get(original.job_id).active_lease_id is None
        )
        closer.start()
        assert wait_until(
            # White-box lock exercises the actual private boundary.
            # pylint: disable-next=protected-access
            lambda: runtime._closed,
            2,
        ), "shutdown did not close admission"
        record_evidence(
            "registered-edit-before-writer-shutdown",
            {
                "shutdown_returned_before_release": returned.is_set(),
                "writer_before_release": runtime.service.store.get(
                    original.job_id,
                ).active_lease_id,
                "model_dispatches_before_release": len(dispatches),
            },
        )
        assert (
            not returned.is_set()
        ), "registered preflight owner was not drained"
        release_preflight.set()
        worker.settle()
        assert returned.wait(
            3,
        ), "shutdown failed to drain rejected preflight owner"
        assert not shutdown_errors, shutdown_errors
        assert (
            worker.errors
        ), "pre-shutdown admission claimed a fresh writer afterward"
        assert not dispatches
        final = assert_original(runtime, original, output, initial, versions)
        assert len(final.operation_history) == len(original.operation_history)
    finally:
        release_preflight.set()
        worker.settle()
        if closer.ident is not None:
            closer.join(4)
            assert not closer.is_alive()
        runtime.shutdown()


def test_shutdown_waits_writer_enter_and_retires_its_actual_claim(
    tmp_path,
    monkeypatch,
):
    runtime, original, output, initial, versions = completed_fixture(
        tmp_path,
        monkeypatch,
    )
    (
        about_to_claim,
        release_claim,
        shutdown_started,
        returned,
        safety_release,
    ) = (threading.Event() for _ in range(5))
    actual_writer = runtime.service.store.writer_lease

    @contextmanager
    def paused_writer(*args, **kwargs):
        about_to_claim.set()
        assert release_claim.wait(
            4,
        ), "synthetic writer-entry barrier timed out"
        with actual_writer(*args, **kwargs) as lease:
            yield lease

    async def offline_model(**kwargs):
        del kwargs
        while not safety_release.is_set():
            await asyncio.sleep(0.01)
        raise RuntimeError("synthetic cleanup safety release")

    monkeypatch.setattr(runtime.service.store, "writer_lease", paused_writer)
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
            "合成 writer 准入屏障",
            initial.pptx_sha256,
        ),
    )
    shutdown_errors = []

    def shutdown():
        shutdown_started.set()
        try:
            runtime.shutdown()
        except BaseException as exc:
            shutdown_errors.append(exc)
        finally:
            returned.set()

    closer = threading.Thread(target=shutdown, daemon=True)
    try:
        assert about_to_claim.wait(3), worker.errors
        closer.start()
        assert shutdown_started.wait(2)
        record_evidence(
            "writer-enter-shutdown-serialization",
            {
                # White-box lock exercises the actual private boundary.
                # pylint: disable-next=protected-access
                "closed_during_writer_enter": runtime._closed,
                "shutdown_returned_during_writer_enter": returned.is_set(),
                "writer_during_barrier": runtime.service.store.get(
                    original.job_id,
                ).active_lease_id,
            },
        )
        assert (
            # White-box lock exercises the actual private boundary.
            # pylint: disable-next=protected-access
            not runtime._closed
        ), "shutdown passed a writer admission still in flight"
        assert not returned.is_set()
        release_claim.set()
        assert returned.wait(
            4,
        ), "newly admitted real writer was missed by shutdown"
        worker.settle()
        assert not shutdown_errors, shutdown_errors
        assert worker.errors
        final = assert_original(runtime, original, output, initial, versions)
        assert (
            len(final.operation_history) == len(original.operation_history) + 1
        )
        assert final.operation_history[-1].status == JobStatus.CANCELLED
    finally:
        release_claim.set()
        safety_release.set()
        worker.settle()
        if closer.ident is not None:
            closer.join(4)
            assert not closer.is_alive()
        runtime.shutdown()
