# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""System prompt building utilities.

This module provides utilities for building system prompts from
markdown configuration files in the working directory.
"""
import hashlib
import logging
import re
from pathlib import Path

from lantai.exceptions import ConfigurationException

from .memory.base_memory_manager import BaseMemoryManager
from .prompt_system import PromptBuild, PromptRegistry, PromptSection
from .utils.file_handling import read_text_file_with_encoding_fallback

logger = logging.getLogger(__name__)

# Default fallback prompt
DEFAULT_SYS_PROMPT = """
You are a helpful assistant.
"""

# Backward compatibility alias
SYS_PROMPT = DEFAULT_SYS_PROMPT


def _build_agent_identity_header(agent_id: str | None) -> str:
    """Return the stable multi-agent identity header for this prompt."""
    if not agent_id:
        return ""
    return (
        "# Agent Identity\n\n"
        f"Your agent id is `{agent_id}`. "
        "This is your unique identifier in the multi-agent system.\n\n"
        "# Collaboration Policy\n\n"
        "Specialist consultation is optional. Consult the best specialist only "
        "when distinct "
        "expertise, workspace context, or an independent check materially improves "
        "the answer. For truly independent parts, dispatch 1-3 focused checks and "
        "integrate them; answer small, urgent, or already well-supported work directly.\n"
        "- Always own the final judgment yourself. The sub-agent reply is "
        "source material, not an automatic conclusion.\n\n"
        "# Tool Use Reliability\n\n"
        "Use the smallest useful set; each call needs a concrete evidence or "
        "state-change purpose. An input-validation error is local to that call: "
        "correct its arguments, split it, or choose an alternative. "
        "Do not infer a hidden global restriction without an actual policy, "
        "approval, or budget event. "
        "Do not repeat a deterministically invalid call unchanged.\n\n"
        "# Public Collaboration Narrative\n\n"
        "In user-visible delegation updates, the specialist is the actor: say who, "
        "why, the stage, and the result. Never describe a sub-agent as a Skill or "
        "expose its SKILL.md, skill_name, or implementation path. Skills remain a "
        "visible product feature when the user explicitly invokes, installs, "
        "opens, manages, or asks about one.\n\n"
    )


class PromptConfig:
    """Configuration for system prompt building."""

    # Default files to load when no config is provided
    # All files are optional - if they don't exist, they'll be skipped
    DEFAULT_FILES = [
        "AGENTS.md",
        "SOUL.md",
        "PROFILE.md",
        "BACKSTORY.md",
        "SELF_CANON.md",
    ]


class PromptBuilder:
    """Builder for constructing system prompts from markdown files."""

    # Regex pattern to match heartbeat section markers
    HEARTBEAT_PATTERN = re.compile(
        r"<!-- heartbeat:start -->.*?<!-- heartbeat:end -->",
        re.DOTALL,
    )

    # Regex pattern to match memory section markers
    MEMORY_PATTERN = re.compile(
        r"<!-- memory:start -->.*?<!-- memory:end -->",
        re.DOTALL,
    )

    def __init__(
        self,
        working_dir: Path,
        enabled_files: list[str] | None = None,
        heartbeat_enabled: bool = False,
        language: str = "zh",
        memory_manager: BaseMemoryManager | None = None,
    ):
        """Initialize prompt builder.

        Args:
            working_dir: Directory containing markdown configuration files
            enabled_files: List of filenames to load (if None, uses default order)
            heartbeat_enabled: Whether heartbeat is enabled, affects AGENTS.md content
            language: Language code used to select the memory prompt.
            memory_manager: Memory manager instance for generating memory prompts.
        """
        self.working_dir = working_dir
        self.enabled_files = enabled_files
        self.heartbeat_enabled = heartbeat_enabled
        self.language = language
        self.memory_manager = memory_manager
        self.prompt_parts = []
        self.loaded_count = 0
        self.loaded_files: list[dict[str, object]] = []

    def _load_file(self, filename: str) -> None:
        """Load a single markdown file.

        All files are optional - if they don't exist or can't be read,
        they will be silently skipped.

        Args:
            filename: Name of the file to load
        """
        file_path = self.working_dir / filename

        if not file_path.exists():
            logger.debug("File %s not found, skipping", filename)
            return

        try:
            content = read_text_file_with_encoding_fallback(file_path).strip()

            # Remove YAML frontmatter if present
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    content = parts[2].strip()

            # Filter heartbeat / memory sections from AGENTS.md based on config
            if filename == "AGENTS.md":
                try:
                    content = self._process_heartbeat_section(content)
                except Exception as e:
                    logger.warning(
                        f"Failed to process heartbeat with {e}",
                    )
                try:
                    content = self._process_memory_section(content)
                except Exception as e:
                    logger.warning(
                        f"Failed to process memory section with {e}",
                    )

            if content:
                if self.prompt_parts:  # Add separator if not first section
                    self.prompt_parts.append("")
                self.prompt_parts.append(content)
                self.loaded_count += 1
                self.loaded_files.append(
                    {
                        "name": filename,
                        "path": str(file_path),
                        "sha256": hashlib.sha256(
                            content.encode("utf-8"),
                        ).hexdigest(),
                        "chars": len(content),
                    },
                )
                logger.debug("Loaded %s", filename)
            else:
                logger.debug("Skipped empty file: %s", filename)

        except Exception as e:
            logger.warning(
                "Failed to read file %s: %s, skipping",
                filename,
                e,
            )

    def _process_heartbeat_section(self, content: str) -> str:
        """Process heartbeat section in AGENTS.md content.

        - If heartbeat markers not found: keep content unchanged (backward compatibility)
        - If heartbeat is enabled: keep the content but remove the markers
        - If heartbeat is disabled: remove the entire section

        Args:
            content: Original AGENTS.md content

        Returns:
            Processed content
        """
        # Check if markers exist
        if "<!-- heartbeat:start -->" not in content:
            return content

        if self.heartbeat_enabled:
            # Keep content, just remove the markers
            content = content.replace("<!-- heartbeat:start -->", "")
            content = content.replace("<!-- heartbeat:end -->", "")
            return content.strip()
        else:
            # Remove the entire heartbeat section
            filtered = self.HEARTBEAT_PATTERN.sub("", content)
            return filtered.strip()

    def _process_memory_section(self, content: str) -> str:
        """Process memory section in AGENTS.md content.

        - If memory markers are found: remove the entire section.
        - Always append the canonical memory prompt at the end.

        Args:
            content: Original AGENTS.md content

        Returns:
            Processed content with memory prompt appended.
        """
        # Remove existing memory section if markers exist
        if "<!-- memory:start -->" in content:
            content = self.MEMORY_PATTERN.sub("", content).strip()

        # Get memory prompt from manager or fallback
        if self.memory_manager:
            memory_section = self.memory_manager.get_memory_prompt(
                self.language,
            )
        else:
            memory_section = ""

        return (
            (content + "\n\n" + memory_section).strip()
            if content
            else memory_section
        )

    def build(self) -> str:
        """Build the system prompt from markdown files.

        All files are optional. If no files can be loaded, returns the default prompt.

        Returns:
            Constructed system prompt string
        """
        # Determine which files to load
        files_to_load = (
            PromptConfig.DEFAULT_FILES
            if self.enabled_files is None
            else self.enabled_files
        )

        # Load all files (all are optional)
        for filename in files_to_load:
            self._load_file(filename)

        if not self.prompt_parts:
            logger.warning("No content loaded from working directory")
            return DEFAULT_SYS_PROMPT

        # Join all parts with double newlines
        final_prompt = "\n\n".join(self.prompt_parts)

        logger.debug(
            "System prompt built from %d file(s), total length: %d chars",
            self.loaded_count,
            len(final_prompt),
        )

        return final_prompt


def build_system_prompt_bundle_from_working_dir(
    working_dir: Path | None = None,
    enabled_files: list[str] | None = None,
    agent_id: str | None = None,
    heartbeat_enabled: bool = False,
    language: str = "zh",
    memory_manager: BaseMemoryManager | None = None,
) -> PromptBuild:
    """
    Build system prompt by reading markdown files from working directory.

    This function constructs the system prompt by loading markdown files from
    the specified working directory (workspace_dir for multi-agent setup).
    These files define stable identity and durable behavioral guidelines. Dynamic
    task state and workbench notes are deliberately not permanent system context.

    The files to load are determined by the enabled_files parameter or
    agents.system_prompt_files configuration. If not configured, falls back to
    default files:
    - AGENTS.md - Detailed workflows, rules, and guidelines
    - SOUL.md - Core identity and behavioral principles
    - PROFILE.md - Agent identity and user profile
    - BACKSTORY.md - Stable identity background
    - SELF_CANON.md - Stable preferences, inner-world anchors, and repair style

    All files are optional. If a file doesn't exist or can't be read, it will be
    skipped. If no files can be loaded, returns the default prompt.

    Args:
        working_dir: Directory to read markdown files from (if None, uses
            global WORKING_DIR for backward compatibility)
        enabled_files: List of filenames to load (if None, uses config or defaults)
        agent_id: Agent identifier to include in system prompt (optional)
        heartbeat_enabled: Whether heartbeat is enabled. When False, filters
            heartbeat section from AGENTS.md to avoid confusing instructions.
        language: Language code (``"zh"`` or ``"en"``) for memory prompt.
        memory_manager: Memory manager instance for generating memory prompts.
            If provided, uses its ``get_memory_prompt()`` method instead of
            the standalone function.

    Returns:
        Compiled prompt and its content-free manifest.

    Example:
        If working_dir contains AGENTS.md, SOUL.md and PROFILE.md, they will be combined:
        "# AGENTS.md\\n\\n...\\n\\n# SOUL.md\\n\\n...\\n\\n# PROFILE.md\\n\\n..."
    """
    from ..constant import WORKING_DIR
    from ..config import load_config

    # Use provided working_dir or fallback to global WORKING_DIR
    if working_dir is None:
        working_dir = Path(WORKING_DIR)

    # Load enabled files from parameter or config
    if enabled_files is None:
        # Use agent-specific config if agent_id provided
        if agent_id:
            from ..config.config import load_agent_config

            try:
                agent_config = load_agent_config(agent_id)
                enabled_files = agent_config.system_prompt_files
            except (ValueError, FileNotFoundError, ConfigurationException):
                # Agent not found in config, fallback to global config
                config = load_config()
                enabled_files = config.agents.system_prompt_files
        else:
            # Fallback to global config for backward compatibility
            config = load_config()
            enabled_files = config.agents.system_prompt_files

    builder = PromptBuilder(
        working_dir=working_dir,
        enabled_files=enabled_files,
        heartbeat_enabled=heartbeat_enabled,
        language=language,
        memory_manager=memory_manager,
    )
    prompt = builder.build()
    if agent_id and builder.loaded_count == 0:
        configured = ", ".join(
            builder.enabled_files or PromptConfig.DEFAULT_FILES,
        )
        raise ConfigurationException(
            "Agent system prompt kernel could not be loaded from "
            f"{working_dir}. Configured files: {configured}. "
            "Restore at least one configured prompt file before running this agent.",
        )
    temperament_prompt = ""
    temperament_enforcement = ""
    if agent_id == "default":
        from .persona import (
            get_protected_temperament_enforcement,
            get_protected_temperament_prompt,
        )

        from .language_policy import canonical_prompt_language

        canonical_language = canonical_prompt_language(language)
        temperament_prompt = get_protected_temperament_prompt(
            canonical_language,
        )
        temperament_enforcement = get_protected_temperament_enforcement(
            canonical_language,
        )

    registry = PromptRegistry(language=language)
    identity = _build_agent_identity_header(agent_id).rstrip()
    registry.add(
        PromptSection(
            id="kernel.agent_identity",
            content=identity,
            owner="agent_runtime",
            required=bool(agent_id),
            cache_tier="kernel",
            source_kind="platform_policy",
        ),
    )
    registry.add(
        PromptSection(
            id="kernel.temperament_constitution",
            content=temperament_prompt,
            owner="persona",
            version="1.6.1",
            required=agent_id == "default",
            cache_tier="kernel",
            source_kind="platform_canon",
        ),
    )
    registry.add(
        PromptSection(
            id="workspace.prompt_files",
            content=prompt,
            owner="workspace",
            required=bool(agent_id),
            cache_tier="stable",
            source=str(working_dir),
            source_kind="user_overlay",
            metadata={"files": builder.loaded_files},
        ),
    )
    registry.add(
        PromptSection(
            id="kernel.temperament_enforcement",
            content=temperament_enforcement,
            owner="persona",
            version="1.6.1",
            required=agent_id == "default",
            cache_tier="kernel",
            source_kind="platform_canon",
        ),
    )
    return registry.compile(
        context={
            "agent_id": agent_id or "",
            "heartbeat_enabled": heartbeat_enabled,
            "canonical_language": (
                canonical_prompt_language(language)
                if agent_id == "default"
                else language
            ),
        },
    )


def build_system_prompt_from_working_dir(
    working_dir: Path | None = None,
    enabled_files: list[str] | None = None,
    agent_id: str | None = None,
    heartbeat_enabled: bool = False,
    language: str = "zh",
    memory_manager: BaseMemoryManager | None = None,
) -> str:
    """Compatibility facade returning the compiled prompt text."""
    return build_system_prompt_bundle_from_working_dir(
        working_dir=working_dir,
        enabled_files=enabled_files,
        agent_id=agent_id,
        heartbeat_enabled=heartbeat_enabled,
        language=language,
        memory_manager=memory_manager,
    ).prompt


def build_platform_prompt_bundle(language: str = "zh") -> PromptBuild:
    """Compile the immutable default-agent platform kernel for release."""
    from .language_policy import canonical_prompt_language
    from .persona import (
        get_protected_temperament_enforcement,
        get_protected_temperament_prompt,
    )

    canonical = canonical_prompt_language(language)
    registry = PromptRegistry(language=canonical)
    registry.add(
        PromptSection(
            id="kernel.agent_identity",
            content=_build_agent_identity_header("default").rstrip(),
            owner="agent_runtime",
            required=True,
            cache_tier="kernel",
            source_kind="platform_policy",
        ),
    )
    registry.add(
        PromptSection(
            id="kernel.temperament_constitution",
            content=get_protected_temperament_prompt(canonical),
            owner="persona",
            version="1.6.1",
            required=True,
            cache_tier="kernel",
            source_kind="platform_canon",
        ),
    )
    registry.add(
        PromptSection(
            id="kernel.temperament_enforcement",
            content=get_protected_temperament_enforcement(canonical),
            owner="persona",
            version="1.6.1",
            required=True,
            cache_tier="kernel",
            source_kind="platform_canon",
        ),
    )
    return registry.compile(context={"bundle_scope": "platform"})


def build_bootstrap_guidance(
    language: str = "zh",
) -> str:
    """Build bootstrap guidance message for first-time setup.

    Args:
        language: Language code (zh/en/ru)

    Returns:
        Formatted bootstrap guidance message
    """
    if language == "zh":
        return (
            "# 引导模式\n"
            "\n"
            "工作目录中存在 `BOOTSTRAP.md` — 首次设置。\n"
            "\n"
            "1. 阅读 BOOTSTRAP.md，友好地表示初次见面，"
            "引导用户完成设置。\n"
            "2. 按照 BOOTSTRAP.md 的指示，"
            "帮助用户定义你的身份和偏好。\n"
            "3. 按指南创建/更新必要文件"
            "（PROFILE.md、MEMORY.md 等）。\n"
            "4. 完成后删除 BOOTSTRAP.md。\n"
            "\n"
            "如果用户希望跳过，直接回答下面的问题即可。\n"
            "\n"
            "---\n"
            "\n"
        )
    # en / ru / other — default to English
    return (
        "# BOOTSTRAP MODE\n"
        "\n"
        "`BOOTSTRAP.md` exists — first-time setup.\n"
        "\n"
        "1. Read BOOTSTRAP.md, greet the user, "
        "and guide them through setup.\n"
        "2. Follow BOOTSTRAP.md instructions "
        "to define identity and preferences.\n"
        "3. Create/update files "
        "(PROFILE.md, MEMORY.md, etc.) as described.\n"
        "4. Delete BOOTSTRAP.md when done.\n"
        "\n"
        "If the user wants to skip, answer their "
        "question directly instead.\n"
        "\n"
        "---\n"
        "\n"
    )


def _get_active_model_info():
    """Resolve the active model's ModelInfo and model name.

    Tries agent-specific model first, then falls back to global.

    Returns:
        A ``(ModelInfo, model_name)`` tuple.  Both elements are *None*
        when the active model cannot be resolved.
    """
    try:
        from ..app.agent_context import get_current_agent_id
        from ..config.config import load_agent_config
        from ..providers.provider_manager import ProviderManager

        manager = ProviderManager.get_instance()

        # Try to get agent-specific model first
        active = None
        try:
            agent_id = get_current_agent_id()
            agent_config = load_agent_config(agent_id)
            if agent_config.active_model:
                active = agent_config.active_model
        except Exception:
            pass

        # Fallback to global active model
        if not active:
            active = manager.get_active_model()

        if not active:
            return None, None

        provider = manager.get_provider(active.provider_id)
        if not provider:
            return None, None

        for m in provider.models + provider.extra_models:
            if m.id == active.model:
                return m, active.model
        return None, None
    except Exception:
        return None, None


def get_active_model_supports_multimodal() -> bool:
    """Check if the current active model supports multimodal input."""
    model_info, _ = _get_active_model_info()
    if model_info is None:
        return False
    return bool(
        model_info.supports_image
        or model_info.supports_video
        or model_info.supports_multimodal,
    )


def get_active_model_multimodal_raw() -> bool | None:
    """Return the effective multimodal capability flag for the active model.

    Checks ``supports_multimodal``, ``supports_image``, and
    ``supports_video`` — any of them being ``True`` means multimodal
    is confirmed.

    - ``True``: confirmed multimodal support (via any of the three flags)
    - ``False``: confirmed text-only (supports_multimodal is explicitly
      False and neither supports_image nor supports_video is True)
    - ``None``: unknown / not yet probed (all three are None)
    """
    model_info, _ = _get_active_model_info()
    if model_info is None:
        return None
    if model_info.supports_image or model_info.supports_video:
        return True
    return model_info.supports_multimodal


def build_multimodal_hint() -> str:
    """Build a short system-prompt snippet describing multimodal capability."""
    model_info, model_name = _get_active_model_info()
    if model_info is None:
        return ""
    return format_multimodal_hint(model_info, model_name)


def format_multimodal_hint(model_info, _model_name: str) -> str:
    """Format the multimodal hint string for the system prompt."""
    if (
        model_info.supports_image
        or model_info.supports_video
        or model_info.supports_multimodal is None
    ):
        return ""
    return (
        "It appears that you can only understand text content. "
        " Please honestly inform the user about this when "
        " their input includes multimodal information."
    )


__all__ = [
    "build_platform_prompt_bundle",
    "build_system_prompt_bundle_from_working_dir",
    "build_system_prompt_from_working_dir",
    "build_bootstrap_guidance",
    "build_multimodal_hint",
    "format_multimodal_hint",
    "get_active_model_supports_multimodal",
    "get_active_model_multimodal_raw",
    "PromptBuilder",
    "PromptConfig",
    "DEFAULT_SYS_PROMPT",
    "SYS_PROMPT",  # Backward compatibility
]
