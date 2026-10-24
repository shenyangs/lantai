# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlparse
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ...config import load_config
from ...config.utils import save_config
from ..agent_context import get_agent_for_request
from ..capabilities.repo import CapabilityRepository
from ..capabilities.policy import (
    CapabilityMatrixEntry,
    build_capability_matrix,
    is_app_store_distribution,
)
from ..browser_workbench import (
    BrowserActionBroker,
    BrowserProfileService,
    BrowserTaskRuntime,
    BrowserTaskStore,
    ComputerActionBroker,
    ContentWorkbenchStore,
    LantaiCliAdapterRegistry,
)
from ..browser_workbench.models import (
    AdapterRunRequest,
    BrowserAction,
    BrowserActionConfirmation,
    BrowserExportApproval,
    BrowserObservation,
    BrowserProfile,
    BrowserProfilePatch,
    BrowserTaskConfirmation,
    BrowserTaskEvent,
    BrowserTaskRequest,
    BrowserTaskRun,
    BrowserTakeoverReceipt,
    ComputerAction,
    ComputerObservation,
    ContentDossier,
    ContentSeriesPlan,
    ContentSource,
    DomainSkillDraft,
    DomainSkillDraftPatch,
    DomainSkillDraftRecord,
    LantaiCliAdapterManifest,
    PublishingDraft,
    PublishingQueueItem,
    utc_now_iso,
)
from ..browser_workbench.safe_scripts import safe_evaluate_metadata

router = APIRouter(prefix="/browser-workbench", tags=["browser-workbench"])

_profiles = BrowserProfileService()
_broker = BrowserActionBroker(profiles=_profiles)
_tasks = BrowserTaskRuntime(
    broker=_broker,
    store=BrowserTaskStore(),
    agent_loop_settings=lambda: load_config().browser.model_dump(mode="json"),
)
_computer = ComputerActionBroker()
_registry = LantaiCliAdapterRegistry(broker=_broker)
_content = ContentWorkbenchStore()


def get_browser_task_runtime() -> BrowserTaskRuntime:
    """Return the shared Browser Workbench task runtime.

    Agent tools and HTTP routes must use the same runtime so background tasks
    created by an agent are visible in the Browser Workbench task panel.
    """

    return _tasks


def browser_file_grant_lifecycle_report(
    *,
    operation: str = "archive",
    dry_run: bool = True,
    run_id: str = "",
    profile_id: str = "",
) -> dict[str, Any]:
    """Return lifecycle state for Browser Workbench upload grants."""

    return _broker.file_grant_lifecycle_report(
        operation=operation,
        dry_run=dry_run,
        run_id=run_id,
        profile_id=profile_id,
    )


def _require_export_approval(
    approval: BrowserExportApproval | None,
    *,
    export_kind: str,
) -> BrowserExportApproval:
    if approval is None or not approval.approved:
        raise HTTPException(
            status_code=403,
            detail=f"{export_kind} export requires explicit approval",
        )
    if not approval.reason.strip():
        raise HTTPException(
            status_code=400,
            detail="Export approval reason required",
        )
    return approval


def _export_approval_response(
    approval: BrowserExportApproval,
    *,
    export_kind: str,
) -> dict[str, Any]:
    payload = approval.model_dump(mode="json")
    payload["export_kind"] = export_kind
    return payload


class BrowserConfigPatch(BaseModel):
    agent_control_enabled: bool | None = None
    agent_loop_enabled: bool | None = None
    agent_loop_mode: str | None = None
    agent_loop_budget_profile: str | None = None
    default_profile_id: str | None = None
    require_confirmation_for_write: bool | None = None
    enable_scheduled_browser_tasks: bool | None = None
    primary_runtime: str | None = None
    optional_engines_enabled: bool | None = None


class BrowserFileGrantRequest(BaseModel):
    path: str
    profile_id: str = "default"
    run_id: str = ""
    page_id: str = ""
    url: str = ""
    selector: str = ""
    ttl_seconds: int = 600
    security_scoped_bookmark: bool = False
    security_scope_ref: str = ""
    security_scope_bookmark_sha256: str = ""


class BrowserTaskCleanupRequest(BaseModel):
    max_runs: int = 200
    max_age_days: int = 30
    include_active: bool = False


class BrowserBridgeHealth(BaseModel):
    status: str
    message: str
    runtime: str = "electron_bridge"
    error_code: str = ""


class BrowserCapabilityFlags(BaseModel):
    can_open: bool = True
    can_read: bool = True
    can_write: bool = False
    can_run_background: bool = True
    can_publish: bool = False
    can_generate_domain_skill: bool = True
    advanced_features_default_open: bool = False


class BrowserWorkbenchAggregateState(BaseModel):
    config: dict[str, Any]
    profiles: list[BrowserProfile] = Field(default_factory=list)
    bridge_health: BrowserBridgeHealth
    recent_tasks: list[BrowserTaskRun] = Field(default_factory=list)
    active_task: BrowserTaskRun | None = None
    adapter_count: int = 0
    domain_skill_draft_count: int = 0
    content_summary: dict[str, int] = Field(default_factory=dict)
    capability_flags: BrowserCapabilityFlags
    capability_matrix: dict[str, CapabilityMatrixEntry] = Field(default_factory=dict)


class DossierBuildRequest(BaseModel):
    topic: str
    brief: str = ""
    sources: list[ContentSource] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReplayResearchPackRequest(BaseModel):
    topic: str = ""
    brief: str = ""
    profile_id: str = "default"
    source_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class UrlResearchPackRunRequest(BaseModel):
    topic: str
    urls: list[str] = Field(default_factory=list)
    brief: str = ""
    profile_id: str = "default"
    run_id: str = ""
    source_type: str | None = None
    screenshot_each: bool = True
    max_urls: int = 20
    metadata: dict[str, Any] = Field(default_factory=dict)


class SeriesPlanRequest(BaseModel):
    title: str
    audience: str = ""
    positioning: str = ""
    cadence: str = "daily"
    article_count: int = 7
    topics: list[str] = Field(default_factory=list)
    start_date: str = ""
    publish_time: str = "09:00"
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublishingDraftRequest(BaseModel):
    title: str
    body: str = ""
    summary: str = ""
    platform: str = "local_markdown"
    source_dossier_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublishingQueueRequest(BaseModel):
    draft_id: str
    scheduled_for: str | None = None
    platform: str = "wechat_official_account"
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublishingQueuePatchRequest(BaseModel):
    status: str | None = None
    requires_confirmation: bool | None = None
    note: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class WechatSeriesPrepareRequest(BaseModel):
    title: str
    audience: str = ""
    positioning: str = ""
    cadence: str = "daily"
    article_count: int = 7
    topics: list[str] = Field(default_factory=list)
    sources: list[ContentSource] = Field(default_factory=list)
    profile_id: str = "default"
    start_date: str = ""
    publish_time: str = "09:00"
    metadata: dict[str, Any] = Field(default_factory=dict)


class WechatDraftWriteRequest(BaseModel):
    profile_id: str = "default"
    confirmed: bool = False
    run_id: str = ""
    queue_item_id: str = ""
    open_url: str = "https://mp.weixin.qq.com/"
    title_selector: str = (
        'input[placeholder*="标题"], textarea[placeholder*="标题"], '
        'input[aria-label*="标题"]'
    )
    body_selector: str = '[contenteditable="true"], .ProseMirror, .ql-editor'
    summary_selector: str = 'textarea[placeholder*="摘要"], input[placeholder*="摘要"]'
    save_after_fill: bool = False
    save_selector: str = ""


class WechatEditorProbeRequest(BaseModel):
    profile_id: str = "default"
    page_id: str = "default"
    url: str = "https://mp.weixin.qq.com/"
    run_id: str = ""


