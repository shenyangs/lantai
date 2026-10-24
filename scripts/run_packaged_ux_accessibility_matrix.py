#!/usr/bin/env python3
"""Capture and verify the packaged Lantai companion UX/accessibility matrix."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPERS_PATH = PROJECT_ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
CAPTURE_SCRIPT = PROJECT_ROOT / "console" / "scripts" / "capture-ui-review-chrome.mjs"


def load_helpers() -> Any:
    spec = importlib.util.spec_from_file_location("packaged_ux_matrix_helpers", HELPERS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load packaged smoke helpers: {HELPERS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    helpers = load_helpers()
    app_path = Path(args.app).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    evidence_path = Path(args.evidence_output).expanduser().resolve()
    executable = helpers._find_app_executable(app_path)
    candidate = helpers._app_metadata_for_evidence(app_path)
    label = "official-packaged-companion-release"
    expected_manifest = output_dir / f"{label}-manifest.json"
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-packaged-ux-matrix-"))
    state_path = smoke_root / "desktop-lifecycle-state.json"
    user_data_dir = smoke_root / "user-data"
    app_log_path = smoke_root / "app.log"
    env = os.environ.copy()
    for key in (
        "LANTAI_WORKING_DIR",
        "LANTAI_SECRET_DIR",
        "LANTAI_DESKTOP_USER_DATA_DIR",
        "LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE",
    ):
        env.pop(key, None)
    env.update(
        {
            "LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE": str(state_path),
            "LANTAI_DESKTOP_USER_DATA_DIR": str(user_data_dir),
            "LANTAI_WORKING_DIR": str(user_data_dir / "working"),
            "LANTAI_SECRET_DIR": str(user_data_dir / "working.secret"),
            "LANTAI_AUTH_ENABLED": "0",
            "LANTAI_DISABLE_GITHUB_UPDATER": "1",
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_REMINDER_DELIVERY_WORKER": "0",
        }
    )

    started_at = utc_now()
    process: subprocess.Popen[str] | None = None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with app_log_path.open("w", encoding="utf-8") as app_log:
            process = subprocess.Popen(
                [str(executable)],
                cwd=str(app_path.parent),
                env=env,
                stdout=app_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            state = helpers._wait_for_state(
                state_path,
                timeout=args.timeout,
                predicate=lambda value: int(value.get("sequence") or 0) >= 1,
                process=process,
                expected_distribution="official",
            )
            helpers._assert_ready_state(state, expected_distribution="official")
            helpers._assert_desktop_readiness(state, timeout=args.timeout)

            capture_env = os.environ.copy()
            capture_env["LANTAI_UI_REVIEW_DESKTOP_TOKEN"] = helpers._state_headers(state)[
                helpers.DESKTOP_TOKEN_HEADER
            ]
            result = subprocess.run(
                [
                    "node",
                    str(CAPTURE_SCRIPT),
                    helpers._state_base_url(state),
                    str(output_dir),
                    label,
                    "companion-release",
                ],
                cwd=str(PROJECT_ROOT / "console"),
                env=capture_env,
                text=True,
                capture_output=True,
                timeout=max(args.timeout, 180),
                check=False,
            )
            if result.returncode != 0:
                raise AssertionError(
                    "Packaged UX matrix capture failed:\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
            manifest = json.loads(expected_manifest.read_text(encoding="utf-8"))
            scenarios = manifest.get("scenarios")
            if not isinstance(scenarios, list) or len(scenarios) != 12:
                raise AssertionError(f"Expected 12 packaged UX scenarios, got {scenarios!r}")

            def count(predicate: Any) -> int:
                return sum(1 for scenario in scenarios if predicate(scenario))

            maximum_overflow = max(
                float(scenario.get("pageState", {}).get("horizontalOverflow") or 0)
                for scenario in scenarios
            )
            unlabeled_count = sum(
                int(scenario.get("pageState", {}).get("unlabeledInteractiveCount") or 0)
                for scenario in scenarios
            )
            keyboard_failures = sum(
                1 for scenario in scenarios if scenario.get("keyboardAudit", {}).get("passed") is not True
            )
            console_error_count = sum(len(scenario.get("consoleErrors") or []) for scenario in scenarios)
            requirements = {
                "light_theme_scenarios": count(lambda item: item.get("theme") == "light"),
                "dark_theme_scenarios": count(lambda item: item.get("theme") == "dark"),
                "narrow_window_scenarios": count(lambda item: int(item.get("width") or 0) <= 900),
                "large_text_scenarios": count(lambda item: float(item.get("fontScale") or 1) >= 1.25),
                "reduced_motion_scenarios": count(lambda item: item.get("motion") == "reduce"),
                "increased_contrast_scenarios": count(lambda item: item.get("contrast") == "more"),
                "keyboard_audited_scenarios": count(
                    lambda item: isinstance(item.get("keyboardAudit"), dict)
                    and item.get("keyboardAudit", {}).get("passed") is True
                ),
            }
            missing = [name for name, value in requirements.items() if value < 1]
            if missing:
                raise AssertionError(f"Packaged UX matrix missed required scenario classes: {missing}")
            if maximum_overflow > 1 or unlabeled_count or keyboard_failures or console_error_count:
                raise AssertionError(
                    "Packaged UX matrix did not meet release thresholds: "
                    f"overflow={maximum_overflow} unlabeled={unlabeled_count} "
                    f"keyboard_failures={keyboard_failures} console_errors={console_error_count}"
                )

            evidence = {
                "schema_version": "com.lantai.packaged-ux-accessibility-matrix.v1",
                "passed": True,
                "started_at": started_at,
                "completed_at": utc_now(),
                "candidate": candidate,
                "manifest_path": str(expected_manifest),
                "manifest_sha256": helpers._sha256_file(expected_manifest),
                "scenario_count": len(scenarios),
                "route_ids": sorted({str(item.get("route", {}).get("id") or "") for item in scenarios}),
                "requirements": requirements,
                "maximum_horizontal_overflow_px": maximum_overflow,
                "unlabeled_interactive_count": unlabeled_count,
                "keyboard_failure_count": keyboard_failures,
                "console_error_count": console_error_count,
                "screenshot_count": sum(
                    1 for item in scenarios if Path(str(item.get("file") or "")).is_file()
                ),
            }
            write_json(evidence_path, evidence)
            print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        helpers._stop_app(process)
        with contextlib.suppress(Exception):
            shutil.rmtree(smoke_root)


if __name__ == "__main__":
    main()
