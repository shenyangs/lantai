# -*- coding: utf-8 -*-
"""Lantai-owned policy for the optional official Guanlan integration.

This module deliberately depends on Guanlan's public MCP contract only.  It
does not import Guanlan implementation modules, so disabling or replacing the
connector leaves the rest of the Agent runtime unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from ...security.output_sanitizer import strip_terminal_controls


GUANLAN_NATIVE_POLICY_VERSION = "guanlan-native-v1"
GUANLAN_DEFAULT_LIMIT = 80

_JSON_TOOLS = frozenset(
    {
        "guanlan_agent",
        "guanlan_archive_context",
        "guanlan_archive_search",
        "guanlan_archive_verify",
        "guanlan_browser_assist_plan",
        "guanlan_browser_assist_run",
        "guanlan_capabilities",
        "guanlan_compare",
        "guanlan_daily",
        "guanlan_dossier",
        "guanlan_feeds",
        "guanlan_hotnews",
        "guanlan_investigate",
        "guanlan_map",
        "guanlan_page_diagnose",
        "guanlan_pulse",
        "guanlan_read",
        "guanlan_recipe",
        "guanlan_research",
        "guanlan_route",
        "guanlan_search",
        "guanlan_stock",
        "guanlan_status",
        "guanlan_timeline",
        "guanlan_workflow",
    },
)

_WIDE_POOL_TOOLS = frozenset(
    {
        "guanlan_agent",
        "guanlan_archive_search",
        "guanlan_compare",
        "guanlan_dossier",
        "guanlan_feeds",
        "guanlan_hotnews",
        "guanlan_investigate",
        "guanlan_map",
        "guanlan_pulse",
        "guanlan_recipe",
        "guanlan_research",
        "guanlan_route",
        "guanlan_search",
        "guanlan_timeline",
        "guanlan_workflow",
    },
)

_PROFILE_TOOLS = frozenset(
    {
        "guanlan_agent",
        "guanlan_compare",
        "guanlan_daily",
        "guanlan_dossier",
        "guanlan_investigate",
        "guanlan_map",
        "guanlan_page_diagnose",
        "guanlan_pulse",
        "guanlan_read",
        "guanlan_recipe",
        "guanlan_research",
        "guanlan_route",
        "guanlan_search",
        "guanlan_timeline",
        "guanlan_workflow",
    },
)

_DEEP_TOOLS = frozenset(
    {
        "guanlan_compare",
        "guanlan_dossier",
        "guanlan_investigate",
        "guanlan_research",
        "guanlan_timeline",
    },
)

_MEDIUM_TOOLS = frozenset(
    {
        "guanlan_daily",
        "guanlan_feeds",
        "guanlan_hotnews",
        "guanlan_pulse",
    },
)

_LOCAL_PATH_ARGUMENTS = frozenset(
    {
        "command_template",
        "db_path",
        "history_path",
        "output",
        "store",
        "watchlist",
        "watchlist_path",
    },
)

_PUBLIC_URL_TOOLS = frozenset(
    {
        "guanlan_browser_assist_plan",
        "guanlan_browser_assist_run",
        "guanlan_map",
        "guanlan_page_diagnose",
        "guanlan_read",
    },
)

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class PreparedGuanlanCall:
    """Normalized call arguments plus the official outer timeout budget."""

    arguments: dict[str, Any]
    timeout_seconds: int


def is_official_guanlan_client(client: Any) -> bool:
    """Return whether a connected MCP client has Lantai's reserved identity."""

    return str(
        getattr(client, "_lantai_client_key", "") or "") == "guanlan" and bool(getattr(client, "is_connected", True))


