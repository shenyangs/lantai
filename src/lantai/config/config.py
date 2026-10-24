# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional, Union, Dict, List, Literal, Any, Set

from pydantic import (
    BaseModel,
    Field,
    ConfigDict,
    field_validator,
    model_validator,
)
import shortuuid
from lantai.exceptions import ConfigurationException

from .timezone import detect_system_timezone
from ..utils.restore_fence import restore_read_fence
from .snapshots import (
    acknowledge_save, file_identity, merge_loaded_changes, remember_read,
)
from ..constant import (
    EnvVarLoader,
    HEARTBEAT_DEFAULT_EVERY,
    HEARTBEAT_DEFAULT_TARGET,
    LLM_ACQUIRE_TIMEOUT,
    LLM_BACKOFF_BASE,
    LLM_BACKOFF_CAP,
    LLM_MAX_CONCURRENT,
    LLM_MAX_RETRIES,
    LLM_MAX_QPM,
    LLM_RATE_LIMIT_JITTER,
    LLM_RATE_LIMIT_PAUSE,
    WORKING_DIR,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Core config models (moved here to avoid circular imports)
# ============================================================================


class ModelSlotConfig(BaseModel):
    """Model slot configuration for LLM routing."""

    provider_id: str = Field(default="")
    model: str = Field(default="")


class UtilityIntelligenceConfig(BaseModel):
    """Isolated model used for short, non-conversational intelligence tasks."""

    model_config = ConfigDict(extra="ignore")

    mode: Literal["follow_active", "dedicated", "disabled"] = Field(
        default="follow_active",
        description=(
            "Use the active chat model, a dedicated model slot, or disable "
            "background utility intelligence."
        ),
    )
    background_processing_enabled: bool = Field(
        default=True,
        description=(
            "Allow post-answer semantic processing. This is independent from "
            "whether a dedicated accelerator model is configured."
        ),
    )
    model: ModelSlotConfig = Field(default_factory=ModelSlotConfig)
    timeout_seconds: float = Field(
        default=12.0,
        ge=1.0,
        le=60.0,
        description="Hard timeout for one non-streaming utility task.",
    )

    @model_validator(mode="after")
    def _migrate_legacy_disabled_mode(self):
        # ``disabled`` historically mixed up two different decisions: not
        # configuring a helper model and turning semantic capabilities off.
        # Preserve capability by routing old values to the primary model. The
        # explicit background_processing_enabled switch is the only true off.
        if self.mode == "disabled":
            self.mode = "follow_active"
        return self


class ImageGenerationConfig(BaseModel):
    """Ordered model routing for image generation inside chat."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=False,
        description=(
            "Expose the in-chat image generation tool when at least one "
            "image model is configured."
        ),
    )
    models: List[ModelSlotConfig] = Field(
        default_factory=list,
        description=(
            "Image models in failover order. Only models in this list are "
            "used for in-chat image generation."
        ),
    )
    timeout_seconds: float = Field(
        default=120.0,
        ge=5.0,
        le=600.0,
        description="Timeout for each image model attempt.",
    )
    works_cover_enabled: bool = Field(
        default=False,
        description=(
            "Generate optional visual covers for user-saved Works. This is "
            "kept separate from in-chat image generation because it may "
            "consume additional model quota."
        ),
    )
    works_cover_model: ModelSlotConfig = Field(
        default_factory=ModelSlotConfig,
        description="The explicitly selected image model for Works covers.",
    )

    @field_validator("models")
    @classmethod
    def _deduplicate_models(
        cls,
        value: List[ModelSlotConfig],
    ) -> List[ModelSlotConfig]:
        result: List[ModelSlotConfig] = []
        seen: Set[tuple[str, str]] = set()
        for slot in value:
            provider_id = slot.provider_id.strip()
            model = slot.model.strip()
            if not provider_id or not model:
                continue
            key = (provider_id, model)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                ModelSlotConfig(provider_id=provider_id, model=model),
            )
        return result


class VideoGenerationConfig(BaseModel):
    """Ordered model routing for video generation inside chat."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=False,
        description=(
            "Expose the in-chat video generation tool when at least one "
            "text-to-video model is configured."
        ),
    )
    models: List[ModelSlotConfig] = Field(
        default_factory=list,
        description=(
            "Text-to-video models in failover order. Only models in this "
            "list are used for in-chat video generation."
        ),
    )
    timeout_seconds: float = Field(
        default=600.0,
        ge=30.0,
        le=1800.0,
        description="Timeout for each video model attempt.",
    )
    legacy_model_flags_migrated: bool = Field(
        default=False,
        description=(
            "Whether the former per-model use_for_video switches were "
            "replaced by this explicit routing configuration."
        ),
    )

    @field_validator("models")
    @classmethod
    def _deduplicate_models(
        cls,
        value: List[ModelSlotConfig],
    ) -> List[ModelSlotConfig]:
        result: List[ModelSlotConfig] = []
        seen: Set[tuple[str, str]] = set()
        for slot in value:
            provider_id = slot.provider_id.strip()
            model = slot.model.strip()
            if not provider_id or not model:
                continue
            key = (provider_id, model)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                ModelSlotConfig(provider_id=provider_id, model=model),
            )
        return result


class ActiveModelsInfo(BaseModel):
    """Active models information for provider manager."""

    active_llm: ModelSlotConfig | None
    context_policy: Dict[str, Any] | None = None


# Agent ID validation: alphanumeric, hyphens, underscores.
_AGENT_ID_PATTERN = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_-]*[a-zA-Z0-9]$",
)
_AGENT_ID_MIN_LENGTH = 2
_AGENT_ID_MAX_LENGTH = 64
_RESERVED_AGENT_IDS = frozenset({"default"})
_LEGACY_RUNTIME_ENGINE = "legacy"
_NATIVE_RUNTIME_ENGINE = "native"
_PRE_NATIVE_RUNTIME_ENGINE_ALIAS = "agent" + "scope"
_RUNTIME_ENGINE_VALUES = frozenset(
    {_LEGACY_RUNTIME_ENGINE, _NATIVE_RUNTIME_ENGINE},
)
_NATIVE_RUNTIME_DEFAULT_CHANNELS = frozenset({"dev", "canary"})
_MAS_RUNTIME_CHANNELS = frozenset(
    {"mas", "appstore", "app-store", "app_store", "testflight"},
)
_NATIVE_RUNTIME_DEV_CANARY_MIGRATION = "native-runtime-dev-canary-default-2026-05-31"
_NATIVE_RUNTIME_DEFAULT_MIGRATION = "native-runtime-default-2026-07-24"
_NATIVE_RUNTIME_ONLY_MIGRATION = "native-runtime-only-2026-08-30"
_LLM_ACQUIRE_TIMEOUT_30S_MIGRATION = "llm-acquire-timeout-30s-2026-08-09"
_STATIC_PROMPT_BOUNDARY_MIGRATION = "static-prompt-boundary-2026-08-09"
_LEGACY_LLM_ACQUIRE_TIMEOUT_SECONDS = 300.0

DEFAULT_AGENT_SYSTEM_PROMPT_FILES = [
    "AGENTS.md",
    "SOUL.md",
    "PROFILE.md",
    "BACKSTORY.md",
    "SELF_CANON.md",
]
AGENT_ON_DEMAND_REFERENCE_FILES = frozenset(
    {"PRODUCT_GUIDE.md", "VISUAL_CANON.md"},
)
AGENT_TRANSIENT_REFERENCE_FILES = frozenset({"STATE.md", "WORKBENCH.md"})
AGENT_INTERNAL_WORKSPACE_MD_FILES = frozenset(
    {
        *DEFAULT_AGENT_SYSTEM_PROMPT_FILES,
        *AGENT_ON_DEMAND_REFERENCE_FILES,
        *AGENT_TRANSIENT_REFERENCE_FILES,
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "MEMORY.md",
    },
)
_DYNAMIC_WORKSPACE_PROMPT_FILES = set(AGENT_TRANSIENT_REFERENCE_FILES)


