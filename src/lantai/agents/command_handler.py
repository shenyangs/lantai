# -*- coding: utf-8 -*-
"""Agent command handler for system commands.

This module handles system commands like /compact, /new, /clear, etc.
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config.config import load_agent_config
from ..constant import DEBUG_HISTORY_FILE, MAX_LOAD_HISTORY_COUNT
from ..exceptions import SystemCommandException
from ..runtime.converters import legacy_message, legacy_message_from_dict
from ..runtime.tool_response import TextBlock

if TYPE_CHECKING:
    from .memory import BaseMemoryManager
    from .context import AgentContext, BaseContextManager
    from ..app.workspace import Workspace

logger = logging.getLogger(__name__)


def _fmt_tokens(n: int) -> str:
    """Format token count as e.g. '82.3k' or '450'."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class ConversationCommandHandlerMixin:
    """Mixin for conversation (system) commands: /compact, /new, /clear, etc.

    Expects self to have: agent_name, memory, formatter, memory_manager,
    context_manager.
    """

    # Supported conversation commands (unchanged set)
    SYSTEM_COMMANDS = frozenset(
        {
            "compact",
            "new",
            "clear",
            "history",
            "compact_str",
            "summarize_status",
            "message",
            "dump_history",
            "load_history",
            "proactive",
            "plan",
        },
    )

    def is_conversation_command(self, query: str | None) -> bool:
        """Check if the query is a conversation system command.

        ``/plan <description>`` (with arguments) is NOT a command — it
        passes through the runner to activate plan mode.  Only bare
        ``/plan`` is treated as a status command.

        Args:
            query: User query string

        Returns:
            True if query is a system command
        """
        if not isinstance(query, str) or not query.startswith("/"):
            return False
        stripped = query.strip().lstrip("/")
        parts = stripped.split(" ", 1)
        cmd = parts[0] if parts else ""
        if cmd == "plan" and len(parts) > 1 and parts[1].strip():
            return False
        return cmd in self.SYSTEM_COMMANDS


