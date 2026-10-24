#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-process smoke for a recoverable LOOP-05 long-running trial."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from lantai.runtime.loop_trial import (
    inspect_loop_interruption_matrix_trial,
    run_browser_content_loop_trial,
    run_loop_interruption_matrix_trial,
    run_loop_trial,
    run_release_verification_loop_trial,
)
from lantai.runtime.store import RuntimeRunStore


RUN_ID = "loop-trial-smoke"
RELEASE_RUN_ID = "loop-release-verification-smoke"
BROWSER_CONTENT_RUN_ID = "loop-browser-content-smoke"
INTERRUPTION_MATRIX_RUN_ID_PREFIX = "loop-interruption-matrix-smoke"


async def _run_once(workspace: Path, *, max_iterations: int) -> dict[str, object]:
    result = await run_loop_trial(
        workspace,
        run_id=RUN_ID,
        max_iterations=max_iterations,
    )
    return {
        "pid": os.getpid(),
        "status": result.status,
        "revision": result.revision,
        "iterations_executed": result.iterations_executed,
        "error_code": result.error_code,
        "progress": result.progress.model_dump(mode="json"),
    }


async def _inspect(workspace: Path) -> dict[str, object]:
    store = RuntimeRunStore(workspace)
    run = await store.load_contract(RUN_ID)
    progress = await store.load_loop_progress_window(
        RUN_ID,
        expected_attempt_id=f"{RUN_ID}:attempt-1",
    )
    latest_report = await store.load_latest_loop_evaluation_report(
        RUN_ID,
        expected_attempt_id=f"{RUN_ID}:attempt-1",
    )
    return {
        "pid": os.getpid(),
        "status": run.status if run is not None else "missing",
        "revision": run.revision if run is not None else None,
        "error_code": run.error.code if run is not None and run.error else None,
        "progress": progress.model_dump(mode="json") if progress is not None else None,
        "latest_report_id": (
            latest_report.report.report_id if latest_report is not None else None
        ),
    }


async def _run_release_once(
    workspace: Path,
    *,
    official_manifest: Path,
    mas_manifest: Path,
) -> dict[str, object]:
    result = await run_release_verification_loop_trial(
        workspace,
        manifests={
            "official": official_manifest,
            "mas": mas_manifest,
        },
        run_id=RELEASE_RUN_ID,
        max_iterations=1,
    )
    return {
        "pid": os.getpid(),
        "status": result.status,
        "revision": result.revision,
        "iterations_executed": result.iterations_executed,
        "error_code": result.error_code,
        "evidence_hash": result.evidence_hash,
        "issues": list(result.issues),
        "progress": result.progress.model_dump(mode="json"),
    }


async def _inspect_release(workspace: Path) -> dict[str, object]:
    store = RuntimeRunStore(workspace)
    run = await store.load_contract(RELEASE_RUN_ID)
    progress = await store.load_loop_progress_window(
        RELEASE_RUN_ID,
        expected_attempt_id=f"{RELEASE_RUN_ID}:attempt-1",
    )
    latest_report = await store.load_latest_loop_evaluation_report(
        RELEASE_RUN_ID,
        expected_attempt_id=f"{RELEASE_RUN_ID}:attempt-1",
    )
    return {
        "pid": os.getpid(),
        "status": run.status if run is not None else "missing",
        "revision": run.revision if run is not None else None,
        "error_code": run.error.code if run is not None and run.error else None,
        "progress": progress.model_dump(mode="json") if progress is not None else None,
        "latest_report_id": (
            latest_report.report.report_id if latest_report is not None else None
        ),
        "latest_report_conclusion": (
            latest_report.report.conclusion if latest_report is not None else None
        ),
    }


async def _run_browser_content_once(
    workspace: Path,
    *,
    payload_path: Path,
    human_gate_decision: str,
) -> dict[str, object]:
    if human_gate_decision not in {"pending", "approved", "rejected"}:
        raise ValueError(f"unsupported human gate decision: {human_gate_decision}")
    result = await run_browser_content_loop_trial(
        workspace,
        payload_path=payload_path,
        run_id=BROWSER_CONTENT_RUN_ID,
        human_gate_decision=human_gate_decision,  # type: ignore[arg-type]
        max_iterations=1,
    )
    return {
        "pid": os.getpid(),
        "status": result.status,
        "revision": result.revision,
        "iterations_executed": result.iterations_executed,
        "error_code": result.error_code,
        "evidence_hash": result.evidence_hash,
        "issues": list(result.issues),
        "progress": result.progress.model_dump(mode="json"),
    }


