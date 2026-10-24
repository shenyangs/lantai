# -*- coding: utf-8 -*-
"""Canonical product surfaces and progressive tool groups."""

from __future__ import annotations

from dataclasses import dataclass

PROGRESSIVE_TOOL_GROUPS: dict[str, tuple[str, frozenset[str]]] = {
    "files": (
        "Read, search, analyze, create, edit, or send local files, inspect media, and run curated Skill workflows.",
        frozenset(
            {
                "read_file",
                "grep_search",
                "glob_search",
                "write_file",
                "edit_file",
                "send_file_to_user",
                "import_data_table",
                "analyze_data_table",
                "get_analysis_result",
                "get_analysis_delivery_status",
                "refresh_data_table",
                "refresh_analysis_result",
                "export_analysis_evidence",
                "create_analysis_report",
                "create_analysis_chart",
                "create_analysis_presentation",
                "clear_analysis_data",
                "desktop_screenshot",
                "view_image",
                "view_video",
                "declare_deliverable_contract",
                "finalize_deliverable",
                "read_skill",
                "list_skill_resources",
                "copy_skill_resource",
                "run_skill_script",
                "create_presentation",
                "get_presentation_generation_status",
                "record_private_editor_context",
                "stage_private_editor_candidate",
                "record_private_editor_angle_result",
                "adopt_private_editor_candidate",
                "retry_presentation_generation",
                "finalize_presentation_generation",
            }
        ),
    ),
    "browser": (
        "Use the browser workbench, browser tasks, or browser automation.",
        frozenset(
            {"browser_workbench", "browser_task", "browser_find", "browser_use"}
        ),
    ),
    "computer": (
        "Inspect, operate, verify, or stop work in a user-selected Mac application window.",
        frozenset(
            {"mac_inspect", "mac_observe", "mac_act", "mac_verify", "mac_session"}
        ),
    ),
    "research": (
        "Search sources and conduct versioned research with exact evidence, full-report review and exports.",
        frozenset(
            {
                "research_project",
                "research_read",
                "research_report",
                "research_review",
                "research_library",
                "research_export",
                "web_search",
                "web_fetch",
                "wiki_search",
                "wiki_read",
                "wiki_graph_neighbors",
                "wiki_ingest_status",
            }
        ),
    ),
    "planning": (
        "Prepare schedules, images, or customization changes.",
        frozenset(
            {
                "prepare_scheduled_task",
                "generate_image",
                "generate_video",
                "inspect_lantai_customization",
                "prepare_lantai_customization",
            }
        ),
    ),
    "collaboration": (
        "Delegate to, message, inspect, or collaborate with other agents.",
        frozenset(
            {
                "list_agents",
                "chat_with_agent",
                "collaborate_with_agents",
                "submit_to_agent",
                "check_agent_task",
            }
        ),
    ),
    "memory": (
        "Search, inspect, store, or remove durable personalized memory.",
        frozenset(
            {
                "memory_search",
                "layered_memory_search",
                "conversation_search",
                "memory_source_open",
                "memory_store",
                "memory_list",
                "memory_forget",
            }
        ),
    ),
    "connected_services": (
        "Install Agent Skills and manage MCP or connected-service capabilities.",
        frozenset(
            {
                "install_agent_skill",
                "inspect_mcp_connectors",
                "manage_mcp_connector",
            }
        ),
    ),
    "advanced": (
        "Run shell commands and inspect token/timezone utilities.",
        frozenset(
            {
                "execute_shell_command",
                "get_current_time",
                "set_user_timezone",
                "get_token_usage",
            }
        ),
    ),
}


def progressive_tool_group_for(
    *,
    origin: str,
    name: str,
    metadata: dict[str, object] | None = None,
) -> str:
    """Map one tool to its on-demand group without trusting server metadata."""

    details = metadata or {}
    if origin == "mcp":
        if details.get("official_integration") == "guanlan":
            return "research"
        if (
            details.get("official_integration") == "chrome-devtools"
            and details.get("integration_policy_version")
            == "lantai-chrome-devtools/1"
            and details.get("progressive_group") == "browser"
        ):
            return "browser"
        return "connected_services"
    if origin == "memory":
        return "memory"
    for group_name, (_description, tool_names) in PROGRESSIVE_TOOL_GROUPS.items():
        if name in tool_names:
            return group_name
    return "advanced"


@dataclass(frozen=True, slots=True)
class ProductCapability:
    id: str
    name_zh: str
    summary_zh: str
    route: str


PRODUCT_CAPABILITIES: tuple[ProductCapability, ...] = (
    ProductCapability("chat", "对话", "连续对话、工具协作与个性化陪伴。", "/chat"),
    ProductCapability("works", "作品", "统一查看、继续编辑和交付生成成果。", "/works"),
    ProductCapability("browser-workbench", "Browser Workbench", "在可见浏览器工作台中检索、阅读与执行网页任务。", "/browser-workbench"),
    ProductCapability("mac-use", "Mac 应用操作", "实验性地选择应用窗口，读取、操作并核验真实应用状态。", "/chat"),
    ProductCapability("office-co-creation", "Office 共创", "围绕文档、表格和演示文稿协同创作。", "/office"),
    ProductCapability("image-generation", "图像生成", "根据描述生成图像并进入作品流。", "/works"),
    ProductCapability("web-search", "联网搜索", "搜索公开网页、读取正文并保留来源，支持可选的 DeepSeek Search / Fetch。", "/chat"),
    ProductCapability("hosted-collaboration", "协作室", "邀请专长 Agent 在公开协作室并行贡献。", "/collaboration"),
    ProductCapability("customization-studio", "个性化工作室", "检查和调整人格、外观与交互偏好。", "/customization"),
    ProductCapability("mcp-connectors", "MCP 与连接器", "接入经授权的外部服务与工具。", "/settings/mcp"),
    ProductCapability("recovery", "恢复中心", "查看失败状态并安全重试或恢复任务。", "/recovery"),
)

_CATALOG_START = "<!-- lantai:capability-catalog:start -->"
_CATALOG_END = "<!-- lantai:capability-catalog:end -->"


def render_product_capability_reference(language: str = "zh") -> str:
    """Render a stable user-facing reference from the canonical catalog."""
    if language != "zh":
        heading = "## Current capabilities (generated from Capability Catalog)"
    else:
        heading = "## 当前能力目录（由 Capability Catalog 生成）"
    rows = [
        f"- **{item.name_zh}**（`{item.route}`）：{item.summary_zh}"
        for item in PRODUCT_CAPABILITIES
    ]
    return "\n".join([_CATALOG_START, heading, "", *rows, _CATALOG_END])