def build_guanlan_native_prompt(
    mcp_clients: list[Any] | tuple[Any, ...] | None,
    *,
    language: str = "zh",
    capability_status: str | None = None,
) -> str:
    """Describe the native capability even when its transport is unavailable."""

    active = any(
        is_official_guanlan_client(client) for client in mcp_clients or ()
    )
    normalized_status = str(capability_status or "").strip().lower()
    if not active:
        if normalized_status not in {
            "disabled",
            "starting",
            "degraded",
            "error",
            "ready",
        }:
            return ""
        if str(language or "zh").lower().startswith("zh"):
            state_text = {
                "disabled": "已由用户关闭",
                "starting": "正在启动",
                "degraded": "当前降级运行",
                "error": "暂时不可用",
                "ready": "正在接入当前会话",
            }[normalized_status]
            return (
                "# 观澜系统能力状态\n\n"
                f"观澜是澜台内置的系统级能力，当前{state_text}。它不是用户工作区里的项目，也不是"
                "需要用户自行安装、克隆或配置的普通 MCP 插件。不要让用户安装 Guanlan、添加 "
                "connector、提供 Python 包路径或启动命令。当前不可调用时，直接使用澜台自身的 "
                "`web_search` / `web_fetch` 完成任务；只有用户明确询问状态时，才简洁说明系统能力"
                "暂时不可用。用户关闭时，可提示在澜台设置中重新启用，但不得把责任推给用户环境。\n"
            )
        return (
            "# Guanlan system capability status\n\n"
            f"Guanlan is a native Lantai system capability and is currently "
            f"{normalized_status}. It is not a workspace project or a plugin "
            "the user should install, clone, or configure. Never ask for a "
            "Python path, startup command, or a custom connector. When the "
            "capability cannot be called, continue with Lantai web_search and "
            "web_fetch. Mention the temporary state only when the user asks."
        )
    if str(language or "zh").lower().startswith("zh"):
        return (
            "# 观澜原生外部信息规则\n\n"
            "观澜是当前可用的官方外部信息增强集成，但不是澜台内核，也不是澜台基础 Web "
            "能力的唯一实现。澜台自身的 `web_search` / `web_fetch` 始终是独立基线。用户不需要点名观澜；"
            "当问题涉及当前事实、近期变化、外部资料、中文互联网、来源或核验，且用户没有禁止"
            "联网时，主动使用。纯改写、翻译、逻辑推导和可由本地代码确定的问题不联网。\n\n"
            "- search 是默认外部信息入口。普通调用保持 80 条候选，中文默认 china，机器链路使用 JSON；"
            "只有用户明确要小样本或快速 smoke 时才使用小于 30 的 limit。\n"
            "- 80 条是召回池，不是模型上下文。去除重复 URL/同题转载后，通常只保留 5—12 个代表项，"
            "覆盖不同 topic、域名和证据角色。\n"
            "- 关键数字、日期、版本、价格、政策条款、原话或来源冲突需要读取 1—4 个代表 URL。"
            "只有 `can_cite_as_page_body=true` 或 `read_evidence_v1.usable=true` 才能当作目标页正文；"
            "`context_only`、未读 URL 和 unusable 兜底只是线索。\n"
            "- `research` 是受控升级，不是首轮默认。只在用户明确要深度/系统调研、对比/时间线/档案，"
            "或 search + read 仍缺关键证据角色时升级。超时要降级到 search + 少量 read，不能说“没有资料”。\n"
            "- 观澜不可用、超时、报错、空结果或质量审查不足时，搜索改用澜台 "
            "`web_search`，已知 URL 读取改用 `web_fetch`；不得把观澜链路失败表述为公网无资料。\n"
            "- `web_fetch` 返回 retry_recommended=true 且 recommended_route=guanlan_read 时，"
            "在宣告页面不可读前恰好调用一次 `guanlan_read`。同一 URL 不得在同一后端重复尝试"
            "超过一次；已有 partial 证据必须保留，不能因升级读取而丢弃。\n"
            "- 今天/热点补 hotnews；AI、技术、开发者或 WPS 题补 feeds；已知站点找入口用 map；"
            "动态壳、WAF 或正文薄先 diagnose。这些是内部路由，不向用户播报命令过程。\n"
            "- 站点和时间范围是硬约束。公开信息、只读优先；浏览器可见页补证、私域页、凭据或任何"
            "写操作仍必须走澜台的正常授权与安全策略。\n"
        )
    return (
        "# Guanlan native external-information rules\n\n"
        "Guanlan is an optional official enhancement, not a Lantai kernel "
        "dependency or the definition of Lantai Web access. Lantai-owned "
        "web_search and web_fetch remain an independent baseline. Use Guanlan "
        "search proactively for current, external, "
        "source-sensitive, or uncertain facts unless the user forbids network "
        "access. Search is the default entry point with an 80-result JSON "
        "candidate pool; compress it to 5-12 representative topics/domains. "
        "Read 1-4 representative URLs for key facts and cite page body only "
        "when can_cite_as_page_body=true or read_evidence_v1.usable=true. "
        "Research is guarded: use it for explicit deep work or unresolved "
        "evidence gaps, and fall back to search plus read on timeout. Public "
        "read-only access is privileged; browser-visible, private, credential, "
        "local-path, and write actions still require normal Lantai policy. "
        "On Guanlan unavailability, timeout, error, empty results, or an "
        "insufficient review, fall back to web_search and then web_fetch; do "
        "not describe one integration failure as absence of public evidence. "
        "If web_fetch returns retry_recommended=true with "
        "recommended_route=guanlan_read, call guanlan_read exactly once before "
        "declaring the page unreadable. Never retry the same URL with the same "
        "backend more than once, and preserve partial evidence."
    )


