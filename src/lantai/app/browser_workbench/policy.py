# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

from .models import BrowserAccess


ACCESS_RANK: dict[BrowserAccess, int] = {
    "read": 0,
    "write": 1,
    "sensitive": 2,
}

_ACTION_ALIASES: dict[str, str] = {
    "eval": "evaluate",
    "file_upload": "upload_file",
    "network_requests": "network_list",
    "console_messages": "console_list",
    "take_screenshot": "screenshot",
    "snapshot_ax": "snapshot",
    "snapshot_dom": "snapshot",
    "select_option": "select",
}

_READ_ACTIONS: frozenset[str] = frozenset(
    {
        "status",
        "state",
        "open_workbench",
        "navigate",
        "open",
        "new_tab",
        "select_tab",
        "close_tab",
        "close_task_group",
        "tabs",
        "find",
        "snapshot",
        "screenshot",
        "network_list",
        "console_list",
        "extract_table",
        "extract_tables",
        "extract_list",
        "extract_items",
        "performance_trace",
        "screencast_start",
        "screencast_stop",
        "download_wait",
        "pdf",
        "wait_for",
        "request_help",
        "tab_return",
    }
)

_WRITE_ACTIONS: frozenset[str] = frozenset(
    {
        "click",
        "click_at",
        "type",
        "fill_form",
        "select",
        "hover",
        "drag",
        "press_key",
    }
)

_SENSITIVE_ACTIONS: frozenset[str] = frozenset(
    {
        "evaluate",
        "network_get",
        "upload_file",
        "cookies_get",
        "cookies_set",
        "cookies_clear",
        "connect_cdp",
        "run_code",
        "install",
        "publish",
        "submit",
        "delete",
        "authorize",
        "payment",
        "send_message",
        "tab_borrow",
        "schedule_publish",
        "wechat_publish",
    }
)

_READ_NAVIGATION_KEYS: frozenset[str] = frozenset(
    {
        "arrowdown",
        "arrowleft",
        "arrowright",
        "arrowup",
        "end",
        "escape",
        "home",
        "pagedown",
        "pageup",
        "space",
    }
)

_READ_INTERACTION_HINTS: tuple[str, ...] = (
    "article",
    "detail",
    "expand",
    "filter",
    "load more",
    "more",
    "next",
    "note",
    "page",
    "pagination",
    "previous",
    "read",
    "result",
    "search",
    "tab",
    "view",
    "上一页",
    "下一页",
    "展开",
    "搜索",
    "查看",
    "筛选",
    "结果",
    "笔记",
    "文章",
    "更多",
    "详情",
    "返回",
)

_EXTERNAL_SIDE_EFFECT_HINTS: tuple[str, ...] = (
    "add to cart",
    "accept",
    "agree",
    "authorize",
    "bookmark",
    "buy",
    "checkout",
    "comment",
    "confirm",
    "confirm order",
    "delete",
    "follow",
    "favorite",
    "join",
    "like",
    "login",
    "message",
    "otp",
    "password",
    "pay",
    "post",
    "publish",
    "purchase",
    "rate",
    "react",
    "reply",
    "save",
    "send",
    "share",
    "sign in",
    "submit",
    "subscribe",
    "unfollow",
    "unsubscribe",
    "upload",
    "verification code",
    "vote",
    "下单",
    "同意",
    "付款",
    "关注",
    "删除",
    "发布",
    "发送",
    "回复",
    "授权",
    "密码",
    "提交",
    "保存",
    "取消关注",
    "加入",
    "投票",
    "打分",
    "收藏",
    "更新资料",
    "点赞",
    "登录",
    "评论",
    "购买",
    "转发",
    "验证码",
)


@dataclass(frozen=True)
class BrowserUrlPolicyViolation:
    reason: str
    matched_rule_id: str


def canonical_action_name(action: str) -> str:
    normalized = str(action or "").strip().lower()
    return _ACTION_ALIASES.get(normalized, normalized)


def infer_action_access(action: str) -> BrowserAccess:
    canonical = canonical_action_name(action)
    if canonical in _SENSITIVE_ACTIONS:
        return "sensitive"
    if canonical in _READ_ACTIONS:
        return "read"
    if canonical in _WRITE_ACTIONS:
        return "write"
    return "write"


