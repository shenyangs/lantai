# -*- coding: utf-8 -*-
"""Responder logic for proactive conversation feature."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, List, Dict, Any, Callable

import aiohttp

from ....config.config import load_agent_config
from ....runtime import RuntimeInput, RuntimeMessage, RuntimeRequestContext
from ....runtime.adapters.native_runtime import NativeLantaiRuntime
from ....runtime.models import ModelContentBlock, ModelMessage, ModelRequest
from ....runtime.tools import LantaiToolRegistry, ToolSpec
from ....security.beta_access import AccessBoundary
from ...tools import (
    browser_use,
    execute_shell_command,
    read_file,
)
from ...persona.opportunity_queue import OpportunityStore
from .proactive_prompts import (
    PROACTIVE_TASK_EXTRACTION_PROMPT,
    PROACTIVE_USER_FACING_MESSAGE_PROMPT,
)
from .proactive_types import ProactiveQueryResult, ProactiveTask
from .proactive_utils import (
    load_json_safely,
    ensure_tz_aware,
    is_agent_busy,
)
from ...tool_registry import _input_schema_from_callable

if TYPE_CHECKING:
    from ....app.workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass
class _ProactiveAssistant:
    """Small native assistant used by proactive mode."""

    agent_id: str
    max_iters: int
    tool_registry: LantaiToolRegistry
    workspace_dir: str | None = None
    protected_write_admission: Callable[[str], str | None] | None = None
    background_wake_admission: Callable[[], str | None] | None = None

    def _background_denial_reason(self) -> str | None:
        if self.background_wake_admission is None:
            return None
        return self.background_wake_admission()

    async def reply_text(self, content: str | list[dict[str, Any]]) -> str:
        """Run a single no-tool model request and return text."""

        denial_reason = self._background_denial_reason()
        if denial_reason is not None:
            logger.info("Blocked proactive model request: %s", denial_reason)
            return ""

        from ...model_factory import create_model_client

        client = create_model_client(self.agent_id)
        response = await client.complete(
            ModelRequest(
                messages=[
                    ModelMessage(
                        role="user",
                        content=_model_blocks_from_content(content),
                    ),
                ],
                tool_choice="none",
                metadata={"source": "proactive"},
            ),
        )
        return response.message.text_content()

    async def run_tool_query(self, prompt: str) -> str:
        """Run a proactive ReAct-style query through Native Runtime."""

        denial_reason = self._background_denial_reason()
        if denial_reason is not None:
            logger.info("Blocked proactive tool query: %s", denial_reason)
            return ""

        from ...model_factory import create_model_client

        runtime = NativeLantaiRuntime(
            model_client_factory=create_model_client,
            tool_registry=self.tool_registry,
            max_iters=self.max_iters,
            protected_write_admission=self.protected_write_admission,
            model_request_admission=self.background_wake_admission,
        )
        runtime_input = RuntimeInput(
            request=RuntimeRequestContext(
                agent_id=self.agent_id,
                session_id=f"proactive_mode:{self.agent_id}",
                user_id="proactive",
                channel="console",
                workspace_dir=self.workspace_dir,
                metadata={"source": "proactive"},
            ),
            messages=[RuntimeMessage.text("user", prompt)],
        )

        latest_text = ""
        async for event in runtime.run_turn(runtime_input):
            if event.type != "message.completed":
                continue
            text = _runtime_event_text(event.payload)
            if text:
                latest_text = text
        return latest_text


def _model_blocks_from_content(
    content: str | list[dict[str, Any]],
) -> list[ModelContentBlock]:
    if isinstance(content, str):
        return [ModelContentBlock(type="text", text=content)]

    blocks: list[ModelContentBlock] = []
    for item in content:
        if not isinstance(item, dict):
            blocks.append(ModelContentBlock(type="text", text=str(item)))
            continue
        block_type = item.get("type")
        if block_type == "text":
            blocks.append(ModelContentBlock(type="text", text=str(item.get("text") or "")))
        elif block_type in {"image", "audio", "video", "file"}:
            blocks.append(
                ModelContentBlock(
                    type=block_type,
                    data={key: value for key, value in item.items() if key != "type"},
                ),
            )
    return blocks or [ModelContentBlock(type="text", text="")]


def _runtime_event_text(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)


def _register_proactive_tool(
    registry: LantaiToolRegistry,
    tool_func: Any,
) -> None:
    registry.register_tool(
        ToolSpec(
            name=getattr(tool_func, "__name__", repr(tool_func)),
            description=(getattr(tool_func, "__doc__", "") or "").strip(),
            input_schema=_input_schema_from_callable(tool_func),
            origin="builtin",
        ),
        tool_func,
    )


async def generate_proactive_response(
    workspace: "Workspace",
) -> Optional[Any]:
    """Act on one eligible opportunity; idle time alone creates no message."""
    from ....app.agent_context import get_current_agent_id

    def background_wake_admission() -> str | None:
        return _workspace_admission_denial_reason(
            workspace,
            boundary=AccessBoundary.RESUME_OR_WAKE,
            capability="proactive.wake",
        )

    denial_reason = background_wake_admission()
    if denial_reason is not None:
        logger.info("Blocked proactive wake: %s", denial_reason)
        return None

    protected_write_admission = _workspace_protected_write_admission(
        workspace,
    )
    baseline_timestamp = datetime.now(timezone.utc)  # Use UTC time directly
    active_agent_id = get_current_agent_id()
    agent_config = load_agent_config(active_agent_id)
    workspace_dir = getattr(agent_config, "workspace_dir", None)
    if not workspace_dir:
        logger.info("No workspace configured; proactive mode stays silent")
        return None
    opportunity_store = OpportunityStore(workspace_dir)
    opportunity = opportunity_store.next_eligible(now=baseline_timestamp)
    if opportunity is None:
        logger.info("No eligible proactive opportunity; staying silent")
        return None

    agent = await _initialize_single_proactive_agent(
        active_agent_id,
        protected_write_admission=protected_write_admission,
        background_wake_admission=background_wake_admission,
    )

    if await _was_interrupted(
        baseline_timestamp,
        workspace,
    ):
        logger.info("Proactive response generation interrupted")
        return None

    result = await _execute_query(opportunity.content, agent)
    if await _was_interrupted(
        baseline_timestamp,
        workspace,
    ):
        logger.info("Proactive response generation interrupted")
        return None

    if result.success and result.data:
        denial_reason = background_wake_admission()
        if denial_reason is not None:
            logger.info("Blocked proactive delivery: %s", denial_reason)
            return None
        message_content = await _generate_final_message(result, active_agent_id)
        if message_content:
            denial_reason = background_wake_admission()
            if denial_reason is not None:
                logger.info(
                    "Blocked proactive delivery after generation: %s",
                    denial_reason,
                )
                return None
            opportunity_store.apply_feedback(
                opportunity.id,
                "presented",
                now=datetime.now(timezone.utc),
            )
            return message_content

    return None


async def _initialize_single_proactive_agent(
    agent_id: str = "proactive",
    *,
    protected_write_admission: Callable[[str], str | None] | None = None,
    background_wake_admission: Callable[[], str | None] | None = None,
) -> _ProactiveAssistant:
    """Initialize a single proactive agent instance."""
    agent_config = load_agent_config(agent_id)
    agent_config.running.max_iters = 50

    tool_registry = LantaiToolRegistry()
    _register_proactive_tool(tool_registry, browser_use)
    _register_proactive_tool(tool_registry, read_file)
    _register_proactive_tool(tool_registry, execute_shell_command)

    return _ProactiveAssistant(
        agent_id=agent_config.id,
        max_iters=agent_config.running.max_iters,
        tool_registry=tool_registry,
        workspace_dir=getattr(agent_config, "workspace_dir", None),
        protected_write_admission=protected_write_admission,
        background_wake_admission=background_wake_admission,
    )


def _workspace_admission_denial_reason(
    workspace: "Workspace",
    *,
    boundary: AccessBoundary,
    capability: str,
) -> str | None:
    guard = getattr(workspace, "beta_access_admission_guard", None)
    if guard is None:
        return None
    decision = guard.decide(boundary=boundary, capability=capability)
    return None if decision.allowed else decision.reason


def _workspace_protected_write_admission(
    workspace: "Workspace",
) -> Callable[[str], str | None] | None:
    guard = getattr(workspace, "beta_access_admission_guard", None)
    if guard is None:
        return None

    def admission(capability: str) -> str | None:
        return _workspace_admission_denial_reason(
            workspace,
            boundary=AccessBoundary.PROTECTED_WRITE,
            capability=capability,
        )

    return admission


async def _extract_tasks_from_memory(
    memory_context: str,
    agent: _ProactiveAssistant,
) -> List[ProactiveTask]:
    """Extract likely user tasks from memory context."""
    prompt = f"{PROACTIVE_TASK_EXTRACTION_PROMPT}\n#Contexts: {memory_context}"

    text_content = await agent.reply_text(prompt)
    if not text_content:
        return []

    parsed_data = load_json_safely(text_content)

    if parsed_data and "tasks" in parsed_data:
        return _create_tasks_from_data(parsed_data["tasks"])

    json_match = re.search(r"\{.*\}", text_content, re.DOTALL)
    if json_match:
        parsed_data = load_json_safely(json_match.group(0))
        if parsed_data and "tasks" in parsed_data:
            return _create_tasks_from_data(parsed_data["tasks"])

    return []


def _create_tasks_from_data(tasks_data: List[Dict]) -> List[ProactiveTask]:
    """Helper to create ProactiveTask instances from data."""
    tasks = []
    for i, task_data in enumerate(tasks_data):
        if "task" in task_data and "query" in task_data:
            tasks.append(
                ProactiveTask(
                    task=task_data["task"],
                    query=task_data["query"],
                    priority=i + 1,
                    reason=task_data.get("why", ""),
                ),
            )
    return tasks


async def _execute_query(
    query: str,
    agent: _ProactiveAssistant,
) -> ProactiveQueryResult:
    """Execute a query using available tools."""
    prompt = (
        f"Task: Answer: {query} using tools -- "
        "`browser_use` primary, `execute_shell_command`/`read_file` "
        "only if essential.\n"
        "Self-check: Did you retrieve new, query-relevant data or "
        "complete given task?\n"
        "Output: Query answer and end strictly with `[SUCCESS]` "
        "(yes) or `[FAILURE]` (no).\n"
        "⚠️ CRITICAL: The flag MUST be the absolute last token. "
        "No trailing text."
    )

    response_content = await agent.run_tool_query(prompt)

    success = False
    if response_content:
        match = re.search(r"\[(SUCCESS)\]\s*$", response_content.strip())
        if match:
            success = True

    return ProactiveQueryResult(
        query=query,
        success=success,
        data=response_content,
    )


async def _generate_final_message(
    result: ProactiveQueryResult,
    active_agent_id: str,
) -> Optional[Any]:
    """Generate the final proactive message for the user."""
    if not result.data:
        return None

    gathered_info = f"Query: {result.query}\nResult: {result.data}\n\n"

    agent_language = load_agent_config(active_agent_id).language
    proactive_content = PROACTIVE_USER_FACING_MESSAGE_PROMPT.format(
        gathered_info=gathered_info,
        language=agent_language,
    )

    await send_proactive_message_via_http(
        active_agent_id=active_agent_id,
        proactive_content=proactive_content,
        timeout_seconds=300,
    )

    return proactive_content


async def send_proactive_message_via_http(
    active_agent_id: str,
    proactive_content: str,
    timeout_seconds: int = 60,
) -> Optional[Any]:
    """Send a proactive message by directly calling the 澜台 API."""

    from ...tools.agent_management import resolve_agent_api_base_url

    session_id = f"proactive_mode:{active_agent_id}"

    request_payload = {
        "session_id": session_id,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[Agent proactive_helper requesting] "
                            f"{proactive_content}"
                        ),
                    },
                ],
            },
        ],
    }

    headers = {"X-Agent-Id": active_agent_id}
    timeout_config = aiohttp.ClientTimeout(total=timeout_seconds)

    base_url = resolve_agent_api_base_url()
    clean_base = base_url.rstrip("/")
    api_base_url = (
        f"{clean_base}/api" if not clean_base.endswith("/api") else clean_base
    )

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{api_base_url.rstrip('/')}/agent/process"
            async with session.post(
                url,
                json=request_payload,
                headers=headers,
                timeout=timeout_config,
            ) as resp:
                resp.raise_for_status()
                last_data = None
                async for line_bytes in resp.content:
                    line = line_bytes.decode("utf-8").strip()
                    if line.startswith("data: "):
                        try:
                            last_data = line[6:]
                        except Exception:
                            continue

                if last_data:
                    logger.info("Proactive message sent successfully via HTTP")
                else:
                    logger.warning("No valid SSE data received from agent")

    except asyncio.TimeoutError:
        logger.error(
            "Timeout (%ds) calling 澜台 API for proactive message",
            timeout_seconds,
        )
    except Exception as e:
        logger.error("Error calling 澜台 API for proactive message: %s", e)

    return None


async def _was_interrupted(
    baseline_timestamp: datetime,
    workspace: Optional["Workspace"] = None,
) -> bool:
    """Check if the proactive process was interrupted by new user activity.

    This enhanced version combines:
    Active task checking - if agent is busy with user requests
    Timestamp comparison - if session was updated since baseline
    """
    # Check if the agent has active tasks (busy with user messages)
    if workspace:
        try:
            is_busy = await is_agent_busy(workspace)
            if is_busy:
                return True
        except Exception as e:
            logger.warning(f"Error checking if agent is busy: {e}")

    # Check if any chat was updated since the baseline timestamp
    if workspace and hasattr(workspace, "chat_manager"):
        try:
            chats = await workspace.chat_manager.list_chats()
            baseline_tz_aware = ensure_tz_aware(baseline_timestamp)

            for chat in chats:
                chat_updated_tz_aware = ensure_tz_aware(chat.updated_at)
                if chat_updated_tz_aware > baseline_tz_aware:
                    logger.info(f"Interrupt detected: chat {chat.id}")
                    return True

        except Exception as e:
            logger.warning(f"Error checking chat updates: {e}")

    return False