async def _inspect_browser_content(workspace: Path) -> dict[str, object]:
    store = RuntimeRunStore(workspace)
    run = await store.load_contract(BROWSER_CONTENT_RUN_ID)
    progress = await store.load_loop_progress_window(
        BROWSER_CONTENT_RUN_ID,
        expected_attempt_id=f"{BROWSER_CONTENT_RUN_ID}:attempt-1",
    )
    latest_report = await store.load_latest_loop_evaluation_report(
        BROWSER_CONTENT_RUN_ID,
        expected_attempt_id=f"{BROWSER_CONTENT_RUN_ID}:attempt-1",
    )
    return {
        "pid": os.getpid(),
        "status": run.status if run is not None else "missing",
        "revision": run.revision if run is not None else None,
        "error_code": run.error.code if run is not None and run.error else None,
        "current_step_kind": (
            run.current_step.kind if run is not None and run.current_step else None
        ),
        "progress": progress.model_dump(mode="json") if progress is not None else None,
        "latest_report_id": (
            latest_report.report.report_id if latest_report is not None else None
        ),
        "latest_report_conclusion": (
            latest_report.report.conclusion if latest_report is not None else None
        ),
        "human_gate_decision": (
            latest_report.report.human_gate_decision
            if latest_report is not None
            else None
        ),
    }


def _interruption_matrix_payload(result, *, pid: int) -> dict[str, object]:
    return {
        "pid": pid,
        "ok": result.ok,
        "run_id_prefix": result.run_id_prefix,
        "scenario_statuses": dict(result.scenario_statuses),
        "error_codes": dict(result.error_codes),
        "stop_reason_codes": dict(result.stop_reason_codes),
        "attempt_ids": dict(result.attempt_ids),
        "evidence_hash": result.evidence_hash,
        "issues": list(result.issues),
        "details": dict(result.details or {}),
    }


async def _run_interruption_matrix_once(workspace: Path) -> dict[str, object]:
    result = await run_loop_interruption_matrix_trial(
        workspace,
        run_id_prefix=INTERRUPTION_MATRIX_RUN_ID_PREFIX,
    )
    return _interruption_matrix_payload(result, pid=os.getpid())


async def _inspect_interruption_matrix(workspace: Path) -> dict[str, object]:
    result = await inspect_loop_interruption_matrix_trial(
        workspace,
        run_id_prefix=INTERRUPTION_MATRIX_RUN_ID_PREFIX,
    )
    return _interruption_matrix_payload(result, pid=os.getpid())


def _run_child(
    workspace: Path,
    phase: str,
    *,
    official_manifest: Path | None = None,
    mas_manifest: Path | None = None,
    browser_content_payload: Path | None = None,
    human_gate_decision: str | None = None,
) -> dict[str, object]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--workspace",
        str(workspace),
        "--phase",
        phase,
    ]
    if official_manifest is not None:
        command.extend(["--release-official-artifact-manifest", str(official_manifest)])
    if mas_manifest is not None:
        command.extend(["--release-mas-artifact-manifest", str(mas_manifest)])
    if browser_content_payload is not None:
        command.extend(["--browser-content-payload", str(browser_content_payload)])
    if human_gate_decision is not None:
        command.extend(["--human-gate-decision", human_gate_decision])
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _run_parent(workspace: Path) -> dict[str, object]:
    first = _run_child(workspace, "first")
    second = _run_child(workspace, "second")
    final = _run_child(workspace, "inspect")
    progress = final.get("progress") if isinstance(final.get("progress"), dict) else {}
    return {
        "ok": (
            first.get("status") == "running"
            and second.get("status") == "stalled"
            and final.get("status") == "stalled"
            and final.get("error_code") == "loop_no_progress_exhausted"
            and progress.get("consecutive_no_progress_iterations") == 2
            and final.get("latest_report_id") == f"{RUN_ID}-eval-2"
        ),
        "first_process": first.get("pid"),
        "second_process": second.get("pid"),
        "inspect_process": final.get("pid"),
        "first_status": first.get("status"),
        "second_status": second.get("status"),
        "final_status": final.get("status"),
        "error_code": final.get("error_code"),
        "progress": progress,
        "latest_report_id": final.get("latest_report_id"),
    }


def _run_release_parent(
    workspace: Path,
    *,
    official_manifest: Path,
    mas_manifest: Path,
) -> dict[str, object]:
    first = _run_child(
        workspace,
        "release-run",
        official_manifest=official_manifest,
        mas_manifest=mas_manifest,
    )
    final = _run_child(workspace, "release-inspect")
    progress = final.get("progress") if isinstance(final.get("progress"), dict) else {}
    return {
        "ok": (
            first.get("status") == "succeeded"
            and final.get("status") == "succeeded"
            and final.get("error_code") is None
            and final.get("latest_report_conclusion") == "pass"
            and progress.get("consecutive_no_progress_iterations") == 0
        ),
        "first_process": first.get("pid"),
        "inspect_process": final.get("pid"),
        "first_status": first.get("status"),
        "final_status": final.get("status"),
        "error_code": final.get("error_code"),
        "issues": first.get("issues"),
        "evidence_hash": first.get("evidence_hash"),
        "progress": progress,
        "latest_report_id": final.get("latest_report_id"),
        "latest_report_conclusion": final.get("latest_report_conclusion"),
    }


