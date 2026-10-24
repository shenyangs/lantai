# -*- coding: utf-8 -*-
"""Independent real-owner recovery/shutdown locks; source and synthetic data
only.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import psutil
import pytest

from lantai.presentation import jobs as jobs_module
from lantai.presentation.jobs import (
    PresentationJobInterruptedError,
    PresentationJobStore,
    PresentationWriterBusyError,
)
from lantai.presentation.models import JobStatus
from lantai.presentation.runtime import PresentationJobRuntime
from lantai.presentation.service import PresentationService

from . import test_whole_job_lifecycle as base
from .test_real_process_settlement import (
    RealProductionPath,
    record_evidence,
    wait_until,
)

ROOT = Path(__file__).resolve().parent


class RecoveryRuntime(PresentationJobRuntime):
    """Only the post-admission semantic stage is offline; all authority is
    real.
    """

    def _run_with_lease(self, job_id, writer_lease, force_compose=False):
        del force_compose
        self.recovered_jobs = getattr(self, "recovered_jobs", []) + [
            (
                job_id,
                writer_lease.deadline_at,
                self.service.store.get(job_id).cost_used_units,
            ),
        ]


def construct_recovery_runtime(jobs_root):
    runtime = object.__new__(RecoveryRuntime)
    error = None
    try:
        runtime.__init__(
            jobs_root,
            semantic_planner=base.FixturePlanner(),
            page_author=base.FixtureAuthor(),
        )
    except BaseException as exc:
        error = exc
    return runtime, error


class RealOwner:
    def __init__(self, tmp_path, mode="lease"):
        self.root = tmp_path / ("owner-" + mode)
        self.root.mkdir(parents=True)
        self.ready = self.root / "ready.json"
        # The explicit owner settles this process in cleanup.
        # pylint: disable-next=consider-using-with
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(ROOT / "lifecycle_owner_driver.py"),
                mode,
                str(self.root),
                str(self.ready),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        if not wait_until(
            lambda: self.ready.exists() or self.process.poll() is not None,
            7,
        ):
            self.process.kill()
            self.process.communicate(timeout=3)
            raise AssertionError("finite synthetic owner startup timed out")
        if not self.ready.exists():
            _out, err = self.process.communicate(timeout=2)
            raise AssertionError(
                "synthetic owner did not enter real writer: " + err[-1200:],
            )
        self.data = json.loads(self.ready.read_text())
        self.store = PresentationJobStore(Path(self.data["jobs_root"]))
        assert self.process.pid == self.data["owner_pid"]
        assert (
            self.store.get(self.data["job_id"]).active_lease_id
            == self.data["lease_id"]
        )

    def crash(self):
        # Only the exact synthetic Popen owner; not its group.
        self.process.kill()
        self.process.communicate(timeout=3)
        assert self.process.returncode == -signal.SIGKILL

    def tree_live(self):
        return any(
            psutil.pid_exists(self.data[key]["pid"])
            for key in ("parent", "child")
        )

    def observe(self, case, **extra):
        record = self.store.get(self.data["job_id"])
        record_evidence(
            case,
            {
                "owner_pid": self.process.pid,
                "owner_alive": self.process.poll() is None,
                "job_id": record.job_id,
                "status": record.status.value,
                "active_lease_id": record.active_lease_id,
                "original_lease_preserved": record.active_lease_id
                == self.data["lease_id"],
                # Keep the canonical API field or synthetic fixture bytes
                # unchanged.
                "resource_settlement_pending": record.resource_settlement_pending,  # noqa: E501
                "deadline": record.deadline_at.isoformat(),
                "cost": record.cost_used_units,
                **extra,
            },
        )

    def close(self):
        (self.root / "owner.release").write_text("synthetic", encoding="utf-8")
        if "state" in self.data:
            (Path(self.data["state"]) / "release").write_text(
                "synthetic",
                encoding="utf-8",
            )
            for key in ("child", "parent"):
                pid = self.data[key]["pid"]
                try:
                    process = psutil.Process(pid)
                    assert (
                        self.data["script"] in process.cmdline()
                    ), "synthetic PID identity changed"
                    process.send_signal(signal.SIGTERM)
                except psutil.NoSuchProcess:
                    pass
        if self.process.poll() is None:
            try:
                self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate(timeout=3)
        if "state" in self.data:
            if not wait_until(lambda: not self.tree_live(), 4):
                for key in ("child", "parent"):
                    try:
                        process = psutil.Process(self.data[key]["pid"])
                        assert self.data["script"] in process.cmdline()
                        process.kill()
                    except psutil.NoSuchProcess:
                        pass
            assert wait_until(
                lambda: not self.tree_live(),
                4,
            ), "synthetic orphan not reaped after safety cleanup"
        self.observe(
            "owner-safety-cleanup",
            owned_tree_alive=(
                self.tree_live() if "state" in self.data else False
            ),
        )


@pytest.mark.parametrize(
    "alive",
    [True, False],
    ids=["live-writer", "crashed-writer"],
)
def test_public_restart_does_not_fail_before_writer_admission(tmp_path, alive):
    owner = RealOwner(tmp_path)
    runtime = None
    try:
        if not alive:
            owner.crash()
        original = owner.store.get(owner.data["job_id"])
        runtime, error = construct_recovery_runtime(owner.store.root)
        owner.observe(
            "restart-before-writer-admission",
            constructor_error=(
                type(error).__name__ + ": " + str(error) if error else None
            ),
        )
        assert error is None, (
            "startup recovery attempted mutation without acquiring its"
            " writer: " + repr(error)
        )
        if alive:
            assert not getattr(runtime, "recovered_jobs", [])
            assert (
                owner.store.get(original.job_id).active_lease_id
                == owner.data["lease_id"]
            )
        else:
            assert wait_until(
                lambda: bool(getattr(runtime, "recovered_jobs", [])),
                3,
            )
            assert runtime.recovered_jobs[0] == (
                original.job_id,
                original.deadline_at,
                original.cost_used_units,
            )
            assert wait_until(
                lambda: owner.store.get(original.job_id).active_lease_id
                is None,
                2,
            )
    finally:
        owner.close()
        if runtime is not None and hasattr(runtime, "_executor"):
            runtime.shutdown()


# Keep the existing test selector unchanged.
def test_expired_crashed_writer_records_terminal_failure_under_recovery_authority(  # noqa: E501
    tmp_path,
    monkeypatch,
):
    runtime = RecoveryRuntime(
        tmp_path / "initially-empty",
        semantic_planner=base.FixturePlanner(),
        page_author=base.FixtureAuthor(),
    )
    owner = RealOwner(tmp_path)
    try:
        owner.crash()
        runtime.service = PresentationService(owner.store.root)
        original = owner.store.get(owner.data["job_id"])
        base.ControlledClock.current = original.deadline_at + timedelta(
            seconds=1,
        )
        monkeypatch.setattr(jobs_module, "datetime", base.ControlledClock)
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        runtime._run(original.job_id)
        record = owner.store.get(original.job_id)
        owner.observe(
            "expired-crashed-writer-after-run",
            semantic_dispatches=len(
                getattr(runtime, "recovered_jobs", []),
            ),
        )
        assert not getattr(
            runtime,
            "recovered_jobs",
            [],
        ), "expired operation dispatched semantic work"
        assert record.status == JobStatus.FAILED, (
            "expired dead writer stayed runnable without a durable terminal"
            " failure"
        )
        assert (
            record.active_lease_id is None
        ), "settled dead owner permanently retained its execution claim"
        assert record.deadline_at == original.deadline_at
        assert record.cost_used_units == original.cost_used_units
        assert record.events[-1].status == JobStatus.FAILED
    finally:
        owner.close()
        runtime.shutdown()


@pytest.mark.parametrize("stage", ["native", "libreoffice"])
def test_shutdown_returns_only_after_actual_owned_processes_settle(
    tmp_path,
    monkeypatch,
    stage,
):
    class Runtime(PresentationJobRuntime):
        def _run_with_lease(self, job_id, writer_lease, force_compose=False):
            del force_compose
            if stage == "native":
                self.service.compose(
                    job_id,
                    base.deck(),
                    writer_lease=writer_lease,
                )
            else:
                self.service.verify_artifact(
                    base.deck(),
                    path.input,
                    path.state.parent / "runtime-render-output",
                    visual_required=False,
                    job_id=job_id,
                )

    runtime = Runtime(
        tmp_path / "empty-runtime",
        semantic_planner=base.FixturePlanner(),
        page_author=base.FixtureAuthor(),
    )
    path = RealProductionPath(tmp_path, monkeypatch, stage)
    runtime.service = path.service
    returned, shutdown_errors = threading.Event(), []
    thread = None
    future = None
    try:
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        runtime._submit(path.record.job_id)
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        future = runtime._futures[path.record.job_id]
        future.add_done_callback(lambda _: path.finished.set())
        assert wait_until(lambda: path.has("parent.ready") or future.done(), 6)
        assert path.has(
            "parent.ready",
        ), "real runtime did not admit synthetic process"
        path.parent = json.loads((path.state / "parent.json").read_text())
        path.child = json.loads((path.state / "child.json").read_text())
        path.lease_id = path.active_owner()

        def stop():
            try:
                runtime.shutdown()
            except BaseException as error:
                shutdown_errors.append(error)
            finally:
                returned.set()

        thread = threading.Thread(target=stop, daemon=True)
        thread.start()
        assert returned.wait(
            3,
        ), "shutdown did not settle within bounded synthetic process cleanup"
        path.observe("runtime-shutdown-returned")
        assert not shutdown_errors, shutdown_errors
        assert future.done() and path.tree_reaped(), (
            "shutdown returned while its worker or actual process tree was"
            " still live"
        )
        assert (
            path.active_owner() is None
        ), "shutdown returned without durable owner settlement"
        assert not (
            path.service.store.job_dir(path.record.job_id)
            / "output/presentation.pptx"
        ).exists()
    finally:
        path.release()
        if future is not None and not future.done():
            state = path.service.store.get(path.record.job_id)
            if state.status not in {JobStatus.CANCELLED, JobStatus.COMPLETED}:
                path.cancel()
        path.close()
        if future is not None:
            future.result(timeout=5)
        if thread is not None:
            thread.join(5)
            assert not thread.is_alive()
        runtime.shutdown()


@pytest.mark.skipif(
    os.name != "posix",
    reason="real POSIX owner-crash boundary",
)
# Keep the existing test selector unchanged.
def test_dead_posix_owner_cannot_reclaim_writer_while_its_isolated_tree_is_live(  # noqa: E501
    tmp_path,
):
    owner = RealOwner(tmp_path, "native")
    try:
        original = owner.store.get(owner.data["job_id"])
        assert owner.data["parent"]["pid"] == owner.data["parent"]["pgid"]
        owner.crash()
        assert (
            owner.tree_live()
        ), "crash fixture did not leave an actual isolated owned child tree"
        admitted = False
        admission_error = None
        try:
            with owner.store.writer_lease(original.job_id) as lease:
                admitted = True
                owner.observe(
                    "crashed-owner-replacement-admission",
                    owned_tree_alive=owner.tree_live(),
                    replacement_lease=lease.lease_id,
                )
                assert not owner.tree_live(), (
                    "replacement writer acquired authority while old owner's"
                    " isolated processes remained live"
                )
        except (
            PresentationWriterBusyError,
            PresentationJobInterruptedError,
        ) as error:
            admission_error = type(error).__name__
        if not admitted:
            current = owner.store.get(original.job_id)
            owner.observe(
                "crashed-owner-replacement-blocked",
                error=admission_error,
                owned_tree_alive=owner.tree_live(),
            )
            assert current.active_lease_id == original.active_lease_id
        assert wait_until(lambda: not owner.tree_live(), 5), (
            "owner crash left its actual isolated tree alive without"
            " autonomous settlement"
        )
        owner.observe(
            "crashed-owner-autonomous-settlement",
            owned_tree_alive=owner.tree_live(),
        )
        assert (
            owner.store.get(original.job_id).deadline_at
            == original.deadline_at
        )
        assert (
            owner.store.get(original.job_id).cost_used_units
            == original.cost_used_units
        )
    finally:
        owner.close()


def test_unclaimed_recovery_preserves_original_deadline_and_budget(tmp_path):
    store = PresentationJobStore(tmp_path)
    original = store.create(base.request())
    runtime = None
    try:
        runtime, error = construct_recovery_runtime(store.root)
        assert error is None, error
        assert wait_until(
            lambda: bool(getattr(runtime, "recovered_jobs", [])),
            3,
        )
        assert runtime.recovered_jobs[0] == (
            original.job_id,
            original.deadline_at,
            original.cost_used_units,
        )
        assert wait_until(
            lambda: store.get(original.job_id).active_lease_id is None,
            2,
        )
    finally:
        if runtime is not None:
            runtime.shutdown()