def _normalize_runtime_engine(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized == _PRE_NATIVE_RUNTIME_ENGINE_ALIAS:
        return _LEGACY_RUNTIME_ENGINE
    return normalized if normalized in _RUNTIME_ENGINE_VALUES else None


def _runtime_distribution_channel() -> str:
    return (
        EnvVarLoader.get_str("LANTAI_DISTRIBUTION_CHANNEL")
        or EnvVarLoader.get_str("LANTAI_RELEASE_CHANNEL")
    ).strip().lower()


def _is_app_store_runtime_distribution(channel: str | None = None) -> bool:
    normalized = str(channel or _runtime_distribution_channel()).strip().lower()
    if EnvVarLoader.get_bool("LANTAI_APP_STORE"):
        return True
    if normalized in _MAS_RUNTIME_CHANNELS:
        return True
    return any("lantai_app store" == part.lower() for part in Path(__file__).parts)


def _distribution_default_runtime_engine() -> Literal["native"]:
    return _NATIVE_RUNTIME_ENGINE


def default_runtime_engine() -> Literal["native"]:
    """Return the runtime engine default for the current distribution."""

    explicit = _normalize_runtime_engine(
        EnvVarLoader.get_str("LANTAI_RUNTIME_ENGINE"),
    )
    if explicit == _LEGACY_RUNTIME_ENGINE:
        logger.warning(
            "Ignoring retired LANTAI_RUNTIME_ENGINE=legacy override; "
            "official product execution is Native-only.",
        )
    return _NATIVE_RUNTIME_ENGINE


def default_harness_mode() -> Literal["off", "shadow", "enforced"]:
    """Return the compatible Coherence Harness rollout mode."""

    normalized = EnvVarLoader.get_str("LANTAI_HARNESS_MODE").strip().lower()
    if normalized in {"off", "shadow", "enforced"}:
        return normalized  # type: ignore[return-value]
    return "shadow"


def generate_short_agent_id() -> str:
    """Generate a 6-character short UUID for agent identification.

    Returns:
        6-character short UUID string
    """
    return shortuuid.ShortUUID().random(length=6)


def sanitize_agent_id(raw: str) -> str:
    """Normalize raw agent ID input: strip whitespace.

    Args:
        raw: Raw user input for agent ID.

    Returns:
        Sanitized agent ID string.
    """
    return raw.strip()


def validate_agent_id(
    agent_id: str,
    existing_ids: Set[str],
) -> None:
    """Validate a custom agent ID.

    Checks length, character set, reserved words, and uniqueness.

    Args:
        agent_id: The sanitized agent ID to validate.
        existing_ids: Set of already-registered agent IDs.

    Raises:
        ValueError: If the ID is invalid.
    """
    if len(agent_id) < _AGENT_ID_MIN_LENGTH:
        raise ValueError(
            f"Agent ID must be at least {_AGENT_ID_MIN_LENGTH} characters, "
            f"got {len(agent_id)}.",
        )
    if len(agent_id) > _AGENT_ID_MAX_LENGTH:
        raise ValueError(
            f"Agent ID must be at most {_AGENT_ID_MAX_LENGTH} characters, "
            f"got {len(agent_id)}.",
        )
    if not _AGENT_ID_PATTERN.match(agent_id):
        raise ValueError(
            f"Agent ID '{agent_id}' contains invalid characters. "
            "Only letters, digits, hyphens, and underscores "
            "are allowed. Cannot start or end with '-' or '_'.",
        )
    if agent_id in _RESERVED_AGENT_IDS:
        raise ValueError(
            f"Agent ID '{agent_id}' is reserved and cannot be used.",
        )
    if agent_id in existing_ids:
        raise ValueError(
            f"Agent ID '{agent_id}' already exists.",
        )


class BaseChannelConfig(BaseModel):
    """Base for channel config (read from config.json, no env)."""

    enabled: bool = False
    bot_prefix: str = ""
    filter_tool_messages: bool = False
    filter_thinking: bool = False
    show_thinking: bool = True
    show_tool_calls: bool = True
    show_tool_results: bool = True
    tool_call_max_length: int = Field(default=200, ge=0)
    tool_result_max_length: int = Field(default=500, ge=0)
    no_text_debounce: bool = True
    # Version 2 makes newly-created external connections closed by default.
    # ChannelConfig migrates persisted pre-v2 entries back to explicit open.
    security_version: Literal[1, 2] = 2
    dm_policy: Literal["open", "allowlist", "disabled"] = "allowlist"
    group_policy: Literal["open", "allowlist", "disabled"] = "allowlist"
    allow_from: List[str] = Field(default_factory=list)
    deny_message: str = ""
    require_mention: bool = False


class IMessageChannelConfig(BaseChannelConfig):
    db_path: str = "~/Library/Messages/chat.db"
    poll_sec: float = 1.0
    media_dir: Optional[str] = None
    max_decoded_size: int = (
        10 * 1024 * 1024
    )  # 10MB default limit for Base64 data


class DiscordConfig(BaseChannelConfig):
    bot_token: str = ""
    http_proxy: str = ""
    http_proxy_auth: str = ""
    accept_bot_messages: bool = False
    streaming_enabled: bool = False
    media_dir: Optional[str] = None


class SlackConfig(BaseChannelConfig):
    """Slack Socket Mode channel (no public webhook required)."""

    bot_token: str = ""
    app_token: str = ""
    proxy: str = ""
    streaming_enabled: bool = False
    require_mention: bool = True
    media_dir: Optional[str] = None


class DingTalkConfig(BaseChannelConfig):
    client_id: str = ""
    client_secret: str = ""
    message_type: str = "markdown"
    cron_message_type: str = "markdown"
    card_template_id: str = ""
    card_template_key: str = "content"
    robot_code: str = ""
    media_dir: Optional[str] = None
    card_auto_layout: bool = False
    at_sender_on_reply: bool = False
    streaming_enabled: bool = False
    share_session_in_group: bool = False
    endpoint: str = ""


class FeishuConfig(BaseChannelConfig):
    """Feishu/Lark channel: app_id, app_secret; optional encrypt_key,
    verification_token for event handler. media_dir for received media.
    domain: 'feishu' for China, 'lark' for international.
    """

    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    media_dir: Optional[str] = None
    domain: str = "feishu"
    streaming_enabled: bool = False
    share_session_in_group: bool = False

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        if value in {"feishu", "lark"} or value.startswith(
            ("http://", "https://"),
        ):
            return value
        raise ValueError(
            "domain must be 'feishu', 'lark', or an http(s) base URL",
        )


class QQConfig(BaseChannelConfig):
    app_id: str = ""
    client_secret: str = ""
    markdown_enabled: bool = True
    max_reconnect_attempts: int = 100
    ack_message: str = ""


class OneBotConfig(BaseChannelConfig):
    """OneBot v11 channel: reverse WebSocket for NapCat/go-cqhttp/Lagrange."""

    ws_host: str = "127.0.0.1"
    ws_port: int = 6199
    access_token: str = ""
    share_session_in_group: bool = False
    media_dir: Optional[str] = None
    media_base64: bool = False
    media_base64_max_mb: int = Field(default=10, gt=0)
    media_download_max_mb: int = Field(default=50, gt=0)


class YuanbaoConfig(BaseChannelConfig):
    """Tencent Yuanbao bot over authenticated protobuf WebSocket."""

    app_id: str = ""
    app_secret: str = ""
    api_domain: str = "bot.yuanbao.tencent.com"
    ws_url: str = ""
    media_dir: Optional[str] = None
    require_mention: bool = True
    accept_bot_messages: bool = False


class TelegramConfig(BaseChannelConfig):
    bot_token: str = ""
    base_url: str = ""
    http_proxy: str = ""
    http_proxy_auth: str = ""
    show_typing: Optional[bool] = None
    streaming_enabled: bool = False


class MQTTConfig(BaseChannelConfig):
    host: str = ""
    port: Optional[int] = None
    transport: str = ""
    clean_session: bool = True
    qos: int = 2
    username: Optional[str] = None
    password: Optional[str] = None
    subscribe_topic: str = ""
    publish_topic: str = ""
    tls_enabled: bool = False
    tls_ca_certs: Optional[str] = None
    tls_certfile: Optional[str] = None
    tls_keyfile: Optional[str] = None


class MattermostConfig(BaseChannelConfig):
    """Mattermost channel: WebSocket polling and REST API."""

    url: str = ""
    bot_token: str = ""
    media_dir: Optional[str] = None
    show_typing: Optional[bool] = None
    thread_follow_without_mention: bool = False


class ConsoleConfig(BaseChannelConfig):
    """Console channel: prints agent responses to stdout."""

    enabled: bool = True
    media_dir: Optional[str] = None


class WecomConfig(BaseChannelConfig):
    """WeCom (Enterprise WeChat) AI Bot channel config."""

    bot_id: str = ""
    secret: str = ""
    ws_url: str = ""
    media_dir: Optional[str] = None
    welcome_text: str = ""
    # If True (default), all group members share one chat; set to
    # False to isolate each member into their own chat.
    share_session_in_group: bool = True
    max_reconnect_attempts: int = -1
    streaming_enabled: bool = False


class MatrixConfig(BaseChannelConfig):
    """Matrix channel configuration."""

    homeserver: str = ""

    @field_validator("homeserver")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    user_id: str = ""
    access_token: str = ""

    # Extended Matrix channel fields
    group_allow_from: List[str] = Field(default_factory=list)
    groups: Dict[str, Any] = Field(default_factory=dict)
    encryption: bool = False
    # When False, images are surfaced as text placeholders (no vision URL).
    vision_enabled: bool = True
    history_limit: int = 50
    username: str = ""
    password: str = ""
    device_name: str = "lantai-worker"
    # matrix-nio sync long-poll timeout (ms); typical 30s
    sync_timeout_ms: int = Field(default=30000, ge=5000, le=300000)
    # When True, prepend HTML pill to formatted_body for outbound mentions.
    # Default False: m.mentions is always set for push, but pill is omitted.
    mention_pill_in_body: bool = False
    # When True, apply m.mentions + optional pill on outbound messages.
    outbound_structured_mentions: bool = True
    streaming_enabled: bool = False
    share_session_in_group: bool = True


class VoiceChannelConfig(BaseChannelConfig):
    """Voice channel: Twilio ConversationRelay + Cloudflare Tunnel."""

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    phone_number: str = ""
    phone_number_sid: str = ""
    tts_provider: str = "google"
    tts_voice: str = "en-US-Journey-D"
    stt_provider: str = "deepgram"
    language: str = "en-US"
    welcome_greeting: str = "Hi! I'm 沈行知. How can I help you?"


class SIPChannelConfig(BaseChannelConfig):
    """SIP voice channel: dual-track (pyVoIP dev / LiveKit production)."""

    sip_mode: str = "dev"
    sip_host: str = "0.0.0.0"
    sip_port: int = 5061
    sip_username: str = ""
    sip_password: str = ""
    sip_server: str = ""
    sip_transport: str = "UDP"
    rtp_port_low: int = 10000
    rtp_port_high: int = 20000
    dashscope_api_key: str = ""
    tts_provider: str = "aliyun"
    tts_voice: str = ""
    stt_provider: str = "aliyun"
    language: str = "zh-CN"
    welcome_greeting: str = "你好，我是沈行知"
    call_timeout: float = 120.0
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_sip_trunk_id: str = ""
    livekit_room_name: str = "sip-inbound"
    livekit_output_sample_rate: int = 24000
    max_concurrent_calls: int = 5


class XiaoYiConfig(BaseChannelConfig):
    """XiaoYi channel: Huawei A2A protocol via WebSocket."""

    ak: str = ""  # Access Key
    sk: str = ""  # Secret Key
    agent_id: str = ""  # Agent ID from XiaoYi platform
    ws_url: str = "wss://hag.cloud.huawei.com/openclaw/v1/ws/link"
    task_timeout_ms: int = 3600000  # 1 hour task timeout


class WeChatConfig(BaseChannelConfig):
    """WeChat (iLink Bot) personal account channel config.

    bot_token:              Bearer token obtained after QR code login.
    bot_token_file:         Path to persist/load the bot_token
                            (default ~/.lantai/wechat_bot_token).
    base_url:               iLink API base URL (leave empty to use default).
    media_dir:              Local directory for downloaded media files.
    message_merge_enabled:  When True, merge multiple outgoing text messages
                            within a single request to reduce message count
                            (mitigates the 10-message context_token limit).
    message_merge_delay_ms: Controls merge behaviour when merging is enabled.
                            0  → merge ALL text messages and send once at the
                                 end of the request (maximum savings).
                            >0 → buffer messages for this many milliseconds;
                                 if no new message arrives within the window
                                 the buffer is flushed (adjacent-merge mode).
    """

    bot_token: str = ""
    bot_token_file: str = ""
    base_url: str = ""
    media_dir: Optional[str] = None
    message_merge_enabled: bool = False
    message_merge_delay_ms: Optional[int] = 0


class ChannelConfig(BaseModel):
    """Built-in channel configs; extra keys allowed for plugin channels."""

    model_config = ConfigDict(extra="allow")

    imessage: IMessageChannelConfig = IMessageChannelConfig()
    discord: DiscordConfig = DiscordConfig()
    slack: SlackConfig = SlackConfig()
    dingtalk: DingTalkConfig = DingTalkConfig()
    feishu: FeishuConfig = FeishuConfig()
    qq: QQConfig = QQConfig()
    telegram: TelegramConfig = TelegramConfig()
    mattermost: MattermostConfig = MattermostConfig()
    mqtt: MQTTConfig = MQTTConfig()
    console: ConsoleConfig = ConsoleConfig()
    matrix: MatrixConfig = MatrixConfig()
    voice: VoiceChannelConfig = VoiceChannelConfig()
    sip: SIPChannelConfig = SIPChannelConfig()
    wecom: WecomConfig = WecomConfig()
    xiaoyi: XiaoYiConfig = XiaoYiConfig()
    wechat: WeChatConfig = WeChatConfig()
    onebot: OneBotConfig = OneBotConfig()
    yuanbao: YuanbaoConfig = YuanbaoConfig()

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_weixin_key(cls, data: Any) -> Any:
        """One-shot migration: legacy ``weixin`` key -> canonical ``wechat``.

        Older config files used ``weixin`` as the WeChat channel key. The
        canonical key is now ``wechat``. When an old config is loaded we
        rename the key in-place so validation succeeds. The on-disk file is
        rewritten by ``load_config`` right after validation (see utils.py).
        """
        if isinstance(data, dict):
            data = dict(data)
            if "weixin" in data:
                legacy = data.pop("weixin")
                if "wechat" not in data:
                    data["wechat"] = legacy

            # Persisted channel entries created before secure-defaults v2 did
            # not carry a version marker. Preserve their behavior explicitly;
            # only genuinely new configs inherit allowlist defaults.
            for key, raw_config in list(data.items()):
                if not isinstance(raw_config, dict):
                    continue
                if "security_version" in raw_config:
                    continue
                migrated = dict(raw_config)
                migrated.setdefault("dm_policy", "open")
                migrated.setdefault("group_policy", "open")
                migrated["security_version"] = 1
                data[key] = migrated
        return data


class LastApiConfig(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None


class ActiveHoursConfig(BaseModel):
    """Optional active window for heartbeat (e.g. 08:00–22:00)."""

    start: str = "08:00"
    end: str = "22:00"


class HeartbeatConfig(BaseModel):
    """Heartbeat: run agent with HEARTBEAT.md as query at interval."""

    model_config = {"populate_by_name": True}

    enabled: bool = Field(default=False, description="Whether heartbeat is on")
    every: str = Field(default=HEARTBEAT_DEFAULT_EVERY)
    target: str = Field(default=HEARTBEAT_DEFAULT_TARGET)
    active_hours: Optional[ActiveHoursConfig] = Field(
        default=None,
        alias="activeHours",
    )


class AgentsDefaultsConfig(BaseModel):
    heartbeat: Optional[HeartbeatConfig] = None


class AutoMemorySearchConfig(BaseModel):
    """Auto memory search configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=True,
        description="Whether to auto search memory on every turn",
    )

    max_results: int = Field(
        default=2,
        ge=1,
        description=(
            "Maximum number of results to return when auto memory"
            " search is enabled"
        ),
    )

    min_score: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum relevance score for results when auto memory"
            " search is enabled"
        ),
    )


class EmbeddingModelConfig(BaseModel):
    """Embedding model configuration."""

    model_config = ConfigDict(extra="ignore")

    backend: str = Field(
        default="local",
        description="Embedding backend (local, onnx, openai-compatible)",
    )
    api_key: str = Field(
        default="",
        description="API key for embedding provider",
    )
    base_url: str = Field(default="", description="Base URL for embedding API")
    model_name: str = Field(
        default="local/bge-small-zh-v1.5",
        description="Embedding model name",
    )
    dimensions: int = Field(default=512, description="Embedding dimensions")
    enable_cache: bool = Field(
        default=True,
        description="Whether to enable embedding cache",
    )
    use_dimensions: bool = Field(
        default=False,
        description="Whether to use custom dimensions",
    )
    max_cache_size: int = Field(default=3000, description="Maximum cache size")
    max_input_length: int = Field(
        default=8192,
        description="Maximum input length for embedding",
    )
    max_batch_size: int = Field(
        default=10,
        description="Maximum batch size for embedding",
    )

    @model_validator(mode="after")
    def migrate_blank_remote_default(self) -> "EmbeddingModelConfig":
        """Upgrade profiles persisted before local embedding became built in."""

        if (
            self.backend.strip().lower() == "openai"
            and not self.base_url.strip()
            and not self.model_name.strip()
        ):
            self.backend = "local"
            self.model_name = "local/bge-small-zh-v1.5"
            self.dimensions = 512
        return self


class LayeredMemoryConfig(BaseModel):
    """Layered long-term memory configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=True,
        description="Whether to enable L0/L1/L2/L3 layered memory.",
    )
    capture_l0_enabled: bool = Field(
        default=True,
        description="Persist raw conversation turns as auditable L0 records.",
    )
    atom_extraction_enabled: bool = Field(
        default=True,
        description="Extract lightweight L1 memory atoms from turns.",
    )
    llm_atom_extraction_enabled: bool = Field(
        default=True,
        description=(
            "Use an isolated utility model to extract L1 atoms, with the "
            "active model as fallback; raw L0 evidence remains when both fail."
        ),
    )
    max_atoms_per_turn: int = Field(
        default=10,
        ge=1,
        le=30,
        description="Maximum L1 atom candidates extracted from one turn.",
    )
    extraction_timeout_seconds: float = Field(
        default=20.0,
        ge=1.0,
        le=120.0,
        description="Hard timeout for LLM-based atom extraction.",
    )
    conflict_detection_enabled: bool = Field(
        default=True,
        description="Detect duplicate/conflicting L1 atoms before writing.",
    )
    scene_enabled: bool = Field(
        default=True,
        description="Generate L2 scene markdown files from L1 atoms.",
    )
    persona_enabled: bool = Field(
        default=True,
        description="Generate L3 persona markdown from scenes/profile memory.",
    )
    system2_enabled: bool = Field(
        default=True,
        description=(
            "Run a background slow-lane consolidation pass that builds "
            "cognitive models, open intentions, and evolution-chain summaries."
        ),
    )
    system2_interval_turns: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Run System2 consolidation every N user turns.",
    )
    system2_max_atoms: int = Field(
        default=80,
        ge=10,
        le=500,
        description="Maximum L1 atoms used by one System2 consolidation pass.",
    )
    max_recall_results: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum layered memory results injected per recall.",
    )
    hybrid_recall_enabled: bool = Field(
        default=True,
        description=(
            "Fuse structured profile, L1 atoms, L0 snippets, and offload "
            "evidence into one ranked recall context."
        ),
    )
    max_context_chars: int = Field(
        default=5200,
        ge=500,
        le=20000,
        description="Maximum characters of layered dynamic recall context.",
    )
    max_context_tokens: int | None = Field(
        default=None,
        ge=128,
        le=131072,
        description="Preferred token budget for layered dynamic recall; when "
        "unset the legacy character limit remains compatible.",
    )
    stable_context_chars: int = Field(
        default=4200,
        ge=500,
        le=20000,
        description="Maximum characters of stable persona/scene context.",
    )
    stable_context_tokens: int | None = Field(
        default=None,
        ge=128,
        le=131072,
        description="Preferred token budget for stable memory; when unset "
        "the legacy character limit remains compatible.",
    )


