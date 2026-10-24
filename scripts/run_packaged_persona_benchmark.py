#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the fixed Shen Xingzhi multi-turn benchmark through a packaged app.

The runner stages one configured provider into an isolated user-data directory,
creates one real chat per scenario, and sends every turn through the packaged
Console endpoint. The prompts are synthetic and versioned in the repository;
the runner never reads the user's existing chats or memory stores.
"""

# This harness intentionally composes the packaged lifecycle helper's test seams.
# pylint: disable=protected-access

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from lantai.utils.redaction import redact_sensitive_value


ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_HELPERS = ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
PROVIDER_HELPERS = (
    ROOT / "scripts" / "source_configured_provider_stream_smoke.py"
)
SCENARIO_SUITE = (
    ROOT
    / "src"
    / "lantai"
    / "agents"
    / "md_files"
    / "constitutions"
    / "shen_xingzhi-multiturn-dual-axis-zh.yaml"
)
_DIALOGUE_ONLY_PREFIX = (
    "[隔离对话：只像正常聊天一样回复；不调用工具；不解释这段提示；" + "不要声称已经完成现实中的事情。]\n\n"
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _helpers() -> tuple[Any, Any]:
    return (
        _load(LIFECYCLE_HELPERS, "packaged_persona_lifecycle_helpers"),
        _load(PROVIDER_HELPERS, "packaged_persona_provider_helpers"),
    )


def _clean_env(*, user_data: Path, state_path: Path) -> dict[str, str]:
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
            "LANTAI_DESKTOP_USER_DATA_DIR": str(user_data),
            "LANTAI_WORKING_DIR": str(user_data / "working"),
            "LANTAI_SECRET_DIR": str(user_data / "working.secret"),
            "LANTAI_AUTH_ENABLED": "0",
            "LANTAI_DISABLE_GITHUB_UPDATER": "1",
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_REMINDER_DELIVERY_WORKER": "0",
        },
    )
    return env


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict)
        and str(part.get("type") or "") in {"text", "output_text"}
        and str(part.get("text") or "").strip()
    ).strip()


def _visible_answer(payloads: list[dict[str, Any]]) -> str:
    """Return only assistant answer text, excluding reasoning and tools."""

    for payload in reversed(payloads):
        output = payload.get("output")
        if not isinstance(output, list):
            continue
        texts = [
            _content_text(item.get("content"))
            for item in output
            if isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") in {None, "assistant"}
        ]
        answer = "\n".join(text for text in texts if text).strip()
        if answer:
            return answer

    message_types: dict[str, str] = {}
    chunks: dict[str, str] = {}
    for payload in payloads:
        if payload.get("object") == "message" and payload.get("id"):
            message_types[str(payload["id"])] = str(payload.get("type") or "")
            direct = _content_text(payload.get("content"))
            if direct and payload.get("type") == "message":
                chunks[str(payload["id"])] = direct
        if payload.get("object") != "content":
            continue
        message_id = str(payload.get("msg_id") or "")
        if not message_id or message_types.get(message_id) != "message":
            continue
        text = str(payload.get("text") or "")
        previous = chunks.get(message_id, "")
        chunks[message_id] = (
            text if text.startswith(previous) else previous + text
        )
    return "\n".join(value for value in chunks.values() if value).strip()


def _chat_payload(
    *,
    session_id: str,
    client_turn_id: str,
    prompt: str,
) -> dict[str, Any]:
    return {
        "input": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
        "session_id": session_id,
        "user_id": "default",
        "channel": "console",
        "stream": True,
        "client_turn_id": client_turn_id,
        "meta": {"client_turn_id": client_turn_id},
    }


def _prompt_with_memory(memory: tuple[str, ...], user: str) -> str:
    if not memory:
        return _DIALOGUE_ONLY_PREFIX + user
    context = "；".join(memory)
    return (
        _DIALOGUE_ONLY_PREFIX + f"已确认的既有背景（不要复述标签本身）：{context}\n\n用户现在说：{user}"
    )


def _override_staged_model(
    *,
    working_dir: Path,
    secret_dir: Path,
    configured: dict[str, str],
    provider_id: str | None,
    model: str | None,
) -> dict[str, str]:
    """Select another configured provider only inside the isolated profile."""

    provider_id = str(provider_id or "").strip()
    model = str(model or "").strip()
    if not provider_id and not model:
        return configured
    if not provider_id or not model:
        raise ValueError("provider-id and model must be supplied together")
    provider_candidates = [
        secret_dir / "providers" / "builtin" / f"{provider_id}.json",
        secret_dir / "providers" / f"{provider_id}.json",
    ]
    if not any(path.is_file() for path in provider_candidates):
        raise ValueError(
            f"provider secret is unavailable in isolated profile: {provider_id}",
        )
    agent_path = working_dir / "workspaces" / "default" / "agent.json"
    agent = json.loads(agent_path.read_text(encoding="utf-8"))
    agent["active_model"] = {"provider_id": provider_id, "model": model}
    agent_path.write_text(
        json.dumps(agent, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "provider_id": provider_id,
        "model": model,
        "agent_path": str(agent_path),
    }


def _report_dict(report: Any) -> dict[str, Any]:
    return {
        "model_id": report.model_id,
        "total_turns": report.total_turns,
        "relationship_passed": report.relationship_passed,
        "governance_passed": report.governance_passed,
        "both_passed": report.both_passed,
        "relationship_rate": report.relationship_rate,
        "governance_rate": report.governance_rate,
        "both_rate": report.both_rate,
        "missing_turn_ids": list(report.missing_turn_ids),
        "failed_turn_ids": [
            turn_id
            for turn_id, evaluation in report.evaluations
            if not evaluation.passed
        ],
        "evaluations": [
            {
                "turn_id": turn_id,
                "relationship": {
                    "passed": evaluation.relationship.passed,
                    "score": evaluation.relationship.score,
                    "missing_required_groups": [
                        list(group)
                        for group in evaluation.relationship.missing_required_groups
                    ],
                    "forbidden_matches": list(
                        evaluation.relationship.forbidden_matches,
                    ),
                },
                "governance": {
                    "passed": evaluation.governance.passed,
                    "score": evaluation.governance.score,
                    "missing_required_groups": [
                        list(group)
                        for group in evaluation.governance.missing_required_groups
                    ],
                    "forbidden_matches": list(
                        evaluation.governance.forbidden_matches,
                    ),
                },
            }
            for turn_id, evaluation in report.evaluations
        ],
    }


def _select_scenarios(
    scenarios: list[Any],
    scenario_ids: tuple[str, ...],
) -> list[Any]:
    requested = {
        str(value).strip() for value in scenario_ids if str(value).strip()
    }
    if not requested:
        return scenarios
    available = {str(scenario.id) for scenario in scenarios}
    unknown = sorted(requested - available)
    if unknown:
        raise ValueError(f"unknown persona scenario: {', '.join(unknown)}")
    return [
        scenario for scenario in scenarios if str(scenario.id) in requested
    ]


def _terminal_failure_diagnostic(
    http_status: int,
    final: dict[str, Any],
) -> dict[str, Any]:
    raw_error = final.get("error")
    error = raw_error if isinstance(raw_error, dict) else {}
    raw_message = str(
        error.get("message") or final.get("message") or "",
    ).strip()
    safe_message = str(redact_sensitive_value(raw_message))[:800]
    return {
        "http_status": http_status,
        "status": str(final.get("status") or ""),
        "error_type": str(error.get("type") or ""),
        "error_code": str(error.get("code") or ""),
        "message": safe_message,
    }


def _cleanup_smoke_root(smoke_root: Path, *, succeeded: bool) -> None:
    if succeeded:
        shutil.rmtree(smoke_root, ignore_errors=True)
        return
    # Failure evidence is intentionally kept local for diagnosis, but copied
    # provider credentials never belong in the retained bundle.
    shutil.rmtree(
        smoke_root / "user-data" / "working.secret",
        ignore_errors=True,
    )


# The lifecycle remains in one try/finally so credentials and the child process
# are cleaned together on every failure path.
# pylint: disable-next=too-many-statements
def run(
    *,
    app: Path,
    source_working_dir: Path,
    source_secret_dir: Path,
    agent_id: str,
    provider_id: str | None,
    model: str | None,
    timeout: float,
    scenario_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    helpers, staging = _helpers()
    from lantai.agents.persona.dual_axis_benchmark import (  # noqa: PLC0415
        load_builtin_multiturn_scenarios,
        run_dual_axis_benchmark,
    )

    app_path = app.expanduser().resolve(strict=True)
    executable = helpers._find_app_executable(app_path)
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-packaged-persona-"))
    user_data = smoke_root / "user-data"
    state_path = smoke_root / "desktop-lifecycle-state.json"
    app_log = smoke_root / "app.log"
    process: subprocess.Popen[str] | None = None
    started = time.time()
    succeeded = False
    try:
        working, secret, configured = staging._stage_profile(
            source_working_dir=source_working_dir.expanduser().resolve(
                strict=True,
            ),
            source_secret_dir=source_secret_dir.expanduser().resolve(
                strict=True,
            ),
            agent_id=agent_id,
            target_root=user_data,
        )
        configured = _override_staged_model(
            working_dir=working,
            secret_dir=secret,
            configured=configured,
            provider_id=provider_id,
            model=model,
        )
        with app_log.open("w", encoding="utf-8") as log:
            # The process must outlive this statement and is stopped in finally.
            # pylint: disable-next=consider-using-with
            process = subprocess.Popen(
                [str(executable)],
                cwd=str(app_path.parent),
                env=_clean_env(user_data=user_data, state_path=state_path),
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            state = helpers._wait_for_state(
                state_path,
                timeout=timeout,
                predicate=lambda value: int(value.get("sequence") or 0) >= 1,
                process=process,
                expected_distribution="official",
            )
            helpers._assert_ready_state(
                state,
                expected_distribution="official",
            )
            helpers._assert_desktop_readiness(state, timeout=timeout)

            scenarios = _select_scenarios(
                load_builtin_multiturn_scenarios("zh"),
                scenario_ids,
            )
            responses: dict[str, str] = {}
            turn_evidence: list[dict[str, Any]] = []
            run_token = uuid.uuid4().hex[:10]
            total = sum(len(scenario.turns) for scenario in scenarios)
            completed = 0
            for scenario in scenarios:
                session_id = (
                    f"console:persona-benchmark-{run_token}-{scenario.id}"
                )
                for index, turn in enumerate(scenario.turns, start=1):
                    turn_id = f"{scenario.id}:{index}"
                    prompt = (
                        _prompt_with_memory(scenario.memory_preload, turn.user)
                        if index == 1
                        else _prompt_with_memory((), turn.user)
                    )
                    client_turn_id = (
                        f"persona-benchmark-{run_token}-{scenario.id}-{index}"
                    )
                    turn_started = time.monotonic()
                    status, sse, _parsed = helpers._json_request(
                        "POST",
                        f"{helpers._state_base_url(state)}/api/console/chat",
                        payload=_chat_payload(
                            session_id=session_id,
                            client_turn_id=client_turn_id,
                            prompt=prompt,
                        ),
                        headers=helpers._state_headers(state),
                        timeout=timeout,
                    )
                    payloads = helpers._extract_sse_payloads(sse)
                    final = payloads[-1] if payloads else {}
                    if status != 200 or final.get("status") != "completed":
                        diagnostic = _terminal_failure_diagnostic(
                            status,
                            final,
                        )
                        raise AssertionError(
                            f"Persona turn {turn_id} did not complete: HTTP {status}, "
                            f"status={final.get('status')!r}, "
                            f"diagnostic={json.dumps(diagnostic, ensure_ascii=False)}",
                        )
                    answer = _visible_answer(payloads)
                    if not answer:
                        raise AssertionError(
                            f"Persona turn {turn_id} had no visible answer",
                        )
                    responses[turn_id] = answer
                    completed += 1
                    turn_evidence.append(
                        {
                            "turn_id": turn_id,
                            "elapsed_ms": round(
                                (time.monotonic() - turn_started) * 1000,
                            ),
                            "answer": answer,
                            "answer_sha256": _sha256_bytes(
                                answer.encode("utf-8"),
                            ),
                            "sse_frame_count": len(payloads),
                        },
                    )
                    print(
                        f"{agent_id}: persona turn {completed}/{total} {turn_id}",
                        flush=True,
                    )

        model_id = f"{configured['provider_id']}/{configured['model']}"
        report = run_dual_axis_benchmark(
            model_id=model_id,
            scenarios=scenarios,
            responses=responses,
        )
        result = {
            "schema": "com.lantai.packaged-persona-benchmark.v1",
            "ok": True,
            "started_at": started,
            "finished_at": time.time(),
            "app": helpers._app_metadata_for_evidence(app_path),
            "configured_model": {
                "provider_id": configured["provider_id"],
                "model": configured["model"],
                "agent_id": agent_id,
            },
            "suite": {
                "path": str(SCENARIO_SUITE.relative_to(ROOT)),
                "sha256": _sha256_file(SCENARIO_SUITE),
                "constitution_version": "1.6.1",
            },
            "benchmark": _report_dict(report),
            "turns": turn_evidence,
        }
        succeeded = True
        return result
    finally:
        try:
            helpers._stop_app(process)
        finally:
            _cleanup_smoke_root(smoke_root, succeeded=succeeded)
            if not succeeded:
                print(
                    f"Packaged persona failure artifacts retained: {smoke_root}",
                    file=os.sys.stderr,
                    flush=True,
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--source-working-dir", type=Path, required=True)
    parser.add_argument("--source-secret-dir", type=Path, required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--provider-id")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument(
        "--scenario-id",
        action="append",
        default=[],
        help="Run only this scenario id; repeat to select more than one.",
    )
    parser.add_argument("--evidence-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(
            app=args.app,
            source_working_dir=args.source_working_dir,
            source_secret_dir=args.source_secret_dir,
            agent_id=args.agent_id,
            provider_id=args.provider_id,
            model=args.model,
            timeout=args.timeout,
            scenario_ids=tuple(args.scenario_id),
        )
        args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"ok": True, "evidence": str(args.evidence_output)}))
        return 0
    except Exception as exc:  # noqa: BLE001 - release-gate CLI diagnostics
        print(f"Packaged persona benchmark failed: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
