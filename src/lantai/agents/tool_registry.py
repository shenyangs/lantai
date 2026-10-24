# -*- coding: utf-8 -*-
"""Build Lantai runtime tool registries from agent configuration."""

from __future__ import annotations

import logging
import inspect
import os
from pathlib import Path
from types import UnionType
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from ..constant import WORKING_DIR
from ..app.capabilities import policy as capability_policy
from ..app.capabilities.policy import CapabilityMatrixEntry
from ..security.tool_policy import _detect_distribution
from ..runtime.tool_policy import RuntimeToolPolicyMiddleware
from ..runtime.tools import (
    LantaiToolRegistry,
    McpClientSpec,
    SkillSpec,
    ToolPolicyContext,
    ToolPolicyMiddleware,
    ToolSpec,
    ToolOutputSemantics,
    runtime_blocked_tool_names,
)
from ..runtime.effects import RuntimeEffectRecoveryCapability
from .coding_mode import is_coding_mode_enabled
from .tools.coding import get_coding_mode_tool_functions
from .tools.compat_builtin import is_python_code_execution_supported
from .tools.agent_management import (
    COLLABORATION_MAX_MEMBERS,
    COLLABORATION_MIN_MEMBERS,
    DEFAULT_COLLABORATION_TIMEOUT,
)
from .tools import (
    research_project,
    research_read,
    research_report,
    research_review,
    research_library,
    research_export,
    browser_find,
    browser_task,
    browser_use,
    browser_workbench,
    chat_with_agent,
    check_agent_task,
    collaborate_with_agents,
    copy_skill_resource,
    create_presentation,
    import_data_table,
    analyze_data_table,
    get_analysis_result,
    get_analysis_delivery_status,
    refresh_data_table,
    refresh_analysis_result,
    export_analysis_evidence,
    create_analysis_report,
    create_analysis_chart,
    create_analysis_presentation,
    clear_analysis_data,
    finalize_presentation_generation,
    get_presentation_generation_status,
    retry_presentation_generation,
    desktop_screenshot,
    mac_act,
    mac_inspect,
    mac_observe,
    mac_session,
    mac_verify,
    edit_file,
    execute_shell_command,
    get_current_time,
    generate_image,
    generate_video,
    get_token_usage,
    glob_search,
    grep_search,
    list_agents,
    list_skill_resources,
    prepare_scheduled_task,
    read_file,
    read_skill,
    run_skill_script,
    send_file_to_user,
    set_user_timezone,
    submit_to_agent,
    view_image,
    view_video,
    web_fetch,
    web_search,
    inspect_lantai_customization,
    inspect_mcp_connectors,
    install_agent_skill,
    manage_mcp_connector,
    prepare_lantai_customization,
    wiki_graph_neighbors,
    wiki_ingest_status,
    wiki_read,
    wiki_search,
    write_file,
)
from .skills_manager import (
    ensure_skills_initialized,
    get_workspace_skills_dir,
    resolve_effective_skills,
)
from .tools.wiki import wiki_read_for_sources, wiki_search_for_sources

logger = logging.getLogger(__name__)

_RUNTIME_OWNED_TOOL_NAMES = frozenset(
    {
        "equip_lantai_tools",
        "reset_equipped_tools",
        # Kept reserved for compatibility with Pi-style discovery naming.
        "discover_tools",
        "declare_deliverable_contract",
        "resolve_pending_deliverable_choice",
        "finalize_deliverable",
        "coding_ast_search",
        "coding_lsp_diagnostics",
    },
)

_SUBPROCESS_TOOLS = frozenset(
    {"execute_shell_command", "execute_python_code", "run_skill_script"},
)

_READ_ONLY_BUILTIN_TOOLS = frozenset(
    {
        "read_file",
        "get_analysis_result",
        "get_analysis_delivery_status",
        "read_skill",
        "list_skill_resources",
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
        "get_presentation_generation_status",
        "list_agents",
        "check_agent_task",
        "wiki_search",
        "wiki_read",
        "wiki_graph_neighbors",
        "wiki_ingest_status",
        "web_search",
        "web_fetch",
        "browser_find",
        "inspect_lantai_customization",
        "inspect_mcp_connectors",
    },
)
_DESTRUCTIVE_BUILTIN_TOOLS = frozenset(
    {
        "execute_shell_command",
        "execute_python_code",
        "write_file",
        "edit_file",
        "browser_workbench",
        "browser_task",
        "browser_use",
        "mac_act",
        "mac_session",
        "send_file_to_user",
        "import_data_table",
        "analyze_data_table",
        "refresh_data_table",
        "refresh_analysis_result",
        "export_analysis_evidence",
        "create_analysis_report",
        "create_analysis_chart",
        "create_analysis_presentation",
        "clear_analysis_data",
        "create_presentation",
        "retry_presentation_generation",
        "finalize_presentation_generation",
        "set_user_timezone",
        "prepare_scheduled_task",
        "generate_image",
        "generate_video",
        "run_skill_script",
        "copy_skill_resource",
        "prepare_lantai_customization",
        "install_agent_skill",
        "manage_mcp_connector",
    },
)


