# -*- coding: utf-8 -*-
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root before reading any env vars
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)


def _get_env(key: str, default: str = "") -> str:
    """Look up an env var with automatic COPAW_ legacy fallback.

    Primary key is always used as-is.  When the primary key starts with
    ``LANTAI_``, the corresponding ``COPAW_`` variant is transparently
    checked as a fallback so that existing deployments keep working.
    """
    if key in os.environ:
        return os.environ[key]
    if key.startswith("LANTAI_"):
        legacy_key = "COPAW_" + key[len("LANTAI_") :]
        if legacy_key in os.environ:
            return os.environ[legacy_key]
    return default


class EnvVarLoader:
    """Utility to load and parse environment variables with type safety
    and defaults.  Pass LANTAI_* keys; COPAW_* legacy variants are
    checked automatically as a fallback inside _get_env.
    """

    @staticmethod
    def get_bool(env_var: str, default: bool = False) -> bool:
        """Get a boolean environment variable,
        interpreting common truthy values."""
        val = _get_env(env_var, str(default)).lower()
        return val in ("true", "1", "yes")

    @staticmethod
    def get_float(
        env_var: str,
        default: float = 0.0,
        min_value: float | None = None,
        max_value: float | None = None,
        allow_inf: bool = False,
    ) -> float:
        """Get a float environment variable with optional bounds
        and infinity handling."""
        try:
            value = float(_get_env(env_var, str(default)))
            if min_value is not None and value < min_value:
                return min_value
            if max_value is not None and value > max_value:
                return max_value
            if not allow_inf and (
                value == float("inf") or value == float("-inf")
            ):
                return default
            return value
        except (TypeError, ValueError):
            return default

    @staticmethod
    def get_int(
        env_var: str,
        default: int = 0,
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> int:
        """Get an integer environment variable with optional bounds."""
        try:
            value = int(_get_env(env_var, str(default)))
            if min_value is not None and value < min_value:
                return min_value
            if max_value is not None and value > max_value:
                return max_value
            return value
        except (TypeError, ValueError):
            return default

    @staticmethod
    def get_str(env_var: str, default: str = "") -> str:
        """Get a string environment variable with a default fallback."""
        return _get_env(env_var, default)


# WORKING_DIR priority:
# 1. LANTAI_WORKING_DIR / COPAW_WORKING_DIR env var is set → use it
# 2. ~/.copaw exists (legacy installation) → use it as-is
# 3. Default → ~/.lantai
_explicit_working_dir = _get_env("LANTAI_WORKING_DIR")
if _explicit_working_dir:
    WORKING_DIR = Path(_explicit_working_dir).expanduser().resolve()
else:
    _legacy_copaw_dir = Path("~/.copaw").expanduser()
    if _legacy_copaw_dir.exists():
        WORKING_DIR = _legacy_copaw_dir.resolve()
    else:
        WORKING_DIR = Path("~/.lantai").expanduser().resolve()
SECRET_DIR = (
    Path(
        EnvVarLoader.get_str(
            "LANTAI_SECRET_DIR",
            f"{WORKING_DIR}.secret",
        ),
    )
    .expanduser()
    .resolve()
)

PROJECT_NAME = "澜台"

# Default media directory for channels (cross-platform)
DEFAULT_MEDIA_DIR = WORKING_DIR / "media"

# Default local provider directory
DEFAULT_LOCAL_PROVIDER_DIR = WORKING_DIR / "local_models"

JOBS_FILE = EnvVarLoader.get_str("LANTAI_JOBS_FILE", "jobs.json")

CHATS_FILE = EnvVarLoader.get_str("LANTAI_CHATS_FILE", "chats.json")


# Builtin Q&A helper profile.  agent_id keeps "Lantai" prefix for existing
# workspaces and agent.json; do not rename.
def _discover_agent_languages() -> frozenset[str]:
    md_root = Path(__file__).resolve().parent / "agents" / "md_files"
    if md_root.is_dir():
        langs = {
            d.name
            for d in md_root.iterdir()
            if d.is_dir()
            and not d.name.startswith(".")
            and any(d.glob("*.md"))
        }
        if langs:
            return frozenset(langs)
    return frozenset({"en", "zh", "ru"})


SUPPORTED_AGENT_LANGUAGES: frozenset[str] = _discover_agent_languages()

BUILTIN_QA_AGENT_ID = "Lantai_QA_Agent_0.2"
BUILTIN_QA_AGENT_NAME = "澜小问"
# Default skills when the builtin QA workspace is first created only.
BUILTIN_QA_AGENT_SKILL_NAMES: tuple[str, ...] = (
    "guidance",
    "QA_source_index",
)

# Builtin team agent IDs — independent agents for specialized roles.
# 沈行知 23 个饮品子 Agent
BUILTIN_AMERICANO_AGENT_ID = "Lantai_Americano_Agent"
BUILTIN_COCONUT_AGENT_ID = "Lantai_Coconut_Agent"
BUILTIN_OOLONG_AGENT_ID = "Lantai_Oolong_Agent"
BUILTIN_ICED_TEA_AGENT_ID = "Lantai_IcedTea_Agent"
BUILTIN_SPRITE_AGENT_ID = "Lantai_Sprite_Agent"
BUILTIN_MILK_TEA_AGENT_ID = "Lantai_MilkTea_Agent"
BUILTIN_SODA_AGENT_ID = "Lantai_Soda_Agent"
BUILTIN_MATCHA_AGENT_ID = "Lantai_Matcha_Agent"
BUILTIN_COLA_AGENT_ID = "Lantai_Cola_Agent"
BUILTIN_LATTE_AGENT_ID = "Lantai_Latte_Agent"
BUILTIN_MOCHA_AGENT_ID = "Lantai_Mocha_Agent"
BUILTIN_ORANGE_AGENT_ID = "Lantai_Orange_Agent"
BUILTIN_LONGJING_AGENT_ID = "Lantai_Longjing_Agent"
BUILTIN_WATER_AGENT_ID = "Lantai_Water_Agent"
BUILTIN_ESPRESSO_AGENT_ID = "Lantai_Espresso_Agent"
BUILTIN_BLACK_COFFEE_AGENT_ID = "Lantai_BlackCoffee_Agent"
BUILTIN_PUER_AGENT_ID = "Lantai_Puer_Agent"
BUILTIN_ROCK_TEA_AGENT_ID = "Lantai_RockTea_Agent"
BUILTIN_BLACK_TEA_AGENT_ID = "Lantai_BlackTea_Agent"
BUILTIN_COCOA_AGENT_ID = "Lantai_Cocoa_Agent"
BUILTIN_BARLEY_TEA_AGENT_ID = "Lantai_BarleyTea_Agent"
BUILTIN_SOY_MILK_AGENT_ID = "Lantai_SoyMilk_Agent"
BUILTIN_COLD_BREW_AGENT_ID = "Lantai_ColdBrew_Agent"
BUILTIN_WHITE_TEA_AGENT_ID = "Lantai_WhiteTea_Agent"
BUILTIN_WARM_WATER_AGENT_ID = "Lantai_WarmWater_Agent"
BUILTIN_CLEAR_TEA_AGENT_ID = "Lantai_ClearTea_Agent"

BUILTIN_TEAM_AGENT_CONFIGS: tuple[dict[str, str], ...] = (
    {
        "id": BUILTIN_COCONUT_AGENT_ID,
        "name": "椰汁",
        "description": "研究与证据 — 检索、来源比较、事实核查、综述与时间线",
        "skill_name": "coconut",
        "emoji": "🥥",
        "capability_id": "core.research",
        "formal_name": "研究与证据",
        "layer": "core",
    },
    {
        "id": BUILTIN_AMERICANO_AGENT_ID,
        "name": "美式",
        "description": "策略与决策 — 目标、约束、选项、取舍、情景与路线图",
        "skill_name": "americano",
        "emoji": "☕",
        "capability_id": "core.strategy",
        "formal_name": "策略与决策",
        "layer": "core",
    },
    {
        "id": BUILTIN_MATCHA_AGENT_ID,
        "name": "抹茶",
        "description": "写作与编辑 — 长短文、结构、改写、论证、语言与统稿",
        "skill_name": "matcha",
        "emoji": "🍵",
        "capability_id": "core.writing",
        "formal_name": "写作与编辑",
        "layer": "core",
    },
    {
        "id": BUILTIN_SODA_AGENT_ID,
        "name": "苏打",
        "description": "数据与量化 — 表格、SQL、统计、指标、实验与可视化",
        "skill_name": "soda",
        "emoji": "🫧",
        "capability_id": "core.data",
        "formal_name": "数据与量化",
        "layer": "core",
    },
    {
        "id": BUILTIN_WATER_AGENT_ID,
        "name": "白水",
        "description": "软件与自动化 — 编码、调试、测试、脚本、集成与工程诊断",
        "skill_name": "water",
        "emoji": "💧",
        "capability_id": "core.software",
        "formal_name": "软件与自动化",
        "layer": "core",
    },
    {
        "id": BUILTIN_MOCHA_AGENT_ID,
        "name": "摩卡",
        "description": "产品与体验 — 用户问题、需求、任务流、信息架构、交互与原型",
        "skill_name": "mocha",
        "emoji": "🍫",
        "capability_id": "core.product_experience",
        "formal_name": "产品与体验",
        "layer": "core",
    },
    {
        "id": BUILTIN_ORANGE_AGENT_ID,
        "name": "橙汁",
        "description": "视觉与创意 — 视觉方向、图像、视频、信息表达与制作规范",
        "skill_name": "orange",
        "emoji": "🍊",
        "capability_id": "core.visual_creative",
        "formal_name": "视觉与创意",
        "layer": "core",
    },
    {
        "id": BUILTIN_LATTE_AGENT_ID,
        "name": "拿铁",
        "description": "办公与文档 — Word、PPT、Excel、PDF、邮件与会议材料交付",
        "skill_name": "latte",
        "emoji": "🥛",
        "capability_id": "core.office",
        "formal_name": "办公与文档",
        "layer": "core",
    },
    {
        "id": BUILTIN_ESPRESSO_AGENT_ID,
        "name": "浓缩",
        "description": "项目与运营 — 计划、依赖、SOP、状态、执行跟踪与复盘",
        "skill_name": "espresso",
        "emoji": "☕",
        "capability_id": "core.project_ops",
        "formal_name": "项目与运营",
        "layer": "core",
    },
    {
        "id": BUILTIN_LONGJING_AGENT_ID,
        "name": "龙井",
        "description": "复核与质量 — 反方、漏洞、风险、验收与发布前独立检查",
        "skill_name": "longjing",
        "emoji": "🍃",
        "capability_id": "core.review",
        "formal_name": "复核与质量",
        "layer": "core",
    },
    {
        "id": BUILTIN_BLACK_COFFEE_AGENT_ID,
        "name": "黑咖",
        "description": "财务与会计 — 报表、预算、成本、现金流与经营分析",
        "skill_name": "black_coffee",
        "emoji": "☕",
        "capability_id": "domain.finance",
        "formal_name": "财务与会计",
        "layer": "domain",
    },
    {
        "id": BUILTIN_PUER_AGENT_ID,
        "name": "普洱",
        "description": "投资研究 — 公司、行业、估值、风险、情景与监控",
        "skill_name": "puer",
        "emoji": "🫖",
        "capability_id": "domain.investment",
        "formal_name": "投资研究",
        "layer": "domain",
    },
    {
        "id": BUILTIN_ROCK_TEA_AGENT_ID,
        "name": "岩茶",
        "description": "法律与合规 — 合同、法规、权利义务、风险与合规检查",
        "skill_name": "rock_tea",
        "emoji": "🪨",
        "capability_id": "domain.legal",
        "formal_name": "法律与合规",
        "layer": "domain",
    },
    {
        "id": BUILTIN_MILK_TEA_AGENT_ID,
        "name": "奶茶",
        "description": "市场与增长 — 定位、市场进入、漏斗、渠道、品牌与实验",
        "skill_name": "milk_tea",
        "emoji": "🧋",
        "capability_id": "domain.marketing",
        "formal_name": "市场与增长",
        "layer": "domain",
    },
    {
        "id": BUILTIN_BLACK_TEA_AGENT_ID,
        "name": "红茶",
        "description": "销售与客户 — 客户研究、商机、提案、客户成功与服务",
        "skill_name": "black_tea",
        "emoji": "🫖",
        "capability_id": "domain.sales_customer",
        "formal_name": "销售与客户",
        "layer": "domain",
    },
    {
        "id": BUILTIN_COCOA_AGENT_ID,
        "name": "可可",
        "description": "人才与组织 — 招聘、绩效、组织、制度与员工沟通",
        "skill_name": "cocoa",
        "emoji": "🍫",
        "capability_id": "domain.people_org",
        "formal_name": "人才与组织",
        "layer": "domain",
    },
    {
        "id": BUILTIN_BARLEY_TEA_AGENT_ID,
        "name": "麦茶",
        "description": "经营与供应链 — 采购、库存、产能、质量、交付与运营",
        "skill_name": "barley_tea",
        "emoji": "🌾",
        "capability_id": "domain.operations_supply",
        "formal_name": "经营与供应链",
        "layer": "domain",
    },
    {
        "id": BUILTIN_SOY_MILK_AGENT_ID,
        "name": "豆乳",
        "description": "学习与教育 — 诊断、课程、练习、反馈、测验与迁移",
        "skill_name": "soy_milk",
        "emoji": "🥛",
        "capability_id": "domain.learning",
        "formal_name": "学习与教育",
        "layer": "domain",
    },
    {
        "id": BUILTIN_COLD_BREW_AGENT_ID,
        "name": "冷萃",
        "description": "科学与工程 — 文献、实验、测量、计算、标准与复现",
        "skill_name": "cold_brew",
        "emoji": "🧊",
        "capability_id": "domain.science_engineering",
        "formal_name": "科学与工程",
        "layer": "domain",
    },
    {
        "id": BUILTIN_WHITE_TEA_AGENT_ID,
        "name": "白茶",
        "description": "政策与公共事务 — 政策文本、比较、影响与利益相关者",
        "skill_name": "white_tea",
        "emoji": "🍵",
        "capability_id": "domain.policy_public",
        "formal_name": "政策与公共事务",
        "layer": "domain",
    },
    {
        "id": BUILTIN_WARM_WATER_AGENT_ID,
        "name": "温水",
        "description": "健康信息 — 循证资料、解释、就诊准备与来源导航",
        "skill_name": "warm_water",
        "emoji": "♨️",
        "capability_id": "domain.health_info",
        "formal_name": "健康信息",
        "layer": "domain",
    },
    {
        "id": BUILTIN_CLEAR_TEA_AGENT_ID,
        "name": "清茶",
        "description": "新闻与编辑 — 选题、采访、信源、事实底稿与更正",
        "skill_name": "clear_tea",
        "emoji": "🍵",
        "capability_id": "domain.journalism_editorial",
        "formal_name": "新闻与编辑",
        "layer": "domain",
    },
    {
        "id": BUILTIN_OOLONG_AGENT_ID,
        "name": "乌龙",
        "description": "公关与传播 — 利益相关者、媒体关系、声明、发布与危机沟通",
        "skill_name": "oolong",
        "emoji": "🍂",
        "capability_id": "domain.public_relations",
        "formal_name": "公关与传播",
        "layer": "domain",
    },
)

# CoPaw-era builtin QA; may remain in config.json — disabled when the current
# ``BUILTIN_QA_AGENT_ID`` profile is first created (see ``migration``), not
# every startup, so users can re-enable this id if they want.
LEGACY_QA_AGENT_ID = "CoPaw_QA_Agent_0.1beta1"

TOKEN_USAGE_FILE = EnvVarLoader.get_str(
    "LANTAI_TOKEN_USAGE_FILE",
    "token_usage.json",
)

CONFIG_FILE = EnvVarLoader.get_str("LANTAI_CONFIG_FILE", "config.json")

HEARTBEAT_FILE = EnvVarLoader.get_str("LANTAI_HEARTBEAT_FILE", "HEARTBEAT.md")
HEARTBEAT_DEFAULT_EVERY = "6h"
HEARTBEAT_DEFAULT_TARGET = "main"
HEARTBEAT_TARGET_LAST = "last"

# Debug history file for /dump_history and /load_history commands
DEBUG_HISTORY_FILE = EnvVarLoader.get_str(
    "LANTAI_DEBUG_HISTORY_FILE",
    "debug_history.jsonl",
)
MAX_LOAD_HISTORY_COUNT = 10000

# Env key for app log level (used by CLI and app load for reload child).
LOG_LEVEL_ENV = "LANTAI_LOG_LEVEL"

# Env to indicate running inside a container (e.g. Docker). Set to 1/true/yes.
RUNNING_IN_CONTAINER = EnvVarLoader.get_bool(
    "LANTAI_RUNNING_IN_CONTAINER",
    False,
)

# Timeout in seconds for checking if a provider is reachable.
MODEL_PROVIDER_CHECK_TIMEOUT = EnvVarLoader.get_float(
    "LANTAI_MODEL_PROVIDER_CHECK_TIMEOUT",
    5.0,
    min_value=0,
    allow_inf=False,
)

# Playwright: use system Chromium when set (e.g. in Docker).
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH_ENV = "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"

# When True, expose /docs, /redoc, /openapi.json
# (dev only; keep False in prod).
DOCS_ENABLED = EnvVarLoader.get_bool("LANTAI_OPENAPI_DOCS", False)

# Memory directory
MEMORY_DIR = WORKING_DIR / "memory"

# Backup directory
BACKUP_DIR = (
    Path(
        EnvVarLoader.get_str(
            "LANTAI_BACKUP_DIR",
            f"{WORKING_DIR}.backups",
        ),
    )
    .expanduser()
    .resolve()
)

# Custom channel modules (installed via `lantai channels install`); manager
# loads BaseChannel subclasses from here.
CUSTOM_CHANNELS_DIR = WORKING_DIR / "custom_channels"

# Plugin directory (installed via `lantai plugin install`)
PLUGINS_DIR = WORKING_DIR / "plugins"

# Local models directory
MODELS_DIR = WORKING_DIR / "models"

MEMORY_COMPACT_KEEP_RECENT = EnvVarLoader.get_int(
    "LANTAI_MEMORY_COMPACT_KEEP_RECENT",
    3,
    min_value=0,
)

# Memory compaction configuration
MEMORY_COMPACT_RATIO = EnvVarLoader.get_float(
    "LANTAI_MEMORY_COMPACT_RATIO",
    0.7,
    min_value=0,
    allow_inf=False,
)

DASHSCOPE_BASE_URL = EnvVarLoader.get_str(
    "DASHSCOPE_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)

# CORS configuration — comma-separated list of allowed origins for dev mode.
# Example: LANTAI_CORS_ORIGINS="http://localhost:5173,http://127.0.0.1:5173"
# When unset, CORS middleware is not applied.
CORS_ORIGINS = EnvVarLoader.get_str("LANTAI_CORS_ORIGINS", "").strip()

# LLM API retry configuration
LLM_MAX_RETRIES = EnvVarLoader.get_int(
    "LANTAI_LLM_MAX_RETRIES",
    3,
    min_value=0,
)

LLM_BACKOFF_BASE = EnvVarLoader.get_float(
    "LANTAI_LLM_BACKOFF_BASE",
    1.0,
    min_value=0.1,
)

LLM_BACKOFF_CAP = EnvVarLoader.get_float(
    "LANTAI_LLM_BACKOFF_CAP",
    10.0,
    min_value=0.5,
)

# LLM concurrency control
# Maximum number of concurrent in-flight LLM calls; excess requests wait on
# the semaphore.  Tune to your API quota: start conservatively at 3-5 and
# increase (e.g. OpenAI Tier 1 ~500 QPM allows ~25 at 3 s/call average).
LLM_MAX_CONCURRENT = EnvVarLoader.get_int(
    "LANTAI_LLM_MAX_CONCURRENT",
    3,
    min_value=1,
    max_value=8,
)

# Maximum queries per minute (QPM), enforced via a 60-second sliding window.
# New requests that would exceed this limit will wait before being dispatched
# to the API — proactively preventing 429s rather than reacting to them.
# 0 = unlimited (disabled).
# Examples: Anthropic Tier-1 ≈ 50 QPM; OpenAI Tier-1 ≈ 500 QPM.
LLM_MAX_QPM = EnvVarLoader.get_int(
    "LANTAI_LLM_MAX_QPM",
    600,
    min_value=0,
)

# Default global pause duration (seconds) applied to all waiters when a 429
# is received.  Overridden by the API's Retry-After header when present.
LLM_RATE_LIMIT_PAUSE = EnvVarLoader.get_float(
    "LANTAI_LLM_RATE_LIMIT_PAUSE",
    5.0,
    min_value=1.0,
)

# Random jitter range (seconds) added on top of the pause remaining time so
# concurrent waiters stagger their wake-up and avoid a new burst.
LLM_RATE_LIMIT_JITTER = EnvVarLoader.get_float(
    "LANTAI_LLM_RATE_LIMIT_JITTER",
    1.0,
    min_value=0.0,
)

# Maximum time (seconds) a caller will wait for a provider-scoped execution
# slot before failing visibly instead of looking frozen for several minutes.
LLM_ACQUIRE_TIMEOUT = EnvVarLoader.get_float(
    "LANTAI_LLM_ACQUIRE_TIMEOUT",
    30.0,
    min_value=10.0,
)

# Tool guard approval timeout (seconds).
try:
    TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS = max(
        float(
            _get_env("LANTAI_TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS", "300"),
        ),
        1.0,
    )
except (TypeError, ValueError):
    TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS = 300.0

# Tool guard approval heartbeat interval (seconds).
# Sends periodic heartbeat messages during approval wait to keep SSE
# connection alive. Should be less than browser/proxy timeout (30-60s).
try:
    TOOL_GUARD_APPROVAL_HEARTBEAT_INTERVAL = max(
        float(
            _get_env("LANTAI_TOOL_GUARD_APPROVAL_HEARTBEAT_INTERVAL", "15"),
        ),
        5.0,
    )
except (TypeError, ValueError):
    TOOL_GUARD_APPROVAL_HEARTBEAT_INTERVAL = 15.0

# Marker prepended to every truncation notice.
# Format:
#   <<<TRUNCATED>>>
#   The output above was truncated.
#   The full content is saved to the file and contains Z lines in total.
#   This excerpt starts at line X and covers the next N bytes.
#   If the current content is not enough, call `read_file` with
#   file_path=<path> start_line=Y to read more.
#
# Split output on this marker to recover the original (untruncated) portion:
#   original = output.split(TRUNCATION_NOTICE_MARKER)[0]
TRUNCATION_NOTICE_MARKER = "<<<TRUNCATED>>>"

# Placeholder text used when media blocks are stripped from messages
# because the model does not support multimodal content.
MEDIA_UNSUPPORTED_PLACEHOLDER = (
    "[Media content removed - model does not support this media type]"
)
