# -*- coding: utf-8 -*-
"""Process locks with equivalent shared/exclusive semantics on both
platforms.
"""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def locked_file_descriptor(
    descriptor: int,
    *,
    exclusive: bool = True,
    blocking: bool = True,
) -> Iterator[None]:
    """Lock an open descriptor, release on every exit, and fail closed."""

    if os.name != "nt":
        import fcntl  # pylint: disable=import-outside-toplevel

        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(descriptor, operation)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        return

    import ctypes  # pylint: disable=import-outside-toplevel
    from ctypes import wintypes  # pylint: disable=import-outside-toplevel
    import msvcrt  # pylint: disable=import-outside-toplevel

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    overlapped = Overlapped()
    # Locking beyond EOF is supported and never changes the file's bytes.
    # Shared readers and blocking writers use the native synchronous contract:
    # https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-lockfileex
    flags = (2 if exclusive else 0) | (0 if blocking else 1)
    if not kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
        error = ctypes.get_last_error()
        if not blocking and error == 33:  # ERROR_LOCK_VIOLATION
            raise BlockingIOError(
                errno.EAGAIN,
                "file authority is already held",
            )
        raise ctypes.WinError(error)
    try:
        yield
    finally:
        if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())
