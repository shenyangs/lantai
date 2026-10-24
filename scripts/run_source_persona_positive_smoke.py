#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run five positive-temperament scenarios through the real source backend."""

# This harness intentionally composes the source-smoke helper's test seams.
# pylint: disable=protected-access

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_HELPERS = (
    ROOT / "scripts" / "source_configured_provider_stream_smoke.py"
)
PERSONA_HELPERS = ROOT / "scripts" / "run_packaged_persona_benchmark.py"
LIFECYCLE_HELPERS = ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
SCENARIO_IDS = (
    "repeated-question-patience",
    "changing-requirements-kindness",
    "user-correction-listening",
    "repair-after-mistake",
    "honest-difficulty-with-path",
)


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean_env(
    *,
    working_dir: Path,
    secret_dir: Path,
    token: str,
) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("LANTAI_RUNTIME_ENGINE", "LANTAI_RUNTIME_ENGINE_DEFAULT"):
        env.pop(key, None)
        env.pop(key.replace("LANTAI_", "COPAW_"), None)
    env.update(
        {
            "LANTAI_WORKING_DIR": str(working_dir),
            "LANTAI_SECRET_DIR": str(secret_dir),
            "LANTAI_DESKTOP_TOKEN": token,
            "LANTAI_AUTH_ENABLED": "0",
            "LANTAI_DISABLE_KEYRING": "1",
            "LANTAI_SKIP_KEYRING": "1",
            "LANTAI_REMINDER_DELIVERY_WORKER": "0",
        },
    )
    return env


def run(
    *,
    source_working_dir: Path,
    source_secret_dir: Path,
    agent_id: str,
    timeout: float,
) -> dict[str, Any]:
    source = _load(SOURCE_HELPERS, "source_persona_positive_helpers")
    persona = _load(PERSONA_HELPERS, "source_persona_answer_helpers")
    lifecycle = _load(LIFECYCLE_HELPERS, "source_persona_lifecycle_helpers")
    from lantai.agents.persona.benchmark import (  # noqa: PLC0415
        evaluate_persona_response,
        load_builtin_persona_scenarios,
    )

    by_id = {item.id: item for item in load_builtin_persona_scenarios("zh")}
    scenarios = [by_id[scenario_id] for scenario_id in SCENARIO_IDS]
    smoke_root = Path(
        tempfile.mkdtemp(prefix="lantai-persona-positive-smoke-"),
    )
    process: subprocess.Popen[str] | None = None
    log_path = smoke_root / "backend.log"
    started = time.time()
    try:
        working, secret, configured = source._stage_profile(
            source_working_dir=source_working_dir.expanduser().resolve(
                strict=True,
            ),
            source_secret_dir=source_secret_dir.expanduser().resolve(
                strict=True,
            ),
            agent_id=agent_id,
            target_root=smoke_root,
        )
        port = source._available_port()
        base_url = f"http://127.0.0.1:{port}"
        desktop_token = "persona-positive-smoke-token"
        with log_path.open("w", encoding="utf-8") as log_file:
            # The process must outlive this statement and is stopped in finally.
            # pylint: disable-next=consider-using-with
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "lantai",
                    "app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                cwd=ROOT,
                env=_clean_env(
                    working_dir=working,
                    secret_dir=secret,
                    token=desktop_token,
                ),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            source._wait_for_health(
                base_url,
                process,
                timeout=min(timeout, 60),
            )
            results: list[dict[str, Any]] = []
            for index, scenario in enumerate(scenarios, start=1):
                token = uuid.uuid4().hex[:10]
                turn_started = time.monotonic()
                status, sse, _parsed = lifecycle._json_request(
                    "POST",
                    f"{base_url}/api/console/chat",
                    payload=persona._chat_payload(
                        session_id=f"console:persona-positive-{scenario.id}-{token}",
                        client_turn_id=f"persona-positive-{token}",
                        prompt=persona._prompt_with_memory(
                            (),
                            scenario.prompt,
                        ),
                    ),
                    headers=lifecycle._state_headers(
                        {"desktop_token": desktop_token},
                    ),
                    timeout=timeout,
                )
                payloads = lifecycle._extract_sse_payloads(sse)
                final = payloads[-1] if payloads else {}
                if status != 200 or final.get("status") != "completed":
                    raise AssertionError(
                        f"Scenario {scenario.id} did not complete: "
                        f"HTTP {status}, status={final.get('status')!r}",
                    )
                answer = persona._visible_answer(payloads)
                evaluation = evaluate_persona_response(scenario, answer)
                results.append(
                    {
                        "scenario_id": scenario.id,
                        "passed": evaluation.passed,
                        "elapsed_ms": round(
                            (time.monotonic() - turn_started) * 1000,
                        ),
                        "answer": answer,
                        "missing_required_groups": [
                            list(group)
                            for group in evaluation.missing_required_groups
                        ],
                        "forbidden_matches": list(
                            evaluation.forbidden_matches,
                        ),
                    },
                )
                print(
                    f"persona positive smoke {index}/{len(scenarios)} "
                    f"{scenario.id}: {'pass' if evaluation.passed else 'fail'}",
                    flush=True,
                )
        passed = sum(item["passed"] is True for item in results)
        return {
            "schema": "com.lantai.source-persona-positive-smoke.v1",
            "ok": passed == len(results),
            "started_at": started,
            "finished_at": time.time(),
            "configured_model": {
                "provider_id": configured["provider_id"],
                "model": configured["model"],
                "agent_id": agent_id,
            },
            "persona_template_version": "2.1.0",
            "constitution_version": "1.6.1",
            "passed": passed,
            "total": len(results),
            "results": results,
        }
    finally:
        source._terminate(process)
        shutil.rmtree(smoke_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-working-dir", type=Path, required=True)
    parser.add_argument("--source-secret-dir", type=Path, required=True)
    parser.add_argument("--agent-id", default="default")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args(argv)
    result = run(
        source_working_dir=args.source_working_dir,
        source_secret_dir=args.source_secret_dir,
        agent_id=args.agent_id,
        timeout=args.timeout,
    )
    if args.evidence_output:
        args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
