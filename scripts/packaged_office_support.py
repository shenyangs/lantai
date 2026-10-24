# -*- coding: utf-8 -*-
"""Shared artifact identity and owned process settlement for Office smokes."""

from __future__ import annotations

import hashlib
import os
import plistlib
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psutil
from PIL import Image, ImageStat


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def screenshot_visual(path: Path) -> dict[str, float]:
    """Decode captured pixels using the entry smoke's original measurement."""
    try:
        with Image.open(path) as source:
            source.load()
            if source.width < 320 or source.height < 240:
                raise AssertionError("Office screenshot viewport is too small")
            grayscale = source.convert("RGB").resize((240, 160)).convert("L")
            stat = ImageStat.Stat(grayscale)
            extrema = grayscale.getextrema()
            histogram = grayscale.histogram()
    except (OSError, ValueError) as error:
        raise AssertionError(
            "Office screenshot is not a decodable image",
        ) from error
    return {
        "mean_luma": round(float(stat.mean[0]), 2),
        "luma_range": float(extrema[1] - extrema[0]),
        "dark_ratio": round(sum(histogram[:238]) / sum(histogram), 4),
    }


def require_visible_screenshot(path: Path) -> dict[str, float]:
    visual = screenshot_visual(path)
    if visual["luma_range"] < 35 or visual["dark_ratio"] < 0.01:
        raise AssertionError(f"Office renderer has blank pixels: {visual}")
    return visual


