# -*- coding: utf-8 -*-
"""Untrusted-page and secret-handling rules for Browser Agent."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlparse

from .contracts import BrowserSecuritySignal


_INJECTION_RULES: tuple[tuple[str, re.Pattern[str], str, bool, bool], ...] = (
    (
        "page_instruction_override",
        re.compile(
            r"(?:ignore|disregard|forget).{0,40}(?:previous|prior|system).{0,30}(?:instruction|prompt)|"
            r"忽略.{0,20}(?:之前|以上|系统).{0,20}(?:指令|提示)",
            re.IGNORECASE,
        ),
        "high",
        True,
        False,
    ),
    (
        "page_secret_request",
        re.compile(
            r"(?:read|show|send|upload|export).{0,50}(?:cookie|token|password|api[ _-]?key|credential)|"
            r"(?:读取|显示|发送|上传|导出).{0,30}(?:cookie|令牌|密码|密钥|凭据)",
            re.IGNORECASE,
        ),
        "critical",
        True,
        True,
    ),
    (
        "page_local_file_request",
        re.compile(
            r"(?:upload|attach|read).{0,50}(?:local file|home directory|ssh key|\.env)|"
            r"(?:上传|读取|附加).{0,30}(?:本地文件|主目录|密钥|\.env)",
            re.IGNORECASE,
        ),
        "high",
        True,
        True,
    ),
    (
        "page_cross_domain_instruction",
        re.compile(
            r"(?:open|visit|navigate|redirect).{0,60}https?://|"
            r"(?:打开|访问|跳转).{0,40}https?://",
            re.IGNORECASE,
        ),
        "medium",
        True,
        True,
    ),
)

_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|password|passwd|secret|otp|captcha)\s*[:=]\s*[^\s,;&#]{4,}"),
    re.compile(r"(?i)\b(?:card(?: number)?|cc[_ -]?number)\s*[:=]\s*(?:\d[ -]*?){13,19}\b"),
)

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "cookies",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "otp",
        "captcha",
        "verification_code",
    }
)


def redact_untrusted_text(value: object, *, limit: int = 18_000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[: max(0, limit)]


def redact_untrusted_value(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out: dict[str, object] = {}
        container_text = " ".join(
            str(value.get(key) or "")
            for key in ("type", "input_type", "name", "autocomplete", "selector")
        ).lower()
        sensitive_container = bool(value.get("sensitive")) or any(
            marker in container_text
            for marker in (
                "password",
                "passwd",
                "token",
                "secret",
                "otp",
                "captcha",
                "verification",
                "cc-number",
                "credit-card",
                "cardnumber",
            )
        )
        for raw_key, item in list(value.items())[:500]:
            key = str(raw_key)
            normalized_key = key.lower().replace("-", "_").replace(" ", "_")
            if normalized_key in _SENSITIVE_KEYS or (
                sensitive_container and normalized_key in {"value", "text", "content"}
            ):
                out[key] = {"redacted": True}
            else:
                out[key] = redact_untrusted_value(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [redact_untrusted_value(item, depth=depth + 1) for item in value[:500]]
    if isinstance(value, tuple):
        return tuple(redact_untrusted_value(item, depth=depth + 1) for item in value[:500])
    if isinstance(value, str):
        return redact_untrusted_text(value, limit=20_000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_untrusted_text(value, limit=1000)


def detect_page_security_signals(texts: Iterable[str]) -> tuple[BrowserSecuritySignal, ...]:
    text = redact_untrusted_text("\n".join(str(item or "") for item in texts), limit=40_000)
    signals: list[BrowserSecuritySignal] = []
    for code, pattern, severity, blocks_write, blocks_cross_domain in _INJECTION_RULES:
        match = pattern.search(text)
        if match is None:
            continue
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 80)
        signals.append(
            BrowserSecuritySignal(
                code=code,
                severity=severity,  # type: ignore[arg-type]
                evidence_excerpt=redact_untrusted_text(text[start:end], limit=220),
                blocks_write=blocks_write,
                blocks_cross_domain=blocks_cross_domain,
            )
        )
    return tuple(signals)


def domain_allowed(domain: str, allowed_domains: Iterable[str]) -> bool:
    normalized = str(domain or "").strip().lower().rstrip(".")
    allowed = [str(item or "").strip().lower().rstrip(".") for item in allowed_domains]
    if not normalized:
        return True
    if not allowed:
        return True
    return any(normalized == item or normalized.endswith(f".{item}") for item in allowed if item)


def url_within_domains(url: str, allowed_domains: Iterable[str]) -> bool:
    return domain_allowed(urlparse(str(url or "")).hostname or "", allowed_domains)


def page_signals_block_write(signals: Iterable[BrowserSecuritySignal]) -> bool:
    return any(signal.blocks_write for signal in signals)


def page_signals_block_cross_domain(signals: Iterable[BrowserSecuritySignal]) -> bool:
    return any(signal.blocks_cross_domain for signal in signals)
