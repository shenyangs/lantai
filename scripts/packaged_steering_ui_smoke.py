#!/usr/bin/env python3
"""Exercise steering UI behavior directly against one exact packaged candidate.

This is deliberately narrower than ``packaged_app_lifecycle_smoke.py``: it
starts an isolated profile, uses the deterministic local provider fixture, and
jumps straight to the existing renderer journeys for active-turn input,
explicit redirection, stop/resend, and route isolation.  It never opens or
modifies the user's daily application profile.
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
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPERS_PATH = PROJECT_ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
EVIDENCE_SCHEMA = "com.lantai.packaged-steering-ui-smoke.v2"


def _load_helpers() -> Any:
    spec = importlib.util.spec_from_file_location(
        "packaged_steering_ui_helpers",
        HELPERS_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("packaged lifecycle smoke helpers are unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe_app_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metadata.get(key)
        for key in (
            "bundle_identifier",
            "short_version",
            "bundle_version",
            "executable",
            "artifact_sha256",
        )
        if key in metadata
    }


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


def _safe_renderer_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    basic = payload.get("basic_chat_ui_send")
    basic = basic if isinstance(basic, dict) else {}
    provider = payload.get("provider_stream_ui")
    provider = provider if isinstance(provider, dict) else {}
    rapid_queue = provider.get("rapid_queue")
    rapid_queue = rapid_queue if isinstance(rapid_queue, dict) else {}
    rapid_final = rapid_queue.get("final")
    rapid_final = rapid_final if isinstance(rapid_final, dict) else {}
    backend = provider.get("backend")
    backend = backend if isinstance(backend, dict) else {}
    stop = payload.get("provider_stop_resend_ui")
    stop = stop if isinstance(stop, dict) else {}
    stop_final = stop.get("final")
    stop_final = stop_final if isinstance(stop_final, dict) else {}
    route = payload.get("provider_stream_route_switch_ui")
    route = route if isinstance(route, dict) else {}
    route_isolation = route.get("isolation")
    route_isolation = (
        route_isolation if isinstance(route_isolation, dict) else {}
    )
    route_restored = route.get("restored")
    route_restored = route_restored if isinstance(route_restored, dict) else {}
    basic_matrix = basic.get("experience_matrix")
    basic_matrix = basic_matrix if isinstance(basic_matrix, list) else []

    return {
        "base_event_stream": {
            "terminal_type": payload.get("terminal_type"),
            "terminal_status": payload.get("terminal_status"),
            "no_replay_event_count": payload.get("no_replay_event_count"),
            "reconnected": int(payload.get("reconnect_event_count") or 0) > 0,
        },
        "ordinary_chat": {
            "ok": basic.get("ok") is True,
            "feedback_ms": {
                "first_user": (basic.get("first") or {}).get("user_visible_ms")
                if isinstance(basic.get("first"), dict)
                else None,
                "first_assistant": (basic.get("first") or {}).get(
                    "assistant_visible_ms"
                )
                if isinstance(basic.get("first"), dict)
                else None,
                "second_user": (basic.get("second") or {}).get(
                    "user_visible_ms"
                )
                if isinstance(basic.get("second"), dict)
                else None,
                "second_assistant": (basic.get("second") or {}).get(
                    "assistant_visible_ms"
                )
                if isinstance(basic.get("second"), dict)
                else None,
            },
            "experience_matrix": [
                {
                    "id": item.get("id"),
                    "passed": item.get("passed") is True,
                }
                for item in basic_matrix
                if isinstance(item, dict)
            ],
        },
        "active_turn": {
            "ok": provider.get("ok") is True,
            "controls": provider.get("active_controls"),
            "input_method": provider.get("input_method"),
            "attachment_drag": provider.get("attachment_drag"),
            "timing_ms": {
                "thinking": provider.get("thinking_visible_ms"),
                "partial": provider.get("partial_visible_ms"),
                "final": provider.get("final_visible_ms"),
            },
            "thinking_before_partial_before_final": (
                int(provider.get("thinking_visible_ms") or 0)
                < int(provider.get("partial_visible_ms") or 0)
                < int(provider.get("final_visible_ms") or 0)
            ),
            "guidance": {
                "accepted_before_first_answer": rapid_queue.get(
                    "queued_before_first_answer"
                )
                is True,
                "submission_count": rapid_queue.get("queued_submission_count"),
                "submission_intents": rapid_queue.get("submission_intents"),
                "initial_admission_statuses": rapid_queue.get(
                    "admitted_statuses"
                ),
                "final_user_count": rapid_final.get("user_bubble_count"),
                "final_assistant_count": rapid_final.get(
                    "assistant_bubble_count"
                ),
                "user_fifo": rapid_final.get("user_texts_in_fifo_order")
                is True,
                "assistant_fifo": rapid_final.get(
                    "assistant_texts_in_fifo_order"
                )
                is True,
                "old_partial_preserved": rapid_final.get(
                    "old_partial_preserved"
                )
                is True,
                "old_full_answer_absent": rapid_final.get(
                    "old_full_answer_absent"
                )
                is True,
                "superseded_answer_absent": rapid_final.get(
                    "superseded_answer_absent"
                )
                is True,
                "redirect_answer_present": rapid_final.get(
                    "redirect_answer_present"
                )
                is True,
                "supplement_disposition": rapid_final.get(
                    "supplement_disposition"
                ),
                "redirect_disposition": rapid_final.get(
                    "redirect_disposition"
                ),
                "no_empty_assistant": int(
                    rapid_final.get("empty_assistant_count") or 0
                )
                == 0,
                "backend_pending_cleared": not str(
                    backend.get("pending_user_message") or ""
                ),
            },
        },
        "stop_resend": {
            "ok": stop.get("ok") is True,
            "terminal": stop.get("stop_terminal"),
            "stopped_answer_absent": stop.get("full_stopped_answer_absent")
            is True,
            "final_user_count": stop_final.get("user_bubble_count"),
            "no_empty_assistant": int(
                stop_final.get("empty_assistant_count") or 0
            )
            == 0,
        },
        "route_switch": {
            "ok": route.get("ok") is True,
            "isolation_user_count": route_isolation.get("user_bubble_count"),
            "isolation_assistant_count": route_isolation.get(
                "assistant_bubble_count"
            ),
            "restored_user_count": route_restored.get("user_bubble_count"),
            "restored_assistant_count": route_restored.get(
                "assistant_bubble_count"
            ),
            "restored_without_empty_assistant": int(
                route_restored.get("empty_assistant_count") or 0
            )
            == 0,
        },
    }


def _build_evidence(
    *,
    app_metadata: dict[str, Any],
    provenance: dict[str, Any],
    renderer_payload: dict[str, Any],
    provider_request_count: int,
) -> dict[str, Any]:
    renderer = _safe_renderer_evidence(renderer_payload)
    base = renderer["base_event_stream"]
    ordinary = renderer["ordinary_chat"]
    active = renderer["active_turn"]
    stop = renderer["stop_resend"]
    route = renderer["route_switch"]
    controls = active.get("controls")
    controls = controls if isinstance(controls, dict) else {}
    input_method = active.get("input_method")
    input_method = input_method if isinstance(input_method, dict) else {}
    attachment_drag = active.get("attachment_drag")
    attachment_drag = (
        attachment_drag if isinstance(attachment_drag, dict) else {}
    )
    checks = {
        "exact_candidate_provenance": (
            provenance.get("canonicalMain") is False
            and bool(provenance.get("gitSha"))
        ),
        "isolated_profile": True,
        "base_stream_reconnect_no_replay": (
            base.get("reconnected") is True
            and base.get("no_replay_event_count") == 0
        ),
        "ordinary_chat_protection": ordinary.get("ok") is True,
        "active_primary_defaults_to_supplement": (
            controls.get("primary_label") == "补充当前任务"
        ),
        "redirect_and_queue_are_explicit": controls.get("explicit_intents")
        == ["立即调整方向", "排队下一任务"],
        "ime_composition_enter_safe": (
            input_method.get("composition_enter_blocked") is True
            and input_method.get("draft_preserved") is True
            and input_method.get("post_composition_submission_count") == 1
        ),
        "drag_attachment_ready_deduplicated_without_submit": (
            attachment_drag.get("file_name") == "steering-drag.png"
            and attachment_drag.get("media_type") == "image/png"
            and attachment_drag.get("dragover_prevented") is True
            and attachment_drag.get("drop_prevented") is True
            and attachment_drag.get("uploaded_ready") is True
            and attachment_drag.get("duplicate_deduplicated") is True
            and attachment_drag.get("removed_cleanly") is True
            and attachment_drag.get("chat_submission_count") == 0
        ),
        "active_guidance_fifo_and_persisted": (
            active.get("ok") is True
            and active.get("thinking_before_partial_before_final") is True
            and (active.get("guidance") or {}).get("submission_intents")
            == ["supplement", "redirect"]
            and (
                (active.get("guidance") or {}).get(
                    "supplement_disposition"
                )
                or {}
            ).get("status")
            == "superseded"
            and (
                (active.get("guidance") or {}).get("redirect_disposition")
                or {}
            ).get("status")
            == "applied"
            and all(
                (active.get("guidance") or {}).get(key) is True
                for key in (
                    "accepted_before_first_answer",
                    "user_fifo",
                    "assistant_fifo",
                    "old_partial_preserved",
                    "old_full_answer_absent",
                    "superseded_answer_absent",
                    "redirect_answer_present",
                    "no_empty_assistant",
                    "backend_pending_cleared",
                )
            )
        ),
        "stop_resend_safe": (
            stop.get("ok") is True
            and stop.get("terminal") in {"interrupted", "cancelled"}
            and stop.get("stopped_answer_absent") is True
        ),
        "route_switch_isolated_and_restored": (
            route.get("ok") is True
            and route.get("isolation_user_count") == 0
            and route.get("isolation_assistant_count") == 0
            and int(route.get("restored_user_count") or 0) == 1
            and int(route.get("restored_assistant_count") or 0) == 1
            and route.get("restored_without_empty_assistant") is True
        ),
    }
    return {
        "schema": EVIDENCE_SCHEMA,
        "ok": all(checks.values()),
        "distribution": "official-artifact-only-candidate",
        "app": _safe_app_metadata(app_metadata),
        "provenance": provenance,
        "provider_fixture": {
            "kind": "isolated-local-openai-compatible",
            "request_count": provider_request_count,
            "paid_requests": 0,
        },
        "renderer": renderer,
        "checks": checks,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    helpers = _load_helpers()
    app_path = Path(args.app).expanduser().resolve()
    executable = helpers._find_app_executable(app_path)
    app_metadata = helpers._app_metadata_for_evidence(app_path)
    provenance = _release_provenance(app_path)
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-steering-ui-smoke-"))
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
    provider: Any = None
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
                    predicate=lambda payload: int(payload.get("sequence") or 0)
                    >= 1,
                    process=proc,
                    expected_distribution=args.distribution,
                )
                helpers._assert_ready_state(
                    state,
                    expected_distribution=args.distribution,
                )
                helpers._assert_desktop_readiness(state, timeout=args.timeout)
                (
                    _session_id,
                    base_chat_id,
                ) = helpers._exercise_console_model_success_path(
                    state,
                    timeout=args.timeout,
                )
                workspace_dir = (
                    user_data_dir / "working" / "workspaces" / "default"
                )
                basic_ui = helpers._seed_chat_basic_ui_send(
                    state,
                    workspace_dir=workspace_dir,
                    timeout=args.timeout,
                )
                provider_ui = helpers._seed_chat_provider_stream_ui(
                    state,
                    workspace_dir=workspace_dir,
                    timeout=args.timeout,
                )
                renderer = helpers._run_chat_runtime_stream_renderer_smoke(
                    state,
                    chat_id=base_chat_id,
                    timeout=args.timeout,
                    basic_ui_send=basic_ui,
                    provider_stream_ui=provider_ui,
                    expected_terminal_type="turn.completed",
                    expected_terminal_statuses={"completed", "finished"},
                )
                evidence = _build_evidence(
                    app_metadata=app_metadata,
                    provenance=provenance,
                    renderer_payload=renderer,
                    provider_request_count=provider.request_count,
                )
                if not evidence["ok"]:
                    raise AssertionError(
                        f"steering UI evidence checks failed: {evidence['checks']!r}"
                    )
                return evidence
    except Exception as exc:
        log_tail = helpers._tail_text(app_log_path, limit=6000)
        if log_tail:
            raise RuntimeError(
                f"{exc}\nPackaged app log tail:\n{log_tail}"
            ) from exc
        raise
    finally:
        helpers._stop_app(proc)
        shutil.rmtree(smoke_root, ignore_errors=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app", required=True, help="Exact packaged .app candidate"
    )
    parser.add_argument("--distribution", default="official")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--evidence-output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    evidence = _run(args)
    _write_json(args.evidence_output, evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        raise SystemExit(main())