class MemoryOffloadConfig(BaseModel):
    """Symbolic tool-result offload configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=True,
        description="Whether to offload large tool results into refs.",
    )
    min_bytes: int = Field(
        default=6000,
        ge=1000,
        description="Minimum tool-result byte size before offloading.",
    )
    max_summary_chars: int = Field(
        default=1600,
        ge=400,
        le=8000,
        description="Maximum symbolic summary characters kept in context.",
    )


class ReMeLightMemoryConfig(BaseModel):
    """ReMeLight memory manager configuration."""

    model_config = ConfigDict(extra="ignore")

    summarize_when_compact: bool = Field(
        default=True,
        description="Whether to enable memory summarization during compaction",
    )

    auto_memory_interval: int | None = Field(
        default=None,
        description="Legacy periodic auto-memory interval. Kept readable for "
        "old configs but no longer schedules extraction; capture and "
        "consolidation are event-driven.",
    )

    dream_cron: str = Field(
        default="0 3 * * *",
        description="Local-time cron expression for governed Dreaming.",
    )

    dreaming_enabled: bool = Field(
        default=False,
        description=(
            "Opt in to scheduled Light/REM/Deep memory consolidation. "
            "Existing legacy dream schedules remain disabled until enabled."
        ),
    )

    dreaming_rem_enabled: bool = Field(
        default=True,
        description=(
            "Generate the human-readable reflection diary. The diary is "
            "isolated from promotion evidence."
        ),
    )

    dreaming_min_score: float = Field(
        default=0.68,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum deterministic Deep score before an inferred fact may "
            "enter the user-review queue."
        ),
    )

    auto_memory_search_config: AutoMemorySearchConfig = Field(
        default_factory=AutoMemorySearchConfig,
    )

    embedding_model_config: EmbeddingModelConfig = Field(
        default_factory=EmbeddingModelConfig,
    )

    layered_memory_config: LayeredMemoryConfig = Field(
        default_factory=LayeredMemoryConfig,
    )

    offload_config: MemoryOffloadConfig = Field(
        default_factory=MemoryOffloadConfig,
    )

    rebuild_memory_index_on_start: bool = Field(
        default=False,
        description=(
            "Whether to clear and rebuild the memory search index when the"
            " agent starts. Set to False to skip re-indexing and only monitor"
            " new file changes."
        ),
    )

    recursive_file_watcher: bool = Field(
        default=True,
        description=(
            "Whether to watch memory directory recursively. "
            "Set to True to include subdirectories like memory/subdirectory/* "
            "in vector search indexing."
        ),
    )

    strong_defaults_applied: bool = Field(
        default=False,
        description=(
            "Internal migration marker for strong auto-updating memory defaults."
        ),
    )


class ContextCompactConfig(BaseModel):
    """Context compaction configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=True,
        description="Whether to enable automatic context compaction",
    )

    compact_threshold_ratio: float = Field(
        default=0.8,
        ge=0.1,
        le=0.9,
        description=(
            "Legacy readable compaction threshold ratio. New runtime "
            "compaction derives its budget from the effective model window."
        ),
    )

    reserve_threshold_ratio: float = Field(
        default=0.1,
        ge=0,
        le=0.3,
        description=(
            "Legacy readable reserve ratio. New runtime compaction keeps a "
            "token budget selected from the effective model window."
        ),
    )

    compact_with_thinking_block: bool = Field(
        default=True,
        description="Whether to include thinking blocks when compacting",
    )


