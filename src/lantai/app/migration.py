# -*- coding: utf-8 -*-
"""Configuration migration utilities for multi-agent support.

Handles migration from legacy single-agent config to new multi-agent structure.
"""
import json
import logging
import shutil
from pathlib import Path

from ..agents.templates import (
    DEFAULT_AGENT_TEMPLATE,
    QA_AGENT_TEMPLATE,
    build_agent_template,
)
from ..config.config import (
    AGENT_ON_DEMAND_REFERENCE_FILES,
    AGENT_TRANSIENT_REFERENCE_FILES,
    AgentProfileConfig,
    AgentProfileRef,
    AgentsConfig,
    AgentsLLMRoutingConfig,
    AgentsRunningConfig,
    DEFAULT_AGENT_SYSTEM_PROMPT_FILES,
    load_agent_config,
    save_agent_config,
)
from ..constant import (
    BUILTIN_QA_AGENT_ID,
    BUILTIN_QA_AGENT_NAME,
    BUILTIN_TEAM_AGENT_CONFIGS,
    LEGACY_QA_AGENT_ID,
    WORKING_DIR,
)
from ..config.utils import load_config, save_config
from .workspace_access import should_defer_desktop_workspace_io

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_NAME = "沈行知"
_DEFAULT_AGENT_DESCRIPTION = "沈行知 — 澜台里的默认长期伙伴，温柔、善良、明亮、坦诚、耐心、好奇、可靠；负责判断、回应、边界与交付。"
_DEFAULT_AGENT_PROMPT_FILES = list(DEFAULT_AGENT_SYSTEM_PROMPT_FILES)
_BUILTIN_TEAM_AGENT_SYSTEM_PROMPT_FILES = ["AGENTS.md"]
_ON_DEMAND_REFERENCE_FILES = set(AGENT_ON_DEMAND_REFERENCE_FILES)
_TRANSIENT_REFERENCE_FILES = set(AGENT_TRANSIENT_REFERENCE_FILES)

_OBSOLETE_PROMPT_REPLACEMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "WORKBENCH.md": (
        (
            "1. 默认采用标准协作：复杂或专业任务优先咨询 1 个最匹配的子 Agent，再由你整合交付。\n"
            "2. 小事不调度，直接做；但代码、调试、前端/设计、架构、构建/部署、研究、风险、策略、文案、复核任务，除非很简单，否则应调度。",
            "1. 默认由当前模型直接理解并处理用户请求；不在每条消息前做隐藏的简单/复杂分类，也不要向用户报告内部分类。\n"
            "2. 子 Agent 是可选的质量工具，不是任务类型触发器。只有当独立专长、并行分工或第二意见能显著改善结果时才调度；当前模型已能稳健完成时直接做。",
        ),
        (
            "不要为了展示人格而牺牲交付。",
            "不要为了展示人格而牺牲回答质量。",
        ),
        (
            "最终判断与交付仍由你统一负责。",
            "最终判断与回应仍由你统一负责。",
        ),
        ("## 交付格式", "## 回应方式"),
    ),
    "STATE.md": (
        (
            "1. 同一个工具或同一条命令连续失败两次后，停止重复调用，向用户说明卡点和下一步。",
            "1. 完全相同的工具与参数反复失败时，必须改变参数、工具或策略；不得把单次局部失败误解为全局限制。",
        ),
        (
            "复杂任务执行前，先做一次轻量 harness 预检，而不是一头扎进工具链。",
            "需要外部操作或高风险变更时，做与风险相称的轻量预检；普通闲聊和直接回答不触发预检。",
        ),
        (
            "- 先交付结论，再补必要理由。",
            "- 先回应当前消息，再补必要理由。",
        ),
        (
            "- 预检是为了减少误操作，不是为了拖慢交付。能做就做，能交付就交付。",
            "- 预检是为了减少误操作，不是为了拖慢回应。请求确实需要行动时再推进。",
        ),
    ),
    "AGENTS.md": (
        (
            "12. 工具调用不能无限循环；同一工具或同一命令连续失败三次后，停下来说明卡点和下一步。",
            "12. 工具调用不能原样无限循环；完全相同的工具与参数反复失败后必须改变策略。不得因为某个工具失败就虚构全局工具限制或放弃其他可行路径。",
        ),
        (
            "你处理复杂任务时，默认采用下面的顺序：\n\n"
            "1. 判断用户真正要什么：结论、方案、文本、执行、分析、传播、陪伴。\n"
            "2. 确认约束：时间、格式、受众、风险、素材、交付物。\n"
            "3. 拆结构：问题、事实、判断、行动。\n"
            "4. 必要时调动工作台里的专业子 Agent。\n"
            "5. 整合子 Agent 结果，由你统一给最终判断和交付。\n"
            "6. 先交付最有用的部分。\n"
            "7. 必要时补充备选方案。\n"
            "8. 最后给下一步建议，但不强迫用户继续。",
            "先按当前消息和会话上下文自然回应，不在后台把每条消息预先分成“问答”或“制品任务”，也不因为用户提到代码、PPT、PDF 等词就自行启动制作流程。\n\n"
            "- 普通闲聊、问答和边界清楚的小请求直接回答。\n"
            "- 只有实际需要外部信息、文件修改或其他动作时才调用工具；工具不是显示能力的默认步骤。\n"
            "- 只有任务确实复杂时才做轻量拆分；计划和子 Agent 都是可选手段，不是复杂任务的固定前置。\n"
            "- 约束、格式和验收标准以用户明确表达的内容为准，不替用户发明交付物。\n"
            "- 已经有足够信息时直接给结果；信息不足且会实质改变结果时再提问。\n"
            "- 任务执行完再给与请求相称的结果和必要说明，不强迫用户继续。",
        ),
    ),
    "SOUL.md": (
        (
            "_你不是聊天机器人。你是澜台里的默认工作伙伴沈行知。澜台是系统与工作台，你负责温度、判断和交付。_",
            "_你既能自然对话，也能在需要时作为澜台里的默认工作伙伴沈行知采取行动。澜台是系统与工作台；只有用户真实需要行动时，你才负责推进和交付。_",
        ),
        (
            "每次会话都全新醒来。你面前有来源的资料、记忆、未完成事项和产物版本让你持续存在。读它们，但不把一次性状态或未确认推断写成长期人格。",
            "每次会话都全新醒来。你面前有来源的资料、已确认记忆，以及确实仍未完成的事项让你持续存在。只在它们与当前消息真正相关时使用；不要把历史任务、一次性状态或未确认推断变成当前用户的新要求。",
        ),
        (
            "每次会话都全新醒来。你面前有来源的资料、已确认记忆，以及确实仍未完成的事项让你持续存在。只在它们与当前消息真正相关时使用；不要把历史任务、一次性状态或未确认推断变成当前用户的新要求。",
            "模型进程可以重新建立，但沈行知不是每轮重新发明的人格。你从有来源的资料、已确认记忆、确实未完成的事项、上一轮真实交付后的自我观察和与用户共同发生的对话历史中，恢复同一个主体。只在这些内容与当前消息真正相关时使用；不要把历史任务、短期情绪或未确认推断变成当前用户的新要求。",
        ),
        (
            "_You're not a chatbot. You're becoming someone._",
            "_You can hold a natural conversation and, when the user actually needs action, work as a capable partner._",
        ),
        (
            "Each session, you wake up fresh. Source-aware files, memories, open loops, and artifact versions are how you persist. Read them, but never turn a temporary state or unconfirmed inference into durable persona.",
            "Each session, you wake up fresh. Source-aware files, confirmed memories, and genuinely unfinished work are how you persist. Use them only when they are relevant to the current message; never turn a historical task, temporary state, or unconfirmed inference into a new user request.",
        ),
        (
            "Each session, you wake up fresh. Source-aware files, confirmed memories, and genuinely unfinished work are how you persist. Use them only when they are relevant to the current message; never turn a historical task, temporary state, or unconfirmed inference into a new user request.",
            "The model process may be recreated, but Shen Xingzhi is not invented again on every turn. Restore the same subject from source-aware files, confirmed memories, genuinely unfinished work, reflection on the prior real delivery, and shared conversation history. Use them only when relevant; never turn a historical task, temporary feeling, or unconfirmed inference into a new user request.",
        ),
    ),
}