class DomainSkillFromReplayRequest(BaseModel):
    title: str = ""
    summary: str = ""
    save: bool = False
    failure_modes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DomainSkillDraftListResponse(BaseModel):
    items: list[DomainSkillDraftRecord]


class DomainSkillSelectorProbeRequest(BaseModel):
    profile_id: str = "default"
    page_id: str = "default"
    url: str = ""
    selectors: list[str] = Field(default_factory=list)
    run_id: str = ""


class CapabilityFromReplayRequest(BaseModel):
    name: str = ""
    description: str = ""
    profile_id: str = "default"
    access: str = "read"
    instruction_template: str = ""
    save: bool = False


def manual_takeover_context_from_observation(
    observation: BrowserObservation,
) -> dict[str, Any] | None:
    payload = observation.data.get("data")
    if not isinstance(payload, dict):
        return None
    page_context = payload.get("page_context")
    if not isinstance(page_context, dict):
        return None
    if not page_context.get("requires_manual_takeover"):
        return None
    return {
        "kind": str(page_context.get("kind") or "sensitive")[:80],
        "requires_manual_takeover": True,
    }


def _browser_config_payload() -> dict[str, Any]:
    cfg = load_config()
    data = cfg.browser.model_dump(mode="json")
    data["app_store_distribution"] = is_app_store_distribution()
    return data


def _terminal_browser_task(status: str) -> bool:
    return status in {"completed", "failed", "cancelled"}


async def _browser_bridge_health(config: dict[str, Any]) -> BrowserBridgeHealth:
    if is_app_store_distribution():
        return BrowserBridgeHealth(
            status="app_store_blocked",
            message="App Store 版默认禁用自动浏览器控制，仅保留可审核读取能力。",
            error_code="app_store_browser_automation_blocked",
        )
    runtime = str(config.get("primary_runtime") or "electron_bridge")
    if runtime == "browser_skill":
        status = await _broker.browser_skill.status()
        return BrowserBridgeHealth(
            status=str(status.get("status") or "unavailable"),
            runtime="browser_skill",
            message=str(status.get("message") or "BrowserSkill 暂时不可用。"),
            error_code=str(status.get("error_code") or ""),
        )
    if runtime != "electron_bridge":
        return BrowserBridgeHealth(
            status="unsupported",
            runtime=runtime,
            message="当前配置未启用 Electron 浏览器桥接。",
            error_code="browser_runtime_not_electron_bridge",
        )
    if not _broker.bridge.available:
        return BrowserBridgeHealth(
            status="disconnected",
            message="尚未检测到本地浏览器桥接服务。",
            error_code="electron_bridge_unavailable",
        )
    try:
        state = await asyncio.wait_for(_broker.bridge.get_state(), timeout=1.5)
    except Exception as exc:
        return BrowserBridgeHealth(
            status="disconnected",
            message=f"浏览器桥接暂时不可达：{exc}",
            error_code="electron_bridge_state_timeout",
        )
    if not isinstance(state, dict):
        return BrowserBridgeHealth(
            status="disconnected",
            message="浏览器桥接返回了无法识别的状态。",
            error_code="electron_bridge_invalid_state",
        )
    error_code = str(state.get("error_code") or "")
    if state.get("ok") is False:
        try:
            http_status = int(state.get("http_status") or 0)
        except (TypeError, ValueError):
            http_status = 0
        if error_code == "electron_bridge_unauthorized" or http_status == 401:
            return BrowserBridgeHealth(
                status="unauthorized",
                message="浏览器桥接已启动，但授权令牌不匹配。",
                error_code=error_code or "electron_bridge_unauthorized",
            )
        return BrowserBridgeHealth(
            status="disconnected",
            message=str(state.get("error") or "浏览器桥接暂时不可用。"),
            error_code=error_code or "electron_bridge_unavailable",
        )
    return BrowserBridgeHealth(
        status="available",
        message="浏览器桥接已连接。",
    )


def _normalize_research_urls(urls: list[str], max_urls: int) -> list[str]:
    out: list[str] = []
    limit = max(1, min(int(max_urls or 20), 60))
    for value in urls:
        item = str(value or "").strip()
        if not item:
            continue
        if not item.startswith(("http://", "https://")):
            item = f"https://{item}"
        if item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _unique_strings(values: list[str], *, limit: int) -> list[str]:
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _safe_scope_candidates(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, str]] = []
    for item in value[:80]:
        if not isinstance(item, dict):
            continue
        selector = str(item.get("selector") or "").strip()[:240]
        frame_selector = str(item.get("frame_selector") or "").strip()[:240]
        shadow_host_selector = str(
            item.get("shadow_host_selector") or ""
        ).strip()[:240]
        if not (frame_selector or shadow_host_selector):
            continue
        key = (selector, frame_selector, shadow_host_selector)
        if key in seen:
            continue
        seen.add(key)
        candidate = {
            "selector": selector,
            "frame_selector": frame_selector,
            "shadow_host_selector": shadow_host_selector,
        }
        out.append({key: value for key, value in candidate.items() if value})
        if len(out) >= 40:
            break
    return out


def wechat_editor_probe_code() -> str:
    return r"""
() => {
  const textOf = (node) => {
    const raw = node.innerText || node.textContent || node.value || node.placeholder || node.ariaLabel || "";
    return String(raw).replace(/\s+/g, " ").trim().slice(0, 180);
  };
  const visible = (node) => {
    try {
      const rect = node.getBoundingClientRect();
      const style = window.getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    } catch {
      return true;
    }
  };
  const selectorHint = (node) => {
    const tag = node.tagName ? node.tagName.toLowerCase() : "";
    if (!tag) return "";
    if (node.id) return `${tag}#${node.id}`;
    const className = typeof node.className === "string"
      ? node.className.split(/\s+/).filter(Boolean).slice(0, 2).join(".")
      : "";
    return className ? `${tag}.${className}` : tag;
  };
  const nodes = Array.from(document.querySelectorAll("button,a,[role='button'],label,input,textarea,[contenteditable='true'],span,div"))
    .filter(visible)
    .slice(0, 5000)
    .map((node) => ({
      tag: node.tagName ? node.tagName.toLowerCase() : "",
      text: textOf(node),
      placeholder: String(node.placeholder || "").slice(0, 120),
      selector_hint: selectorHint(node),
    }))
    .filter((item) => item.text || item.placeholder);
  const matchFeature = (id, label, patterns) => {
    const samples = nodes
      .filter((item) => patterns.some((pattern) => `${item.text} ${item.placeholder}`.includes(pattern)))
      .slice(0, 8);
    return {
      id,
      label,
      found: samples.length > 0,
      count: samples.length,
      samples,
    };
  };
  const options = [
    matchFeature("cover", "封面", ["封面", "从正文选择", "图片库", "AI 配图", "拖拽或选择封面"]),
    matchFeature("original", "原创", ["原创", "声明原创", "未声明", "著作权保护"]),
    matchFeature("comment", "留言", ["留言", "精选公开", "留言自动", "留言管理"]),
    matchFeature("reward", "赞赏", ["赞赏", "赞赏账户"]),
  ];
  const risk_patterns = ["发布", "发表", "群发", "定时群发", "提交审核", "删除", "支付", "授权", "发送"];
  const manual_patterns = ["扫码登录", "微信扫码", "验证码", "安全验证", "二维码", "登录"];
  const risk_buttons = nodes
    .filter((item) => risk_patterns.some((pattern) => item.text.includes(pattern)))
    .slice(0, 12);
  const manual_takeover_hints = nodes
    .filter((item) => manual_patterns.some((pattern) => item.text.includes(pattern)))
    .slice(0, 12);
  return {
    page_title: document.title || "",
    url: location.href,
    read_only: true,
    options,
    risk_buttons,
    manual_takeover_hints,
    requires_manual_takeover: manual_takeover_hints.length > 0,
    safety: {
      no_write_actions: true,
      publish_requires_confirmation: true,
      cover_change_requires_confirmation: true,
      original_declaration_requires_confirmation: true,
      comment_policy_requires_confirmation: true,
      silent_publish_enabled: false,
    },
  };
}
""".strip()