def _run_browser_content_parent(
    workspace: Path,
    *,
    payload_path: Path,
) -> dict[str, object]:
    pending = _run_child(
        workspace,
        "browser-content-run",
        browser_content_payload=payload_path,
        human_gate_decision="pending",
    )
    waiting = _run_child(workspace, "browser-content-inspect")
    approved = _run_child(
        workspace,
        "browser-content-run",
        browser_content_payload=payload_path,
        human_gate_decision="approved",
    )
    final = _run_child(workspace, "browser-content-inspect")
    return {
        "ok": (
            pending.get("status") == "waiting_approval"
            and waiting.get("status") == "waiting_approval"
            and waiting.get("latest_report_conclusion") == "waiting_human"
            and waiting.get("human_gate_decision") == "pending"
            and approved.get("status") == "succeeded"
            and final.get("status") == "succeeded"
            and final.get("latest_report_conclusion") == "pass"
            and final.get("human_gate_decision") == "approved"
        ),
        "pending_process": pending.get("pid"),
        "approved_process": approved.get("pid"),
        "inspect_process": final.get("pid"),
        "pending_status": pending.get("status"),
        "waiting_status": waiting.get("status"),
        "approved_status": approved.get("status"),
        "final_status": final.get("status"),
        "error_code": final.get("error_code"),
        "issues": approved.get("issues"),
        "evidence_hash": approved.get("evidence_hash"),
        "latest_report_id": final.get("latest_report_id"),
        "latest_report_conclusion": final.get("latest_report_conclusion"),
        "human_gate_decision": final.get("human_gate_decision"),
    }


def _run_interruption_matrix_parent(workspace: Path) -> dict[str, object]:
    first = _run_child(workspace, "interruption-matrix-run")
    final = _run_child(workspace, "interruption-matrix-inspect")
    return {
        "ok": (
            bool(first.get("ok"))
            and bool(final.get("ok"))
            and first.get("pid") != final.get("pid")
        ),
        "first_process": first.get("pid"),
        "inspect_process": final.get("pid"),
        "scenario_statuses": final.get("scenario_statuses"),
        "error_codes": final.get("error_codes"),
        "stop_reason_codes": final.get("stop_reason_codes"),
        "attempt_ids": final.get("attempt_ids"),
        "issues": final.get("issues"),
        "evidence_hash": final.get("evidence_hash"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--scenario",
        choices=(
            "code-safety",
            "release-verification",
            "browser-content",
            "interruption-matrix",
        ),
        default="code-safety",
    )
    parser.add_argument("--release-official-artifact-manifest", type=Path)
    parser.add_argument("--release-mas-artifact-manifest", type=Path)
    parser.add_argument("--browser-content-payload", type=Path)
    parser.add_argument(
        "--human-gate-decision",
        choices=("pending", "approved", "rejected"),
        default="pending",
    )
    parser.add_argument(
        "--phase",
        choices=(
            "parent",
            "first",
            "second",
            "inspect",
            "release-run",
            "release-inspect",
            "browser-content-run",
            "browser-content-inspect",
            "interruption-matrix-run",
            "interruption-matrix-inspect",
        ),
        default="parent",
    )
    args = parser.parse_args(argv)

    if args.phase == "parent":
        if args.scenario == "release-verification":
            if (
                args.release_official_artifact_manifest is None
                or args.release_mas_artifact_manifest is None
            ):
                parser.error(
                    "release-verification requires both release artifact manifest paths"
                )
            result = _run_release_parent(
                args.workspace,
                official_manifest=args.release_official_artifact_manifest,
                mas_manifest=args.release_mas_artifact_manifest,
            )
        elif args.scenario == "browser-content":
            if args.browser_content_payload is None:
                parser.error("browser-content requires --browser-content-payload")
            result = _run_browser_content_parent(
                args.workspace,
                payload_path=args.browser_content_payload,
            )
        elif args.scenario == "interruption-matrix":
            result = _run_interruption_matrix_parent(args.workspace)
        else:
            result = _run_parent(args.workspace)
    elif args.phase == "first":
        result = asyncio.run(_run_once(args.workspace, max_iterations=1))
    elif args.phase == "second":
        result = asyncio.run(_run_once(args.workspace, max_iterations=10))
    elif args.phase == "release-run":
        if (
            args.release_official_artifact_manifest is None
            or args.release_mas_artifact_manifest is None
        ):
            parser.error("release-run requires both release artifact manifest paths")
        result = asyncio.run(
            _run_release_once(
                args.workspace,
                official_manifest=args.release_official_artifact_manifest,
                mas_manifest=args.release_mas_artifact_manifest,
            )
        )
    elif args.phase == "release-inspect":
        result = asyncio.run(_inspect_release(args.workspace))
    elif args.phase == "browser-content-run":
        if args.browser_content_payload is None:
            parser.error("browser-content-run requires --browser-content-payload")
        result = asyncio.run(
            _run_browser_content_once(
                args.workspace,
                payload_path=args.browser_content_payload,
                human_gate_decision=args.human_gate_decision,
            )
        )
    elif args.phase == "browser-content-inspect":
        result = asyncio.run(_inspect_browser_content(args.workspace))
    elif args.phase == "interruption-matrix-run":
        result = asyncio.run(_run_interruption_matrix_once(args.workspace))
    elif args.phase == "interruption-matrix-inspect":
        result = asyncio.run(_inspect_interruption_matrix(args.workspace))
    else:
        result = asyncio.run(_inspect(args.workspace))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if bool(result.get("ok", True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
