# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from typing import Any


SAFE_EVALUATE_SCRIPT_IDS: frozenset[str] = frozenset(
    {
        "browser.page_summary",
        "domain_skill.selector_probe",
        "wechat.editor_options_probe",
        "wechat.inspect_controls",
    }
)


def safe_evaluate_script_id_from_metadata(metadata: dict[str, Any]) -> str:
    script_id = str(
        metadata.get("safe_script_id")
        or metadata.get("script_id")
        or ""
    ).strip().lower()
    return script_id if script_id in SAFE_EVALUATE_SCRIPT_IDS else ""


def safe_evaluate_script_params(metadata: dict[str, Any]) -> dict[str, Any]:
    params = metadata.get("safe_script_params")
    return params if isinstance(params, dict) else metadata


def safe_evaluate_metadata(
    script_id: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = str(script_id or "").strip().lower()
    if normalized not in SAFE_EVALUATE_SCRIPT_IDS:
        raise ValueError(f"Unknown safe browser script id: {script_id}")
    return {
        "safe_script_id": normalized,
        "safe_script_params": params or {},
    }


def safe_evaluate_script_source(
    script_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    normalized = str(script_id or "").strip().lower()
    values = params or {}
    if normalized == "browser.page_summary":
        return _page_summary_script()
    if normalized == "domain_skill.selector_probe":
        return _selector_probe_script(_string_list(values.get("selectors") or values.get("domain_skill_selectors"), 50))
    if normalized == "wechat.editor_options_probe":
        return _wechat_editor_options_script()
    if normalized == "wechat.inspect_controls":
        return _wechat_inspect_controls_script(str(values.get("selectors") or "body"))
    raise ValueError(f"Unknown safe browser script id: {script_id}")


def _string_list(value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            value = decoded
        except json.JSONDecodeError:
            value = [value]
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _page_summary_script() -> str:
    return """
() => ({
  title: document.title || "",
  url: location.href || "",
  text: (document.body?.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 8000),
  links: Array.from(document.querySelectorAll("a[href]")).slice(0, 80).map((link) => ({
    text: (link.innerText || link.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim().slice(0, 180),
    href: link.href || "",
  })),
})
""".strip()


def _selector_probe_script(selectors: list[str]) -> str:
    encoded = json.dumps(selectors, ensure_ascii=False)
    return f"""
() => {{
  const selectors = {encoded};
  const sampledNodes = Array.from(document.querySelectorAll("*")).slice(0, 5000);
  const diagnostics = {{
    document_ready_state: document.readyState,
    iframe_count: document.querySelectorAll("iframe").length,
    accessible_iframe_count: 0,
    blocked_iframe_count: document.querySelectorAll("iframe").length,
    shadow_host_count: sampledNodes.filter((node) => Boolean(node.shadowRoot)).length,
    open_shadow_root_count: sampledNodes.filter((node) => Boolean(node.shadowRoot)).length,
    sampled_node_count: sampledNodes.length,
  }};
  const sampleNodes = (nodes) => nodes.slice(0, 10).map((node) => ({{
    scope: "document",
    tag: node.tagName ? node.tagName.toLowerCase() : "",
    text: (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 180),
    href: node.href || node.querySelector?.("a[href]")?.href || "",
  }}));
  return selectors.map((selector) => {{
    try {{
      const nodes = Array.from(document.querySelectorAll(selector));
      return {{
        selector,
        ok: true,
        count: nodes.length,
        diagnostics,
        scope_counts: {{ document: nodes.length, iframe: 0, shadow: 0 }},
        blocked_frame_count: diagnostics.blocked_iframe_count,
        samples: sampleNodes(nodes),
        scopes: [{{
          scope: "document",
          kind: "document",
          ok: true,
          blocked: false,
          count: nodes.length,
          samples: sampleNodes(nodes),
          url: location.href || "",
          host: null,
          error: "",
        }}],
        error: "",
      }};
    }} catch (error) {{
      return {{
        selector,
        ok: false,
        count: 0,
        diagnostics,
        scope_counts: {{ document: 0, iframe: 0, shadow: 0 }},
        blocked_frame_count: diagnostics.blocked_iframe_count,
        samples: [],
        scopes: [],
        error: String(error && error.message ? error.message : error),
      }};
    }}
  }});
}}
""".strip()


def _wechat_editor_options_script() -> str:
    return """
() => {
  const textOf = (node) => {
    const raw = node.innerText || node.textContent || node.value || node.placeholder || node.ariaLabel || "";
    return String(raw).replace(/\\s+/g, " ").trim().slice(0, 180);
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
      ? node.className.split(/\\s+/).filter(Boolean).slice(0, 2).join(".")
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
    return { id, label, found: samples.length > 0, count: samples.length, samples };
  };
  const options = [
    matchFeature("cover", "封面", ["封面", "从正文选择", "图片库", "AI 配图", "拖拽或选择封面"]),
    matchFeature("original", "原创", ["原创", "声明原创", "未声明", "著作权保护"]),
    matchFeature("comment", "留言", ["留言", "精选公开", "留言自动", "留言管理"]),
    matchFeature("reward", "赞赏", ["赞赏", "赞赏账户"]),
  ];
  const riskPatterns = ["发布", "发表", "群发", "定时群发", "提交审核", "删除", "支付", "授权", "发送"];
  const manualPatterns = ["扫码登录", "微信扫码", "验证码", "安全验证", "二维码", "登录"];
  const risk_buttons = nodes.filter((item) => riskPatterns.some((pattern) => item.text.includes(pattern))).slice(0, 12);
  const manual_takeover_hints = nodes.filter((item) => manualPatterns.some((pattern) => item.text.includes(pattern))).slice(0, 12);
  return {
    page_title: document.title || "",
    url: location.href || "",
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


def _wechat_inspect_controls_script(selectors: str) -> str:
    encoded = json.dumps(selectors, ensure_ascii=False)
    return f"""
() => {{
  const visible = (el) => {{
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== "none" && s.visibility !== "hidden" && r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0;
  }};
  const selectors = {encoded};
  return Array.from(document.querySelectorAll(selectors)).filter(visible).slice(0, 40).map((el) => {{
    const r = el.getBoundingClientRect();
    return {{
      tag: el.tagName.toLowerCase(),
      id: el.id || "",
      className: String(el.className || ""),
      text: (el.innerText || el.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim().slice(0, 180),
      rect: {{ x: r.x, y: r.y, width: r.width, height: r.height }},
    }};
  }});
}}
""".strip()
