#!/usr/bin/env python3
"""Verify a packaged app upgrades the protected 1.x customization fixture."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packaged_app_lifecycle_smoke import (
    _app_metadata_for_evidence,
    _assert_desktop_readiness,
    _assert_ready_state,
    _find_app_executable,
    _install_termination_signal_handlers,
    _json_request,
    _state_base_url,
    _state_headers,
    _stop_app,
    _wait_for_state,
    _write_json_file,
)


SCHEMA = "com.lantai.packaged-customization-upgrade-smoke.v1"
PROFILE_ID = "profile_v1_upgrade_fixture"
EXPECTED_VALUES = {
    "appearance.palette": "warm",
    "appearance.composer_size": "spacious",
    "conversation.show_thinking": True,
    "persona.presence": "quiet",
}
ALLOWED_GENERATED_METADATA = {"legacy-migration.json"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _json_bytes(root: Path, *, excluding: set[Path] | None = None) -> dict[str, bytes]:
    excluding = excluding or set()
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*.json")
        if path not in excluding
    }


def _get_json(state: dict[str, Any], endpoint: str, *, timeout: float) -> Any:
    _status, _body, payload = _json_request(
        "GET",
        f"{_state_base_url(state)}{endpoint}",
        headers=_state_headers(state),
        timeout=timeout,
    )
    return payload


def _assert_generated_metadata(
    customization_dir: Path, generated_files: list[str]
) -> None:
    unsupported = sorted(set(generated_files) - ALLOWED_GENERATED_METADATA)
    if unsupported:
        raise AssertionError(f"Unexpected customization metadata: {unsupported!r}")
    if "legacy-migration.json" in generated_files:
        marker = json.loads(
            (customization_dir / "legacy-migration.json").read_text(encoding="utf-8")
        )
        if (
            not isinstance(marker, dict)
            or marker.get("completed") is not True
            or marker.get("profile_id") is not None
            or marker.get("legacy_css_archive") is not None
        ):
            raise AssertionError(f"Unsafe legacy migration marker: {marker!r}")


def _wait_for_boot_acceptance(
    state: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = _get_json(state, "/api/customization/state", timeout=timeout)
        if isinstance(payload, dict):
            latest = payload
            if (
                payload.get("pending_boot_validation") == {}
                and payload.get("last_known_good", {}).get(PROFILE_ID) == 3
            ):
                return payload
        time.sleep(0.1)
    raise TimeoutError(f"Packaged customization boot was not accepted: {latest!r}")


def _run(args: argparse.Namespace) -> None:
    app_path = Path(args.app).expanduser().resolve()
    fixture = Path(args.fixture).expanduser().resolve()
    executable = _find_app_executable(app_path)
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-customization-upgrade-"))
    user_data_dir = smoke_root / "user-data"
    customization_dir = user_data_dir / "working" / "customization"
    state_path = smoke_root / "desktop-lifecycle-state.json"
    app_log_path = smoke_root / "app.log"
    evidence_path = Path(args.evidence_output).expanduser().resolve()
    started_at = _utc_now_iso()
    proc: subprocess.Popen[str] | None = None
    restore_handlers = _install_termination_signal_handlers()
    failure: str | None = None
    summary: dict[str, Any] = {}

    shutil.copytree(fixture, customization_dir)
    fixture_state_path = customization_dir / "state.json"
    immutable_before = _json_bytes(
        customization_dir,
        excluding={fixture_state_path},
    )
    state_before = json.loads(fixture_state_path.read_text(encoding="utf-8"))
    env = os.environ.copy()
    env.update(
        {
            "LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE": str(state_path),
            "LANTAI_DESKTOP_USER_DATA_DIR": str(user_data_dir),
            "LANTAI_DESKTOP_HEADLESS_SMOKE": "1",
            "LANTAI_AUTH_ENABLED": "0",
            "LANTAI_DISABLE_GITHUB_UPDATER": "1",
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_REMINDER_DELIVERY_WORKER": "0",
        }
    )

    try:
        with app_log_path.open("w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                [str(executable)],
                cwd=str(app_path.parent),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            desktop_state = _wait_for_state(
                state_path,
                timeout=args.timeout,
                predicate=lambda payload: int(payload.get("sequence") or 0) >= 1,
                process=proc,
                expected_distribution="official",
            )
            _assert_ready_state(desktop_state, expected_distribution="official")
            _assert_desktop_readiness(desktop_state, timeout=args.timeout)
            state_after = _wait_for_boot_acceptance(
                desktop_state,
                timeout=args.timeout,
            )
            effective = _get_json(
                desktop_state,
                "/api/customization/effective-state",
                timeout=args.timeout,
            )
            profiles = _get_json(
                desktop_state,
                "/api/customization/profiles",
                timeout=args.timeout,
            )
            slots = _get_json(
                desktop_state,
                "/api/customization/slots",
                timeout=args.timeout,
            )

            values = effective.get("values") if isinstance(effective, dict) else {}
            observed_values = {key: values.get(key) for key in EXPECTED_VALUES}
            if observed_values != EXPECTED_VALUES:
                raise AssertionError(
                    f"Effective customization changed across upgrade: {observed_values!r}"
                )
            profile = next(
                (
                    item
                    for item in profiles
                    if isinstance(item, dict) and item.get("id") == PROFILE_ID
                ),
                None,
            ) if isinstance(profiles, list) else None
            if not isinstance(profile, dict) or profile.get("revision") != 3:
                raise AssertionError(f"Upgraded Profile identity/revision changed: {profile!r}")
            if (
                not isinstance(slots, list)
                or len(slots) != 5
                or slots[0].get("profile_id") != PROFILE_ID
                or any(slot.get("profile_id") is not None for slot in slots[1:])
            ):
                raise AssertionError(f"Upgraded customization slots changed: {slots!r}")
            if state_after.get("active_profile_id") != PROFILE_ID:
                raise AssertionError(f"Active Profile pointer changed: {state_after!r}")
            if state_after.get("mode") != "normal":
                raise AssertionError(f"Customization mode changed: {state_after!r}")

            immutable_after = _json_bytes(
                customization_dir,
                excluding={fixture_state_path},
            )
            generated_metadata_files = sorted(
                set(immutable_after) - set(immutable_before)
            )
            _assert_generated_metadata(
                customization_dir, generated_metadata_files
            )
            unexpected_files = sorted(
                set(generated_metadata_files) - ALLOWED_GENERATED_METADATA
            )
            changed_files = sorted(
                key
                for key, before in immutable_before.items()
                if immutable_after.get(key) != before
            )
            if unexpected_files or changed_files:
                raise AssertionError(
                    "Packaged upgrade rewrote protected Profile/revision/slot files: "
                    f"unexpected={unexpected_files!r} changed={changed_files!r}"
                )
            summary = {
                "profile_id": PROFILE_ID,
                "profile_revision": profile.get("revision"),
                "slot_count": len(slots),
                "active_profile_id_before": state_before.get("active_profile_id"),
                "active_profile_id_after": state_after.get("active_profile_id"),
                "last_known_good_before": state_before.get("last_known_good"),
                "last_known_good_after": state_after.get("last_known_good"),
                "pending_boot_validation_before": state_before.get(
                    "pending_boot_validation"
                ),
                "pending_boot_validation_after": state_after.get(
                    "pending_boot_validation"
                ),
                "effective_values": observed_values,
                "immutable_json_files": sorted(immutable_before),
                "generated_metadata_files": generated_metadata_files,
                "unexpected_files": unexpected_files,
                "changed_files": changed_files,
            }
    except Exception as exc:  # noqa: BLE001 - preserve exact gate failure
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _stop_app(proc)
        _write_json_file(
            evidence_path,
            {
                "schema": SCHEMA,
                "passed": failure is None,
                "started_at": started_at,
                "completed_at": _utc_now_iso(),
                "failure": failure,
                "app": _app_metadata_for_evidence(app_path),
                "fixture": str(fixture),
                "summary": summary,
            },
        )
        restore_handlers()
        if args.keep_workdir:
            print(f"Customization upgrade workdir: {smoke_root}")
        else:
            shutil.rmtree(smoke_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument(
        "--fixture",
        default=str(
            Path(__file__).resolve().parents[1]
            / "tests"
            / "fixtures"
            / "customization_v1"
        ),
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args(argv)
    try:
        _run(args)
    except Exception as exc:  # noqa: BLE001 - CLI diagnostics
        print(f"Packaged customization upgrade smoke failed: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
