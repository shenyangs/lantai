"""An opt-in process scope for download workers that launch health children."""
from __future__ import annotations

import ctypes
import os
import signal
import threading
import time
from contextlib import suppress
from typing import Any, Callable

import psutil

_FORCE_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


class _WindowsJob:
    """Keep the Job handle in the owner so owner death also kills its children."""

    def __init__(self, pid: int) -> None:
        from ctypes import wintypes

        size_t = ctypes.c_size_t

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", size_t),
                ("MaximumWorkingSetSize", size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                ("ProcessMemoryLimit", size_t), ("JobMemoryLimit", size_t),
                ("PeakProcessMemoryUsed", size_t), ("PeakJobMemoryUsed", size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._accounting_type = Accounting
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        api = self._api
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        api.QueryInformationJobObject.restype = wintypes.BOOL
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self._handle = api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = api.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not api.AssignProcessToJobObject(self._handle, process_handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                api.CloseHandle(process_handle)
        except BaseException:
            self.close()
            raise

    def terminate(self) -> None:
        if self._handle and not self._api.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait_empty(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while self._handle:
            accounting = self._accounting_type()
            if not self._api.QueryInformationJobObject(
                self._handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not accounting.ActiveProcesses:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Owned download children did not settle")
            time.sleep(0.02)

    def close(self) -> None:
        handle = self._handle
        if handle and not self._api.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = None


def _watch_owner(control: Any, group_id: int | None) -> None:
    try:
        control.recv()
    except (EOFError, OSError):
        if group_id is not None:
            with suppress(ProcessLookupError):
                os.killpg(group_id, _FORCE_SIGNAL)
        # On Windows the owner-held Job closes when the owner exits.
        os._exit(1)


def _run_owned_worker(target: Callable, payload: dict, queue: Any, control: Any) -> None:
    """Do not enter the download target until the parent owns the child scope."""
    group_id = None
    if os.name != "nt":
        os.setsid()
        group_id = os.getpgrp()
        if group_id != os.getpid():
            raise RuntimeError("Could not isolate the download process group")
    control.send(("ready", os.getpid(), group_id))
    if control.recv() != "start":
        raise RuntimeError("Download worker has no process-scope admission")
    watcher = threading.Thread(target=_watch_owner, args=(control, group_id), daemon=True)
    watcher.start()
    target(payload, queue)


class OwnedDownloadProcess:
    """Multiprocessing-compatible wrapper that owns only one admitted scope."""

    def __init__(self, context: Any, *, target: Callable, payload: dict, queue: Any, name: str) -> None:
        self._control, self._child_control = context.Pipe(duplex=True)
        self._process = context.Process(
            target=_run_owned_worker,
            args=(target, payload, queue, self._child_control),
            name=name, daemon=True,
        )
        self._group_id: int | None = None
        self._birth: float | None = None
        self._pid: int | None = None
        self._job: _WindowsJob | None = None
        self._closed = False
        self._started = False
        self._exitcode: int | None = None
        self._lock = threading.RLock()

    @property
    def pid(self) -> int:
        return self._pid if self._pid is not None else self._process.pid

    @property
    def exitcode(self) -> int | None:
        return self._exitcode if self._closed else self._process.exitcode

    def start(self) -> None:
        try:
            self._process.start()
            self._started = True
            self._pid = self._process.pid
            self._child_control.close()
            if not self._control.poll(8.0):
                raise RuntimeError("Download process-scope admission timed out")
            message = self._control.recv()
            if not isinstance(message, tuple) or len(message) != 3 or message[:2] != ("ready", self.pid):
                raise RuntimeError("Download process-scope admission identity mismatch")
            if os.name == "nt":
                if message[2] is not None:
                    raise RuntimeError("Invalid Windows download scope")
                self._job = _WindowsJob(self.pid)
            else:
                if message[2] != self.pid or os.getpgid(self.pid) != self.pid:
                    raise RuntimeError("Download worker does not own its process group")
                self._birth = psutil.Process(self.pid).create_time()
                self._group_id = self.pid
            self._control.send("start")
        except BaseException:
            self.close()
            raise

    def _signal_scope(self, sig: int) -> None:
        if self._closed:
            return
        if self._job is not None:
            self._job.terminate()
        elif self._group_id is not None:
            # An empty group can disappear and its numeric ID can be reused.
            # Never signal a new leader at the original worker PID.
            try:
                if psutil.Process(self._group_id).create_time() != self._birth:
                    self._group_id = None
                    raise RuntimeError("Download process-group identity changed")
            except psutil.NoSuchProcess:
                pass  # A surviving child still keeps the original group alive.
            with suppress(ProcessLookupError):
                os.killpg(self._group_id, sig)
        elif self._started and self._process.is_alive():
            # Before admission the worker cannot execute a target or child.
            (self._process.kill if sig == _FORCE_SIGNAL else self._process.terminate)()

    def terminate(self) -> None:
        with self._lock:
            self._signal_scope(signal.SIGTERM)

    def kill(self) -> None:
        with self._lock:
            self._signal_scope(_FORCE_SIGNAL)

    def is_alive(self) -> bool:
        return self._started and not self._closed and self._process.is_alive()

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._process.join(timeout=timeout)
            if not self._process.is_alive():
                self._exitcode = self._process.exitcode
                self.kill()  # A worker can exit while its health child lives.
                self._wait_scope_empty()

    def _wait_scope_empty(self, timeout: float = 2.0) -> None:
        if self._job is not None:
            self._job.wait_empty(timeout)
            return
        group_id = self._group_id
        if group_id is None:
            return
        deadline = time.monotonic() + timeout
        while True:
            # SIGKILL delivery alone does not prove exit. Read only PID, status
            # and group membership; zombies cannot keep or mutate stage files.
            alive = False
            for process in psutil.process_iter(attrs=["pid", "status"]):
                try:
                    if os.getpgid(process.pid) == group_id and process.info["status"] != psutil.STATUS_ZOMBIE:
                        alive = True
                        break
                except (ProcessLookupError, psutil.NoSuchProcess):
                    continue
            if not alive:
                self._group_id = None
                self._birth = None
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Owned download children did not settle")
            time.sleep(0.02)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._started:
                self.terminate()
                self._process.join(timeout=1.0)
                self.kill()
                self._process.join(timeout=1.0)
                if self._process.is_alive():
                    raise RuntimeError("Owned download worker did not settle")
                self._exitcode = self._process.exitcode
                self._wait_scope_empty()
            if self._job is not None:
                self._job.close()
                self._job = None
            self._control.close()
            self._child_control.close()
            self._process.close()
            self._closed = True