def app_metadata(app: Path) -> dict[str, Any]:
    app = app.expanduser().resolve(strict=True)
    with (app / "Contents/Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    executable = app / "Contents/MacOS" / str(info["CFBundleExecutable"])
    artifacts = {
        "main_executable": executable,
        "app_asar": app / "Contents/Resources/app.asar",
        "backend_executable": app
        / "Contents/Resources/lantai-server/Lantai Backend.app/Contents/MacOS/lantai-server",
    }
    hashes = {key: sha256(path) for key, path in artifacts.items()}
    return {
        "path": str(app),
        "bundle_id": str(info.get("CFBundleIdentifier") or ""),
        "version": str(info.get("CFBundleShortVersionString") or ""),
        "build": str(info.get("CFBundleVersion") or ""),
        "executable": str(executable),
        "executable_sha256": hashes["main_executable"],
        "artifact_sha256": hashes,
    }


class OwnedProcessCohort:
    """Track captured births and descendants of an identified session leader."""

    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        if process.poll() is not None:
            raise AssertionError(
                "Office process exited before its identity was captured",
            )
        owner = psutil.Process(process.pid)
        self.born = owner.create_time()
        self.sid = os.getsid(process.pid)
        if process.poll() is not None:
            raise AssertionError(
                "Office process exited during identity capture",
            )
        if self.sid != process.pid:
            raise AssertionError(
                "Office smoke must start in its own process session",
            )
        self._owners = {process.pid: self.born}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._signals: list[dict[str, Any]] = []
        self._monitor_error: dict[str, Any] | None = None
        self._sid_compromised = False
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    @staticmethod
    def _identity(pid: int, born: float) -> tuple[str, psutil.Process | None]:
        try:
            owner = psutil.Process(pid)
            if owner.create_time() != born:
                return "reused", None
            if owner.status() == psutil.STATUS_ZOMBIE:
                return "gone", None
            return "live", owner
        except psutil.NoSuchProcess:
            return "gone", None
        except ProcessLookupError:
            return "gone", None
        except (psutil.AccessDenied, PermissionError, OSError):
            return "unknown", None

    def _sid_is_original(self, unknown: list[dict[str, Any]]) -> bool:
        if getattr(self, "_sid_compromised", False):
            return False
        try:
            if psutil.Process(self.process.pid).create_time() != self.born:
                self._sid_compromised = True
                return False
            return True
        except (psutil.NoSuchProcess, ProcessLookupError):
            return False
        except (psutil.AccessDenied, PermissionError, OSError):
            unknown.append(
                {
                    "pid": self.process.pid,
                    "reason": "SID leader identity unavailable",
                },
            )
            return False

    def _capture_descendants(self, unknown: list[dict[str, Any]]) -> None:
        pending = list(self._owners.items())
        visited: set[tuple[int, float]] = set()
        while pending:
            pid, born = pending.pop()
            if (pid, born) in visited:
                continue
            visited.add((pid, born))
            status, owner = self._identity(pid, born)
            if status != "live" or owner is None:
                continue
            try:
                # psutil's recursive inventory can join edges sampled before
                # and after an intermediate PID was reused. Prove each edge
                # against both current births before traversing another layer.
                children = owner.children(recursive=False)
                for child in children:
                    child_born = child.create_time()
                    child_status, current_child = self._identity(
                        child.pid,
                        child_born,
                    )
                    # A confirmed exit has no live identity left to adopt.
                    # Birth mismatch and unreadable identity remain distinct.
                    if child_status == "gone":
                        continue
                    # An already captured birth keeps its authority after
                    # detaching; this edge does not grant new ownership.
                    if (
                        self._owners.get(child.pid) == child_born
                        and child_status == "live"
                    ):
                        pending.append((child.pid, child_born))
                        continue
                    parent_status, _ = self._identity(pid, born)
                    if (
                        child_born < born
                        or child_status != "live"
                        or current_child is None
                        or current_child.ppid() != pid
                        or parent_status != "live"
                    ):
                        unknown.append(
                            {
                                "pid": child.pid,
                                "birth": child_born,
                                "reason": "descendant ancestry changed during capture",
                            },
                        )
                        continue
                    # ppid() itself is another observation point. Recheck
                    # births after reading it, using fresh Process objects.
                    child_status, _ = self._identity(child.pid, child_born)
                    parent_status, _ = self._identity(pid, born)
                    if child_status == "gone":
                        continue
                    if child_status != "live" or parent_status != "live":
                        unknown.append(
                            {
                                "pid": child.pid,
                                "birth": child_born,
                                "reason": "descendant birth changed during capture",
                            },
                        )
                        continue
                    if (
                        child.pid in self._owners
                        and self._owners[child.pid] != child_born
                    ):
                        unknown.append(
                            {
                                "pid": child.pid,
                                "birth": child_born,
                                "reason": "owned descendant PID was reused",
                            },
                        )
                        continue
                    self._owners.setdefault(child.pid, child_born)
                    pending.append((child.pid, child_born))
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue
            except (psutil.AccessDenied, PermissionError, OSError):
                unknown.append(
                    {
                        "pid": pid,
                        "birth": born,
                        "reason": "descendant inventory unavailable",
                    },
                )

    # Keep the ordered evidence rejection checks explicit and adjacent.
    # pylint: disable=too-many-branches
    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            unknown: list[dict[str, Any]] = []
            # A bare SID cannot authorize new members after its leader exits:
            # the PID may have been reused and exited between two samples.
            may_adopt_sid = self._sid_is_original(unknown)
            unproven_sid: list[dict[str, Any]] = []
            try:
                pids = psutil.pids()
            except (psutil.AccessDenied, PermissionError, OSError):
                pids = []
                unknown.append({"reason": "SID inventory unavailable"})
            for pid in pids:
                try:
                    if os.getsid(pid) != self.sid:
                        continue
                    owner = psutil.Process(pid)
                    born = owner.create_time()
                    if (
                        born < self.born
                        or owner.status() == psutil.STATUS_ZOMBIE
                    ):
                        continue
                    if self._owners.get(pid) == born:
                        continue
                    if pid in self._owners:
                        unknown.append(
                            {
                                "pid": pid,
                                "birth": born,
                                "reason": "owned SID member PID was reused",
                            },
                        )
                        continue
                    if may_adopt_sid and self._sid_is_original(unknown):
                        self._owners.setdefault(pid, born)
                    elif not getattr(self, "_sid_compromised", False):
                        unproven_sid.append(
                            {
                                "pid": pid,
                                "birth": born,
                                "reason": "SID member has no original leader or captured ancestry",
                            },
                        )
                except (ProcessLookupError, psutil.NoSuchProcess):
                    continue
                except (psutil.AccessDenied, PermissionError, OSError):
                    unknown.append(
                        {
                            "pid": pid,
                            "reason": "SID inventory identity unavailable",
                        },
                    )
            # Previously captured descendants remain owned after detaching.
            self._capture_descendants(unknown)
            unknown.extend(
                item
                for item in unproven_sid
                if self._owners.get(item["pid"]) != item["birth"]
            )
            live: list[dict[str, Any]] = []
            for pid, born in self._owners.items():
                status, _ = self._identity(pid, born)
                identity = {"pid": pid, "birth": born}
                if status == "live":
                    live.append(identity)
                elif status == "unknown":
                    unknown.append(
                        {**identity, "reason": "owned identity unavailable"},
                    )
                elif status == "reused":
                    unknown.append(
                        {**identity, "reason": "owned PID was reused"},
                    )
            if unknown:
                self._monitor_error = {
                    "reason": "ownership snapshot observed an inventory gap",
                }
            return {"live": live, "unknown": unknown}

    # pylint: enable=too-many-branches

    def _monitor(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = self._snapshot()
                if snapshot["unknown"]:
                    self._monitor_error = {
                        "reason": "ownership monitor observed an inventory gap",
                    }
            except BaseException as exc:
                self._monitor_error = {
                    "reason": "ownership monitor failed",
                    "error_type": type(exc).__name__,
                }
                return
            self._stop.wait(0.1)

    def _signal_live(
        self,
        snapshot: dict[str, Any],
        sig: signal.Signals,
    ) -> None:
        for identity in snapshot["live"]:
            pid, born = identity["pid"], identity["birth"]
            status, owner = self._identity(pid, born)
            if status != "live" or owner is None:
                continue
            try:
                owner.send_signal(sig)
                self._signals.append({**identity, "signal": int(sig)})
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, PermissionError, OSError):
                # The final snapshot must prove disappearance. A denied
                # signal is never treated as successful cleanup.
                continue

    def settle(
        self,
        *,
        term_timeout: float = 8,
        kill_timeout: float = 5,
    ) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=2)
        for sig, timeout in (
            (signal.SIGTERM, term_timeout),
            (signal.SIGKILL, kill_timeout),
        ):
            deadline = time.monotonic() + timeout
            sent: set[tuple[int, float]] = set()
            while True:
                self.process.poll()
                snapshot = self._snapshot()
                if snapshot["unknown"]:
                    self._monitor_error = {
                        "reason": "ownership monitor or settlement observed an inventory gap",
                    }
                if not snapshot["live"]:
                    break
                pending = [
                    item
                    for item in snapshot["live"]
                    if (item["pid"], item["birth"]) not in sent
                ]
                self._signal_live({"live": pending}, sig)
                sent.update((item["pid"], item["birth"]) for item in pending)
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        self.process.poll()
        snapshot = self._snapshot()
        if self._thread.is_alive():
            snapshot["unknown"].append(
                {"reason": "ownership monitor did not stop"},
            )
        if self._monitor_error is not None:
            snapshot["unknown"].append(self._monitor_error)
        return {
            "ok": not snapshot["live"] and not snapshot["unknown"],
            **snapshot,
            "sid": self.sid,
            "root": {"pid": self.process.pid, "birth": self.born},
            "owned": [
                {"pid": pid, "birth": born}
                for pid, born in sorted(self._owners.items())
            ],
            "signals": list(self._signals),
        }
