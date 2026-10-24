#!/usr/bin/env python3
"""Verify companion depth and semantic work cards on one exact packaged app."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import parse

from packaged_app_lifecycle_smoke import (
    SMOKE_MODEL_ID,
    _app_metadata_for_evidence,
    _assert_desktop_readiness,
    _assert_ready_state,
    _console_electron_binary,
    _extract_json_object,
    _extract_sse_payloads,
    _find_app_executable,
    _find_chat_id,
    _is_new_desktop_instance,
    _json_request,
    _state_base_url,
    _state_headers,
    _stop_app,
    _wait_for_state,
    _write_json_file,
    _write_smoke_openai_provider_config,
)
from packaged_app_rapid_lifecycle_smoke import (
    _descendant_pids,
    _process_snapshot,
    _wait_for_process_cleanup,
)


SCHEMA = "com.lantai.packaged-companion-depth-smoke.v2"
TURN_RE = re.compile(r"LANTAI_DEPTH_USER_(\d{3})_([a-z0-9]+)")
QUQU_RE = re.compile(r"~>\[(?:lead|echo)\]\s*([^\n]+)")
ARTIFACT_TRIGGER = "LANTAI_DEPTH_ARTIFACT_MATRIX"
RESEARCH_TRIGGER = "LANTAI_DEPTH_RESEARCH_REPORT"
ARTIFACT_CASES = (
    {
        "kind": "creative",
        "title": "夜航",
        "source_text": "《夜航》\n\n江灯摇碎一船星，\n风送潮声入梦听。",
    },
    {
        "kind": "correspondence",
        "title": "给未来的你",
        "source_text": "《给未来的你》\n\n你好：\n愿你仍记得今天的光。\n此致\n沈行知",
    },
    {
        "kind": "copy",
        "title": "夏日茶案",
        "source_text": "《夏日茶案》\n\n一口清凉，把午后还给自己。\n今日限定，等你慢慢喝。",
    },
    {
        "kind": "script",
        "title": "雨夜站台",
        "source_text": "《雨夜站台》\n\n【外景·站台·夜】\n甲：车还会来吗？\n乙：会，我们再等一会儿。",
    },
)
ARTIFACT_RESPONSE = "\n\n---\n\n".join(
    str(item["source_text"]) for item in ARTIFACT_CASES
)
RESEARCH_REPORT = """# 供应商研究报告

## 样本与结论

本次核对了三家供应商在 2026 年 8 月的官方套餐页与近期实测，A 方案当前更适合小团队。

## 使用范围

