# -*- coding: utf-8 -*-
"""Actual public download/health/controller ownership locks with only own
Python processes.

Synthetic trusted archive bytes remain inert. External HTTP, license
catalog and run_command inputs alone are replaced in the spawn worker. No
health-admission, controller, install validator or process-settlement
function is substituted.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from lantai.local_models.download_manager import (
    DownloadProgressTracker,
    ProcessDownloadController,
)
import lantai.local_models.llamacpp as module
from tests.unit.local_models.process_health_fixture_worker import (
    offline_health_worker,
)
from tests.unit.local_models.test_source_install_regressions import (
    BASE_URL,
    TAG,
    backend,
    good_archive,
    synthetic_reviewed_license_manifest,
)


class RecordingContext:
    def __init__(self):
        self.raw = multiprocessing.get_context("spawn")
        self.processes = []

    def Process(self, **kwargs):
        process = self.raw.Process(**kwargs)
        self.processes.append(process)
        return process

    def __getattr__(self, name):
        return getattr(self.raw, name)


def wait_until(predicate, timeout=7):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.015)
    raise AssertionError(
        "own process fixture did not reach its finite boundary",
    )


def process_identity(pid):
    # Query only a PID explicitly created and recorded by this fixture. No
    # argv,
    # environment or arbitrary user process inventory is read.
    output = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "pid=,pgid=,lstart=,stat="],
        capture_output=True,
        text=True,
        check=False,
        timeout=2,
    ).stdout.strip()
    return (
        " ".join(output.split()[:-1])
        if output and output.split()[-1][0] != "Z"
        else None
    )


def read_record(path, token):
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if value.get("token") != token:
        raise AssertionError("own process identity token differs from fixture")
    return value


def build_public_spec(monkeypatch, tmp_path, mode):
    instance = backend(tmp_path / "installation" / "bin")
    data = good_archive(instance, marker=b"own-health-process-fixture-inert")
    fields = {
        "tag": TAG,
        "os_name": instance.os_name,
        "arch": instance.arch,
        "backend": instance.backend,
        "cuda_version": instance.cuda_version,
        "file_name": getattr(instance, "_build_filename")(TAG),
        "url": f"{BASE_URL}/{TAG}/{getattr(instance, '_build_filename')(TAG)}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }
    asset = module.TrustedLlamaCppAsset(**fields)
    monkeypatch.setattr(
        instance,
        "_resolve_release_asset",
        lambda base_url, tag: asset,
    )
    monkeypatch.setattr(
        module,
        "reviewed_license_manifest",
        lambda admitted: synthetic_reviewed_license_manifest(),
    )
    instance.start_download(BASE_URL, TAG, chunk_size=1024, timeout=1)
    spec = getattr(instance, "_download_controller").specs[-1]
    stage = Path(spec.task.payload["staging_dir"])
    token = uuid.uuid4().hex
    actual_cleanup = spec.task.cleanup

    def witnessed_cleanup():
        # Observe the boundary, then call the original identity-bound cleanup.
        # This does not replace any settlement or directory validator.
        alive = []
        for name in ("child.json", "license-child.json"):
            record = read_record(tmp_path / name, token)
            if (
                record is not None
                and process_identity(record["pid"]) is not None
            ):
                alive.append(record["pid"])
        (tmp_path / "cleanup-observation.json").write_text(
            json.dumps({"live_owned_children": alive}) + "\n",
        )
        actual_cleanup()

    spec.task.cleanup = witnessed_cleanup
    spec.task.payload.update(
        fixture_archive=data,
        fixture_root=str(tmp_path),
        fixture_mode=mode,
        fixture_token=token,
    )
    # Only the external worker input wrapper is changed; it invokes the actual
    # product download worker and does not override owns_child_processes.
    spec.task.target = offline_health_worker
    spec.poll_interval = 0.02
    context = RecordingContext()
    controller = ProcessDownloadController(
        context=context,
        progress=DownloadProgressTracker(),
    )
    setattr(instance, "_download_controller", controller)
    return instance, controller, context, spec, stage, token


@pytest.fixture(name="own_process_safety")
def owned_process_safety(monkeypatch):
    parent_group = os.getpgid(0)
    allowed_contexts = []
    group_signals = []
    real_killpg = os.killpg

    def guarded_killpg(pgid, signum):
        # A bad implementation cannot signal the pytest/user parent group. The
        # guard still raises, so refusing the signal cannot turn a bug green.
        if pgid == parent_group:
            raise AssertionError(
                (
                    "download cleanup attempted to signal the parent process "
                    "group"
                ),
            )
        owned = {
            process.pid
            for context in allowed_contexts
            for process in context.processes
            if process.pid is not None
        }
        if pgid not in owned:
            raise AssertionError(
                (
                    "download cleanup attempted to signal an unowned process "
                    "group"
                ),
            )
        group_signals.append((pgid, int(signum)))
        return real_killpg(pgid, signum)

    monkeypatch.setattr(os, "killpg", guarded_killpg)
    return allowed_contexts, group_signals


def cleanup_own_processes(context, tmp_path, token, observed):
    # This independent finally path runs on RED too, and checks observed OS
    # creation identity before sending any single-PID cleanup signal.
    for name in (
        "child.json",
        "license-child.json",
        "same-group-sentinel.json",
        "other-session-sentinel.json",
    ):
        record = read_record(tmp_path / name, token)
        if record is not None and record["pid"] not in observed:
            observed[record["pid"]] = process_identity(record["pid"])
    for pid, birth in observed.items():
        if birth is not None and process_identity(pid) == birth:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for process in context.processes:
        try:
            if process.is_alive():
                process.kill()
            process.join(timeout=2)
        except (AssertionError, ValueError):
            pass
    for pid, birth in observed.items():
        if birth is not None:
            wait_until(
                lambda pid=pid, birth=birth: process_identity(pid) != birth,
                timeout=3,
            )


def start_sentinel(tmp_path, token, name, *, new_session):
    marker = tmp_path / name
    # The caller retains and settles this cross-group sentinel.
    # pylint: disable=consider-using-with
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("own_health_child.py")),
            str(marker),
            token,
            "30",
            "normal",
        ],
        start_new_session=new_session,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    # pylint: enable=consider-using-with
    wait_until(marker.exists)
    return process


def test_public_llama_download_explicitly_owns_health_children(
    tmp_path,
    monkeypatch,
):
    _, _, _, spec, _, _ = build_public_spec(
        monkeypatch,
        tmp_path,
        "before_target",
    )
    try:
        assert (
            getattr(spec.task, "owns_child_processes", False) is True
        ), "public llama download lacks child ownership opt-in"
    finally:
        spec.task.run_cleanup()


@pytest.mark.skipif(
    os.name != "posix",
    reason="actual POSIX ownership, Windows real host remains pending",
)
def test_cancel_before_product_worker_entry_settles_owned_stage(
    tmp_path,
    monkeypatch,
    own_process_safety,
):
    instance, controller, context, spec, stage, token = build_public_spec(
        monkeypatch,
        tmp_path,
        "before_target",
    )
    own_process_safety[0].append(context)
    observed = {}
    try:
        controller.start(spec)
        worker = wait_until(
            lambda: read_record(tmp_path / "worker.json", token),
        )
        instance.cancel_download()
        instance.cancel_download()
        assert controller.snapshot()["status"] == "cancelled"
        assert not controller.is_active()
        assert process_identity(worker["pid"]) is None
        assert not (
            tmp_path / "child.json"
        ).exists(), "health started despite pre-entry cancellation"
        assert not stage.exists()
    finally:
        cleanup_own_processes(context, tmp_path, token, observed)


@pytest.mark.skipif(
    os.name != "posix",
    reason="actual POSIX ownership, Windows real host remains pending",
)
@pytest.mark.parametrize("mode", ["health_pending", "ignore_term"])
def test_health_cancel_settles_child_and_preserves_other_groups(
    tmp_path,
    monkeypatch,
    own_process_safety,
    mode,
):
    instance, controller, context, spec, stage, token = build_public_spec(
        monkeypatch,
        tmp_path,
        mode,
    )
    own_process_safety[0].append(context)
    observed = {}
    sentinels = []
    try:
        for name, new_session in (
            ("same-group-sentinel.json", False),
            ("other-session-sentinel.json", True),
        ):
            sentinel = start_sentinel(
                tmp_path,
                token,
                name,
                new_session=new_session,
            )
            sentinels.append(sentinel)
            observed[sentinel.pid] = process_identity(sentinel.pid)
        controller.start(spec)
        pending = wait_until(
            lambda: read_record(tmp_path / "health-pending.json", token),
        )
        child = read_record(tmp_path / "child.json", token)
        observed[child["pid"]] = process_identity(child["pid"])
        assert (
            pending["child_pid"] == child["pid"]
            and observed[child["pid"]] is not None
        )
        instance.cancel_download()
        instance.cancel_download()
        assert controller.snapshot()["status"] == "cancelled"
        assert not controller.is_active()
        assert all(
            sentinel.poll() is None for sentinel in sentinels
        ), "owned cancellation touched unrelated own sentinels"
        assert not json.loads(
            (tmp_path / "cleanup-observation.json").read_text(),
        )[
            "live_owned_children"
        ], "stage cleanup ran while owned health children remained alive"
        assert (
            process_identity(child["pid"]) is None
        ), "cancelled worker leaked its running health child"
        assert not stage.exists()
    finally:
        cleanup_own_processes(context, tmp_path, token, observed)
        for sentinel in sentinels:
            sentinel.wait(timeout=2)


@pytest.mark.skipif(
    os.name != "posix",
    reason="actual POSIX ownership, Windows real host remains pending",
)
def test_unexpected_health_worker_exit_settles_child_before_releasing_stage(
    tmp_path,
    monkeypatch,
    own_process_safety,
):
    _, controller, context, spec, stage, token = build_public_spec(
        monkeypatch,
        tmp_path,
        "worker_exit",
    )
    own_process_safety[0].append(context)
    observed = {}
    try:
        controller.start(spec)
        child = wait_until(lambda: read_record(tmp_path / "child.json", token))
        observed[child["pid"]] = process_identity(child["pid"])
        assert observed[child["pid"]] is not None
        (tmp_path / "worker-exit-observed.ready").write_text(token)
        wait_until(lambda: controller.snapshot()["status"] == "failed")
        assert not controller.is_active()
        assert not json.loads(
            (tmp_path / "cleanup-observation.json").read_text(),
        )[
            "live_owned_children"
        ], "unexpected-exit stage cleanup preceded child settlement"
        assert (
            process_identity(child["pid"]) is None
        ), "unexpected worker exit leaked its health child"
        assert not stage.exists()
    finally:
        cleanup_own_processes(context, tmp_path, token, observed)


@pytest.mark.skipif(
    os.name != "posix",
    reason="actual POSIX ownership, Windows real host remains pending",
)
def test_normal_actual_worker_waits_for_own_health_commands_and_publishes(
    tmp_path,
    monkeypatch,
    own_process_safety,
):
    instance, controller, context, spec, stage, token = build_public_spec(
        monkeypatch,
        tmp_path,
        "normal",
    )
    own_process_safety[0].append(context)
    observed = {}
    try:
        controller.start(spec)
        wait_until(
            lambda: controller.snapshot()["status"] in {"completed", "failed"},
        )
        assert controller.snapshot()["status"] == "completed"
        assert not controller.is_active()
        for name in ("child.json", "license-child.json"):
            record = read_record(tmp_path / name, token)
            assert record is not None
            assert process_identity(record["pid"]) is None
        assert not stage.exists()
        assert instance.check_llamacpp_installation() == (True, "")
    finally:
        cleanup_own_processes(context, tmp_path, token, observed)