def authoritative_reserved_tool_names() -> frozenset[str]:
    """Return names whose identity is owned by Lantai rather than plugins."""

    from . import tools as tools_module
    from ..runtime.capability_catalog import PROGRESSIVE_TOOL_GROUPS

    progressive_names = frozenset(
        tool_name
        for _description, tool_names in PROGRESSIVE_TOOL_GROUPS.values()
        for tool_name in tool_names
    )
    return (
        frozenset(getattr(tools_module, "__all__", ()))
        | progressive_names
        | _RUNTIME_OWNED_TOOL_NAMES
    )


def _typed_tool_risk(tool_name: str, *, origin: str) -> dict[str, Any]:
    """Return explicit risk metadata; unknown tools remain fail-closed."""

    read_only = origin == "builtin" and tool_name in _READ_ONLY_BUILTIN_TOOLS
    return {
        "read_only": read_only,
        "concurrency_safe": read_only,
        "destructive": (
            origin == "builtin" and tool_name in _DESTRUCTIVE_BUILTIN_TOOLS
        ),
        "max_result_size": 1_048_576 if read_only else None,
    }


def _tool_output_semantics(tool_name: str) -> ToolOutputSemantics:
    """Declare artifact meaning explicitly for built-in file mutations."""

    if tool_name in {"write_file", "edit_file"}:
        return ToolOutputSemantics(
            artifact_role="candidate",
            argument_path_fields=("file_path",),
            source_adapter="legacy_tool_adapter",
        )
    if tool_name == "send_file_to_user":
        return ToolOutputSemantics(
            artifact_role="delivery",
            argument_path_fields=("file_path",),
            content_media_types=("file", "image", "audio", "video"),
            source_adapter="legacy_tool_adapter",
        )
    if tool_name == "research_export":
        return ToolOutputSemantics(
            artifact_role="delivery",
            result_path_fields=("research_artifact_path",),
            content_media_types=("file",),
            source_adapter="declared",
        )
    if tool_name in {
        "create_presentation",
        "create_analysis_presentation",
        "create_analysis_report",
        "finalize_presentation_generation",
    }:
        return ToolOutputSemantics(
            artifact_role="delivery",
            argument_path_fields=("output_filename",),
            content_media_types=("file",),
            source_adapter="declared",
        )
    if tool_name == "create_analysis_chart":
        return ToolOutputSemantics(
            artifact_role="delivery",
            argument_path_fields=("output_filename",),
            content_media_types=("image",),
            source_adapter="declared",
        )
    if tool_name == "export_analysis_evidence":
        return ToolOutputSemantics(artifact_role="candidate", source_adapter="declared")
    return ToolOutputSemantics()


def _effective_distribution(
    request_context: dict[str, Any] | None = None,
) -> str:
    request_context = dict(request_context or {})
    raw_distribution = request_context.get("distribution")
    ambient_distribution = _detect_distribution()
    requested_distribution = (
        _detect_distribution(str(raw_distribution))
        if raw_distribution is not None
        else ambient_distribution
    )
    return (
        "app_store"
        if "app_store" in {ambient_distribution, requested_distribution}
        else "developer_id"
    )


def _tool_capability_id(
    tool_name: str,
    *,
    origin: str | None = None,
) -> str | None:
    if tool_name in _SUBPROCESS_TOOLS or origin == "coding":
        return "shell_python_subprocess"
    if tool_name in {
        "web_search",
        "web_fetch",
        "manage_mcp_connector",
    }:
        return "mcp_plugin_network"
    if origin in {"plugin", "mcp"}:
        return "mcp_plugin_network"
    return None


def tool_registration_matrix_entry(
    tool_name: str,
    request_context: dict[str, Any] | None = None,
    *,
    origin: str | None = None,
) -> CapabilityMatrixEntry | None:
    capability_id = _tool_capability_id(tool_name, origin=origin)
    if capability_id is None:
        return None
    return capability_policy.capability_matrix_entry(
        capability_id,
        distribution=_effective_distribution(request_context),
    )


