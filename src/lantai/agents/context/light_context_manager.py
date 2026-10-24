# -*- coding: utf-8 -*-
# pylint: disable=too-many-nested-blocks,too-many-branches
# pylint: disable=too-many-return-statements,too-many-statements
"""Context manager for agents with compaction support."""
import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Set

from .agent_context import AgentContext
from .as_msg_handler import AsMsgHandler
from .base_context_manager import BaseContextManager, context_registry
from ..capture_scope import should_capture_global_memory
from ..tools.utils import truncate_text_output, DEFAULT_MAX_BYTES
from ..utils import get_token_counter
from ..utils.estimate_token_counter import EstimatedTokenCounter
from ...config.config import load_agent_config
from ...constant import TRUNCATION_NOTICE_MARKER
from ...runtime.converters import legacy_message
from ...runtime.tool_response import TextBlock

if TYPE_CHECKING:
    from ..legacy_lantai_agent import LegacyLantaiAgent

logger = logging.getLogger(__name__)

FOREGROUND_MEMORY_RECALL_TIMEOUT_SECONDS = 1.5


def _fmt_tokens(n: int) -> str:
    """Format token count as e.g. '82.3k' or '450'."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


@context_registry.register("light")
class LightContextManager(BaseContextManager):
    """Context manager for agents with compaction support.

    Handles conversation context compaction and the agent context object.

    Responsibilities:
    - Tool-result pruning via _prune_tool_result()
    - Context-size checking via _check_context()
    - Message compaction via _compact_context()
    - Agent context retrieval via get_agent_context()
    """

    def __init__(self, working_dir: str, agent_id: str):
        """Initialize context manager.

        Args:
            working_dir: Working directory for context storage.
            agent_id: Agent ID for config loading.
        """
        super().__init__(working_dir=working_dir, agent_id=agent_id)
        logger.info(
            f"LightContextManager init: "
            f"agent_id={agent_id}, working_dir={working_dir}",
        )
        self._post_reply_tasks: set[asyncio.Task] = set()
        self._compaction_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # BaseContextManager interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the context manager lifecycle."""

    async def close(self) -> bool:
        """Close context manager and cleanup expired tool result files."""
        logger.info(f"LightContextManager closing: agent_id={self.agent_id}")
        if self._post_reply_tasks:
            pending = [
                task for task in self._post_reply_tasks if not task.done()
            ]
            if pending:
                # These tasks have already received a completed user turn.
                # Draining them preserves memory during workspace reload or
                # application shutdown; cancelling here silently loses the
                # very last disclosure in a session.
                await asyncio.gather(*pending, return_exceptions=True)
            self._post_reply_tasks.clear()
        self._cleanup_expired_tool_result_files()
        logger.info(f"LightContextManager closed: agent_id={self.agent_id}")
        return True

    def _schedule_post_reply_task(
        self,
        coro,
        *,
        name: str,
    ) -> None:
        """Run non-critical post-reply work without holding the chat run."""
        task = asyncio.create_task(coro, name=name)
        self._post_reply_tasks.add(task)
        task.add_done_callback(self._post_reply_tasks.discard)

    async def _capture_turn_after_reply(
        self,
        memory_manager: Any,
        messages: list[Any],
        session_id: str = "",
    ) -> None:
        try:
            capture = getattr(
                memory_manager,
                "capture_turn_serialized",
                memory_manager.capture_turn,
            )
            result = await capture(
                messages=messages,
                session_id=session_id,
                source="post_reply",
            )
            if isinstance(result, dict) and result.get("maintenance_required"):
                await self._system2_after_reply(memory_manager=memory_manager)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "layered memory post_reply capture failed",
                exc_info=True,
            )

    async def _system2_after_reply(
        self,
        memory_manager: Any,
    ) -> None:
        try:
            runner = getattr(memory_manager, "run_system2_consolidation", None)
            if runner is None:
                return
            await runner(reason="post_reply")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "layered memory System2 post_reply consolidation failed",
                exc_info=True,
            )

    def _cleanup_expired_tool_result_files(self) -> int:
        """Clean up tool result files older than retention_days.

        Returns:
            Number of files successfully deleted.
        """
        agent_config = load_agent_config(self.agent_id)
        lcc = agent_config.running.light_context_config
        trc = lcc.tool_result_pruning_config
        tool_result_dir = Path(self.working_dir) / trc.tool_results_cache
        retention_days = trc.offload_retention_days

        if not tool_result_dir.exists():
            return 0

        cutoff = datetime.now() - timedelta(days=retention_days)
        deleted = failed = 0

        for fp in tool_result_dir.glob("*.txt"):
            try:
                stat = os.stat(fp)
                if sys.platform == "win32":
                    ts = stat.st_ctime  # creation time on Windows
                else:
                    ts = getattr(
                        stat,
                        "st_birthtime",
                        stat.st_mtime,
                    )  # macOS/BSD; Linux fallback to mtime
                if datetime.fromtimestamp(ts) < cutoff:
                    fp.unlink()
                    deleted += 1
            except FileNotFoundError:
                pass  # deleted by another process between glob and stat/unlink
            except Exception as e:
                failed += 1
                logger.warning("Failed to delete %s: %s", fp, e)

        if deleted or failed:
            logger.info(
                "Cleaned up %d expired tool result files (%d failed)",
                deleted,
                failed,
            )
        return deleted

    def _truncate_tool_result(
        self,
        content: str,
        max_bytes: int,
        encoding: str = "utf-8",
    ) -> str:
        """Truncate tool result content, saving full content to file if needed.

        Args:
            content: The content to truncate.
            max_bytes: Maximum bytes allowed.
            encoding: Character encoding.

        Returns:
            Truncated content with notice if truncated,
            or original if under limit.
        """
        if not content:
            return content

        # Already truncated content - retruncate with new limit
        if TRUNCATION_NOTICE_MARKER in content:
            return truncate_text_output(
                content,
                max_bytes=max_bytes,
                encoding=encoding,
            )

        # Check if content fits within limit (with small slack)
        try:
            content_bytes = len(content.encode(encoding))
        except UnicodeEncodeError as e:
            logger.warning("Failed to encode content: %s", e)
            return content

        if content_bytes <= max_bytes + 100:
            return content

        # Save full content to file
        agent_config = load_agent_config(self.agent_id)
        lcc = agent_config.running.light_context_config
        trc = lcc.tool_result_pruning_config
        tool_result_dir = Path(self.working_dir) / trc.tool_results_cache

        try:
            tool_result_dir.mkdir(parents=True, exist_ok=True)
            fp = tool_result_dir / f"{uuid.uuid4().hex}.txt"
            fp.write_text(content, encoding=encoding)
            saved_path = str(fp)
        except OSError as e:
            logger.exception(f"Failed to save tool result to file: {e}")
            # Fallback: truncate without saving
            return truncate_text_output(
                content,
                max_bytes=max_bytes,
                encoding=encoding,
            )

        # Truncate and include file path in notice
        return truncate_text_output(
            content,
            start_line=1,
            total_lines=content.count("\n") + 1,
            max_bytes=max_bytes,
            file_path=saved_path,
            encoding=encoding,
        )

    def _prune_output(
        self,
        output: str | list[dict],
        max_bytes: int,
        encoding: str = "utf-8",
    ) -> str | list[dict]:
        """Prune output by truncating to max_bytes.

        Args:
            output: The output to prune (str or list[dict]).
            max_bytes: Maximum bytes allowed.
            encoding: Character encoding.

        Returns:
            Pruned output.
        """
        if isinstance(output, str):
            return self._truncate_tool_result(output, max_bytes, encoding)
        if isinstance(output, list):
            for block in output:
                if isinstance(block, dict) and block.get("type") == "text":
                    block["text"] = self._truncate_tool_result(
                        block.get("text", ""),
                        max_bytes,
                        encoding,
                    )
        return output

    async def _prune_tool_result(
        self,
        messages: list[Any],
        recent_n: int = 1,
        old_max_bytes: int = 3000,
        recent_max_bytes: int = DEFAULT_MAX_BYTES,
        **_kwargs,
    ) -> list[Any]:
        """Process all messages, truncating large tool results.

        Args:
            messages: List of messages to process.
            recent_n: Number of recent messages to treat with recent_max_bytes.
            old_max_bytes: Maximum bytes for older tool results.
            recent_max_bytes: Maximum bytes for recent tool results.
            retention_days: Days to retain offloaded files
                (unused here, set in init).

        Returns:
            Processed messages list.
        """
        if not messages:
            return messages

        # Count recent tool_result messages from the end
        recent_count = 0
        for msg in reversed(messages):
            if not isinstance(msg.content, list) or not any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in msg.content
            ):
                break
            recent_count += 1
        split_index = max(0, len(messages) - max(recent_count, recent_n))

        # Detect tool_use IDs for exempt file extensions and tool names
        exempt_tool_ids: Set[str] = set()
        try:
            # Load exempt lists from config
            agent_config = load_agent_config(self.agent_id)
            lcc = agent_config.running.light_context_config
            trc = lcc.tool_result_pruning_config
            exempt_extensions = set(
                ext.lower() for ext in trc.exempt_file_extensions
            )
            exempt_tools = set(name.lower() for name in trc.exempt_tool_names)

            for msg in messages:
                if not isinstance(msg.content, list):
                    continue

                for block in msg.content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                    ):
                        tool_id = block.get("id", "")
                        if not tool_id:
                            continue

                        tool_name = block.get("name", "").lower()
                        raw_input = (block.get("raw_input") or "").lower()

                        # Check if tool name is in exempt list
                        if tool_name in exempt_tools:
                            exempt_tool_ids.add(tool_id)
                            continue

                        # Check if file extension is in exempt list
                        # for read_file
                        if tool_name == "read_file":
                            for ext in exempt_extensions:
                                if ext in raw_input:
                                    exempt_tool_ids.add(tool_id)
                                    break
        except Exception as e:
            logger.warning("Failed to detect exempt tool ids: %s", e)

        # Prune tool_result blocks
        for idx, msg in enumerate(messages):
            if not isinstance(msg.content, list):
                continue
            is_recent = idx >= split_index
            max_bytes = recent_max_bytes if is_recent else old_max_bytes

            for block in msg.content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                ):
                    tool_id = block.get("id", "")
                    output = block.get("output")
                    if not output:
                        continue

                    # Use recent_max_bytes for exempt tool results
                    effective_max_bytes = (
                        recent_max_bytes
                        if tool_id in exempt_tool_ids
                        else max_bytes
                    )
                    block["output"] = self._prune_output(
                        output,
                        effective_max_bytes,
                    )

        return messages

    @staticmethod
    async def _check_context(
        messages: list[Any],
        context_compact_threshold: int,
        context_compact_reserve: int,
        as_token_counter: EstimatedTokenCounter,
    ) -> tuple[list[Any], list[Any], int, int]:
        """Check context size and determine if compaction is needed.

        Uses AsMsgHandler to analyze messages and split them into
        messages_to_compact and messages_to_keep based on token thresholds.

        Args:
            messages: List of conversation messages to check.
            context_compact_threshold: Token threshold triggering compaction.
            context_compact_reserve: Token limit for messages to keep.
            as_token_counter: Token counter instance.

        Returns:
            Tuple of (messages_to_compact, messages_to_keep,
            total_tokens, keep_tokens):
            - messages_to_compact: Older messages exceeding reserve limit.
            - messages_to_keep: Recent messages within reserve limit.
            - total_tokens: Total token count of all messages.
            - keep_tokens: Token count of messages to keep.
        """
        msg_handler = AsMsgHandler(as_token_counter)
        return await msg_handler.context_check(
            messages=messages,
            context_compact_threshold=context_compact_threshold,
            context_compact_reserve=context_compact_reserve,
        )

    @staticmethod
    def _is_valid_summary(content: str) -> bool:
        """Check if the summary content is valid.

        Args:
            content: The summary content to validate.

        Returns:
            True if valid, False otherwise.
        """
        if not content or not content.strip():
            return False
        if "##" not in content:
            return False
        return True

    async def _compact_context(
        self,
        messages: list[Any],
        previous_summary: str = "",
        extra_instruction: str = "",
        as_llm: Any | None = None,
        as_llm_formatter: Any | None = None,
        as_token_counter: EstimatedTokenCounter | None = None,
        language: str = "en",
        max_input_length: int = 100000,
        compact_ratio: float = 0.5,
        compact_token_budget: int | None = None,
        add_thinking_block: bool = True,
        **_kwargs,
    ) -> dict:
        """Checkpoint older messages without making a hidden model request.

        Args:
            messages: List of messages to compact.
            previous_summary: Previous summary to update.
            extra_instruction: Extra instruction for compaction.
            as_llm: Legacy compatibility parameter; ignored.
            as_llm_formatter: Legacy compatibility parameter; ignored.
            as_token_counter: Token counter instance.
            language: Language for the checkpoint heading ("en" or "zh").
            max_input_length: Maximum input length for token calculation.
            compact_ratio: Legacy ratio used only when no token budget is
                supplied by the context planner.
            compact_token_budget: Planner-derived token budget for the source
                material retained in the local checkpoint.
            add_thinking_block: Whether to include thinking blocks.

        Returns:
            Dict with keys:
            - success: Whether compaction produced a valid result.
            - reason: Failure reason (empty string on success).
            - user_message: Empty; retained for API compatibility.
            - history_compact: A bounded local conversation checkpoint.
            - is_valid: Whether the summary passed format validation.
            - before_tokens: Token count of messages before compaction.
            - after_tokens: Token count of the compacted summary.
        """
        if not messages:
            return {
                "success": False,
                "reason": "empty messages",
                "user_message": "",
                "history_compact": "",
                "is_valid": False,
                "before_tokens": 0,
                "after_tokens": 0,
            }

        agent_config = load_agent_config(self.agent_id)

        # Use provided token counter or get from config
        token_counter = as_token_counter or get_token_counter(agent_config)

        msg_handler = AsMsgHandler(token_counter)
        before_token_count = await msg_handler.count_msgs_token(messages)

        # New callers provide a capacity-aware token budget.  Keep the ratio
        # fallback for old direct callers/config files, but do not use a fixed
        # percentage in the production lifecycle path.
        memory_compact_threshold = (
            max(1, int(compact_token_budget))
            if compact_token_budget is not None
            else int(max_input_length * compact_ratio)
        )

        history_formatted_str: str = await msg_handler.format_msgs_to_str(
            messages=messages,
            context_compact_threshold=memory_compact_threshold,
            include_thinking=add_thinking_block,
        )
        after_token_count = await msg_handler.count_str_token(
            history_formatted_str,
        )
        logger.info(
            f"Compactor before_token_count={before_token_count} "
            f"after_token_count={after_token_count}",
        )

        if not history_formatted_str:
            logger.warning(f"No history to compact. messages={messages}")
            return {
                "success": False,
                "reason": "formatted history is empty",
                "user_message": "",
                "history_compact": "",
                "is_valid": False,
                "before_tokens": before_token_count,
                "after_tokens": 0,
            }

        del as_llm, as_llm_formatter, extra_instruction

        # Foreground context maintenance must be deterministic and bounded.
        # A second, hidden LLM call used to block the user's requested answer
        # for up to 20 seconds and could contend with the active model.  Keep a
        # verbatim checkpoint here; richer semantic memory consolidation runs
        # after the visible reply on the existing background path.
        heading = "## 较早对话检查点" if language.lower() == "zh" else "## Earlier conversation checkpoint"
        parts = [heading]
        if previous_summary.strip():
            prior_heading = "### 已有检查点" if language.lower() == "zh" else "### Previous checkpoint"
            parts.extend((prior_heading, previous_summary.strip()))
        current_heading = "### 本次保留的原始记录" if language.lower() == "zh" else "### Preserved transcript"
        parts.extend((current_heading, history_formatted_str.strip()))
        history_compact = "\n\n".join(part for part in parts if part)
        is_valid = self._is_valid_summary(history_compact)

        after_tokens = await msg_handler.count_str_token(history_compact)
        logger.info(f"Compactor Result:\n{history_compact[:500]}...")

        return {
            "success": True,
            "reason": "",
            "user_message": "",
            "history_compact": history_compact,
            "is_valid": True,
            "before_tokens": before_token_count,
            "after_tokens": after_tokens,
        }

    async def _compact_context_safe(
        self,
        messages: list[Any],
        previous_summary: str = "",
        extra_instruction: str = "",
        as_llm: Any | None = None,
        as_llm_formatter: Any | None = None,
        as_token_counter: EstimatedTokenCounter | None = None,
        language: str = "en",
        max_input_length: int = 100000,
        compact_ratio: float = 0.5,
        compact_token_budget: int | None = None,
        add_thinking_block: bool = True,
        **_kwargs,
    ) -> dict:
        """Safe wrapper for _compact_context with fallback on failure.

        Args: Same as _compact_context.

        Returns:
            Dict with same keys as _compact_context, guaranteed to return
            a valid dict even on exception.
        """
        if self._compaction_lock.locked():
            return {
                "success": False,
                "reason": "context compaction already in progress",
                "user_message": "",
                "history_compact": "",
                "is_valid": False,
                "before_tokens": 0,
                "after_tokens": 0,
            }

        try:
            async with self._compaction_lock:
                return await self._compact_context(
                    messages=messages,
                    previous_summary=previous_summary,
                    extra_instruction=extra_instruction,
                    as_llm=as_llm,
                    as_llm_formatter=as_llm_formatter,
                    as_token_counter=as_token_counter,
                    language=language,
                    max_input_length=max_input_length,
                    compact_ratio=compact_ratio,
                    compact_token_budget=compact_token_budget,
                    add_thinking_block=add_thinking_block,
                    **_kwargs,
                )
        except Exception as e:
            logger.exception("Context compaction failed: %s", e)
            return {
                "success": False,
                "reason": f"LLM error: {e}",
                "user_message": "",
                "history_compact": "",
                "is_valid": False,
                "before_tokens": 0,
                "after_tokens": 0,
            }

    async def compact_context(
        self,
        messages: list[Any],
        previous_summary: str = "",
        extra_instruction: str = "",
    ) -> dict:
        """Public interface for context compaction.

        Args:
            messages: List of messages to compact.
            previous_summary: Previous summary to update (if exists).
            extra_instruction: Extra instruction for compaction.

        Returns:
            Dict with keys: success, reason, history_compact,
            before_tokens, after_tokens.
        """
        agent_config = load_agent_config(self.agent_id)
        running_config = agent_config.running
        ccc = running_config.light_context_config.context_compact_config
        effective_window = self._effective_context_window(agent_config)

        result = await self._compact_context_safe(
            messages=messages,
            previous_summary=previous_summary,
            extra_instruction=extra_instruction,
            as_token_counter=get_token_counter(agent_config),
            language=agent_config.language,
            max_input_length=effective_window,
            compact_ratio=ccc.compact_threshold_ratio,
            compact_token_budget=self._compaction_source_budget(effective_window),
            add_thinking_block=ccc.compact_with_thinking_block,
        )
        return {
            "success": result.get("success", False),
            "reason": result.get("reason", ""),
            "history_compact": result.get("history_compact", ""),
            "before_tokens": result.get("before_tokens", 0),
            "after_tokens": result.get("after_tokens", 0),
        }

    # ------------------------------------------------------------------
    # Agent lifecycle hook methods
    # ------------------------------------------------------------------

    @staticmethod
    def _compaction_source_budget(effective_window: int) -> int:
        """Bound one summarizer input without a percentage-based threshold."""

        return min(8192, max(512, int(effective_window) - 384))

    @staticmethod
    def _effective_context_window(agent_config: Any) -> int:
        """Resolve the actual hard cap used by legacy compaction.

        The old code used the requested Agent setting directly, which could
        make a 256K Agent compact too late for a local 64K llama.cpp runtime.
        This intentionally mirrors the model-call Profile resolver.
        """

        profile = LightContextManager._context_profile(agent_config)
        if profile is not None and profile.effective_window is not None:
            return int(profile.effective_window)
        running = getattr(agent_config, "running", None)
        return int(getattr(running, "max_input_length", 0) or 0)

    @staticmethod
    def _context_input_budget(agent_config: Any) -> int:
        """Return the policy target that triggers pre-turn compaction."""

        profile = LightContextManager._context_profile(agent_config)
        running = getattr(agent_config, "running", None)
        if profile is not None:
            mode = str(
                getattr(running, "context_policy_mode", "smart")
                or "smart",
            )
            return int(profile.policy(mode).target_input_tokens)
        return int(getattr(running, "max_input_length", 0) or 0)

    @staticmethod
    def _context_profile(agent_config: Any) -> Any | None:
        """Resolve one exact model profile for compaction and diagnostics."""

        running = getattr(agent_config, "running", None)
        requested = int(getattr(running, "max_input_length", 0) or 0)
        try:
            from ...model_context_profile import ModelContextProfile
            from ...providers.provider_manager import ProviderManager

            manager = ProviderManager.get_instance()
            slot = getattr(agent_config, "active_model", None) or manager.get_active_model()
            provider_id = str(getattr(slot, "provider_id", "") or "")
            model_id = str(getattr(slot, "model", "") or "")
            local_window = None
            if provider_id in {"lantai-local", "copaw-local"}:
                from ...local_models.manager import LocalModelManager

                local_window = LocalModelManager.get_instance().get_config().max_context_length
            return ModelContextProfile.from_provider_selection(
                provider_manager=manager,
                provider_id=provider_id,
                model_id=model_id,
                user_window=requested or None,
                local_runtime_window=local_window,
            )
        except Exception:
            logger.debug("Unable to resolve compaction context profile", exc_info=True)
        return None

    @staticmethod
    def _context_compaction_status_metadata(
        *,
        status: str,
        title: str,
        detail: str,
        **payload: Any,
    ) -> dict[str, Any]:
        """Build the user-facing contract for automatic compaction events."""

        action: dict[str, Any] = {
            "kind": "context_compaction",
            "status": status,
            "title": title,
            "detail": detail,
            "automatic": True,
        }
        action.update(
            {
                key: value
                for key, value in payload.items()
                if value is not None
            },
        )
        return {"system_action": action}

    @staticmethod
    async def _print_status_message(
        agent: "LegacyLantaiAgent",
        text: str,
        *,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        last: bool = True,
        sink: Any | None = None,
    ) -> Any:
        msg = legacy_message(
            name=agent.name,
            role="assistant",
            content=[TextBlock(type="text", text=text)],
            metadata=metadata,
        )
        if message_id:
            msg.id = message_id
        if callable(sink):
            result = sink(msg, last)
            if isinstance(result, asyncio.Future) or hasattr(result, "__await__"):
                await result
        else:
            await agent.print(msg, last=last)
        return msg

    async def pre_reply(
        self,
        agent: "LegacyLantaiAgent",
        kwargs: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Augment ``msg`` with retrieved memory results before reply.

        When ``auto_memory_search_config.enabled`` is enabled, calls
        ``memory_manager.retrieve()`` which returns a dict with updated
        messages. Commands are skipped because ``reply()`` returns early
        before the ReAct loop runs.
        """
        msg = kwargs.get("msg")
        if msg is None:
            return None

        last_msg = msg[-1] if isinstance(msg, list) else msg
        query = (
            last_msg.get_text_content()
            if hasattr(last_msg, "get_text_content")
            else None
        )

        # Commands are handled before the ReAct loop — skip memory search.
        command_handler = agent.command_handler
        if command_handler is not None and command_handler.is_command(query):
            return None

        agent_config = load_agent_config(self.agent_id)
        rlmc = agent_config.running.reme_light_memory_config
        ms = rlmc.auto_memory_search_config

        if not ms.enabled:
            return None

        memory_manager = agent.memory_manager
        if memory_manager is None:
            return None

        try:
            result = await asyncio.wait_for(
                memory_manager.retrieve(msg, agent_name=agent.name),
                timeout=FOREGROUND_MEMORY_RECALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "memory_manager.retrieve exceeded the foreground budget; "
                "continuing without automatic recall",
            )
            return None
        except BaseException as e:
            logger.warning(
                "memory_manager.retrieve failed, skipping e=%s",
                e,
            )
            return None

        if result is None:
            return None

        return {**kwargs, **result}

    async def pre_reasoning(
        self,
        agent: "LegacyLantaiAgent",
        kwargs: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Check context size and compact memory when threshold is exceeded.

        Mirrors the compaction logic from ``MemoryCompactionHook`` but
        excludes tool-result pruning, which is handled by
        ``post_acting``.
        """

        compaction_message_id: str | None = None
        compaction_terminal_emitted = False
        status_sink = kwargs.get("_context_status_sink")
        try:
            memory_manager = agent.memory_manager
            if memory_manager is None:
                return None

            agent_config = load_agent_config(self.agent_id)
            running_config = agent_config.running
            token_counter = get_token_counter(agent_config)

            memory = agent.memory
            system_prompt = agent.sys_prompt
            compressed_summary = memory.get_compressed_summary()
            sys_token_count = await token_counter.count(
                messages=[],
                text=(system_prompt or ""),
            )
            summary_token_count = await token_counter.count(
                messages=[],
                text=(compressed_summary or ""),
            )
            additional_messages = kwargs.get("_additional_context_messages")
            if not isinstance(additional_messages, list):
                additional_messages = []
            additional_token_count = (
                await AsMsgHandler(token_counter).count_msgs_token(
                    additional_messages,
                )
                if additional_messages
                else 0
            )
            str_token_count = (
                sys_token_count
                + summary_token_count
                + additional_token_count
            )

            ccc = running_config.light_context_config.context_compact_config
            max_len = self._context_input_budget(agent_config)
            # Use the exact same policy target as the final payload planner.
            # Output/reasoning and serialization reserves were already removed
            # by ``ModelContextProfile.policy``.
            context_compact_threshold = max(0, max_len)
            left_compact_threshold = (
                context_compact_threshold - str_token_count
            )
            context_compact_reserve = min(
                8192,
                max(512, left_compact_threshold),
            )

            if left_compact_threshold <= 0:
                logger.warning(
                    "The context_compact_threshold is set too low; "
                    "the combined token length of system_prompt and "
                    "compressed_summary exceeds the configured threshold. "
                    "Alternatively, you could use /clear to reset the context "
                    "and compressed_summary, ensuring the total remains "
                    "below the threshold.",
                )
                return None

            messages = await memory.get_memory(prepend_summary=False)

            (
                messages_to_compact,
                messages_to_keep,
                ctx_total_tokens,
                ctx_keep_tokens,
            ) = await self._check_context(
                messages=messages,
                context_compact_threshold=left_compact_threshold,
                context_compact_reserve=context_compact_reserve,
                as_token_counter=token_counter,
            )

            if not messages_to_compact:
                return None

            # Build context status info for printing
            compact_count = len(messages_to_compact)
            keep_count = len(messages_to_keep)

            total_tokens = str_token_count + ctx_total_tokens
            keep_tokens = ctx_keep_tokens
            compaction_message_id = f"context-compaction-{uuid.uuid4().hex}"
            terminal_text = ""
            terminal_metadata: dict[str, Any] = {}

            if ccc.enabled:
                started_detail = (
                    f"正在将较早的 {compact_count} 条消息转换为摘要；"
                    "完成后将继续回答。"
                )
                await self._print_status_message(
                    agent,
                    f"正在自动压缩上下文。{started_detail}",
                    message_id=compaction_message_id,
                    metadata=self._context_compaction_status_metadata(
                        status="running",
                        title="正在自动压缩上下文",
                        detail=started_detail,
                        message_count=compact_count,
                        before_tokens=total_tokens,
                    ),
                    last=False,
                    sink=status_sink,
                )
                result = await self._compact_context_safe(
                    messages=messages_to_compact,
                    previous_summary=memory.get_compressed_summary(),
                    as_llm=agent.model,
                    as_llm_formatter=agent.formatter,
                    as_token_counter=token_counter,
                    language=agent_config.language,
                    max_input_length=max_len,
                    compact_ratio=ccc.compact_threshold_ratio,
                    compact_token_budget=self._compaction_source_budget(max_len),
                    add_thinking_block=ccc.compact_with_thinking_block,
                )

                compact_content = (
                    result.get("history_compact", "")
                    if result.get("success")
                    else ""
                )
                if not compact_content:
                    reason = result.get("reason", "unknown")
                    # Fallback keeps everything that fits the same
                    # capacity-aware budget; it never restores a percentage
                    # based threshold.
                    (
                        fallback_to_compact,
                        fallback_to_keep,
                        ctx_total_tokens,
                        ctx_keep_tokens,
                    ) = await self._check_context(
                        messages=messages,
                        context_compact_threshold=left_compact_threshold,
                        context_compact_reserve=left_compact_threshold,
                        as_token_counter=token_counter,
                    )
                    messages_to_compact = fallback_to_compact
                    messages_to_keep = fallback_to_keep
                    compact_content = memory.get_compressed_summary() or ""
                    keep_count = len(messages_to_keep)
                    compact_count = len(messages_to_compact)
                    total_tokens = str_token_count + ctx_total_tokens
                    if compact_count > 0:
                        terminal_detail = (
                            f"自动压缩未完成；已保留最近的 {keep_count} 条消息"
                            f"继续回答，较早的 {compact_count} 条未进入本轮上下文。"
                            "若后续出现细节断层，请补充关键背景。"
                        )
                    else:
                        terminal_detail = (
                            f"自动压缩未完成；已保留当前 {keep_count} 条消息"
                            "继续回答。"
                        )
                    terminal_text = f"上下文已降级整理。{terminal_detail}"
                    terminal_metadata = self._context_compaction_status_metadata(
                        status="degraded",
                        title="上下文已降级整理",
                        detail=terminal_detail,
                        message_count=compact_count,
                        technical_reason=str(reason),
                        summary=compact_content or None,
                    )
                else:
                    after_total = (
                        sys_token_count
                        + result.get("after_tokens", 0)
                        + keep_tokens
                    )
                    saved_tokens = max(0, total_tokens - after_total)
                    saved_percent = (
                        round(saved_tokens / total_tokens * 100)
                        if total_tokens > 0
                        else 0
                    )
                    terminal_detail = (
                        f"较早的 {compact_count} 条消息已转为摘要，"
                        f"最近 {keep_count} 条保留原文。"
                        "后续回答可能省略部分原始措辞与细节。"
                    )
                    terminal_text = f"上下文已自动压缩。{terminal_detail}"
                    terminal_metadata = self._context_compaction_status_metadata(
                        status="completed",
                        title="上下文已自动压缩",
                        detail=terminal_detail,
                        message_count=compact_count,
                        before_tokens=total_tokens,
                        after_tokens=after_total,
                        saved_tokens=saved_tokens,
                        saved_percent=saved_percent,
                        summary=compact_content,
                    )
            else:
                # See the enabled-compaction fallback above: use the same
                # planner-derived budget when dropping old history.
                (
                    fallback_to_compact,
                    fallback_to_keep,
                    ctx_total_tokens,
                    ctx_keep_tokens,
                ) = await self._check_context(
                    messages=messages,
                    context_compact_threshold=left_compact_threshold,
                    context_compact_reserve=left_compact_threshold,
                    as_token_counter=token_counter,
                )
                messages_to_compact = fallback_to_compact
                messages_to_keep = fallback_to_keep
                compact_content = memory.get_compressed_summary() or ""
                keep_count = len(messages_to_keep)
                compact_count = len(messages_to_compact)
                total_tokens = str_token_count + ctx_total_tokens
                if compact_count > 0:
                    terminal_detail = (
                        "自动压缩已关闭；为继续回答，"
                        f"已保留最近 {keep_count} 条消息，较早的 "
                        f"{compact_count} 条未进入本轮上下文。"
                        "若后续出现细节断层，请补充关键背景。"
                    )
                else:
                    terminal_detail = (
                        "自动压缩已关闭；当前对话仍可在模型窗口内继续。"
                    )
                terminal_text = f"上下文已裁剪。{terminal_detail}"
                terminal_metadata = self._context_compaction_status_metadata(
                    status="degraded",
                    title="上下文已裁剪",
                    detail=terminal_detail,
                    message_count=compact_count,
                    summary=compact_content or None,
                )

            updated_count = await memory.mark_messages_compressed(
                messages_to_compact,
            )
            logger.info(f"Marked {updated_count} messages as compacted")

            await memory.update_compressed_summary(compact_content)

            terminal_message = await self._print_status_message(
                agent,
                terminal_text,
                message_id=compaction_message_id,
                metadata=terminal_metadata,
                sink=status_sink,
            )
            await memory.add(terminal_message)
            compaction_terminal_emitted = True

        except Exception as e:
            logger.exception(
                "Failed to compact memory in pre_reasoning hook: %s",
                e,
                exc_info=True,
            )
            if compaction_message_id and not compaction_terminal_emitted:
                detail = (
                    "整理过程异常中断；本轮将继续使用当前可用的"
                    "上下文。若后续出现细节断层，请补充关键背景。"
                )
                try:
                    await self._print_status_message(
                        agent,
                        f"上下文整理已中断。{detail}",
                        message_id=compaction_message_id,
                        metadata=self._context_compaction_status_metadata(
                            status="failed",
                            title="上下文整理已中断",
                            detail=detail,
                            technical_reason=str(e),
                        ),
                        sink=status_sink,
                    )
                except Exception:
                    logger.debug(
                        "Unable to emit terminal compaction status",
                        exc_info=True,
                    )

        return None

    async def post_acting(
        self,
        agent: "LegacyLantaiAgent",
        kwargs: dict[str, Any],
        output: Any,
    ) -> Any | None:
        """Truncate oversized tool-call results after each acting step."""
        try:
            agent_config = load_agent_config(self.agent_id)
            lcc = agent_config.running.light_context_config
            trc = lcc.tool_result_pruning_config
            if not trc.enabled:
                return None

            memory = agent.memory
            messages = await memory.get_memory(prepend_summary=False)
            memory_manager = agent.memory_manager
            if memory_manager is not None:
                await memory_manager.offload_tool_results(messages=messages)
            await self._prune_tool_result(
                messages=messages,
                recent_n=trc.pruning_recent_n,
                old_max_bytes=trc.pruning_old_msg_max_bytes,
                recent_max_bytes=trc.pruning_recent_msg_max_bytes,
                retention_days=trc.offload_retention_days,
            )
        except Exception as e:
            logger.exception(
                "Failed to prune tool results in post_acting hook: %s",
                e,
                exc_info=True,
            )

        return None

    async def post_reply(
        self,
        agent: "LegacyLantaiAgent",
        kwargs: dict[str, Any],
        output: Any,
    ) -> Any | None:
        """Capture memory after a reply without periodic maintenance work."""
        try:
            memory_manager = agent.memory_manager
            if memory_manager is None:
                return None

            request_context = getattr(agent, "_request_context", {}) or {}
            if not should_capture_global_memory(request_context):
                return None

            memory = agent.memory
            messages = [msg for msg, _ in memory.content]
            if messages:
                session_id = str(request_context.get("session_id") or "")
                self._schedule_post_reply_task(
                    self._capture_turn_after_reply(
                        memory_manager=memory_manager,
                        messages=list(messages[-12:]),
                        session_id=session_id,
                    ),
                    name=f"post-reply-memory-capture:{self.agent_id}",
                )

            # Legacy ``auto_memory_interval`` stays readable in configuration,
            # but is intentionally not a scheduler: L0 capture emits the work
            # event, Atom extraction decides whether System2 is required, and
            # compaction retains ownership of summary generation.
        except Exception as e:
            logger.warning("post_reply hook failed: %s", e)

        return None

    def get_agent_context(self, **_kwargs) -> AgentContext:
        """Retrieve the agent context object with token counting support."""
        agent_config = load_agent_config(self.agent_id)
        dialog_path = os.path.join(
            self.working_dir,
            agent_config.running.light_context_config.dialog_path,
        )
        return AgentContext(
            token_counter=get_token_counter(agent_config),
            dialog_path=dialog_path,
        )
