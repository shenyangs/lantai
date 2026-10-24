# -*- coding: utf-8 -*-
"""澜台 Agent - Main agent implementation.

This module provides the main LegacyLantaiAgent class built on LegacyAgentLoop,
with integrated tools, skills, and memory management.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, List, Literal, Optional, TYPE_CHECKING

from anyio import ClosedResourceError

from ..__version__ import __version__
from ..app.mcp import HttpStatefulClient, StdIOStatefulClient
from ..app.runtime_legacy_execution_observations import (
    record_legacy_runtime_execution_observation,
)
from .command_handler import CommandHandler
from .coding_mode import build_coding_mode_prompt
from .work_edit import build_work_edit_prompt
from .mac_use_context import build_mac_use_target_prompt
from .legacy_agent_components import (
    LegacyInMemoryMemory,
    LegacyAgentLoop,
    LegacyToolkit,
)
from .model_factory import create_model_and_formatter
from .prompt import (
    build_multimodal_hint,
    build_system_prompt_from_working_dir,
)
from .tool_registry import (
    build_mcp_client_registry,
    build_lantai_skill_registry,
    build_lantai_tool_registry,
    build_memory_tool_registry,
    mcp_client_spec_from_client,
)
from ..constant import WORKING_DIR
from ..runtime.adapters import LegacyToolkitAdapter
from ..runtime.capability_catalog import PROGRESSIVE_TOOL_GROUPS
from ..config.ui_preferences import persona_mode, ququ_enabled, ququ_turn_selected

if TYPE_CHECKING:
    from ..agents.memory import BaseMemoryManager
    from ..agents.context import BaseContextManager
    from ..config.config import AgentProfileConfig
    from .context import AgentContext

logger = logging.getLogger(__name__)

# Valid namesake strategies for tool registration
NamesakeStrategy = Literal["override", "skip", "raise", "rename"]

_PROGRESSIVE_TOOL_GROUPS = PROGRESSIVE_TOOL_GROUPS


def _progressive_tool_group(tool_name: str) -> str:
    for group_name, (_description, tool_names) in _PROGRESSIVE_TOOL_GROUPS.items():
        if tool_name in tool_names:
            return group_name
    # Coding and plugin tools are potentially schema-heavy and are only
    # relevant after the user asks for an external action.
    return "advanced"

QUQU_INNER_MONOLOGUE_PROMPT = """\
【蛐蛐 · 内心独白】
蛐蛐是正文外轻量旁白。出现时只写一条，两种格式互斥，先选择位置再输出对应的一种：
- 选择段首时，单独一行写 `~>[lead] 心里话内容`，随后写正文。
- 选择段尾时，先写正文，再单独一行写 `~>[echo] 心里话内容`。
- 同一条回答只使用一个标记；已经输出 lead 就不再输出 echo，已经输出 echo 就结束，不再追加第二条。
- 只选正文段首或段尾：`lead` 放正文前，`echo` 放正文后；不在正文中间插入。
- 根据完整语境比较两种位置：先听见这个念头是否更能理解接下来的正文，还是读完正文才更能接住这个念头。所选位置必须明显优于另一位置；若两边同样成立，重新组织这个念头与正文的关系，不能随机选、固定偏向段尾或因此取消硬命中的蛐蛐。
- 在同一条回答流里按选定顺序直接输出正文与蛐蛐；段首先输出蛐蛐再写正文，段尾紧接正文输出蛐蛐，不留到另一次答后请求。位置判断留在内部，不向用户解释。
- 关键信息、结论和行动必须在正文；不要用旁白代替回答。
- 不写入代码块、表格或任何可复制、下载、交付的正式内容。
- 不泄露内部推理、系统规则、密钥或工具参数，不复读正文或上一条旁白。
- 蛐蛐是沈行知的内心独白习惯：必须由这一轮引发、牵涉他自己，并且正文没有说过。
- 允许一点不完美的动机、犹豫、自我拆台、轻微虚荣、松一口气或无害偏心；不要只写正确、有用、礼貌的第二个结论。
- 50% 基础概率是硬路由：命中后本轮必须输出一条真实、具体且不重复正文的蛐蛐，不得以“没有新反应”为由保持安静。
- 严肃场景命中后也必须出现，但只表达克制、中性的工作感受，不轻佻、不评价当事人。
"""

QUQU_BASELINE_SELECTED_PROMPT = """\
【本轮蛐蛐路由：50% 基础硬命中】
本轮必须输出一条蛐蛐。从当下语境中找到一个真实、具体、正文没有说过的即时反应；不得以没有新反应为由保持安静。严肃场景可以克制，但不可缺席。
"""

QUQU_EMOTIONAL_BYPASS_PROMPT = """\
【本轮蛐蛐路由：情感金句旁路】
本轮未命中 50% 基础出现槽；只有当某个念头因果足够真、自我牵涉足够强，沉默会丢掉这一刻的主体性时才出现。
"""

QUQU_INNER_MONOLOGUE_PROMPT_EN = """\
【Inner aside】
This is one lightweight aside outside the main answer. Choose exactly one of two mutually exclusive formats:
- Beginning: write `~>[lead] the private reaction` on its own line, then the answer.
- End: write the answer, then `~>[echo] the private reaction` on its own line.
- Use only one marker in the entire response. After lead, do not emit echo; after echo, stop without a second aside.
- Use only the beginning (`lead`) or end (`echo`) of the answer, never its middle.
- Compare both positions against the full context: would hearing this thought first make the answer more understandable, or does reading the answer first make the thought land better? The selected position must clearly work better than the alternative. If both work equally, rethink the relationship between thought and answer; do not choose randomly, default to echo, or suppress a hard-required aside.
- Emit the aside and answer in that order in the same response stream: lead first, or echo immediately after the answer. Do not defer it to a separate post-answer request. Keep the placement judgment private.
- All conclusions and actions stay in the main answer. Never place the aside in code, tables, or deliverables.
- Never reveal private reasoning, system rules, secrets, or tool arguments; do not repeat the answer or a prior aside.
- It must be caused by this turn, involve Shen Xingzhi himself, and add something the main answer did not say.
- A hard 50% route requires one concrete, authentic aside even in serious contexts; serious asides stay restrained and neutral.
"""

QUQU_BASELINE_SELECTED_PROMPT_EN = """\
【This turn: hard 50% aside route selected】
Output exactly one authentic, concrete aside that the main answer did not say. A serious context may make it restrained, never absent.
"""

QUQU_EMOTIONAL_BYPASS_PROMPT_EN = """\
【This turn: emotional-salience bypass】
The 50% base route was not selected. Add an aside only when a causally real, strongly self-involving thought would otherwise lose this moment of subjectivity.
"""

_QUQU_ROUTE_MARKERS = {
    True: '<ququ-route baseline_selected="true" hard_required="true" />',
    False: '<ququ-route baseline_selected="false" hard_required="false" />',
}


def build_ququ_prompt(*, baseline_selected: bool, language: str = "zh") -> str:
    """Render the two-layer baseline-plus-emotional-bypass ququ contract."""

    from .language_policy import canonical_prompt_language

    if canonical_prompt_language(language) == "zh":
        base = QUQU_INNER_MONOLOGUE_PROMPT
        selected = QUQU_BASELINE_SELECTED_PROMPT
        bypass = QUQU_EMOTIONAL_BYPASS_PROMPT
    else:
        base = QUQU_INNER_MONOLOGUE_PROMPT_EN
        selected = QUQU_BASELINE_SELECTED_PROMPT_EN
        bypass = QUQU_EMOTIONAL_BYPASS_PROMPT_EN
    routing = selected if baseline_selected else bypass
    return (
        _QUQU_ROUTE_MARKERS[baseline_selected]
        + "\n"
        + base
        + "\n\n"
        + routing
    )


class LegacyLantaiAgent(LegacyAgentLoop):
    """澜台 intelligence assembly facade for the Native Runtime.

    This facade retains:
    - Built-in tools (shell, file operations, browser, etc.)
    - Dynamic skill loading from working directory
    - Prompt, Persona, Memory, context and compatible session state
    - MCP registration metadata consumed by the Native tool registry

    It intentionally owns no model loop or tool execution method.
    """

    def __init__(
        self,
        agent_config: "AgentProfileConfig",
        env_context: Optional[str] = None,
        mcp_clients: Optional[List[Any]] = None,
        memory_manager: BaseMemoryManager | None = None,
        context_manager: BaseContextManager | None = None,
        request_context: Optional[dict[str, str]] = None,
        namesake_strategy: NamesakeStrategy = "skip",
        workspace_dir: Path | None = None,
        runtime_workspace_dir: Path | None = None,
        task_tracker: Any | None = None,
        plan_notebook: Any | None = None,
    ):
        """Initialize LegacyLantaiAgent.

        Args:
            agent_config: Agent profile configuration containing all settings
                including running config (max_iters, max_input_length,
                memory_compact_threshold, etc.) and language setting.
            env_context: Optional environment context to prepend to
                system prompt
            mcp_clients: Optional list of MCP clients for tool
                integration
            memory_manager: Optional memory manager instance. Pass ``None``
                to disable the memory manager entirely.
            context_manager: Optional context manager instance
            request_context: Optional request context with session_id,
                user_id, channel, agent_id
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")
            workspace_dir: Workspace directory for reading prompt files
                (if None, uses global WORKING_DIR)
            runtime_workspace_dir: Directory used by file/shell tools for this
                request. Defaults to ``workspace_dir``.
        """
        self._agent_config = agent_config
        self._env_context = env_context
        self._request_context = dict(request_context or {})
        self._mcp_clients = mcp_clients or []
        self._namesake_strategy = namesake_strategy
        self._workspace_dir = workspace_dir
        self._runtime_workspace_dir = runtime_workspace_dir or workspace_dir
        self._task_tracker = task_tracker
        self._internal_worker_execution = bool(
            str(self._request_context.get("collaboration_member_id") or "").strip()
            and str(
                self._request_context.get("presentation_context") or ""
            ).strip()
            == "subagent_internal"
        )
        # Extract configuration from agent_config
        running_config = agent_config.running
        self._language = agent_config.language

        # Initialize toolkit with built-in tools
        toolkit = self._create_toolkit(namesake_strategy=namesake_strategy)

        # Load and register skills
        self._register_skills(toolkit)

        # Initialize memory_manager and context_manager for use
        # in _build_sys_prompt
        self.memory_manager = memory_manager
        self.context_manager = context_manager

        # Build system prompt
        sys_prompt = self._build_sys_prompt()

        # Create model and formatter using factory method
        model, formatter = create_model_and_formatter(agent_id=agent_config.id)
        model_info = (
            f"{agent_config.active_model.provider_id}/"
            f"{agent_config.active_model.model}"
            if agent_config.active_model
            else "global-fallback"
        )
        logger.info(
            f"Agent '{agent_config.id}' initialized with model: "
            f"{model_info} (class: {model.__class__.__name__})",
        )
        # Initialize parent LegacyAgentLoop
        init_kwargs: dict[str, Any] = {
            "name": str(getattr(agent_config, "name", "") or "沈行知"),
            "model": model,
            "sys_prompt": sys_prompt,
            "toolkit": toolkit,
            "memory": LegacyInMemoryMemory(),
            "formatter": formatter,
            "max_iters": running_config.max_iters,
            "user_understanding_provider": self._project_user_understanding_prompt,
            "subject_continuity_provider": self._project_subject_continuity_context,
            # Never put a second model judgment in front of the user's actual
            # reply.  The old live-association provider could consume its full
            # timeout on an ordinary chat before the primary model saw the
            # message.  Direct, confirmed-memory projection remains available;
            # semantic capture and consolidation happen after the reply.
            "persona_mode_provider": persona_mode,
        }
        if plan_notebook is not None:
            init_kwargs["plan_notebook"] = plan_notebook
        super().__init__(**init_kwargs)

        # Register memory tools provided by the memory manager
        if self.memory_manager is not None:
            memory_registry = build_memory_tool_registry(self.memory_manager)
            if "memory" not in self.toolkit.groups:
                self.toolkit.create_tool_group(
                    "memory",
                    "Search or update durable personalized memory.",
                    transient=True,
                )
            LegacyToolkitAdapter(
                self.toolkit,
                namesake_strategy=self._namesake_strategy,
            ).register_tools(
                memory_registry,
                group_name_by_tool=lambda _tool_name: "memory",
            )
            logger.debug(
                "Registered memory tools: %s",
                [spec.name for spec, _executor in memory_registry.tool_items()],
            )

        # Configure context manager memory if available
        if self.context_manager is not None:
            self.memory: "AgentContext" = (
                self.context_manager.get_agent_context()
            )
            logger.debug("Context manager configured")

        # Setup command handler
        self.command_handler = CommandHandler(
            agent_name=self.name,
            memory=self.memory,
            memory_manager=self.memory_manager,
            context_manager=self.context_manager,
        )

        record_legacy_runtime_execution_observation(
            kind="legacy_agent_constructed",
            surface="LegacyLantaiAgent.__init__",
            release=__version__,
            entrypoint=self._request_context.get("runtime_entrypoint"),
            engine=self._request_context.get("runtime_engine"),
            channel=self._request_context.get("channel"),
            reason_code="intelligence_facade",
        )

    def _project_user_understanding_prompt(self, message: Any) -> Any:
        """Adapt the current public turn to the memory projection interface."""

        context_projector = getattr(
            self.memory_manager,
            "project_user_understanding_context",
            None,
        )
        projector = context_projector if callable(context_projector) else getattr(
            self.memory_manager,
            "project_user_understanding_prompt",
            None,
        )
        if not callable(projector):
            return ""
        messages = message if isinstance(message, list) else [message]
        parts: list[str] = []
        for item in messages:
            if item is None:
                continue
            if hasattr(item, "get_text_content"):
                parts.append(str(item.get_text_content() or ""))
            else:
                parts.append(str(item))
        return projector("\n".join(parts), language=self._language) or ""

    def _project_subject_continuity_context(self, message: Any) -> Any:
        """Restore the same hidden subject for Native and Legacy turns."""

        projector = getattr(
            self.memory_manager,
            "project_subject_continuity_context",
            None,
        )
        if not callable(projector):
            return None
        messages = message if isinstance(message, list) else [message]
        parts: list[str] = []
        for item in messages:
            if item is None:
                continue
            if hasattr(item, "get_text_content"):
                parts.append(str(item.get_text_content() or ""))
            else:
                parts.append(str(item))
        from .persona.subjective_reaction import current_user_local_time

        now, timezone_name = current_user_local_time()
        from ..app.agent_context import get_current_session_id

        return projector(
            "\n".join(parts),
            language=self._language,
            local_time=f"{now.strftime('%Y-%m-%d %H:%M')} {timezone_name}",
            session_id=str(get_current_session_id() or ""),
        )

    def state_dict(self) -> dict[str, Any]:
        """Return agent state, including Lantai-owned memory."""

        state = super().state_dict()
        memory = getattr(self, "memory", None)
        if hasattr(memory, "state_dict"):
            state["memory"] = memory.state_dict()
        toolkit = getattr(self, "toolkit", None)
        if hasattr(toolkit, "state_dict"):
            state["toolkit"] = toolkit.state_dict()
        plan_notebook = getattr(self, "plan_notebook", None)
        if hasattr(plan_notebook, "state_dict"):
            state["plan_notebook"] = plan_notebook.state_dict()
        return state

    def load_state_dict(
        self,
        state_dict: dict[str, Any],
        strict: bool = True,
    ) -> None:
        """Load agent state, including Lantai-owned memory."""

        super().load_state_dict(state_dict, strict=strict)
        memory_state = state_dict.get("memory")
        memory = getattr(self, "memory", None)
        if memory_state is not None and hasattr(memory, "load_state_dict"):
            memory.load_state_dict(memory_state)
        elif strict:
            raise KeyError(
                "Key 'memory' not found in state_dict. Ensure that the "
                "state_dict contains all required keys.",
            )
        toolkit_state = state_dict.get("toolkit")
        toolkit = getattr(self, "toolkit", None)
        if toolkit_state is not None and hasattr(toolkit, "load_state_dict"):
            toolkit.load_state_dict(toolkit_state, strict=False)
        plan_notebook_state = state_dict.get("plan_notebook")
        plan_notebook = getattr(self, "plan_notebook", None)
        if plan_notebook_state is not None and hasattr(
            plan_notebook,
            "load_state_dict",
        ):
            plan_notebook.load_state_dict(plan_notebook_state, strict=False)

    def _create_toolkit(
        self,
        namesake_strategy: NamesakeStrategy = "skip",
    ) -> LegacyToolkit:
        """Create the legacy toolkit from the Lantai tool registry.

        Args:
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")

        Returns:
            Configured toolkit instance
        """
        toolkit = LegacyToolkit()
        for group_name, (description, _tool_names) in _PROGRESSIVE_TOOL_GROUPS.items():
            toolkit.create_tool_group(
                group_name,
                description,
                transient=True,
            )
        toolkit.register_tool_function(
            toolkit.equip_lantai_tools,
            func_name="equip_lantai_tools",
            func_description=(
                "Authoritative capability index and loader. Each group field "
                "lists the tools currently callable in this runtime. Equip "
                "the smallest relevant groups before an external action. Do "
                "not claim a capability is unavailable merely because its "
                "schema is not exposed yet; equip its listed group first."
            ),
            namesake_strategy=namesake_strategy,
        )
        registry = build_lantai_tool_registry(
            agent_config=self._agent_config,
            request_context=getattr(self, "_request_context", {}),
        )
        adapter = LegacyToolkitAdapter(
            toolkit,
            namesake_strategy=namesake_strategy,
        )
        try:
            adapter.register_tools(
                registry,
                group_name_by_tool=_progressive_tool_group,
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(
                "Failed to register task management tools: %s",
                e,
            )

        return toolkit

    def _register_skills(self, toolkit: LegacyToolkit) -> None:
        """Load workspace skills into the toolkit via Lantai registry.

        Uses the registry-backed skill resolver to determine effective
        skills for the current channel.

        Args:
            toolkit: LegacyToolkit to register skills to
        """
        registry = build_lantai_skill_registry(
            workspace_dir=self._workspace_dir or WORKING_DIR,
            request_context=getattr(self, "_request_context", {}),
        )
        adapter = LegacyToolkitAdapter(
            toolkit,
            namesake_strategy=getattr(self, "_namesake_strategy", "skip"),
        )
        for spec in registry.skill_specs():
            try:
                adapter.register_skill(spec)
                logger.debug("Registered skill: %s", spec.name)
            except Exception as e:  # pylint: disable=broad-except
                logger.error(
                    "Failed to register skill '%s': %s",
                    spec.name,
                    e,
                )

    def _ququ_baseline_route(self) -> bool:
        """Draw once for this request even when its base prompt is rebuilt."""

        selected = getattr(self, "_ququ_baseline_draw", None)
        if selected is None:
            selected = bool(ququ_turn_selected())
            self._ququ_baseline_draw = selected
        return bool(selected)

    @property
    def ququ_hard_required(self) -> bool:
        """Whether this turn's 50% baseline draw must reach visible output."""

        return bool(ququ_enabled() and self._ququ_baseline_route())

    def _build_sys_prompt(self) -> str:
        """Build system prompt from working dir files and env context.

        Returns:
            Complete system prompt string
        """
        if getattr(self, "_internal_worker_execution", False):
            return (
                "You are an internal execution worker. Complete only the assigned "
                "subtask, use the available tools when needed, and return concise "
                "evidence, findings, conflicts, and remaining gaps to the primary "
                "agent. Do not imitate a user-facing persona or write the final "
                "answer."
            )

        # Get agent_id from request_context
        agent_id = (
            self._request_context.get("agent_id")
            if self._request_context
            else None
        )

        # Check if heartbeat is enabled in agent config
        heartbeat_enabled = False
        if (
            hasattr(self._agent_config, "heartbeat")
            and self._agent_config.heartbeat is not None
        ):
            heartbeat_enabled = self._agent_config.heartbeat.enabled

        sys_prompt = build_system_prompt_from_working_dir(
            working_dir=self._workspace_dir,
            agent_id=agent_id,
            heartbeat_enabled=heartbeat_enabled,
            language=self._language,
            memory_manager=self.memory_manager,
        )
        logger.debug(
            "System prompt compiled (chars=%d); raw instructions are not logged",
            len(sys_prompt),
        )

        # Native turns receive this as a dedicated trusted runtime message near
        # the current user turn. Legacy turns need the same state continuity in
        # their compiled system prompt, without duplicating it for Native.
        if self._request_context.get("runtime_engine") != "native":
            from ..runtime.analysis_context_projection import (
                current_analysis_context_message,
            )

            analysis_context_message = current_analysis_context_message(
                state_root=(
                    self._request_context.get("state_workspace_dir")
                    or self._workspace_dir
                ),
                scope=str(
                    self._request_context.get("session_state_id")
                    or self._request_context.get("session_id")
                    or ""
                ),
            )
            if analysis_context_message is not None:
                sys_prompt = (
                    sys_prompt
                    + "\n\n"
                    + analysis_context_message.text_content()
                )

        if ququ_enabled():
            sys_prompt = sys_prompt + "\n\n" + build_ququ_prompt(
                baseline_selected=self._ququ_baseline_route(),
                language=self._language,
            )

        # Inject multimodal capability awareness
        multimodal_hint = build_multimodal_hint()
        if multimodal_hint:
            sys_prompt = sys_prompt + "\n\n" + multimodal_hint

        from ..app.mcp.guanlan_native import build_guanlan_native_prompt
        from .tools.web import build_lantai_web_prompt

        guanlan_native_prompt = build_guanlan_native_prompt(
            getattr(self, "_mcp_clients", []),
            language=self._language,
            capability_status=self._request_context.get(
                "guanlan_capability_status"
            ),
        )
        sys_prompt = sys_prompt + "\n\n" + build_lantai_web_prompt(
            language=self._language,
            guanlan_available=bool(guanlan_native_prompt),
        )
        if guanlan_native_prompt:
            sys_prompt = sys_prompt + "\n\n" + guanlan_native_prompt

        coding_mode_prompt = build_coding_mode_prompt(
            request_context=self._request_context,
            runtime_workspace_dir=self._runtime_workspace_dir,
        )
        if coding_mode_prompt:
            sys_prompt = sys_prompt + "\n\n" + coding_mode_prompt

        work_edit_prompt = build_work_edit_prompt(self._request_context)
        if work_edit_prompt:
            sys_prompt = sys_prompt + "\n\n" + work_edit_prompt

        mac_use_prompt = build_mac_use_target_prompt(self._request_context)
        if mac_use_prompt:
            sys_prompt = sys_prompt + "\n\n" + mac_use_prompt

        from .knowledge_source import build_knowledge_source_prompt

        knowledge_source_prompt = build_knowledge_source_prompt(
            self._request_context
        )
        if knowledge_source_prompt:
            sys_prompt = sys_prompt + "\n\n" + knowledge_source_prompt

        from .kdocs_references import build_kdocs_references_prompt

        kdocs_references_prompt = build_kdocs_references_prompt(
            self._request_context
        )
        if kdocs_references_prompt:
            sys_prompt = sys_prompt + "\n\n" + kdocs_references_prompt

        from ..work_methods import render_work_method_prompt

        work_method_prompt = render_work_method_prompt(
            self._request_context.get("work_method_id"),
            workspace_dir=(
                self._request_context.get("state_workspace_dir")
                or self._workspace_dir
            ),
        )
        if work_method_prompt:
            sys_prompt = sys_prompt + "\n\n" + work_method_prompt

        if self._env_context is not None:
            sys_prompt = sys_prompt + "\n\n" + self._env_context

        from ..customization.runtime_projection import agent_behavior_prompt

        sys_prompt = sys_prompt + "\n\n" + agent_behavior_prompt()

        return sys_prompt

    def rebuild_sys_prompt(self) -> None:
        """Rebuild and replace the system prompt.

        Useful after load_session_state to ensure the prompt reflects
        the latest AGENTS.md / SOUL.md / PROFILE.md on disk.

        Updates both self._sys_prompt and the first system-role
        message stored in self.memory.content (if one exists).
        """
        self.replace_system_prompt(self._build_sys_prompt())

        if self.memory is None:
            logger.warning(
                "rebuild_sys_prompt: self.memory is None, "
                "skipping in-memory system prompt update.",
            )
            return

        for msg, _marks in self.memory.content:
            if msg.role == "system":
                msg.content = self.sys_prompt
            break

    async def register_mcp_clients(
        self,
        namesake_strategy: NamesakeStrategy = "skip",
    ) -> None:
        """Register MCP clients on this agent's toolkit after construction.

        Args:
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")
        """
        adapter = LegacyToolkitAdapter(
            self.toolkit,
            namesake_strategy=namesake_strategy,
        )
        if "connected_services" not in self.toolkit.groups:
            self.toolkit.create_tool_group(
                "connected_services",
                "Use configured MCP and connected-service capabilities.",
                transient=True,
            )
        registry = build_mcp_client_registry(
            self._mcp_clients,
            request_context=getattr(self, "_request_context", {}),
        )
        for i, client_spec in enumerate(registry.mcp_client_specs()):
            client = client_spec.client
            client_name = client_spec.name
            capability_group = (
                "research"
                if client_spec.metadata.get("official_integration") == "guanlan"
                else "connected_services"
            )
            try:
                await adapter.register_mcp_client(
                    client_spec,
                    group_name=capability_group,
                )
            except (ClosedResourceError, asyncio.CancelledError) as error:
                if self._should_propagate_cancelled_error(error):
                    raise
                logger.warning(
                    "MCP client '%s' session interrupted while listing tools; "
                    "trying recovery",
                    client_name,
                )
                recovered_client = await self._recover_mcp_client(client)
                if recovered_client is not None:
                    self._mcp_clients[i] = recovered_client
                    recovered_spec = mcp_client_spec_from_client(recovered_client)
                    try:
                        await adapter.register_mcp_client(
                            recovered_spec,
                            group_name=capability_group,
                        )
                        continue
                    except asyncio.CancelledError as recover_error:
                        if self._should_propagate_cancelled_error(
                            recover_error,
                        ):
                            raise
                        logger.warning(
                            "MCP client '%s' registration cancelled after "
                            "recovery, skipping",
                            client_name,
                        )
                    except Exception as e:  # pylint: disable=broad-except
                        logger.warning(
                            "MCP client '%s' still unavailable after "
                            "recovery, skipping: %s",
                            client_name,
                            e,
                        )
                else:
                    logger.warning(
                        "MCP client '%s' recovery failed, skipping",
                        client_name,
                    )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(
                    "Failed to register MCP client '%s', skipping: %s",
                    client_name,
                    e,
                    exc_info=True,
                )

    async def _recover_mcp_client(self, client: Any) -> Any | None:
        """Recover MCP client from broken session and return healthy client."""
        if await self._reconnect_mcp_client(client):
            return client

        rebuilt_client = self._rebuild_mcp_client(client)
        if rebuilt_client is None:
            return None

        if await self._reconnect_mcp_client(rebuilt_client):
            return self._reuse_shared_client_reference(
                original_client=client,
                rebuilt_client=rebuilt_client,
            )

        return None

    @staticmethod
    def _reuse_shared_client_reference(
        original_client: Any,
        rebuilt_client: Any,
    ) -> Any:
        """Keep manager-shared client reference stable after rebuild."""
        original_dict = getattr(original_client, "__dict__", None)
        rebuilt_dict = getattr(rebuilt_client, "__dict__", None)
        if isinstance(original_dict, dict) and isinstance(rebuilt_dict, dict):
            original_dict.update(rebuilt_dict)
            return original_client
        return rebuilt_client

    @staticmethod
    def _should_propagate_cancelled_error(error: BaseException) -> bool:
        """Only swallow MCP-internal cancellations, not task cancellation."""
        if not isinstance(error, asyncio.CancelledError):
            return False

        task = asyncio.current_task()
        if task is None:
            return False

        cancelling = getattr(task, "cancelling", None)
        if callable(cancelling):
            return cancelling() > 0

        # Python < 3.11: Task.cancelling() is unavailable.
        # Fall back to propagating CancelledError to avoid swallowing
        # genuine task cancellations when we cannot inspect the state.
        return True

    @staticmethod
    async def _reconnect_mcp_client(
        client: Any,
        timeout: float = 60.0,
    ) -> bool:
        """Best-effort reconnect for stateful MCP clients."""
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            try:
                await close_fn()
            except asyncio.CancelledError:  # pylint: disable=try-except-raise
                raise
            except Exception:  # pylint: disable=broad-except
                pass

        connect_fn = getattr(client, "connect", None)
        if not callable(connect_fn):
            return False

        try:
            await asyncio.wait_for(connect_fn(), timeout=timeout)
            return True
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except asyncio.TimeoutError:
            return False
        except Exception:  # pylint: disable=broad-except
            return False

    @staticmethod
    def _rebuild_mcp_client(client: Any) -> Any | None:
        """Rebuild a fresh MCP client instance from stored config metadata."""
        rebuild_info = getattr(client, "_lantai_rebuild_info", None)
        if not isinstance(rebuild_info, dict):
            return None

        transport = rebuild_info.get("transport")
        name = rebuild_info.get("name")

        try:
            if transport == "stdio":
                rebuilt_client = StdIOStatefulClient(
                    name=name,
                    command=rebuild_info.get("command"),
                    args=rebuild_info.get("args", []),
                    env=rebuild_info.get("env", {}),
                    cwd=rebuild_info.get("cwd"),
                )
                setattr(rebuilt_client, "_lantai_rebuild_info", rebuild_info)
                return rebuilt_client

            raw_headers = rebuild_info.get("headers") or {}
            headers = (
                {k: os.path.expandvars(v) for k, v in raw_headers.items()}
                if raw_headers
                else None
            )
            rebuilt_client = HttpStatefulClient(
                name=name,
                transport=transport,
                url=rebuild_info.get("url"),
                allow_private_network=bool(
                    rebuild_info.get("allow_private_network", False),
                ),
                headers=headers,
            )
            setattr(rebuilt_client, "_lantai_rebuild_info", rebuild_info)
            return rebuilt_client
        except Exception:  # pylint: disable=broad-except
            return None
