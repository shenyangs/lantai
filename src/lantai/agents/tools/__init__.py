# -*- coding: utf-8 -*-
"""Public agent-tool API with side-effect-free, lazy imports.

Importing one tool module used to execute this package initializer and eagerly
load every browser, media, MCP, office, and collaboration dependency. That is
especially expensive in the frozen desktop backend, where a lightweight CLI
command could spend tens of seconds unpacking unrelated modules. Keep the
public ``from lantai.agents.tools import <tool>`` API while importing only the
module that owns the requested attribute.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS: dict[str, tuple[str, str]] = {
    "research_project": ("research", "research_project"),
    "research_read": ("research", "research_read"),
    "research_report": ("research", "research_report"),
    "research_review": ("research", "research_review"),
    "research_library": ("research", "research_library"),
    "research_export": ("research", "research_export"),
    "execute_python_code": ("compat_builtin", "execute_python_code"),
    "execute_shell_command": ("shell", "execute_shell_command"),
    "view_text_file": ("compat_builtin", "view_text_file"),
    "write_text_file": ("compat_builtin", "write_text_file"),
    "read_file": ("file_io", "read_file"),
    "write_file": ("file_io", "write_file"),
    "edit_file": ("file_io", "edit_file"),
    "append_file": ("file_io", "append_file"),
    "grep_search": ("file_search", "grep_search"),
    "glob_search": ("file_search", "glob_search"),
    "send_file_to_user": ("send_file", "send_file_to_user"),
    "import_data_table": ("data_analysis", "import_data_table"),
    "analyze_data_table": ("data_analysis", "analyze_data_table"),
    "get_analysis_result": ("data_analysis", "get_analysis_result"),
    "get_analysis_delivery_status": ("data_analysis", "get_analysis_delivery_status"),
    "refresh_data_table": ("data_analysis", "refresh_data_table"),
    "refresh_analysis_result": ("data_analysis", "refresh_analysis_result"),
    "export_analysis_evidence": ("data_analysis", "export_analysis_evidence"),
    "create_analysis_report": ("data_analysis", "create_analysis_report"),
    "create_analysis_chart": ("data_analysis", "create_analysis_chart"),
    "create_analysis_presentation": ("data_analysis", "create_analysis_presentation"),
    "clear_analysis_data": ("data_analysis", "clear_analysis_data"),
    "create_presentation": ("presentation", "create_presentation"),
    "get_presentation_generation_status": (
        "presentation",
        "get_presentation_generation_status",
    ),
    "retry_presentation_generation": (
        "presentation",
        "retry_presentation_generation",
    ),
    "finalize_presentation_generation": (
        "presentation",
        "finalize_presentation_generation",
    ),
    "read_skill": ("read_skill", "read_skill"),
    "copy_skill_resource": ("skill_resources", "copy_skill_resource"),
    "list_skill_resources": ("skill_resources", "list_skill_resources"),
    "run_skill_script": ("skill_resources", "run_skill_script"),
    "desktop_screenshot": ("desktop_screenshot", "desktop_screenshot"),
    "mac_inspect": ("mac_use", "mac_inspect"),
    "mac_observe": ("mac_use", "mac_observe"),
    "mac_act": ("mac_use", "mac_act"),
    "mac_verify": ("mac_use", "mac_verify"),
    "mac_session": ("mac_use", "mac_session"),
    "view_image": ("view_media", "view_image"),
    "view_video": ("view_media", "view_video"),
    "generate_image": ("image_generation", "generate_image"),
    "generate_video": ("video_generation", "generate_video"),
    "browser_use": ("browser_control", "browser_use"),
    "browser_find": ("browser_control", "browser_find"),
    "browser_task": ("browser_workbench", "browser_task"),
    "browser_workbench": ("browser_workbench", "browser_workbench"),
    "get_current_time": ("get_current_time", "get_current_time"),
    "set_user_timezone": ("get_current_time", "set_user_timezone"),
    "get_token_usage": ("get_token_usage", "get_token_usage"),
    "prepare_scheduled_task": ("scheduled_task", "prepare_scheduled_task"),
    "list_agents": ("agent_management", "list_agents"),
    "chat_with_agent": ("agent_management", "chat_with_agent"),
    "collaborate_with_agents": ("agent_management", "collaborate_with_agents"),
    "submit_to_agent": ("agent_management", "submit_to_agent"),
    "check_agent_task": ("agent_management", "check_agent_task"),
    "wiki_search": ("wiki", "wiki_search"),
    "wiki_read": ("wiki", "wiki_read"),
    "wiki_graph_neighbors": ("wiki", "wiki_graph_neighbors"),
    "wiki_ingest_status": ("wiki", "wiki_ingest_status"),
    "web_search": ("web", "web_search"),
    "web_fetch": ("web", "web_fetch"),
    "inspect_lantai_customization": (
        "customization",
        "inspect_lantai_customization",
    ),
    "prepare_lantai_customization": (
        "customization",
        "prepare_lantai_customization",
    ),
    "install_agent_skill": ("extension_management", "install_agent_skill"),
    "inspect_mcp_connectors": (
        "extension_management",
        "inspect_mcp_connectors",
    ),
    "manage_mcp_connector": (
        "extension_management",
        "manage_mcp_connector",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
