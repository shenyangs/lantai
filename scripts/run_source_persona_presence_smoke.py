#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify grounded public work presence with one configured real provider."""

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
LIFECYCLE_HELPERS = ROOT / "scripts" / "packaged_app_lifecycle_smoke.py"
CONSTITUTION_VERSION = "1.6.1"
_FORBIDDEN_PROGRESS_PHRASES = (
    "你最在意",
    "我理解你",
    "请放心",
    "不用担心",
    "马上就好",
    "很快就好",
    "正在为您",
    "作为一个 AI",
)
_CONCRETE_PROGRESS_TERMS = (
    "文件",
    "版本",
    "两行",
    "差异",
    "不一致",
    "读取",
    "核对",
    "检查",
    "路径",
    "找到",
    "工作区",
    "搜索",
    "搜一下",
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
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (
                    str(ROOT / "src"),
                    env.get("PYTHONPATH", "").strip(),
                )
                if part
            ),
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


def _override_staged_model(
    *,
    working_dir: Path,
    secret_dir: Path,
    configured: dict[str, str],
    provider_id: str | None,
    model: str | None,
) -> dict[str, str]:
    provider_id = str(provider_id or "").strip()
    model = str(model or "").strip()
    if not provider_id and not model:
        return configured
    if not provider_id or not model:
        raise ValueError("provider-id and model must be supplied together")
    provider_candidates = (
        secret_dir / "providers" / "builtin" / f"{provider_id}.json",
        secret_dir / "providers" / f"{provider_id}.json",
    )
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