def _wechat_editor_confirmation_context(
    *,
    kind: str,
    action_label: str,
    target_selector: str = "",
    field_selectors: list[str] | None = None,
    input_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "platform": "wechat_official_account",
        "kind": kind,
        "action_label": action_label,
        "target_element": target_selector,
        "field_selectors": [item for item in field_selectors or [] if item],
        "input_summary": input_summary or {},
        "risk_reasons": [
            "微信公众号后台写操作会改变平台草稿状态",
            "发布、群发、提交审核、定时发布不允许静默执行",
            "封面、原创声明、留言策略等配置修改必须再次确认",
        ],
        "safe_boundaries": [
            "本动作只允许写入或保存草稿",
            "不会点击发布、发表、群发、提交审核、删除、支付或授权按钮",
            "遇到登录、验证码、二维码、安全验证时必须人工接管",
        ],
        "requires_confirmation": True,
        "silent_publish_enabled": False,
    }


def domain_skill_selector_probe_code(selectors: list[str]) -> str:
    safe_selectors = [
        str(selector or "").strip()
        for selector in selectors
        if str(selector or "").strip()
    ][:50]
    encoded = json.dumps(safe_selectors, ensure_ascii=False)
    return f"""
() => {{
  const selectors = {encoded};
  const sampledNodes = Array.from(document.querySelectorAll("*")).slice(0, 5000);
  const iframeEntries = Array.from(document.querySelectorAll("iframe")).slice(0, 20).map((frame, index) => {{
    try {{
      const frameDocument = frame.contentDocument;
      if (frameDocument) {{
        return {{
          kind: "iframe",
          scope: `iframe[${{index}}]`,
          root: frameDocument,
          url: frame.src || frameDocument.location?.href || "",
        }};
      }}
    }} catch (error) {{
      return {{
        kind: "iframe",
        scope: `iframe[${{index}}]`,
        blocked: true,
        url: frame.src || "",
        error: "cross_origin_or_unavailable",
      }};
    }}
    return {{
      kind: "iframe",
      scope: `iframe[${{index}}]`,
      blocked: true,
      url: frame.src || "",
      error: "cross_origin_or_unavailable",
    }};
  }});
  const shadowEntries = sampledNodes
    .filter((node) => Boolean(node.shadowRoot))
    .slice(0, 50)
    .map((node, index) => ({{
      kind: "shadow",
      scope: `shadow[${{index}}]`,
      root: node.shadowRoot,
      host: {{
        tag: node.tagName ? node.tagName.toLowerCase() : "",
        id: node.id || "",
        class_name: typeof node.className === "string" ? node.className.slice(0, 120) : "",
      }},
    }}));
  const pageDiagnostics = {{
    document_ready_state: document.readyState,
    iframe_count: document.querySelectorAll("iframe").length,
    accessible_iframe_count: iframeEntries.filter((entry) => Boolean(entry.root)).length,
    blocked_iframe_count: iframeEntries.filter((entry) => Boolean(entry.blocked)).length,
    shadow_host_count: sampledNodes.filter((node) => Boolean(node.shadowRoot)).length,
    open_shadow_root_count: shadowEntries.length,
    sampled_node_count: sampledNodes.length,
  }};
  const sampleNodes = (nodes, scope) => nodes.slice(0, 5).map((node) => ({{
    scope,
    tag: node.tagName ? node.tagName.toLowerCase() : "",
    text: (node.innerText || node.textContent || "").trim().slice(0, 180),
    href: node.href || node.querySelector?.("a[href]")?.href || "",
  }}));
  const queryEntry = (entry, selector) => {{
    if (entry.blocked) {{
      return {{
        scope: entry.scope,
        kind: entry.kind,
        ok: false,
        blocked: true,
        count: 0,
        samples: [],
        url: entry.url || "",
        error: entry.error || "unavailable",
      }};
    }}
    try {{
      const nodes = Array.from(entry.root.querySelectorAll(selector));
      return {{
        scope: entry.scope,
        kind: entry.kind,
        ok: true,
        blocked: false,
        count: nodes.length,
        samples: sampleNodes(nodes, entry.scope),
        url: entry.url || "",
        host: entry.host || null,
      }};
    }} catch (error) {{
      return {{
        scope: entry.scope,
        kind: entry.kind,
        ok: false,
        blocked: false,
        count: 0,
        samples: [],
        url: entry.url || "",
        host: entry.host || null,
        error: String(error && error.message ? error.message : error),
      }};
    }}
  }};
  const roots = [
    {{ kind: "document", scope: "document", root: document, url: location.href }},
    ...iframeEntries,
    ...shadowEntries,
  ];
  return selectors.map((selector) => {{
    const scopes = roots.map((entry) => queryEntry(entry, selector));
    const fatal = scopes.find((scope) => !scope.ok && !scope.blocked && scope.kind === "document");
    const scopeCounts = scopes.reduce(
      (acc, scope) => {{
        acc[scope.kind] = (acc[scope.kind] || 0) + Number(scope.count || 0);
        return acc;
      }},
      {{ document: 0, iframe: 0, shadow: 0 }},
    );
    const blockedFrameCount = scopes.filter((scope) => scope.kind === "iframe" && scope.blocked).length;
    return {{
      selector,
      ok: !fatal,
      count: scopes.reduce((total, scope) => total + Number(scope.count || 0), 0),
      diagnostics: pageDiagnostics,
      scope_counts: scopeCounts,
      blocked_frame_count: blockedFrameCount,
      samples: scopes.flatMap((scope) => scope.samples || []).slice(0, 10),
      scopes: scopes.map((scope) => ({{
        scope: scope.scope,
        kind: scope.kind,
        ok: scope.ok,
        blocked: scope.blocked,
        count: scope.count,
        samples: scope.samples,
        url: scope.url,
        host: scope.host,
        error: scope.error || "",
      }})),
      error: fatal ? fatal.error || "" : "",
    }};
  }});
}}
""".strip()


def domain_skill_selector_probe_items(
    observation: BrowserObservation,
) -> list[dict[str, Any]]:
    data = observation.data.get("data")
    if isinstance(data, list):
        return [
            item
            for item in data
            if isinstance(item, dict)
        ]
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return [
            item
            for item in data["data"]
            if isinstance(item, dict)
        ]
    return []