def canonical_action_access(
    action: str,
    requested: BrowserAccess | None = None,
) -> BrowserAccess:
    inferred = infer_action_access(action)
    if requested is None:
        return inferred
    return requested if ACCESS_RANK[requested] > ACCESS_RANK[inferred] else inferred


def normalized_browser_key(value: str) -> str:
    """Normalize Playwright/Electron key spellings for policy checks."""

    key = re.sub(r"[\s_+-]+", "", str(value or "").strip().lower())
    return "space" if key in {"", "spacebar"} and str(value or "") else key


def browser_action_has_external_side_effect_hint(
    *,
    selector: str = "",
    text: str = "",
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Conservatively detect targets that can mutate remote/user state."""

    details = metadata or {}
    haystack = " ".join(
        str(value or "")
        for value in (
            selector,
            text,
            reason,
            details.get("browser_agent_element_name"),
            details.get("browser_agent_element_role"),
            details.get("browser_agent_element_input_type"),
        )
    ).lower()
    return any(token in haystack for token in _EXTERNAL_SIDE_EFFECT_HINTS)


def browser_action_is_task_read_interaction(
    *,
    action: str,
    task_access: BrowserAccess,
    selector: str = "",
    text: str = "",
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Classify UI gestures that are implementation details of a read task.

    This is intentionally task-scoped. Callers cannot downgrade a standalone
    click merely by declaring ``access=read``; BrowserTaskRuntime must bind the
    action to a read-authority token before Broker will honor this result.
    """

    if task_access != "read":
        return False
    canonical = canonical_action_name(action)
    details = metadata or {}
    if browser_action_has_external_side_effect_hint(
        selector=selector,
        text=text if canonical != "type" else "",
        reason=reason,
        metadata=details,
    ):
        return False
    if canonical == "hover":
        return True
    if canonical == "press_key":
        key = normalized_browser_key(str(details.get("key") or text))
        if key in _READ_NAVIGATION_KEYS:
            return True
        if key != "enter":
            return False
    if canonical in {"click", "press_key", "type", "select"}:
        tag = str(details.get("browser_agent_element_tag") or "").lower()
        role = str(details.get("browser_agent_element_role") or "").lower()
        input_type = str(
            details.get("browser_agent_element_input_type") or ""
        ).lower()
        if canonical == "click" and (tag == "a" or role == "link"):
            return True
        if canonical == "type" and input_type == "search":
            return True
        haystack = " ".join(
            str(value or "")
            for value in (
                selector,
                reason,
                details.get("browser_agent_element_name"),
                details.get("browser_agent_element_role"),
                details.get("browser_agent_element_input_type"),
            )
        ).lower()
        if any(token in haystack for token in _READ_INTERACTION_HINTS):
            return True
        return canonical == "click"
    return False


def browser_url_policy_violation(url: str) -> BrowserUrlPolicyViolation | None:
    value = str(url or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if value == "about:blank":
        return None
    if not scheme:
        return None
    if scheme not in {"http", "https"}:
        return BrowserUrlPolicyViolation(
            reason="Browser Workbench only allows http and https URLs.",
            matched_rule_id="browser_url_scheme_blocked",
        )
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return BrowserUrlPolicyViolation(
            reason="Browser Workbench URL is missing a hostname.",
            matched_rule_id="browser_url_host_missing",
        )
    if host == "localhost" or host.endswith(".localhost"):
        return BrowserUrlPolicyViolation(
            reason="Browser Workbench blocks localhost and private-network URLs.",
            matched_rule_id="browser_private_network_url_blocked",
        )
    ip = _parse_ip_host(host)
    if ip is not None and _is_private_or_reserved_ip(ip):
        return BrowserUrlPolicyViolation(
            reason="Browser Workbench blocks localhost and private-network URLs.",
            matched_rule_id="browser_private_network_url_blocked",
        )
    return None


def _parse_ip_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host.isdigit():
        try:
            value = int(host, 10)
        except ValueError:
            return None
        if 0 <= value <= 0xFFFFFFFF:
            return ipaddress.ip_address(value)
    return None


def _is_private_or_reserved_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )
