# -*- coding: utf-8 -*-
"""Lightweight failure attribution for Runtime events."""

from __future__ import annotations

import json
from typing import Any

MAX_DIAGNOSIS_TEXT = 180

_STRUCTURED_FAILURE_DIAGNOSES: dict[str, tuple[str, str, str]] = {
    "invalid_parameter_error": (
        "model_request",
        "模型服务拒绝了请求参数",
        "检查 Provider 与模型的参数兼容性，修正请求后再重试。",
    ),
    "invalid_request_error": (
        "model_request",
        "模型服务拒绝了请求参数",
        "检查 Provider 与模型的参数兼容性，修正请求后再重试。",
    ),
    "model_first_event_timeout": (
        "model_first_event",
        "模型服务长时间未开始响应",
        "重试当前请求；若仍未开始响应，再检查 Provider 状态或切换可用模型。",
    ),
    "model_stream_idle_timeout": (
        "model_stream_idle",
        "模型响应中途长时间中断",
        "从最近 checkpoint 恢复已完成阶段；保留现有产物，不要从头重复执行。",
    ),
    "model_output_truncated": (
        "model_output",
        "模型单次输出达到上限",
        "保留已有进度和成功工具结果，从中断处继续；不要从头重复分析或执行。",
    ),
    "artifact_not_created": (
        "artifact",
        "未生成要求的文件",
        "回到生成阶段并继续恢复；目标文件存在、验证并交付前不得标记完成。",
    ),
    "required_capability_failed": (
        "capability",
        "必需能力执行失败",
        "确认失败的必需能力和最近 checkpoint，再从该阶段恢复或请求必要授权。",
    ),
}


def _event_payload(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    return payload if isinstance(payload, dict) else {}


def short_diagnosis_text(value: Any, limit: int = MAX_DIAGNOSIS_TEXT) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}..."


def _string_payload_field(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _event_stage(event: Any) -> str:
    payload = _event_payload(event)
    return (
        _string_payload_field(payload, "trace_title")
        or _string_payload_field(payload, "tool_name", "command", "summary")
        or str(getattr(event, "type", "") or "").strip()
        or "运行事件"
    )


def _payload_text_fields(payload: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _event_failure_text(event: Any) -> str:
    payload = _event_payload(event)
    error = payload.get("error")
    nested = error if isinstance(error, dict) else {}
    provider_error = _provider_error_details(payload)
    parts = [
        *_payload_text_fields(provider_error, "message"),
        *_payload_text_fields(nested, "message"),
        *_payload_text_fields(
            payload,
            "failure_reason",
            "error",
            "error_text",
            "stderr",
            "stdout",
            "trace_detail",
            "detail",
            "output_summary",
            "result_summary",
            "result_preview",
            "command",
        ),
        str(getattr(event, "summary", "") or "").strip(),
        str(getattr(event, "type", "") or "").strip(),
    ]
    return "\n".join(part for part in parts if part)


def _provider_error_details(payload: dict[str, Any]) -> dict[str, Any]:
    """Read a bounded JSON/SSE error retained inside a redacted model failure.

    Command output is deliberately excluded: its JSON is not a Provider error.
    """

    error = payload.get("error")
    nested = error if isinstance(error, dict) else {}
    message = _string_payload_field(nested, "message")
    if not message or len(message) > 8192:
        return {}
    if message.startswith("data:"):
        message = message[5:].strip()
    try:
        decoded = json.loads(message)
    except (TypeError, ValueError):
        return {}
    provider_error = decoded.get("error") if isinstance(decoded, dict) else None
    return provider_error if isinstance(provider_error, dict) else {}


def _event_error_codes(payload: dict[str, Any]) -> tuple[str, ...]:
    """Return structured codes from most specific to most general."""

    error = payload.get("error")
    nested = error if isinstance(error, dict) else {}
    provider_error = _provider_error_details(payload)
    values = (
        nested.get("cause_code"),
        payload.get("cause_code"),
        nested.get("error_code"),
        payload.get("error_code"),
        nested.get("code"),
        provider_error.get("code"),
        provider_error.get("type"),
    )
    codes: list[str] = []
    for value in values:
        code = str(value or "").strip().lower()
        if code and code not in codes:
            codes.append(code)
    return tuple(codes)


def _structured_failure_diagnosis(
    payload: dict[str, Any],
) -> tuple[str, str, str, str] | None:
    for error_code in _event_error_codes(payload):
        diagnosis = _STRUCTURED_FAILURE_DIAGNOSES.get(error_code)
        if diagnosis is not None:
            category, summary, next_action = diagnosis
            return category, summary, next_action, error_code
    return None


def _first_evidence_line(text: str) -> str:
    for line in text.splitlines():
        candidate = short_diagnosis_text(line, 140)
        if candidate:
            return candidate
    return ""


def _event_looks_failed(event: Any) -> bool:
    payload = _event_payload(event)
    status = str(getattr(event, "status", "") or "").strip().lower()
    if status in {"failed", "stalled"}:
        return True
    exit_code = payload.get("exit_code")
    if exit_code not in (None, "", 0, "0"):
        return True
    return bool(
        _string_payload_field(
            payload,
            "failure_reason",
            "error",
            "error_text",
        ),
    )


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def failure_diagnosis_for_event(event: Any) -> dict[str, str] | None:
    """Return a short user-facing diagnosis for a failed Runtime event."""

    if not _event_looks_failed(event):
        return None

    payload = _event_payload(event)
    text = _event_failure_text(event)
    lower = text.lower()
    phase = _string_payload_field(payload, "phase").lower()
    command = _string_payload_field(payload, "command").lower()

    category = "command"
    summary = "命令或工具执行失败"
    next_action = "先复现同一命令，保留最小失败输出，再修复首个确定性错误。"
    structured_error_code = ""

    structured = _structured_failure_diagnosis(payload)
    if structured is not None:
        category, summary, next_action, structured_error_code = structured
    elif _contains_any(
        lower,
        (
            "permission denied",
            "operation not permitted",
            "not allowed",
            "approval",
            "risk",
            "sandbox",
            "app store",
            "blocked",
            "被阻断",
        ),
    ):
        category = "blocked"
        summary = "权限、审核或策略阻断"
        next_action = "不要绕过策略；先说明被阻断能力和必要性，改用低风险路径或等待用户确认。"
    elif _contains_any(
        lower,
        (
            "module not found",
            "cannot find module",
            "no module named",
            "importerror",
            "modulenotfounderror",
            "package not found",
            "missing dependency",
        ),
    ):
        category = "dependency"
        summary = "依赖或导入缺失"
        next_action = "先确认依赖是否应存在于当前环境，再按项目锁文件和既有包管理器补齐或修正 import 路径。"
    elif _contains_any(
        command,
        ("pytest", "vitest", "jest", "npm test", "pnpm test", "yarn test"),
    ) or _contains_any(
        lower,
        (
            "assertionerror",
            "assertion failed",
            "expected",
            "received",
            "tests failed",
            "test failed",
            "failed tests",
            "断言失败",
        ),
    ):
        category = "test"
        summary = "测试失败"
        next_action = "先重跑同一最小测试，定位首个 assertion/expected/received 差异，再改实现或测试夹具。"
    elif _contains_any(command, ("build", "tsc", "vite build")) or _contains_any(
        lower,
        (
            "typescript",
            "ts",
            "type error",
            "typeerror",
            "syntaxerror",
            "compile",
            "compilation failed",
            "vite build",
            "rollup",
            "webpack",
        ),
    ):
        category = "build"
        summary = "构建、类型检查或编译失败"
        next_action = "先找最靠前的编译错误和对应文件行，修完后重跑同一 build/typecheck 命令。"
    elif phase == "preview" or _contains_any(
        lower,
        (
            "preview",
            "playwright",
            "page.goto",
            "net::err",
            "err_connection_refused",
            "failed to fetch",
            "localhost",
            "127.0.0.1",
            "vite dev server",
        ),
    ):
        category = "preview"
        summary = "前端预览或浏览器验证失败"
        next_action = "先确认 dev server 端口和页面控制台首个错误，再修复导致页面不可访问或空白的根因。"
    elif _contains_any(
        lower,
        ("timeout", "timed out", "stalled", "no new event", "没有新的运行事件", "超时"),
    ):
        category = "timeout"
        summary = "执行超时或运行卡住"
        next_action = "先确认进程是否还活着和最后输出点；若无活动任务，从最后已完成步骤继续。"

    stage = _event_stage(event)
    seq = getattr(event, "seq", None)
    label = f"#{seq} {stage}" if seq is not None else stage
    evidence = _first_evidence_line(text)
    diagnosis = {
        "label": short_diagnosis_text(label, 96),
        "category": category,
        "summary": summary,
        "next_action": next_action,
        "evidence": evidence,
    }
    if structured_error_code:
        diagnosis["error_code"] = structured_error_code
    return diagnosis


def failure_diagnosis_lines_for_events(
    events: list[Any] | None,
    *,
    max_events: int = 6,
    max_diagnoses: int = 3,
) -> list[str]:
    """Return compact diagnosis lines for the latest failed events."""

    if not events:
        return []
    diagnoses: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for event in reversed(events[-max_events:]):
        diagnosis = failure_diagnosis_for_event(event)
        if diagnosis is None:
            continue
        key = (diagnosis["category"], diagnosis["summary"])
        if key in seen:
            continue
        seen.add(key)
        diagnoses.append(diagnosis)
        if len(diagnoses) >= max_diagnoses:
            break
    lines: list[str] = []
    for diagnosis in reversed(diagnoses):
        line = (
            f"- {diagnosis['label']} -> {diagnosis['summary']}"
            f" [{diagnosis['category']}]；建议: {diagnosis['next_action']}"
        )
        if diagnosis["evidence"]:
            line += f"；证据: {diagnosis['evidence']}"
        lines.append(line)
    return lines
