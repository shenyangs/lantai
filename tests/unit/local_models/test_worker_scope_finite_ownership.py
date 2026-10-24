"""Limited Windows Job API doubles, not Windows host or package evidence.

The implementation itself runs. The double replaces only unavailable OS APIs
and a paused multiprocessing child; it never invokes a real Windows process.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


class FakeFunction:
    def __init__(self, name, callback):
        self.name, self.callback = name, callback
    def __call__(self, *args):
        return self.callback(*args)


class FiniteJobAPI:
    JOB, PROCESS, PID = 41001, 41002, 41003
    def __init__(self, events):
        self.events = events
        self.active = 1
        self.fail_limits = False
        self.close_failures = 0
        self.process = None
        for name, callback in (
            ("CreateJobObjectW", self.create), ("SetInformationJobObject", self.set_limits),
            ("QueryInformationJobObject", self.query), ("OpenProcess", self.open_process),
            ("AssignProcessToJobObject", self.assign), ("TerminateJobObject", self.terminate),
            ("CloseHandle", self.close),
        ):
            setattr(self, name, FakeFunction(name, callback))
    def create(self, attributes, name):
        assert attributes is None and name is None
        self.events.append(("create", self.JOB))
        return self.JOB
    def set_limits(self, job, info_class, limits, size):
        assert job == self.JOB and info_class == 9
        assert limits._obj.BasicLimitInformation.LimitFlags == 0x2000, "Job must kill children when owner handle closes"
        assert size > 0
        self.events.append(("kill-on-close", job))
        return not self.fail_limits
    def query(self, job, info_class, accounting, size, returned):
        assert job == self.JOB and info_class == 1 and size > 0
        accounting._obj.ActiveProcesses = self.active
        self.events.append(("query", job, self.active))
        return True
    def open_process(self, rights, inherit, pid):
        assert pid == self.PID and inherit is False
        assert rights & 0x100 and rights & 0x1
        self.events.append(("open-owned-pid", pid))
        return self.PROCESS
    def assign(self, job, process):
        assert (job, process) == (self.JOB, self.PROCESS)
        self.events.append(("assign", job, process))
        return True
    def terminate(self, job, exit_code):
        assert job == self.JOB and exit_code == 1
        self.events.append(("terminate-owned-job", job))
        self.active = 0
        if self.process is not None:
            self.process.alive = False
        return True
    def close(self, handle):
        assert handle in {self.JOB, self.PROCESS}
        self.events.append(("close", handle))
        if handle == self.JOB and self.close_failures:
            self.close_failures -= 1
            return False
        return True


class PausedControl:
    def __init__(self, events):
        self.events = events
        self.closed = False
    def poll(self, timeout):
        assert 0 < timeout <= 8
        return True
    def recv(self):
        return ("ready", FiniteJobAPI.PID, None)
    def send(self, message):
        self.events.append(("control-send", message))
    def close(self):
        self.closed = True


class PausedProcess:
    pid = FiniteJobAPI.PID
    def __init__(self, events):
        self.events = events
        self.alive, self.started, self.closed = False, False, False
    @property
    def exitcode(self):
        return None if not self.started or self.alive else 0
    def start(self):
        self.started, self.alive = True, True
        self.events.append(("worker-paused", self.pid))
    def is_alive(self): return self.alive
    def terminate(self):
        self.alive = False
        self.events.append(("raw-terminate", self.pid))
    def kill(self):
        self.alive = False
        self.events.append(("raw-kill", self.pid))
    def join(self, timeout=None): return None
    def close(self):
        assert not self.alive
        self.closed = True


class PausedContext:
    def __init__(self, events, api):
        self.events, self.api = events, api
        self.control, self.child_control = PausedControl(events), PausedControl(events)
    def Pipe(self, duplex):
        assert duplex is True
        return self.control, self.child_control
    def Process(self, **kwargs):
        assert kwargs["daemon"] is True
        self.api.process = PausedProcess(self.events)
        return self.api.process


@pytest.fixture
def finite_scope_api(monkeypatch):
    design_path = os.environ.get("LANTAI_SCOPE_DESIGN_FILE")
    if design_path:
        path = Path(design_path).resolve()
        assert path.parent == Path(__file__).resolve().parent, "design review file must belong to external evidence"
        spec = importlib.util.spec_from_file_location("lta022_worker_scope_design_only", path)
        scope = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scope)
    else:
        scope = importlib.import_module("lantai.local_models.worker_scope")
    events = []
    api = FiniteJobAPI(events)
    monkeypatch.setattr(scope.ctypes, "WinDLL", lambda name, use_last_error: api, raising=False)
    monkeypatch.setattr(scope.ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(scope.ctypes, "WinError", lambda code: OSError("finite synthetic Win32 failure"), raising=False)
    # Replace only this module's platform selection, not global os.name.
    monkeypatch.setattr(scope, "os", SimpleNamespace(name="nt"))
    return scope, api, events


def test_windows_scope_assigns_owned_pid_before_target_admission(finite_scope_api):
    scope, api, events = finite_scope_api
    context = PausedContext(events, api)
    target_calls = []
    worker = scope.OwnedDownloadProcess(context, target=lambda payload, queue: target_calls.append(payload),
                                         payload={}, queue=None, name="finite-owned-worker")
    try:
        worker.start()
        assert events.index(("kill-on-close", api.JOB)) < events.index(("assign", api.JOB, api.PROCESS))
        assert events.index(("assign", api.JOB, api.PROCESS)) < events.index(("control-send", "start"))
        assert not target_calls, "paused process double must not execute target in parent"
    finally:
        worker.close()
    assert ("terminate-owned-job", api.JOB) in events
    assert ("query", api.JOB, 0) in events
    assert ("close", api.JOB) in events
    assert api.process.closed


def test_windows_failed_job_setup_never_admits_target(finite_scope_api):
    scope, api, events = finite_scope_api
    api.fail_limits = True
    context = PausedContext(events, api)
    worker = scope.OwnedDownloadProcess(context, target=lambda payload, queue: None,
                                         payload={}, queue=None, name="finite-owned-worker")
    with pytest.raises(OSError):
        worker.start()
    assert ("control-send", "start") not in events
    assert not any(item[0] == "assign" for item in events)
    assert ("close", api.JOB) in events
    assert api.process.closed and not api.process.is_alive()


def test_windows_live_job_members_cannot_be_reported_settled(finite_scope_api):
    scope, api, events = finite_scope_api
    job = scope._WindowsJob(api.PID)
    try:
        with pytest.raises(RuntimeError, match="settle"):
            job.wait_empty(timeout=0)
        assert job._handle == api.JOB
        assert ("close", api.JOB) not in events
        job.terminate()
        job.wait_empty(timeout=0)
        assert ("query", api.JOB, 0) in events
    finally:
        job.close()


def test_windows_failed_handle_close_retains_exact_handle_for_retry(finite_scope_api):
    scope, api, events = finite_scope_api
    job = scope._WindowsJob(api.PID)
    job.terminate()
    job.wait_empty(timeout=0)
    api.close_failures = 1
    with pytest.raises(OSError):
        job.close()
    assert job._handle == api.JOB, "failed close discarded the only owned Job identity"
    job.close()
    assert job._handle is None
    assert events.count(("close", api.JOB)) == 2