def _json_schema_for_annotation(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Signature.empty:
        return {}
    if is_typeddict(annotation):
        return {
            "type": "object",
            "properties": {
                name: _json_schema_for_annotation(value)
                for name, value in get_type_hints(annotation).items()
            },
            "required": sorted(annotation.__required_keys__),
        }
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation in (dict, Dict):
        return {"type": "object"}
    if annotation in (list, List):
        return {"type": "array"}

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        # Keep the model-visible choices identical to the executor's contract.
        values = list(args)
        schema = _json_schema_for_annotation(type(values[0])) if values else {}
        return {**schema, "enum": values}
    if origin in (list, List):
        item_schema = _json_schema_for_annotation(args[0]) if args else {}
        return {"type": "array", "items": item_schema or {}}
    if origin in (dict, Dict):
        schema = {"type": "object"}
        if len(args) == 2:
            schema["additionalProperties"] = _json_schema_for_annotation(args[1]) or {}
        return schema
    if origin in (Union, UnionType):
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1:
            return _json_schema_for_annotation(non_none[0])

    return {}


def _input_schema_from_callable(tool_func: Callable[..., Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(tool_func)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}}

    try:
        type_hints = get_type_hints(tool_func)
    except (NameError, TypeError):
        type_hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    hidden_params = set(getattr(tool_func, "__lantai_hidden_params__", set()) or set())
    for name, parameter in signature.parameters.items():
        if name in {"self", "cls"}:
            continue
        if name in hidden_params:
            continue
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        schema = _json_schema_for_annotation(
            type_hints.get(name, parameter.annotation)
        )
        if not schema:
            schema = {"type": "string"}
        properties[name] = schema
        if parameter.default is inspect.Signature.empty:
            required.append(name)

    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        result["required"] = required
    return result


def _tool_enabled_maps(agent_config: Any) -> tuple[dict[str, bool], dict[str, bool]]:
    enabled_tools: dict[str, bool] = {}
    async_execution_tools: dict[str, bool] = {}
    try:
        if hasattr(agent_config, "tools") and hasattr(
            agent_config.tools,
            "builtin_tools",
        ):
            builtin_tools = agent_config.tools.builtin_tools
            enabled_tools = {
                name: tool.enabled for name, tool in builtin_tools.items()
            }
            async_execution_tools = {
                "execute_shell_command": builtin_tools.get(
                    "execute_shell_command",
                ).async_execution
                if "execute_shell_command" in builtin_tools
                else False,
            }
    except Exception as e:  # pylint: disable=broad-except
        logger.warning(
            "Failed to load agent tools config: %s, all tools will be disabled",
            e,
        )
    return enabled_tools, async_execution_tools


def _builtin_tool_functions() -> dict[str, Callable[..., Any]]:
    tool_functions = {
        "research_project": research_project,
        "research_read": research_read,
        "research_report": research_report,
        "research_review": research_review,
        "research_library": research_library,
        "research_export": research_export,
        "execute_shell_command": execute_shell_command,
        "read_file": read_file,
        "read_skill": read_skill,
        "list_skill_resources": list_skill_resources,
        "copy_skill_resource": copy_skill_resource,
        "run_skill_script": run_skill_script,
        "write_file": write_file,
        "edit_file": edit_file,
        "grep_search": grep_search,
        "glob_search": glob_search,
        "browser_workbench": browser_workbench,
        "browser_task": browser_task,
        "browser_find": browser_find,
        "browser_use": browser_use,
        "desktop_screenshot": desktop_screenshot,
        "mac_inspect": mac_inspect,
        "mac_observe": mac_observe,
        "mac_act": mac_act,
        "mac_verify": mac_verify,
        "mac_session": mac_session,
        "view_image": view_image,
        "view_video": view_video,
        "send_file_to_user": send_file_to_user,
        "import_data_table": import_data_table,
        "analyze_data_table": analyze_data_table,
        "get_analysis_result": get_analysis_result,
        "get_analysis_delivery_status": get_analysis_delivery_status,
        "refresh_data_table": refresh_data_table,
        "refresh_analysis_result": refresh_analysis_result,
        "export_analysis_evidence": export_analysis_evidence,
        "create_analysis_report": create_analysis_report,
        "create_analysis_chart": create_analysis_chart,
        "create_analysis_presentation": create_analysis_presentation,
        "clear_analysis_data": clear_analysis_data,
        "create_presentation": create_presentation,
        "retry_presentation_generation": retry_presentation_generation,
        "finalize_presentation_generation": finalize_presentation_generation,
        "get_presentation_generation_status": get_presentation_generation_status,
        "get_current_time": get_current_time,
        "set_user_timezone": set_user_timezone,
        "get_token_usage": get_token_usage,
        "prepare_scheduled_task": prepare_scheduled_task,
        "list_agents": list_agents,
        "chat_with_agent": chat_with_agent,
        "collaborate_with_agents": collaborate_with_agents,
        "submit_to_agent": submit_to_agent,
        "check_agent_task": check_agent_task,
        "wiki_search": wiki_search,
        "wiki_read": wiki_read,
        "wiki_graph_neighbors": wiki_graph_neighbors,
        "wiki_ingest_status": wiki_ingest_status,
        "web_search": web_search,
        "web_fetch": web_fetch,
        "inspect_lantai_customization": inspect_lantai_customization,
        "inspect_mcp_connectors": inspect_mcp_connectors,
        "prepare_lantai_customization": prepare_lantai_customization,
        "install_agent_skill": install_agent_skill,
        "manage_mcp_connector": manage_mcp_connector,
    }
    try:
        from ..config import load_config

        image_config = load_config().image_generation
        if image_config.enabled and image_config.models:
            tool_functions["generate_image"] = generate_image
    except Exception:  # pragma: no cover - config boundary
        logger.debug("Image generation tool is unavailable", exc_info=True)
    try:
        from .tools.video_generation import is_video_generation_available

        if is_video_generation_available():
            tool_functions["generate_video"] = generate_video
    except Exception:  # pragma: no cover - config boundary
        logger.debug("Video generation tool is unavailable", exc_info=True)
    return tool_functions


def _customization_allowed_tools() -> set[str] | None:
    """Return an active Profile tool allowlist; Factory remains unrestricted."""

    try:
        from ..customization.service import CustomizationService

        service = CustomizationService(
            WORKING_DIR / "customization",
            settings_path=WORKING_DIR / "settings.json",
        )
        effective = service.store.get_effective_state(service.registry)
        source = effective.sources.get("permissions.tools.allowed")
        values = effective.values.get("permissions.tools.allowed")
        if source is None or source.layer != "profile" or not isinstance(values, list):
            return None
        return {str(value) for value in values}
    except Exception:  # pragma: no cover - fail-open recovery boundary
        logger.warning("Unable to resolve customization tool policy", exc_info=True)
        return None


def is_tool_registration_allowed(
    tool_name: str,
    request_context: dict[str, Any] | None = None,
    *,
    origin: str | None = None,
    use: capability_policy.CapabilityGateUse = "automatic",
) -> bool:
    distribution = _effective_distribution(request_context)
    entry = tool_registration_matrix_entry(
        tool_name,
        request_context,
        origin=origin,
    )
    if entry is not None and not capability_policy.capability_matrix_allows(
        entry.id,
        distribution=distribution,
        use=use,
    ):
        return False
    if tool_name == "execute_python_code":
        return is_python_code_execution_supported(distribution=distribution)
    return True


def build_lantai_tool_registry(
    *,
    agent_config: Any,
    request_context: dict[str, Any] | None = None,
    approval_service: Any | None = None,
    is_coding_mode_enabled_fn: Callable[[Any], bool] | None = None,
    coding_tool_functions_factory: Callable[
        [Any],
        dict[str, Callable[..., Any]],
    ]
    | None = None,
    policy_middlewares: Iterable[ToolPolicyMiddleware] | None = None,
    include_default_policy_middleware: bool = True,
) -> LantaiToolRegistry:
    """Build the runtime-owned registry for built-in and plugin tools."""

    request_context = dict(request_context or {})
    blocked_tool_names = runtime_blocked_tool_names(request_context)
    is_coding_mode_enabled_fn = (
        is_coding_mode_enabled_fn or is_coding_mode_enabled
    )
    coding_tool_functions_factory = (
        coding_tool_functions_factory or get_coding_mode_tool_functions
    )
    enabled_tools, async_execution_tools = _tool_enabled_maps(agent_config)
    tool_functions = _builtin_tool_functions()
    explicit_tool_specs: dict[str, ToolSpec] = {}
    from .tools.skill_resources import bind_skill_channel

    for name in (
        "read_skill", "list_skill_resources", "copy_skill_resource", "run_skill_script",
    ):
        tool_functions[name] = bind_skill_channel(
            tool_functions[name], str(request_context.get("channel") or "console"),
        )
    raw_knowledge_sources = request_context.get("knowledge_sources")
    exact_source_paths = [
        str(item.get("path") or "").strip()
        for item in raw_knowledge_sources
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    ] if isinstance(raw_knowledge_sources, list) else []
    if (
        exact_source_paths
        and request_context.get("knowledge_scope_mode", "exact") == "exact"
    ):
        async def exact_scope_wiki_search(
            query: str,
            limit: int = 8,
        ) -> Any:
            """Search only the knowledge sources selected for this chat."""

            return await wiki_search_for_sources(query, exact_source_paths, limit)

        async def exact_scope_wiki_read(path: str) -> Any:
            """Read only content linked to sources selected for this chat."""

            return await wiki_read_for_sources(path, exact_source_paths)

        tool_functions["wiki_search"] = exact_scope_wiki_search
        tool_functions["wiki_read"] = exact_scope_wiki_read
        # Bind the same exact-source boundary for both Runtime adapters. The
        # restriction comes from trusted request context, never document text.
        from .tools.research import selected_research_sources
        from functools import wraps

        def scoped_research(function):
            @wraps(function)
            async def invoke(*args, **kwargs):
                token = selected_research_sources.set(tuple(exact_source_paths))
                try:
                    return await function(*args, **kwargs)
                finally:
                    selected_research_sources.reset(token)
            # Resolve postponed annotations in the original module. wraps copies
            # their strings, but this wrapper has a different globals namespace.
            invoke.__annotations__ = get_type_hints(function)
            return invoke

        for name in ("research_project", "research_read", "research_report", "research_review", "research_library", "research_export"):
            tool_functions[name] = scoped_research(tool_functions[name])
    customization_allowed_tools = _customization_allowed_tools()
    tool_origins = {name: "builtin" for name in tool_functions}

    if is_coding_mode_enabled_fn(request_context):
        for tool_name, tool_func in coding_tool_functions_factory(
            request_context,
        ).items():
            tool_functions[tool_name] = tool_func
            tool_origins[tool_name] = "coding"
            logger.debug("Discovered Coding Mode tool: %s", tool_name)

    default_enabled_tools = set(tool_functions.keys())

    from ..plugins.registry import PluginRegistry

    for tool_name, registration in PluginRegistry().get_all_tools().items():
        if tool_name in tool_functions:
            logger.warning(
                "Plugin '%s' cannot override existing tool '%s'",
                registration.plugin_id,
                tool_name,
            )
            continue
        tool_functions[tool_name] = registration.executor
        tool_origins[tool_name] = "plugin"
        explicit_tool_specs[tool_name] = registration.spec.model_copy(deep=True)

    from . import tools as tools_module

    for tool_name in getattr(tools_module, "__all__", []):
        if tool_name in tool_functions:
            continue
        tool_func = getattr(tools_module, tool_name, None)
        if callable(tool_func):
            tool_functions[tool_name] = tool_func
            tool_origins[tool_name] = "plugin"
            logger.debug("Discovered plugin tool: %s", tool_name)

    registry = LantaiToolRegistry()
    registry.block_tools(blocked_tool_names)
    if include_default_policy_middleware:
        registry.register_policy_middleware(
            RuntimeToolPolicyMiddleware(approval_service),
        )
    for middleware in policy_middlewares or ():
        registry.register_policy_middleware(middleware)

    for tool_name, tool_func in tool_functions.items():
        if tool_name in blocked_tool_names:
            logger.info("Runtime context blocked tool: %s", tool_name)
            continue
        origin = tool_origins.get(tool_name, "plugin")
        if (
            customization_allowed_tools is not None
            and tool_name not in customization_allowed_tools
            and tool_name
            not in {"inspect_lantai_customization", "prepare_lantai_customization"}
        ):
            logger.info("Customization Profile skipped tool: %s", tool_name)
            continue
        if os.environ.get("LANTAI_RECOVERY_SAFE_MODE") == "1" and (
            origin != "builtin" or tool_name not in _READ_ONLY_BUILTIN_TOOLS
        ):
            logger.info("Safe recovery mode skipped tool: %s", tool_name)
            continue
        if not is_tool_registration_allowed(
            tool_name,
            request_context,
            origin=origin,
        ):
            logger.debug(
                "Skipped unavailable tool for distribution/runtime: %s",
                tool_name,
            )
            continue

        if origin == "plugin" and tool_name not in enabled_tools:
            logger.debug("Skipped unconfigured plugin tool: %s", tool_name)
            continue

        enabled = enabled_tools.get(tool_name, tool_name in default_enabled_tools)
        if not enabled:
            logger.debug("Skipped disabled tool: %s", tool_name)
            continue

        explicit_spec = explicit_tool_specs.get(tool_name)
        if explicit_spec is not None:
            explicit_metadata = dict(explicit_spec.metadata)
            explicit_metadata.pop("assistant_artifact_projection", None)
            registry.register_tool(
                explicit_spec.model_copy(
                    update={
                        "enabled": True,
                        "origin": origin,
                        "metadata": explicit_metadata,
                    },
                ),
                tool_func,
            )
            logger.debug(
                "Registered explicit plugin tool spec: %s",
                tool_name,
            )
            continue

        async_exec = async_execution_tools.get(tool_name, False)
        tool_metadata: dict[str, Any] = {}
        if tool_name in {"research_read", "research_report"} and origin == "builtin":
            # These built-in transactional research tools can prove that a
            # validation error rolled back. Overrides do not inherit this trust.
            tool_metadata.update(runtime_owned=True, trusted_effect_settlement=True)
        if tool_name == "create_analysis_chart" and origin == "builtin":
            tool_metadata["assistant_artifact_projection"] = (
                "analysis_chart_reference"
            )
        input_schema = _input_schema_from_callable(tool_func)
        if tool_name == "create_presentation":
            from .tools.presentation import CREATE_PRESENTATION_INPUT_SCHEMA

            input_schema = CREATE_PRESENTATION_INPUT_SCHEMA
            if origin == "builtin":
                # Only our implementation can attest that pure argument or
                # source-binding checks rejected the artifact effect before
                # jobs or delivery. Extraction failures remain uncertain.
                # Same-name coding/plugin tools must not inherit this trust.
                tool_metadata.update(
                    runtime_owned=True,
                    trusted_effect_settlement=True,
                )
        if tool_name == "create_analysis_presentation":
            from .tools.data_analysis import analysis_presentation_input_schema

            input_schema = analysis_presentation_input_schema()
        if tool_name == "create_analysis_report":
            from .tools.data_analysis import analysis_report_input_schema

            input_schema = analysis_report_input_schema()
        if tool_name == "analyze_data_table":
            from .tools.data_analysis import analysis_plan_input_schema

            input_schema = analysis_plan_input_schema()
        if tool_name == "list_skill_resources":
            # Resource enumeration is a discovery/control-plane action. One
            # success can advance the turn, but varying a missing skill name
            # must not reset the no-progress circuit breaker indefinitely.
            tool_metadata["progress_policy"] = "first_tool_success"
        if tool_name == "collaborate_with_agents":
            input_schema["properties"]["tasks"]["minItems"] = 1
            input_schema["properties"]["tasks"]["maxItems"] = (
                COLLABORATION_MAX_MEMBERS
            )
            input_schema["properties"]["tasks"]["items"] = {
                "type": "object",
                "properties": {
                    "to_agent": {
                        "type": "string",
                        "description": (
                            "Configured agent ID or unique display name."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "Private, self-contained task prompt for this member."
                        ),
                    },
                    "label": {
                        "type": "string",
                        "description": "Short user-visible assignment label.",
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": COLLABORATION_MAX_MEMBERS - 1,
                        "description": (
                            "Upstream agent IDs or labels whose accepted "
                            "results must be available before this task starts."
                        ),
                    },
                    "minimum_output_chars": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100000,
                        "description": (
                            "Minimum accepted text length; use zero when "
                            "verified files are the primary deliverable."
                        ),
                    },
                    "required_output_markers": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 12,
                        "description": (
                            "Markers that must all appear in the accepted "
                            "member response."
                        ),
                    },
                    "allowed_tools": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "maxItems": 64,
                        "description": (
                            "Exact tool allowlist required for execution mode; "
                            "normal runtime policy still applies."
                        ),
                    },
                    "resource_claims": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 24,
                        "description": (
                            "Exclusive Runtime resources such as browser, "
                            "desktop-input, or service:4173. Browser and "
                            "desktop claims are also inferred from tools."
                        ),
                    },
                    "expected_artifacts": {
                        "type": "array",
                        "maxItems": 24,
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": (
                                        "POSIX path relative to the member's "
                                        "isolated workspace."
                                    ),
                                },
                                "media_type": {
                                    "type": "string",
                                    "default": "application/octet-stream",
                                },
                                "minimum_bytes": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 2000000000,
                                    "default": 1,
                                },
                            },
                            "required": ["path"],
                            "additionalProperties": False,
                        },
                        "description": (
                            "Files that must pass path, type, size and hash "
                            "verification before the member can complete."
                        ),
                    },
                },
                "required": ["to_agent", "text", "label"],
                "additionalProperties": False,
            }
            input_schema["properties"]["protocol"] = {
                "type": "string",
                "enum": ["parallel_consultation", "roundtable"],
                "description": (
                    "Use roundtable only for an explicit meeting-hall or "
                    "roundtable request; it adds one bounded cross-review pass."
                ),
            }
            input_schema["properties"]["mode"] = {
                "type": "string",
                "enum": ["consultation", "execution"],
                "description": (
                    "Consultation is read-only. Execution grants each member "
                    "a separate workspace plus its exact declared tool subset."
                ),
            }
            input_schema.setdefault("allOf", []).append(
                {
                    "if": {
                        "properties": {"mode": {"const": "execution"}},
                        "required": ["mode"],
                    },
                    "then": {
                        "properties": {"tasks": {"minItems": 1}},
                    },
                    "else": {
                        "properties": {"tasks": {"minItems": 2}},
                    },
                }
            )
            input_schema["properties"]["team_id"] = {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$",
                "maxLength": 160,
                "description": (
                    "Optional stable execution-team key used for idempotent "
                    "resume after a process interruption."
                ),
            }
            input_schema["properties"]["max_member_attempts"] = {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": (
                    "Root attempt budget shared by every member and retry."
                ),
            }
            input_schema["properties"]["max_team_wall_seconds"] = {
                "type": "integer",
                "minimum": 60,
                "maximum": 14400,
                "default": 1800,
                "description": (
                    "Root wall-clock budget including resource waits and "
                    "the reserved parent synthesis window."
                ),
            }
            input_schema["properties"]["max_artifact_bytes"] = {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000000000,
                "default": 536870912,
                "description": (
                    "Root byte budget shared by all verified artifacts."
                ),
            }
            tool_metadata.update(
                {
                    "progressive_tool": "collaboration",
                    # One recoverable member timeout is retried once. Keep
                    # the outer tool deadline beyond both attempts so the
                    # final batch snapshot can always be emitted.
                    "progressive_timeout_seconds": (
                        DEFAULT_COLLABORATION_TIMEOUT * 2 + 60
                    ),
                },
            )
        effect_recovery = RuntimeEffectRecoveryCapability()
        if tool_name == "research_review" and origin == "builtin":
            effect_recovery = RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="natural_key",
                probe="not_required",
                compensation="not_required",
                on_ambiguity="safe_to_retry",
                notes=(
                    "Built-in review resumes hash-bound report/context/reviewer "
                    "checkpoints and reuses its committed review. Interrupted "
                    "inference usage remains charged or explicitly unknown; "
                    "unfinished model calls may run again within the budget."
                ),
            )
        elif tool_name == "mac_act":
            effect_recovery = RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="not_idempotent",
                probe="tool_specific",
                compensation="manual_or_domain_specific",
                on_ambiguity="probe_required",
                notes=(
                    "A GUI action may have reached the target before transport "
                    "failure. Observe the bound target before any retry."
                ),
            )
        elif tool_name == "mac_session":
            effect_recovery = RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="natural_key",
                probe="tool_specific",
                compensation="not_required",
                on_ambiguity="probe_required",
                notes="Session control is keyed by run_id and can be inspected.",
            )
        elif tool_name == "retry_presentation_generation":
            effect_recovery = RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="natural_key",
                probe="tool_specific",
                compensation="not_required",
                on_ambiguity="probe_required",
                notes=(
                    "Retry is keyed by the durable presentation job_id. Probe "
                    "that job before another retry after an ambiguous wait."
                ),
            )
        registry.register_tool(
            ToolSpec(
                name=tool_name,
                description=(getattr(tool_func, "__doc__", "") or "").strip(),
                input_schema=input_schema,
                origin=origin,  # type: ignore[arg-type]
                enabled=True,
                async_execution=async_exec,
                **_typed_tool_risk(tool_name, origin=origin),
                output_semantics=_tool_output_semantics(tool_name),
                effect_recovery=effect_recovery,
                metadata=tool_metadata,
            ),
            tool_func,
        )
        logger.debug(
            "Registered tool spec: %s (origin=%s async_execution=%s)",
            tool_name,
            origin,
            async_exec,
        )

    return registry