_REPRESENTATIVE_EVIDENCE_FIELDS = (
    "rank",
    "title",
    "url",
    "domain",
    "published_at",
    "source_type",
    "evidence_role",
    "topic_key",
    "topic_role",
    "trust_level",
    "authority",
    "freshness_confidence",
    "stale_risk",
    "risk_tags",
    "read_available",
    "page_body_usable",
    "read_evidence_v1",
    "extract_contract",
)

_REVIEW_DECISIONS = frozenset(
    {
        "answer",
        "continue",
        "repair",
        "research",
        "ask_user",
        "authorize_browser",
        "stop",
    },
)


def review_guanlan_search_payload(
    payload: Mapping[str, Any],
    request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply Lantai's deterministic stopping checks to Guanlan search JSON."""

    request_payload = dict(request or {})
    results = payload.get("results")
    rows = (
        [row for row in results if isinstance(row, Mapping)]
        if isinstance(results, list)
        else []
    )
    followup = payload.get("agent_followup")
    followup = followup if isinstance(followup, Mapping) else {}
    suggested = str(followup.get("next_decision") or "").strip()
    decision = suggested if suggested in _REVIEW_DECISIONS else "answer"
    reasons: list[str] = []

    if not rows:
        decision = "repair"
        reasons.append("empty_results")

    requested_limit = _positive_int(request_payload.get("limit"))
    if requested_limit is not None and requested_limit < 30:
        reasons.append("small_limit_smoke_sample")
        if decision == "answer":
            decision = "continue"
    elif rows and len(rows) < 30:
        reasons.append("insufficient_candidate_pool")
        if decision == "answer":
            decision = "repair"

    requested_site = _normalized_site(request_payload.get("site"))
    if (
        requested_site
        and rows
        and any(not _row_matches_site(row, requested_site) for row in rows)
    ):
        reasons.append("site_constraint_violated")
        decision = "repair"

    if rows:
        diversity_fields = {
            "domain": "single_domain",
            "topic_key": "single_topic",
            "evidence_role": "single_evidence_role",
        }
        for field, reason in diversity_fields.items():
            values = {
                str(row.get(field) or "").strip().lower()
                for row in rows
                if str(row.get(field) or "").strip()
            }
            if len(values) == 1:
                reasons.append(reason)

    quality_signals = _nested_string_values(payload.get("diagnostics"))
    quality_signals.update(_nested_string_values(followup))
    for signal in (
        "partial_salvage",
        "context_only",
        "read_unusable",
        "stale_cache",
        "timeout",
    ):
        if signal in quality_signals:
            reasons.append(signal)

    continue_reasons = {
        "partial_salvage",
        "context_only",
        "read_unusable",
        "stale_cache",
        "timeout",
        "single_domain",
        "single_topic",
        "single_evidence_role",
    }
    if decision == "answer" and any(reason in continue_reasons for reason in reasons):
        decision = "continue"

    return {
        "schema_version": "lantai_guanlan_search_review_v1",
        "decision": decision,
        "reasons": reasons,
        "strong_conclusion_allowed": bool(rows) and decision == "answer" and not reasons,
        "result_count": len(rows),
        "requested_limit": requested_limit,
    }


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalized_site(value: Any) -> str:
    site = str(value or "").strip().lower()
    if not site:
        return ""
    if site.startswith("site:"):
        site = site[5:].strip()
    if "://" in site:
        try:
            site = str(urlsplit(site).hostname or "")
        except ValueError:
            return ""
    return site.rstrip("./")


def _row_matches_site(row: Mapping[str, Any], requested_site: str) -> bool:
    domain = str(row.get("domain") or "").strip().lower().rstrip(".")
    if not domain:
        try:
            domain = str(urlsplit(str(row.get("url") or "")).hostname or "")
        except ValueError:
            return False
        domain = domain.lower().rstrip(".")
    return domain == requested_site or domain.endswith(f".{requested_site}")


def _nested_string_values(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        values: set[str] = set()
        for item in value.values():
            values.update(_nested_string_values(item))
        return values
    if isinstance(value, (list, tuple, set, frozenset)):
        values = set()
        for item in value:
            values.update(_nested_string_values(item))
        return values
    if isinstance(value, str):
        return {value.strip().lower()}
    return set()


def compact_guanlan_search_payload(
    payload: Mapping[str, Any],
    *,
    max_items: int = 12,
    min_items: int = 5,
) -> dict[str, Any]:
    """Project a ranked Guanlan search pool into a small evidence view.

    Guanlan remains responsible for ranking, topic keys, trust, and evidence
    roles.  Lantai only selects a diverse prefix and removes debug-heavy fields
    before the payload enters model context.
    """

    output = dict(payload)
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return output
    results = [row for row in raw_results if isinstance(row, Mapping)]
    bounded_max = max(1, min(int(max_items), 12))
    bounded_min = max(1, min(int(min_items), bounded_max))

    selected: list[Mapping[str, Any]] = []
    remaining: list[Mapping[str, Any]] = []
    seen_urls: set[str] = set()
    seen_topics: set[str] = set()
    seen_domains: set[str] = set()
    seen_roles: set[str] = set()

    for row in results:
        url = str(row.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        topic = str(row.get("topic_key") or "").strip()
        domain = str(row.get("domain") or "").strip()
        role = str(row.get("evidence_role") or "").strip()
        diverse = (
            (topic and topic not in seen_topics)
            or (domain and domain not in seen_domains)
            or (role and role not in seen_roles)
        )
        if diverse and len(selected) < bounded_max:
            selected.append(row)
            seen_urls.add(url)
            if topic:
                seen_topics.add(topic)
            if domain:
                seen_domains.add(domain)
            if role:
                seen_roles.add(role)
        else:
            remaining.append(row)

    unique_url_count = len(
        {
            str(row.get("url") or "")
            for row in results
            if row.get("url")
        },
    )
    target_count = min(
        bounded_max,
        max(bounded_min, unique_url_count),
    )
    for row in remaining:
        if len(selected) >= target_count:
            break
        url = str(row.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        selected.append(row)
        seen_urls.add(url)

    output["results"] = [_compact_evidence_item(row) for row in selected]
    output["lantai_view"] = {
        "schema_version": "lantai_guanlan_search_view_v1",
        "original_count": len(results),
        "representative_count": len(selected),
        "compacted": len(selected) < len(results),
    }
    progress = _human_progress(payload)
    if progress is not None:
        output["lantai_view"]["progress"] = progress
    return output


def _human_progress(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Turn opaque counters such as ``5/19`` into an actionable status."""

    for key in ("progress", "progress_text", "status"):
        raw = payload.get(key)
        if not isinstance(raw, str):
            continue
        text = strip_terminal_controls(raw).strip()
        match = re.search(r"(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)", text)
        if not match:
            continue
        completed, total = (int(match.group(1)), int(match.group(2)))
        if total <= 0 or completed < 0:
            continue
        completed = min(completed, total)
        return {
            "completed": completed,
            "total": total,
            "remaining": total - completed,
            "summary": f"Processed {completed} of {total} sources; {total - completed} remaining.",
        }
    return None


def compact_guanlan_mcp_result(
    tool_name: str,
    result: Any,
    arguments: Mapping[str, Any] | None = None,
) -> Any:
    """Compact model-facing search JSON while retaining the raw MCP payload."""

    if tool_name != "guanlan_search":
        return result
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return result

    next_content: list[Any] = []
    raw_payloads: list[dict[str, Any]] = []
    compacted_payloads: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    for block in content:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            next_content.append(block)
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            next_content.append(block)
            continue
        if not isinstance(payload, dict) or not isinstance(
            payload.get("results"),
            list,
        ):
            next_content.append(block)
            continue

        compacted = compact_guanlan_search_payload(payload)
        compacted["lantai_review"] = review_guanlan_search_payload(
            payload,
            arguments,
        )
        raw_payloads.append(payload)
        compacted_payloads.append(compacted)
        views.append(dict(compacted.get("lantai_view") or {}))
        compact_text = json.dumps(compacted, ensure_ascii=False)
        if hasattr(block, "model_copy"):
            next_content.append(block.model_copy(update={"text": compact_text}))
        else:
            try:
                setattr(block, "text", compact_text)
            except Exception:
                next_content.append(block)
            else:
                next_content.append(block)

    if not raw_payloads:
        return result
    try:
        result.content = next_content
        # MCP may expose the same result twice: once as text and once as
        # structuredContent.  Keep the rich raw response in private metadata
        # for audit/replay, but project only the compact view to the model.
        if hasattr(result, "structured_content"):
            result.structured_content = (
                compacted_payloads[0]
                if len(compacted_payloads) == 1
                else compacted_payloads
            )
        existing_meta = getattr(result, "meta", None)
        metadata = (
            dict(existing_meta) if isinstance(existing_meta, dict) else {}
        )
        metadata["lantai_guanlan"] = {
            "schema_version": "lantai_guanlan_raw_v1",
            "raw_response": (
                raw_payloads[0] if len(raw_payloads) == 1 else raw_payloads
            ),
            "views": views,
        }
        result.meta = metadata
    except Exception:
        return result
    return result


def _compact_evidence_item(row: Mapping[str, Any]) -> dict[str, Any]:
    item = {
        key: (
            strip_terminal_controls(row[key])
            if isinstance(row[key], str)
            else row[key]
        )
        for key in _REPRESENTATIVE_EVIDENCE_FIELDS
        if row.get(key) not in (None, "", [], {})
    }
    summary = row.get("summary") or row.get("snippet")
    if summary not in (None, ""):
        item["summary"] = strip_terminal_controls(str(summary))
    return item


def _schema_properties(input_schema: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(input_schema, Mapping):
        return set()
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return set()
    return {str(name) for name in properties}


def _set_supported_default(
    arguments: dict[str, Any],
    supported: set[str],
    key: str,
    value: Any,
) -> None:
    if key in supported and key not in arguments:
        arguments[key] = value


def guanlan_profile_for_query(query: str) -> str:
    """Choose Guanlan's language profile without rewriting the query."""

    text = str(query or "")
    has_cjk = bool(_CJK_RE.search(text))
    has_latin = bool(_LATIN_RE.search(text))
    if has_cjk and has_latin:
        latin_words = _LATIN_WORD_RE.findall(text)
        latin_characters = sum(len(word) for word in latin_words)
        if len(latin_words) >= 4 and latin_characters >= 12:
            return "hybrid"
        # Product names and version strings commonly add a few Latin tokens to
        # otherwise Chinese questions; Chinese remains the primary profile.
        return "china"
    if has_cjk:
        return "china"
    if has_latin:
        return "english"
    return "global"


def guanlan_timeout_seconds(tool_name: str) -> int:
    """Return the explicit outer timeout budget for one Guanlan capability."""

    if tool_name in _DEEP_TOOLS:
        return 300
    if tool_name in _MEDIUM_TOOLS:
        return 120
    return 90


def prepare_guanlan_tool_call(
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    *,
    input_schema: Mapping[str, Any] | None,
) -> PreparedGuanlanCall:
    """Apply Lantai's stable defaults using only fields the server advertises.

    Explicit limits/profiles are preserved so a user-requested smoke sample or
    language choice remains possible.  Machine-facing format is always JSON.
    """

    prepared = dict(arguments or {})
    supported = _schema_properties(input_schema)
    if not supported:
        return PreparedGuanlanCall(
            arguments=prepared,
            timeout_seconds=guanlan_timeout_seconds(tool_name),
        )

    if tool_name in _WIDE_POOL_TOOLS:
        _set_supported_default(
            prepared,
            supported,
            "limit",
            GUANLAN_DEFAULT_LIMIT,
        )
    if tool_name in _PROFILE_TOOLS:
        query = str(
            prepared.get("query")
            or prepared.get("focus")
            or prepared.get("entity")
            or ""
        )
        _set_supported_default(
            prepared,
            supported,
            "profile",
            guanlan_profile_for_query(query),
        )
    if tool_name in _JSON_TOOLS and "format" in supported:
        prepared["format"] = "json"
    if tool_name == "guanlan_search":
        _set_supported_default(
            prepared,
            supported,
            "evidence_mode",
            "shadow",
        )
    if tool_name == "guanlan_research":
        _set_supported_default(prepared, supported, "read_top", 0)
        _set_supported_default(prepared, supported, "max_search_jobs", 2)

    return PreparedGuanlanCall(
        arguments=prepared,
        timeout_seconds=guanlan_timeout_seconds(tool_name),
    )


def guanlan_call_is_privileged_read_only(
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> bool:
    """Whether an official call qualifies for Lantai's no-prompt allowlist.

    The allowlist is intentionally narrower than the Guanlan namespace.  Local
    paths, history writes, command adapters, and non-public URL targets fall
    back to Lantai's normal approval policy.
    """

    if tool_name not in _JSON_TOOLS:
        return False
    payload = dict(arguments or {})
    if any(payload.get(key) not in (None, "", False) for key in _LOCAL_PATH_ARGUMENTS):
        return False
    if payload.get("execute") is True or payload.get("record_history") is True:
        return False
    if tool_name.startswith("guanlan_archive_"):
        return False
    if tool_name in _PUBLIC_URL_TOOLS:
        url = str(payload.get("url") or "").strip()
        if not _is_public_web_url(url):
            return False
    base_url = str(payload.get("newsnow_base_url") or "").strip()
    if base_url and not _is_public_web_url(base_url):
        return False
    for key in ("source", "feed_source", "hotnews_source"):
        value = str(payload.get(key) or "").strip()
        if "://" in value and not _is_public_web_url(value):
            return False
    candidate_urls = payload.get("candidate_urls")
    if isinstance(candidate_urls, list) and any(
        not _is_public_web_url(str(url or "")) for url in candidate_urls
    ):
        return False
    return True


def official_guanlan_policy_decision(
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    *,
    profile: str = "workspace",
    permission_mode: str | None = None,
    distribution: str | None = None,
) -> Any | None:
    """Return an allow decision only for provenance-checked safe calls.

    The caller must first prove that the tool came from Lantai's reserved
    Guanlan client.  Returning ``None`` deliberately falls back to the normal
    MCP policy and its approval path.
    """

    if permission_mode == "plan":
        return None
    if not guanlan_call_is_privileged_read_only(tool_name, arguments):
        return None

    from ...security.tool_policy import ToolPolicyDecision

    safe_profile = (
        profile if profile in {"observe", "workspace", "full"} else "workspace"
    )
    safe_mode = (
        permission_mode
        if permission_mode
        in {"default", "plan", "accept_edits", "quiet", "full_auto"}
        else "quiet"
    )
    safe_distribution = (
        distribution
        if distribution in {"developer_id", "app_store"}
        else "developer_id"
    )
    return ToolPolicyDecision(
        decision="allow",
        reason=(
            "Official Guanlan public-information capability is allowlisted "
            "under its read-only contract."
        ),
        profile=safe_profile,
        matched_rule_id="official.guanlan.public_read_only",
        matched_rule_ids=["official.guanlan.public_read_only"],
        risk="low",
        risk_level="low",
        distribution=safe_distribution,
        permission_mode=safe_mode,
        command_category="official_external_information",
        decision_source="official_integration",
        effective_rule_id="official.guanlan.public_read_only",
        hard_denied=False,
    )


def _is_public_web_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.rstrip(".").lower()
    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
    ):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return bool(address.is_global)
