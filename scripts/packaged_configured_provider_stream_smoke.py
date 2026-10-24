#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Opt-in packaged-app UI smoke against one configured model provider.

The command launches an isolated packaged app candidate, copies the selected
encrypted provider profile only into an operating-system temporary directory,
and drives the candidate's rendered chat page through Electron. It records
only timing, counts, hashes, and configured provider/model identifiers. It
never prints a secret, copied path, prompt body, or model response.
"""

# pylint: disable=consider-using-with,protected-access,too-many-boolean-expressions

from __future__ import annotations

import argparse
import codecs
import hashlib
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
_PACKAGED_HELPERS_PATH = (
    PROJECT_ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
)
_CONFIGURED_PROVIDER_HELPERS_PATH = (
    PROJECT_ROOT / "scripts" / "source_configured_provider_stream_smoke.py"
)
_SMOKE_TURNS = (
    (
        "Reason through this dependency puzzle before answering: task D requires "
        "both B and C; B requires A; C requires A. After completing the analysis, "
        "reply with exactly RUNTIME_CONFIGURED_PROVIDER_ONE.",
        "RUNTIME_CONFIGURED_PROVIDER_ONE",
    ),
    (
        "Reason through this scheduling puzzle before answering: X precedes Y and "
        "Z; both Y and Z precede W. After completing the analysis, reply with "
        "exactly RUNTIME_CONFIGURED_PROVIDER_TWO.",
        "RUNTIME_CONFIGURED_PROVIDER_TWO",
    ),
)
_ARTIFACT_FILENAME = "delivery-check-note.txt"
_ARTIFACT_CONTENT = (
    "This note confirms that Lantai created and delivered the requested file."
)
_ARTIFACT_ANSWER_SENTINEL = "The requested file is ready to download."
_ARTIFACT_PROMPT = (
    "Please perform an actual end-to-end file delivery check. Create a UTF-8 "
    f"text file named {_ARTIFACT_FILENAME} in the current topic workspace. Write "
    f'exactly this sentence into it: "{_ARTIFACT_CONTENT}" Use the available file '
    "tools now and complete the delivery; do not only describe the steps. Once "
    "the file card is visible, end your final answer with exactly this sentence: "
    f'"{_ARTIFACT_ANSWER_SENTINEL}"'
)
_ALLOWED_PHASE_KEYS = {
    "user_bubble_count",
    "assistant_bubble_count",
    "empty_assistant_count",
    "thinking_count",
    "thinking_statuses",
    "response_avatar_count",
    "action_bar_count",
    "deep_thinking_control_present",
    "deep_thinking_control_enabled",
    "deep_thinking_control_active",
    "user_contains_prompt",
    "assistant_contains_sentinel",
    "assistant_sentinel_occurrences",
    "answer_node_completed",
    "user_clear_of_composer",
    "thinking_clear_of_composer",
    "answer_clear_of_composer",
    "inline_timeline_present",
    "run_drawer_trigger_present",
    "run_drawer_trigger_completed",
    "artifact_card_count",
    "matching_artifact_card_count",
    "artifact_card_has_open_action",
    "artifact_card_has_download_action",
}
_MAX_RENDERER_DIAGNOSTIC_CHARS = 12_000


def _load_module(path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load helper module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_helpers() -> tuple[Any, Any]:
    return (
        _load_module(
            _PACKAGED_HELPERS_PATH,
            "packaged_configured_provider_helpers",
        ),
        _load_module(
            _CONFIGURED_PROVIDER_HELPERS_PATH,
            "packaged_configured_provider_staging",
        ),
    )


def _clean_app_env(
    *,
    user_data_dir: Path,
    state_path: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "LANTAI_WORKING_DIR",
        "LANTAI_SECRET_DIR",
        "LANTAI_DESKTOP_USER_DATA_DIR",
        "LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE",
        "LANTAI_RUNTIME_ENGINE",
        "LANTAI_RUNTIME_ENGINE_DEFAULT",
    ):
        env.pop(key, None)
        env.pop(key.replace("LANTAI_", "COPAW_"), None)
    env.update(
        {
            "LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE": str(state_path),
            "LANTAI_DESKTOP_USER_DATA_DIR": str(user_data_dir),
            # The staged profile must be app-managed; otherwise desktop's
            # external-workspace guard correctly returns HTTP 423.
            "LANTAI_WORKING_DIR": str(user_data_dir / "working"),
            "LANTAI_SECRET_DIR": str(user_data_dir / "working.secret"),
            "LANTAI_AUTH_ENABLED": "0",
            "LANTAI_DISABLE_GITHUB_UPDATER": "1",
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_REMINDER_DELIVERY_WORKER": "0",
        },
    )
    return env


def _create_chat(
    helpers: Any,
    state: dict[str, Any],
    *,
    workspace_dir: Path,
    timeout: float,
) -> str:
    suffix = uuid.uuid4().hex[:12]
    status, _body, payload = helpers._json_request(
        "POST",
        f"{helpers._state_base_url(state)}/api/chats",
        payload={
            "name": f"configured provider packaged UI smoke {suffix}",
            "session_id": f"console:configured-provider-packaged-{suffix}",
            "user_id": "default",
            "channel": "console",
            "meta": {
                "topic_workspace_dir": str(
                    workspace_dir
                    / "topic-workspaces"
                    / f"configured-provider-{suffix}",
                ),
            },
        },
        headers=helpers._state_headers(state),
        timeout=timeout,
    )
    if (
        status != 200
        or not isinstance(payload, dict)
        or not isinstance(payload.get("id"), str)
    ):
        raise AssertionError(
            "Packaged configured-provider smoke could not create a chat",
        )
    return str(payload["id"])


def _staged_agent_workspace_dir(working_dir: Path) -> Path:
    """Return the configured default agent root within the staged profile."""
    return working_dir / "workspaces" / "default"


def _compact_phase(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssertionError(
            "Renderer result did not include a phase snapshot",
        )
    compact = {key: value.get(key) for key in _ALLOWED_PHASE_KEYS}
    if not isinstance(compact["thinking_statuses"], list):
        raise AssertionError(
            "Renderer phase did not include thinking statuses",
        )
    occurrences = compact["assistant_sentinel_occurrences"]
    if not isinstance(occurrences, int) or occurrences < 0:
        raise AssertionError(
            "Renderer returned an invalid answer marker count",
        )
    return compact


def _compact_turn(
    payload: Any,
    *,
    turn_number: int,
    artifact_required: bool = False,
) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or payload.get("turn_number") != turn_number
    ):
        raise AssertionError(
            "Configured-provider renderer returned an invalid turn",
        )
    timings: dict[str, int] = {}
    for key in ("user_visible_ms", "thinking_visible_ms", "answer_visible_ms"):
        value = payload.get(key)
        if not isinstance(value, int) or value < 0:
            raise AssertionError(
                f"Configured-provider renderer returned invalid {key}",
            )
        timings[key] = value
    if not timings["thinking_visible_ms"] < timings["answer_visible_ms"]:
        raise AssertionError(
            "Configured-provider renderer did not expose thinking first",
        )
    raw_run_drawer = payload.get("run_drawer")
    if not isinstance(raw_run_drawer, dict):
        raise AssertionError(
            "Configured-provider renderer did not inspect the run drawer",
        )
    run_drawer = {
        "visible": raw_run_drawer.get("visible") is True,
        "stage_count": raw_run_drawer.get("stage_count"),
        "trigger_completed": raw_run_drawer.get("trigger_completed") is True,
        "geometry": raw_run_drawer.get("geometry"),
    }
    geometry = run_drawer["geometry"]
    if (
        run_drawer["visible"] is not True
        or not isinstance(run_drawer["stage_count"], int)
        or run_drawer["stage_count"] < 1
        or run_drawer["trigger_completed"] is not True
        or not isinstance(geometry, dict)
        or not isinstance(geometry.get("width"), int)
        or geometry["width"] < 380
        or not isinstance(geometry.get("right"), int)
        or not isinstance(geometry.get("viewport_width"), int)
        or abs(geometry["right"] - geometry["viewport_width"]) > 1
    ):
        raise AssertionError(
            "Configured-provider run drawer was not healthy: "
            + json.dumps(run_drawer, ensure_ascii=False, sort_keys=True),
        )

    phases = {
        name: _compact_phase(payload.get(name))
        for name in ("user", "thinking", "answer", "final")
    }
    user = phases["user"]
    thinking = phases["thinking"]
    answer = phases["answer"]
    final = phases["final"]
    final_thinking_count_valid = isinstance(final["thinking_count"], int) and (
        final["thinking_count"] >= turn_number
        if artifact_required
        else final["thinking_count"] == turn_number
    )
    artifact_card_valid = not artifact_required or (
        final["artifact_card_count"] == 1
        and final["matching_artifact_card_count"] == 1
        and final["artifact_card_has_open_action"] is True
        and final["artifact_card_has_download_action"] is True
    )
    raw_deep_thinking_transport = payload.get("deep_thinking_transport")
    deep_thinking_transport = (
        {
            "present": raw_deep_thinking_transport.get("present") is True,
            "enabled": raw_deep_thinking_transport.get("enabled") is True,
            "effort": str(raw_deep_thinking_transport.get("effort") or ""),
        }
        if isinstance(raw_deep_thinking_transport, dict)
        else None
    )
    raw_deep_thinking_admission = payload.get("deep_thinking_admission")
    deep_thinking_admission = (
        {
            "marker_count": int(
                raw_deep_thinking_admission.get("deep_thinking_marker_count")
                or 0,
            ),
            "control_active": raw_deep_thinking_admission.get(
                "deep_thinking_control_active",
            )
            is True,
        }
        if isinstance(raw_deep_thinking_admission, dict)
        else {"marker_count": 0, "control_active": False}
    )
    user_assistant_count = user["assistant_bubble_count"]
    user_has_valid_assistant_feedback = user_assistant_count in {
        turn_number - 1,
        turn_number,
    }
    if (
        not user_has_valid_assistant_feedback
        or user["empty_assistant_count"] != 0
        or user["thinking_count"] != turn_number - 1
        or any(status != "completed" for status in user["thinking_statuses"])
        or user["response_avatar_count"] != turn_number - 1
        or user["action_bar_count"] != turn_number - 1
        or user["user_bubble_count"] != turn_number
        or user["user_contains_prompt"] is not True
        or user["assistant_contains_sentinel"] is not False
        or user["user_clear_of_composer"] is not True
        or user["inline_timeline_present"] is not False
        or user["run_drawer_trigger_present"] is not True
        or thinking["assistant_bubble_count"] != turn_number
        or thinking["empty_assistant_count"] != 0
        or thinking["thinking_count"] != turn_number
        or thinking["thinking_statuses"][:-1]
        != ["completed"] * (turn_number - 1)
        or thinking["thinking_statuses"][-1:] != ["in_progress"]
        or thinking["response_avatar_count"] != turn_number
        or thinking["action_bar_count"] != turn_number
        or thinking["assistant_contains_sentinel"] is not False
        or thinking["thinking_clear_of_composer"] is not True
        or thinking["inline_timeline_present"] is not False
        or thinking["run_drawer_trigger_present"] is not True
        or any(status != "completed" for status in answer["thinking_statuses"])
        or answer["answer_clear_of_composer"] is not True
        or answer["inline_timeline_present"] is not False
        or answer["run_drawer_trigger_present"] is not True
        or final["assistant_bubble_count"] != turn_number
        or final["empty_assistant_count"] != 0
        or not final_thinking_count_valid
        or any(status != "completed" for status in final["thinking_statuses"])
        or final["assistant_contains_sentinel"] is not True
        or final["answer_node_completed"] is not True
        or final["response_avatar_count"] != turn_number
        or final["action_bar_count"] != turn_number
        or (
            final["deep_thinking_control_present"] is True
            and final["deep_thinking_control_enabled"] is True
            and final["deep_thinking_control_active"] is not True
        )
        or (
            deep_thinking_admission["marker_count"] > 0
            and deep_thinking_admission["control_active"] is True
            and deep_thinking_transport
            != {"present": True, "enabled": True, "effort": "high"}
        )
        or final["answer_clear_of_composer"] is not True
        or final["inline_timeline_present"] is not False
        or final["run_drawer_trigger_present"] is not True
        or final["run_drawer_trigger_completed"] is not True
        or not artifact_card_valid
    ):
        raise AssertionError(
            "Configured-provider renderer final state was not healthy: "
            + json.dumps(phases, ensure_ascii=False, sort_keys=True),
        )
    compact = {
        "turn_number": turn_number,
        **timings,
        "run_drawer": run_drawer,
        "phases": phases,
        "deep_thinking_transport": deep_thinking_transport,
        "deep_thinking_admission": deep_thinking_admission,
    }
    if artifact_required:
        compact["artifact_delivery"] = {
            "card_count": final["artifact_card_count"],
            "matching_card_count": final["matching_artifact_card_count"],
            "open_action": final["artifact_card_has_open_action"],
            "download_action": final["artifact_card_has_download_action"],
        }
    return compact


def _compact_renderer_payload(
    payload: Any,
    *,
    chat_id: str,
    require_artifact_delivery: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise AssertionError(
            "Configured-provider renderer smoke did not return success",
        )
    if payload.get("chat_id") != chat_id:
        raise AssertionError(
            "Configured-provider renderer smoke returned the wrong chat",
        )
    raw_turns = payload.get("turns")
    expected_turn_count = len(_SMOKE_TURNS) + int(require_artifact_delivery)
    if (
        not isinstance(raw_turns, list)
        or len(raw_turns) != expected_turn_count
    ):
        raise AssertionError(
            "Configured-provider renderer did not complete every required turn",
        )
    turns = [
        _compact_turn(
            raw_turn,
            turn_number=index,
            artifact_required=(
                require_artifact_delivery and index == expected_turn_count
            ),
        )
        for index, raw_turn in enumerate(raw_turns, start=1)
    ]
    result = {
        "chat_id": chat_id,
        "turns": turns,
        "final": turns[-1]["phases"]["final"],
    }
    if require_artifact_delivery:
        result["artifact_delivery"] = turns[-1]["artifact_delivery"]
    return result


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _inspect_expected_artifact_bytes(value: bytes) -> dict[str, Any]:
    """Validate exact text while honoring the TXT writer's single UTF-8 BOM."""

    bom_count = 0
    content_bytes = value
    while content_bytes.startswith(codecs.BOM_UTF8):
        bom_count += 1
        content_bytes = content_bytes[len(codecs.BOM_UTF8) :]
    content_sha256 = _sha256_bytes(value)
    if bom_count > 1:
        raise AssertionError(
            "Configured-provider artifact smoke wrote unexpected file content "
            f"(bytes={len(value)}, sha256={content_sha256}, "
            f"utf8_bom_count={bom_count})",
        )
    try:
        decoded = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssertionError(
            "Configured-provider artifact smoke wrote non-UTF-8 file content "
            f"(bytes={len(value)}, sha256={content_sha256}, "
            f"utf8_bom_count={bom_count})",
        ) from exc
    if decoded != _ARTIFACT_CONTENT:
        raise AssertionError(
            "Configured-provider artifact smoke wrote unexpected file content "
            f"(bytes={len(value)}, sha256={content_sha256}, "
            f"utf8_bom_count={bom_count})",
        )
    return {
        "content_sha256": content_sha256,
        "content_bytes": len(value),
        "utf8_bom_count": bom_count,
    }


def _inspect_packaged_artifact_delivery(
    agent_workspace_dir: Path,
) -> dict[str, Any]:
    """Verify the real file plus committed/delivered S6 journal identities."""

    candidates = [
        path
        for path in (agent_workspace_dir / "topic-workspaces").rglob(
            _ARTIFACT_FILENAME,
        )
        if path.is_file() and not path.is_symlink()
    ]
    if len(candidates) != 1:
        raise AssertionError(
            "Configured-provider artifact smoke did not create exactly one file",
        )
    artifact_path = candidates[0]
    artifact_bytes = artifact_path.read_bytes()
    content_observation = _inspect_expected_artifact_bytes(artifact_bytes)
    content_sha256 = str(content_observation["content_sha256"])

    ledgers = list(
        (artifact_path.parent / ".lantai/runtime/v1/runs").rglob(
            "artifact-effects.jsonl",
        ),
    )
    latest: dict[str, dict[str, Any]] = {}
    for ledger in ledgers:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            envelope = json.loads(line)
            payload = (
                envelope.get("payload") if isinstance(envelope, dict) else None
            )
            if not isinstance(payload, dict):
                continue
            effect_id = str(payload.get("artifact_effect_id") or "")
            revision = int(payload.get("revision") or 0)
            if not effect_id:
                continue
            current = latest.get(effect_id)
            if current is None or revision > int(current.get("revision") or 0):
                latest[effect_id] = payload

    def relevant(role: str, status: str) -> list[dict[str, Any]]:
        return [
            row
            for row in latest.values()
            if row.get("artifact_role") == role
            and row.get("status") == status
            and str(row.get("relative_path") or "") == _ARTIFACT_FILENAME
            and row.get("sha256") == content_sha256
            and row.get("size_bytes") == len(artifact_bytes)
        ]

    candidates_committed = relevant("candidate", "committed")
    deliveries = relevant("delivery", "delivered")
    matching_pairs = [
        (candidate, delivery)
        for candidate in candidates_committed
        for delivery in deliveries
        if candidate.get("artifact_id")
        and candidate.get("artifact_id") == delivery.get("artifact_id")
        and candidate.get("run_id")
        and candidate.get("run_id") == delivery.get("run_id")
        and candidate.get("attempt_id")
        and candidate.get("attempt_id") == delivery.get("attempt_id")
        and candidate.get("artifact_effect_id")
        and delivery.get("artifact_effect_id")
        and candidate.get("artifact_effect_id")
        != delivery.get("artifact_effect_id")
        and candidate.get("tool_call_id")
        and delivery.get("tool_call_id")
        and candidate.get("tool_call_id") != delivery.get("tool_call_id")
    ]
    if (
        len(candidates_committed) != 1
        or len(deliveries) != 1
        or len(matching_pairs) != 1
    ):
        raise AssertionError(
            "Configured-provider artifact smoke lacked one exact committed-"
            "candidate-to-delivered EffectRecord chain",
        )
    return {
        "file_count": len(candidates),
        "content_sha256": content_sha256,
        "content_bytes": content_observation["content_bytes"],
        "utf8_bom_count": content_observation["utf8_bom_count"],
        "candidate_committed": True,
        "delivery_delivered": True,
        "shared_artifact_identity": True,
        "shared_run_identity": True,
        "distinct_effect_identities": True,
        "distinct_tool_call_identities": True,
        "ledger_count": len(ledgers),
    }