这份结论只覆盖报告中的三家供应商，不代表整个市场。"""
RESEARCH_ADVISORY_BODY = (
    "定价和套餐范围会变动，发布前应重新核对报告中"
    "2026 年 8 月的小团队价格。"
)


def _post_answer_reflection_response(*, ququ_line: str = "") -> str:
    """Return the current model-owned post-answer reflection protocol.

    The packaged runtime deliberately rejects free-form prose here.  Keeping
    the loopback provider on the production JSON contract prevents a stale
    smoke stub from mistaking protocol rejection for Ququ de-duplication.
    """

    return json.dumps(
        {
            "persona_line": "",
            "ququ_line": ququ_line,
            "attention": "",
            "self_observation": "",
            "unresolved_thought": "",
            "relationship_stance": "steady",
            "inner_world_note": "",
        },
        ensure_ascii=False,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, dict):
                for key in ("text", "thinking", "refusal"):
                    if isinstance(item.get(key), str):
                        chunks.append(item[key])
        return "\n".join(chunks)
    return ""


def _request_text(payload: dict[str, Any]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    return "\n".join(
        _content_text(message.get("content"))
        for message in messages
        if isinstance(message, dict)
    )


def _latest_turn(payload: dict[str, Any]) -> int | None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        match = TURN_RE.search(_content_text(message.get("content")))
        if match:
            return int(match.group(1))
    return None


class _DepthProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LantaiDepthProvider/1.0"

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        path = parse.urlparse(self.path).path.rstrip("/")
        if path not in {"/v1/models", "/models"}:
            self._send_json(404, {"error": {"message": "not found"}})
            return
        self._send_json(
            200,
            {
                "object": "list",
                "data": [
                    {
                        "id": SMOKE_MODEL_ID,
                        "object": "model",
                        "owned_by": "lantai-depth-smoke",
                    }
                ],
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        path = parse.urlparse(self.path).path.rstrip("/")
        if path not in {"/v1/chat/completions", "/chat/completions"}:
            self._send_json(404, {"error": {"message": "not found"}})
            return
        content_length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(content_length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "invalid json"}})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": {"message": "invalid request"}})
            return

        request_text = _request_text(payload)
        turn = _latest_turn(payload)
        repair = "\u6b63\u6587\u5df2\u7ecf\u5b8c\u6210" in request_text and "~>[lead]" in request_text
        presentation_classifier = any(
            marker in request_text
            for marker in (
                "invisible JSON classifier",
                "invisible JSON presentation analyst",
            )
        )
        artifact_request = ARTIFACT_TRIGGER in request_text
        research_request = RESEARCH_TRIGGER in request_text
        research_classification = (
            presentation_classifier and RESEARCH_REPORT in request_text
        )
        baseline = 'baseline_selected="true"' in request_text
        run_token = str(getattr(self.server, "run_token"))
        response_text = "LANTAI_DEPTH_AUXILIARY_OK"
        record: dict[str, Any] = {
            "turn": turn,
            "repair": repair,
            "baseline_selected": baseline,
            "stream": payload.get("stream") is not False,
            "message_count": len(payload.get("messages") or []),
            "contains_first_turn": bool(
                re.search(rf"LANTAI_DEPTH_USER_001_{re.escape(run_token)}", request_text)
            ),
            "contains_prior_ququ": "LANTAI_DEPTH_QUQU_" in request_text,
        }
        lock = getattr(self.server, "record_lock")
        with lock:
            if research_classification:
                response_text = json.dumps(
                    {
                        "artifacts": [],
                        "advisory": {
                            "label": "注意",
                            "body": RESEARCH_ADVISORY_BODY,
                        },
                    },
                    ensure_ascii=False,
                )
                record["request_kind"] = "research_advisory_classifier"
            elif presentation_classifier:
                response_text = json.dumps(
                    {
                        "artifacts": [
                            {
                                "kind": item["kind"],
                                "title": item["title"],
                                "source_text": item["source_text"],
                            }
                            for item in ARTIFACT_CASES
                        ],
                        "advisory": None,
                    },
                    ensure_ascii=False,
                )
                record["request_kind"] = "artifact_classifier"
            elif repair and artifact_request:
                response_text = _post_answer_reflection_response()
                record["request_kind"] = "artifact_ququ_repair"
                record["repair_mode"] = "empty"
            elif artifact_request:
                response_text = ARTIFACT_RESPONSE
                record["request_kind"] = "artifact_main"
            elif research_request:
                response_text = RESEARCH_REPORT
                record["request_kind"] = "research_main"
            elif repair:
                self.server.repair_count += 1
                repair_index = int(self.server.repair_count)
                record["repair_index"] = repair_index
                if repair_index == 1:
                    response_text = _post_answer_reflection_response(
                        ququ_line=f"~>[echo] LANTAI_DEPTH_QUQU_FIRST_{run_token}",
                    )
                    record["repair_mode"] = "unique"
                elif repair_index == 2:
                    response_text = _post_answer_reflection_response(
                        ququ_line=f"~>[echo] LANTAI_DEPTH_QUQU_FIRST_{run_token}",
                    )
                    record["repair_mode"] = "repeat_previous"
                elif repair_index == 3:
                    response_text = _post_answer_reflection_response()
                    record["repair_mode"] = "empty"
                else:
                    response_text = _post_answer_reflection_response(
                        ququ_line=(
                            f"~>[echo] LANTAI_DEPTH_QUQU_{turn:03d}_{run_token}"
                            if turn is not None
                            else f"~>[echo] LANTAI_DEPTH_QUQU_AUX_{repair_index}_{run_token}"
                        ),
                    )
                    record["repair_mode"] = "unique"
            elif turn is not None:
                response_text = f"LANTAI_DEPTH_ANSWER_{turn:03d}_{run_token}"
                if not baseline and not self.server.bypass_injected:
                    response_text += (
                        f"\n\n~>[echo] LANTAI_DEPTH_QUQU_BYPASS_{run_token}"
                    )
                    self.server.bypass_injected = True
                    record["emotional_bypass_injected"] = True
            self.server.records.append(record)

        if payload.get("stream") is False:
            self._send_json(
                200,
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": str(payload.get("model") or SMOKE_MODEL_ID),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response_text,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"

        def write_frame(frame: bytes) -> None:
            self.wfile.write(f"{len(frame):X}\r\n".encode("ascii"))
            self.wfile.write(frame)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        def write_chunk(delta: dict[str, Any], finish_reason: str | None = None) -> None:
            event = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": str(payload.get("model") or SMOKE_MODEL_ID),
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": finish_reason,
                    }
                ],
            }
            write_frame(
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
            )

        try:
            write_chunk({"role": "assistant"})
            if not presentation_classifier:
                write_chunk({"reasoning_content": f"DEPTH_THINKING_{turn or 0:03d}"})
            midpoint = max(1, len(response_text) // 2)
            write_chunk({"content": response_text[:midpoint]})
            write_chunk({"content": response_text[midpoint:]})
            write_chunk({}, finish_reason="stop")
            write_frame(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


class _DepthProviderServer:
    def __init__(self, *, run_token: str) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _DepthProviderHandler)
        self._server.run_token = run_token  # type: ignore[attr-defined]
        self._server.records = []  # type: ignore[attr-defined]
        self._server.record_lock = threading.Lock()  # type: ignore[attr-defined]
        self._server.repair_count = 0  # type: ignore[attr-defined]
        self._server.bypass_injected = False  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    @property
    def records(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._server.records]  # type: ignore[attr-defined]

    def __enter__(self) -> "_DepthProviderServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _history_text(payload: Any) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return ""
    chunks: list[str] = []
    for message in payload["messages"]:
        if not isinstance(message, dict):
            continue
        chunks.append(_content_text(message.get("content")))
    return "\n".join(chunk for chunk in chunks if chunk)


def _history_role_counts(payload: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return counts
    for message in payload["messages"]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        counts[role] = counts.get(role, 0) + 1
    return counts


def _validate_long_history(payload: Any, *, turns: int, run_token: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AssertionError("long-chat history is not an object")
    if str(payload.get("status") or "") not in {"idle", "finished", "completed"}:
        raise AssertionError(f"long-chat history did not settle: {payload.get('status')!r}")
    if payload.get("pending_user_message"):
        raise AssertionError("long-chat history retained a stale pending message")
    history = _history_text(payload)
    missing: list[str] = []
    duplicates: list[str] = []
    for turn in range(1, turns + 1):
        for sentinel in (
            f"LANTAI_DEPTH_USER_{turn:03d}_{run_token}",
            f"LANTAI_DEPTH_ANSWER_{turn:03d}_{run_token}",
        ):
            occurrences = history.count(sentinel)
            if occurrences == 0:
                missing.append(sentinel)
            elif occurrences != 1:
                duplicates.append(f"{sentinel}:{occurrences}")
    if missing or duplicates:
        raise AssertionError(
            f"long-chat history mismatch: missing={missing} duplicates={duplicates}"
        )
    role_counts = _history_role_counts(payload)
    if role_counts.get("user") != turns:
        raise AssertionError(f"expected {turns} user messages, got {role_counts}")
    return {
        "turns": turns,
        "role_counts": role_counts,
        "missing_sentinel_count": 0,
        "duplicate_sentinel_count": 0,
        "pending_user_message": False,
        "status": str(payload.get("status") or ""),
    }


def _validate_ququ_outcomes(
    *, records: list[dict[str, Any]], history_text: str, turns: int, run_token: str
) -> dict[str, Any]:
    main = [item for item in records if item.get("turn") and not item.get("repair")]
    repairs = [item for item in records if item.get("repair") and item.get("turn")]
    if len(main) != turns:
        raise AssertionError(f"expected {turns} main requests, got {len(main)}")
    baseline = [item for item in main if item.get("baseline_selected") is True]
    if not (turns * 0.25 <= len(baseline) <= turns * 0.75):
        raise AssertionError(
            f"50% Ququ route escaped statistical release band: {len(baseline)}/{turns}"
        )
    if len(repairs) != len(baseline):
        raise AssertionError(
            f"baseline/repair mismatch: baseline={len(baseline)} repair={len(repairs)}"
        )
    modes = [str(item.get("repair_mode") or "") for item in repairs]
    if modes[:3] != ["unique", "repeat_previous", "empty"]:
        raise AssertionError(f"Ququ rejection probes did not execute: {modes[:3]}")
    first = f"LANTAI_DEPTH_QUQU_FIRST_{run_token}"
    bypass = f"LANTAI_DEPTH_QUQU_BYPASS_{run_token}"
    first_count = history_text.count(first)
    if first_count != 1:
        raise AssertionError(
            "repeated Ququ provider output reached history an unexpected number "
            f"of times: count={first_count} visible={QUQU_RE.findall(history_text)!r} "
            f"repairs={repairs!r}"
        )
    if history_text.count(bypass) != 1:
        raise AssertionError("emotional-insight bypass did not reach history exactly once")

    visible_lines = [match.strip() for match in QUQU_RE.findall(history_text)]
    expected_provider_lines = {first, bypass}
    for item in repairs[3:]:
        turn = item.get("turn")
        if isinstance(turn, int):
            expected_provider_lines.add(f"LANTAI_DEPTH_QUQU_{turn:03d}_{run_token}")
    unexpected = sorted(set(visible_lines) - expected_provider_lines)
    if unexpected:
        raise AssertionError(f"non-provider/local Ququ fallback appeared: {unexpected}")
    duplicate_lines = sorted(
        line for line in set(visible_lines) if visible_lines.count(line) > 1
    )
    if duplicate_lines:
        raise AssertionError(f"duplicate visible Ququ lines appeared: {duplicate_lines}")
    expected_visible = 1 + 1 + max(0, len(repairs) - 3)
    if len(visible_lines) != expected_visible:
        raise AssertionError(
            f"unexpected visible Ququ count: {len(visible_lines)} != {expected_visible}"
        )
    return {
        "turns": turns,
        "baseline_selected": len(baseline),
        "baseline_ratio": round(len(baseline) / turns, 4),
        "repair_requests": len(repairs),
        "visible_ququ_count": len(visible_lines),
        "emotional_bypass_visible": True,
        "repeat_rejected": True,
        "empty_repair_has_no_fallback": True,
        "unexpected_local_fallback_count": 0,
        "duplicate_visible_ququ_count": 0,
    }


def _validate_work_lineage(
    works: Any, *, original_id: str, version_two_id: str, version_three_id: str
) -> dict[str, Any]:
    if not isinstance(works, list):
        raise AssertionError("Works list is not an array")
    by_id = {
        str(item.get("id")): item for item in works if isinstance(item, dict) and item.get("id")
    }
    expected = {original_id, version_two_id, version_three_id}
    if set(by_id) != expected:
        raise AssertionError(f"Works lineage ids mismatch: {set(by_id)} != {expected}")
    first, second, third = (by_id[original_id], by_id[version_two_id], by_id[version_three_id])
    if (
        int(first.get("version") or 0) != 1
        or first.get("parent_work_id") is not None
        or first.get("lineage_root_id") != original_id
        or int(second.get("version") or 0) != 2
        or second.get("parent_work_id") != original_id
        or second.get("lineage_root_id") != original_id
        or int(third.get("version") or 0) != 3
        or third.get("parent_work_id") != version_two_id
        or third.get("lineage_root_id") != original_id
    ):
        raise AssertionError(f"Works lineage metadata is invalid: {works!r}")
    return {
        "version_count": 3,
        "versions": [1, 2, 3],
        "lineage_root_id": original_id,
        "parent_chain": [None, original_id, version_two_id],
    }


def _post_turn(
    state: dict[str, Any], *, session_id: str, turn: int, run_token: str, timeout: float
) -> None:
    client_turn_id = f"depth-turn-{turn:03d}-{run_token}"
    prompt = f"LANTAI_DEPTH_USER_{turn:03d}_{run_token}"
    status, sse_text, _parsed = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/console/chat",
        payload={
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
            "session_id": session_id,
            "user_id": "default",
            "channel": "console",
            "stream": True,
            "client_turn_id": client_turn_id,
            "meta": {
                "client_turn_id": client_turn_id,
                "deep_thinking": True,
            },
        },
        headers=_state_headers(state),
        timeout=timeout,
    )
    payloads = _extract_sse_payloads(sse_text)
    final = payloads[-1] if payloads else {}
    if status != 200 or final.get("status") != "completed":
        raise AssertionError(f"long-chat turn {turn} failed: HTTP {status}, final={final!r}")


def _post_artifact_matrix(
    state: dict[str, Any], *, session_id: str, run_token: str, timeout: float
) -> str:
    client_turn_id = f"depth-artifact-{run_token}"
    status, sse_text, _parsed = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/console/chat",
        payload={
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{ARTIFACT_TRIGGER}_{run_token}",
                        }
                    ],
                }
            ],
            "session_id": session_id,
            "user_id": "default",
            "channel": "console",
            "stream": True,
            "client_turn_id": client_turn_id,
            "meta": {
                "client_turn_id": client_turn_id,
                "deep_thinking": True,
            },
        },
        headers=_state_headers(state),
        timeout=timeout,
    )
    payloads = _extract_sse_payloads(sse_text)
    final = payloads[-1] if payloads else {}
    if status != 200 or final.get("status") != "completed":
        raise AssertionError(
            f"semantic-artifact matrix turn failed: HTTP {status}, final={final!r}"
        )
    _status, _body, chats = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/chats?mode=all",
        headers=_state_headers(state),
        timeout=timeout,
    )
    chat_id = _find_chat_id(chats, session_id)
    _status, _body, history = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/chats/{chat_id}",
        headers=_state_headers(state),
        timeout=timeout,
    )
    history_text = _history_text(history)
    if history_text.count(ARTIFACT_RESPONSE) != 1:
        raise AssertionError("semantic-artifact source response was not persisted exactly once")
    for item in ARTIFACT_CASES:
        source_text = str(item["source_text"])
        if history_text.count(source_text) != 1:
            raise AssertionError(
                f"semantic-artifact source changed or duplicated: {item['title']}"
            )
    return chat_id


def _post_research_report(
    state: dict[str, Any], *, session_id: str, run_token: str, timeout: float
) -> str:
    client_turn_id = f"depth-research-{run_token}"
    status, sse_text, _parsed = _json_request(
        "POST",
        f"{_state_base_url(state)}/api/console/chat",
        payload={
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{RESEARCH_TRIGGER}_{run_token}",
                        }
                    ],
                }
            ],
            "session_id": session_id,
            "user_id": "default",
            "channel": "console",
            "stream": True,
            "client_turn_id": client_turn_id,
            "meta": {
                "client_turn_id": client_turn_id,
                "deep_thinking": True,
            },
        },
        headers=_state_headers(state),
        timeout=timeout,
    )
    payloads = _extract_sse_payloads(sse_text)
    final = payloads[-1] if payloads else {}
    if status != 200 or final.get("status") != "completed":
        raise AssertionError(
            f"research-report turn failed: HTTP {status}, final={final!r}"
        )
    _status, _body, chats = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/chats?mode=all",
        headers=_state_headers(state),
        timeout=timeout,
    )
    chat_id = _find_chat_id(chats, session_id)
    _status, _body, history = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/chats/{chat_id}",
        headers=_state_headers(state),
        timeout=timeout,
    )
    if _history_text(history).count(RESEARCH_REPORT) != 1:
        raise AssertionError("research report was not persisted exactly once")
    return chat_id


def _get_chat_history(
    state: dict[str, Any], *, chat_id: str, timeout: float
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    _status, _body, payload = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/chats/{chat_id}",
        headers=_state_headers(state),
        timeout=timeout,
    )
    elapsed = time.monotonic() - started
    if not isinstance(payload, dict):
        raise AssertionError(f"chat history is not an object: {payload!r}")
    return payload, elapsed


def _create_work_versions(
    state: dict[str, Any], *, workspace_dir: Path, run_token: str, timeout: float
) -> tuple[dict[str, Any], dict[str, bytes]]:
    source_root = workspace_dir / "depth-work-source"
    source_root.mkdir(parents=True, exist_ok=True)
    source = source_root / "index.html"
    script = source_root / "app.js"
    source.write_text("<title>Depth V1</title>", encoding="utf-8")
    script.write_text("window.depth = 1", encoding="utf-8")
    originals = {"html": source.read_bytes(), "script": script.read_bytes()}
    title = f"Companion Depth Work {run_token}"

    def post_work(path: Path, *, source_chat_id: str | None = None) -> dict[str, Any]:
        _status, _body, payload = _json_request(
            "POST",
            f"{_state_base_url(state)}/api/works",
            payload={
                "path": str(path),
                "title": title,
                "source_chat_id": source_chat_id,
                "source_run_id": f"depth-work-{uuid.uuid4().hex[:8]}",
            },
            headers=_state_headers(state),
            timeout=timeout,
        )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise AssertionError(f"Works create failed: {payload!r}")
        return payload

    def edit_session(work_id: str) -> tuple[dict[str, Any], Path]:
        _status, _body, chat = _json_request(
            "POST",
            f"{_state_base_url(state)}/api/works/{work_id}/edit-session",
            payload={},
            headers=_state_headers(state),
            timeout=timeout,
        )
        meta = chat.get("meta") if isinstance(chat, dict) else None
        entry = Path(str((meta or {}).get("work_edit_entry_path") or ""))
        if not isinstance(chat, dict) or not chat.get("id") or not entry.is_file():
            raise AssertionError(f"Works edit session failed: {chat!r}")
        return chat, entry

    first = post_work(source)
    edit_two_chat, edit_two = edit_session(str(first["id"]))
    edit_two.write_text("<title>Depth V2</title>", encoding="utf-8")
    (edit_two.parent / "app.js").write_text("window.depth = 2", encoding="utf-8")
    second = post_work(edit_two, source_chat_id=str(edit_two_chat["id"]))

    edit_three_chat, edit_three = edit_session(str(second["id"]))
    edit_three.write_text("<title>Depth V3</title>", encoding="utf-8")
    (edit_three.parent / "app.js").write_text("window.depth = 3", encoding="utf-8")
    third = post_work(edit_three, source_chat_id=str(edit_three_chat["id"]))

    unchanged_chat, unchanged_entry = edit_session(str(third["id"]))
    unchanged = post_work(unchanged_entry, source_chat_id=str(unchanged_chat["id"]))
    if unchanged.get("id") != third.get("id"):
        raise AssertionError("unchanged edit branch created a duplicate Work version")
    if source.read_bytes() != originals["html"] or script.read_bytes() != originals["script"]:
        raise AssertionError("Works versioning modified the user's source files")
    if Path(str(first["path"])).read_bytes() != originals["html"]:
        raise AssertionError("Works V1 managed copy was modified")
    if Path(str(second["path"])).read_text(encoding="utf-8") != "<title>Depth V2</title>":
        raise AssertionError("Works V2 content mismatch")
    if Path(str(third["path"])).read_text(encoding="utf-8") != "<title>Depth V3</title>":
        raise AssertionError("Works V3 content mismatch")
    _status, _body, works = _json_request(
        "GET",
        f"{_state_base_url(state)}/api/works",
        headers=_state_headers(state),
        timeout=timeout,
    )
    lineage = _validate_work_lineage(
        works,
        original_id=str(first["id"]),
        version_two_id=str(second["id"]),
        version_three_id=str(third["id"]),
    )
    lineage.update(
        {
            "title": title,
            "source_preserved": True,
            "v1_preserved": True,
            "unchanged_edit_idempotent": True,
            "managed_content_versions": [1, 2, 3],
        }
    )
    return lineage, originals


def _run_renderer(
    state: dict[str, Any], *, chat_id: str, turns: int, run_token: str,
    expected_ququ_count: int, artifact_chat_id: str, research_chat_id: str,
    work_title: str, screenshot_dir: Path, timeout: float
) -> dict[str, Any]:
    script = Path(__file__).resolve().parents[1] / "console" / "electron" / "companionDepthRendererSmoke.cjs"
    command = [
        str(_console_electron_binary(Path(__file__).resolve().parents[1])),
        str(script),
        "--base-url", _state_base_url(state),
        "--desktop-token", _state_headers(state)["X-Lantai-Desktop-Token"],
        "--chat-id", chat_id,
        "--turns", str(turns),
        "--first-prompt", f"LANTAI_DEPTH_USER_001_{run_token}",
        "--last-prompt", f"LANTAI_DEPTH_USER_{turns:03d}_{run_token}",
        "--first-answer", f"LANTAI_DEPTH_ANSWER_001_{run_token}",
        "--last-answer", f"LANTAI_DEPTH_ANSWER_{turns:03d}_{run_token}",
        "--answer-prefix", "LANTAI_DEPTH_ANSWER_",
        "--ququ-prefix", "LANTAI_DEPTH_QUQU_",
        "--expected-ququ-count", str(expected_ququ_count),
        "--artifact-chat-id", artifact_chat_id,
        "--artifact-count", str(len(ARTIFACT_CASES)),
        "--research-chat-id", research_chat_id,
        "--research-advisory-label", "注意",
        "--research-advisory-body", RESEARCH_ADVISORY_BODY,
        "--work-title", work_title,
        "--work-version-count", "3",
        "--timeout-ms", str(max(1000, int(timeout * 1000))),
        "--screenshot-dir", str(screenshot_dir),
    ]
    result = subprocess.run(
        command,
        cwd=str(script.parents[1]),
        env={**os.environ, "ELECTRON_DISABLE_SECURITY_WARNINGS": "1"},
        text=True,
        capture_output=True,
        timeout=timeout + 30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "companion depth renderer failed: "
            + (result.stderr or result.stdout)[-12000:]
        )
    payload = _extract_json_object(result.stdout)
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise AssertionError(f"renderer result invalid: {payload!r}")
    return payload


def run(args: argparse.Namespace) -> dict[str, Any]:
    app_path = Path(args.app).expanduser().resolve()
    executable = _find_app_executable(app_path)
    evidence_path = Path(args.evidence_output).expanduser().resolve()
    screenshot_dir = Path(args.screenshot_dir).expanduser().resolve()
    run_token = uuid.uuid4().hex[:10]
    smoke_root = Path(tempfile.mkdtemp(prefix="lantai-companion-depth-"))
    user_data_dir = smoke_root / "user-data"
    workspace_dir = user_data_dir / "working" / "workspaces" / "default"
    state_path = smoke_root / "desktop-lifecycle-state.json"
    app_log_path = smoke_root / "app.log"
    started_at = _utc_now()
    process: subprocess.Popen[str] | None = None
    tracked_pids: set[int] = set()
    history_latencies: list[float] = []
    cleanup: dict[str, Any] = {"tracked_alive": {}, "matching_alive": {}}
    env = os.environ.copy()
    for key in ("LANTAI_WORKING_DIR", "LANTAI_SECRET_DIR", "LANTAI_DESKTOP_USER_DATA_DIR"):
        env.pop(key, None)
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
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        with _DepthProviderServer(run_token=run_token) as provider:
            _write_smoke_openai_provider_config(user_data_dir, base_url=provider.base_url)
            with app_log_path.open("w", encoding="utf-8") as app_log:
                process = subprocess.Popen(
                    [str(executable)],
                    cwd=str(app_path.parent),
                    env=env,
                    stdout=app_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                state = _wait_for_state(
                    state_path,
                    timeout=args.timeout,
                    predicate=lambda value: int(value.get("sequence") or 0) >= 1,
                    process=process,
                    expected_distribution="official",
                )
                _assert_ready_state(state, expected_distribution="official")
                _assert_desktop_readiness(state, timeout=args.timeout)
                tracked_pids.update({process.pid, int(state["backend_pid"])})
                session_id = f"console:companion-depth-{run_token}"
                chat_id = ""
                restart_proof: dict[str, Any] = {}

                for turn in range(1, args.turns + 1):
                    _post_turn(
                        state,
                        session_id=session_id,
                        turn=turn,
                        run_token=run_token,
                        timeout=args.timeout,
                    )
                    if not chat_id:
                        _status, _body, chats = _json_request(
                            "GET",
                            f"{_state_base_url(state)}/api/chats?mode=all",
                            headers=_state_headers(state),
                            timeout=args.timeout,
                        )
                        chat_id = _find_chat_id(chats, session_id)
                    if turn in {1, args.turns // 2, args.turns}:
                        partial, latency = _get_chat_history(
                            state, chat_id=chat_id, timeout=args.timeout
                        )
                        history_latencies.append(latency)
                        _validate_long_history(
                            partial, turns=turn, run_token=run_token
                        )
                    if turn == args.turns // 2:
                        previous_nonce = str(state.get("desktop_instance_nonce") or "")
                        snapshot = _process_snapshot()
                        old_pids = {process.pid, int(state["backend_pid"])}
                        old_pids.update(_descendant_pids(snapshot, process.pid))
                        _stop_app(process)
                        process = None
                        old_cleanup = _wait_for_process_cleanup(
                            tracked_pids=old_pids,
                            markers=(),
                            timeout=args.cleanup_timeout,
                        )
                        if old_cleanup["tracked_alive"]:
                            raise AssertionError(
                                f"old app process tree survived restart: {old_cleanup}"
                            )
                        state_path.unlink(missing_ok=True)
                        process = subprocess.Popen(
                            [str(executable)],
                            cwd=str(app_path.parent),
                            env=env,
                            stdout=app_log,
                            stderr=subprocess.STDOUT,
                            text=True,
                        )
                        state = _wait_for_state(
                            state_path,
                            timeout=args.timeout,
                            predicate=lambda value: _is_new_desktop_instance(
                                value, previous_nonce=previous_nonce
                            ),
                            process=process,
                            expected_distribution="official",
                        )
                        _assert_desktop_readiness(state, timeout=args.timeout)
                        tracked_pids.update({process.pid, int(state["backend_pid"])})
                        restored, latency = _get_chat_history(
                            state, chat_id=chat_id, timeout=args.timeout
                        )
                        history_latencies.append(latency)
                        _validate_long_history(
                            restored, turns=turn, run_token=run_token
                        )
                        restart_proof = {
                            "at_turn": turn,
                            "previous_nonce_changed": (
                                str(state.get("desktop_instance_nonce") or "")
                                != previous_nonce
                            ),
                            "history_preserved": True,
                            "old_process_tree_clean": True,
                        }

                history, latency = _get_chat_history(
                    state, chat_id=chat_id, timeout=args.timeout
                )
                history_latencies.append(latency)
                long_chat = _validate_long_history(
                    history, turns=args.turns, run_token=run_token
                )
                history_text = _history_text(history)
                ququ = _validate_ququ_outcomes(
                    records=provider.records,
                    history_text=history_text,
                    turns=args.turns,
                    run_token=run_token,
                )
                post_restart_main = [
                    item
                    for item in provider.records
                    if item.get("turn") == args.turns // 2 + 1
                    and not item.get("repair")
                ]
                restart_proof["model_context_preserved"] = bool(
                    post_restart_main
                    and post_restart_main[0].get("contains_first_turn")
                    and post_restart_main[0].get("contains_prior_ququ")
                )
                if not restart_proof["model_context_preserved"]:
                    raise AssertionError(
                        "post-restart model request lost prior long-chat/Ququ context"
                    )

                artifact_session_id = f"console:semantic-artifacts-{run_token}"
                artifact_chat_id = _post_artifact_matrix(
                    state,
                    session_id=artifact_session_id,
                    run_token=run_token,
                    timeout=args.timeout,
                )
                research_session_id = f"console:research-advisory-{run_token}"
                research_chat_id = _post_research_report(
                    state,
                    session_id=research_session_id,
                    run_token=run_token,
                    timeout=args.timeout,
                )
                works, _originals = _create_work_versions(
                    state,
                    workspace_dir=workspace_dir,
                    run_token=run_token,
                    timeout=args.timeout,
                )
                renderer = _run_renderer(
                    state,
                    chat_id=chat_id,
                    turns=args.turns,
                    run_token=run_token,
                    expected_ququ_count=ququ["visible_ququ_count"],
                    artifact_chat_id=artifact_chat_id,
                    research_chat_id=research_chat_id,
                    work_title=str(works["title"]),
                    screenshot_dir=screenshot_dir,
                    timeout=args.timeout,
                )
                long_chat.update(
                    {
                        "history_read_samples": len(history_latencies),
                        "history_read_max_seconds": round(max(history_latencies), 4),
                        "history_read_p95_seconds": round(
                            sorted(history_latencies)[
                                max(0, int(len(history_latencies) * 0.95) - 1)
                            ],
                            4,
                        ),
                        "renderer": renderer["long_chat"],
                        "restart": restart_proof,
                    }
                )
                if max(history_latencies) > 2.0:
                    raise AssertionError(
                        "long-chat history read exceeded 2s: "
                        f"max={max(history_latencies):.3f}s "
                        f"samples={[round(value, 4) for value in history_latencies]}"
                    )
                if float(renderer["long_chat"].get("ready_ms") or 0) > 8000:
                    raise AssertionError(
                        f"long-chat renderer exceeded 8s: {renderer['long_chat']!r}"
                    )

                evidence = {
                    "schema": SCHEMA,
                    "passed": True,
                    "started_at": started_at,
                    "completed_at": _utc_now(),
                    "app": _app_metadata_for_evidence(app_path),
                    "isolation": {
                        "temporary_user_data": True,
                        "updater_disabled": True,
                        "user_workspace_read": False,
                        "loopback_provider_only": True,
                    },
                    "long_chat": long_chat,
                    "ququ": ququ,
                    "semantic_artifacts": renderer["semantic_artifacts"],
                    "research_advisory": renderer["research_advisory"],
                    "works_lineage": works,
                    "renderer": renderer,
                    "provider": {
                        "main_request_count": len(
                            [
                                item
                                for item in provider.records
                                if item.get("turn") and not item.get("repair")
                            ]
                        ),
                        "repair_request_count": len(
                            [
                                item
                                for item in provider.records
                                if item.get("repair") and item.get("turn")
                            ]
                        ),
                        "artifact_classifier_request_count": len(
                            [
                                item
                                for item in provider.records
                                if item.get("request_kind") == "artifact_classifier"
                            ]
                        ),
                        "research_advisory_classifier_request_count": len(
                            [
                                item
                                for item in provider.records
                                if item.get("request_kind")
                                == "research_advisory_classifier"
                            ]
                        ),
                        "request_records": provider.records,
                    },
                    "screenshots": [
                        {
                            "path": str((screenshot_dir / name).resolve()),
                            "sha256": _sha256(screenshot_dir / name),
                        }
                        for name in (
                            "long-chat.png",
                            "semantic-artifacts.png",
                            "research-advisory.png",
                            "works-lineage.png",
                        )
                    ],
                }
                _write_json_file(evidence_path, evidence)
                return evidence
    finally:
        if process is not None:
            snapshot = _process_snapshot()
            tracked_pids.update(_descendant_pids(snapshot, process.pid))
        _stop_app(process)
        cleanup = _wait_for_process_cleanup(
            tracked_pids=tracked_pids,
            markers=(),
            timeout=args.cleanup_timeout,
        )
        if cleanup["tracked_alive"]:
            raise AssertionError(f"companion depth smoke left processes alive: {cleanup}")
        shutil.rmtree(smoke_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--turns", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--cleanup-timeout", type=float, default=30.0)
    parser.add_argument("--screenshot-dir", required=True)
    parser.add_argument("--evidence-output", required=True)
    args = parser.parse_args(argv)
    if args.turns < 12 or args.turns % 2:
        parser.error("--turns must be an even integer of at least 12")
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except Exception as exc:  # noqa: BLE001
        print(f"Packaged companion depth smoke failed: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