class ToolResultPruningConfig(BaseModel):
    """Tool result pruning configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default=True,
        description="Whether to enable tool result pruning",
    )

    pruning_recent_n: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Number of recent messages to use recent_max_bytes for",
    )

    pruning_old_msg_max_bytes: int = Field(
        default=3000,
        ge=100,
        description=("Byte threshold for old messages in tool result pruning"),
    )

    pruning_recent_msg_max_bytes: int = Field(
        default=50000,
        ge=1000,
        description=(
            "Byte threshold for recent messages in tool result pruning"
        ),
    )

    offload_retention_days: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Number of days to retain tool result files",
    )

    tool_results_cache: str = Field(
        default="tool_results",
        description="Directory name for tool result cache files "
        "relative to working_dir",
    )

    exempt_file_extensions: List[str] = Field(
        default_factory=lambda: [".md"],
        description=(
            "File extensions exempt from tool result pruning. "
            "Tool results for read_file operations on these file types "
            "will use recent_max_bytes instead of old_max_bytes."
        ),
    )

    exempt_tool_names: List[str] = Field(
        default_factory=lambda: ["chat_with_agent"],
        description=(
            "Tool names exempt from tool result pruning. "
            "Tool results from these tools will use recent_max_bytes "
            "instead of old_max_bytes."
        ),
    )


class LightContextConfig(BaseModel):
    """Light context manager configuration."""

    model_config = ConfigDict(extra="ignore")

    dialog_path: str = Field(
        default="dialog",
        description="Path for dialog persistence to jsonl files "
        "relative to working_dir.",
    )

    token_count_estimate_divisor: float = Field(
        default=4,
        ge=2,
        le=5,
        description=(
            "Divisor for byte-based token estimation (byte_len / divisor)"
        ),
    )

    context_compact_config: ContextCompactConfig = Field(
        default_factory=ContextCompactConfig,
    )
    tool_result_pruning_config: ToolResultPruningConfig = Field(
        default_factory=ToolResultPruningConfig,
    )


LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH = 128 * 1024
DEFAULT_AGENT_MAX_INPUT_LENGTH = 256 * 1024
_AGENT_CONTEXT_WINDOW_256K_MIGRATION = "agent_context_window_256k"
_KIMI_CONTEXT_WINDOW_256K_MIGRATION = "kimi_context_window_256k"


class AgentsRunningConfig(BaseModel):
    """Agent runtime behavior configuration."""

    model_config = ConfigDict(extra="ignore")

    max_iters: int = Field(
        default=100,
        ge=1,
        description=(
            "Maximum number of reasoning-acting iterations for ReAct agent"
        ),
    )

    tool_call_limit_per_tool: int = Field(
        default=50,
        ge=0,
        description=(
            "Maximum calls to the same tool within one agent reply. "
            "0 disables this guard."
        ),
    )

    repeated_tool_call_limit: int = Field(
        default=3,
        ge=0,
        description=(
            "Maximum identical calls to the same tool with identical "
            "arguments within one agent reply. 0 disables this guard."
        ),
    )

    failed_tool_call_limit: int = Field(
        default=3,
        ge=0,
        description=(
            "Maximum failed calls with the same tool and identical arguments "
            "within one agent reply. 0 disables this guard."
        ),
    )

    enableQuqu: Optional[bool] = Field(
        default=None,
        description=(
            "Enable the ququ / inner monologue response style. Proxied from "
            "the agent profile; when set via running-config API, the value "
            "is written back to the agent profile."
        ),
    )

    llm_retry_enabled: bool = Field(
        default=LLM_MAX_RETRIES > 0,
        description="Whether to auto-retry transient LLM API errors",
    )

    llm_max_retries: int = Field(
        default=max(LLM_MAX_RETRIES, 1),
        ge=1,
        description="Maximum retry attempts for transient LLM API errors",
    )

    llm_backoff_base: float = Field(
        default=LLM_BACKOFF_BASE,
        ge=0.1,
        description="Base delay in seconds for exponential LLM retry backoff",
    )

    llm_backoff_cap: float = Field(
        default=LLM_BACKOFF_CAP,
        ge=0.5,
        description=(
            "Maximum delay cap in seconds for LLM retry backoff; "
            "must be greater than or equal to the base delay"
        ),
    )

    llm_max_concurrent: int = Field(
        default=LLM_MAX_CONCURRENT,
        ge=1,
        description=(
            "Maximum concurrent in-flight LLM calls per provider. "
            "Agents using different providers do not block one another."
        ),
    )

    llm_max_qpm: int = Field(
        default=LLM_MAX_QPM,
        ge=0,
        description=(
            "Maximum queries per minute (60-second sliding window). "
            "New requests that would exceed this limit wait before being "
            "dispatched — proactively preventing 429s. 0 = disabled."
        ),
    )

    llm_rate_limit_pause: float = Field(
        default=LLM_RATE_LIMIT_PAUSE,
        ge=1.0,
        description=(
            "Default pause duration (seconds) applied to the affected "
            "provider when a 429 rate-limit response is received."
        ),
    )

    llm_rate_limit_jitter: float = Field(
        default=LLM_RATE_LIMIT_JITTER,
        ge=0.0,
        description=(
            "Random jitter range (seconds) added on top of the pause so "
            "concurrent waiters stagger their wake-up."
        ),
    )

    llm_acquire_timeout: float = Field(
        default=LLM_ACQUIRE_TIMEOUT,
        ge=10.0,
        description=(
            "Maximum time (seconds) a caller waits to acquire a rate-limiter "
            "slot before giving up with an error."
        ),
    )

    shell_command_timeout: float = Field(
        default=60.0,
        ge=1.0,
        description=(
            "Default timeout in seconds for execute_shell_command. "
            "The LLM may still override this per-call via the timeout "
            "parameter."
        ),
    )

    @model_validator(mode="after")
    def validate_llm_retry_backoff(self) -> "AgentsRunningConfig":
        """Validate LLM retry backoff relationships."""
        if self.llm_backoff_cap < self.llm_backoff_base:
            raise ConfigurationException(
                config_key="llm_backoff",
                message=(
                    "llm_backoff_cap must be greater than or equal to "
                    "llm_backoff_base"
                ),
            )
        return self

    max_input_length: int = Field(
        default=DEFAULT_AGENT_MAX_INPUT_LENGTH,
        ge=1000,
        description=(
            "User ceiling for model input. The effective budget is resolved "
            "from the exact model, route, runtime, and context policy."
        ),
    )

    context_policy_mode: Literal["smart", "max", "economy"] = Field(
        default="smart",
        description=(
            "Context budget policy. Smart is the product default; max uses "
            "the full verified input budget; economy compacts earlier."
        ),
    )

    history_max_length: int = Field(
        default=10000,
        ge=1000,
        description="Maximum length for /history command output",
    )

    context_manager_backend: str = Field(default="light")

    light_context_config: LightContextConfig = Field(
        default_factory=LightContextConfig,
    )

    memory_manager_backend: str = Field(default="remelight")

    reme_light_memory_config: ReMeLightMemoryConfig = Field(
        default_factory=ReMeLightMemoryConfig,
    )

    daily_memory_dir: str = Field(
        default="memory",
        description="Dir name to daily summary file",
    )

    approval_level: Optional[str] = Field(
        default=None,
        description=(
            "Tool execution security level (proxied from agent profile): "
            "STRICT, SMART, AUTO, or OFF.  When set via running-config API, "
            "the value is written back to the agent profile."
        ),
    )


class AgentsLLMRoutingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=False)
    mode: Literal["local_first", "cloud_first"] = Field(
        default="local_first",
        description=(
            "local_first routes to the local slot by default; cloud_first "
            "routes to the cloud slot by default. Smarter switching can be "
            "added later without changing the dual-slot config shape."
        ),
    )
    local: ModelSlotConfig = Field(
        default_factory=ModelSlotConfig,
        description="Local model slot (required when routing is enabled).",
    )
    cloud: Optional[ModelSlotConfig] = Field(
        default=None,
        description=(
            "Optional explicit cloud model slot; when null, uses "
            "providers.json active_llm."
        ),
    )


class RuntimeConfig(BaseModel):
    """Runtime kernel and compatible Coherence Harness rollout.

    The official desktop build defaults to Native Runtime. Harness extensions
    start in shadow mode so old records and the direct Transcript writer remain
    compatible while manifests and health evidence are collected.
    """

    model_config = ConfigDict(extra="ignore")

    engine: Literal["native"] = Field(
        default_factory=default_runtime_engine,
    )
    harness_mode: Literal["off", "shadow", "enforced"] = Field(
        default_factory=default_harness_mode,
    )
    health_recovery: Literal["observe", "safe"] = "safe"
    world_ledger_mode: Literal["off", "read_only", "supervised"] = "read_only"

    @field_validator("engine", mode="before")
    @classmethod
    def _normalize_engine_alias(cls, value: Any) -> Any:
        normalized = _normalize_runtime_engine(value)
        if normalized in {_LEGACY_RUNTIME_ENGINE, _NATIVE_RUNTIME_ENGINE}:
            return _NATIVE_RUNTIME_ENGINE
        return value

    @field_validator("harness_mode", mode="before")
    @classmethod
    def _normalize_harness_mode(cls, value: Any) -> Any:
        normalized = str(value or "").strip().lower()
        aliases = {"disabled": "off", "observe": "shadow", "on": "enforced"}
        return aliases.get(normalized, normalized)


class AgentProfileRef(BaseModel):
    """Agent Profile reference (stored in root config.json).

    Only contains ID and workspace directory reference.
    Full agent configuration is stored in workspace/agent.json.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="Unique agent ID")
    workspace_dir: str = Field(
        ...,
        description="Path to agent's workspace directory",
    )
    enabled: bool = Field(
        default=True,
        description="Whether agent is enabled (controls instance loading)",
    )


class PlanConfig(BaseModel):
    """Plan mode configuration (stored in agent.json)."""

    enabled: bool = Field(
        default=False,
        description="Whether plan mode is enabled for this agent",
    )


class AgentProfileConfig(BaseModel):
    """Complete Agent Profile configuration (stored in workspace/agent.json).

    Each agent has its own configuration file with all settings.
    """

    id: str = Field(..., description="Unique agent ID")
    name: str = Field(..., description="Human-readable agent name")
    description: str = Field(default="", description="Agent description")
    workspace_dir: str = Field(
        default="",
        description="Path to agent's workspace (optional, for reference)",
    )
    template_id: Optional[str] = Field(
        default=None,
        description="Builtin template used when this agent was created",
    )
    runtime_migrations: List[str] = Field(
        default_factory=list,
        description="Internal runtime config migrations applied to this agent",
    )

    # Agent-specific configurations
    channels: Optional["ChannelConfig"] = Field(
        default=None,
        description="Channel configurations for this agent",
    )
    mcp: Optional["MCPConfig"] = Field(
        default=None,
        description="MCP clients for this agent",
    )
    heartbeat: Optional[HeartbeatConfig] = Field(
        default=None,
        description="Heartbeat configuration for this agent",
    )
    last_dispatch: Optional["LastDispatchConfig"] = Field(
        default=None,
        description="Last dispatch target for this agent",
    )
    running: AgentsRunningConfig = Field(
        default_factory=AgentsRunningConfig,
        description="Runtime configuration",
    )
    llm_routing: AgentsLLMRoutingConfig = Field(
        default_factory=AgentsLLMRoutingConfig,
        description="LLM routing settings",
    )
    runtime: RuntimeConfig = Field(
        default_factory=RuntimeConfig,
        description="Runtime kernel selection for this agent.",
    )
    active_model: Optional["ModelSlotConfig"] = Field(
        default=None,
        description="Active model for this agent (provider_id + model)",
    )
    language: str = Field(
        default="zh",
        description="Language setting for this agent",
    )
    approval_level: str = Field(
        default="AUTO",
        description=(
            "Tool execution security level: "
            "STRICT (all tools need approval), "
            "SMART (low-risk auto-allowed), "
            "AUTO (only guarded tools), "
            "OFF (guard disabled)"
        ),
    )
    enableQuqu: bool = Field(
        default=False,
        description=(
            "Whether to inject ququ / inner monologue instructions into the "
            "agent system prompt."
        ),
    )
    system_prompt_files: List[str] = Field(
        default_factory=lambda: list(DEFAULT_AGENT_SYSTEM_PROMPT_FILES),
        description="System prompt markdown files",
    )
    tools: Optional["ToolsConfig"] = Field(
        default=None,
        description="Tools configuration for this agent",
    )
    security: Optional["SecurityConfig"] = Field(
        default=None,
        description="Security configuration for this agent",
    )
    acp: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Retired external-assistant configuration retained as opaque data "
            "for compatibility. It is never read or executed."
        ),
    )
    plan: PlanConfig = Field(
        default_factory=PlanConfig,
        description="Plan mode configuration for this agent",
    )


class AgentsConfig(BaseModel):
    """Agents configuration (root config.json only contains references)."""

    active_agent: str = Field(
        default="default",
        description="Currently active agent ID",
    )
    agent_order: List[str] = Field(
        default_factory=lambda: ["default"],
        description="Persisted UI order for configured agents",
    )
    profiles: Dict[str, AgentProfileRef] = Field(
        default_factory=lambda: {
            "default": AgentProfileRef(
                id="default",
                workspace_dir=f"{WORKING_DIR}/workspaces/default",
            ),
        },
        description="Agent profile references (ID and workspace path only)",
    )

    # Legacy fields for backward compatibility (deprecated)
    # These fields MUST have default values (not None) to support downgrade
    defaults: Optional[AgentsDefaultsConfig] = None
    running: AgentsRunningConfig = Field(
        default_factory=AgentsRunningConfig,
    )
    llm_routing: AgentsLLMRoutingConfig = Field(
        default_factory=AgentsLLMRoutingConfig,
    )
    language: str = Field(default="zh")
    installed_md_files_language: Optional[str] = None
    system_prompt_files: List[str] = Field(
        default_factory=lambda: list(DEFAULT_AGENT_SYSTEM_PROMPT_FILES),
    )


class LastDispatchConfig(BaseModel):
    """Last channel/user/session that received a user-originated reply."""

    channel: str = ""
    user_id: str = ""
    session_id: str = ""


class MCPOAuthConfig(BaseModel):
    """OAuth 2.0 Authorization Code + PKCE configuration for remote MCP."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    authorization_url: str = ""
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: List[str] = Field(default_factory=list)
    redirect_uri: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int | None = None
    token_type: str = "Bearer"
    extra_authorize_params: Dict[str, str] = Field(default_factory=dict)
    extra_token_params: Dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_enabled_oauth(self):
        if not self.enabled:
            return self
        missing = [
            label
            for label, value in (
                ("authorization_url", self.authorization_url),
                ("token_url", self.token_url),
                ("client_id", self.client_id),
            )
            if not value.strip()
        ]
        if missing:
            raise ConfigurationException(
                config_key="mcp.oauth",
                message=(
                    "enabled OAuth requires: " + ", ".join(missing)
                ),
            )
        return self


class MCPClientConfig(BaseModel):
    """Configuration for a single MCP client."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    description: str = ""
    enabled: bool = True
    revision: int = Field(
        default=0,
        ge=0,
        description="Monotonic resource revision for ordered configuration writes",
    )
    transport: Literal["stdio", "streamable_http", "sse"] = "stdio"
    url: str = ""
    allow_private_network: bool = False
    headers: Dict[str, str] = Field(default_factory=dict)
    command: str = ""
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    oauth: MCPOAuthConfig = Field(default_factory=MCPOAuthConfig)
    allowed_tools: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_fields(cls, data):
        """Normalize common MCP field aliases from third-party examples."""
        if not isinstance(data, dict):
            return data

        payload = dict(data)

        if "isActive" in payload and "enabled" not in payload:
            payload["enabled"] = payload["isActive"]

        if "baseUrl" in payload and "url" not in payload:
            payload["url"] = payload["baseUrl"]

        if "type" in payload and "transport" not in payload:
            payload["transport"] = payload["type"]

        if (
            "transport" not in payload
            and (payload.get("url") or payload.get("baseUrl"))
            and not payload.get("command")
        ):
            payload["transport"] = "streamable_http"

        raw_transport = payload.get("transport")
        if isinstance(raw_transport, str):
            normalized = raw_transport.strip().lower()
            transport_alias_map = {
                "streamablehttp": "streamable_http",
                "http": "streamable_http",
                "stdio": "stdio",
                "sse": "sse",
            }
            payload["transport"] = transport_alias_map.get(
                normalized,
                normalized,
            )

        return payload

    @model_validator(mode="after")
    def _validate_transport_config(self):
        """Validate required fields for each MCP transport type."""
        if self.transport == "stdio":
            if not self.command.strip():
                raise ConfigurationException(
                    config_key="mcp.command",
                    message="stdio MCP client requires non-empty command",
                )
            return self

        if not self.url.strip():
            raise ConfigurationException(
                config_key="mcp.url",
                message=f"{self.transport} MCP client requires non-empty url",
            )
        return self