def _bounded_renderer_diagnostic(value: str) -> str:
    """Retain failure context from both ends without exposing renderer content."""
    diagnostic = value.strip()
    if len(diagnostic) <= _MAX_RENDERER_DIAGNOSTIC_CHARS:
        return diagnostic
    head_length = _MAX_RENDERER_DIAGNOSTIC_CHARS // 2
    tail_length = _MAX_RENDERER_DIAGNOSTIC_CHARS - head_length
    return (
        diagnostic[:head_length]
        + "\n... renderer diagnostic truncated in the middle ...\n"
        + diagnostic[-tail_length:]
    )


def _run_renderer_smoke(
    helpers: Any,
    state: dict[str, Any],
    *,
    chat_id: str,
    timeout: float,
    screenshot_dir: Path | None,
    require_artifact_delivery: bool = False,
) -> dict[str, Any]:
    console_dir = PROJECT_ROOT / "console"
    script = console_dir / "electron" / "configuredProviderStreamSmoke.cjs"
    if not script.is_file():
        raise FileNotFoundError(
            f"Configured-provider Electron smoke is missing: {script}",
        )
    command = [
        str(helpers._console_electron_binary(PROJECT_ROOT)),
        str(script),
        "--base-url",
        helpers._state_base_url(state),
        "--desktop-token",
        helpers._state_headers(state)["X-Lantai-Desktop-Token"],
        "--chat-id",
        chat_id,
        "--prompt",
        _SMOKE_TURNS[0][0],
        "--answer-sentinel",
        _SMOKE_TURNS[0][1],
        "--second-prompt",
        _SMOKE_TURNS[1][0],
        "--second-answer-sentinel",
        _SMOKE_TURNS[1][1],
        "--timeout-ms",
        str(max(1, int(timeout * 1000))),
    ]
    if screenshot_dir is not None:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        command.extend(["--screenshot-dir", str(screenshot_dir.resolve())])
    if require_artifact_delivery:
        command.extend(["--permission-preset", "full"])
        command.extend(
            [
                "--artifact-prompt",
                _ARTIFACT_PROMPT,
                "--artifact-filename",
                _ARTIFACT_FILENAME,
                "--artifact-answer-sentinel",
                _ARTIFACT_ANSWER_SENTINEL,
            ],
        )
    try:
        result = subprocess.run(
            command,
            cwd=str(console_dir),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=timeout + 30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "Configured-provider Electron renderer smoke timed out",
        ) from exc
    if result.returncode != 0:
        diagnostic = _bounded_renderer_diagnostic(
            result.stderr or result.stdout,
        )
        raise AssertionError(
            "Configured-provider Electron renderer smoke failed "
            f"with exit code {result.returncode}: {diagnostic}",
        )
    return _compact_renderer_payload(
        helpers._extract_json_object(result.stdout),
        chat_id=chat_id,
        require_artifact_delivery=require_artifact_delivery,
    )