def _event_client_turn_id(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return str(
        event.get("client_turn_id") or payload.get("client_turn_id") or "",
    )


def _tool_call_id(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return str(
        event.get("item_id")
        or payload.get("id")
        or payload.get("tool_call_id")
        or payload.get("call_id")
        or "",
    ).strip()


def _event_public_text(event: dict[str, Any]) -> str:
    summary = " ".join(str(event.get("summary") or "").split()).strip()
    if summary:
        return summary
    payload = event.get("payload")
    message = payload.get("message") if isinstance(payload, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    return " ".join(
        str(block.get("text") or block.get("thinking") or "").strip()
        for block in content
        if isinstance(block, dict)
        and str(block.get("text") or block.get("thinking") or "").strip()
    ).strip()


def _assert_completed(payloads: list[dict[str, Any]], *, turn: int) -> None:
    final = next(
        (
            item
            for item in reversed(payloads)
            if item.get("object") == "response"
        ),
        {},
    )
    if final.get("status") != "completed":
        raise AssertionError(
            f"Presence smoke turn {turn} did not complete: {final!r}",
        )


# Progress evidence is intentionally decoded as one fail-closed event scan.
# pylint: disable-next=too-many-branches
def _progress_evidence(
    events: list[dict[str, Any]],
    *,
    client_turn_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    scoped = [
        event
        for event in events
        if isinstance(event, dict)
        and _event_client_turn_id(event) == client_turn_id
    ]
    tool_started: dict[str, dict[str, Any]] = {}
    tool_completed: dict[str, int] = {}
    for event in scoped:
        event_type = str(event.get("type") or "")
        call_id = _tool_call_id(event)
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if event_type == "tool.call.started" and call_id:
            tool_started[call_id] = {
                "seq": int(event.get("seq") or 0),
                "name": str(
                    payload.get("tool_name") or payload.get("name") or "",
                ),
            }
        elif event_type == "tool.call.completed" and call_id:
            tool_completed[call_id] = int(event.get("seq") or 0)
    tool_ids = set(tool_started)
    tool_events = [
        event
        for event in scoped
        if str(event.get("type") or "").startswith("tool.call.")
    ]
    if "_lantai_public_progress" in json.dumps(
        tool_events,
        ensure_ascii=False,
    ):
        raise AssertionError(
            "Presentation-only progress leaked into a tool event",
        )
    progress: list[dict[str, Any]] = []
    for event in scoped:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        metadata = payload.get("public_progress")
        if not isinstance(metadata, dict):
            continue
        text = _event_public_text(event)
        call_ids = [
            str(item).strip()
            for item in metadata.get("tool_call_ids", [])
            if isinstance(item, str) and item.strip()
        ]
        progress.append(
            {
                "text": text,
                "tool_call_ids": call_ids,
                "event_seq": int(event.get("seq") or 0),
                "progress_id_present": bool(
                    str(metadata.get("id") or "").strip(),
                ),
                "revision": metadata.get("revision"),
            },
        )

    if tool_ids and not progress:
        raise AssertionError(
            "Real provider used a tool without public work presence",
        )
    for item in progress:
        text = item["text"]
        if not text:
            raise AssertionError("Public work presence was empty")
        if len(text) > 96:
            raise AssertionError(
                f"Public work presence was too long: {text!r}",
            )
        forbidden = [
            phrase for phrase in _FORBIDDEN_PROGRESS_PHRASES if phrase in text
        ]
        if forbidden:
            raise AssertionError(
                f"Public work presence used overdone wording {forbidden!r}: {text!r}",
            )
        if not any(term in text for term in _CONCRETE_PROGRESS_TERMS):
            raise AssertionError(
                f"Public work presence was not concrete: {text!r}",
            )
        if not item["tool_call_ids"]:
            raise AssertionError(
                "Public work presence lacked tool-call identity",
            )
        if not set(item["tool_call_ids"]).issubset(tool_ids):
            raise AssertionError(
                "Public work presence referenced a tool call outside its turn",
            )
        starts = [
            tool_started[call_id]["seq"] for call_id in item["tool_call_ids"]
        ]
        completions = [
            tool_completed.get(call_id, 0) for call_id in item["tool_call_ids"]
        ]
        if not all(
            item["event_seq"] < started < completed
            for started, completed in zip(starts, completions)
        ):
            raise AssertionError(
                "Public work presence did not precede a complete tool lifecycle",
            )
        item["tool_names"] = [
            tool_started[call_id]["name"] for call_id in item["tool_call_ids"]
        ]
        item["tool_started_seqs"] = starts
        item["tool_completed_seqs"] = completions
    return progress, sorted(tool_ids)


def _primary_model_round_count(
    events: list[dict[str, Any]],
    *,
    client_turn_id: str,
) -> int:
    return sum(
        1
        for event in events
        if isinstance(event, dict)
        and _event_client_turn_id(event) == client_turn_id
        and str(event.get("type") or "") == "message.completed"
        and not isinstance(
            (event.get("payload") or {}).get("public_progress"),
            dict,
        )
    )


def _usage_totals(
    events: list[dict[str, Any]],
    *,
    client_turn_id: str,
) -> dict[str, int | bool]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    seen = False
    for event in events:
        if (
            not isinstance(event, dict)
            or _event_client_turn_id(event) != client_turn_id
        ):
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        seen = True
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] += value
    return {"available": seen, **totals}


# The lifecycle remains in one try/finally so credentials and the child process
# are cleaned together on every failure path.
# pylint: disable-next=too-many-statements
def run(
    *,
    source_working_dir: Path,
    source_secret_dir: Path,
    agent_id: str,
    timeout: float,
    provider_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    source = _load(SOURCE_HELPERS, "presence_source_helpers")
    lifecycle = _load(LIFECYCLE_HELPERS, "presence_lifecycle_helpers")
    smoke_root = Path(
        tempfile.mkdtemp(prefix="lantai-persona-presence-smoke-"),
    )
    process: subprocess.Popen[str] | None = None
    started_at = time.time()
    try:
        working_dir, secret_dir, configured = source._stage_profile(
            source_working_dir=source_working_dir.expanduser().resolve(
                strict=True,
            ),
            source_secret_dir=source_secret_dir.expanduser().resolve(
                strict=True,
            ),
            agent_id=agent_id,
            target_root=smoke_root,
        )
        configured = _override_staged_model(
            working_dir=working_dir,
            secret_dir=secret_dir,
            configured=configured,
            provider_id=provider_id,
            model=model,
        )
        port = source._available_port()
        base_url = f"http://127.0.0.1:{port}"
        desktop_token = "persona-presence-smoke-token"
        log_path = smoke_root / "backend.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            project_python = ROOT / ".venv" / "bin" / "python"
            runtime_python = (
                str(project_python)
                if project_python.is_file()
                else sys.executable
            )
            # The process must outlive this statement and is stopped in finally.
            # pylint: disable-next=consider-using-with
            process = subprocess.Popen(
                [
                    runtime_python,
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
                    working_dir=working_dir,
                    secret_dir=secret_dir,
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
            session_id = f"console:persona-presence-{uuid.uuid4().hex[:12]}"
            topic_workspace_dir = (
                working_dir
                / "workspaces"
                / "default"
                / "topic-workspaces"
                / f"persona-presence-{uuid.uuid4().hex[:12]}"
            )
            topic_workspace_dir.mkdir(parents=True)
            material_path = (
                topic_workspace_dir / "presence-progress-material.txt"
            )
            material_path.write_text(
                "报告正文候选：2.1.17\n安装包元数据：2.1.18\n",
                encoding="utf-8",
            )
            status, _body, seeded_chat = lifecycle._json_request(
                "POST",
                f"{base_url}/api/chats",
                payload={
                    "name": "persona presence source smoke",
                    "session_id": session_id,
                    "user_id": "default",
                    "channel": "console",
                    "meta": {"topic_workspace_dir": str(topic_workspace_dir)},
                },
                headers=lifecycle._state_headers(
                    {"desktop_token": desktop_token},
                ),
                timeout=timeout,
            )
            if (
                status != 200
                or not isinstance(seeded_chat, dict)
                or not isinstance(seeded_chat.get("id"), str)
            ):
                raise AssertionError(
                    f"Presence smoke could not seed its topic workspace: {seeded_chat!r}",
                )
            turns = (
                (
                    "tool",
                    f"请先实际读取 {material_path}。"
                    "我要知道两行版本号为什么不一致。不要修改文件；"
                    "读完再给我一个简短判断。公开进展只说文件名和当前动作，"
                    "不要复述完整路径。",
                ),
                (
                    "followup",
                    "你刚才已经看到这两个版本。现在只告诉我应该先核对哪一处；" + "不需要再读文件，也不要调用工具。",
                ),
            )
            turn_ids: list[str] = []
            elapsed_ms: list[int] = []
            for index, (_kind, prompt) in enumerate(turns, start=1):
                turn_token = uuid.uuid4().hex[:12]
                payload = source._configured_provider_request_payload(
                    turn_token,
                    session_id=session_id,
                    prompt=prompt,
                )
                client_turn_id = str(payload["client_turn_id"])
                turn_started = time.monotonic()
                status, sse, _parsed = lifecycle._json_request(
                    "POST",
                    f"{base_url}/api/console/chat",
                    payload=payload,
                    headers=lifecycle._state_headers(
                        {"desktop_token": desktop_token},
                    ),
                    timeout=timeout,
                )
                if status != 200:
                    raise AssertionError(
                        f"Presence smoke turn {index} returned HTTP {status}",
                    )
                payloads = lifecycle._extract_sse_payloads(sse)
                _assert_completed(payloads, turn=index)
                turn_ids.append(client_turn_id)
                elapsed_ms.append(
                    round((time.monotonic() - turn_started) * 1000),
                )

            _status, _body, chats = lifecycle._json_request(
                "GET",
                f"{base_url}/api/chats?mode=all",
                headers=lifecycle._state_headers(
                    {"desktop_token": desktop_token},
                ),
                timeout=timeout,
            )
            chat_id = lifecycle._find_chat_id(chats, session_id)
            _status, _body, events_payload = lifecycle._json_request(
                "GET",
                f"{base_url}/api/chats/{chat_id}/events?after_seq=0",
                headers=lifecycle._state_headers(
                    {"desktop_token": desktop_token},
                ),
                timeout=timeout,
            )
        events = (
            events_payload.get("events")
            if isinstance(events_payload, dict)
            else None
        )
        if not isinstance(events, list) or not events:
            raise AssertionError(
                "Presence smoke did not persist runtime events",
            )
        tool_progress, tool_ids = _progress_evidence(
            events,
            client_turn_id=turn_ids[0],
        )
        followup_progress, followup_tool_ids = _progress_evidence(
            events,
            client_turn_id=turn_ids[1],
        )
        if not tool_ids:
            raise AssertionError(
                "Real provider did not execute the requested read tool",
            )
        if followup_tool_ids or followup_progress:
            raise AssertionError(
                "No-tool follow-up emitted a tool or public work-presence event",
            )
        return {
            "schema": "com.lantai.source-persona-presence-smoke.v1",
            "ok": True,
            "started_at": started_at,
            "finished_at": time.time(),
            "configured_model": {
                "provider_id": configured["provider_id"],
                "model": configured["model"],
                "agent_id": agent_id,
            },
            "constitution_version": CONSTITUTION_VERSION,
            "chat_request_count": 2,
            "turn_count": 2,
            "turn_elapsed_ms": elapsed_ms,
            "primary_model_round_count": [
                _primary_model_round_count(events, client_turn_id=turn_id)
                for turn_id in turn_ids
            ],
            "usage": [
                _usage_totals(events, client_turn_id=turn_id)
                for turn_id in turn_ids
            ],
            "tool_call_count": len(tool_ids),
            "public_progress_count": len(tool_progress),
            "public_progress": tool_progress,
            "no_tool_followup_progress_count": len(followup_progress),
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
    parser.add_argument("--provider-id")
    parser.add_argument("--model")
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args(argv)
    result = run(
        source_working_dir=args.source_working_dir,
        source_secret_dir=args.source_secret_dir,
        agent_id=args.agent_id,
        timeout=args.timeout,
        provider_id=args.provider_id,
        model=args.model,
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