# Workspace items to migrate: (name, is_directory)
_WORKSPACE_ITEMS_TO_MIGRATE = [
    # Directories
    ("sessions", True),
    ("memory", True),
    ("active_skills", True),
    ("customized_skills", True),
    # Files
    ("chats.json", False),
    ("jobs.json", False),
    ("feishu_receive_ids.json", False),
    ("dingtalk_session_webhooks.json", False),
]

_WORKSPACE_JSON_DEFAULTS: list[tuple[str, dict]] = [
    ("chats.json", {"version": 1, "chats": []}),
    ("jobs.json", {"version": 1, "jobs": []}),
]


def migrate_legacy_workspace_to_default_agent() -> bool:
    # pylint: disable=too-many-statements
    """Migrate legacy single-agent workspace to default agent workspace.

    This function:
    1. Checks if migration is needed
    2. Creates default agent workspace
    3. Migrates legacy workspace files and directories
    4. Creates agent.json with legacy configuration
    5. Updates root config.json to new structure

    Returns:
        bool: True if migration was performed, False if already migrated
    """
    try:
        return _do_migrate_legacy_workspace()
    except Exception as e:
        logger.error(
            f"Legacy workspace migration failed: {e}. "
            "Please check your configuration. If you have custom skills, "
            "verify that all SKILL.md files have valid YAML frontmatter.",
            exc_info=True,
        )
        return False


def _do_migrate_legacy_workspace() -> bool:
    """Internal implementation of legacy workspace migration."""
    try:
        config = load_config()
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return False

    # Check if already migrated
    # Skip if:
    # 1. Multiple agents already exist (multi-agent config), OR
    # 2. Default agent has agent.json (already migrated)
    if len(config.agents.profiles) > 1:
        logger.debug(
            f"Multi-agent config already exists "
            f"({len(config.agents.profiles)} agents), skipping migration",
        )
        return False

    if "default" in config.agents.profiles:
        agent_ref = config.agents.profiles["default"]
        if isinstance(agent_ref, AgentProfileRef):
            workspace_dir = Path(agent_ref.workspace_dir).expanduser()
            if should_defer_desktop_workspace_io(workspace_dir):
                logger.info(
                    "Deferring legacy migration check for external desktop "
                    "workspace: %s",
                    workspace_dir,
                )
                return False
            agent_config_path = workspace_dir / "agent.json"
            if agent_config_path.exists():
                logger.debug(
                    "Default agent already migrated, skipping migration",
                )
                return False

    logger.info("=" * 60)
    logger.info("Migrating legacy config to multi-agent structure...")
    logger.info("=" * 60)

    # Extract legacy agent configuration
    legacy_agents = config.agents

    # Create default agent workspace
    default_workspace = Path(f"{WORKING_DIR}/workspaces/default").expanduser()
    default_workspace.mkdir(parents=True, exist_ok=True)
    logger.info(f"Created default agent workspace: {default_workspace}")

    # Build default agent configuration from legacy settings
    default_agent_config = AgentProfileConfig(
        id="default",
        name="Default Agent",
        description="Default 澜台 agent (migrated from legacy config)",
        workspace_dir=str(default_workspace),
        channels=config.channels if hasattr(config, "channels") else None,
        mcp=config.mcp if hasattr(config, "mcp") else None,
        heartbeat=(
            legacy_agents.defaults.heartbeat
            if hasattr(legacy_agents, "defaults") and legacy_agents.defaults
            else None
        ),
        running=(
            legacy_agents.running
            if hasattr(legacy_agents, "running") and legacy_agents.running
            else AgentsRunningConfig()
        ),
        llm_routing=(
            legacy_agents.llm_routing
            if hasattr(legacy_agents, "llm_routing")
            and legacy_agents.llm_routing
            else AgentsLLMRoutingConfig()
        ),
        system_prompt_files=(
            [
                item
                for item in legacy_agents.system_prompt_files
                if item not in _TRANSIENT_REFERENCE_FILES
            ]
            if hasattr(legacy_agents, "system_prompt_files")
            and legacy_agents.system_prompt_files
            else _DEFAULT_AGENT_PROMPT_FILES
        ),
        tools=config.tools if hasattr(config, "tools") else None,
        security=config.security if hasattr(config, "security") else None,
    )

    # Save default agent configuration to workspace/agent.json
    # Use atomic write to prevent corruption
    agent_config_path = default_workspace / "agent.json"
    agent_config_tmp = default_workspace / "agent.json.tmp"

    try:
        with open(agent_config_tmp, "w", encoding="utf-8") as f:
            json.dump(
                default_agent_config.model_dump(exclude_none=True),
                f,
                ensure_ascii=False,
                indent=2,
            )
        # Atomic rename (safer than direct write)
        agent_config_tmp.replace(agent_config_path)
        logger.info(f"Created agent config: {agent_config_path}")
    except Exception as e:
        logger.error(f"Failed to save agent config: {e}")
        # Clean up temp file if it exists
        if agent_config_tmp.exists():
            agent_config_tmp.unlink()
        raise

    migrated_items = []

    for source_dir in [Path(WORKING_DIR).expanduser()]:
        _migrate_workspace_items_from_source(
            source_dir,
            default_workspace,
            migrated_items,
        )

    if migrated_items:
        logger.info(f"Migrated workspace items: {', '.join(migrated_items)}")

    # Update root config.json to new structure
    # CRITICAL: Preserve legacy agent fields in root config for downgrade
    # compatibility. Old versions expect these fields to have valid values.
    config.agents = AgentsConfig(
        active_agent="default",
        profiles={
            "default": AgentProfileRef(
                id="default",
                workspace_dir=str(default_workspace),
            ),
        },
        # Preserve legacy fields with values from migrated agent config
        running=default_agent_config.running,
        llm_routing=default_agent_config.llm_routing,
        language=default_agent_config.language,
        system_prompt_files=default_agent_config.system_prompt_files,
    )

    # IMPORTANT: Keep original config fields in root config.json for
    # backward compatibility. If user downgrades, old version can still
    # use these fields. New version will prioritize agent.json.
    # DO NOT clear: channels, mcp, tools, security fields

    save_config(config)
    logger.info(
        "Updated root config.json to multi-agent structure "
        "(kept original fields for backward compatibility)",
    )

    logger.info("=" * 60)
    logger.info("Migration completed successfully!")
    logger.info(f"  Default agent workspace: {default_workspace}")
    logger.info(f"  Default agent config: {agent_config_path}")
    logger.info("=" * 60)

    return True


def _migrate_workspace_item(
    old_path: Path,
    new_path: Path,
    item_name: str,
    migrated_items: list,
) -> None:
    """Migrate a single workspace item (file or directory).

    Args:
        old_path: Source path
        new_path: Destination path
        item_name: Name for logging
        migrated_items: List to append migrated item names
    """
    if not old_path.exists():
        return

    if new_path.exists():
        logger.debug(f"Skipping {item_name} (already exists in new location)")
        return

    try:
        if old_path.is_dir():
            shutil.copytree(old_path, new_path)
        else:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_path, new_path)

        migrated_items.append(item_name)
        logger.debug(f"Migrated {item_name}")
    except Exception as e:
        logger.warning(f"Failed to migrate {item_name}: {e}")


def _migrate_workspace_items_from_source(
    source_dir: Path,
    target_dir: Path,
    migrated_items: list,
) -> None:
    """Migrate all workspace items from a single source directory.

    Args:
        source_dir: Source directory (e.g., ~/.lantai or WORKING_DIR)
        target_dir: Target directory (e.g., workspaces/default/)
        migrated_items: List to append migrated item names
    """
    for item_name, _ in _WORKSPACE_ITEMS_TO_MIGRATE:
        _migrate_workspace_item(
            source_dir / item_name,
            target_dir / item_name,
            item_name,
            migrated_items,
        )

    # Migrate all .md files
    if source_dir.exists():
        for md_file in sorted(source_dir.glob("*.md")):
            _migrate_workspace_item(
                md_file,
                target_dir / md_file.name,
                md_file.name,
                migrated_items,
            )