def run(
    *,
    app: Path,
    distribution: str,
    source_working_dir: Path,
    source_secret_dir: Path,
    agent_id: str,
    timeout: float,
    screenshot_dir: Path | None = None,
    require_artifact_delivery: bool = False,
) -> dict[str, Any]:
    if distribution != "official":
        raise ValueError(f"Unsupported distribution: {distribution}")
    helpers, staging = _load_helpers()
    app_path = app.expanduser().resolve()
    executable = helpers._find_app_executable(app_path)
    smoke_root = Path(
        tempfile.mkdtemp(prefix="lantai-packaged-configured-provider-"),
    )
    user_data_dir = smoke_root / "user-data"
    state_path = smoke_root / "desktop-lifecycle-state.json"
    app_log_path = smoke_root / "app.log"
    process: subprocess.Popen[str] | None = None
    try:
        working_dir, _secret_dir, configured_model = staging._stage_profile(
            source_working_dir=source_working_dir.expanduser().resolve(),
            source_secret_dir=source_secret_dir.expanduser().resolve(),
            agent_id=agent_id,
            target_root=user_data_dir,
        )
        agent_workspace_dir = _staged_agent_workspace_dir(working_dir)
        env = _clean_app_env(
            user_data_dir=user_data_dir,
            state_path=state_path,
        )
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
                timeout=timeout,
                predicate=lambda payload: int(payload.get("sequence") or 0)
                >= 1,
                process=process,
                expected_distribution=distribution,
            )
            helpers._assert_ready_state(
                state,
                expected_distribution=distribution,
            )
            ready_pid = helpers._assert_desktop_readiness(
                state,
                timeout=timeout,
            )
            if ready_pid != state.get("backend_pid"):
                raise AssertionError(
                    "Packaged configured-provider readiness pid mismatch",
                )
            chat_id = _create_chat(
                helpers,
                state,
                workspace_dir=agent_workspace_dir,
                timeout=timeout,
            )
            renderer = _run_renderer_smoke(
                helpers,
                state,
                chat_id=chat_id,
                timeout=timeout,
                screenshot_dir=screenshot_dir,
                require_artifact_delivery=require_artifact_delivery,
            )
            artifact_delivery = (
                _inspect_packaged_artifact_delivery(agent_workspace_dir)
                if require_artifact_delivery
                else None
            )
            expected_turn_count = len(_SMOKE_TURNS) + int(
                require_artifact_delivery,
            )
            screenshot_paths = (
                [
                    (screenshot_dir / f"turn-{turn}-{phase}.png").resolve()
                    for turn in range(1, expected_turn_count + 1)
                    for phase in (
                        "user",
                        "thinking",
                        "answer",
                        "final",
                        "run-drawer",
                    )
                ]
                if screenshot_dir is not None
                else []
            )
            missing_screenshots = [
                str(path) for path in screenshot_paths if not path.is_file()
            ]
            if missing_screenshots:
                raise AssertionError(
                    "Configured-provider visual evidence is incomplete: "
                    + ", ".join(missing_screenshots),
                )
        migration = staging._assert_profile_migrated(
            Path(str(configured_model["agent_path"])),
        )
        evidence = {
            "ok": True,
            "distribution": distribution,
            "app": helpers._app_metadata_for_evidence(app_path),
            "configured_model": {
                "provider_id": configured_model["provider_id"],
                "model": configured_model["model"],
            },
            "migration": migration,
            "renderer": renderer,
            "screenshots": [str(path) for path in screenshot_paths],
        }
        if artifact_delivery is not None:
            evidence["artifact_delivery"] = artifact_delivery
        return evidence
    finally:
        helpers._stop_app(process)
        shutil.rmtree(smoke_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument(
        "--distribution",
        choices=("official",),
        default="official",
    )
    parser.add_argument("--source-working-dir", type=Path, required=True)
    parser.add_argument("--source-secret-dir", type=Path, required=True)
    parser.add_argument("--agent-id", default="default")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument(
        "--require-artifact-delivery",
        action="store_true",
        help="Add a real file-generation turn and require one EffectRecord-backed card.",
    )
    parser.add_argument(
        "--screenshot-dir",
        type=Path,
        help="Save real-renderer user/thinking/answer/final screenshots here.",
    )
    parser.add_argument(
        "--evidence-output",
        type=Path,
        help="Write the non-secret successful result JSON to this path.",
    )
    args = parser.parse_args(argv)
    try:
        result = run(
            app=args.app,
            distribution=args.distribution,
            source_working_dir=args.source_working_dir,
            source_secret_dir=args.source_secret_dir,
            agent_id=args.agent_id,
            timeout=args.timeout,
            screenshot_dir=args.screenshot_dir,
            require_artifact_delivery=args.require_artifact_delivery,
        )
        if args.evidence_output:
            helpers, _staging = _load_helpers()
            helpers._write_json_file(args.evidence_output, result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - command-line diagnostics
        print(
            f"Packaged configured-provider stream smoke failed: {exc}",
            file=os.sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