class MCPConfig(BaseModel):
    """MCP clients configuration.

    Uses a dict to allow dynamic client definitions. External services are
    opt-in and must never appear as inert placeholders in every new profile.
    """

    clients: Dict[str, MCPClientConfig] = Field(default_factory=dict)


_LEGACY_TAVILY_CLIENT_KEY = "tavily_search"
_LEGACY_TAVILY_PLACEHOLDER = MCPClientConfig(
    name="tavily_mcp",
    enabled=False,
    command="npx",
    args=["-y", "tavily-mcp@latest"],
    env={"TAVILY_API_KEY": ""},
).model_dump(exclude_none=True)


def _retire_untouched_tavily_placeholder_from_raw(data: object) -> bool:
    """Remove only the generated disabled Tavily placeholder from raw config."""

    if not isinstance(data, dict):
        return False
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return False
    clients = mcp.get("clients")
    if not isinstance(clients, dict):
        return False
    candidate = clients.get(_LEGACY_TAVILY_CLIENT_KEY)
    if candidate != _LEGACY_TAVILY_PLACEHOLDER:
        return False
    del clients[_LEGACY_TAVILY_CLIENT_KEY]
    return True


class BuiltinToolConfig(BaseModel):
    """Configuration for a single built-in tool."""

    name: str = Field(..., description="Tool function name")
    enabled: bool = Field(
        default=True,
        description="Whether the tool is enabled",
    )
    revision: int = Field(
        default=0,
        ge=0,
        description="Monotonic resource revision for ordered state writes",
    )
    description: str = Field(default="", description="Tool description")
    display_to_user: bool = Field(
        default=True,
        description="Whether tool output is rendered to user channels",
    )
    async_execution: bool = Field(
        default=False,
        description="Whether to execute the tool asynchronously in background",
    )
    icon: str | None = Field(
        default=None,
        description="Emoji icon for the tool",
    )
    config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Tool-specific configuration (e.g., API keys)",
    )


def _default_builtin_tools() -> Dict[str, BuiltinToolConfig]:
    """Return a fresh copy of the canonical built-in tool definitions.

    This includes both hardcoded tools and dynamically registered tools
    from plugins.
    """
    tools = {
        "execute_shell_command": BuiltinToolConfig(
            name="execute_shell_command",
            enabled=True,
            async_execution=False,
            description="Execute shell commands",
            icon="💻",
        ),
        "read_file": BuiltinToolConfig(
            name="read_file",
            enabled=True,
            description="Read file contents",
            icon="📄",
        ),
        "read_skill": BuiltinToolConfig(
            name="read_skill",
            enabled=True,
            description="Read enabled skill instructions and resources",
            icon="📖",
        ),
        "list_skill_resources": BuiltinToolConfig(
            name="list_skill_resources",
            enabled=True,
            description="List logical resources bundled with an enabled Skill",
            icon="🧰",
        ),
        "copy_skill_resource": BuiltinToolConfig(
            name="copy_skill_resource",
            enabled=True,
            description="Copy a bundled Skill resource into the topic workspace",
            icon="📦",
        ),
        "run_skill_script": BuiltinToolConfig(
            name="run_skill_script",
            enabled=True,
            description="Run a script bundled with an enabled Skill",
            icon="▶️",
        ),
        "write_file": BuiltinToolConfig(
            name="write_file",
            enabled=True,
            description="Write content to file",
            icon="✍️",
        ),
        "edit_file": BuiltinToolConfig(
            name="edit_file",
            enabled=True,
            description="Edit file using find-and-replace",
            icon="🖊️",
        ),
        "grep_search": BuiltinToolConfig(
            name="grep_search",
            enabled=True,
            description="Search file contents by pattern",
            icon="🔍",
        ),
        "glob_search": BuiltinToolConfig(
            name="glob_search",
            enabled=True,
            description="Find files matching a glob pattern",
            icon="📁",
        ),
        "browser_use": BuiltinToolConfig(
            name="browser_use",
            enabled=True,
            description="Browser automation and web interaction",
            icon="🌐",
        ),
        "browser_find": BuiltinToolConfig(
            name="browser_find",
            enabled=True,
            description="Find compact matches in the current browser snapshot",
            icon="🔎",
        ),
        "browser_workbench": BuiltinToolConfig(
            name="browser_workbench",
            enabled=True,
            description="Agentic browser workbench with profiles, policy, and replay",
            icon="🧭",
        ),
        "browser_task": BuiltinToolConfig(
            name="browser_task",
            enabled=True,
            description="Observable background browser tasks with replay and confirmation",
            icon="🧭",
        ),
        "desktop_screenshot": BuiltinToolConfig(
            name="desktop_screenshot",
            enabled=True,
            description="Capture desktop screenshots",
            icon="📸",
        ),
        "mac_inspect": BuiltinToolConfig(
            name="mac_inspect",
            enabled=True,
            description="Inspect Mac applications and available windows",
            icon="🖥️",
        ),
        "mac_observe": BuiltinToolConfig(
            name="mac_observe",
            enabled=True,
            description="Read one selected Mac application window",
            icon="👁️",
        ),
        "mac_act": BuiltinToolConfig(
            name="mac_act",
            enabled=True,
            description="Perform an authorized action in a selected Mac application",
            icon="🖱️",
        ),
        "mac_verify": BuiltinToolConfig(
            name="mac_verify",
            enabled=True,
            description="Verify the result of a Mac application action",
            icon="✅",
        ),
        "mac_session": BuiltinToolConfig(
            name="mac_session",
            enabled=True,
            description="Pause, resume, stop, or inspect Mac application control",
            icon="⏹️",
        ),
        "view_image": BuiltinToolConfig(
            name="view_image",
            enabled=True,
            description="Load an image into LLM context for visual analysis",
            display_to_user=False,
            icon="🖼️",
        ),
        "view_video": BuiltinToolConfig(
            name="view_video",
            enabled=True,
            description="Load a video into LLM context for visual analysis",
            display_to_user=False,
            icon="🎥",
        ),
        "generate_image": BuiltinToolConfig(
            name="generate_image",
            enabled=True,
            description="Generate images directly inside the current chat",
            icon="🖼️",
        ),
        "generate_video": BuiltinToolConfig(
            name="generate_video",
            enabled=True,
            description="Generate videos for explicit user video tasks",
            icon="🎬",
        ),
        "send_file_to_user": BuiltinToolConfig(
            name="send_file_to_user",
            enabled=True,
            description="Send files to user",
            icon="📤",
        ),
        "get_current_time": BuiltinToolConfig(
            name="get_current_time",
            enabled=True,
            description="Get current date and time",
            icon="🕐",
        ),
        "set_user_timezone": BuiltinToolConfig(
            name="set_user_timezone",
            enabled=True,
            description="Set user timezone",
            icon="🌍",
        ),
        "get_token_usage": BuiltinToolConfig(
            name="get_token_usage",
            enabled=True,
            description="Get llm token usage",
            icon="📊",
        ),
        "prepare_scheduled_task": BuiltinToolConfig(
            name="prepare_scheduled_task",
            enabled=True,
            description=(
                "Prepare a user-confirmed scheduled task draft from natural "
                "language"
            ),
            icon="⏰",
        ),
        "list_agents": BuiltinToolConfig(
            name="list_agents",
            enabled=True,
            description=(
                "List configured agents before choosing a specialist for "
                "non-trivial collaboration"
            ),
            icon="🤖",
        ),
        "chat_with_agent": BuiltinToolConfig(
            name="chat_with_agent",
            enabled=True,
            description=(
                "Consult another configured agent and wait for its reply; "
                "prefer this for non-trivial code, design, research, strategy, "
                "risk, copywriting, or review tasks"
            ),
            icon="💬",
        ),
        "collaborate_with_agents": BuiltinToolConfig(
            name="collaborate_with_agents",
            enabled=True,
            description=(
                "Consult 2-4 independent specialist agents concurrently and "
                "return one stable collaboration batch"
            ),
            icon="👥",
        ),
        "submit_to_agent": BuiltinToolConfig(
            name="submit_to_agent",
            enabled=True,
            description=(
                "Submit independent background work to another configured "
                "agent for broader multi-part tasks"
            ),
            icon="📨",
        ),
        "check_agent_task": BuiltinToolConfig(
            name="check_agent_task",
            enabled=True,
            description="Check the status of a background agent task",
            icon="⏳",
        ),
        "wiki_search": BuiltinToolConfig(
            name="wiki_search",
            enabled=True,
            description="Search the local Wiki knowledge base with citations",
            icon="📚",
        ),
        "wiki_read": BuiltinToolConfig(
            name="wiki_read",
            enabled=True,
            description="Read a public Wiki page or raw source returned by search",
            icon="📖",
        ),
        "wiki_graph_neighbors": BuiltinToolConfig(
            name="wiki_graph_neighbors",
            enabled=True,
            description="Read neighboring pages and sources in the local Wiki graph",
            icon="🕸️",
        ),
        "wiki_ingest_status": BuiltinToolConfig(
            name="wiki_ingest_status",
            enabled=True,
            description="Read local Wiki ingest queue status",
            icon="📥",
        ),
    }

    # Merge dynamically registered tools from plugins
    try:
        from ..plugins.registry import PluginRegistry

        registry = PluginRegistry()
        # Access manifests via public method
        all_manifests = registry.get_all_plugin_manifests()
        for tool_name, registration in registry.get_all_tools().items():
            if tool_name in tools:
                continue
            manifest = all_manifests.get(registration.plugin_id, {})
            meta = manifest.get("meta", {})
            tools[tool_name] = BuiltinToolConfig(
                name=tool_name,
                enabled=False,
                description=(
                    registration.spec.description
                    or f"Tool from plugin {registration.plugin_id}"
                ),
                display_to_user=True,
                async_execution=registration.spec.async_execution,
                icon=meta.get("tool_icon", "🔧"),
            )
        for plugin_id, manifest in all_manifests.items():
            meta = manifest.get("meta", {})
            if meta.get("tool_name"):
                tool_name = meta["tool_name"]
                if tool_name not in tools:
                    tools[tool_name] = BuiltinToolConfig(
                        name=tool_name,
                        enabled=False,
                        description=meta.get(
                            "tool_description",
                            f"Tool from plugin {plugin_id}",
                        ),
                        display_to_user=True,
                        async_execution=False,
                        icon=meta.get("tool_icon", "🔧"),
                    )
    except Exception:
        # Plugins not loaded yet, return hardcoded tools only
        pass

    return tools


class ToolsConfig(BaseModel):
    """Built-in tools management configuration."""

    builtin_tools: Dict[str, BuiltinToolConfig] = Field(
        default_factory=_default_builtin_tools,
    )
    shell_async_default_enabled_applied: bool = Field(
        default=True,
        description=(
            "Legacy migration marker for the old shell async default."
        ),
    )
    shell_sync_default_applied: bool = Field(
        default=False,
        description=(
            "Internal migration marker for the safe shell sync default."
        ),
    )

    @model_validator(mode="after")
    def _merge_default_tools(self):
        """Ensure new code-defined tools are present in saved configs.

        Also normalises legacy entries whose ``icon`` is ``None`` so that
        downstream serialisation (e.g. ``ToolInfo``) never receives a null
        icon value.
        """
        defaults = _default_builtin_tools()
        for retired_tool_name in (
            "generate_image_with_provider",
            "delegate_external_agent",
            "sessions_spawn",
        ):
            self.builtin_tools.pop(retired_tool_name, None)
        for name, tc in defaults.items():
            if name not in self.builtin_tools:
                self.builtin_tools[name] = tc
            elif self.builtin_tools[name].icon is None:
                self.builtin_tools[name].icon = tc.icon
        if not self.shell_sync_default_applied:
            shell = self.builtin_tools.get("execute_shell_command")
            if shell is not None and shell.async_execution:
                shell.async_execution = False
            self.shell_sync_default_applied = True
        # Normalise legacy/stale entries not in the current defaults
        for name, tc in self.builtin_tools.items():
            if name not in defaults and tc.icon is None:
                tc.icon = ""
        return self


def _apply_shell_sync_default_migration_to_raw(data: dict) -> bool:
    """Reset legacy shell async defaults once in raw config data.

    Shell background execution is still user-configurable, but it should not be
    the default for chat agents: models may start an async command and finish
    the response before calling ``wait_task``, which looks like a truncated
    answer to users.
    """

    tools = data.get("tools")
    if not isinstance(tools, dict):
        return False
    if tools.get("shell_sync_default_applied") is True:
        if tools.get("shell_async_default_enabled_applied") is not True:
            tools["shell_async_default_enabled_applied"] = True
            return True
        return False

    builtin_tools = tools.get("builtin_tools")
    if not isinstance(builtin_tools, dict):
        return False

    shell = builtin_tools.get("execute_shell_command")
    if not isinstance(shell, dict):
        return False

    if shell.get("async_execution") is True:
        shell["async_execution"] = False
    tools["shell_async_default_enabled_applied"] = True
    tools["shell_sync_default_applied"] = True
    return True


def _apply_shell_async_default_migration_to_raw(data: dict) -> bool:
    """Backward-compatible alias for the safe shell sync migration."""

    return _apply_shell_sync_default_migration_to_raw(data)


def build_qa_agent_tools_config() -> ToolsConfig:
    """Tools preset for builtin ``default_qa_agent`` (first workspace init).

    Only these are enabled: execute_shell_command, read_file, edit_file,
    write_file, view_image. All other built-ins are disabled.
    """
    allow = frozenset(
        {
            "execute_shell_command",
            "read_file",
            "write_file",
            "edit_file",
            "view_image",
        },
    )
    builtin_tools = {
        name: tc.model_copy(update={"enabled": name in allow})
        for name, tc in _default_builtin_tools().items()
    }
    return ToolsConfig(builtin_tools=builtin_tools)


def build_local_agent_tools_config() -> ToolsConfig:
    """Tools preset for local collaborative agents.

    Inter-agent coordination tools are enabled by default, along with
    execute_shell_command and file read/write/edit tools, so a local small
    model can escalate planning work while still handling basic workspace
    actions. All other built-ins are disabled.
    """
    allow = frozenset(
        {
            "list_agents",
            "chat_with_agent",
            "collaborate_with_agents",
            "submit_to_agent",
            "check_agent_task",
            "execute_shell_command",
            "read_file",
            "write_file",
            "edit_file",
        },
    )
    builtin_tools = {
        name: tc.model_copy(update={"enabled": name in allow})
        for name, tc in _default_builtin_tools().items()
    }
    return ToolsConfig(builtin_tools=builtin_tools)


class ToolGuardRuleConfig(BaseModel):
    """A single user-defined guard rule (stored in config.json)."""

    id: str
    tools: List[str] = Field(default_factory=list)
    params: List[str] = Field(default_factory=list)
    category: str = "command_injection"
    severity: str = "HIGH"
    patterns: List[str] = Field(default_factory=list)
    exclude_patterns: List[str] = Field(default_factory=list)
    description: str = ""
    remediation: str = ""


class ToolGuardCommandRuleConfig(BaseModel):
    """A user-facing allow/ask/deny rule for tool and shell permissions."""

    id: str = ""
    enabled: bool = True
    effect: Literal["allow", "ask", "deny"] = "ask"
    tool_name: str = "execute_shell_command"
    command_pattern: str = ""
    path_pattern: str = ""
    description: str = ""
    source: str = "user"


def _default_shell_evasion_checks() -> Dict[str, bool]:
    """Return default shell-evasion checks (all disabled at startup)."""
    return {
        "command_substitution": False,
        "obfuscated_flags": False,
        "backslash_escaped_whitespace": False,
        "backslash_escaped_operators": False,
        "newlines": False,
        "comment_quote_desync": False,
        "quoted_newline": False,
    }


class ToolGuardRememberedApprovalConfig(BaseModel):
    """A user-approved tool-policy prompt remembered for one workspace."""

    id: str = ""
    enabled: bool = True
    scope: Literal["workspace"] = "workspace"
    workspace_dir: str = ""
    workspace_grant_id: str = ""
    workspace_device: int | None = None
    workspace_inode: int | None = None
    policy_version: int = 1
    tool_name: str = ""
    permission_profile: str = "workspace"
    matched_rule_ids: List[str] = Field(default_factory=list)
    command_head: str = ""
    command_pattern: str = ""
    path_pattern: str = ""
    risk_level: str = "medium"
    approval_copy: str = ""
    policy_decision: Dict[str, Any] = Field(default_factory=dict)
    source_agent_id: str = ""
    created_at: str = ""
    updated_at: str = ""


class ToolGuardConfig(BaseModel):
    """Tool guard settings under ``security.tool_guard``.

    ``guarded_tools``: ``None`` → use built-in default set; empty list → guard
    nothing; non-empty list → guard only those tools.
    """

    enabled: bool = True
    permission_mode: Literal[
        "default",
        "plan",
        "accept_edits",
        "quiet",
        "full_auto",
    ] = "quiet"
    default_workspace_dir: str = ""
    guarded_tools: Optional[List[str]] = None
    denied_tools: List[str] = Field(default_factory=list)
    auto_denied_rules: List[str] = Field(default_factory=list)
    custom_rules: List[ToolGuardRuleConfig] = Field(default_factory=list)
    command_rules: List[ToolGuardCommandRuleConfig] = Field(default_factory=list)
    disabled_rules: List[str] = Field(default_factory=list)
    remembered_approvals: List[ToolGuardRememberedApprovalConfig] = Field(
        default_factory=list,
    )
    shell_evasion_checks: Dict[str, bool] = Field(
        default_factory=_default_shell_evasion_checks,
    )


class FileGuardConfig(BaseModel):
    """File guard settings under ``security.file_guard``."""

    enabled: bool = True
    sensitive_files: List[str] = Field(default_factory=list)


class SkillScannerWhitelistEntry(BaseModel):
    """A whitelisted skill (identified by name + content hash)."""

    skill_name: str
    content_hash: str = Field(
        default="",
        description="SHA-256 of concatenated file contents at whitelist time. "
        "Empty string means any content is allowed.",
    )
    added_at: str = Field(
        default="",
        description="ISO 8601 timestamp when the entry was added.",
    )


class SkillScannerConfig(BaseModel):
    """Skill scanner settings under ``security.skill_scanner``.

    ``mode`` controls the scanner behavior:
    * ``"block"`` – scan and block unsafe skills.
    * ``"warn"``  – scan but only log warnings, do not block (default).
    * ``"off"``   – disable scanning entirely.
    """

    mode: Literal["block", "warn", "off"] = Field(
        default="warn",
        description="Scanner mode: block, warn, or off.",
    )
    timeout: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Max seconds to wait for a scan to complete.",
    )
    whitelist: List[SkillScannerWhitelistEntry] = Field(
        default_factory=list,
        description="Skills that bypass security scanning.",
    )


class SecurityConfig(BaseModel):
    """Top-level ``security`` section in config.json."""

    tool_guard: ToolGuardConfig = Field(default_factory=ToolGuardConfig)
    file_guard: FileGuardConfig = Field(default_factory=FileGuardConfig)
    skill_scanner: SkillScannerConfig = Field(
        default_factory=SkillScannerConfig,
    )
    allow_no_auth_hosts: List[str] = Field(
        default_factory=lambda: ["127.0.0.1", "::1"],
        description=(
            "List of client IP addresses that can access API endpoints "
            "without authentication. By default, localhost addresses "
            "(127.0.0.1 for IPv4, ::1 for IPv6) are allowed. "
            "WARNING: Only add trusted IP addresses to this list."
        ),
    )


class BrowserWorkbenchConfig(BaseModel):
    """Agentic browser workbench settings."""

    agent_control_enabled: bool = True
    agent_loop_enabled: bool = False
    agent_loop_mode: Literal["off", "fixture", "read_canary", "interactive"] = (
        "off"
    )
    agent_loop_budget_profile: Literal[
        "quick",
        "standard",
        "long_read",
        "draft_write",
    ] = "standard"
    default_profile_id: str = "default"
    require_confirmation_for_write: bool = True
    enable_scheduled_browser_tasks: bool = False
    primary_runtime: Literal[
        "electron_bridge",
        "browser_skill",
        "playwright_fallback",
    ] = "electron_bridge"
    optional_engines_enabled: bool = False


class Config(BaseModel):
    """Root config (config.json)."""

    channels: ChannelConfig = ChannelConfig()
    mcp: MCPConfig = MCPConfig()
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    last_api: LastApiConfig = LastApiConfig()
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    last_dispatch: Optional[LastDispatchConfig] = None
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    browser: BrowserWorkbenchConfig = Field(default_factory=BrowserWorkbenchConfig)
    utility_intelligence: UtilityIntelligenceConfig = Field(
        default_factory=UtilityIntelligenceConfig,
    )
    image_generation: ImageGenerationConfig = Field(
        default_factory=ImageGenerationConfig,
    )
    video_generation: VideoGenerationConfig = Field(
        default_factory=VideoGenerationConfig,
    )
    acp: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Retired external-assistant configuration retained as opaque data "
            "for compatibility. It is never read or executed."
        ),
    )
    show_tool_details: bool = True
    user_timezone: str = Field(
        default_factory=detect_system_timezone,
        description="User IANA timezone (e.g. Asia/Shanghai). "
        "Defaults to the system timezone.",
    )
    plugins: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Plugin configurations. Key is plugin_id, "
        "value is plugin-specific config dict.",
    )


ChannelConfigUnion = Union[
    IMessageChannelConfig,
    DiscordConfig,
    SlackConfig,
    DingTalkConfig,
    FeishuConfig,
    QQConfig,
    TelegramConfig,
    MattermostConfig,
    MQTTConfig,
    ConsoleConfig,
    MatrixConfig,
    VoiceChannelConfig,
    SIPChannelConfig,
    WecomConfig,
    OneBotConfig,
    YuanbaoConfig,
    XiaoYiConfig,
    WeChatConfig,
]


# Agent configuration utility functions


