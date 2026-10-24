# -*- coding: utf-8 -*-
"""Owner-held Windows Job for a process admitted while still suspended."""
from __future__ import annotations

import ctypes
import time


class WindowsProcessJob:
    def __init__(self, process) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IOCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
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

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        api = self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": (
                [ctypes.c_void_p, wintypes.LPCWSTR],
                wintypes.HANDLE,
            ),
            "SetInformationJobObject": (
                [
                    wintypes.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                ],
                wintypes.BOOL,
            ),
            "QueryInformationJobObject": (
                [
                    wintypes.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            "AssignProcessToJobObject": (
                [wintypes.HANDLE, wintypes.HANDLE],
                wintypes.BOOL,
            ),
            "TerminateJobObject": (
                [wintypes.HANDLE, wintypes.UINT],
                wintypes.BOOL,
            ),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "CreateToolhelp32Snapshot": (
                [wintypes.DWORD, wintypes.DWORD],
                wintypes.HANDLE,
            ),
            "Thread32First": (
                [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)],
                wintypes.BOOL,
            ),
            "Thread32Next": (
                [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)],
                wintypes.BOOL,
            ),
            "OpenThread": (
                [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                wintypes.HANDLE,
            ),
            "GetProcessIdOfThread": ([wintypes.HANDLE], wintypes.DWORD),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        }
        for name, (arguments, result) in signatures.items():
            getattr(api, name).argtypes = arguments
            getattr(api, name).restype = result
        self._accounting_type = Accounting
        self._handle = api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = (
                0x2000  # KILL_ON_JOB_CLOSE
            )
            if not api.SetInformationJobObject(
                self._handle,
                9,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            # Popen's retained process handle pins the exact process identity.
            # The primary thread is suspended until ownership is established.
            if not api.AssignProcessToJobObject(
                self._handle,
                wintypes.HANDLE(int(process._handle)),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self._resume_primary_thread(process, ThreadEntry, api)
        except BaseException:
            self.close()
            raise

    def _resume_primary_thread(self, process, thread_entry_type, api) -> None:
        snapshot = api.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = thread_entry_type()
            entry.dwSize = ctypes.sizeof(entry)
            available = api.Thread32First(snapshot, ctypes.byref(entry))
            resumed = False
            while available:
                if entry.th32OwnerProcessID == process.pid:
                    thread = api.OpenThread(
                        0x0002 | 0x0040,
                        False,
                        entry.th32ThreadID,
                    )
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if api.GetProcessIdOfThread(thread) != process.pid:
                            raise RuntimeError(
                                "Windows primary thread identity changed",
                            )
                        if api.ResumeThread(thread) != 1:
                            raise RuntimeError(
                                "Windows owned process was not suspended once",
                            )
                        resumed = True
                    finally:
                        api.CloseHandle(thread)
                    break
                available = api.Thread32Next(snapshot, ctypes.byref(entry))
            if not resumed:
                raise RuntimeError(
                    "Windows owned process has no primary thread",
                )
        finally:
            api.CloseHandle(snapshot)

    def terminate(self) -> None:
        if not self._api.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait_empty(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            accounting = self._accounting_type()
            if not self._api.QueryInformationJobObject(
                self._handle,
                1,
                ctypes.byref(accounting),
                ctypes.sizeof(accounting),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if accounting.ActiveProcesses == 0:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Windows owned process tree did not settle")
            time.sleep(0.02)

    def close(self) -> None:
        if self._handle:
            if not self._api.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None