def domain_skill_url_pattern_matches(
    *,
    url: str,
    domain: str,
    url_patterns: list[str],
) -> list[str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return []
    host = (parsed.hostname or "").lower()
    expected_domain = domain.strip().lower()
    if expected_domain and host != expected_domain:
        return []
    matches: list[str] = []
    for pattern in url_patterns:
        item = str(pattern or "").strip()
        if not item:
            continue
        try:
            parsed_pattern = urlparse(item)
        except Exception:
            continue
        pattern_host = (parsed_pattern.hostname or "").lower()
        if pattern_host and pattern_host != host:
            continue
        path_prefix = parsed_pattern.path.split("{", 1)[0].rstrip("/")
        if not path_prefix or parsed.path.startswith(path_prefix):
            matches.append(item)
    return matches


def domain_skill_selector_probe_summary(
    *,
    observation: BrowserObservation,
    run_id: str,
    selectors: list[str],
    domain: str,
    url_patterns: list[str],
) -> dict[str, Any]:
    items = domain_skill_selector_probe_items(observation)
    total_matches = 0
    document_match_count = 0
    iframe_match_count = 0
    shadow_match_count = 0
    blocked_frame_count = 0
    matched_selectors: list[str] = []
    failed_selectors: list[str] = []
    for item in items:
        selector = str(item.get("selector") or "")
        try:
            count = int(item.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        scope_counts = item.get("scope_counts")
        if isinstance(scope_counts, dict):
            try:
                document_match_count += int(scope_counts.get("document") or 0)
                iframe_match_count += int(scope_counts.get("iframe") or 0)
                shadow_match_count += int(scope_counts.get("shadow") or 0)
            except (TypeError, ValueError):
                pass
        else:
            document_match_count += count
        try:
            blocked_frame_count = max(
                blocked_frame_count,
                int(item.get("blocked_frame_count") or 0),
            )
        except (TypeError, ValueError):
            pass
        total_matches += count
        if item.get("ok") is True and count > 0:
            matched_selectors.append(selector)
        else:
            failed_selectors.append(selector)
    probed_selectors = [
        str(selector or "").strip()
        for selector in selectors
        if str(selector or "").strip()
    ]
    matched_url_patterns = domain_skill_url_pattern_matches(
        url=observation.url,
        domain=domain,
        url_patterns=url_patterns,
    )
    page_diagnostics: dict[str, Any] = {}
    for item in items:
        diagnostics = item.get("diagnostics")
        if isinstance(diagnostics, dict):
            page_diagnostics = diagnostics
            try:
                blocked_frame_count = max(
                    blocked_frame_count,
                    int(diagnostics.get("blocked_iframe_count") or 0),
                )
            except (TypeError, ValueError):
                pass
            break
    return {
        "observed_at": utc_now_iso(),
        "run_id": run_id,
        "ok": observation.ok,
        "selector_count": len(probed_selectors),
        "returned_selector_count": len(items),
        "matched_selector_count": len(matched_selectors),
        "total_matches": total_matches,
        "document_match_count": document_match_count,
        "iframe_match_count": iframe_match_count,
        "shadow_match_count": shadow_match_count,
        "blocked_frame_count": blocked_frame_count,
        "scope_match_counts": {
            "document": document_match_count,
            "iframe": iframe_match_count,
            "shadow": shadow_match_count,
        },
        "matched_selectors": matched_selectors[:50],
        "failed_selectors": failed_selectors[:50],
        "page_id": observation.page_id,
        "url": observation.url,
        "url_pattern_count": len(url_patterns),
        "url_pattern_matched": not url_patterns or bool(matched_url_patterns),
        "matched_url_patterns": matched_url_patterns[:20],
        "page_diagnostics": page_diagnostics,
        "replay_event_id": observation.replay_event_id,
        "error": observation.error or "",
    }


async def run_url_research_pack_workflow(
    *,
    broker: BrowserActionBroker,
    content: ContentWorkbenchStore,
    request: UrlResearchPackRunRequest,
) -> dict[str, Any]:
    run_id = request.run_id or uuid4().hex
    urls = _normalize_research_urls(request.urls, request.max_urls)
    if not urls:
        return {
            "ok": False,
            "run_id": run_id,
            "visited_urls": [],
            "observations": [],
            "sources": [],
            "dossier": None,
            "workflow_run": None,
            "export_paths": {},
            "message": "No valid URLs were provided.",
        }

    observations: list[BrowserObservation] = []
    for index, url in enumerate(urls, start=1):
        actions = [
            BrowserAction(
                action="open",
                profile_id=request.profile_id,
                url=url,
                access="read",
                run_id=run_id,
                reason=f"Open source {index} for browser research pack",
                metadata={
                    "workflow": "browser_research_pack",
                    "topic": request.topic,
                    "source_index": index,
                    "source_url": url,
                    **request.metadata,
                },
            ),
            BrowserAction(
                action="snapshot",
                profile_id=request.profile_id,
                url=url,
                access="read",
                run_id=run_id,
                reason=f"Capture source {index} DOM summary for research pack",
                metadata={
                    "workflow": "browser_research_pack",
                    "topic": request.topic,
                    "source_index": index,
                    "source_url": url,
                    **request.metadata,
                },
            ),
        ]
        if request.screenshot_each:
            actions.append(
                BrowserAction(
                    action="screenshot",
                    profile_id=request.profile_id,
                    url=url,
                    access="read",
                    run_id=run_id,
                    reason=f"Capture source {index} screenshot evidence for research pack",
                    metadata={
                        "workflow": "browser_research_pack",
                        "topic": request.topic,
                        "source_index": index,
                        "source_url": url,
                        **request.metadata,
                    },
                )
            )

        for action in actions:
            observation = await broker.execute(action)
            observations.append(observation)
            if not observation.ok:
                break

    pack = content.create_replay_research_pack(
        run_id=run_id,
        events=broker.replay.list_events(run_id),
        topic=request.topic,
        brief=request.brief,
        profile_id=request.profile_id,
        source_type=request.source_type,
        metadata={
            "workflow": "browser_research_pack",
            "visited_urls": urls,
            "observation_count": len(observations),
            **request.metadata,
        },
    )
    return {
        **pack,
        "run_id": run_id,
        "visited_urls": urls,
        "observations": [item.model_dump(mode="json") for item in observations],
        "ok_observation_count": sum(1 for item in observations if item.ok),
        "blocked_observation_count": sum(1 for item in observations if not item.ok),
        "safety": {
            "read_only": True,
            "broker_enforced": True,
            "silent_publish_enabled": False,
        },
    }


@router.get("/state", response_model=BrowserWorkbenchAggregateState)
async def get_browser_workbench_state() -> BrowserWorkbenchAggregateState:
    config = _browser_config_payload()
    profiles = _profiles.list_profiles()
    tasks = _tasks.list_runs()
    tasks = sorted(tasks, key=lambda task: task.updated_at, reverse=True)
    active_task = next(
        (task for task in tasks if not _terminal_browser_task(task.status)),
        None,
    )
    bridge_health = await _browser_bridge_health(config)
    app_store = bool(config.get("app_store_distribution"))
    matrix = build_capability_matrix(
        distribution="app_store" if app_store else "developer_id",
    )
    browser_read = matrix.entry("browser_read_navigation")
    browser_write = matrix.entry("browser_write")
    browser_file_transfer = matrix.entry("browser_upload_download")
    background = matrix.entry("background_scheduled_execution")
    content_summary = {
        "sources": len(_content.list_sources()),
        "dossiers": len(_content.list_dossiers()),
        "drafts": len(_content.list_drafts()),
        "queue": len(_content.list_queue()),
        "workflows": len(_content.list_workflow_runs()),
    }
    return BrowserWorkbenchAggregateState(
        config=config,
        profiles=profiles,
        bridge_health=bridge_health,
        recent_tasks=tasks[:20],
        active_task=active_task,
        adapter_count=len(_registry.list_adapters()),
        domain_skill_draft_count=len(_registry.list_domain_skill_drafts()),
        content_summary=content_summary,
        capability_flags=BrowserCapabilityFlags(
            can_open=browser_read.mode == "enabled",
            can_read=browser_read.mode == "enabled",
            can_write=browser_write.mode in {"enabled", "interactive_only"}
            and bool(config.get("agent_control_enabled")),
            can_run_background=background.mode == "enabled"
            and bool(config.get("agent_control_enabled")),
            can_publish=False,
            can_generate_domain_skill=browser_file_transfer.mode != "disabled"
            and browser_read.mode == "enabled",
            advanced_features_default_open=False,
        ),
        capability_matrix=matrix.entries,
    )


@router.get("/config", summary="Get browser workbench config")
async def get_browser_config() -> dict[str, Any]:
    return _browser_config_payload()


@router.get("/browser-skill/status", summary="Get BrowserSkill runtime status")
async def get_browser_skill_status() -> dict[str, Any]:
    """Report local CLI/extension health without installing or updating it."""

    return await _broker.browser_skill.status()


@router.put("/config", summary="Update browser workbench config")
async def put_browser_config(body: BrowserConfigPatch) -> dict[str, Any]:
    cfg = load_config()
    patch = body.model_dump(exclude_unset=True)
    if patch:
        data = cfg.browser.model_dump(mode="json")
        data.update(patch)
        cfg.browser = cfg.browser.__class__.model_validate(data)
        save_config(cfg)
    data = cfg.browser.model_dump(mode="json")
    data["app_store_distribution"] = is_app_store_distribution()
    return data


@router.get("/profiles", response_model=list[BrowserProfile])
async def list_browser_profiles() -> list[BrowserProfile]:
    return _profiles.list_profiles()


@router.post("/profiles", response_model=BrowserProfile)
async def upsert_browser_profile(profile: BrowserProfile) -> BrowserProfile:
    try:
        return _profiles.upsert_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/profiles/{profile_id}", response_model=BrowserProfile)
async def patch_browser_profile(
    profile_id: str,
    patch: BrowserProfilePatch,
) -> BrowserProfile:
    try:
        return _profiles.patch_profile(profile_id, patch)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/profiles/{profile_id}")
async def delete_browser_profile(profile_id: str) -> dict[str, Any]:
    try:
        normalized_profile_id = _profiles.normalize_profile_id(profile_id)
        if normalized_profile_id == "default":
            raise ValueError("default browser profile cannot be deleted")
        storage_cleanup = await _clear_browser_profile_storage(normalized_profile_id)
        removed = _profiles.delete_profile(profile_id)
        local_storage_removed = (
            _profiles.delete_profile_storage_dir(normalized_profile_id)
            if removed
            else False
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "removed": removed,
        "profile_id": normalized_profile_id,
        "electron_storage": storage_cleanup,
        "local_storage_removed": local_storage_removed,
    }


async def _clear_browser_profile_storage(profile_id: str) -> dict[str, Any]:
    bridge = _broker.bridge
    if not bridge.available:
        return {
            "ok": False,
            "pending": True,
            "error_code": "electron_bridge_unavailable",
            "message": "Electron Browser Bridge is unavailable; profile storage cleanup is pending.",
        }
    payload = await bridge.execute(
        BrowserAction(
            action="clear_profile_storage",
            profile_id=profile_id,
            reason="Delete Browser Workbench profile storage.",
        )
    )
    return payload if isinstance(payload, dict) else {"ok": False, "payload": payload}


@router.post("/actions", response_model=BrowserObservation)
async def run_browser_action(action: BrowserAction) -> BrowserObservation:
    return await _broker.execute(action)


@router.post("/file-grants")
async def create_browser_file_grant(
    request: BrowserFileGrantRequest,
) -> dict[str, Any]:
    try:
        grant = _broker.grant_upload_file(
            path=request.path,
            profile_id=request.profile_id,
            run_id=request.run_id,
            page_id=request.page_id,
            url=request.url,
            selector=request.selector,
            ttl_seconds=request.ttl_seconds,
            security_scoped_bookmark=request.security_scoped_bookmark,
            security_scope_ref=request.security_scope_ref,
            security_scope_bookmark_sha256=request.security_scope_bookmark_sha256,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "file_grant_id": grant.id,
        "grant_ref": grant.grant_ref,
        "security_scope_ref": grant.security_scope_ref,
        "security_scoped_bookmark": grant.security_scoped_bookmark,
        "security_scope_bookmark_sha256": grant.security_scope_bookmark_sha256,
        "path_identity_hash": grant.path_identity_hash,
        "path": grant.path,
        "profile_id": grant.profile_id,
        "run_id": grant.run_id,
        "page_id": grant.page_id,
        "url": grant.url,
        "selector": grant.selector,
        "expires_at": grant.expires_at.isoformat(),
    }


@router.post("/actions/confirm", response_model=BrowserObservation)
async def confirm_browser_action(
    confirmation: BrowserActionConfirmation,
) -> BrowserObservation:
    if confirmation.approved:
        action = _broker.confirm_action(
            confirmation.action,
            reason=confirmation.reason,
        )
        return await _broker.execute(action)
    return _broker.deny_confirmation(
        confirmation.action,
        reason=confirmation.reason,
    )


@router.post("/tasks", response_model=BrowserTaskRun)
async def create_browser_task(request: BrowserTaskRequest) -> BrowserTaskRun:
    return await _tasks.create(request)


@router.get("/tasks", response_model=list[BrowserTaskRun])
async def list_browser_tasks() -> list[BrowserTaskRun]:
    return _tasks.list_runs()


@router.post("/tasks/cleanup")
async def cleanup_browser_tasks(
    request: BrowserTaskCleanupRequest,
) -> dict[str, Any]:
    return _tasks.cleanup_history(
        max_runs=request.max_runs,
        max_age_days=request.max_age_days,
        include_active=request.include_active,
    )


@router.get("/tasks/{run_id}", response_model=BrowserTaskRun)
async def get_browser_task(run_id: str) -> BrowserTaskRun:
    try:
        run = _tasks.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Browser task not found: {run_id}") from exc


@router.get("/tasks/{run_id}/events", response_model=list[BrowserTaskEvent])
async def list_browser_task_events(
    run_id: str,
    after_id: str = "",
) -> list[BrowserTaskEvent]:
    if _tasks.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Browser task not found: {run_id}")
    return _tasks.list_events(run_id, after_id=after_id)


@router.get("/tasks/{run_id}/events/stream")
async def stream_browser_task_events(
    run_id: str,
    after_id: str = "",
    timeout_seconds: float = 60.0,
) -> StreamingResponse:
    if _tasks.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Browser task not found: {run_id}")

    async def event_stream():
        last_seen = after_id
        deadline = time.monotonic() + min(max(timeout_seconds, 1.0), 300.0)
        while time.monotonic() < deadline:
            emitted = False
            events = _tasks.list_events(run_id, after_id=last_seen)
            for event in events:
                last_seen = event.id
                emitted = True
                payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                yield f"id: {event.id}\nevent: {event.type}\ndata: {payload}\n\n"
            run = _tasks.get_run(run_id)
            if (
                run is None
                or run.status
                in {"completed", "failed", "cancelled", "manual_takeover_required"}
            ):
                if not emitted:
                    break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/tasks/{run_id}/pause", response_model=BrowserTaskRun)
async def pause_browser_task(run_id: str) -> BrowserTaskRun:
    try:
        return await _tasks.pause_canonical(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/tasks/{run_id}/takeover",
    response_model=BrowserTakeoverReceipt,
)
async def request_browser_task_takeover(run_id: str) -> BrowserTakeoverReceipt:
    try:
        return await _tasks.request_takeover(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{run_id}/resume", response_model=BrowserTaskRun)
async def resume_browser_task(run_id: str) -> BrowserTaskRun:
    try:
        return await _tasks.resume_canonical(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{run_id}/cancel", response_model=BrowserTaskRun)
async def cancel_browser_task(run_id: str) -> BrowserTaskRun:
    try:
        return await _tasks.cancel_canonical(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{run_id}/confirm", response_model=BrowserTaskRun)
async def confirm_browser_task(
    run_id: str,
    confirmation: BrowserTaskConfirmation,
) -> BrowserTaskRun:
    try:
        return await _tasks.confirm(run_id, confirmation)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/tasks/{run_id}/artifacts")
async def get_browser_task_artifacts(run_id: str) -> dict[str, Any]:
    try:
        return _tasks.artifacts(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/computer-actions/actions", response_model=ComputerObservation)
async def run_computer_action(action: ComputerAction) -> ComputerObservation:
    return await _computer.execute(action)


@router.get("/replay/{run_id}")
async def list_browser_replay(run_id: str) -> dict[str, Any]:
    return {"run_id": run_id, "events": _broker.replay.list_events(run_id)}


@router.post("/replay/{run_id}/export")
async def export_browser_replay(
    run_id: str,
    approval: BrowserExportApproval | None = None,
) -> dict[str, Any]:
    approved = _require_export_approval(
        approval,
        export_kind="browser_replay_bundle",
    )
    try:
        return _broker.replay.export_bundle(run_id, approval=approved)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/adapters", response_model=list[LantaiCliAdapterManifest])
async def list_lantaicli_adapters() -> list[LantaiCliAdapterManifest]:
    return _registry.list_adapters()


@router.post("/adapters/{adapter_id}/run", response_model=BrowserObservation)
async def run_lantaicli_adapter(
    adapter_id: str,
    request: AdapterRunRequest,
) -> BrowserObservation:
    return await _registry.run_adapter(adapter_id, request)


@router.post("/adapters/{adapter_id}/verify")
async def verify_lantaicli_adapter(
    adapter_id: str,
    request: AdapterRunRequest,
) -> dict[str, Any]:
    return _registry.verify_adapter(adapter_id, request)


@router.post("/domain-skills/drafts")
async def save_domain_skill_draft(draft: DomainSkillDraft) -> dict[str, Any]:
    path = _registry.save_domain_skill_draft(draft)
    return {"ok": True, "path": str(path)}


@router.get("/domain-skills/drafts", response_model=DomainSkillDraftListResponse)
async def list_domain_skill_drafts(
    domain: str | None = None,
) -> DomainSkillDraftListResponse:
    return DomainSkillDraftListResponse(
        items=_registry.list_domain_skill_drafts(domain=domain)
    )


@router.patch(
    "/domain-skills/drafts/{draft_id}",
    response_model=DomainSkillDraftRecord,
)
async def update_domain_skill_draft(
    draft_id: str,
    patch: DomainSkillDraftPatch,
) -> DomainSkillDraftRecord:
    try:
        return _registry.update_domain_skill_draft(draft_id, patch)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/domain-skills/drafts/{draft_id}/probe-selectors",
    response_model=BrowserObservation,
)
async def probe_domain_skill_selectors(
    draft_id: str,
    request: DomainSkillSelectorProbeRequest,
) -> BrowserObservation:
    try:
        record = _registry.load_domain_skill_draft(draft_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    selectors = request.selectors or record.draft.selectors
    run_id = request.run_id or uuid4().hex
    observation = await _broker.execute(
        BrowserAction(
            action="evaluate",
            profile_id=request.profile_id,
            page_id=request.page_id,
            url=request.url,
            access="read",
            run_id=run_id,
            code=domain_skill_selector_probe_code(selectors),
            reason=f"Probe selectors for Domain Skill draft {draft_id}",
            metadata={
                "domain_skill_id": draft_id,
                "domain_skill_title": record.draft.title,
                "probe": "domain_skill_selectors",
                "selector_count": len(selectors),
                **safe_evaluate_metadata(
                    "domain_skill.selector_probe",
                    {"selectors": selectors},
                ),
            },
        )
    )
    summary = domain_skill_selector_probe_summary(
        observation=observation,
        run_id=run_id,
        selectors=selectors,
        domain=record.draft.domain,
        url_patterns=record.draft.url_patterns,
    )
    existing_history = record.draft.metadata.get("selector_probe_history")
    history_items = [
        item
        for item in (existing_history if isinstance(existing_history, list) else [])
        if isinstance(item, dict)
    ]
    updated = _registry.update_domain_skill_draft(
        draft_id,
        DomainSkillDraftPatch(
            metadata={
                "last_selector_probe": summary,
                "selector_probe_history": [summary, *history_items[:19]],
            }
        ),
    )
    observation.data = {
        **observation.data,
        "selector_probe_summary": summary,
        "domain_skill_record": updated.model_dump(mode="json"),
    }
    return observation


@router.post("/domain-skills/drafts/from-replay/{run_id}")
async def build_domain_skill_draft_from_replay(
    run_id: str,
    request: DomainSkillFromReplayRequest,
) -> dict[str, Any]:
    draft = _broker.replay.build_domain_skill_draft(
        run_id,
        title=request.title,
        summary=request.summary,
    )
    extra_failures = [
        str(item or "").strip()[:240]
        for item in request.failure_modes
        if str(item or "").strip()
    ]
    if extra_failures:
        draft.failure_modes = _unique_strings(
            [*draft.failure_modes, *extra_failures],
            limit=20,
        )
    if request.metadata:
        safe_metadata = {
            key: value
            for key, value in request.metadata.items()
            if key
            not in {
                "cookie",
                "cookies",
                "password",
                "token",
                "secret",
            }
        }
        scope_candidates = _safe_scope_candidates(
            safe_metadata.get("scope_candidates")
            or safe_metadata.get("preflight_scope_candidates")
        )
        if scope_candidates:
            existing_scopes = _safe_scope_candidates(draft.metadata.get("scopes"))
            safe_metadata["scope_candidates"] = scope_candidates
            safe_metadata["scopes"] = [
                *existing_scopes,
                *[
                    item
                    for item in scope_candidates
                    if item not in existing_scopes
                ],
            ][:40]
        draft.metadata = {
            **draft.metadata,
            **safe_metadata,
        }
    path = ""
    if request.save:
        path = str(_registry.save_domain_skill_draft(draft))
    return {
        "ok": True,
        "draft": draft.model_dump(mode="json"),
        "path": path,
    }


@router.post("/capabilities/from-replay/{run_id}")
async def build_capability_from_replay(
    run_id: str,
    body: CapabilityFromReplayRequest,
    request: Request,
) -> dict[str, Any]:
    capability = _broker.replay.build_capability_from_replay(
        run_id,
        name=body.name,
        description=body.description,
        profile_id=body.profile_id,
        access=body.access,
        instruction_template=body.instruction_template,
    )
    saved = False
    if body.save:
        workspace = await get_agent_for_request(request)
        repo = CapabilityRepository(workspace.workspace_dir)
        await repo.upsert_capability(capability)
        saved = True
    return {
        "ok": True,
        "saved": saved,
        "capability": capability.model_dump(mode="json"),
    }


@router.post("/content/sources", response_model=ContentSource)
async def save_content_source(source: ContentSource) -> ContentSource:
    return _content.save_source(source)


@router.get("/content/sources")
async def list_content_sources() -> dict[str, Any]:
    return {"items": _content.list_sources()}


@router.get("/content/sources/{source_id}")
async def get_content_source(source_id: str) -> dict[str, Any]:
    try:
        return _content.get_source_detail(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source not found") from exc


@router.get("/content/sources/{source_id}/evidence/{index}")
async def get_content_source_evidence(source_id: str, index: int) -> FileResponse:
    try:
        path = _content.get_source_evidence_asset(source_id, index)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source not found") from exc
    except IndexError as exc:
        raise HTTPException(status_code=404, detail="Evidence not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Evidence file not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Evidence path is not allowed") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path)


@router.post("/content/sources/{source_id}/export-markdown")
async def export_content_source_markdown(
    source_id: str,
    approval: BrowserExportApproval | None = None,
) -> dict[str, Any]:
    approved = _require_export_approval(
        approval,
        export_kind="content_source_markdown",
    )
    try:
        path = _content.export_source_markdown(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source not found") from exc
    return {
        "ok": True,
        "path": str(path),
        "export_approval": _export_approval_response(
            approved,
            export_kind="content_source_markdown",
        ),
    }


@router.post("/content/sources/{source_id}/export-wiki")
async def export_content_source_to_wiki(
    source_id: str,
    request: Request,
    approval: BrowserExportApproval | None = None,
) -> dict[str, Any]:
    approved = _require_export_approval(
        approval,
        export_kind="content_source_wiki",
    )
    try:
        path = _content.export_source_markdown(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source not found") from exc
    workspace = await get_agent_for_request(request)
    payload = _content.import_markdown_export_to_wiki(
        path,
        workspace_dir=workspace.workspace_dir,
        agent_id=workspace.agent_id,
    )
    payload["export_path"] = str(path)
    payload["export_approval"] = _export_approval_response(
        approved,
        export_kind="content_source_wiki",
    )
    return payload


@router.post("/content/dossiers", response_model=ContentDossier)
async def build_content_dossier(request: DossierBuildRequest) -> ContentDossier:
    return _content.build_dossier(
        topic=request.topic,
        sources=request.sources,
        brief=request.brief,
        metadata=request.metadata,
    )


@router.get("/content/dossiers")
async def list_content_dossiers() -> dict[str, Any]:
    return {"items": _content.list_dossiers()}


@router.post("/content/sources/from-replay/{run_id}")
async def build_content_sources_from_replay(
    run_id: str,
    request: ReplayResearchPackRequest,
) -> dict[str, Any]:
    events = _broker.replay.list_events(run_id)
    return _content.create_replay_research_pack(
        run_id=run_id,
        events=events,
        topic=request.topic,
        brief=request.brief,
        profile_id=request.profile_id,
        source_type=request.source_type,
        metadata=request.metadata,
    )


@router.post("/content/dossiers/{dossier_id}/export-markdown")
async def export_content_dossier_markdown(
    dossier_id: str,
    approval: BrowserExportApproval | None = None,
) -> dict[str, Any]:
    approved = _require_export_approval(
        approval,
        export_kind="content_dossier_markdown",
    )
    try:
        path = _content.export_dossier_markdown(dossier_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Dossier not found") from exc
    return {
        "ok": True,
        "path": str(path),
        "export_approval": _export_approval_response(
            approved,
            export_kind="content_dossier_markdown",
        ),
    }


@router.post("/content/dossiers/{dossier_id}/export-wiki")
async def export_content_dossier_to_wiki(
    dossier_id: str,
    request: Request,
    approval: BrowserExportApproval | None = None,
) -> dict[str, Any]:
    approved = _require_export_approval(
        approval,
        export_kind="content_dossier_wiki",
    )
    try:
        path = _content.export_dossier_markdown(dossier_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Dossier not found") from exc
    workspace = await get_agent_for_request(request)
    payload = _content.import_markdown_export_to_wiki(
        path,
        workspace_dir=workspace.workspace_dir,
        agent_id=workspace.agent_id,
    )
    payload["export_path"] = str(path)
    payload["export_approval"] = _export_approval_response(
        approved,
        export_kind="content_dossier_wiki",
    )
    return payload


@router.post("/content/series", response_model=ContentSeriesPlan)
async def create_content_series_plan(
    request: SeriesPlanRequest,
) -> ContentSeriesPlan:
    return _content.create_series_plan(
        title=request.title,
        audience=request.audience,
        positioning=request.positioning,
        cadence=request.cadence,
        article_count=request.article_count,
        topics=request.topics,
        start_date=request.start_date,
        publish_time=request.publish_time,
        metadata=request.metadata,
    )


@router.post("/content/workflows/wechat-series/prepare")
async def prepare_wechat_series_workflow(
    request: WechatSeriesPrepareRequest,
) -> dict[str, Any]:
    return _content.prepare_wechat_series(
        title=request.title,
        audience=request.audience,
        positioning=request.positioning,
        cadence=request.cadence,
        article_count=request.article_count,
        topics=request.topics,
        sources=request.sources,
        profile_id=request.profile_id,
        start_date=request.start_date,
        publish_time=request.publish_time,
        metadata=request.metadata,
    )


@router.post("/publishing/wechat/editor-probe")
async def probe_wechat_editor_options(
    request: WechatEditorProbeRequest,
) -> dict[str, Any]:
    run_id = request.run_id or uuid4().hex
    observation = await _broker.execute(
        BrowserAction(
            action="evaluate",
            profile_id=request.profile_id,
            page_id=request.page_id,
            url=request.url,
            access="read",
            run_id=run_id,
            code=wechat_editor_probe_code(),
            reason=(
                "Read WeChat Official Account editor controls without "
                "changing cover, original declaration, comment policy, or "
                "publishing state."
            ),
            metadata={
                "domain": "mp.weixin.qq.com",
                "probe": "wechat_editor_options",
                "read_only": True,
                "no_write_actions": True,
                "publish_requires_confirmation": True,
                **safe_evaluate_metadata("wechat.editor_options_probe"),
            },
        )
    )
    probe_payload = observation.data.get("data")
    if not isinstance(probe_payload, dict):
        probe_payload = {}
    safety = {
        "read_only": True,
        "no_write_actions": True,
        "publish_requires_confirmation": True,
        "cover_change_requires_confirmation": True,
        "original_declaration_requires_confirmation": True,
        "comment_policy_requires_confirmation": True,
        "silent_publish_enabled": False,
    }
    if isinstance(probe_payload.get("safety"), dict):
        safety = {
            **safety,
            **probe_payload["safety"],
        }
    return {
        "ok": observation.ok,
        "run_id": run_id,
        "requires_manual_takeover": bool(
            probe_payload.get("requires_manual_takeover")
        ),
        "probe": probe_payload,
        "safety": safety,
        "observation": observation.model_dump(mode="json"),
    }


@router.post("/content/workflows/research-pack/run")
async def run_content_research_pack_workflow(
    request: UrlResearchPackRunRequest,
) -> dict[str, Any]:
    return await run_url_research_pack_workflow(
        broker=_broker,
        content=_content,
        request=request,
    )


@router.post("/publishing/drafts", response_model=PublishingDraft)
async def create_publishing_draft(
    request: PublishingDraftRequest,
) -> PublishingDraft:
    return _content.create_publishing_draft(
        title=request.title,
        body=request.body,
        summary=request.summary,
        platform=request.platform,
        source_dossier_id=request.source_dossier_id,
        metadata=request.metadata,
    )


@router.get("/publishing/drafts")
async def list_publishing_drafts() -> dict[str, Any]:
    return {"items": _content.list_drafts()}


@router.post("/publishing/drafts/{draft_id}/export-markdown")
async def export_publishing_draft_markdown(
    draft_id: str,
    approval: BrowserExportApproval | None = None,
) -> dict[str, Any]:
    approved = _require_export_approval(
        approval,
        export_kind="publishing_draft_markdown",
    )
    try:
        path = _content.export_draft_markdown(draft_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Draft not found") from exc
    return {
        "ok": True,
        "path": str(path),
        "export_approval": _export_approval_response(
            approved,
            export_kind="publishing_draft_markdown",
        ),
    }


@router.post("/publishing/drafts/{draft_id}/export-wiki")
async def export_publishing_draft_to_wiki(
    draft_id: str,
    request: Request,
    approval: BrowserExportApproval | None = None,
) -> dict[str, Any]:
    approved = _require_export_approval(
        approval,
        export_kind="publishing_draft_wiki",
    )
    try:
        path = _content.export_draft_markdown(draft_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Draft not found") from exc
    workspace = await get_agent_for_request(request)
    payload = _content.import_markdown_export_to_wiki(
        path,
        workspace_dir=workspace.workspace_dir,
        agent_id=workspace.agent_id,
    )
    payload["export_path"] = str(path)
    payload["export_approval"] = _export_approval_response(
        approved,
        export_kind="publishing_draft_wiki",
    )
    return payload


@router.post("/publishing/drafts/{draft_id}/wechat/write-request")
async def request_wechat_draft_write(
    draft_id: str,
    request: WechatDraftWriteRequest,
) -> dict[str, Any]:
    try:
        draft = _content.load_draft(draft_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Draft not found") from exc
    queue_item_id = request.queue_item_id.strip()
    if queue_item_id:
        try:
            _content.load_queue_item(queue_item_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Queue item not found") from exc
    run_id = request.run_id or uuid4().hex
    fields = {
        request.title_selector: draft.title,
        request.body_selector: draft.body,
    }
    if draft.summary and request.summary_selector:
        fields[request.summary_selector] = draft.summary
    fill_confirmation = _wechat_editor_confirmation_context(
        kind="draft_fill",
        action_label="填写微信公众号草稿标题、正文和摘要",
        field_selectors=list(fields.keys()),
        input_summary={
            "title_length": len(draft.title),
            "body_length": len(draft.body),
            "summary_length": len(draft.summary or ""),
            "field_count": len(fields),
        },
    )
    save_confirmation = _wechat_editor_confirmation_context(
        kind="draft_save",
        action_label="保存微信公众号草稿",
        target_selector=request.save_selector,
    )
    actions = [
        BrowserAction(
            action="open",
            profile_id=request.profile_id,
            url=request.open_url,
            access="read",
            run_id=run_id,
            reason="Open WeChat Official Account backend before writing draft",
            metadata={"domain": "mp.weixin.qq.com", "draft_id": draft.id},
        ),
        BrowserAction(
            action="snapshot",
            profile_id=request.profile_id,
            access="read",
            run_id=run_id,
            reason="Capture WeChat backend state before writing draft",
            metadata={"domain": "mp.weixin.qq.com", "draft_id": draft.id},
        ),
        BrowserAction(
            action="fill_form",
            profile_id=request.profile_id,
            access="write",
            confirmed=request.confirmed,
            run_id=run_id,
            reason="Fill WeChat Official Account draft fields after confirmation",
            metadata={
                "domain": "mp.weixin.qq.com",
                "draft_id": draft.id,
                "fields_json": fields,
                "requires_user_confirmation": True,
                "confirmation_context": fill_confirmation,
            },
        ),
    ]
    if request.save_after_fill and request.save_selector:
        actions.append(
            BrowserAction(
                action="click",
                profile_id=request.profile_id,
                selector=request.save_selector,
                access="write",
                confirmed=request.confirmed,
                run_id=run_id,
                reason="Save WeChat Official Account draft after confirmation",
                metadata={
                    "domain": "mp.weixin.qq.com",
                    "draft_id": draft.id,
                    "requires_user_confirmation": True,
                    "confirmation_context": save_confirmation,
                },
            )
        )

    observations: list[BrowserObservation] = []
    blocked_action: BrowserAction | None = None
    for action in actions:
        observation = await _broker.execute(action)
        observations.append(observation)
        if not observation.ok:
            blocked_action = action
            break
        if action.action == "snapshot":
            page_context = manual_takeover_context_from_observation(observation)
            if page_context:
                updated_draft = _content.update_publishing_draft(
                    draft.id,
                    status="blocked",
                    platform_url=request.open_url,
                    metadata={
                        "wechat_write_run_id": run_id,
                        "manual_takeover_required": True,
                        "page_context": page_context,
                        "requires_publish_confirmation": True,
                    },
                )
                if queue_item_id:
                    _content.update_publishing_queue_item(
                        queue_item_id,
                        status="blocked",
                        note="manual takeover required before platform write",
                        source="wechat_write_request",
                        metadata={
                            "wechat_write_run_id": run_id,
                            "manual_takeover_required": True,
                            "page_context": page_context,
                        },
                    )
                return {
                    "ok": False,
                    "run_id": run_id,
                    "requires_confirmation": False,
                    "requires_manual_takeover": True,
                    "safety": {
                        "publish_requires_confirmation": True,
                        "silent_publish_enabled": False,
                        "save_after_fill": request.save_after_fill,
                        "manual_takeover_required": True,
                    },
                    "draft": updated_draft.model_dump(mode="json"),
                    "observations": [
                        item.model_dump(mode="json") for item in observations
                    ],
                }

    last = observations[-1] if observations else None
    requires_confirmation = bool(
        last
        and last.decision
        and last.decision.decision == "prompt"
        and last.decision.requires_confirmation
    )
    confirmation_context = (
        blocked_action.metadata.get("confirmation_context")
        if blocked_action
        else None
    )
    if not isinstance(confirmation_context, dict):
        confirmation_context = None
    ok = bool(observations) and all(item.ok for item in observations)
    updated_draft = draft
    if ok:
        updated_draft = _content.update_publishing_draft(
            draft.id,
            status="saved_to_platform" if request.save_after_fill else "write_requested",
            platform_url=request.open_url,
            metadata={
                "wechat_write_run_id": run_id,
                "save_after_fill": request.save_after_fill,
                "requires_publish_confirmation": True,
            },
        )
        if queue_item_id:
            _content.update_publishing_queue_item(
                queue_item_id,
                status="saved" if request.save_after_fill else "write_requested",
                note="platform draft write request completed",
                source="wechat_write_request",
                metadata={
                    "wechat_write_run_id": run_id,
                    "save_after_fill": request.save_after_fill,
                },
            )
    elif queue_item_id and requires_confirmation:
        _content.update_publishing_queue_item(
            queue_item_id,
            status="needs_confirmation",
            note="broker requested explicit write confirmation",
            source="wechat_write_request",
            metadata={"wechat_write_run_id": run_id},
        )
    elif queue_item_id:
        _content.update_publishing_queue_item(
            queue_item_id,
            status="blocked",
            note="platform draft write request failed",
            source="wechat_write_request",
            metadata={"wechat_write_run_id": run_id},
        )
    return {
        "ok": ok,
        "run_id": run_id,
        "requires_confirmation": requires_confirmation,
        "confirmation": confirmation_context if requires_confirmation else None,
        "safety": {
            "publish_requires_confirmation": True,
            "silent_publish_enabled": False,
            "save_after_fill": request.save_after_fill,
        },
        "draft": updated_draft.model_dump(mode="json"),
        "observations": [item.model_dump(mode="json") for item in observations],
    }


@router.post("/publishing/queue", response_model=PublishingQueueItem)
async def queue_publishing_draft(
    request: PublishingQueueRequest,
) -> PublishingQueueItem:
    return _content.queue_publishing_draft(
        draft_id=request.draft_id,
        scheduled_for=request.scheduled_for,
        platform=request.platform,
        metadata=request.metadata,
    )


@router.get("/publishing/queue")
async def list_publishing_queue() -> dict[str, Any]:
    return {"items": _content.list_queue()}


@router.get("/publishing/queue/{queue_item_id}")
async def get_publishing_queue_item(queue_item_id: str) -> dict[str, Any]:
    try:
        return _content.get_publishing_queue_detail(queue_item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Queue item not found") from exc


@router.patch("/publishing/queue/{queue_item_id}")
async def patch_publishing_queue_item(
    queue_item_id: str,
    request: PublishingQueuePatchRequest,
) -> dict[str, Any]:
    try:
        item = _content.update_publishing_queue_item(
            queue_item_id,
            status=request.status,
            requires_confirmation=request.requires_confirmation,
            note=request.note,
            source="browser_workbench_ui",
            metadata=request.metadata,
        )
        detail = _content.get_publishing_queue_detail(item.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Queue item not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return detail


@router.get("/content/workflows")
async def list_content_workflows() -> dict[str, Any]:
    return {"items": _content.list_workflow_runs()}