def build_fallback_agent_profile_config(
    agent_id: str,
    config: "Config",
) -> AgentProfileConfig:
    """Build the same profile as when ``agent.json``
    is missing (no disk read/write).

    Used by :func:`load_agent_config` and ``lantai doctor fix``
    so defaults stay in sync.
    """
    if agent_id not in config.agents.profiles:
        raise ValueError(f"Agent '{agent_id}' not found in config")

    agent_ref = config.agents.profiles[agent_id]
    workspace_dir = Path(agent_ref.workspace_dir).expanduser()
    return AgentProfileConfig(
        id=agent_id,
        name=agent_id.title(),
        description=f"{agent_id} agent",
        workspace_dir=str(workspace_dir),
        channels=(
            config.channels
            if hasattr(config, "channels") and config.channels
            else None
        ),
        mcp=config.mcp if hasattr(config, "mcp") and config.mcp else None,
        tools=(
            config.tools if hasattr(config, "tools") and config.tools else None
        ),
        security=(
            config.security
            if hasattr(config, "security") and config.security
            else None
        ),
        running=(
            config.agents.running
            if hasattr(config.agents, "running") and config.agents.running
            else AgentsRunningConfig()
        ),
        llm_routing=(
            config.agents.llm_routing
            if hasattr(config.agents, "llm_routing")
            and config.agents.llm_routing
            else AgentsLLMRoutingConfig()
        ),
        runtime=(
            config.runtime
            if hasattr(config, "runtime") and config.runtime
            else RuntimeConfig()
        ),
        system_prompt_files=(
            [
                str(item)
                for item in config.agents.system_prompt_files
                if str(item) not in _DYNAMIC_WORKSPACE_PROMPT_FILES
            ]
            if hasattr(config.agents, "system_prompt_files")
            and config.agents.system_prompt_files
            else list(DEFAULT_AGENT_SYSTEM_PROMPT_FILES)
        ),
    )


def _agent_raw_uses_kimi_coding(data: dict) -> bool:
    """Return whether raw agent config targets Kimi Code Plan."""
    active_model = data.get("active_model")
    if not isinstance(active_model, dict):
        return False
    provider_id = str(active_model.get("provider_id") or "").strip()
    model = str(active_model.get("model") or "").strip()
    return provider_id == "kimi-codingplan" or model == "kimi-for-coding"


def _apply_agent_context_window_migration_to_raw(data: dict) -> bool:
    """Promote every untouched legacy 128K Agent default to 256K once."""

    migrations = data.get("runtime_migrations")
    if not isinstance(migrations, list):
        migrations = []
    if _AGENT_CONTEXT_WINDOW_256K_MIGRATION in migrations:
        return False

    running = data.setdefault("running", {})
    if not isinstance(running, dict):
        return False
    try:
        current_max = int(
            running.get(
                "max_input_length",
                LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH,
            ),
        )
    except (TypeError, ValueError):
        current_max = LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH
    if current_max == LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH:
        running["max_input_length"] = DEFAULT_AGENT_MAX_INPUT_LENGTH

    migrations.append(_AGENT_CONTEXT_WINDOW_256K_MIGRATION)
    data["runtime_migrations"] = migrations
    return True


def _apply_kimi_context_window_migration_to_raw(data: dict) -> bool:
    """Promote old Kimi Code Plan agent context defaults from 128K to 256K.

    The previous app default was 128K. Kimi Code Plan documents a 256K
    context window, so existing Kimi agents that still carry the old generated
    default should move up. Agents with a user-custom value are left untouched
    but marked, so a later manual 128K choice is not treated as stale default.
    """
    if not _agent_raw_uses_kimi_coding(data):
        return False

    migrations = data.get("runtime_migrations")
    if not isinstance(migrations, list):
        migrations = []
    if _KIMI_CONTEXT_WINDOW_256K_MIGRATION in migrations:
        return False

    running = data.setdefault("running", {})
    if not isinstance(running, dict):
        return False

    try:
        current_max = int(
            running.get(
                "max_input_length",
                LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH,
            ),
        )
    except (TypeError, ValueError):
        current_max = LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH

    if current_max == LEGACY_DEFAULT_AGENT_MAX_INPUT_LENGTH:
        running["max_input_length"] = DEFAULT_AGENT_MAX_INPUT_LENGTH

    migrations.append(_KIMI_CONTEXT_WINDOW_256K_MIGRATION)
    data["runtime_migrations"] = migrations
    return True


def _apply_native_runtime_default_migration_to_raw(data: dict) -> bool:
    """Back up and retire every persisted Legacy execution selection once."""

    migrations = data.get("runtime_migrations")
    if not isinstance(migrations, list):
        migrations = []
    runtime = data.setdefault("runtime", {})
    if not isinstance(runtime, dict):
        return False

    current_engine = _normalize_runtime_engine(runtime.get("engine"))
    if (
        _NATIVE_RUNTIME_ONLY_MIGRATION in migrations
        and current_engine == _NATIVE_RUNTIME_ENGINE
    ):
        return False
    if current_engine not in {None, _LEGACY_RUNTIME_ENGINE}:
        return False

    runtime["engine"] = _NATIVE_RUNTIME_ENGINE
    if _NATIVE_RUNTIME_DEFAULT_MIGRATION not in migrations:
        migrations.append(_NATIVE_RUNTIME_DEFAULT_MIGRATION)
    if _NATIVE_RUNTIME_ONLY_MIGRATION not in migrations:
        migrations.append(_NATIVE_RUNTIME_ONLY_MIGRATION)
    data["runtime_migrations"] = migrations
    return True


def _apply_llm_acquire_timeout_migration_to_raw(data: dict) -> bool:
    """Replace only the old generated five-minute queue timeout.

    User-custom values are preserved. The marker prevents a later manual
    choice of 300 seconds from being mistaken for the retired default.
    """

    migrations = data.get("runtime_migrations")
    if not isinstance(migrations, list):
        migrations = []
    if _LLM_ACQUIRE_TIMEOUT_30S_MIGRATION in migrations:
        return False

    running = data.setdefault("running", {})
    if not isinstance(running, dict):
        return False
    try:
        current_timeout = float(
            running.get(
                "llm_acquire_timeout",
                _LEGACY_LLM_ACQUIRE_TIMEOUT_SECONDS,
            ),
        )
    except (TypeError, ValueError):
        current_timeout = _LEGACY_LLM_ACQUIRE_TIMEOUT_SECONDS
    if current_timeout == _LEGACY_LLM_ACQUIRE_TIMEOUT_SECONDS:
        running["llm_acquire_timeout"] = LLM_ACQUIRE_TIMEOUT

    migrations.append(_LLM_ACQUIRE_TIMEOUT_30S_MIGRATION)
    data["runtime_migrations"] = migrations
    return True


def _apply_static_prompt_boundary_migration_to_raw(data: dict) -> bool:
    """Keep transient workspace state out of permanent system instructions.

    Historical profiles injected ``STATE.md`` and ``WORKBENCH.md`` on every
    turn. Those files can contain stale task state and procedural preferences,
    so an ordinary conversation could inherit a previous task's obligation to
    preflight, delegate, or produce an artifact. They remain readable workspace
    references, but are no longer ambient authority.
    """

    migrations = data.get("runtime_migrations")
    if not isinstance(migrations, list):
        migrations = []
    if _STATIC_PROMPT_BOUNDARY_MIGRATION in migrations:
        return False

    prompt_files = data.get("system_prompt_files")
    if not isinstance(prompt_files, list):
        prompt_files = list(DEFAULT_AGENT_SYSTEM_PROMPT_FILES)
    data["system_prompt_files"] = [
        str(item)
        for item in prompt_files
        if str(item) not in _DYNAMIC_WORKSPACE_PROMPT_FILES
    ]
    migrations.append(_STATIC_PROMPT_BOUNDARY_MIGRATION)
    data["runtime_migrations"] = migrations
    return True


