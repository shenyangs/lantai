# -*- coding: utf-8 -*-
"""Finite additional shutdown/guardian boundaries, synthetic files and Python
only.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lantai.presentation import intelligence as intelligence_module
from lantai.presentation.jobs import PresentationJobStore
from lantai.presentation.models import JobStatus
from lantai.presentation.quality import file_sha256
from lantai.utils import owned_subprocess

from . import test_whole_job_lifecycle as base
from .test_completed_edit_stop import completed_fixture
from .test_real_process_settlement import record_evidence


def test_shutdown_drains_active_synchronous_completed_edit(
    tmp_path,
    monkeypatch,
):
    runtime, original, output, initial, versions = completed_fixture(
        tmp_path,
        monkeypatch,
    )
    entered, cancelled, release_work, release_cleanup, returned = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    shutdown_errors = []

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
            "合成修改请求",
            initial.pptx_sha256,
        ),
    )

    def shutdown():
        try:
            runtime.shutdown()
        except BaseException as error:
            shutdown_errors.append(error)
        finally:
            returned.set()

    closer = threading.Thread(target=shutdown, daemon=True)
    try:
        assert entered.wait(4), worker.errors
        active = runtime.service.store.get(original.job_id)
        assert active.active_operation is not None and active.active_lease_id
        closer.start()
        assert cancelled.wait(1.5), "shutdown did not retire actual edit model"
        record_evidence(
            "shutdown-active-synchronous-edit",
            {
                "shutdown_returned": returned.is_set(),
                "worker_finished": worker.finished.is_set(),
                "model_cleanup_pending": not release_cleanup.is_set(),
                "writer_original": runtime.service.store.get(
                    original.job_id,
                ).active_lease_id
                == active.active_lease_id,
            },
        )
        assert (
            not returned.is_set()
        ), "shutdown returned before synchronous edit model cleanup"
        assert not worker.finished.is_set()
        assert (
            runtime.service.store.get(original.job_id).active_lease_id
            == active.active_lease_id
        )
        release_cleanup.set()
        worker.settle()
        assert returned.wait(3)
        assert not shutdown_errors, shutdown_errors
        final = runtime.service.store.get(original.job_id)
        assert final.active_lease_id is None and final.active_operation is None
        assert final.status == JobStatus.COMPLETED
        assert final.operation_history[-1].status == JobStatus.CANCELLED
        assert (
            final.deadline_at == original.deadline_at
            and final.cost_used_units == original.cost_used_units
        )
        assert file_sha256(output) == initial.pptx_sha256
        assert versions.current().version_id == initial.version_id
    finally:
        release_work.set()
        release_cleanup.set()
        worker.settle()
        if closer.ident is not None:
            closer.join(4)
            assert not closer.is_alive(), "synthetic shutdown worker leaked"
        runtime.shutdown()


def test_closed_runtime_refuses_new_explicit_edit_before_model_or_writer(
    tmp_path,
    monkeypatch,
):
    runtime, original, output, initial, versions = completed_fixture(
        tmp_path,
        monkeypatch,
    )
    runtime.shutdown()
    dispatched = []

    async def offline_model(**kwargs):
        del kwargs
        dispatched.append("synthetic-model")
        raise RuntimeError("synthetic safety refusal")

    monkeypatch.setattr(
        intelligence_module,
        "run_structured_utility_task",
        offline_model,
    )
    error = None
    try:
        runtime.redo_slide(
            original.job_id,
            original.conversation_id,
            "edit-first",
            "关闭后合成修改请求",
            initial.pptx_sha256,
        )
    except BaseException as exc:
        error = exc
    finally:
        runtime.shutdown()
    final = runtime.service.store.get(original.job_id)
    record_evidence(
        "closed-runtime-explicit-edit-admission",
        {
            "model_dispatches": len(dispatched),
            "error_type": type(error).__name__,
            "active_lease_id": final.active_lease_id,
            "operation_history_count": len(final.operation_history),
            "status": final.status.value,
        },
    )
    assert (
        not dispatched
    ), "closed Runtime admitted a fresh edit model operation"
    assert error is not None
    assert final.active_lease_id is None and final.active_operation is None
    assert len(final.operation_history) == len(original.operation_history)
    assert final.status == JobStatus.COMPLETED
    assert (
        final.deadline_at == original.deadline_at
        and final.cost_used_units == original.cost_used_units
    )
    assert file_sha256(output) == initial.pptx_sha256
    assert versions.current().version_id == initial.version_id


def test_guardian_missing_executable_confirms_settlement_and_retains_errno(
    tmp_path,
):
    store = PresentationJobStore(tmp_path / "jobs")
    original = store.create(base.request())
    with pytest.raises(FileNotFoundError) as raised:
        with store.writer_lease(original.job_id):
            owned_subprocess.run(
                [str(tmp_path / "does-not-exist")],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
    assert raised.value.errno == 2
    final = store.get(original.job_id)
    assert (
        final.active_lease_id is None and not final.resource_settlement_pending
    )
    assert (
        final.deadline_at == original.deadline_at
        and final.cost_used_units == original.cost_used_units
    )
    with store.writer_lease(original.job_id):
        pass


def test_guardian_preserves_real_stdin_stdout_and_target_status(tmp_path):
    store = PresentationJobStore(tmp_path / "jobs")
    original = store.create(base.request())
    with store.writer_lease(original.job_id):
        result = owned_subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "import sys; print('SYNTHETIC:' + sys.stdin.read());"
                    " sys.exit(7)"
                ),
            ],
            input="private-synthetic-input",
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert result.returncode == 7
        assert result.stdout.strip() == "SYNTHETIC:private-synthetic-input"
        assert result.stderr == ""
    assert store.get(original.job_id).active_lease_id is None


@pytest.mark.parametrize(
    "frozen_layout",
    [False, True],
    ids=["source-entry", "frozen-layout-source-entry"],
)
def test_fixed_entry_dispatches_guardian_before_cli_or_skill_bridge(
    tmp_path,
    frozen_layout,
):
    source = Path(owned_subprocess.__file__).resolve().parents[3]
    entry = source / "electron" / "entry.py"
    # This runs actual source entry under Python, never a packaged executable.
    wrapper = tmp_path / "entry-probe.py"
    forbidden = tmp_path / "forbidden-skill-bridge.py"
    marker = tmp_path / "forbidden-marker"
    forbidden.write_text(
        "from pathlib import Path\nPath("
        + repr(str(marker))
        + ").write_text('unexpected')\n",
    )
    wrapper.write_text(
        "import runpy,sys\nsys.frozen="
        + repr(frozen_layout)
        + "\nsys._MEIPASS="
        + repr(str(source / "src"))
        + "\nsys.argv=["
        + repr(str(entry))
        + ",*sys.argv[1:]]\nrunpy.run_path("
        + repr(str(entry))
        + ",run_name='__main__')\n",
    )
    config_read, config_write = os.pipe()
    life_read, life_write = os.pipe()
    receipt_read, receipt_write = os.pipe()
    store = PresentationJobStore(tmp_path / "entry-jobs")
    original = store.create(base.request())
    process = None
    descriptors = (
        config_read,
        config_write,
        life_read,
        life_write,
        receipt_read,
        receipt_write,
    )
    try:
        with store.writer_lease(original.job_id):
            # Opening an independent FD is only an inherited regular descriptor
            # shape control; the actual writer admission is above.
            with (store.job_dir(original.job_id) / ".writer.lock").open(
                "a+b",
            ) as fence:
                env = os.environ.copy()
                env["LANTAI_RUN_SKILL_SCRIPT_PATH"] = str(forbidden)
                # The explicit owner settles this process in cleanup.
                # pylint: disable-next=consider-using-with
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        str(wrapper),
                        "--owned-process-supervisor",
                        str(config_read),
                        str(life_read),
                        str(receipt_write),
                        str(fence.fileno()),
                    ],
                    pass_fds=(
                        config_read,
                        life_read,
                        receipt_write,
                        fence.fileno(),
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    start_new_session=True,
                )
                for descriptor in (config_read, life_read, receipt_write):
                    os.close(descriptor)
                command = {
                    "args": [
                        sys.executable,
                        "-B",
                        "-c",
                        "print('SYNTHETIC_ENTRY_OK')",
                    ],
                    "shell": False,
                    "executable": None,
                }
                os.write(config_write, json.dumps(command).encode("utf-8"))
                os.close(config_write)
                stdout, stderr = process.communicate(timeout=5)
                receipt = json.loads(os.read(receipt_read, 65536))
                assert process.returncode == 0 and receipt == {
                    "returncode": 0,
                    "settled": True,
                }, stderr
                assert stdout.strip() == "SYNTHETIC_ENTRY_OK"
                assert (
                    not marker.exists()
                ), "private route reached generic skill bridge"
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