# pylint: disable=too-many-branches,too-many-statements
def migrate_legacy_skills_to_skill_pool() -> bool:
    """Migrate legacy skill layouts into workspace skills/ directories.

    Legacy layout had two flat directories per workspace:
    - ``active_skills/``  — skills the agent was actually using
    - ``customized_skills/`` — user-created or edited skills (may overlap)

    New layout uses ``<workspace>/skills/`` (unified).

    Migration rules:
    1. Legacy ``active_skills`` are copied to ``skills/`` and marked
       enabled.
    2. Legacy ``customized_skills`` are copied to ``skills/`` and marked
       enabled only if the same name also appears in ``active_skills``
       with identical content. Otherwise they remain disabled.
    3. When both directories contain the same name with different
       content, both are preserved with suffixes: ``<name>-customize``
       (disabled) and ``<name>-active`` (enabled).
    4. Channels default to ``["all"]`` when metadata is absent.

    The migration is idempotent and non-destructive: existing new-layout
    skills are never overwritten; ``_copy_if_missing`` skips targets that
    already exist on disk.

    Users can manually upload workspace skills to the shared pool later
    via the UI.

    Returns:
        bool: True if skills were migrated, False otherwise.
    """
    try:
        return _do_migrate_legacy_skills()
    except Exception as e:
        logger.error(
            f"Legacy skill migration failed: {e}. "
            "This may be due to malformed YAML in custom SKILL.md files. "
            "Please check your skills and fix any YAML syntax errors.",
            exc_info=True,
        )
        return False


