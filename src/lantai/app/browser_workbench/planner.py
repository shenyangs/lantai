# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .adapters import LantaiCliAdapterRegistry
from .models import (
    AdapterRunRequest,
    BrowserAction,
    BrowserTaskRequest,
    DomainSkillDraftRecord,
)
from .safe_scripts import safe_evaluate_metadata


@dataclass(frozen=True)
class BrowserTaskPlan:
    actions: list[BrowserAction]
    metadata: dict[str, Any]


class BrowserTaskPlanner:
    """Deterministic planner for background Browser Workbench tasks.

    This is intentionally policy-light: it only chooses a small action plan and
    adapter candidate. BrowserActionBroker remains the authority for profile,
    domain, write confirmation, manual takeover, and replay decisions.
    """

    def __init__(self, registry: LantaiCliAdapterRegistry | None = None) -> None:
        self.registry = registry or LantaiCliAdapterRegistry()

    def plan(self, request: BrowserTaskRequest) -> BrowserTaskPlan:
        if request.actions:
            actions = [
                self._tag_action(
                    action.model_copy(
                        update={
                            "profile_id": action.profile_id or request.profile_id,
                            "run_id": request.run_id,
                            "access": action.access or request.access,
                        }
                    ),
                    source="explicit_actions",
                )
                for action in request.actions
            ]
            return BrowserTaskPlan(
                actions=actions,
                metadata={
                    "strategy": "explicit_actions",
                    "source": "request.actions",
                    "adapter_id": "",
                    "expected_outputs": [],
                    "action_count": len(actions),
                },
            )

        domain_skill = self._select_domain_skill(request)
        adapter_id = self._select_adapter_id(request)
        if not adapter_id and domain_skill:
            adapter_id = domain_skill.recommended_adapter_id

        if adapter_id:
            actions, metadata = self._plan_adapter_actions(
                adapter_id,
                request,
                domain_skill=domain_skill,
            )
        elif request.access == "read":
            actions, metadata = self._plan_generic_read(request)
            if domain_skill:
                actions = [
                    self._tag_domain_skill_action(action, domain_skill)
                    for action in actions
                ]
                actions = self._with_domain_skill_recovery_preflight(
                    actions,
                    request,
                    domain_skill,
                )
                metadata = {
                    **metadata,
                    "strategy": "domain_skill_generic_read",
                    "source": "domain_skill_registry",
                    "domain_skill": self._domain_skill_metadata(domain_skill),
                }
        else:
            actions = []
            metadata = {
                "strategy": "blocked_without_explicit_actions",
                "source": "deterministic_planner",
                "adapter_id": "",
                "expected_outputs": [],
                "action_count": 0,
                "reason": "Write/sensitive browser tasks need explicit actions or an adapter.",
            }
        return BrowserTaskPlan(actions=actions, metadata=metadata)

    def _select_adapter_id(self, request: BrowserTaskRequest) -> str:
        explicit = str(
            request.metadata.get("adapter_id")
            or request.metadata.get("lantaicli_adapter_id")
            or request.metadata.get("planner_adapter_id")
            or ""
        ).strip()
        if explicit:
            return explicit

        url = self._target_url(request)
        domain = self._domain(url)
        instruction = request.instruction.lower()
        haystack = f"{instruction} {url.lower()} {' '.join(request.target_domains)}"

        if "github.com" in domain:
            if any(token in haystack for token in {"pull request", " pr ", "prs", "合并请求"}):
                return "github.prs.list"
            if any(token in haystack for token in {"issue", "issues", "工单", "缺陷"}):
                return "github.issues.list"
        if "xiaohongshu.com" in domain or "小红书" in haystack:
            return "xiaohongshu.note.collect"
        if "douyin.com" in domain or "抖音" in haystack:
            return "douyin.video.research"
        if "bilibili.com" in domain or "b站" in haystack or "bilibili" in haystack:
            return "bilibili.video.research"
        if "weibo.com" in domain or "微博" in haystack:
            return "weibo.topic.scan"
        if any(token in haystack for token in {"table", "表格"}):
            return "web.table.extract"
        if any(token in haystack for token in {"list", "列表", "清单"}):
            return "web.list.extract"
        if any(token in haystack for token in {"video", "视频"}):
            return "video.page.summarize"
        if any(token in haystack for token in {"post", "帖子", "笔记", "评论", "动态"}):
            return "social.post.collect"
        if any(token in haystack for token in {"article", "文章", "媒体", "新闻", "报道"}):
            return "media.article.extract"
        return ""

    def _plan_adapter_actions(
        self,
        adapter_id: str,
        request: BrowserTaskRequest,
        *,
        domain_skill: DomainSkillDraftRecord | None = None,
    ) -> tuple[list[BrowserAction], dict[str, Any]]:
        manifest = self.registry.get_adapter(adapter_id)
        if manifest is None:
            return [], {
                "strategy": "adapter_not_found",
                "source": "deterministic_planner",
                "adapter_id": adapter_id,
                "expected_outputs": [],
                "action_count": 0,
                "reason": f"LantaiCLI adapter not found: {adapter_id}",
            }
        inputs = self._adapter_inputs(manifest.inputs, request)
        run_request = AdapterRunRequest(
            profile_id=request.profile_id,
            inputs=inputs,
            confirmed=bool(request.metadata.get("confirmed") or False),
            run_id=request.run_id,
        )
        actions = []
        for action in self.registry.plan_actions(adapter_id, run_request):
            next_action = self._tag_action(
                action.model_copy(
                    update={
                        "access": action.access or manifest.access or request.access,
                        "metadata": {
                            **action.metadata,
                            "planner_strategy": "lantaicli_adapter",
                            "planner_adapter_id": adapter_id,
                            "adapter_outputs": manifest.outputs,
                        },
                    }
                ),
                source="lantaicli_adapter",
            )
            if domain_skill:
                next_action = self._tag_domain_skill_action(next_action, domain_skill)
            actions.append(next_action)
        actions = self._append_screenshot_if_needed(actions, request)
        if domain_skill:
            actions = [
                self._tag_domain_skill_action(action, domain_skill)
                for action in actions
            ]
            actions = self._with_domain_skill_recovery_preflight(
                actions,
                request,
                domain_skill,
            )
        metadata = {
            "strategy": "lantaicli_adapter",
            "source": "deterministic_planner",
            "adapter_id": adapter_id,
            "adapter_domain": manifest.domain,
            "adapter_strategy": manifest.strategy,
            "expected_outputs": manifest.outputs,
            "inputs": inputs,
            "action_count": len(actions),
        }
        if domain_skill:
            metadata = {
                **metadata,
                "strategy": "domain_skill_adapter",
                "source": "domain_skill_registry",
                "domain_skill": self._domain_skill_metadata(domain_skill),
            }
        return actions, metadata

    def _plan_generic_read(self, request: BrowserTaskRequest) -> tuple[list[BrowserAction], dict[str, Any]]:
        url = self._target_url(request) or "about:blank"
        actions = [
            BrowserAction(
                action="open",
                profile_id=request.profile_id,
                url=url,
                access="read",
                run_id=request.run_id,
                reason=request.instruction or "Open page for background browser task.",
                metadata={"planner_strategy": "generic_read"},
            ),
            BrowserAction(
                action="snapshot",
                profile_id=request.profile_id,
                access="read",
                run_id=request.run_id,
                reason="Capture DOM/AX summary for background browser task.",
                metadata={"planner_strategy": "generic_read"},
            ),
            BrowserAction(
                action="screenshot",
                profile_id=request.profile_id,
                access="read",
                run_id=request.run_id,
                reason="Capture screenshot evidence for background browser task.",
                metadata={"planner_strategy": "generic_read"},
            ),
        ]
        return actions, {
            "strategy": "generic_read",
            "source": "deterministic_planner",
            "adapter_id": "",
            "expected_outputs": ["snapshot", "screenshot"],
            "inputs": {"url": url},
            "action_count": len(actions),
        }

    def _adapter_inputs(
        self,
        declared_inputs: list[str],
        request: BrowserTaskRequest,
    ) -> dict[str, Any]:
        raw_inputs = request.metadata.get("adapter_inputs")
        inputs = dict(raw_inputs) if isinstance(raw_inputs, dict) else {}
        url = self._target_url(request)
        for name in declared_inputs:
            if name == "url":
                inputs.setdefault("url", url)
            elif name == "repo_url":
                inputs.setdefault("repo_url", url)
            elif name == "limit":
                inputs.setdefault("limit", request.metadata.get("limit") or 10)
            elif name == "query":
                inputs.setdefault("query", request.instruction)
            elif name == "urls":
                inputs.setdefault("urls", [url] if url else request.target_domains)
            elif name in {"theme", "topic", "title"}:
                inputs.setdefault(name, request.instruction)
            elif name == "sources":
                inputs.setdefault("sources", [])
        return inputs

    def _append_screenshot_if_needed(
        self,
        actions: list[BrowserAction],
        request: BrowserTaskRequest,
    ) -> list[BrowserAction]:
        if request.access != "read":
            return actions
        capture = request.metadata.get("capture_screenshot", True)
        if capture is False or any(action.action in {"screenshot", "take_screenshot"} for action in actions):
            return actions
        return [
            *actions,
            BrowserAction(
                action="screenshot",
                profile_id=request.profile_id,
                access="read",
                run_id=request.run_id,
                reason="Capture screenshot evidence for planned browser task.",
                metadata={"planner_strategy": "evidence_screenshot"},
            ),
        ]

    def _select_domain_skill(
        self,
        request: BrowserTaskRequest,
    ) -> DomainSkillDraftRecord | None:
        url = self._target_url(request)
        domain = self._domain(url)
        if not domain and request.target_domains:
            domain = request.target_domains[0].strip().lower()
        if not domain:
            return None
        for record in self.registry.list_domain_skill_drafts(domain=domain):
            if not record.enabled:
                continue
            if not self._domain_skill_probe_allows(record):
                continue
            if self._domain_skill_matches(record, url, domain):
                return record
        return None

    @staticmethod
    def _domain_skill_matches(
        record: DomainSkillDraftRecord,
        url: str,
        domain: str,
    ) -> bool:
        if record.draft.domain != domain:
            return False
        if not url or not record.draft.url_patterns:
            return True
        parsed = urlparse(url)
        for pattern in record.draft.url_patterns:
            parsed_pattern = urlparse(pattern)
            if (parsed_pattern.hostname or "").lower() != domain:
                continue
            path_prefix = parsed_pattern.path.split("{", 1)[0].rstrip("/")
            if not path_prefix or parsed.path.startswith(path_prefix):
                return True
        return False

    @staticmethod
    def _domain_skill_probe_allows(record: DomainSkillDraftRecord) -> bool:
        probe = record.draft.metadata.get("last_selector_probe")
        if not isinstance(probe, dict):
            return True
        if probe.get("ok") is False:
            return False
        try:
            selector_count = int(probe.get("selector_count") or 0)
            matched_count = int(probe.get("matched_selector_count") or 0)
        except (TypeError, ValueError):
            return True
        return selector_count <= 0 or matched_count > 0

    @staticmethod
    def _domain_skill_metadata(record: DomainSkillDraftRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "title": record.draft.title,
            "domain": record.draft.domain,
            "url_patterns": record.draft.url_patterns,
            "selectors": record.draft.selectors,
            "scopes": BrowserTaskPlanner._domain_skill_scopes(record),
            "wait_conditions": record.draft.wait_conditions,
            "failure_modes": record.draft.failure_modes,
            "recovery_guard": BrowserTaskPlanner._domain_skill_recovery_guard(
                record
            ),
            "recommended_adapter_id": record.recommended_adapter_id,
            "source_run_id": record.source_run_id,
        }

    def _tag_domain_skill_action(
        self,
        action: BrowserAction,
        record: DomainSkillDraftRecord,
    ) -> BrowserAction:
        scopes = self._domain_skill_scopes(record)
        scoped_update = self._scope_for_action(action, scopes)
        recovery_guard = self._domain_skill_recovery_guard(record)
        metadata = {
            **action.metadata,
            "domain_skill_id": record.id,
            "domain_skill_title": record.draft.title,
            "domain_skill_selectors": record.draft.selectors,
            "domain_skill_scopes": scopes,
            "domain_skill_wait_conditions": record.draft.wait_conditions,
            "domain_skill_failure_modes": record.draft.failure_modes,
            "domain_skill_recovery_guard": recovery_guard,
            "domain_skill_recommended_adapter_id": (
                record.recommended_adapter_id
            ),
        }
        for key, value in scoped_update.items():
            metadata.setdefault(key, value)
        return action.model_copy(
            update={
                "metadata": metadata
            }
        )

    def _with_domain_skill_recovery_preflight(
        self,
        actions: list[BrowserAction],
        request: BrowserTaskRequest,
        record: DomainSkillDraftRecord,
    ) -> list[BrowserAction]:
        guard = self._domain_skill_recovery_guard(record)
        if not guard.get("requires_selector_probe"):
            return actions
        selectors = [item for item in record.draft.selectors[:20] if item.strip()]
        if not selectors:
            return actions
        if any(action.metadata.get("domain_skill_preflight") for action in actions):
            return actions
        index = next(
            (
                idx
                for idx, action in enumerate(actions)
                if (action.access or request.access) in {"write", "sensitive"}
            ),
            min(1, len(actions)),
        )
        preflight = BrowserAction(
            action="evaluate",
            profile_id=request.profile_id,
            url=self._target_url(request),
            access="read",
            run_id=request.run_id,
            reason=(
                "Run Domain Skill selector recovery preflight before replaying "
                "a workflow with historical selector failures."
            ),
            code=self._selector_preflight_code(selectors),
            metadata={
                "planner_strategy": "domain_skill_recovery_preflight",
                "planner_source": "domain_skill_registry",
                "domain_skill_id": record.id,
                "domain_skill_title": record.draft.title,
                "domain_skill_preflight": "selector_probe",
                "domain_skill_recovery_guard": guard,
                "domain_skill_selectors": selectors,
                **safe_evaluate_metadata(
                    "domain_skill.selector_probe",
                    {"selectors": selectors},
                ),
            },
        )
        return [*actions[:index], preflight, *actions[index:]]

    @staticmethod
    def _domain_skill_recovery_guard(
        record: DomainSkillDraftRecord,
    ) -> dict[str, Any]:
        text = " ".join(record.draft.failure_modes).lower()
        if not text:
            return {}
        failure_kinds: list[str] = []
        if "element_not_found" in text or "element not found" in text:
            failure_kinds.append("element_not_found")
        if "frame_unavailable" in text or "frame is cross-origin" in text:
            failure_kinds.append("frame_unavailable")
        if "shadow_unavailable" in text or "shadow root" in text:
            failure_kinds.append("shadow_unavailable")
        if "download_failed" in text or "download" in text:
            failure_kinds.append("download_failed")
        if not failure_kinds:
            return {}
        return {
            "failure_kinds": failure_kinds,
            "requires_selector_probe": any(
                item in {"element_not_found", "frame_unavailable", "shadow_unavailable"}
                for item in failure_kinds
            ),
            "requires_scope_probe": any(
                item in {"frame_unavailable", "shadow_unavailable"}
                for item in failure_kinds
            ),
            "requires_download_check": "download_failed" in failure_kinds,
            "source": "domain_skill_failure_modes",
        }

    @staticmethod
    def _selector_preflight_code(selectors: list[str]) -> str:
        encoded = json.dumps(selectors[:20], ensure_ascii=False)
        return f"""
(() => {{
  const selectors = {encoded};
  const escapeCss = (value) => {{
    if (window.CSS && typeof window.CSS.escape === "function") {{
      return window.CSS.escape(String(value || ""));
    }}
    return String(value || "").replace(/([^a-zA-Z0-9_-])/g, "\\\\$1");
  }};
  const elementSelector = (element) => {{
    if (!element || !element.tagName) return "";
    const tag = element.tagName.toLowerCase();
    if (element.id) return `${{tag}}#${{escapeCss(element.id)}}`;
    const classes = typeof element.className === "string"
      ? element.className.split(/\\s+/).filter(Boolean).slice(0, 2)
      : [];
    const classPart = classes.map((item) => `.${{escapeCss(item)}}`).join("");
    const base = `${{tag}}${{classPart}}`;
    const parent = element.parentElement;
    if (!parent) return base;
    const siblings = Array.from(parent.children).filter((child) => child.tagName === element.tagName);
    if (siblings.length > 1) {{
      return `${{base}}:nth-of-type(${{siblings.indexOf(element) + 1}})`;
    }}
    return base;
  }};
  const countInRoot = (root, selector) => {{
    try {{
      return root.querySelectorAll(selector).length;
    }} catch (error) {{
      return 0;
    }}
  }};
  const iframeEntries = Array.from(document.querySelectorAll("iframe")).slice(0, 20).map((frame, index) => {{
    const frameSelector = elementSelector(frame) || `iframe:nth-of-type(${{index + 1}})`;
    try {{
      const doc = frame.contentDocument || frame.contentWindow?.document;
      if (!doc) {{
        return {{
          kind: "iframe",
          scope: `iframe[${{index}}]`,
          frame_selector: frameSelector,
          blocked: true,
          error: "frame_document_unavailable",
        }};
      }}
      return {{
        kind: "iframe",
        scope: `iframe[${{index}}]`,
        root: doc,
        frame_selector: frameSelector,
        blocked: false,
      }};
    }} catch (error) {{
      return {{
        kind: "iframe",
        scope: `iframe[${{index}}]`,
        frame_selector: frameSelector,
        blocked: true,
        error: "cross_origin_or_unavailable",
      }};
    }}
  }});
  const shadowEntries = Array.from(document.querySelectorAll("*"))
    .filter((element) => Boolean(element.shadowRoot))
    .slice(0, 50)
    .map((element, index) => ({{
      kind: "shadow",
      scope: `shadow[${{index}}]`,
      root: element.shadowRoot,
      shadow_host_selector: elementSelector(element),
      blocked: false,
    }}));
  const queryEntry = (entry, selector) => {{
    if (entry.blocked || !entry.root) {{
      return {{
        kind: entry.kind,
        scope: entry.scope,
        count: 0,
        blocked: true,
        frame_selector: entry.frame_selector || "",
        shadow_host_selector: entry.shadow_host_selector || "",
        error: entry.error || "unavailable",
      }};
    }}
    return {{
      kind: entry.kind,
      scope: entry.scope,
      count: countInRoot(entry.root, selector),
      blocked: false,
      frame_selector: entry.frame_selector || "",
      shadow_host_selector: entry.shadow_host_selector || "",
      error: "",
    }};
  }};
  const roots = [
    {{ kind: "document", scope: "document", root: document, blocked: false }},
    ...iframeEntries,
    ...shadowEntries,
  ];
  return selectors.map((selector) => {{
    const scopes = roots.map((entry) => queryEntry(entry, selector));
    const documentCount = scopes
      .filter((item) => item.kind === "document")
      .reduce((total, item) => total + Number(item.count || 0), 0);
    const iframeCount = scopes
      .filter((item) => item.kind === "iframe")
      .reduce((total, item) => total + Number(item.count || 0), 0);
    const shadowCount = scopes
      .filter((item) => item.kind === "shadow")
      .reduce((total, item) => total + Number(item.count || 0), 0);
    const blockedFrameCount = scopes
      .filter((item) => item.kind === "iframe" && item.blocked)
      .length;
    return {{
      selector,
      count: documentCount + iframeCount + shadowCount,
      scope_counts: {{
        document: documentCount,
        iframe: iframeCount,
        shadow: shadowCount,
      }},
      blocked_frame_count: blockedFrameCount,
      scopes: scopes.map((item) => ({{
        kind: item.kind,
        scope: item.scope,
        count: item.count,
        blocked: Boolean(item.blocked),
        frame_selector: item.frame_selector || "",
        shadow_host_selector: item.shadow_host_selector || "",
        error: item.error || "",
      }})),
    }};
  }});
}})()
""".strip()

    @staticmethod
    def _domain_skill_scopes(record: DomainSkillDraftRecord) -> list[dict[str, str]]:
        raw_scopes = record.draft.metadata.get("scopes")
        if not isinstance(raw_scopes, list):
            return []
        scopes: list[dict[str, str]] = []
        for item in raw_scopes[:40]:
            if not isinstance(item, dict):
                continue
            selector = str(item.get("selector") or "").strip()[:240]
            frame_selector = str(item.get("frame_selector") or "").strip()[:240]
            shadow_host_selector = str(
                item.get("shadow_host_selector") or ""
            ).strip()[:240]
            if not (frame_selector or shadow_host_selector):
                continue
            scope = {
                "selector": selector,
                "frame_selector": frame_selector,
                "shadow_host_selector": shadow_host_selector,
            }
            scopes.append({key: value for key, value in scope.items() if value})
        return scopes

    @staticmethod
    def _scope_for_action(
        action: BrowserAction,
        scopes: list[dict[str, str]],
    ) -> dict[str, str]:
        if action.action not in {
            "click",
            "type",
            "select",
            "select_option",
            "fill_form",
            "hover",
            "upload_file",
        }:
            return {}
        if not scopes:
            return {}
        selected = None
        if action.selector:
            selected = next(
                (
                    scope
                    for scope in scopes
                    if scope.get("selector") == action.selector
                ),
                None,
            )
        selected = selected or next(
            (scope for scope in scopes if not scope.get("selector")),
            None,
        ) or scopes[0]
        out: dict[str, str] = {}
        for key in ("frame_selector", "shadow_host_selector"):
            value = str(selected.get(key) or "").strip()
            if value:
                out[key] = value
        return out

    @staticmethod
    def _target_url(request: BrowserTaskRequest) -> str:
        url = request.url.strip()
        if url:
            return url
        if request.target_domains:
            return f"https://{request.target_domains[0]}"
        return ""

    @staticmethod
    def _domain(url: str) -> str:
        try:
            return (urlparse(url).hostname or "").lower()
        except Exception:
            return ""

    @staticmethod
    def _tag_action(action: BrowserAction, *, source: str) -> BrowserAction:
        return action.model_copy(
            update={
                "metadata": {
                    **action.metadata,
                    "planner_source": source,
                }
            }
        )