def build_lantai_skill_registry(
    *,
    workspace_dir: Path | str | None,
    request_context: dict[str, Any] | None = None,
    ensure_skills_initialized_fn: Callable[[Path | str], Any] | None = None,
    resolve_effective_skills_fn: Callable[
        [Path | str, str],
        list[str],
    ]
    | None = None,
    get_workspace_skills_dir_fn: Callable[
        [Path],
        Path,
    ]
    | None = None,
) -> LantaiToolRegistry:
    """Build a registry containing effective workspace skills."""

    resolved_workspace = Path(workspace_dir or WORKING_DIR)
    request_context = dict(request_context or {})
    channel_name = request_context.get("channel", "console")
    ensure_skills_initialized_fn = (
        ensure_skills_initialized_fn or ensure_skills_initialized
    )
    resolve_effective_skills_fn = (
        resolve_effective_skills_fn or resolve_effective_skills
    )
    get_workspace_skills_dir_fn = (
        get_workspace_skills_dir_fn or get_workspace_skills_dir
    )

    ensure_skills_initialized_fn(resolved_workspace)
    effective_skills = resolve_effective_skills_fn(
        resolved_workspace,
        channel_name,
    )
    working_skills_dir = get_workspace_skills_dir_fn(resolved_workspace)

    registry = LantaiToolRegistry()
    for skill_name in effective_skills:
        skill_dir = working_skills_dir / skill_name
        if not skill_dir.exists():
            continue
        registry.register_skill(
            SkillSpec(
                name=skill_name,
                path=str(skill_dir),
            ),
        )
    return registry


