#!/usr/bin/env python3
"""Close steering UI L4 gaps against one exact artifact-only candidate.

The packaged application runs with a disposable user-data directory and a
local OpenAI-compatible fixture.  The probe injects only renderer-side control
submission failures; primary turns still stream through the packaged runtime.
It never opens or modifies the daily application profile.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPERS_PATH = PROJECT_ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
EVIDENCE_SCHEMA = "com.lantai.packaged-steering-ui-gap-smoke.v1"


def _load_helpers() -> Any:
    spec = importlib.util.spec_from_file_location(
        "packaged_steering_ui_gap_helpers",
        HELPERS_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("packaged lifecycle smoke helpers are unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _release_provenance(app_path: Path) -> dict[str, Any]:
    path = (
        app_path
        / "Contents"
        / "Resources"
        / "console"
        / "dist"
        / "release-provenance.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("release provenance must be a JSON object")
    return {
        key: payload.get(key)
        for key in (
            "schemaVersion",
            "version",
            "gitSha",
            "sourceBranch",
            "canonicalMain",
        )
    }


def _actions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    actions = payload.get("guidance", {}).get("actions", [])
    return [item for item in actions if isinstance(item, dict)]


def _enabled_labels(payload: dict[str, Any]) -> list[str]:
    return [
        str(item.get("label") or "")
        for item in _actions(payload)
        if item.get("disabled") is False
    ]


def _status_matches_server(ui_status: str, server_status: str) -> bool:
    normalized = str(server_status or "").strip().lower()
    if not normalized:
        return False
    expected = {
        "pending": "admitted",
        "admitted": "admitted",
        "claimed": "claimed",
        "bound": "bound",
        "applied": "applied",
        "steered": "steered",
        "queued": "queued",
        "cancelled": "cancelled",
        "superseded": "superseded",
        "failed": "failed",
    }.get(normalized, normalized)
    return ui_status == expected


def _build_evidence(
    *,
    app_metadata: dict[str, Any],
    provenance: dict[str, Any],
    renderer: dict[str, Any],
    provider_request_count: int,
    expected_git_sha: str,
) -> dict[str, Any]:
    deterministic = renderer.get("deterministic")
    deterministic = deterministic if isinstance(deterministic, dict) else {}
    unknown = renderer.get("unknown")
    unknown = unknown if isinstance(unknown, dict) else {}
    restore = renderer.get("restore")
    restore = restore if isinstance(restore, dict) else {}
    accessibility = renderer.get("accessibility")
    accessibility = accessibility if isinstance(accessibility, dict) else {}
    visuals = renderer.get("visuals")
    visuals = visuals if isinstance(visuals, dict) else {}
    restore_before = restore.get("before")
    restore_before = restore_before if isinstance(restore_before, dict) else {}
    restore_after = restore.get("after")
    restore_after = restore_after if isinstance(restore_after, dict) else {}
    restored_dom = restore.get("restored")
    restored_dom = restored_dom if isinstance(restored_dom, dict) else {}
    visual_modes = [
        item
        for item in visuals.get("modes", [])
        if isinstance(item, dict)
    ]
    visual_by_theme = {
        str(item.get("theme") or ""): item for item in visual_modes
    }
    light = visual_by_theme.get("light", {})
    dark = visual_by_theme.get("dark", {})
    narrow = visuals.get("narrow")
    narrow = narrow if isinstance(narrow, dict) else {}
    deterministic_actions = _actions(deterministic)
    unknown_actions = _actions(unknown)
    unknown_enabled = _enabled_labels(unknown)
    ax_nodes = [
        item
        for item in accessibility.get("ax_nodes", [])
        if isinstance(item, dict)
    ]
    ax_names = [str(item.get("name") or "") for item in ax_nodes]

    checks = {
        "exact_artifact_only_candidate": (
            provenance.get("canonicalMain") is False
            and provenance.get("gitSha") == expected_git_sha
        ),
        "isolated_local_fixture_without_paid_requests": (
            provider_request_count >= 3
        ),
        "http_400_preserves_primary_run": (
            deterministic.get("primary_active_before_control") is True
            and deterministic.get("primary_active_after_control") is True
            and deterministic.get("stop_label_after_control") == "停止当前任务"
            and deterministic.get("guidance", {}).get("status") == "failed"
            and "输入无法接收 · 请编辑后重试"
            in str(deterministic.get("guidance", {}).get("text") or "")
        ),
        "network_uncertainty_preserves_primary_run": (
            unknown.get("primary_active_before_control") is True
            and unknown.get("primary_active_after_control") is True
            and unknown.get("guidance", {}).get("status") == "uncertain"
            and "接收待确认 · 可安全重试"
            in str(unknown.get("guidance", {}).get("text") or "")
        ),
        "deterministic_failure_offers_all_recovery_actions": (
            len(deterministic_actions) == 5
            and all(item.get("disabled") is False for item in deterministic_actions)
            and any(item.get("text") == "重试调整方向" for item in deterministic_actions)
        ),
        "unknown_failure_only_retries_original_intent": (
            len(unknown_actions) == 5
            and len(unknown_enabled) == 1
            and unknown_enabled[0].startswith("调整方向：")
            and any(item.get("text") == "重试调整方向" for item in unknown_actions)
        ),
        "failure_classes_have_distinct_copy_and_actions": (
            deterministic.get("guidance", {}).get("text")
            != unknown.get("guidance", {}).get("text")
            and _enabled_labels(deterministic) != unknown_enabled
        ),
        "return_reconciles_with_server": (
            int(restore.get("status_requests_after_return") or 0) > 0
            and _status_matches_server(
                str(restored_dom.get("queue_status") or ""),
                str(restore.get("latest_server_status") or ""),
            )
        ),
        "return_preserves_logical_command_and_time": (
            bool(restore_before.get("command_id"))
            and restore_before.get("command_id") == restore_after.get("command_id")
            and restore_before.get("timestamp") is not None
            and restore_before.get("timestamp") == restore_after.get("timestamp")
        ),
        "guidance_has_complete_accessible_semantics": (
            accessibility.get("status_role") == "status"
            and accessibility.get("status_live") == "polite"
            and str(accessibility.get("article_label") or "").startswith("引导 ")
            and str(accessibility.get("group_label") or "").startswith("引导操作：")
            and len(accessibility.get("button_labels") or []) == 5
            and all(accessibility.get("button_labels") or [])
            and len(ax_names) >= 5
        ),
        "keyboard_focus_advances_between_actions": (
            bool(accessibility.get("focus_before"))
            and bool(accessibility.get("focus_after"))
            and accessibility.get("focus_before") != accessibility.get("focus_after")
        ),
        "light_and_dark_keep_status_visible": (
            bool(light)
            and bool(dark)
            and "dark-mode" not in str(light.get("html_class") or "")
            and "dark-mode" in str(dark.get("html_class") or "")
            and bool(light.get("status_text"))
            and light.get("status_text") == dark.get("status_text")
            and light.get("screenshot_sha256") != dark.get("screenshot_sha256")
        ),
        "narrow_layout_has_no_horizontal_overflow": (
            narrow.get("no_horizontal_overflow") is True
            and narrow.get("card_in_viewport") is True
            and bool(narrow.get("status_text"))
        ),
    }
    return {
        "schema": EVIDENCE_SCHEMA,
        "ok": all(checks.values()),
        "distribution": "official-artifact-only-candidate",
        "app": {
            key: app_metadata.get(key)
            for key in (
                "bundle_identifier",
                "short_version",
                "bundle_version",
                "executable",
                "artifact_sha256",
            )
            if key in app_metadata
        },
        "provenance": provenance,
        "expected_git_sha": expected_git_sha,
        "provider_fixture": {
            "kind": "isolated-local-openai-compatible",
            "request_count": provider_request_count,
            "paid_requests": 0,
        },
        "checks": checks,
        "renderer": renderer,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _run_renderer(
    helpers: Any,
    state: dict[str, Any],
    seeded: dict[str, str],
    *,
    electron_binary: Path | None,
    timeout: float,
) -> dict[str, Any]:
    script = PROJECT_ROOT / "console" / "electron" / "chatSteeringControlGapSmoke.cjs"
    suffix = uuid.uuid4().hex[:8]
    renderer_binary = (
        electron_binary.expanduser().resolve()
        if electron_binary is not None
        else helpers._console_electron_binary(PROJECT_ROOT)
    )
    if not renderer_binary.is_file():
        raise FileNotFoundError(f"Electron renderer binary not found: {renderer_binary}")
    command = [
        str(renderer_binary),
        str(script),
        "--base-url",
        helpers._state_base_url(state),
        "--desktop-token",
        helpers._state_headers(state)[helpers.DESKTOP_TOKEN_HEADER],
        "--timeout-ms",
        str(max(1, int(timeout * 1000))),
        "--error-chat-id",
        seeded["chat_id"],
        "--error-prompt",
        seeded["prompt_text"],
        "--error-control",
        f"PACKAGED_CONTROL_400_{suffix}",
        "--network-chat-id",
        seeded["stop_chat_id"],
        "--network-prompt",
        seeded["stop_prompt_text"],
        "--network-control",
        f"PACKAGED_CONTROL_NETWORK_{suffix}",
        "--restore-chat-id",
        seeded["switch_chat_id"],
        "--restore-prompt",
        seeded["switch_prompt_text"],
        "--restore-control",
        f"PACKAGED_CONTROL_RESTORE_{suffix}",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT / "console"),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=timeout + 30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "steering UI gap renderer timed out: "
            f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
        ) from exc
    if result.returncode != 0:
        raise AssertionError(
            "steering UI gap renderer failed: "
            f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    payload = helpers._extract_json_object(result.stdout)
    if payload.get("ok") is not True:
        raise AssertionError(f"unexpected renderer payload: {payload!r}")
    return payload


def _run(args: argparse.Namespace) -> dict[str, Any]:
    helpers = _load_helpers()
    app_path = Path(args.app).expanduser().resolve()
    executable = helpers._find_app_executable(app_path)
    app_metadata = helpers._app_metadata_for_evidence(app_path)
    provenance = _release_provenance(app_path)
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-steering-ui-gap-smoke-"))
    state_path = smoke_root / "desktop-state.json"
    user_data_dir = smoke_root / "user-data"
    app_log_path = smoke_root / "app.log"
    env = os.environ.copy()
    env.update(
        {
            "LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE": str(state_path),
            "LANTAI_DESKTOP_USER_DATA_DIR": str(user_data_dir),
            "LANTAI_AUTH_ENABLED": "0",
            "LANTAI_DISABLE_GITHUB_UPDATER": "1",
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_REMINDER_DELIVERY_WORKER": "0",
            "LANTAI_DESKTOP_HEADLESS_SMOKE": "1",
        }
    )
    proc: subprocess.Popen[str] | None = None
    try:
        with helpers._LocalOpenAISmokeServer() as provider:
            helpers._write_smoke_openai_provider_config(
                user_data_dir,
                base_url=provider.base_url,
            )
            with app_log_path.open("w", encoding="utf-8") as log_file:
                proc = subprocess.Popen(
                    [str(executable)],
                    cwd=str(app_path.parent),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                state = helpers._wait_for_state(
                    state_path,
                    timeout=args.timeout,
                    predicate=lambda payload: int(payload.get("sequence") or 0) >= 1,
                    process=proc,
                    expected_distribution=args.distribution,
                )
                helpers._assert_ready_state(
                    state,
                    expected_distribution=args.distribution,
                )
                helpers._assert_desktop_readiness(state, timeout=args.timeout)
                helpers._exercise_console_model_success_path(
                    state,
                    timeout=args.timeout,
                )
                workspace_dir = user_data_dir / "working" / "workspaces" / "default"
                seeded = helpers._seed_chat_provider_stream_ui(
                    state,
                    workspace_dir=workspace_dir,
                    timeout=args.timeout,
                )
                renderer = _run_renderer(
                    helpers,
                    state,
                    seeded,
                    electron_binary=args.electron_binary,
                    timeout=max(args.timeout, 90.0),
                )
                evidence = _build_evidence(
                    app_metadata=app_metadata,
                    provenance=provenance,
                    renderer=renderer,
                    provider_request_count=provider.request_count,
                    expected_git_sha=args.expected_git_sha,
                )
                return evidence
    except Exception as exc:
        log_tail = helpers._tail_text(app_log_path, limit=6000)
        if log_tail:
            raise RuntimeError(f"{exc}\nPackaged app log tail:\n{log_tail}") from exc
        raise
    finally:
        helpers._stop_app(proc)
        shutil.rmtree(smoke_root, ignore_errors=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, help="Exact packaged .app candidate")
    parser.add_argument(
        "--expected-git-sha",
        required=True,
        help="Full Git SHA that release-provenance.json must identify",
    )
    parser.add_argument(
        "--electron-binary",
        type=Path,
        help="Optional existing Electron test driver; does not alter the candidate",
    )
    parser.add_argument("--distribution", default="official")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--evidence-output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    evidence = _run(args)
    _write_json(args.evidence_output, evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if evidence.get("ok") is True else 1


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        raise SystemExit(main())