class CommandHandler(ConversationCommandHandlerMixin):
    """Handler for system commands (uses ConversationCommandHandlerMixin)."""

    def __init__(
        self,
        agent_name: str,
        memory: "AgentContext",
        memory_manager: "BaseMemoryManager | None" = None,
        context_manager: "BaseContextManager | None" = None,
        workspace: "Workspace | None" = None,
    ):
        """Initialize command handler.

        Args:
            agent_name: Name of the agent for message creation
            memory: Agent's context instance (AgentContext)
            memory_manager: Optional memory manager instance
            context_manager: Optional context manager instance
            workspace: Guarded application workspace for background features
        """
        self.agent_name = agent_name
        self.memory: "AgentContext" = memory
        self.memory_manager: "BaseMemoryManager" = memory_manager
        self.context_manager: "BaseContextManager" = context_manager
        self.workspace = workspace

    def _get_agent_config(self):
        """Get hot-reloaded agent config.

        Returns:
            AgentProfileConfig: The current agent configuration
        """
        return load_agent_config(self.memory_manager.agent_id)

    def is_command(self, query: str | None) -> bool:
        """Check if the query is a system command (alias for mixin)."""
        return self.is_conversation_command(query)

    async def _make_system_msg(
        self,
        text: str,
        metadata: dict | None = None,
    ) -> Any:
        """Create a system response message.

        Args:
            text: Message text content
            metadata: Optional structured metadata for downstream consumers

        Returns:
            System message
        """
        return legacy_message(
            name=self.agent_name,
            role="assistant",
            content=[TextBlock(type="text", text=text)],
            metadata=metadata or {},
        )

    @staticmethod
    def _system_action_metadata(
        *,
        kind: str,
        status: str,
        title: str,
        detail: str = "",
        **payload: Any,
    ) -> dict[str, Any]:
        """Build the stable presentation contract for command-only turns.

        System commands are control-plane events rather than authored answers.
        Keeping this distinction in metadata lets the console render a compact
        action notice instead of accidentally classifying the response as a
        document or other user-facing artifact.
        """
        action: dict[str, Any] = {
            "kind": kind,
            "status": status,
            "title": title,
        }
        if detail:
            action["detail"] = detail
        action.update(
            {
                key: value
                for key, value in payload.items()
                if value is not None
            },
        )
        return {"system_action": action}

    def _has_memory_manager(self) -> bool:
        """Check if memory manager is available."""
        return self.memory_manager is not None

    def _has_context_manager(self) -> bool:
        """Check if context manager is available."""
        return self.context_manager is not None

    async def _process_compact(
        self,
        messages: list[Any],
        args: str = "",
    ) -> Any:
        """Process /compact command."""
        extra_instruction = args.strip()
        if not messages:
            return await self._make_system_msg(
                "当前没有需要压缩的对话。",
                metadata=self._system_action_metadata(
                    kind="context_compaction",
                    status="empty",
                    title="无需压缩",
                    detail="当前对话上下文为空。",
                    message_count=0,
                ),
            )
        if not self._has_memory_manager() or not self._has_context_manager():
            return await self._make_system_msg(
                "当前未启用上下文压缩。",
                metadata=self._system_action_metadata(
                    kind="context_compaction",
                    status="unavailable",
                    title="暂时无法压缩",
                    detail="当前智能体未启用记忆或上下文管理。",
                    message_count=len(messages),
                ),
            )

        result = await self.context_manager.compact_context(
            messages=messages,
            previous_summary=self.memory.get_compressed_summary(),
            extra_instruction=extra_instruction,
        )

        if not result.get("success"):
            reason = result.get("reason", "unknown")
            before = result.get("before_tokens", 0)
            max_len = self._get_agent_config().running.max_input_length
            before_pct = (
                f"{before / max_len * 100:.0f}%" if max_len > 0 else "N/A"
            )
            return await self._make_system_msg(
                "对话压缩失败，请稍后重试。",
                metadata=self._system_action_metadata(
                    kind="context_compaction",
                    status="failed",
                    title="压缩未完成",
                    detail="可以重试；若上下文已超出上限，可新建或清空对话。",
                    technical_reason=str(reason),
                    message_count=len(messages),
                    before_tokens=before,
                    max_tokens=max_len,
                    before_percent=before_pct,
                ),
            )

        compact_content = result.get("history_compact", "")
        await self.memory.update_compressed_summary(compact_content)
        before = result.get("before_tokens", 0)
        after = result.get("after_tokens", 0)
        max_len = self._get_agent_config().running.max_input_length
        await self.memory.clear_content()
        saved_tokens = max(0, before - after)
        saved_percent = round(saved_tokens / before * 100) if before > 0 else 0
        return await self._make_system_msg(
            (
                f"对话已压缩：{len(messages)} 条消息，"
                f"{_fmt_tokens(before)} → {_fmt_tokens(after)} Token。"
            ),
            metadata=self._system_action_metadata(
                kind="context_compaction",
                status="completed",
                title="对话已压缩",
                detail="后续对话会继续使用这份上下文摘要。",
                message_count=len(messages),
                before_tokens=before,
                after_tokens=after,
                max_tokens=max_len,
                saved_tokens=saved_tokens,
                saved_percent=saved_percent,
                summary=compact_content,
            ),
        )

    async def _process_new(self, messages: list[Any], _args: str = "") -> Any:
        """Process /new command."""
        if not messages:
            self.memory.clear_compressed_summary()
            return await self._make_system_msg(
                "当前已是空对话。",
                metadata={
                    "clear_plan": True,
                    **self._system_action_metadata(
                        kind="new_conversation",
                        status="empty",
                        title="当前已是新对话",
                        detail="上下文摘要和任务计划均为空。",
                    ),
                },
            )
        if not self._has_memory_manager():
            return await self._make_system_msg(
                "当前无法保留摘要并新建对话。",
                metadata=self._system_action_metadata(
                    kind="new_conversation",
                    status="unavailable",
                    title="暂时无法新建对话",
                    detail="当前智能体未启用记忆管理。",
                ),
            )

        self.memory_manager.add_summarize_task(messages=messages)
        self.memory.clear_compressed_summary()

        await self.memory.clear_content()
        return await self._make_system_msg(
            "已开始新对话。",
            metadata={
                "clear_plan": True,
                **self._system_action_metadata(
                    kind="new_conversation",
                    status="completed",
                    title="已开始新对话",
                    detail="上一段对话正在后台归纳，任务计划已重置。",
                    message_count=len(messages),
                    background_memory_task=True,
                ),
            },
        )

    async def _process_clear(
        self,
        _messages: list[Any],
        _args: str = "",
    ) -> Any:
        """Process /clear command."""
        await self.memory.clear_content()
        self.memory.clear_compressed_summary()
        return await self._make_system_msg(
            "对话记录已清空。",
            metadata={
                "clear_history": True,
                "clear_plan": True,
                **self._system_action_metadata(
                    kind="history_clear",
                    status="completed",
                    title="对话记录已清空",
                    detail="上下文摘要和任务计划也已重置。",
                ),
            },
        )

    async def _process_compact_str(
        self,
        _messages: list[Any],
        _args: str = "",
    ) -> Any:
        """Process /compact_str command to show compressed summary."""
        summary = self.memory.get_compressed_summary()
        if not summary:
            return await self._make_system_msg(
                "当前还没有上下文摘要。",
                metadata=self._system_action_metadata(
                    kind="compressed_summary",
                    status="empty",
                    title="暂无上下文摘要",
                    detail="可以先压缩对话，或等待自动压缩。",
                ),
            )
        return await self._make_system_msg(
            "已读取当前上下文摘要。",
            metadata=self._system_action_metadata(
                kind="compressed_summary",
                status="completed",
                title="当前上下文摘要",
                summary=summary,
            ),
        )

    async def _process_history(
        self,
        _messages: list[Any],
        _args: str = "",
    ) -> Any:
        """Process /history command."""
        agent_config = self._get_agent_config()
        running_config = agent_config.running
        history_str = await self.memory.get_history_str(
            max_input_length=running_config.max_input_length,
        )

        # Truncate if too long
        if len(history_str) > running_config.history_max_length:
            half = running_config.history_max_length // 2
            history_str = f"{history_str[:half]}\n...\n{history_str[-half:]}"

        history_str += (
            "\n\n---\n\n- Use /message <index> to view full message content"
        )

        # Add compact summary hint if available
        if self.memory.get_compressed_summary():
            history_str += "\n- Use /compact_str to view full compact summary"

        return await self._make_system_msg(history_str)

    async def _process_summarize_status(
        self,
        _messages: list[Any],
        _args: str = "",
    ) -> Any:
        """Process /summarize_status command to show all status."""
        if not self._has_memory_manager():
            return await self._make_system_msg(
                "**Memory Manager Disabled**\n\n"
                "- Cannot list summary task status\n"
                "- Enable memory manager to use this feature",
            )

        task_list = self.memory_manager.list_summarize_status()
        if not task_list:
            return await self._make_system_msg(
                "**No Summary Tasks**\n\n"
                "- No summary tasks have been started",
            )

        status_lines = ["**Summary Task Status**\n\n"]
        for info in task_list:
            status_lines.append(
                f"- **{info['task_id']}**\n"
                f"  - Start: {info['start_time']}\n"
                f"  - Status: {info['status']}\n",
            )
            if info["status"] == "completed" and info["result"]:
                status_lines.append(f"  - Result: {info['result'][:200]}...\n")
            elif info["status"] == "failed" and info["error"]:
                status_lines.append(f"  - Error: {info['error']}\n")

        return await self._make_system_msg("".join(status_lines))

    async def _process_message(
        self,
        messages: list[Any],
        args: str = "",
    ) -> Any:
        """Process /message x command to show the nth message.

        Args:
            messages: List of messages in memory
            args: Command arguments (message index)

        Returns:
            System message with the requested message content
        """
        agent_config = self._get_agent_config()
        history_max_length = agent_config.running.history_max_length

        if not args:
            return await self._make_system_msg(
                "**Usage: /message <index>**\n\n"
                "- Example: /message 1 (show first message)\n"
                f"- Available messages: 1 to {len(messages)}",
            )

        try:
            index = int(args.strip())
        except ValueError:
            return await self._make_system_msg(
                f"**Invalid Index: '{args}'**\n\n"
                "- Index must be a number\n"
                "- Example: /message 1",
            )

        if not messages:
            return await self._make_system_msg(
                "**No Messages Available**\n\n- Current memory is empty",
            )

        if index < 1 or index > len(messages):
            return await self._make_system_msg(
                f"**Index Out of Range: {index}**\n\n"
                f"- Available range: 1 to {len(messages)}\n"
                f"- Example: /message 1",
            )

        msg = messages[index - 1]

        # Handle content display with truncation
        content_str = str(msg.content)
        truncated = False
        if len(content_str) > history_max_length:
            half = history_max_length // 2
            content_str = f"{content_str[:half]}\n...\n{content_str[-half:]}"
            truncated = True

        truncation_hint = (
            "\n\n- Content truncated, use /dump_history to view full content"
            if truncated
            else ""
        )
        return await self._make_system_msg(
            f"**Message {index}/{len(messages)}**\n\n"
            f"- **Timestamp:** {msg.timestamp}\n"
            f"- **Name:** {msg.name}\n"
            f"- **Role:** {msg.role}\n"
            f"- **Content:**\n{content_str}{truncation_hint}",
        )

    async def _process_dump_history(
        self,
        messages: list[Any],
        _args: str = "",
    ) -> Any:
        """Process /dump_history command to save messages to a JSONL file.

        Args:
            messages: List of messages in memory
            _args: Command arguments (unused)

        Returns:
            System message with dump result
        """
        agent_config = self._get_agent_config()
        history_file = Path(agent_config.workspace_dir) / DEBUG_HISTORY_FILE

        try:
            # Check if there's a compressed summary
            compressed_summary = self.memory.get_compressed_summary()
            has_summary = bool(compressed_summary)

            # Build dump messages: summary first (if exists), then messages
            dump_messages = []
            if has_summary:
                summary_msg = legacy_message(
                    name="user",
                    role="user",
                    content=[TextBlock(type="text", text=compressed_summary)],
                    metadata={"has_compressed_summary": "true"},
                )
                dump_messages.append(summary_msg)

            dump_messages.extend(messages)

            with open(history_file, "w", encoding="utf-8") as f:
                for msg in dump_messages:
                    f.write(
                        json.dumps(msg.to_dict(), ensure_ascii=False) + "\n",
                    )

            logger.info(
                f"Dumped {len(dump_messages)} messages to {history_file}",
            )
            return await self._make_system_msg(
                f"**History Dumped!**\n\n"
                f"- Messages saved: {len(dump_messages)}\n"
                f"- Has summary: {has_summary}\n"
                f"- File: `{history_file}`",
            )
        except Exception as e:
            logger.exception(f"Failed to dump history: {e}")
            return await self._make_system_msg(
                f"**Dump Failed**\n\n" f"- Error: {e}",
            )

    async def _process_load_history(
        self,
        _messages: list[Any],
        _args: str = "",
    ) -> Any:
        """Process /load_history command to load messages from a JSONL file.

        Args:
            _messages: List of messages in memory (unused)
            _args: Command arguments (unused)

        Returns:
            System message with load result
        """
        agent_config = self._get_agent_config()
        history_file = Path(agent_config.workspace_dir) / DEBUG_HISTORY_FILE

        if not history_file.exists():
            return await self._make_system_msg(
                f"**Load Failed**\n\n"
                f"- File not found: `{history_file}`\n"
                f"- Use /dump_history first to create the file",
            )

        try:
            loaded_messages: list[Any] = []
            has_summary_marker = False
            with open(history_file, encoding="utf-8") as f:
                for i, line in enumerate(f):
                    line = line.strip()
                    if line:
                        msg_dict = json.loads(line)
                        msg = legacy_message_from_dict(msg_dict)
                        loaded_messages.append(msg)
                        # Check first message for summary marker
                        if (
                            i == 0
                            and msg.metadata.get("has_compressed_summary")
                            == "true"
                        ):
                            has_summary_marker = True
                        if len(loaded_messages) >= MAX_LOAD_HISTORY_COUNT:
                            break

            # Clear existing memory
            self.memory.content.clear()
            self.memory.clear_compressed_summary()

            # If first message has summary marker, extract and restore summary
            if has_summary_marker and loaded_messages:
                summary_msg = loaded_messages.pop(0)
                # Extract summary content from the message
                summary_content = summary_msg.get_text_content() or ""
                # Set the compressed summary directly
                await self.memory.update_compressed_summary(summary_content)
                logger.info("Restored compressed summary from history file")

            for msg in loaded_messages:
                await self.memory.add(msg)

            logger.info(
                f"Loaded {len(loaded_messages)} messages from {history_file}",
            )
            return await self._make_system_msg(
                f"**History Loaded!**\n\n"
                f"- Messages loaded: {len(loaded_messages)}\n"
                f"- Has summary: {has_summary_marker}\n"
                f"- File: `{history_file}`\n"
                f"- Memory cleared before loading",
            )
        except Exception as e:
            logger.exception(f"Failed to load history: {e}")
            return await self._make_system_msg(
                f"**Load Failed**\n\n" f"- Error: {e}",
            )

    async def handle_conversation_command(self, query: str) -> Any:
        """Process conversation system commands.

        Args:
            query: Command string (e.g., "/compact", "/new", "/message 5")

        Returns:
            System response message

        Raises:
            SystemCommandException: If command is not recognized
        """
        messages = await self.memory.get_memory(
            prepend_summary=False,
        )
        # Parse command and arguments
        parts = query.strip().lstrip("/").split(" ", maxsplit=1)
        command = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        logger.info(f"Processing command: {command}, args: {args}")

        handler = getattr(self, f"_process_{command}", None)
        if handler is None:
            raise SystemCommandException(
                message=f"Unknown command: {query}",
            )
        response = await handler(messages, args)
        metadata = getattr(response, "metadata", None)
        if isinstance(metadata, dict):
            metadata.setdefault("system_command", command)
        return response

    async def handle_command(self, query: str) -> Any:
        """Process system commands (alias for handle_conversation_command)."""
        return await self.handle_conversation_command(query)

    async def _process_plan(
        self,
        _messages: list[Any],
        _args: str = "",
    ) -> Any:
        """Process bare /plan command to show plan status.

        This handler is only called for ``/plan`` without arguments.
        ``/plan <description>`` is routed through the runner instead.
        """
        from ..app.agent_context import get_current_agent_id

        agent_id = get_current_agent_id()
        try:
            agent_config = load_agent_config(agent_id)
            plan_enabled = getattr(
                getattr(agent_config, "plan", None),
                "enabled",
                False,
            )
        except Exception:
            plan_enabled = False

        if not plan_enabled:
            return await self._make_system_msg(
                "**Plan Mode**\n\n"
                "- Status: **disabled**\n"
                "- Enable plan mode in Settings → Plan to use "
                "`/plan <description>` for creating structured plans.",
            )
        return await self._make_system_msg(
            "**Plan Mode**\n\n"
            "- Status: **enabled**\n"
            "- Use `/plan <description>` to create a new plan\n"
            "- The plan panel on the right shows the current plan and "
            "progress\n"
            "- Use `/clear` or `/new` to clear any active plan",
        )

    async def _process_proactive(
        self,
        _messages: list[Any],
        args: str = "",
    ) -> Any:
        """Process /proactive command for proactive message feature."""
        args = args.strip().lower()
        from .memory import enable_proactive_for_session
        from ..app.agent_context import get_current_agent_id

        # Get current agent ID and language
        active_agent_id = get_current_agent_id()
        agent_config = load_agent_config(active_agent_id)
        agent_lang = getattr(agent_config, "language", "en")

        # Define warnings in both languages
        warning_en = (
            "**NOTE**: Proactive mode only acts on an evidence-linked, "
            "time-limited opportunity. Idle time alone creates no message, "
            "and screen capture is not used by default. "
            "Proactive mode can be turned off via /proactive off."
        )

        warning_zh = (
            "**请注意**：主动模式只会处理有证据、有有效期的提醒机会；"
            "仅仅空闲不会制造消息，默认也不会截取屏幕。"
            "可通过 /proactive off 关闭主动模式。"
        )

        # Define all message templates in both languages
        msg_templates = {
            "en": {
                "enabled": (
                    "**Proactive Mode Enabled**\n\n"
                    "- Idle time: {minutes} minutes\n"
                    "- Status: {result}\n"
                    "- Proactive messages will be sent after "
                    "{minutes} minutes of inactivity\n\n{warning}"
                ),
                "disabled": (
                    "**Proactive Mode Disabled**\n\n"
                    "- Proactive monitoring has been stopped\n"
                    "- No more proactive messages will be sent"
                ),
                "error_en": ("**Error Enabling Proactive Mode**\n-{error}"),
                "error_dis": ("**Error Disabling Proactive Mode**\n- {error}"),
                "error_args": (
                    "**Error Enabling Proactive Mode**\n\n"
                    "- {error}"
                    "- Usage: /proactive [minutes|on|off]\n"
                    "- Examples:\n"
                    "  • /proactive (default 30 minutes)\n"
                    "  • /proactive 45 (45 minutes idle time)\n"
                    "  • /proactive on (default 30 minutes)\n"
                    "  • /proactive off (disable proactive mode)\n"
                ),
            },
            "zh": {
                "enabled": (
                    "**主动模式已启用**\n\n"
                    "- 空闲时间: {minutes} 分钟\n"
                    "- 状态: {result}\n"
                    "- 将在 {minutes} 分钟不活动后发送主动消息\n\n{warning}"
                ),
                "disabled": ("**主动模式已停用**\n" "- 不再发送主动消息"),
                "error_en": ("**启用主动模式时出错**\n\n-{error}"),
                "error_dis": ("**禁用主动模式时出错**\n\n- {error}"),
                "error_args": (
                    "**启用主动模式时出错**\n\n"
                    "- {error}"
                    "- 使用方法: /proactive [分钟数|on|off]\n"
                    "- 示例:\n"
                    "  • /proactive (默认30分钟)\n"
                    "  • /proactive 45 (45分钟空闲时间)\n"
                    "  • /proactive on (默认30分钟)\n"
                    "  • /proactive off (禁用主动模式)\n"
                ),
            },
        }

        # Select messages and warning based on agent language
        lang_key = "zh" if agent_lang.lower() == "zh" else "en"
        msgs = msg_templates[lang_key]
        selected_warning = warning_zh if lang_key == "zh" else warning_en

        if not args or args == "on":
            try:
                result = enable_proactive_for_session(
                    self.agent_name,
                    30,
                    workspace=self.workspace,
                )
                return await self._make_system_msg(
                    msgs["enabled"].format(
                        minutes=30,
                        result=result,
                        warning=selected_warning,
                    ),
                )
            except Exception as e:
                return await self._make_system_msg(
                    msgs["error_en"].format(error=str(e)),
                )

        elif args == "off":
            try:
                import asyncio
                from .memory import proactive_tasks

                if self.agent_name in proactive_tasks:
                    task = proactive_tasks[self.agent_name]
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    del proactive_tasks[self.agent_name]

                return await self._make_system_msg(
                    msgs["disabled"],
                )
            except Exception as e:
                return await self._make_system_msg(
                    msgs["error_dis"].format(error=str(e)),
                )
        else:
            try:
                minutes = int(args)
                if minutes <= 0:
                    raise ValueError("Minutes must be a positive integer")

                result = enable_proactive_for_session(
                    self.agent_name,
                    minutes,
                    workspace=self.workspace,
                )
                return await self._make_system_msg(
                    msgs["enabled"].format(
                        minutes=minutes,
                        result=result,
                        warning=selected_warning,
                    ),
                )
            except Exception as e:
                return await self._make_system_msg(
                    msgs["error_args"].format(error=str(e)),
                )