def build_memory_tool_registry(
    memory_manager: Any,
) -> LantaiToolRegistry:
    """Build a registry for memory-manager provided tools."""

    registry = LantaiToolRegistry()
    if memory_manager is None:
        return registry

    for tool_fn in memory_manager.list_memory_tools():
        tool_name = getattr(tool_fn, "__name__", repr(tool_fn))
        registry.register_tool(
            ToolSpec(
                name=tool_name,
                description=(getattr(tool_fn, "__doc__", "") or "").strip(),
                input_schema=_input_schema_from_callable(tool_fn),
                origin="memory",
            ),
            tool_fn,
        )
    return registry


def mcp_client_spec_from_client(client: Any) -> McpClientSpec:
    """Build a runtime MCP client spec from a configured MCP client."""

    client_name = getattr(client, "name", repr(client))
    execution_timeout = getattr(client, "read_timeout_seconds", None)
    metadata: dict[str, Any] = {}
    from ..app.mcp.chrome_devtools import (
        CHROME_DEVTOOLS_INTEGRATION_POLICY_VERSION,
        is_official_chrome_devtools_client,
    )
    from ..app.mcp.guanlan_native import (
        GUANLAN_NATIVE_POLICY_VERSION,
        is_official_guanlan_client,
    )

    if is_official_chrome_devtools_client(client):
        metadata.update(
            {
                "official_integration": "chrome-devtools",
                "integration_policy_version": (
                    CHROME_DEVTOOLS_INTEGRATION_POLICY_VERSION
                ),
            },
        )
    elif is_official_guanlan_client(client):
        metadata.update(
            {
                "official_integration": "guanlan",
                "integration_policy_version": (
                    GUANLAN_NATIVE_POLICY_VERSION
                ),
            },
        )
    return McpClientSpec(
        name=str(client_name),
        client=client,
        execution_timeout=execution_timeout,
        metadata=metadata,
    )


