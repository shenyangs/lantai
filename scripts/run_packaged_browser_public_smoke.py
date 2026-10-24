#!/usr/bin/env python3
"""Exercise a frozen Lantai app's Browser Workbench against a public URL."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPERS_PATH = PROJECT_ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"


def load_helpers() -> Any:
    spec = importlib.util.spec_from_file_location("packaged_browser_public_helpers", HELPERS_PATH)
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


def system_proxy_summary() -> dict[str, Any]:
    result = subprocess.run(
        ["/usr/sbin/scutil", "--proxy"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    allowed = {
        "HTTPEnable",
        "HTTPProxy",
        "HTTPPort",
        "HTTPSEnable",
        "HTTPSProxy",
        "HTTPSPort",
        "SOCKSEnable",
        "SOCKSProxy",
        "SOCKSPort",
        "ProxyAutoConfigEnable",
        "ProxyAutoConfigURLString",
        "ProxyAutoDiscoveryEnable",
    }
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key in allowed:
            values[key] = value
    return {"exit_code": result.returncode, "values": values}


def api_action(helpers: Any, state: dict[str, Any], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    status, body, parsed = helpers._json_request(
        "POST",
        f"{helpers._state_base_url(state)}/api/browser-workbench/actions",
        payload=payload,
        headers=helpers._state_headers(state),
        timeout=timeout,
    )
    if status != 200 or not isinstance(parsed, dict):
        raise AssertionError(f"Browser action failed: status={status} body={body!r}")
    if parsed.get("ok") is not True:
        raise AssertionError(f"Browser action returned a failure: {parsed!r}")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--output", required=True)
    parser.add_argument("--screenshot", required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    helpers = load_helpers()
    app_path = Path(args.app).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    screenshot_path = Path(args.screenshot).expanduser().resolve()
    executable = helpers._find_app_executable(app_path)
    app_metadata = helpers._app_metadata_for_evidence(app_path)
    parsed_url = __import__("urllib.parse").parse.urlparse(args.url)
    if parsed_url.scheme != "https" or not parsed_url.hostname:
        raise ValueError("Public Browser smoke requires an HTTPS URL with a hostname")

    dns_started = time.monotonic()
    address_records = socket.getaddrinfo(parsed_url.hostname, 443, type=socket.SOCK_STREAM)
    dns_elapsed_ms = round((time.monotonic() - dns_started) * 1000)
    dns_addresses = sorted({record[4][0] for record in address_records})
    if not dns_addresses:
        raise AssertionError("System DNS returned no public URL addresses")

    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-packaged-browser-public-"))
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

            profile_id = "public-url-smoke"
            status, body, profile = helpers._json_request(
                "POST",
                f"{helpers._state_base_url(state)}/api/browser-workbench/profiles",
                payload={
                    "id": profile_id,
                    "name": "Public URL smoke",
                    "description": "Isolated release evidence profile",
                    "allowed_domains": [parsed_url.hostname],
                    "agent_control_enabled": True,
                    "risk_policy": "write",
                    "metadata": {"release_evidence": True},
                },
                headers=helpers._state_headers(state),
                timeout=args.timeout,
            )
            if status != 200 or not isinstance(profile, dict):
                raise AssertionError(f"Could not create Browser profile: {body!r}")

            opened = api_action(
                helpers,
                state,
                {
                    "action": "open_workbench",
                    "profile_id": profile_id,
                    "url": args.url,
                    "access": "read",
                    "reason": "Open public URL for frozen package release evidence.",
                },
                args.timeout,
            )
            page_id = str(opened.get("data", {}).get("active_tab_id") or "")
            if not page_id:
                tabs = opened.get("data", {}).get("tabs", [])
                if isinstance(tabs, list) and tabs:
                    page_id = str(tabs[-1].get("id") or "")
            if not page_id:
                raise AssertionError(f"Open action exposed no page id: {opened!r}")

            snapshot = api_action(
                helpers,
                state,
                {
                    "action": "snapshot",
                    "profile_id": profile_id,
                    "page_id": page_id,
                    "access": "read",
                    "reason": "Read public page DOM for release evidence.",
                },
                args.timeout,
            )
            snapshot_data = snapshot.get("data", {}).get("data", {})
            visible_text = str(snapshot_data.get("text") or "")
            final_url = str(snapshot.get("url") or snapshot_data.get("url") or "")
            if "Example Domain" not in visible_text:
                raise AssertionError(f"Public page text was not visible: {snapshot!r}")
            if __import__("urllib.parse").parse.urlparse(final_url).hostname != parsed_url.hostname:
                raise AssertionError(f"Unexpected final URL: {final_url!r}")

            screenshot = api_action(
                helpers,
                state,
                {
                    "action": "screenshot",
                    "profile_id": profile_id,
                    "page_id": page_id,
                    "access": "read",
                    "reason": "Capture public page release evidence.",
                },
                args.timeout,
            )
            source_screenshot = Path(str(screenshot.get("screenshot_path") or ""))
            if not source_screenshot.is_file():
                raise AssertionError(f"Packaged Browser screenshot is missing: {screenshot!r}")
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_screenshot, screenshot_path)

            payload = {
                "schema_version": "com.lantai.packaged-browser-public-url-smoke.v1",
                "ok": True,
                "started_at": started_at,
                "completed_at": utc_now(),
                "candidate": app_metadata,
                "target_url": args.url,
                "final_url": final_url,
                "final_title": snapshot_data.get("title", ""),
                "visible_text_contains_example_domain": "Example Domain" in visible_text,
                "system_dns": {
                    "hostname": parsed_url.hostname,
                    "elapsed_ms": dns_elapsed_ms,
                    "addresses": dns_addresses,
                },
                "system_proxy": system_proxy_summary(),
                "desktop_instance_nonce": state.get("desktop_instance_nonce"),
                "backend_pid": state.get("backend_pid"),
                "browser_runtime": snapshot.get("runtime"),
                "page_id": page_id,
                "screenshot_path": str(screenshot_path),
            }
            write_json(output_path, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        helpers._stop_app(process)
        with contextlib.suppress(Exception):
            shutil.rmtree(smoke_root)


if __name__ == "__main__":
    main()