@restore_read_fence()
def load_agent_config(agent_id: str) -> AgentProfileConfig:
    """Load agent's complete configuration from workspace/agent.json with
    mtime-based caching.

    Uses file modification time to avoid unnecessary disk reads.

    Args:
        agent_id: Agent ID to load

    Returns:
        AgentProfileConfig: Complete agent configuration

    Raises:
        ValueError: If agent ID not found in root config
    """
    from .utils import (
        load_config,
        _agent_config_cache,
        _agent_config_lock,
    )

    config = load_config()

    if agent_id not in config.agents.profiles:
        raise ConfigurationException(
            config_key="agent",
            message=f"Agent '{agent_id}' not found in config",
        )

    agent_ref = config.agents.profiles[agent_id]
    workspace_dir = Path(agent_ref.workspace_dir).expanduser()
    agent_config_path = workspace_dir / "agent.json"
    key = f"{WORKING_DIR.resolve()}:{agent_id}"

    if not agent_config_path.exists():
        fallback_config = build_fallback_agent_profile_config(agent_id, config)
        # Save for future use
        save_agent_config(agent_id, fallback_config)
        return fallback_config

    # Check mtime to see if we can use cached config
    try:
        current_mtime = file_identity(agent_config_path)
    except OSError:
        fallback_config = build_fallback_agent_profile_config(agent_id, config)
        save_agent_config(agent_id, fallback_config)
        return fallback_config

    with _agent_config_lock:
        # Return cached config if mtime hasn't changed
        if agent_id in _agent_config_cache:
            cached_config, cached_mtime = _agent_config_cache[agent_id]
            if cached_mtime == current_mtime:
                return remember_read(cached_config.model_copy(deep=True), key)

        # Need to reload config from disk
        with open(agent_config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # MCP credentials historically lived in plaintext inside agent.json.
        # Encrypt them before any other migration can rewrite the file, then
        # keep only a decrypted in-memory copy for Pydantic/runtime use.
        from ..security.mcp_credentials import (
            protect_agent_mcp_credentials,
            reveal_agent_mcp_credentials,
        )

        protected_data, credentials_migrated = protect_agent_mcp_credentials(
            data,
        )
        if credentials_migrated:
            import uuid as _uuid
            import shutil as _shutil

            backup_path = agent_config_path.with_suffix(
                f".{_uuid.uuid4().hex[:8]}.mcp-credentials.bak",
            )
            _shutil.copy2(agent_config_path, backup_path)
            with open(agent_config_path, "w", encoding="utf-8") as f:
                json.dump(protected_data, f, ensure_ascii=False, indent=2)
            try:
                current_mtime = file_identity(agent_config_path)
            except OSError:
                pass
        data = reveal_agent_mcp_credentials(protected_data)

        # One-shot migration: old generated profiles carried a disabled,
        # unconfigured Tavily MCP entry. Remove only that exact placeholder;
        # any enabled, credentialed, or otherwise customized Tavily client is
        # user configuration and must remain untouched.
        try:
            if _retire_untouched_tavily_placeholder_from_raw(data):
                import uuid as _uuid
                import shutil as _shutil

                backup_path = agent_config_path.with_suffix(
                    f".{_uuid.uuid4().hex[:8]}.tavily-placeholder.bak",
                )
                _shutil.copy2(agent_config_path, backup_path)
                protected_tavily_data, _ = protect_agent_mcp_credentials(data)
                with open(agent_config_path, "w", encoding="utf-8") as f:
                    json.dump(
                        protected_tavily_data,
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                try:
                    current_mtime = file_identity(agent_config_path)
                except OSError:
                    pass
        except OSError:
            pass

        # One-shot migration: rename legacy ``channels.weixin`` key to
        # ``channels.wechat`` and rewrite the file on disk so future loads
        # see the canonical key directly. This rewrite must happen BEFORE
        # any in-memory normalization (e.g. ~/.copaw path rewriting) so we
        # only persist the key rename, not unrelated runtime transforms.
        channels = data.get("channels")
        if isinstance(channels, dict) and "weixin" in channels:
            legacy = channels.pop("weixin")
            if "wechat" not in channels:
                channels["wechat"] = legacy
            try:
                import uuid as _uuid
                import shutil as _shutil

                backup_path = agent_config_path.with_suffix(
                    f".{_uuid.uuid4().hex[:8]}.weixin-migrate.bak",
                )
                _shutil.copy2(agent_config_path, backup_path)
                with open(
                    agent_config_path,
                    "w",
                    encoding="utf-8",
                ) as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                # Refresh mtime cache key after rewriting the file so the
                # cached config still reflects the on-disk state.
                try:
                    current_mtime = file_identity(agent_config_path)
                except OSError:
                    pass
            except OSError:
                pass

        # One-shot migration: old generated agent.json files persisted the
        # previous weak memory defaults (periodic memory disabled, automatic
        # memory search disabled). Keep legacy interval values readable, but
        # never manufacture a fixed every-N-turn maintenance schedule.
        try:
            running = data.setdefault("running", {})
            if isinstance(running, dict):
                reme_cfg = running.setdefault(
                    "reme_light_memory_config",
                    {},
                )
                if (
                    isinstance(reme_cfg, dict)
                    and not reme_cfg.get("strong_defaults_applied")
                ):
                    search_cfg = reme_cfg.setdefault(
                        "auto_memory_search_config",
                        {},
                    )
                    if isinstance(search_cfg, dict):
                        search_cfg["enabled"] = True

                    reme_cfg["recursive_file_watcher"] = True
                    reme_cfg["strong_defaults_applied"] = True

                    import uuid as _uuid
                    import shutil as _shutil

                    backup_path = agent_config_path.with_suffix(
                        f".{_uuid.uuid4().hex[:8]}.memory-defaults.bak",
                    )
                    _shutil.copy2(agent_config_path, backup_path)
                    with open(agent_config_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    try:
                        current_mtime = file_identity(agent_config_path)
                    except OSError:
                        pass
        except OSError:
            pass

        # One-shot migration: shell commands are safer as regular synchronous
        # tools by default. Older configs promoted shell to async execution,
        # which can make the model stop after only starting a background task.
        try:
            if _apply_shell_sync_default_migration_to_raw(data):
                import uuid as _uuid
                import shutil as _shutil

                backup_path = agent_config_path.with_suffix(
                    f".{_uuid.uuid4().hex[:8]}.shell-sync-defaults.bak",
                )
                _shutil.copy2(agent_config_path, backup_path)
                with open(agent_config_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                try:
                    current_mtime = file_identity(agent_config_path)
                except OSError:
                    pass
        except OSError:
            pass

        # One-shot migration: every untouched profile created under the old
        # 128K product default moves to the current 256K Agent baseline. Exact
        # provider and route capabilities still clamp the effective window.
        try:
            if _apply_agent_context_window_migration_to_raw(data):
                import uuid as _uuid
                import shutil as _shutil

                backup_path = agent_config_path.with_suffix(
                    f".{_uuid.uuid4().hex[:8]}.agent-context-256k.bak",
                )
                _shutil.copy2(agent_config_path, backup_path)
                with open(agent_config_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                try:
                    current_mtime = file_identity(agent_config_path)
                except OSError:
                    pass
        except OSError:
            pass

        # One-shot migration: a native desktop default must also reach old
        # generated profiles, otherwise their persisted legacy value silently
        # overrides the launch policy forever.
        try:
            if _apply_native_runtime_default_migration_to_raw(data):
                import uuid as _uuid
                import shutil as _shutil

                backup_path = agent_config_path.with_suffix(
                    f".{_uuid.uuid4().hex[:8]}.native-runtime-default.bak",
                )
                _shutil.copy2(agent_config_path, backup_path)
                with open(agent_config_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                try:
                    current_mtime = file_identity(agent_config_path)
                except OSError:
                    pass
        except OSError:
            pass

        # One-shot migration: the retired default allowed an ordinary chat to
        # wait silently for five minutes behind an unrelated model request.
        try:
            if _apply_llm_acquire_timeout_migration_to_raw(data):
                import uuid as _uuid
                import shutil as _shutil

                backup_path = agent_config_path.with_suffix(
                    f".{_uuid.uuid4().hex[:8]}.llm-acquire-timeout.bak",
                )
                _shutil.copy2(agent_config_path, backup_path)
                with open(agent_config_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                try:
                    current_mtime = file_identity(agent_config_path)
                except OSError:
                    pass
        except OSError:
            pass

        # One-shot migration: stale task/workbench notes are references, not
        # permanent system authority. This runs after migrations that still
        # rewrite decrypted payloads and persists a protected document.
        try:
            if _apply_static_prompt_boundary_migration_to_raw(data):
                import uuid as _uuid
                import shutil as _shutil

                backup_path = agent_config_path.with_suffix(
                    f".{_uuid.uuid4().hex[:8]}.prompt-boundary.bak",
                )
                _shutil.copy2(agent_config_path, backup_path)
                protected_prompt_data, _ = protect_agent_mcp_credentials(data)
                with open(agent_config_path, "w", encoding="utf-8") as f:
                    json.dump(
                        protected_prompt_data,
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                try:
                    current_mtime = file_identity(agent_config_path)
                except OSError:
                    pass
        except OSError:
            pass

        # Some legacy one-shot migrations above still rewrite their in-memory
        # (decrypted) payload directly. Re-protect the final on-disk document
        # so a simultaneous migration can never regress MCP secrets back to
        # plaintext. Already encrypted files are left byte-for-byte alone.
        try:
            with open(agent_config_path, "r", encoding="utf-8") as f:
                final_disk_data = json.load(f)
            protected_final, final_credentials_changed = (
                protect_agent_mcp_credentials(final_disk_data)
            )
            if final_credentials_changed:
                with open(agent_config_path, "w", encoding="utf-8") as f:
                    json.dump(
                        protected_final,
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                try:
                    current_mtime = file_identity(agent_config_path)
                except OSError:
                    pass
        except OSError:
            pass

        # Normalize legacy ~/.copaw-bound paths to current WORKING_DIR.
        # This keeps LANTAI_WORKING_DIR effective even if existing agent.json
        # contains older hard-coded paths like "~/.copaw/media".
        # NOTE: this transform is applied in-memory only; it must not be
        # persisted back to disk.
        try:
            from .utils import _normalize_working_dir_bound_paths

            data = _normalize_working_dir_bound_paths(data)
        except Exception:
            pass

        agent_config = AgentProfileConfig(**data)

        # Cache the config with its mtime
        _agent_config_cache[agent_id] = (agent_config, current_mtime)

        return remember_read(agent_config.model_copy(deep=True), key)


@restore_read_fence()
def save_agent_config(
    agent_id: str,
    agent_config: AgentProfileConfig,
) -> None:
    """Save agent configuration to workspace/agent.json and invalidate cache.

    Args:
        agent_id: Agent ID
        agent_config: Complete agent configuration to save

    Raises:
        ValueError: If agent ID not found in root config
    """
    from .utils import (
        load_config,
        _agent_config_cache,
        _agent_config_lock,
    )

    config = load_config()

    if agent_id not in config.agents.profiles:
        raise ConfigurationException(
            config_key="agent",
            message=f"Agent '{agent_id}' not found in config",
        )

    agent_ref = config.agents.profiles[agent_id]
    workspace_dir = Path(agent_ref.workspace_dir).expanduser()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    agent_config_path = workspace_dir / "agent.json"
    key = f"{WORKING_DIR.resolve()}:{agent_id}"
    if agent_config_path.is_file() and getattr(agent_config, "_lantai_config_read_snapshot", None):
        saved = merge_loaded_changes(agent_config, load_agent_config(agent_id), key)
    else:
        saved = agent_config

    from ..security.mcp_credentials import protect_agent_mcp_credentials

    payload, _ = protect_agent_mcp_credentials(
        saved.model_dump(exclude_none=True),
    )
    with open(agent_config_path, "w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
        )

    # Invalidate cache after saving
    with _agent_config_lock:
        if agent_id in _agent_config_cache:
            del _agent_config_cache[agent_id]
    acknowledge_save(agent_config, saved, key)


@restore_read_fence()
def migrate_legacy_config_to_multi_agent() -> bool:
    """Migrate legacy single-agent config to new multi-agent structure.

    Returns:
        bool: True if migration was performed, False if already migrated
    """
    from .utils import load_config, save_config

    config = load_config()

    # Check if already migrated (new structure has only AgentProfileRef)
    if "default" in config.agents.profiles:
        agent_ref = config.agents.profiles["default"]
        # If it's already a AgentProfileRef, migration done
        if isinstance(agent_ref, AgentProfileRef):
            # Check if default agent config exists
            workspace_dir = Path(agent_ref.workspace_dir).expanduser()
            agent_config_path = workspace_dir / "agent.json"
            if agent_config_path.exists():
                return False  # Already migrated

    # Perform migration
    print("Migrating legacy config to multi-agent structure...")

    # Extract legacy agent configuration
    legacy_agents = config.agents

    # Create default agent workspace
    default_workspace = Path(f"{WORKING_DIR}/workspaces/default").expanduser()
    default_workspace.mkdir(parents=True, exist_ok=True)

    # Create default agent configuration from legacy settings
    default_agent_config = AgentProfileConfig(
        id="default",
        name="Default Agent",
        description="Default 澜台 agent",
        workspace_dir=str(default_workspace),
        channels=config.channels if config.channels else None,
        mcp=config.mcp if config.mcp else None,
        heartbeat=(
            legacy_agents.defaults.heartbeat
            if legacy_agents.defaults
            else None
        ),
        running=(
            legacy_agents.running
            if legacy_agents.running
            else AgentsRunningConfig()
        ),
        llm_routing=(
            legacy_agents.llm_routing
            if legacy_agents.llm_routing
            else AgentsLLMRoutingConfig()
        ),
        runtime=config.runtime if config.runtime else RuntimeConfig(),
        system_prompt_files=(
            [
                str(item)
                for item in legacy_agents.system_prompt_files
                if str(item) not in _DYNAMIC_WORKSPACE_PROMPT_FILES
            ]
            if legacy_agents.system_prompt_files
            else list(DEFAULT_AGENT_SYSTEM_PROMPT_FILES)
        ),
        tools=config.tools if config.tools else None,
        security=config.security if config.security else None,
    )

    # Save default agent configuration to workspace
    agent_config_path = default_workspace / "agent.json"
    with open(agent_config_path, "w", encoding="utf-8") as f:
        json.dump(
            default_agent_config.model_dump(exclude_none=True),
            f,
            ensure_ascii=False,
            indent=2,
        )

    # Migrate existing workspace files from legacy default working dir.
    # When LANTAI_WORKING_DIR is customized, historical data may still exist
    # under "~/.copaw".
    old_workspace = Path("~/.copaw").expanduser().resolve()

    # Move sessions, memory, and other workspace files
    for item_name in ["sessions", "memory", "jobs.json"]:
        old_path = old_workspace / item_name
        if old_path.exists():
            new_path = default_workspace / item_name
            if not new_path.exists():
                import shutil

                if old_path.is_dir():
                    shutil.copytree(old_path, new_path)
                else:
                    shutil.copy2(old_path, new_path)
                print(f"  Migrated {item_name} to default workspace")

    # Copy markdown files (AGENTS.md, SOUL.md, PROFILE.md, BACKSTORY.md)
    for md_file in ["AGENTS.md", "SOUL.md", "PROFILE.md", "BACKSTORY.md"]:
        old_md = old_workspace / md_file
        if old_md.exists():
            new_md = default_workspace / md_file
            if not new_md.exists():
                import shutil

                shutil.copy2(old_md, new_md)
                print(f"  Migrated {md_file} to default workspace")

    # Update root config.json to new structure
    # CRITICAL: Preserve legacy agent fields for downgrade compatibility
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
        language=(
            default_agent_config.language
            if hasattr(default_agent_config, "language")
            else "zh"
        ),
        system_prompt_files=default_agent_config.system_prompt_files,
    )

    # IMPORTANT: Keep channels, mcp, tools, security in root config for
    # backward compatibility. Do NOT clear these fields.
    # Old versions expect these fields to exist with valid values.

    save_config(config)

    print("Migration completed successfully!")
    print(f"  Default agent workspace: {default_workspace}")
    print(f"  Default agent config: {agent_config_path}")

    return True
