# -*- coding: utf-8 -*-
"""Real production subprocess locks with a finite local Python process tree.

Only executable resolution/script instance configuration is synthetic.
The genuine service/compiler/render/conversion launch and writer paths run.
No subprocess/Popen/communicate/shutdown implementation is monkeypatched.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from lantai.app import office_conversion
from lantai.presentation.jobs import (
    PresentationExecutionLimits,
    PresentationJobInterruptedError,
)
from lantai.presentation.models import CompositionEngine, JobStatus
from lantai.presentation.pptx_compiler import NativePptxCompiler
from lantai.presentation.service import PresentationService

from . import test_whole_job_lifecycle as baseline

ROOT = Path(__file__).resolve().parent
PROMPT_STOP_SECONDS = 1.5
REAP_SECONDS = 2.5
# SIGTERM handler files are observations only. A legitimate hard group kill
# also qualifies once all actual members have exited and been reaped.


def wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def record_evidence(name, values):
    target = os.environ.get("INDEPENDENT_PPT_PROCESS_OBSERVATIONS")
    if target:
        with Path(target).open("a", encoding="utf-8") as output:
            output.write(
                json.dumps({"case": name, **values}, sort_keys=True) + "\n",
            )


class RealProductionPath:
    def __init__(self, tmp_path, monkeypatch, stage, delayed=False):
        self.stage = stage
        self.state = tmp_path / stage / "process-state"
        self.state.mkdir(parents=True)
        self.script = self.state.parent / "synthetic-executable.py"
        body = (ROOT / "synthetic_process_tree.py").read_text(encoding="utf-8")
        header = (
            "#!"
            + sys.executable
            + "\nfrom pathlib import Path\n"
            + "STATE = Path("
            + repr(str(self.state))
            + ")\n"
            + "DELAYED = "
            + repr(delayed)
            + "\n"
        )
        # The fixture imports __future__, which must precede these constants.
        body = body.replace("from __future__ import annotations\n", "")
        self.script.write_text(header + body, encoding="utf-8")
        self.script.chmod(0o700)
        self.service = PresentationService(
            tmp_path / stage / "jobs",
            execution_limits=PresentationExecutionLimits(),
        )
        req = baseline.request(
            composition_engine=(
                CompositionEngine.NATIVE_PPTX
                if stage == "native"
                else CompositionEngine.LEGACY_PYTHON_PPTX
            ),
        )
        self.record = self.service.create_job(req)
        self.lease_id = None
        self.errors = []
        self.finished = threading.Event()
        self.result = None
        if stage == "native":
            backend = self.state.parent / "synthetic-backend"
            backend.mkdir()
            compiler = NativePptxCompiler(
                node_binary=sys.executable,
                backend_module_path=backend,
            )
            compiler.script_path = self.script
            self.service._native_pptx_compiler = compiler
        else:
            monkeypatch.setattr(
                office_conversion,
                "find_soffice",
                lambda: str(self.script),
            )
            self.input = self.state.parent / "synthetic.pptx"
            baseline.PresentationCompiler().compile(
                baseline.deck(),
                self.input,
            )

    def start(self):
        def action():
            try:
                with self.service.store.writer_lease(
                    self.record.job_id,
                ) as lease:
                    self.lease_id = lease.lease_id
                    if self.stage == "native":
                        self.result = self.service.compose(
                            self.record.job_id,
                            baseline.deck(),
                            writer_lease=lease,
                        )
                    else:
                        self.result = self.service.verify_artifact(
                            baseline.deck(),
                            self.input,
                            self.state.parent / "render-output",
                            visual_required=False,
                            job_id=self.record.job_id,
                        )
            except BaseException as error:
                self.errors.append(error)
            finally:
                self.finished.set()

        self.thread = threading.Thread(target=action, daemon=True)
        self.thread.start()
        assert wait_until(
            lambda: self.has("parent.ready") or self.finished.is_set(),
            5,
        ), "actual production subprocess was not admitted"
        assert self.has("parent.ready"), [
            type(e).__name__ + ": " + str(e) for e in self.errors
        ]
        self.parent = json.loads((self.state / "parent.json").read_text())
        self.child = json.loads((self.state / "child.json").read_text())
        assert self.parent["child_pid"] == self.child["pid"]
        assert self.parent["pgid"] == self.child["pgid"]
        assert self.parent["script"] == str(self.script.resolve())

    def has(self, name):
        return (self.state / name).is_file()

    def active_owner(self):
        return self.service.store.get(self.record.job_id).active_lease_id

    def cancel(self):
        self.service.store.cancel(
            self.record.job_id,
            self.record.conversation_id,
        )

    def release(self):
        (self.state / "release").write_text("synthetic", encoding="utf-8")

    def pid_exists(self, pid):
        return psutil.pid_exists(pid)

    def tree_reaped(self):
        return not any(
            self.pid_exists(info["pid"]) for info in (self.parent, self.child)
        )

    def observe(self, name):
        record_evidence(
            name,
            {
                "stage": self.stage,
                "parent_pid": self.parent["pid"],
                "parent_pgid": self.parent["pgid"],
                "child_pid": self.child["pid"],
                "child_pgid": self.child["pgid"],
                "group_isolated": self.parent["pid"] == self.parent["pgid"],
                "parent_term": self.has("parent.term"),
                "child_term": self.has("child.term"),
                "child_cleanup_entered": self.has("child.cleanup-entered"),
                "child_cleanup_complete": self.has("child.cleanup-complete"),
                "parent_cleanup_complete": self.has("parent.cleanup-complete"),
                "child_reaped_ack": self.has("child.reaped"),
                "parent_pid_exists": self.pid_exists(self.parent["pid"]),
                "child_pid_exists": self.pid_exists(self.child["pid"]),
                "writer_matches_original": self.active_owner()
                == self.lease_id,
                "writer_present": bool(self.active_owner()),
                "worker_finished": self.finished.is_set(),
                "worker_errors": [
                    type(error).__name__ for error in self.errors
                ],
            },
        )

    def close(self):
        # Cleanup only our exact synthetic PIDs after verifying their
        # executable
        # script. Never signal a shared process group, App, or unrelated
        # process.
        self.release()
        for pidfile in ("child.json", "parent.json"):
            path = self.state / pidfile
            if not path.is_file():
                continue
            pid = json.loads(path.read_text())["pid"]
            if pid <= 1 or pid == os.getpid():
                raise AssertionError("unsafe synthetic PID")
            try:
                process = psutil.Process(pid)
                assert (
                    str(self.script) in process.cmdline()
                ), "PID no longer belongs to fixture"
                process.send_signal(signal.SIGTERM)
            except psutil.NoSuchProcess:
                pass
        if hasattr(self, "thread"):
            if not self.finished.wait(4):
                for pidfile in ("child.json", "parent.json"):
                    path = self.state / pidfile
                    if not path.is_file():
                        continue
                    pid = json.loads(path.read_text())["pid"]
                    try:
                        process = psutil.Process(pid)
                        assert str(self.script) in process.cmdline()
                        process.kill()
                    except psutil.NoSuchProcess:
                        pass
            assert self.finished.wait(
                5,
            ), "actual launch worker did not settle after safety cleanup"
            self.thread.join(1)
        if hasattr(self, "parent"):
            assert wait_until(
                self.tree_reaped,
                3,
            ), "owned synthetic process survived safety cleanup"
            self.observe("post-safety-cleanup")


@pytest.mark.parametrize("stage", ["native", "libreoffice"])
def test_actual_subprocess_stop_terminates_and_reaps_entire_tree(
    tmp_path,
    monkeypatch,
    stage,
):
    path = RealProductionPath(tmp_path, monkeypatch, stage)
    try:
        path.start()
        original_owner = path.active_owner()
        assert original_owner == path.lease_id
        path.cancel()
        settled = path.finished.wait(PROMPT_STOP_SECONDS)
        path.observe("stop-before-safety-release")
        assert (
            settled
        ), "Stop left the actual production process launch unsettled"
        assert wait_until(
            path.tree_reaped,
            1,
        ), "Stop left an unreaped parent or descendant"
        assert path.active_owner() is None
        assert (
            path.service.store.get(path.record.job_id).status
            == JobStatus.CANCELLED
        )
        assert not (
            path.service.store.job_dir(path.record.job_id)
            / "output/presentation.pptx"
        ).exists()
    finally:
        path.close()


def test_actual_subprocess_deadline_terminates_both_production_chains(
    tmp_path,
    monkeypatch,
):
    """Two minimum-valid 30-second jobs run concurrently; no forged
    clock/deadline.
    """
    paths = [
        RealProductionPath(tmp_path, monkeypatch, stage)
        for stage in ("native", "libreoffice")
    ]
    try:
        for path in paths:
            path.start()
        deadline = max(
            path.service.store.get(path.record.job_id).deadline_at.timestamp()
            for path in paths
        )
        # This uses the real persisted deadline and actual wall clock. It does
        # not stub a timeout, advance a test clock, or manually interrupt a
        # job.
        assert wait_until(
            lambda: time.time() >= deadline + PROMPT_STOP_SECONDS,
            34,
        ), "real minimum job deadline was not reached"
        for path in paths:
            path.observe("deadline-before-safety-release")
        violations = []
        for path in paths:
            if not path.finished.wait(0.1):
                violations.append(
                    path.stage + ": deadline left its worker running",
                )
            if not path.tree_reaped():
                violations.append(
                    path.stage + ": deadline left owned PIDs unreaped",
                )
            if path.active_owner() is not None:
                violations.append(
                    path.stage + ": deadline did not settle writer ownership",
                )
        assert not violations, "; ".join(violations)
    finally:
        for path in paths:
            path.close()


@pytest.mark.parametrize("stage", ["native", "libreoffice"])
def test_duplicate_stop_keeps_writer_until_delayed_process_cleanup_ack(
    tmp_path,
    monkeypatch,
    stage,
):
    path = RealProductionPath(tmp_path, monkeypatch, stage, delayed=True)
    try:
        path.start()
        path.cancel()
        delivered = wait_until(
            lambda: path.has("child.cleanup-entered")
            or path.finished.is_set(),
            PROMPT_STOP_SECONDS,
        )
        path.observe("first-stop-delayed-cleanup")
        assert (
            delivered
        ), "Stop neither entered descendant cleanup nor reaped the actual tree"
        owner = path.active_owner()
        if not path.tree_reaped():
            assert (
                owner == path.lease_id
            ), "writer released before actual process cleanup"
            assert (
                not path.finished.is_set()
            ), "launch settled with live owned process members"
        path.cancel()
        time.sleep(0.05)
        path.observe("duplicate-stop-delayed-cleanup")
        owner = path.active_owner()
        if not path.tree_reaped():
            assert (
                not path.finished.is_set()
            ), "duplicate Stop abandoned pending process cleanup"
            assert (
                owner == path.lease_id
            ), "duplicate Stop cleared an unsettled writer"
            # A second independent store cannot acquire this live writer's
            # lock.
            competing = baseline.PresentationJobStore(path.service.store.root)
            with pytest.raises(
                (RuntimeError, PresentationJobInterruptedError),
            ):
                with competing.writer_lease(path.record.job_id):
                    raise AssertionError(
                        "duplicate Stop released a live process owner's writer"
                        " lock",
                    )
        path.release()
        assert path.finished.wait(REAP_SECONDS)
        assert wait_until(path.tree_reaped, 1)
        assert path.active_owner() is None
        assert (
            path.service.store.get(path.record.job_id).status
            == JobStatus.CANCELLED
        )
    finally:
        path.close()


@pytest.mark.parametrize("stage", ["native", "libreoffice"])
def test_actual_subprocess_owns_an_isolated_process_group(
    tmp_path,
    monkeypatch,
    stage,
):
    path = RealProductionPath(tmp_path, monkeypatch, stage)
    try:
        path.start()
        path.observe("process-group-admission")
        assert path.parent["pgid"] == path.parent["pid"], (
            "production launch shares the verifier process group; safe group"
            " Stop is impossible"
        )
        assert path.child["pgid"] == path.parent["pid"]
    finally:
        path.close()


@pytest.mark.parametrize("stage", ["native", "libreoffice"])
def test_natural_executable_failure_is_reaped_without_false_success(
    tmp_path,
    monkeypatch,
    stage,
):
    path = RealProductionPath(tmp_path, monkeypatch, stage)
    try:
        path.start()
        assert not path.finished.is_set()
        assert path.active_owner() == path.lease_id
        path.release()
        assert path.finished.wait(REAP_SECONDS)
        assert path.has("child.cleanup-complete") and path.has("child.reaped")
        assert wait_until(path.tree_reaped, 1)
        assert path.active_owner() is None
        assert (
            path.service.store.get(path.record.job_id).status
            != JobStatus.COMPLETED
        )
        assert not (
            path.service.store.job_dir(path.record.job_id)
            / "output/presentation.pptx"
        ).exists()
        if stage == "native":
            assert len(path.errors) == 1 and isinstance(
                path.errors[0],
                RuntimeError,
            )
        else:
            assert not path.errors, [
                type(error).__name__ for error in path.errors
            ]
            report, images = path.result
            assert not images
            assert any(
                issue.code == "render.failed" for issue in report.issues
            )
        path.observe("natural-failure-settled")
    finally:
        path.close()
