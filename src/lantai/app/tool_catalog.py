# -*- coding: utf-8 -*-
"""Stable product metadata for Lantai's built-in tool catalogue.

The runtime registry remains the source of truth for which callables exist.
This module only adds presentation and governance metadata used by the
management API and console.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ToolRiskHint = Literal["read_only", "workspace_change", "external_action"]


@dataclass(frozen=True)
class ToolCategory:
    """User-facing category metadata."""

    id: str
    name: str
    description: str
    order: int


TOOL_CATEGORIES: tuple[ToolCategory, ...] = (
    ToolCategory("shell", "终端", "执行命令、脚本和本地开发流程", 10),
    ToolCategory("files", "文件", "读取、创建、编辑和交付工作区文件", 20),
    ToolCategory("search", "搜索", "检索文件内容、路径与公开网络", 30),
    ToolCategory("browser", "浏览器", "网页浏览、交互、截图与可回放任务", 40),
    ToolCategory("computer", "Mac 应用", "读取、操作并核验用户选定的应用窗口", 45),
    ToolCategory("media", "多媒体", "查看与生成图片、视频等媒体内容", 50),
    ToolCategory("knowledge", "知识库", "检索、阅读和维护本地 Wiki", 60),
    ToolCategory("agents", "智能体协作", "发现、咨询和委派给其他智能体", 70),
    ToolCategory("automation", "任务与自动化", "准备定时任务和后台执行", 80),
    ToolCategory("system", "系统信息", "时间、时区和用量等运行信息", 90),
    ToolCategory("extensions", "扩展工具", "由插件注册的附加能力", 100),
)

_CATEGORY_BY_ID = {category.id: category for category in TOOL_CATEGORIES}

_TOOL_CATEGORY: dict[str, str] = {
    "execute_shell_command": "shell",
    "execute_python_code": "shell",
    "read_file": "files",
    "read_skill": "files",
    "list_skill_resources": "files",
    "copy_skill_resource": "files",
    "run_skill_script": "shell",
    "view_text_file": "files",
    "write_file": "files",
    "write_text_file": "files",
    "edit_file": "files",
    "append_file": "files",
    "send_file_to_user": "files",
    "grep_search": "search",
    "glob_search": "search",
    "web_search": "search",
    "web_fetch": "search",
    "deepseek_quick_search": "search",
    "browser_use": "browser",
    "browser_workbench": "browser",
    "browser_task": "browser",
    "desktop_screenshot": "browser",
    "mac_inspect": "computer",
    "mac_observe": "computer",
    "mac_act": "computer",
    "mac_verify": "computer",
    "mac_session": "computer",
    "view_image": "media",
    "view_video": "media",
    "generate_image": "media",
    "generate_video": "media",
    "wiki_search": "knowledge",
    "wiki_read": "knowledge",
    "wiki_graph_neighbors": "knowledge",
    "wiki_ingest_status": "knowledge",
    "list_agents": "agents",
    "chat_with_agent": "agents",
    "submit_to_agent": "agents",
    "check_agent_task": "agents",
    "prepare_scheduled_task": "automation",
    "inspect_lantai_customization": "system",
    "prepare_lantai_customization": "system",
    "get_current_time": "system",
    "set_user_timezone": "system",
    "get_token_usage": "system",
}

_TOOL_DISPLAY_NAMES: dict[str, str] = {
    "execute_shell_command": "终端",
    "execute_python_code": "Python 执行",
    "read_file": "读取文件",
    "read_skill": "读取技能",
    "list_skill_resources": "技能资源",
    "copy_skill_resource": "取用技能资源",
    "run_skill_script": "运行技能脚本",
    "view_text_file": "查看文本",
    "write_file": "写入文件",
    "write_text_file": "写入文本",
    "edit_file": "编辑文件",
    "append_file": "追加文件",
    "send_file_to_user": "交付文件",
    "grep_search": "内容搜索",
    "glob_search": "路径匹配",
    "web_search": "网页搜索",
    "web_fetch": "网页读取",
    "deepseek_quick_search": "DeepSeek 快搜",
    "browser_use": "浏览器控制",
    "browser_workbench": "浏览器工作台",
    "browser_task": "后台浏览任务",
    "desktop_screenshot": "桌面截图",
    "mac_inspect": "查看 Mac 应用",
    "mac_observe": "读取 Mac 窗口",
    "mac_act": "操作 Mac 应用",
    "mac_verify": "核验 Mac 操作",
    "mac_session": "Mac 操作会话",
    "view_image": "查看图片",
    "view_video": "查看视频",
    "generate_image": "图片生成",
    "generate_video": "视频生成",
    "wiki_search": "Wiki 搜索",
    "wiki_read": "Wiki 阅读",
    "wiki_graph_neighbors": "Wiki 关联",
    "wiki_ingest_status": "Wiki 入库状态",
    "list_agents": "列出智能体",
    "chat_with_agent": "咨询智能体",
    "submit_to_agent": "后台委派",
    "check_agent_task": "检查委派任务",
    "prepare_scheduled_task": "定时任务草稿",
    "inspect_lantai_customization": "澜台改造手册",
    "prepare_lantai_customization": "澜台改造草案",
    "get_current_time": "当前时间",
    "set_user_timezone": "设置时区",
    "get_token_usage": "Token 用量",
}

# These tools form the minimum local workspace toolchain. Availability is
# locked, while every invocation still passes through the runtime risk policy.
CORE_LOCKED_TOOLS = frozenset(
    {
        "execute_shell_command",
        "read_file",
        "read_skill",
        "list_skill_resources",
        "copy_skill_resource",
        "run_skill_script",
        "write_file",
        "edit_file",
        "grep_search",
        "glob_search",
        "web_search",
        "web_fetch",
    }
)

_READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "read_skill",
        "list_skill_resources",
        "view_text_file",
        "grep_search",
        "glob_search",
        "desktop_screenshot",
        "mac_inspect",
        "mac_observe",
        "mac_verify",
        "view_image",
        "view_video",
        "get_current_time",
        "get_token_usage",
        "list_agents",
        "check_agent_task",
        "wiki_search",
        "wiki_read",
        "wiki_graph_neighbors",
        "wiki_ingest_status",
    }
)

_EXTERNAL_ACTION_TOOLS = frozenset(
    {
        "browser_use",
        "browser_workbench",
        "browser_task",
        "generate_image",
        "generate_video",
        "send_file_to_user",
        "chat_with_agent",
        "submit_to_agent",
        "mac_act",
        "mac_session",
    }
)

_RISK_LABELS: dict[ToolRiskHint, str] = {
    "read_only": "只读观察",
    "workspace_change": "可修改工作区",
    "external_action": "可与外部交互",
}


def get_tool_catalog_metadata(
    tool_name: str,
    *,
    origin: str = "builtin",
) -> dict[str, object]:
    """Return stable display and governance metadata for one tool."""

    category_id = (
        "extensions"
        if origin == "plugin"
        else _TOOL_CATEGORY.get(tool_name, "extensions")
    )
    category = _CATEGORY_BY_ID[category_id]
    if tool_name in _READ_ONLY_TOOLS:
        risk_hint: ToolRiskHint = "read_only"
    elif origin == "plugin" or tool_name in _EXTERNAL_ACTION_TOOLS:
        risk_hint = "external_action"
    else:
        risk_hint = "workspace_change"

    return {
        "display_name": _TOOL_DISPLAY_NAMES.get(tool_name, tool_name),
        "category": category.id,
        "category_name": category.name,
        "category_description": category.description,
        "category_order": category.order,
        "source": origin,
        "source_label": "插件" if origin == "plugin" else "澜台内置",
        "locked": tool_name in CORE_LOCKED_TOOLS,
        "risk_hint": risk_hint,
        "risk_label": _RISK_LABELS[risk_hint],
    }