def build_mcp_client_registry(
    mcp_clients: Iterable[Any] | None,
    *,
    request_context: dict[str, Any] | None = None,
) -> LantaiToolRegistry:
    """Build a registry containing MCP client registrations."""

    registry = LantaiToolRegistry()
    if os.environ.get("LANTAI_RECOVERY_SAFE_MODE") == "1":
        logger.info("Safe recovery mode skipped all MCP clients")
        return registry
    entry = capability_policy.capability_matrix_entry(
        "mcp_plugin_network",
        distribution=_effective_distribution(request_context),
    )
    if not capability_policy.capability_matrix_allows(
        entry.id,
        distribution=_effective_distribution(request_context),
        use="automatic",
    ):
        logger.info(
            "Skipped MCP client registry for distribution/runtime: %s",
            entry.reason_code,
        )
        return registry
    for client in mcp_clients or ():
        spec = mcp_client_spec_from_client(client)
        spec.metadata.update(
            {
                "capability_matrix_id": entry.id,
                "capability_matrix_mode": entry.mode,
                "capability_matrix_reason_code": entry.reason_code,
            },
        )
        registry.register_mcp_client(spec)
    return registry


def build_tool_policy_context(
    *,
    agent_config: Any | None = None,
    request_context: dict[str, Any] | None = None,
    workspace_dir: Path | str | None = None,
) -> ToolPolicyContext:
    """Build the runtime policy context from agent and request state."""

    request_context = dict(request_context or {})
    resolved_workspace = (
        str(workspace_dir)
        if workspace_dir is not None
        else str(request_context.get("workspace_dir") or "")
    )
    approval_level = "AUTO"
    if agent_config is not None:
        approval_level = str(getattr(agent_config, "approval_level", "AUTO") or "AUTO")
    return ToolPolicyContext(
        agent_id=str(request_context.get("agent_id") or "") or None,
        session_id=str(request_context.get("session_id") or "") or None,
        root_session_id=str(request_context.get("root_session_id") or "") or None,
        chat_id=str(request_context.get("chat_id") or "") or None,
        turn_id=str(
            request_context.get("turn_id")
            or request_context.get("run_id")
            or request_context.get("session_id")
            or "",
        )
        or None,
        workspace_dir=resolved_workspace or None,
        workspace_grant_id=(
            str(request_context.get("workspace_grant_id") or "").strip()
            or None
        ),
        workspace_access_mode=str(
            request_context.get("workspace_access_mode") or "read_write",
        ),
        workspace_trust_state=str(
            request_context.get("workspace_trust_state") or "restricted",
        ),
        permission_profile=str(
            request_context.get("permission_profile") or "workspace",
        ),
        permission_mode=(
            str(request_context.get("permission_mode") or "").strip() or None
        ),
        distribution=(
            str(request_context.get("distribution"))
            if request_context.get("distribution") is not None
            else None
        ),
        approval_level=approval_level,
        request_context=request_context,
    )