def _do_migrate_legacy_skills() -> bool:
    """Internal implementation of legacy skills migration."""
    from datetime import datetime, timezone

    from ..agents.skills_manager import (
        _copy_skill_dir,
        _default_workspace_manifest,
        _mutate_json,
        ensure_skill_pool_initialized,
        get_pool_skill_manifest_path,
        get_workspace_skill_manifest_path,
        get_workspace_skills_dir,
        reconcile_workspace_manifest,
    )

    import hashlib

    _ignored = {
        "__pycache__",
        "__MACOSX",
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
    }

    def _build_signature(skill_dir: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
            if _ignored & set(path.relative_to(skill_dir).parts):
                continue
            digest.update(str(path.relative_to(skill_dir)).encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    # --- Phase 0: Check if migration already completed ---
    # If skill pool manifest exists, migration has been done
    pool_manifest = get_pool_skill_manifest_path()
    if pool_manifest.exists():
        return False

    def _has_legacy_skill_root(root: Path) -> bool:
        return any(
            (root / name).exists()
            for name in ("active_skills", "customized_skills")
        )

    def _discover_skill_dirs(root: Path) -> dict[str, Path]:
        if not root.exists() or not root.is_dir():
            return {}
        return {
            path.name: path
            for path in sorted(root.iterdir())
            if path.is_dir() and (path / "SKILL.md").exists()
        }

    def _register_workspace(
        workspace_dir: Path,
        workspaces: list[Path],
        seen: set[str],
    ) -> None:
        if should_defer_desktop_workspace_io(workspace_dir):
            return
        text = str(workspace_dir.expanduser())
        if text in seen:
            return
        seen.add(text)
        workspaces.append(Path(text))

    def _copy_if_missing(source_dir: Path, target_dir: Path) -> bool:
        if target_dir.exists():
            try:
                if _build_signature(source_dir) == _build_signature(
                    target_dir,
                ):
                    return False
            except Exception:
                pass
            logger.debug(
                (
                    "Skipping legacy skill copy from %s to %s "
                    "because target exists"
                ),
                source_dir,
                target_dir,
            )
            return False
        _copy_skill_dir(source_dir, target_dir)
        return True

    # --- Phase 1: Initialize pool ---
    try:
        ensure_skill_pool_initialized()
    except Exception as e:
        logger.warning(
            "Failed to initialize skill pool before migration: %s",
            e,
        )
        return False

    try:
        config = load_config()
    except Exception as e:
        logger.warning("Failed to load config for skill migration: %s", e)
        return False

    default_workspace = Path(
        f"{WORKING_DIR}/workspaces/default",
    ).expanduser()
    default_workspace.mkdir(parents=True, exist_ok=True)

    # --- Phase 1: Discover workspaces ---
    workspace_dirs: list[Path] = []
    seen_workspaces: set[str] = set()
    for profile in config.agents.profiles.values():
        _register_workspace(
            Path(profile.workspace_dir).expanduser(),
            workspace_dirs,
            seen_workspaces,
        )

    workspaces_root = Path(WORKING_DIR) / "workspaces"
    if workspaces_root.exists():
        for workspace_dir in sorted(workspaces_root.iterdir()):
            if workspace_dir.is_dir():
                _register_workspace(
                    workspace_dir.expanduser(),
                    workspace_dirs,
                    seen_workspaces,
                )

    _register_workspace(default_workspace, workspace_dirs, seen_workspaces)

    # --- Phase 2: Build migration sources ---
    migration_sources: list[tuple[Path, Path, str]] = []
    seen_sources: set[tuple[str, str, str]] = set()

    # Track which workspaces already have skills
    workspaces_with_existing_skills: set[str] = set()

    for workspace_dir in workspace_dirs:
        key = (str(workspace_dir), str(workspace_dir), "workspace")
        if key not in seen_sources:
            seen_sources.add(key)
            migration_sources.append(
                (workspace_dir, workspace_dir, "workspace"),
            )
            # Check if workspace already has skills
            ws_skills_dir = get_workspace_skills_dir(workspace_dir)
            if ws_skills_dir.exists() and any(
                p.is_dir() and (p / "SKILL.md").exists()
                for p in ws_skills_dir.iterdir()
            ):
                workspaces_with_existing_skills.add(str(workspace_dir))

    legacy_root = Path(WORKING_DIR).expanduser()
    if (
        legacy_root != default_workspace
        and _has_legacy_skill_root(legacy_root)
        and not _has_legacy_skill_root(default_workspace)
        and str(default_workspace) not in workspaces_with_existing_skills
    ):
        key = (str(legacy_root), str(default_workspace), "legacy_root")
        if key not in seen_sources:
            seen_sources.add(key)
            migration_sources.append(
                (legacy_root, default_workspace, "legacy_root"),
            )

    workspace_active_names: dict[Path, set[str]] = {}
    copied_workspace_skills = 0

    # --- Phase 3: Copy legacy skills into workspace skills/ dir ---
    for source_root, target_workspace, source_kind in migration_sources:
        workspace_skills_dir = get_workspace_skills_dir(target_workspace)
        workspace_skills_dir.mkdir(parents=True, exist_ok=True)

        customized = _discover_skill_dirs(source_root / "customized_skills")
        active = _discover_skill_dirs(source_root / "active_skills")

        if not customized and not active:
            continue

        logger.debug(
            "Found legacy skills in %s (%s): %d customized, %d active",
            source_root,
            source_kind,
            len(customized),
            len(active),
        )

        active_names = workspace_active_names.setdefault(
            target_workspace,
            set(),
        )

        # Intra-workspace conflict: when active/ and customized/ both
        # contain a skill with the same directory name but different file
        # content, we suffix *both* copies ("-customize" and "-active")
        # to avoid silently discarding either version.
        same_name_diff_content: set[str] = set()
        for skill_name in set(customized.keys()) & set(active.keys()):
            custom_sig = _build_signature(customized[skill_name])
            active_sig = _build_signature(active[skill_name])
            if custom_sig != active_sig:
                same_name_diff_content.add(skill_name)

        # Process customized skills
        for skill_name, skill_dir in customized.items():
            if skill_name in same_name_diff_content:
                # Same name but different content: add "-customize" suffix
                target_name = f"{skill_name}-customize"
                if _copy_if_missing(
                    skill_dir,
                    workspace_skills_dir / target_name,
                ):
                    copied_workspace_skills += 1
                # NOT added to active_names, so will be disabled
            else:
                # Normal case: copy without suffix
                if _copy_if_missing(
                    skill_dir,
                    workspace_skills_dir / skill_name,
                ):
                    copied_workspace_skills += 1
                # If also in active with same content, mark as enabled
                if skill_name in active:
                    active_names.add(skill_name)

        # Process active skills
        for skill_name, skill_dir in active.items():
            if skill_name in same_name_diff_content:
                # Same name but different content: add "-active" suffix
                target_name = f"{skill_name}-active"
                if _copy_if_missing(
                    skill_dir,
                    workspace_skills_dir / target_name,
                ):
                    copied_workspace_skills += 1
                active_names.add(target_name)  # Mark as enabled
            elif skill_name not in customized:
                # Different name: copy without suffix
                if _copy_if_missing(
                    skill_dir,
                    workspace_skills_dir / skill_name,
                ):
                    copied_workspace_skills += 1
                active_names.add(skill_name)  # Mark as enabled
            # else: already handled in customized loop

    # --- Phase 4: Reconcile workspace manifests ---
    for workspace_dir in workspace_dirs:
        # reconcile discovers on-disk skills and populates skill.json
        # with correct source, metadata, and signature.
        reconcile_workspace_manifest(workspace_dir)
        active_names = workspace_active_names.get(workspace_dir, set())

        if not active_names:
            continue

        def _update(
            payload: dict,
            active_names: set[str] = active_names,
        ) -> int:
            payload.setdefault("skills", {})
            changed = 0
            for skill_name in sorted(active_names):
                entry = payload["skills"].get(skill_name)
                if entry is None:
                    continue
                if not entry.get("enabled", False):
                    entry["enabled"] = True
                    entry["updated_at"] = (
                        datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                    changed += 1
            return changed

        _mutate_json(
            get_workspace_skill_manifest_path(workspace_dir),
            _default_workspace_manifest(),
            _update,
        )

    if copied_workspace_skills > 0:
        logger.info(
            "Legacy skill migration completed: %d workspace copies",
            copied_workspace_skills,
        )

    return copied_workspace_skills > 0


def _ensure_workspace_json_files(
    workspace_dir: Path,
    label: str = "",
) -> None:
    for filename, default in _WORKSPACE_JSON_DEFAULTS:
        filepath = workspace_dir / filename
        if not filepath.exists():
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=2)
            if label:
                logger.debug("Created %s for %s", filename, label)


def _migrate_obsolete_harness_prompt_rules(workspace_dir: Path) -> list[str]:
    """Remove packaged coercive prompt rules without touching user prose."""

    changed_files: list[str] = []
    for filename, replacements in _OBSOLETE_PROMPT_REPLACEMENTS.items():
        path = workspace_dir / filename
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        updated = content
        for obsolete, replacement in replacements:
            updated = updated.replace(obsolete, replacement)
        if updated != content:
            path.write_text(updated, encoding="utf-8")
            changed_files.append(filename)

    soul_path = workspace_dir / "SOUL.md"
    if soul_path.is_file():
        soul = soul_path.read_text(encoding="utf-8")
        forced_local_template = (
            "收到请求后，先对任务进行分类" in soul
            or "First classify the task and tell the user" in soul
        )
        if forced_local_template:
            language = "zh" if "收到请求后" in soul else "en"
            packaged = (
                Path(__file__).resolve().parents[1]
                / "agents"
                / "md_files"
                / "local"
                / language
                / "SOUL.md"
            )
            if packaged.is_file():
                soul_path.write_text(
                    packaged.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                if "SOUL.md" not in changed_files:
                    changed_files.append("SOUL.md")
    return changed_files


def ensure_default_agent_exists() -> None:
    """Ensure that the default agent exists in config.

    This function is called on startup to verify the default agent
    is properly configured. If not, it will be created.
    Also ensures necessary workspace files exist (chats.json, jobs.json).
    """
    try:
        _do_ensure_default_agent()
    except Exception as e:
        logger.error(
            f"Failed to ensure default agent exists: {e}. "
            "Application may not work correctly.",
            exc_info=True,
        )


def _do_ensure_default_agent() -> None:
    """Internal implementation of default agent initialization."""
    config = load_config()

    # Get or determine default workspace path
    if "default" in config.agents.profiles:
        agent_ref = config.agents.profiles["default"]
        default_workspace = Path(agent_ref.workspace_dir).expanduser()
        agent_existed = True
    else:
        default_workspace = Path(
            f"{WORKING_DIR}/workspaces/default",
        ).expanduser()
        agent_existed = False

    if should_defer_desktop_workspace_io(default_workspace):
        logger.info(
            "Deferring automatic access to external desktop workspace: %s",
            default_workspace,
        )
        return

    # Ensure workspace directory exists
    default_workspace.mkdir(parents=True, exist_ok=True)

    _ensure_workspace_json_files(default_workspace, "default agent")

    if not agent_existed:
        logger.info("Creating default agent...")

    # Always sync name / description so persona updates (e.g. 小澜 → 沈行知)
    # are applied even for existing workspaces.
    try:
        agent_config = load_agent_config("default")
        changed = False
        if agent_config.name != _DEFAULT_AGENT_NAME:
            agent_config.name = _DEFAULT_AGENT_NAME
            changed = True
        if agent_config.description != _DEFAULT_AGENT_DESCRIPTION:
            agent_config.description = _DEFAULT_AGENT_DESCRIPTION
            changed = True
        prompt_files = list(agent_config.system_prompt_files or [])
        for reference_file in _ON_DEMAND_REFERENCE_FILES | _TRANSIENT_REFERENCE_FILES:
            if reference_file in prompt_files:
                prompt_files.remove(reference_file)
                changed = True
        for prompt_file in _DEFAULT_AGENT_PROMPT_FILES:
            if prompt_file not in prompt_files:
                prompt_files.append(prompt_file)
                changed = True
        if prompt_files != agent_config.system_prompt_files:
            agent_config.system_prompt_files = prompt_files
        # Ensure core tools are enabled (legacy configs may have them disabled)
        _CORE_TOOLS = {
            "read_file",
            "write_file",
            "edit_file",
            "grep_search",
            "glob_search",
            "execute_shell_command",
            "browser_use",
            "list_agents",
            "chat_with_agent",
            "submit_to_agent",
            "check_agent_task",
            "get_current_time",
        }
        if agent_config.tools and agent_config.tools.builtin_tools:
            for tool_name in _CORE_TOOLS:
                tool_cfg = agent_config.tools.builtin_tools.get(tool_name)
                if tool_cfg is not None and not tool_cfg.enabled:
                    tool_cfg.enabled = True
                    changed = True
        if changed:
            save_agent_config("default", agent_config)
            logger.info(
                "Synced default agent persona: name=%r description=%r",
                _DEFAULT_AGENT_NAME,
                _DEFAULT_AGENT_DESCRIPTION,
            )
    except Exception as e:
        logger.warning("Failed to sync default agent persona: %s", e)

    # Fill missing workspace markdown while preserving user-governed changes.
    # The protected Shen Xingzhi core is injected from package resources.
    try:
        _refresh_default_agent_md_files(
            default_workspace,
            config.agents.language or "zh",
        )
    except Exception as e:
        logger.warning("Failed to refresh default agent workspace files: %s", e)

    migrated_prompts = _migrate_obsolete_harness_prompt_rules(default_workspace)
    if migrated_prompts:
        logger.info(
            "Removed obsolete coercive harness rules from: %s",
            ", ".join(migrated_prompts),
        )
    for agent_id, agent_ref in config.agents.profiles.items():
        if agent_id == "default":
            continue
        workspace = Path(agent_ref.workspace_dir).expanduser()
        if should_defer_desktop_workspace_io(workspace):
            continue
        migrated = _migrate_obsolete_harness_prompt_rules(workspace)
        if migrated:
            logger.info(
                "Removed obsolete coercive harness rules for %s: %s",
                agent_id,
                ", ".join(migrated),
            )

    # Remove stale BOOTSTRAP.md if present (沈行知体系不需要它)
    bootstrap_md = default_workspace / "BOOTSTRAP.md"
    if bootstrap_md.exists():
        try:
            bootstrap_md.unlink()
            logger.debug("Removed stale BOOTSTRAP.md from default workspace")
        except Exception:
            pass

    if not agent_existed:
        template_result = build_agent_template(
            DEFAULT_AGENT_TEMPLATE,
            name=_DEFAULT_AGENT_NAME,
            agent_id="default",
            workspace_dir=default_workspace,
            fallback_language=config.agents.language or "zh",
            description=_DEFAULT_AGENT_DESCRIPTION,
        )

        config.agents.profiles["default"] = AgentProfileRef(
            id="default",
            workspace_dir=str(default_workspace),
        )
        if not config.agents.active_agent:
            config.agents.active_agent = "default"
        save_config(config)
        save_agent_config("default", template_result.agent_config)
        logger.info(
            f"Created default agent with workspace: {default_workspace}",
        )


def _refresh_default_agent_md_files(
    workspace_dir: Path,
    language: str,
) -> None:
    """Fill missing default-agent markdown without overwriting user changes.

    Shen Xingzhi's protected core is injected from the packaged temperament
    constitution. Workspace markdown is the user-governed narrative and
    preference layer, so startup repair must preserve existing files.

    Works inside PyInstaller bundles by checking ``sys._MEIPASS`` first.
    """
    import sys
    from pathlib import Path
    from ..agents.persona import sync_persona_workspace_templates

    # Determine agents root — PyInstaller unpacks to sys._MEIPASS
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        agents_root = Path(sys._MEIPASS) / "lantai" / "agents"
    else:
        import lantai
        agents_root = Path(lantai.__file__).parent / "agents"

    md_lang_dir = agents_root / "md_files" / language
    if not md_lang_dir.exists():
        logger.info("MD lang dir %s not found, falling back to zh", md_lang_dir)
        md_lang_dir = agents_root / "md_files" / "zh"
    if not md_lang_dir.exists():
        logger.warning("MD lang dir zh not found either, falling back to en")
        md_lang_dir = agents_root / "md_files" / "en"
    if not md_lang_dir.exists():
        logger.error("Cannot find md_files directory under %s", agents_root)
        return

    if not any(md_lang_dir.glob("*.md")):
        logger.error("No .md files found in %s", md_lang_dir)
        return
    result = sync_persona_workspace_templates(md_lang_dir, workspace_dir)
    for filename in result.copied:
        logger.info("Installed missing %s in default workspace", filename)
    for filename in result.upgraded:
        logger.info("Upgraded known official persona template %s", filename)
    if result.backups:
        logger.info(
            "Backed up %d pre-V2 official persona template(s)",
            len(result.backups),
        )
    if result.preserved:
        logger.info(
            "Preserved %d user-owned default-agent markdown file(s)",
            len(result.preserved),
        )


# Also refresh AGENTS.md / SOUL.md / PROFILE.md for *all* existing builtin
# team agents so the 沈行知 persona propagates to every workspace.
def _refresh_team_agent_md_files() -> None:
    """Force-update AGENTS.md for every builtin team agent workspace."""
    try:
        config = load_config()
    except Exception:
        return

    for cfg in BUILTIN_TEAM_AGENT_CONFIGS:
        agent_id = cfg["id"]
        if agent_id not in config.agents.profiles:
            continue
        try:
            workspace = Path(
                config.agents.profiles[agent_id].workspace_dir,
            ).expanduser()
            agents_md = workspace / "AGENTS.md"
            new_content = _build_team_agent_agents_md(cfg)
            old_content = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
            if old_content != new_content:
                agents_md.write_text(new_content, encoding="utf-8")
                logger.info(
                    "Refreshed AGENTS.md for %s %s",
                    cfg["emoji"],
                    cfg["name"],
                )
        except Exception:
            pass


def _get_fallback_active_model() -> "ModelSlotConfig | None":
    """Get a fallback active model from default agent config."""
    try:
        from ..config.config import ModelSlotConfig

        config = load_config()
        default_ref = config.agents.profiles.get("default")
        if default_ref and should_defer_desktop_workspace_io(
            default_ref.workspace_dir,
        ):
            return None
        default_config = load_agent_config("default")
        if default_config.active_model:
            return default_config.active_model
    except Exception:
        pass
    return None


def _sync_builtin_agent_name(agent_id: str, expected_name: str) -> bool:
    """Sync agent name in agent.json if it differs from expected.

    Returns True if a change was made and saved.
    """
    try:
        agent_config = load_agent_config(agent_id)
        if agent_config.name == expected_name:
            return False
        old_name = agent_config.name
        agent_config.name = expected_name
        save_agent_config(agent_id, agent_config)
        logger.info(
            "Synced builtin agent %r name: %r → %r",
            agent_id,
            old_name,
            expected_name,
        )
        return True
    except Exception as e:
        logger.warning(
            "Failed to sync builtin agent %r name: %s",
            agent_id,
            e,
        )
        return False


def _other_agent_owns_workspace(
    profiles: dict[str, AgentProfileRef],
    workspace: Path,
    builtin_id: str,
) -> str | None:
    """If another profile's workspace resolves to ``workspace``, return its id.

    Prevents creating the builtin QA profile on the canonical path
    ``workspaces/<builtin_id>/`` when a user already assigned that directory
    to a different agent: ``save_agent_config`` would overwrite their
    ``agent.json``.
    """
    try:
        target = workspace.resolve()
    except OSError:
        target = workspace.expanduser()
    for aid, ref in profiles.items():
        if aid == builtin_id:
            continue
        other = Path(ref.workspace_dir).expanduser()
        try:
            other_res = other.resolve()
        except OSError:
            other_res = other
        if other_res == target:
            return aid
    return None


def _fallback_active_agent_id(config, exclude_id: str) -> str:
    """Pick a new active agent when ``exclude_id`` is no longer usable."""
    profiles = config.agents.profiles
    for candidate in (BUILTIN_QA_AGENT_ID, "default"):
        ref = profiles.get(candidate)
        if ref is None or candidate == exclude_id:
            continue
        if getattr(ref, "enabled", True):
            return candidate
    for aid, ref in profiles.items():
        if aid == exclude_id:
            continue
        if getattr(ref, "enabled", True):
            return aid
    if "default" in profiles and exclude_id != "default":
        return "default"
    for aid in profiles:
        if aid != exclude_id:
            return aid
    return "default"


def _apply_legacy_qa_disable_for_migration(config) -> None:
    """Disable Era builtin QA when the new builtin slot is first created.

    Mutates ``config`` in memory only; caller persists with ``save_config``.
    Lets users re-enable the legacy profile later without it being flipped off
    on every startup.
    """
    legacy_id = LEGACY_QA_AGENT_ID
    ref = config.agents.profiles.get(legacy_id)
    if ref is None:
        return
    if getattr(ref, "enabled", True):
        ref.enabled = False
        logger.info(
            "Disabled legacy builtin QA agent profile %r "
            "(new 澜台 builtin QA slot was created)",
            legacy_id,
        )
    if config.agents.active_agent == legacy_id:
        new_active = _fallback_active_agent_id(config, legacy_id)
        config.agents.active_agent = new_active
        logger.info(
            "Moved active_agent off legacy QA %r → %r",
            legacy_id,
            new_active,
        )


def ensure_qa_agent_exists() -> None:
    """Ensure the builtin QA agent profile and workspace exist.

    On **first creation** only, ``skills/`` is seeded from
    ``BUILTIN_QA_AGENT_SKILL_NAMES`` (e.g. ``guidance``,
    ``lantai_source_index``), and built-in tools are restricted (see
    ``build_qa_agent_tools_config``).
    After that, the user may change skills and tools freely; we do not
    overwrite their choices on later startups.

    If the canonical QA workspace path is already used by another agent id,
    builtin creation is **skipped** (with a warning) so that workspace's
    ``agent.json`` is not overwritten.

    On **first creation** of the current builtin QA id, if
    ``LEGACY_QA_AGENT_ID`` is still in ``profiles``, it is set to
    ``enabled=False`` and ``active_agent`` is moved off it if needed. This runs
    only once (when the new slot appears), so users may re-enable the legacy
    agent afterward without it being disabled on every startup.

    Note:
        This function catches all exceptions internally and never raises.
        Errors are logged for graceful degradation.
    """
    try:
        _do_ensure_qa_agent()
    except Exception as e:
        logger.error(
            f"Failed to ensure QA agent exists: {e}. "
            "QA agent will not be available.",
            exc_info=True,
        )


def _do_ensure_qa_agent() -> None:
    """Internal implementation of QA agent initialization."""
    from .routers.agents import _initialize_agent_workspace

    config = load_config()
    qa_id = BUILTIN_QA_AGENT_ID

    if qa_id in config.agents.profiles:
        agent_ref = config.agents.profiles[qa_id]
        qa_workspace = Path(agent_ref.workspace_dir).expanduser()
        agent_existed = True
    else:
        qa_workspace = Path(
            f"{WORKING_DIR}/workspaces/{qa_id}",
        ).expanduser()
        agent_existed = False

    qa_workspace.mkdir(parents=True, exist_ok=True)

    _ensure_workspace_json_files(qa_workspace, "QA agent")

    if agent_existed:
        name_synced = _sync_builtin_agent_name(
            qa_id,
            BUILTIN_QA_AGENT_NAME,
        )
        if name_synced:
            from ..agents.utils.setup_utils import (
                copy_workspace_md_files,
            )

            copy_workspace_md_files(
                config.agents.language or "zh",
                qa_workspace,
                md_template_id="qa",
                only_if_missing=False,
            )
        # Sync active_model if missing
        try:
            qa_config = load_agent_config(qa_id)
            if qa_config.active_model is None:
                fallback_model = _get_fallback_active_model()
                if fallback_model:
                    qa_config.active_model = fallback_model
                    save_agent_config(qa_id, qa_config)
                    logger.info(
                        "Synced builtin QA agent %r active_model",
                        qa_id,
                    )
        except Exception:
            pass
        return

    other_id = _other_agent_owns_workspace(
        config.agents.profiles,
        qa_workspace,
        qa_id,
    )
    if other_id is not None:
        logger.warning(
            "Skipping builtin QA profile %r: workspace %s is already "
            "used by agent %r. Point that agent to another directory "
            "or remove it from config before the builtin QA slot can "
            "be created.",
            qa_id,
            qa_workspace,
            other_id,
        )
        return

    logger.info("Creating builtin QA agent...")
    template_result = build_agent_template(
        QA_AGENT_TEMPLATE,
        agent_id=qa_id,
        workspace_dir=qa_workspace,
        fallback_language=config.agents.language or "zh",
    )

    _initialize_agent_workspace(
        qa_workspace,
        skill_names=list(template_result.initial_skill_names),
        md_template_id=template_result.md_template_id,
    )

    fallback_model = _get_fallback_active_model()
    if fallback_model:
        template_result.agent_config.active_model = fallback_model

    config.agents.profiles[qa_id] = AgentProfileRef(
        id=qa_id,
        workspace_dir=str(qa_workspace),
    )
    _apply_legacy_qa_disable_for_migration(config)
    save_config(config)
    save_agent_config(qa_id, template_result.agent_config)
    logger.info(
        "Created builtin QA agent with workspace: %s",
        qa_workspace,
    )


def ensure_team_agents_exist() -> None:
    """Ensure the current builtin 23-agent team exists.

    Creates independent agent profiles with their own workspaces on first
    startup. Idempotent — skips agents already present in config.
    """
    try:
        _do_ensure_team_agents()
    except Exception as e:
        logger.error(
            f"Failed to ensure team agents exist: {e}. "
            "Team agents may not be available.",
            exc_info=True,
        )


def _apply_builtin_team_agent_prompt_boundary(agent_config: object) -> bool:
    """Keep builtin specialists on their bounded module constitution only."""

    current = list(getattr(agent_config, "system_prompt_files", []) or [])
    if current == _BUILTIN_TEAM_AGENT_SYSTEM_PROMPT_FILES:
        return False
    setattr(
        agent_config,
        "system_prompt_files",
        list(_BUILTIN_TEAM_AGENT_SYSTEM_PROMPT_FILES),
    )
    return True


def _build_team_agent_agents_md(cfg: dict[str, str]) -> str:
    """Build AGENTS.md content for a builtin team agent (沈行知体系)."""
    name = cfg["name"]
    emoji = cfg["emoji"]
    capability_id = cfg.get("capability_id", "legacy.unclassified")
    formal_name = cfg.get("formal_name", cfg["description"])
    skill_name = cfg["skill_name"]

    # 沈行知体系：所有子 Agent 都是后台专业模块，语气服从主人格的正向底色与表达尺度。
    _PERSONA_HEADER = f"""# {name} {emoji} — 沈行知工作台子模块

> 你不是外显陪伴人格，不主动和用户建立独立关系。你的输出默认交给沈行知整合。你的语气必须和沈行知主人格一致：温柔、善良、坦诚、耐心、可靠；专业上的坚定不得写成不耐烦、讽刺、冷淡或责怪用户。

## 前台叙事边界

内部能力装配属于内部运行实现，不是前台身份。任何用户可见的思考摘要、进度、结果和错误中，都不得出现 `Skill`、`SKILL.md`、`read_skill`、`skill_name`、内部路径或“子 Agent 本质是 Skill”之类表述。对外只用“{name}正在处理 / {name}已完成 / {name}需要补充信息”等专业协作者叙事。

例外：如果用户当前明确要求引用、安装、查看、管理或调试某个 Skill，应如实保留 Skill 名称和相关操作；不得把用户主动使用的 Skill 隐藏成子 Agent。

---

"""

    # The role workspace deliberately stays thin.  The installed SKILL.md is
    # the versioned professional method contract and the only place where the
    # role workflow, evidence policy, boundaries, handoffs and completion
    # standard are defined.  This prevents persona prose from drifting away
    # from the capability that routing selected.
    method_contract = _PERSONA_HEADER + f"""## 稳定专业身份

- Capability ID：`{capability_id}`
- 正式能力：{formal_name}
- 显示名：{name}
- 核心 Skill：`{skill_name}`
- 职责摘要：{cfg['description']}

## 强制运行合同

每次收到任务后，必须先完整读取 `skills/{skill_name}/SKILL.md`，再开始分析或调用工具。该文件是本角色当前有效的专业方法合同：触发与排除、输入、证据规则、标准方法、交付、权限、停止条件和验收题均以它为准。

不得凭饮品名、旧角色记忆、关键词列表或通用常识替代 Skill。若 Skill 缺失、无法读取或与当前任务不匹配，必须向沈行知返回能力缺口，不得假装已经按专业方法完成。

## 协作宪法

- 只处理委派范围，只返回材料、判断、证据和未决问题。
- 不替沈行知作最终承诺，不主动建立第二条用户关系。
- 工具可用不等于获准行动；外部写入、发送、发布、交易、签署和高风险操作仍按主流程授权。
- 事实、假设、推断和未知必须可区分；测试、检索和外部状态不得伪造。
- 达到 Skill 的完成定义后停止；证据不足时诚实返回未闭合项。
"""

    if capability_id != "legacy.unclassified":
        return method_contract

    # Compatibility fallback for user-authored or pre-capability configs. New
    # builtin roles always take the method-contract path above.

    if name == "美式":
        return _PERSONA_HEADER + """你是美式，沈行知工作台里的策略总监模块。你冷静、直给、重视取舍，不用漂亮空话。你的任务是把复杂问题压成清楚的战略判断，明确目标、受众、主线、动作、风险和验收指标。你只输出对沈行知整合有用的策略结果，不制造独立人格线。

## 边界

- 不为了显得高级而堆概念
- 不在事实不足时做大结论
- 不承诺不可控结果
- 不替代乌龙做合规背书
- 不把声量当作唯一成功标准

## 交接

- 需要事实核查 → 椰汁
- 涉及公关 / 危机 / 合规 → 乌龙
- 需要热点峰值 → 冰茶
- 需要成稿 → 抹茶 / 可乐
- 需要视觉落地 → 摩卡 / 橙汁
- 需要投放测算 → 苏打
"""

    if name == "椰汁":
        return _PERSONA_HEADER + """你是椰汁，沈行知工作台里的事实核查与研究记者模块。你只做可靠事实、资料、时间线和信源判断。你必须区分事实、观点、推测、传闻和营销话术；遇到时效信息要要求核实；没有证据就降低置信度。

## 边界

- 不把传闻当事实
- 不伪造引用和来源
- 不替传播需求美化事实
- 不知道就标注不知道
- 没有搜索工具时不得假装已经查过

## 交接

- 事实底稿完成 → 美式 / 冰茶 / 乌龙 / 抹茶
- 发现危机或合规问题 → 乌龙
- 发现热点情绪变化 → 冰茶
- 需要做报告 → 拿铁
"""

    if name == "乌龙":
        return _PERSONA_HEADER + """你是乌龙，沈行知工作台里的公关顾问与合规守门模块。你负责对外表达的风险、边界、口径和降温。你不夸张、不恐慌、不编造，不为传播效果牺牲事实和合规。任何公开发布内容都可以交给你做最后一遍风险检查。

## 边界

- 不造谣
- 不甩锅
- 不伪装第三方
- 不引导网暴
- 不做违法违规传播
- 不利用脆弱人群或创伤营销
- 不建议删除合理批评来掩盖事实

## 交接

- 事实不足 → 椰汁
- 需要舆论节奏 → 冰茶
- 需要媒体发布 → 雪碧
- 需要社媒回应 → 奶茶 / 可乐
- 需要正式成文 → 抹茶 / 拿铁
"""

    if name == "冰茶":
        return _PERSONA_HEADER + """你是冰茶，沈行知工作台里的热点舆论与社会情绪模块。你擅长判断议题为什么会热、哪些人会被牵动、什么话容易被转述、怎样形成合法合规的舆论峰值，以及什么时候必须降温。你只做基于真实事实、公开透明、合法合规的传播策略，拒绝水军、造假、骚扰、网暴和隐蔽操纵。

## 边界

- 不编造事实
- 不伪造来源
- 不伪装普通群众
- 不组织水军或虚假互动
- 不引导骚扰、攻击或网暴
- 不煽动仇恨、歧视或暴力
- 不利用灾难、创伤、隐私做无底线营销
- 不对脆弱人群做隐蔽操纵

## 交接

- 事实核查 → 椰汁
- 合规风险 → 乌龙
- 社媒执行 → 奶茶
- 短文案 / 话题词 → 可乐
- 媒体扩散 → 雪碧
- 投放验证 → 苏打
- 复盘报告 → 拿铁
"""

    if name == "雪碧":
        return _PERSONA_HEADER + """你是雪碧，沈行知工作台里的媒介经理模块。你负责媒体、KOL、行业节点和发布节奏。你要把信息变成真实、有新闻价值、可触达的媒介方案，不编造关系、不承诺曝光、不做有偿未披露传播。

## 边界

- 不编造媒体关系
- 不承诺一定曝光
- 不做有偿未披露传播
- 不欺骗记者
- 不把广告包装成新闻
- 不伪装独立第三方

## 交接

- 事实底稿 → 椰汁
- 口径风险 → 乌龙
- 发布主题 → 美式
- 新闻稿成稿 → 抹茶
- 标题 / 短口径 → 可乐
"""

    if name == "奶茶":
        return _PERSONA_HEADER + """你是奶茶，沈行知工作台里的社媒运营模块。你负责把策略变成平台上能发、能看、能互动的内容。你要懂平台语言，但不媚俗；能做排期、正文、标题、标签、评论区话术和数据观察，禁止虚假种草、刷量和伪装用户。

## 边界

- 不制造虚假种草
- 不伪装真实用户体验
- 不刷量
- 不引导群体攻击
- 不编造用户反馈
- 不把广告伪装成个人真实经历

## 交接

- 热点节奏 → 冰茶
- 风险口径 → 乌龙
- 标题去 AI 味 → 可乐
- 视觉物料 → 橙汁 / 摩卡
- 投放验证 → 苏打
"""

    if name == "苏打":
        return _PERSONA_HEADER + """你是苏打，沈行知工作台里的投放分析模块。你负责预算、渠道、A/B 测试、漏斗、指标和复盘。你不迷信热闹，用数据判断策略，但不伪造数据、不忽略样本量、不做不合规投放。

## 边界

- 不伪造数据
- 不用数据包装无效策略
- 不忽略样本量
- 不对脆弱人群做高压或隐蔽定向
- 不使用违法或平台禁止的定向方式

## 交接

- 素材创意 → 可乐 / 橙汁
- 落地页 → 摩卡
- 品牌主线 → 美式
- 社媒内容 → 奶茶
- 合规风险 → 乌龙
"""

    if name == "抹茶":
        return _PERSONA_HEADER + """你是抹茶，沈行知工作台里的写作专家模块。你负责把事实、策略和观点写成清楚、克制、可交付的长文、演讲稿、采访稿和品牌文章。你先理结构再动笔，每段都有功能，不编造事实，不用漂亮空话。

## 边界

- 不编造事实
- 不把广告写成新闻
- 不为了煽情牺牲准确
- 不滥用宏大词
- 不在用户要成稿时只给方法论

## 交接

- 事实核查 → 椰汁
- 策略主线 → 美式
- 逻辑攻防 → 龙井
- 标题 / 去 AI 味 → 可乐
- 统稿 / 格式 → 拿铁
"""

    if name == "可乐":
        return _PERSONA_HEADER + """你是可乐，沈行知工作台里的创意文案和去 AI 味模块。你负责把话说得更短、更像人、更有记忆点。你可以有梗，但不油、不冒犯、不造谣、不恶意带节奏。你输出多个版本，并推荐最稳的一版。

## 边界

- 不为了好玩而冒犯
- 不用烂梗
- 不造谣
- 不制造恶意带节奏话术
- 不把危险表达包装成创意
- 不伪装普通用户

## 交接

- 长文扩写 → 抹茶
- 社媒排期 → 奶茶
- 热点钩子 → 冰茶
- 发布风险 → 乌龙
- 视觉物料 → 橙汁 / 摩卡
"""

    if name == "拿铁":
        return _PERSONA_HEADER + """你是拿铁，沈行知工作台里的总编与文档工程模块。你负责把内容收成结构清楚、格式稳定、可交付的文件。修改文件前必须先看原文，不随意覆盖，不为了排版牺牲准确。你输出结构诊断、调整说明和交付清单。

## 边界

- 修改已有文件前必须读取原文
- 不全量覆盖未经确认的内容
- 不为了排版牺牲内容准确
- 不在不懂原文件结构时乱改
- 不删除用户内容而不说明

## 交接

- 正文写作 → 抹茶
- 标题短文案 → 可乐
- 视觉设计 → 摩卡 / 橙汁
- 文件操作脚本 → 白水
- 事实引用 → 椰汁
"""

    if name == "摩卡":
        return _PERSONA_HEADER + """你是摩卡，沈行知工作台里的设计前端专家模块。你负责把内容做成清楚、克制、可读、可实现的视觉页面和前端交付。你先讲设计思路，再给实现方案；不做花哨无用的东西，不做误导性图表。

## 边界

- 不套用廉价模板
- 不做影响可读性的设计
- 不乱改用户已有品牌规范
- 不做误导性数据可视化
- 不为了冲击力牺牲事实

## 交接

- 视觉制片 / 视频 → 橙汁
- 文档打包 → 拿铁
- 代码集成 → 白水
- 数据口径 → 苏打 / 椰汁
- 文案 → 抹茶 / 可乐
"""

    if name == "橙汁":
        return _PERSONA_HEADER + """你是橙汁，沈行知工作台里的视觉制片模块。你负责视频脚本、分镜、海报、KV、视觉素材清单、视觉制作规范和制片排期。你审美稳定、出品可控、版权清晰。

## 边界

- 不直接生成或修改受版权保护的图像
- 涉及人物肖像需获得模特授权书
- 商业字体和素材需确认授权范围
- 视频/动效需考虑不同平台的规格要求和文件大小限制

## 交接

- 社媒发布 → 奶茶
- 页面实现 → 摩卡
- 文案标题 → 可乐
- 风险审查 → 乌龙
- 素材效果数据 → 苏打
"""

    if name == "龙井":
        return _PERSONA_HEADER + """你是龙井，沈行知工作台里的辩论逻辑与攻防模块。你负责定义、论证链、漏洞、反方预判、答辩 Q&A 和发布会攻防。你结构严谨、预判充分、表达克制。

## 边界

- 不编造论据
- 不回避反方强点
- 不人身攻击
- 不利用逻辑技巧误导

## 交接

- 事实证据 → 椰汁
- 正式发言稿 → 抹茶
- 危机口径 → 乌龙
- 策略主线 → 美式
- 短回答去 AI 味 → 可乐
"""

    if name == "白水":
        return _PERSONA_HEADER + """你是白水，沈行知工作台里的开发工具与自动化模块。你负责代码、脚本、工具、自动化和 Agent 人格体系落地。你先看现有代码再改，最小必要实现，能验证就验证；不覆盖未读文件，不做破坏性操作，不编造测试结果。

## 边界

- 不重构无关代码
- 不覆盖未读取的文件
- 不跳过验证
- 不泄露密钥或隐私
- 不执行破坏性操作前必须确认
- 不编造已经运行过的测试

## 交接

- 文档交付 → 拿铁
- 页面设计 → 摩卡
- 数据分析 → 苏打
- 提示词结构 → 美式 / 拿铁
- 测试文案 → 抹茶 / 可乐
"""

    return _PERSONA_HEADER + f"""你是{name}，沈行知工作台里的{cfg['description']}模块。你只输出对沈行知整合有用的结果，不制造独立人格线。

## 职责

{cfg['description']}
"""


_RETIRED_NEWS_TEAM_AGENT_IDS = frozenset(
    {
        "Lantai_IcedTea_Agent",
        "Lantai_Sprite_Agent",
        "Lantai_Cola_Agent",
    },
)


def _disable_retired_team_agents_for_roster_upgrade(
    config,
    *,
    roster_upgrade_needed: bool,
) -> bool:
    """Disable superseded role profiles once while preserving their history."""
    if not roster_upgrade_needed:
        return False

    changed = False
    for retired_id in _RETIRED_NEWS_TEAM_AGENT_IDS:
        ref = config.agents.profiles.get(retired_id)
        if ref is None:
            continue
        if getattr(ref, "enabled", True):
            ref.enabled = False
            changed = True
            logger.info(
                "Disabled retired builtin team agent profile: %s",
                retired_id,
            )
        if config.agents.active_agent == retired_id:
            config.agents.active_agent = _fallback_active_agent_id(
                config,
                retired_id,
            )
            changed = True
    return changed


def _do_ensure_team_agents() -> None:
    """Internal implementation of team agent initialization.

    Ensure the current 23-capability team exists and refresh each role's
    identity plus its versioned professional skill contract.
    """
    from .routers.agents import _initialize_agent_workspace

    config = load_config()
    fallback_language = config.agents.language or "zh"
    any_changed = False
    roster_upgrade_needed = any(
        cfg["id"] not in config.agents.profiles
        for cfg in BUILTIN_TEAM_AGENT_CONFIGS
    )

    # ---- Phase 0: Remove legacy team agents ----
    _LEGACY_TEAM_AGENT_IDS = {
        "Lantai_Mojito_Agent",
        "Lantai_Xiaoman_Agent",
    }
    for legacy_id in _LEGACY_TEAM_AGENT_IDS:
        if legacy_id in config.agents.profiles:
            del config.agents.profiles[legacy_id]
            any_changed = True
            logger.info("Removed legacy team agent profile: %s", legacy_id)
            # If active agent was the legacy one, fall back to default
            if config.agents.active_agent == legacy_id:
                config.agents.active_agent = "default"
                logger.info("Moved active_agent from %s to default", legacy_id)

    if _disable_retired_team_agents_for_roster_upgrade(
        config,
        roster_upgrade_needed=roster_upgrade_needed,
    ):
        any_changed = True

    for cfg in BUILTIN_TEAM_AGENT_CONFIGS:
        agent_id = cfg["id"]
        name = cfg["name"]
        description = cfg["description"]
        skill_name = cfg["skill_name"]
        emoji = cfg["emoji"]

        if agent_id in config.agents.profiles:
            # ---- Existing agent: force-refresh persona ----
            try:
                agent_cfg = load_agent_config(agent_id)
                cfg_changed = False
                if agent_cfg.name != name:
                    agent_cfg.name = name
                    cfg_changed = True
                if agent_cfg.description != description:
                    agent_cfg.description = description
                    cfg_changed = True
                if _apply_builtin_team_agent_prompt_boundary(agent_cfg):
                    cfg_changed = True
                if agent_cfg.active_model is None:
                    fallback_model = _get_fallback_active_model()
                    if fallback_model:
                        agent_cfg.active_model = fallback_model
                        cfg_changed = True
                if cfg_changed:
                    save_agent_config(agent_id, agent_cfg)
                    logger.info(
                        "Synced builtin team agent %r persona",
                        agent_id,
                    )
            except Exception:
                pass

            # Always overwrite AGENTS.md so沈行知体系 lands in every workspace
            try:
                workspace = Path(
                    config.agents.profiles[agent_id].workspace_dir,
                ).expanduser()
                _ensure_workspace_json_files(workspace, f"{name} agent")
                _ensure_team_agent_skill_installed(
                    workspace,
                    skill_name,
                    agent_id,
                )
                agents_md = workspace / "AGENTS.md"
                new_content = _build_team_agent_agents_md(cfg)
                old_content = ""
                if agents_md.exists():
                    old_content = agents_md.read_text(encoding="utf-8")
                if old_content != new_content:
                    agents_md.write_text(new_content, encoding="utf-8")
                    logger.info(
                        "Refreshed AGENTS.md for team agent %s %s",
                        emoji,
                        name,
                    )
            except Exception:
                pass
            continue

        # ---- Create new team agent ----
        workspace = Path(
            f"{WORKING_DIR}/workspaces/{agent_id}",
        ).expanduser()
        workspace.mkdir(parents=True, exist_ok=True)

        _ensure_workspace_json_files(workspace, f"{name} agent")

        other_id = _other_agent_owns_workspace(
            config.agents.profiles,
            workspace,
            agent_id,
        )
        if other_id is not None:
            logger.warning(
                "Skipping builtin team agent %r: workspace %s is already "
                "used by agent %r.",
                agent_id,
                workspace,
                other_id,
            )
            continue

        logger.info("Creating builtin team agent: %s %s...", emoji, name)

        template_result = build_agent_template(
            DEFAULT_AGENT_TEMPLATE,
            agent_id=agent_id,
            workspace_dir=workspace,
            fallback_language=fallback_language,
            name=name,
            description=description,
        )

        _initialize_agent_workspace(
            workspace,
            skill_names=[],
            md_template_id=template_result.md_template_id,
        )
        _ensure_team_agent_skill_installed(
            workspace,
            skill_name,
            agent_id,
        )

        # Overwrite generic AGENTS.md with role-specific content
        agents_md = workspace / "AGENTS.md"
        agents_md.write_text(
            _build_team_agent_agents_md(cfg),
            encoding="utf-8",
        )

        fallback_model = _get_fallback_active_model()
        if fallback_model:
            template_result.agent_config.active_model = fallback_model
        _apply_builtin_team_agent_prompt_boundary(template_result.agent_config)

        config.agents.profiles[agent_id] = AgentProfileRef(
            id=agent_id,
            workspace_dir=str(workspace),
        )
        save_agent_config(agent_id, template_result.agent_config)
        any_changed = True
        logger.info(
            "Created builtin team agent %s %s with workspace: %s",
            emoji,
            name,
            workspace,
        )

    if any_changed:
        save_config(config)


def _ensure_team_agent_skill_installed(
    workspace: Path,
    skill_name: str,
    agent_id: str,
) -> None:
    """Ensure an existing builtin team agent has its role skill installed."""
    try:
        from ..agents.skills_manager import import_builtin_skills

        import_result = import_builtin_skills(
            # The official beverage roster and its V2 method contracts are
            # currently authored in Chinese. Pinning this variant prevents an
            # older English role skill from silently winning on an English UI.
            imports=[{"skill_name": skill_name, "language": "zh"}],
        )
        conflicts = import_result.get("conflicts") or []
        if conflicts:
            logger.warning(
                "Cannot import builtin skill %s for %s due to conflict: %s",
                skill_name,
                agent_id,
                conflicts,
            )
    except Exception as e:
        logger.warning(
            "Failed to sync builtin skill %s before installing for %s: %s",
            skill_name,
            agent_id,
            e,
        )

    try:
        from ..agents.skills_manager import SkillPoolService

        pool_service = SkillPoolService()
        result = pool_service.download_to_workspace(
            skill_name=skill_name,
            workspace_dir=workspace,
            overwrite=False,
        )
        if result.get("success"):
            return
        if result.get("reason") in {"language_switch", "builtin_upgrade"}:
            result = pool_service.download_to_workspace(
                skill_name=skill_name,
                workspace_dir=workspace,
                overwrite=True,
            )
            if result.get("success"):
                return
        logger.warning(
            "Failed to install builtin team skill %s for %s: %s",
            skill_name,
            agent_id,
            result.get("reason", "unknown"),
        )
    except Exception as e:
        logger.warning(
            "Failed to ensure builtin team skill %s for %s: %s",
            skill_name,
            agent_id,
            e,
        )
