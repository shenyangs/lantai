#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Open real files in a packaged Lantai app and reject crashes/blank windows."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

try:
    from packaged_office_edit_roundtrip import (
        AppSession,
        _app_metadata,
        _document_binding,
        _launch,
        _sha256,
        _stop_preserving_primary,
    )
    from packaged_office_support import screenshot_visual
except ModuleNotFoundError as import_error:
    if import_error.name not in {
        "packaged_office_edit_roundtrip",
        "packaged_office_support",
    }:
        raise
    from scripts.packaged_office_edit_roundtrip import (
        AppSession,
        _app_metadata,
        _document_binding,
        _launch,
        _sha256,
        _stop_preserving_primary,
    )
    from scripts.packaged_office_support import screenshot_visual


def _windows_for_pid(pid: int) -> list[dict[str, Any]]:
    import Quartz

    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    windows = Quartz.CGWindowListCopyWindowInfo(
        options,
        Quartz.kCGNullWindowID,
    )
    result: list[dict[str, Any]] = []
    for item in windows or ():
        if int(item.get(Quartz.kCGWindowOwnerPID, 0)) != pid:
            continue
        bounds = dict(item.get(Quartz.kCGWindowBounds) or {})
        width = int(bounds.get("Width") or 0)
        height = int(bounds.get("Height") or 0)
        if width < 320 or height < 240:
            continue
        result.append(
            {
                "number": int(item.get(Quartz.kCGWindowNumber, 0)),
                "title": str(item.get(Quartz.kCGWindowName) or ""),
                "layer": int(item.get(Quartz.kCGWindowLayer, -1)),
                "bounds": {
                    "x": int(bounds.get("X") or 0),
                    "y": int(bounds.get("Y") or 0),
                    "width": width,
                    "height": height,
                },
            },
        )
    return sorted(
        result,
        key=lambda value: value["bounds"]["width"] * value["bounds"]["height"],
        reverse=True,
    )


def _capture_renderer(
    session: AppSession,
    target: Path,
) -> tuple[dict[str, float], dict[str, Any]]:
    renderer = _document_binding(
        session.cdp,
        Path(session.renderer_identity["document_path"]),
        session.workbench,
    )
    session.cdp.screenshot(target)
    target.chmod(0o600)
    return screenshot_visual(target), renderer


# Keep the ordered evidence rejection checks explicit and adjacent.
# pylint: disable=too-many-branches,too-many-statements
def _run_one(
    app: Path,
    fixture: Path,
    evidence_dir: Path,
    timeout: float,
) -> dict[str, Any]:
    fixture = fixture.expanduser().resolve(strict=True)
    fixture_sha256 = _sha256(fixture)
    artifact_name = (
        f"{fixture.parent.name}-{fixture.stem}-{fixture.suffix[1:]}"
    )
    log_path = evidence_dir / f"{artifact_name}.log"
    session = _launch(app, fixture, log_path, timeout)
    process = session.process
    result: dict[str, Any]
    try:
        _document_binding(session.cdp, fixture, session.workbench)
        deadline = time.monotonic() + timeout
        windows: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"{fixture.suffix} app exited before its editor appeared: {process.returncode}",
                )
            windows = _windows_for_pid(process.pid)
            # Office uses an embedded WebContentsView inside the main shell.
            if windows:
                break
            time.sleep(0.2)
        if not windows:
            raise AssertionError(
                f"{fixture.suffix} application window did not appear",
            )
        # Preserve the original paint allowance and five capture attempts.
        time.sleep(6)
        if process.poll() is not None:
            raise AssertionError(f"{fixture.suffix} app crashed after opening")
        screenshot = evidence_dir / f"{artifact_name}.png"
        editor = None
        visual = None
        renderer = None
        capture_errors: list[str] = []
        for _attempt in range(5):
            windows = _windows_for_pid(process.pid)
            for candidate in windows:
                bounds = candidate["bounds"]
                if candidate["layer"] != 0:
                    continue
                if (
                    bounds["y"] < 20
                    or bounds["width"] < 640
                    or bounds["height"] < 480
                ):
                    continue
                try:
                    visual, renderer = _capture_renderer(session, screenshot)
                    editor = candidate
                    break
                except RuntimeError as error:
                    capture_errors.append(str(error))
            if editor is not None:
                break
            time.sleep(0.4)
        if editor is None or visual is None:
            raise AssertionError(
                f"{fixture.suffix} editor could not be captured: {capture_errors[-3:]}",
            )
        if visual["luma_range"] < 35 or visual["dark_ratio"] < 0.01:
            raise AssertionError(
                f"{fixture.suffix} editor rendered blank: {visual}",
            )
        # Selection must still be the same document after screenshot capture.
        renderer = _document_binding(session.cdp, fixture, session.workbench)
        result = {
            "fixture": str(fixture),
            "fixture_sha256": fixture_sha256,
            "pid": process.pid,
            "window_count": len(windows),
            "editor": editor,
            "renderer": renderer,
            "visual": visual,
            "screenshot": str(screenshot),
            "screenshot_sha256": _sha256(screenshot),
            "log": str(session.log),
        }
    finally:
        _stop_preserving_primary(session)
    if _sha256(fixture) != fixture_sha256:
        raise AssertionError("Office entry smoke changed its corpus fixture")
    if (
        session.process_settlement.get("ok") is not True
        or session.process_settlement.get("live") != []
        or session.process_settlement.get("unknown") != []
    ):
        raise AssertionError(
            "Office owned process settlement is incomplete or uncertain",
        )
    result["process_settlement"] = session.process_settlement
    return result


# pylint: enable=too-many-branches,too-many-statements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, action="append", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    app = args.app.expanduser().resolve(strict=True)
    fixtures = [
        value.expanduser().resolve(strict=True) for value in args.fixture
    ]
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    before_metadata = _app_metadata(app)
    results = [
        _run_one(app, fixture, args.evidence_dir, args.timeout)
        for fixture in fixtures
    ]
    if _app_metadata(app) != before_metadata:
        raise AssertionError(
            "Office entry smoke app components changed during verification",
        )
    payload = {
        "ok": True,
        "app": str(app),
        "app_metadata": before_metadata,
        "results": results,
    }
    args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.evidence_output.chmod(0o600)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
