# -*- coding: utf-8 -*-
"""A Manager class to handle all providers, including built-in and custom ones.
It provides a unified interface to manage providers, such as listing available
providers, adding/removing custom providers, and fetching provider details."""

import asyncio
from copy import deepcopy
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Dict, List

from pydantic import BaseModel

from lantai.exceptions import ModelNotFoundException

from ..config.config import ModelSlotConfig
from ..constant import SECRET_DIR
from ..exceptions import ProviderError
from ..security.cloud_account_store import (
    is_watcha_account_connected,
    load_watcha_account_context,
)
from ..security.secret_store import (
    PROVIDER_SECRET_FIELDS,
    SecretDecryptionError,
    decrypt_dict_fields,
    encrypt_dict_fields,
    is_encrypted,
)
from .anthropic_provider import AnthropicProvider
from .gemini_provider import GeminiProvider
from .lmstudio_provider import LMStudioProvider
from .ollama_provider import OllamaProvider
from .openai_provider import OpenAIProvider
from .opencode_provider import OpenCodeProvider
from .openrouter_provider import OpenRouterProvider
from .provider import (
    ModelInfo,
    Provider,
    ProviderInfo,
)
from .request_profiles import KIMI_CODING_BASE_URL, KIMI_CODING_MODEL_ID
from .provider_state import (
    STATE_VERSION,
    VERSION_KEY,
    atomic_write_provider,
    migrate_provider_file,
)
from .tokendance_authorization import (
    TOKENDANCE_BINDING_META_KEY,
    TOKENDANCE_PAYMENT_INTENTS_META_KEY,
    TOKENDANCE_PAYMENT_SESSIONS_META_KEY,
    TokenDanceAuthorization,
    TokenDanceAuthorizationError,
    assert_tokendance_authorization_current,
    resolve_tokendance_authorization,
)
from .tokendance_provider import (
    TOKENDANCE_OPENAI_BASE_URL,
    TokenDanceProvider,
)

logger = logging.getLogger(__name__)

_TOKENDANCE_PAYMENT_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TOKENDANCE_PAYMENT_INTENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_TOKENDANCE_PAYMENT_SESSION_LIMIT = 100
_TOKENDANCE_PAYMENT_INTENT_LIMIT = 100

# -------------------------------------------------------
# Built-in provider definitions and their default models.
# -------------------------------------------------------

MODELSCOPE_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="Qwen/Qwen3.5-122B-A10B",
        name="Qwen3.5-122B-A10B",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="ZhipuAI/GLM-5",
        name="GLM-5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

DASHSCOPE_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="qwen3-max",
        name="Qwen3 Max",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="qwen3-235b-a22b-thinking-2507",
        name="Qwen3 235B A22B Thinking",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="deepseek-v3.2",
        name="DeepSeek-V3.2",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

ALIYUN_TOKENPLAN_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="qwen3.6-plus",
        name="Qwen3.6 Plus",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="glm-5",
        name="GLM-5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.5",
        name="MiniMax M2.5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="deepseek-v3.2",
        name="DeepSeek-V3.2",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2.5",
        name="Kimi K2.5",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
]

ALIYUN_CODINGPLAN_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="qwen3.6-plus",
        name="Qwen3.6 Plus",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="qwen3.5-plus",
        name="Qwen3.5 Plus",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="glm-5",
        name="GLM-5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="glm-4.7",
        name="GLM-4.7",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.5",
        name="MiniMax M2.5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2.5",
        name="Kimi K2.5",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="qwen3-max-2026-01-23",
        name="Qwen3 Max 2026-01-23",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="qwen3-coder-next",
        name="Qwen3 Coder Next",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="qwen3-coder-plus",
        name="Qwen3 Coder Plus",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

ZHIPU_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="glm-5",
        name="glm-5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="glm-5.1",
        name="glm-5.1",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="glm-5-turbo",
        name="glm-5-turbo",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="glm-5v-turbo",
        name="glm-5v-turbo",
        supports_image=True,
        supports_video=False,
        probe_source="documentation",
    ),
]

OPENAI_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gpt-5.2",
        name="GPT-5.2",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-5",
        name="GPT-5",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-5-mini",
        name="GPT-5 Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-5-nano",
        name="GPT-5 Nano",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1",
        name="GPT-4.1",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1-mini",
        name="GPT-4.1 Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1-nano",
        name="GPT-4.1 Nano",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="o3",
        name="o3",
        supports_image=True,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="o4-mini",
        name="o4-mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4o",
        name="GPT-4o",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4o-mini",
        name="GPT-4o Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
]

OPENCODE_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="big-pickle",
        name="Big Pickle",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
        is_free=True,
    ),
]

AZURE_OPENAI_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gpt-5-chat",
        name="GPT-5 Chat",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-5-mini",
        name="GPT-5 Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-5-nano",
        name="GPT-5 Nano",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1",
        name="GPT-4.1",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1-mini",
        name="GPT-4.1 Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1-nano",
        name="GPT-4.1 Nano",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4o",
        name="GPT-4o",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4o-mini",
        name="GPT-4o Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
]

MINIMAX_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="MiniMax-M3",
        name="MiniMax M3",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.5",
        name="MiniMax M2.5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.5-highspeed",
        name="MiniMax M2.5 Highspeed",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.7",
        name="MiniMax M2.7",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.7-highspeed",
        name="MiniMax M2.7 Highspeed",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

KIMI_CODINGPLAN_MODELS: List[ModelInfo] = [
    ModelInfo(
        id=KIMI_CODING_MODEL_ID,
        name="Kimi K2.7 Code",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-for-coding-highspeed",
        name="Kimi K2.7 HighSpeed",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="k3-256k",
        name="Kimi K3 256K",
        supports_image=True,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="k3",
        name="Kimi K3 (up to 1M)",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
]

KIMI_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="kimi-k2.5",
        name="Kimi K2.5",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2-0905-preview",
        name="Kimi K2 0905 Preview",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2-0711-preview",
        name="Kimi K2 0711 Preview",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2-turbo-preview",
        name="Kimi K2 Turbo Preview",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2-thinking",
        name="Kimi K2 Thinking",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2-thinking-turbo",
        name="Kimi K2 Thinking Turbo",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

DEEPSEEK_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="deepseek-chat",
        name="DeepSeek Chat",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="deepseek-reasoner",
        name="DeepSeek Reasoner",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="deepseek-v4-flash",
        name="DeepSeek V4 Flash",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="deepseek-v4-pro",
        name="DeepSeek V4 Pro",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

VOLCENGINE_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="Doubao-Seed-2.0-Code",
        name="Doubao-Seed-2.0-Code",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="Doubao-Seed-2.0-pro",
        name="Doubao-Seed-2.0-pro",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="Doubao-Seed-2.0-lite",
        name="Doubao-Seed-2.0-lite",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="Doubao-Seed-Code",
        name="Doubao-Seed-Code",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="GLM-5.1",
        name="GLM-5.1",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.7",
        name="MiniMax-M2.7",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="Kimi-K2.6",
        name="Kimi-K2.6",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="Kimi-K2.5",
        name="Kimi-K2.5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="GLM-4.7",
        name="GLM-4.7",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="DeepSeek-V3.2",
        name="DeepSeek-V3.2",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

VOLCENGINE_CODINGPLAN_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="Doubao-Seed-2.0-Code",
        name="Doubao-Seed-2.0-Code",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="Doubao-Seed-2.0-pro",
        name="Doubao-Seed-2.0-pro",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="Doubao-Seed-2.0-lite",
        name="Doubao-Seed-2.0-lite",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="Doubao-Seed-Code",
        name="Doubao-Seed-Code",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="GLM-5.1",
        name="GLM-5.1",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.7",
        name="MiniMax-M2.7",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="Kimi-K2.6",
        name="Kimi-K2.6",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="Kimi-K2.5",
        name="Kimi-K2.5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="GLM-4.7",
        name="GLM-4.7",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="DeepSeek-V3.2",
        name="DeepSeek-V3.2",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

ANTHROPIC_MODELS: List[ModelInfo] = []

GEMINI_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gemini-3.1-pro-preview",
        name="Gemini 3.1 Pro Preview",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-3-flash-preview",
        name="Gemini 3 Flash Preview",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-3.1-flash-lite-preview",
        name="Gemini 3.1 Flash Lite Preview",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-2.5-pro",
        name="Gemini 2.5 Pro",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-2.5-flash",
        name="Gemini 2.5 Flash",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-2.5-flash-lite",
        name="Gemini 2.5 Flash Lite",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-2.0-flash",
        name="Gemini 2.0 Flash",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
]

PROVIDER_MODELSCOPE = OpenAIProvider(
    id="modelscope",
    name="ModelScope",
    base_url="https://api-inference.modelscope.cn/v1",
    api_key_prefix="ms",
    models=MODELSCOPE_MODELS,
    freeze_url=True,
)

PROVIDER_DASHSCOPE = OpenAIProvider(
    id="dashscope",
    name="DashScope",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_key_prefix="sk",
    models=DASHSCOPE_MODELS,
    freeze_url=True,
)

PROVIDER_ALIYUN_CODINGPLAN = OpenAIProvider(
    id="aliyun-codingplan",
    name="Aliyun Coding Plan (China)",
    base_url="https://coding.dashscope.aliyuncs.com/v1",
    api_key_prefix="sk-sp",
    models=ALIYUN_CODINGPLAN_MODELS,
    # This provider doesn't support connection check without model config
    support_connection_check=False,
    freeze_url=True,
)

PROVIDER_ALIYUN_CODINGPLAN_INTL = OpenAIProvider(
    id="aliyun-codingplan-intl",
    name="Aliyun Coding Plan (International)",
    base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
    api_key_prefix="sk-sp",
    models=ALIYUN_CODINGPLAN_MODELS,
    # This provider doesn't support connection check without model config
    support_connection_check=False,
    freeze_url=True,
)

PROVIDER_ALIYUN_TOKENPLAN = OpenAIProvider(
    id="aliyun-tokenplan",
    name="Aliyun Token Plan",
    base_url=(
        "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    ),
    api_key_prefix="sk-sp",
    models=ALIYUN_TOKENPLAN_MODELS,
    support_connection_check=False,
    freeze_url=True,
)

PROVIDER_ZHIPU_CN = OpenAIProvider(
    id="zhipu-cn",
    name="Zhipu (BigModel)",
    base_url="https://open.bigmodel.cn/api/paas/v4",
    api_key_prefix="",
    models=ZHIPU_MODELS,
    freeze_url=True,
)

PROVIDER_ZHIPU_CN_CODINGPLAN = OpenAIProvider(
    id="zhipu-cn-codingplan",
    name="Zhipu Coding Plan (BigModel)",
    base_url="https://open.bigmodel.cn/api/coding/paas/v4",
    api_key_prefix="",
    models=ZHIPU_MODELS,
    freeze_url=True,
    support_connection_check=False,
)

PROVIDER_ZHIPU_INTL = OpenAIProvider(
    id="zhipu-intl",
    name="Zhipu (Z.AI)",
    base_url="https://api.z.ai/api/paas/v4",
    api_key_prefix="",
    models=ZHIPU_MODELS,
    freeze_url=True,
)

PROVIDER_ZHIPU_INTL_CODINGPLAN = OpenAIProvider(
    id="zhipu-intl-codingplan",
    name="Zhipu Coding Plan (Z.AI)",
    base_url="https://api.z.ai/api/coding/paas/v4",
    api_key_prefix="",
    models=ZHIPU_MODELS,
    freeze_url=True,
    support_connection_check=False,
)

PROVIDER_LANTAI_LOCAL = OpenAIProvider(
    id="lantai-local",
    name="Lantai Local",
    is_local=True,
    require_api_key=False,
)

PROVIDER_OPENAI = OpenAIProvider(
    id="openai",
    name="OpenAI",
    base_url="https://api.openai.com/v1",
    api_key_prefix="sk-",
    models=OPENAI_MODELS,
    freeze_url=True,
)

PROVIDER_OPENCODE = OpenCodeProvider(
    id="opencode",
    name="OpenCode",
    base_url="https://opencode.ai/zen/v1",
    api_key_prefix="",
    models=OPENCODE_MODELS,
    freeze_url=True,
    require_api_key=False,
    support_model_discovery=True,
)

PROVIDER_AZURE_OPENAI = OpenAIProvider(
    id="azure-openai",
    name="Azure OpenAI",
    api_key_prefix="",
    models=AZURE_OPENAI_MODELS,
)

PROVIDER_MINIMAX = AnthropicProvider(
    id="minimax",
    name="MiniMax (International)",
    base_url="https://api.minimax.io/anthropic",
    models=MINIMAX_MODELS,
    chat_model="AnthropicChatModel",
    freeze_url=True,
    support_connection_check=True,
)

PROVIDER_MINIMAX_CN = AnthropicProvider(
    id="minimax-cn",
    name="MiniMax (China)",
    base_url="https://api.minimaxi.com/anthropic",
    models=MINIMAX_MODELS,
    chat_model="AnthropicChatModel",
    freeze_url=True,
    support_connection_check=True,
)

PROVIDER_KIMI_CODINGPLAN = OpenAIProvider(
    id="kimi-codingplan",
    name="Kimi Code Plan",
    base_url=KIMI_CODING_BASE_URL,
    api_key_prefix="",
    models=KIMI_CODINGPLAN_MODELS,
    freeze_url=True,
    support_model_discovery=False,
    support_connection_check=True,
)

PROVIDER_KIMI_CN = OpenAIProvider(
    id="kimi-cn",
    name="Kimi (China)",
    base_url="https://api.moonshot.cn/v1",
    api_key_prefix="",
    models=KIMI_MODELS,
    freeze_url=True,
)

PROVIDER_KIMI_INTL = OpenAIProvider(
    id="kimi-intl",
    name="Kimi (International)",
    base_url="https://api.moonshot.ai/v1",
    api_key_prefix="",
    models=KIMI_MODELS,
    freeze_url=True,
)

PROVIDER_DEEPSEEK = OpenAIProvider(
    id="deepseek",
    name="DeepSeek",
    base_url="https://api.deepseek.com",
    api_key_prefix="sk-",
    models=DEEPSEEK_MODELS,
    freeze_url=True,
)

PROVIDER_ANTHROPIC = AnthropicProvider(
    id="anthropic",
    name="Anthropic",
    base_url="https://api.anthropic.com",
    api_key_prefix="sk-ant-",
    models=ANTHROPIC_MODELS,
    chat_model="AnthropicChatModel",
    freeze_url=True,
)

PROVIDER_GEMINI = GeminiProvider(
    id="gemini",
    name="Google Gemini",
    base_url="https://generativelanguage.googleapis.com",
    api_key_prefix="",
    models=GEMINI_MODELS,
    chat_model="GeminiChatModel",
    freeze_url=True,
)

PROVIDER_OLLAMA = OllamaProvider(
    id="ollama",
    name="Ollama",
    is_local=True,
    require_api_key=False,
    support_model_discovery=True,
    generate_kwargs={"max_tokens": None},
    meta={
        "docs_url": "https://docs.ollama.com/quickstart",
        "provider_kind": "local_runtime",
    },
)

PROVIDER_OPENROUTER = OpenRouterProvider(
    id="openrouter",
    name="OpenRouter",
    base_url="https://openrouter.ai/api/v1",
    api_key_prefix="sk-or-v1-",
    models=[],
    freeze_url=True,
)

PROVIDER_TOKENDANCE = TokenDanceProvider(
    id="tokendance",
    name="TokenDance",
    base_url=TOKENDANCE_OPENAI_BASE_URL,
    api_key_prefix="",
    models=[],
    freeze_url=True,
    require_api_key=True,
    support_model_discovery=True,
    support_connection_check=False,
    meta={
        "docs_url": "https://tokendance.space/docs/quickstart",
        "protocol_docs_url": "https://tokendance.space/docs/multi-protocol",
        "api_key_url": "https://tokendance.space/keys",
        "oauth_mode": "api_key_pkce",
        "oauth_app_url": "https://lantai.guanlan.xin",
    },
)

PROVIDER_LMSTUDIO = LMStudioProvider(
    id="lmstudio",
    name="LM Studio",
    is_local=True,
    base_url="http://localhost:1234/v1",
    require_api_key=False,
    api_key_prefix="",
    support_model_discovery=True,
    generate_kwargs={"max_tokens": None},
    meta={
        "docs_url": "https://lmstudio.ai/docs/developer/core/server",
        "provider_kind": "local_runtime",
    },
)

PROVIDER_SILICONFLOW_CN = OpenAIProvider(
    id="siliconflow-cn",
    name="SiliconFlow (China)",
    base_url="https://api.siliconflow.cn/v1",
    api_key_prefix="sk-",
    models=[],
    freeze_url=True,
    require_api_key=True,
)

PROVIDER_SILICONFLOW_INTL = OpenAIProvider(
    id="siliconflow-intl",
    name="SiliconFlow (International)",
    base_url="https://api.siliconflow.com/v1",
    api_key_prefix="sk-",
    models=[],
    freeze_url=True,
    require_api_key=True,
)

PROVIDER_VOLCENGINE_CN = OpenAIProvider(
    id="volcengine-cn",
    name="Volcano Engine",
    base_url="https://ark.cn-beijing.volces.com/api/v3",
    api_key_prefix="",
    models=VOLCENGINE_MODELS,
    freeze_url=True,
)

PROVIDER_VOLCENGINE_CN_CODINGPLAN = OpenAIProvider(
    id="volcengine-cn-codingplan",
    name="Volcano Engine Coding Plan",
    base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
    api_key_prefix="",
    models=VOLCENGINE_CODINGPLAN_MODELS,
    support_connection_check=False,
    freeze_url=True,
)

AUTO_RECOVERY_HIDDEN_PROVIDER_IDS = {"opencode"}


class ProviderManager:  # pylint: disable=too-many-public-methods
    """A manager class to handle all providers,
    including built-in and custom ones."""

    _instance = None

    def __init__(self) -> None:
        # Initialize provider manager, load providers from registry and store
        # any necessary state (e.g., cached models).
        self.builtin_providers: Dict[str, Provider] = {}
        self.custom_providers: Dict[str, Provider] = {}
        self.plugin_providers: Dict[str, Dict] = {}  # Plugin providers
        self.active_model: ModelSlotConfig | None = None
        self.root_path = SECRET_DIR / "providers"
        self.builtin_path = self.root_path / "builtin"
        self.custom_path = self.root_path / "custom"
        self.plugin_path = self.root_path / "plugin"  # Plugin provider configs
        self._prepare_disk_storage()
        self._init_builtins()
        try:
            self._migrate_legacy_providers()
        except Exception as e:
            logger.warning("Failed to migrate legacy providers: %s", e)
        self._init_from_storage()
        self._apply_default_annotations()

    def _prepare_disk_storage(self):
        """Prepare directory structure"""
        for path in [
            self.root_path,
            self.builtin_path,
            self.custom_path,
            self.plugin_path,
        ]:
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)  # Restrict permissions for security
            except Exception:
                pass

    def _init_builtins(self):
        self._add_builtin(PROVIDER_LANTAI_LOCAL)
        self._add_builtin(PROVIDER_OLLAMA)
        self._add_builtin(PROVIDER_LMSTUDIO)
        self._add_builtin(PROVIDER_OPENROUTER)
        self._add_builtin(PROVIDER_TOKENDANCE)
        self._add_builtin(PROVIDER_MODELSCOPE)
        self._add_builtin(PROVIDER_DASHSCOPE)
        self._add_builtin(PROVIDER_ALIYUN_CODINGPLAN)
        self._add_builtin(PROVIDER_ALIYUN_CODINGPLAN_INTL)
        self._add_builtin(PROVIDER_ALIYUN_TOKENPLAN)
        self._add_builtin(PROVIDER_OPENCODE)
        self._add_builtin(PROVIDER_OPENAI)
        self._add_builtin(PROVIDER_AZURE_OPENAI)
        self._add_builtin(PROVIDER_ANTHROPIC)
        self._add_builtin(PROVIDER_GEMINI)
        self._add_builtin(PROVIDER_DEEPSEEK)
        self._add_builtin(PROVIDER_KIMI_CODINGPLAN)
        self._add_builtin(PROVIDER_KIMI_CN)
        self._add_builtin(PROVIDER_KIMI_INTL)
        self._add_builtin(PROVIDER_MINIMAX_CN)
        self._add_builtin(PROVIDER_MINIMAX)
        self._add_builtin(PROVIDER_ZHIPU_CN)
        self._add_builtin(PROVIDER_ZHIPU_CN_CODINGPLAN)
        self._add_builtin(PROVIDER_ZHIPU_INTL)
        self._add_builtin(PROVIDER_ZHIPU_INTL_CODINGPLAN)
        self._add_builtin(PROVIDER_SILICONFLOW_CN)
        self._add_builtin(PROVIDER_SILICONFLOW_INTL)
        self._add_builtin(PROVIDER_VOLCENGINE_CN)
        self._add_builtin(PROVIDER_VOLCENGINE_CN_CODINGPLAN)

    def _add_builtin(self, provider: Provider):
        self.builtin_providers[provider.id] = provider.model_copy(deep=True)

    async def list_provider_info(self) -> List[ProviderInfo]:
        tasks = [
            provider.get_info() for provider in self.builtin_providers.values()
        ]
        tasks += [
            provider.get_info() for provider in self.custom_providers.values()
        ]
        # Add plugin providers - directly return their ProviderInfo
        for plugin_provider in self.plugin_providers.values():
            provider_info = plugin_provider["info"]
            # Plugin providers store ProviderInfo directly, no need to
            # instantiate
            tasks.append(self._get_plugin_provider_info(provider_info))

        provider_infos = await asyncio.gather(*tasks)
        return [self._public_provider_info(info) for info in provider_infos]

    def _public_provider_info(self, info: ProviderInfo) -> ProviderInfo:
        """Project private TokenDance authorization state to a safe status."""

        if info.id != "tokendance":
            return info

        provider = self.get_provider("tokendance")
        if provider is None or not str(provider.api_key or "").strip():
            credential_status = "disconnected"
        else:
            try:
                resolve_tokendance_authorization(self, use_kind="model")
                credential_status = "active"
            except TokenDanceAuthorizationError as exc:
                credential_status = (
                    "wrong_account"
                    if exc.code == "credential_owner_mismatch"
                    else "reauthorization_required"
                )

        public_meta = dict(info.meta)
        public_meta.pop(TOKENDANCE_BINDING_META_KEY, None)
        public_meta.pop(TOKENDANCE_PAYMENT_SESSIONS_META_KEY, None)
        public_meta.pop(TOKENDANCE_PAYMENT_INTENTS_META_KEY, None)
        public_meta["credential_status"] = credential_status
        return info.model_copy(update={"meta": public_meta})

    async def _get_plugin_provider_info(
        self,
        provider_info: ProviderInfo,
    ) -> ProviderInfo:
        """Helper to return plugin provider info as async task."""
        return provider_info

    @staticmethod
    def _normalize_provider_id(provider_id: str) -> str:
        """Normalize provider ID for backward compatibility.

        Maps legacy 'copaw-local' to 'lantai-local'.
        """
        if provider_id == "copaw-local":
            return "lantai-local"
        return provider_id

    def get_provider(self, provider_id: str) -> Provider | None:
        # Return a provider instance by its ID. This will be used to create
        # chat model instances for the agent.
        # Normalize provider ID for backward compatibility
        provider_id = self._normalize_provider_id(provider_id)
        # Check plugin providers first
        if provider_id in self.plugin_providers:
            plugin_provider = self.plugin_providers[provider_id]
            provider_info = plugin_provider["info"]
            provider_class = plugin_provider["class"]
            # Instantiate with **dict unpacking for Pydantic BaseModel
            return provider_class(**provider_info.model_dump())
        if provider_id in self.builtin_providers:
            return self.builtin_providers[provider_id]
        if provider_id in self.custom_providers:
            return self.custom_providers[provider_id]
        return None

    async def get_provider_info(self, provider_id: str) -> ProviderInfo | None:
        provider = self.get_provider(provider_id)
        if provider is None:
            return None
        return self._public_provider_info(await provider.get_info())

    def get_active_model(self) -> ModelSlotConfig | None:
        # Return the currently active provider/model configuration.
        if self.is_chat_model_slot_selectable(self.active_model):
            return self.active_model
        return self._recover_active_model_if_possible()

    def is_model_slot_available(
        self,
        slot: ModelSlotConfig | None,
    ) -> bool:
        """Return whether a provider/model slot is configured and usable.

        A model can remain persisted after its provider key is removed.  This
        helper distinguishes "remembered choice" from "currently runnable" so
        UI and runtime paths do not keep advertising stale credentials.
        """
        if slot is None or not slot.provider_id or not slot.model:
            return False

        provider = self.get_provider(slot.provider_id)
        if provider is None:
            return False
        if not provider.has_model(slot.model):
            return False
        if provider.require_api_key and not (provider.api_key or "").strip():
            return False
        if provider.require_api_key and not provider.is_local and not (
            provider.base_url or ""
        ).strip():
            return False
        if provider.is_custom and not (provider.base_url or "").strip():
            return False
        if slot.provider_id == "tokendance":
            try:
                resolve_tokendance_authorization(self, use_kind="model")
            except TokenDanceAuthorizationError:
                return False
        return True

    def is_chat_model_slot_selectable(
        self,
        slot: ModelSlotConfig | None,
    ) -> bool:
        """Return whether a configured model may be selected for chat."""

        if not self.is_model_slot_available(slot) or slot is None:
            return False
        if slot.provider_id == "tokendance" and not is_watcha_account_connected():
            return False
        provider = self.get_provider(slot.provider_id)
        model = provider.get_model_info(slot.model) if provider else None
        return bool(
            provider
            and model
            and provider.supports_chat_model(slot.model)
            and model.show_in_chat
        )

    def _iter_available_model_slots(self):
        """Yield all currently runnable provider/model slots.

        Slots are emitted in provider registration order and de-duplicated by
        ``(provider_id, model_id)``.
        """

        seen: set[tuple[str, str]] = set()

        provider_ids = [
            *self.builtin_providers.keys(),
            *self.custom_providers.keys(),
            *self.plugin_providers.keys(),
        ]
        for provider_id in provider_ids:
            if provider_id in AUTO_RECOVERY_HIDDEN_PROVIDER_IDS:
                continue
            provider = self.get_provider(provider_id)
            if provider is None:
                continue
            for model in [*provider.models, *provider.extra_models]:
                slot = ModelSlotConfig(
                    provider_id=provider_id,
                    model=model.id,
                )
                key = (slot.provider_id, slot.model)
                if key in seen:
                    continue
                seen.add(key)
                if self.is_chat_model_slot_selectable(slot):
                    yield slot

    def get_model_fallback_slot(
        self,
        primary: ModelSlotConfig | None,
    ) -> ModelSlotConfig | None:
        """Return one deterministic runnable alternative to ``primary``.

        Prefer the user's global selection when it differs from an agent's
        model.  Otherwise prefer a stable model over an experimental/preview
        entry while retaining provider registration order.
        """

        def distinct(slot: ModelSlotConfig | None) -> bool:
            return bool(
                slot is not None
                and self.is_chat_model_slot_selectable(slot)
                and (
                    primary is None
                    or (slot.provider_id, slot.model)
                    != (primary.provider_id, primary.model)
                )
            )

        active = self.active_model
        if distinct(active):
            return active

        candidates = [
            slot for slot in self._iter_available_model_slots() if distinct(slot)
        ]
        if not candidates:
            return None

        experimental_markers = ("experimental", "preview", "beta", "-exp")
        candidates.sort(
            key=lambda slot: any(
                marker in slot.model.lower() for marker in experimental_markers
            ),
        )
        return candidates[0]

    def _recover_active_model_if_possible(self) -> ModelSlotConfig | None:
        """Auto-recover a missing/stale active model when only one slot fits.

        We intentionally recover only when there is exactly one runnable slot.
        If there are multiple viable choices, we leave the selection unset and
        let the user choose explicitly.
        """

        if self.is_chat_model_slot_selectable(self.active_model):
            return self.active_model

        candidates: list[ModelSlotConfig] = []
        for slot in self._iter_available_model_slots():
            candidates.append(slot)
            if len(candidates) > 1:
                break

        if len(candidates) != 1:
            if self.active_model is not None:
                logger.info(
                    "Clearing unavailable active model: %s/%s",
                    self.active_model.provider_id,
                    self.active_model.model,
                )
                self.clear_active_model(self.active_model.provider_id)
            return None

        recovered = candidates[0]
        if self.active_model != recovered:
            logger.info(
                "Auto-recovered active model: %s/%s",
                recovered.provider_id,
                recovered.model,
            )
            self.active_model = recovered
            self.save_active_model(recovered)
        return recovered

    def update_provider(self, provider_id: str, config: Dict) -> bool:
        # Update the configuration of a provider (e.g., base URL, API key).
        # This will be called when the user edits a provider's settings in the
        # UI. It should update the in-memory provider instance and persist the
        # changes to providers.json.
        # Normalize provider ID for backward compatibility
        provider_id = self._normalize_provider_id(provider_id)
        if provider_id == "tokendance" and "api_key" in config:
            raw_api_key = config.get("api_key")
            if str(raw_api_key or "").strip():
                raise ValueError(
                    "TokenDance keys must use bind_tokendance_credential",
                )
            if raw_api_key is not None:
                return self.revoke_tokendance_credential(config)
        provider = self.get_provider(provider_id)
        if not provider:
            return False
        provider.update_config(config)

        # Determine save location
        is_builtin = provider_id in self.builtin_providers
        is_plugin = provider_id in self.plugin_providers

        if is_plugin:
            # Update plugin provider info in memory (convert Provider to
            # ProviderInfo)
            provider_info = ProviderInfo(**provider.model_dump())
            self.plugin_providers[provider_id]["info"] = provider_info
            # Save to plugin path (separate from builtin)
            self._save_plugin_provider(provider)
        else:
            self._save_provider(provider, is_builtin=is_builtin)

        return True

    def revoke_tokendance_credential(self, config: Dict | None = None) -> bool:
        """Clear a TokenDance key and every binding-scoped local record.

        Revocation is persisted atomically with the optional provider settings
        update.  A later key must therefore establish a fresh owner binding;
        stale payment session ids can never survive and attach to it.
        """

        provider = self.get_provider("tokendance")
        if provider is None:
            return False
        before = provider.model_copy(deep=True)
        try:
            provider.update_config({**(config or {}), "api_key": ""})
            provider.meta.pop(TOKENDANCE_BINDING_META_KEY, None)
            provider.meta.pop(TOKENDANCE_PAYMENT_SESSIONS_META_KEY, None)
            provider.meta.pop(TOKENDANCE_PAYMENT_INTENTS_META_KEY, None)
            provider.meta[VERSION_KEY] = STATE_VERSION
            self._save_provider(provider, is_builtin=True)
        except Exception:
            for field_name in type(provider).model_fields:
                setattr(
                    provider,
                    field_name,
                    deepcopy(getattr(before, field_name)),
                )
            raise
        return True

    def bind_tokendance_credential(
        self,
        api_key: str,
        *,
        expected_owner_subject_id: str,
        expected_account_epoch: int,
        ownership_evidence: str,
    ) -> dict:
        """Persist a TokenDance key and its trusted owner in one file write."""

        normalized_key = str(api_key or "").strip()
        if not normalized_key:
            raise ValueError("TokenDance API key is missing")
        if ownership_evidence not in {"oauth_pkce", "user_attested"}:
            raise ValueError("Unsupported TokenDance ownership evidence")

        context = load_watcha_account_context()
        if (
            context.subject_id != expected_owner_subject_id
            or context.account_epoch != expected_account_epoch
        ):
            raise ValueError("Watcha account context changed during authorization")

        provider = self.get_provider("tokendance")
        if provider is None:
            raise ValueError("TokenDance provider is unavailable")
        prior_binding = provider.meta.get(TOKENDANCE_BINDING_META_KEY)
        prior_revision = (
            prior_binding.get("binding_revision", 0)
            if isinstance(prior_binding, dict)
            else 0
        )
        if not isinstance(prior_revision, int) or prior_revision < 0:
            prior_revision = 0

        binding = {
            "schema_version": 1,
            "status": "active",
            "owner_subject_id": expected_owner_subject_id,
            "credential_ref": f"tdcred_{secrets.token_urlsafe(18)}",
            "payment_binding_id": f"tdpay_{secrets.token_urlsafe(18)}",
            "binding_revision": prior_revision + 1,
            "account_epoch_at_binding": expected_account_epoch,
            "ownership_evidence": ownership_evidence,
            "scopes": ["balance", "model", "payment", "usage"],
        }

        before_key = provider.api_key
        before_meta = deepcopy(provider.meta)
        before_revision = provider.configuration_revision
        before_verified = (
            provider.connection_verified_revision,
            provider.connection_verified_at,
            provider.model_verified_revision,
            provider.model_verified_id,
            provider.model_verified_at,
        )
        try:
            provider.update_config({"api_key": normalized_key})
            provider.meta[TOKENDANCE_BINDING_META_KEY] = binding
            provider.meta[TOKENDANCE_PAYMENT_SESSIONS_META_KEY] = {}
            provider.meta[TOKENDANCE_PAYMENT_INTENTS_META_KEY] = {}
            provider.meta[VERSION_KEY] = STATE_VERSION
            self._save_provider(provider, is_builtin=True)
        except Exception:
            provider.api_key = before_key
            provider.meta = before_meta
            provider.configuration_revision = before_revision
            (
                provider.connection_verified_revision,
                provider.connection_verified_at,
                provider.model_verified_revision,
                provider.model_verified_id,
                provider.model_verified_at,
            ) = before_verified
            raise
        return deepcopy(binding)

    @staticmethod
    def _tokendance_payment_binding_record(
        authorization: TokenDanceAuthorization,
    ) -> dict[str, object]:
        return {
            "owner_subject_id": authorization.owner_subject_id,
            "credential_ref": authorization.credential_ref,
            "payment_binding_id": authorization.payment_binding_id,
            "binding_revision": authorization.binding_revision,
        }

    def _require_current_tokendance_payment_binding(
        self,
        authorization: TokenDanceAuthorization,
    ) -> Provider:
        if authorization.use_kind != "payment":
            raise TokenDanceAuthorizationError(
                "tokendance_payment_scope_required",
                "当前授权不能用于词元跳动充值会话。",
                status_code=403,
            )
        assert_tokendance_authorization_current(self, authorization)
        provider = self.get_provider("tokendance")
        binding = (
            provider.meta.get(TOKENDANCE_BINDING_META_KEY)
            if provider is not None and isinstance(provider.meta, dict)
            else None
        )
        expected = self._tokendance_payment_binding_record(authorization)
        actual = {
            key: binding.get(key) if isinstance(binding, dict) else None
            for key in expected
        }
        if actual != expected:
            raise TokenDanceAuthorizationError(
                "authorization_context_stale",
                "账号或词元跳动授权已变化，请重新查询。",
                status_code=409,
            )
        return provider

    def record_tokendance_payment_session(
        self,
        session_id: str,
        authorization: TokenDanceAuthorization,
    ) -> None:
        """Persist proof that a payment session belongs to one binding."""

        if not _TOKENDANCE_PAYMENT_SESSION_ID_RE.fullmatch(session_id):
            raise ValueError("Invalid TokenDance payment session id")
        provider = self._require_current_tokendance_payment_binding(
            authorization,
        )
        before_meta = deepcopy(provider.meta)
        sessions = provider.meta.get(TOKENDANCE_PAYMENT_SESSIONS_META_KEY)
        sessions = deepcopy(sessions) if isinstance(sessions, dict) else {}
        record = self._tokendance_payment_binding_record(authorization)
        existing = sessions.get(session_id)
        existing_binding = (
            {key: existing.get(key) for key in record}
            if isinstance(existing, dict)
            else existing
        )
        if existing is not None and existing_binding != record:
            raise TokenDanceAuthorizationError(
                "tokendance_payment_session_conflict",
                "充值会话已绑定到其他授权，已拒绝覆盖。",
                status_code=409,
            )
        record["recorded_at"] = datetime.now(timezone.utc).isoformat()
        sessions[session_id] = record
        while len(sessions) > _TOKENDANCE_PAYMENT_SESSION_LIMIT:
            sessions.pop(next(iter(sessions)))
        try:
            provider.meta[TOKENDANCE_PAYMENT_SESSIONS_META_KEY] = sessions
            self._save_provider(provider, is_builtin=True)
        except Exception:
            provider.meta = before_meta
            raise

    def prepare_tokendance_payment_intent(
        self,
        client_intent_id: str,
        amount: int,
        authorization: TokenDanceAuthorization,
    ) -> tuple[dict, bool]:
        """Durably reserve one local intent before an external POST.

        A persisted ``prepared`` record is deliberately not retried: after a
        process crash it is impossible to know whether TokenDance accepted the
        request.  The user can create a fresh intent only after checking the
        provider account.
        """

        if not _TOKENDANCE_PAYMENT_INTENT_ID_RE.fullmatch(client_intent_id):
            raise ValueError("Invalid TokenDance payment intent id")
        if not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= 100_000:
            raise ValueError("Invalid TokenDance payment amount")
        provider = self._require_current_tokendance_payment_binding(
            authorization,
        )
        before_meta = deepcopy(provider.meta)
        intents = provider.meta.get(TOKENDANCE_PAYMENT_INTENTS_META_KEY)
        intents = deepcopy(intents) if isinstance(intents, dict) else {}
        binding = self._tokendance_payment_binding_record(authorization)
        existing = intents.get(client_intent_id)
        if isinstance(existing, dict):
            existing_binding = {key: existing.get(key) for key in binding}
            if existing_binding != binding:
                raise TokenDanceAuthorizationError(
                    "tokendance_payment_intent_conflict",
                    "充值意图已属于其他账号授权。",
                    status_code=409,
                )
            if existing.get("amount") != amount:
                raise TokenDanceAuthorizationError(
                    "tokendance_payment_intent_conflict",
                    "同一充值意图不能修改充值金额。",
                    status_code=409,
                )
            return deepcopy(existing), False
        if existing is not None:
            raise TokenDanceAuthorizationError(
                "tokendance_payment_intent_conflict",
                "充值意图记录无法识别，请创建新的充值意图。",
                status_code=409,
            )
        record = {
            "client_intent_id": client_intent_id,
            "amount": amount,
            "status": "prepared",
            **binding,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        intents[client_intent_id] = record
        while len(intents) > _TOKENDANCE_PAYMENT_INTENT_LIMIT:
            intents.pop(next(iter(intents)))
        try:
            provider.meta[TOKENDANCE_PAYMENT_INTENTS_META_KEY] = intents
            self._save_provider(provider, is_builtin=True)
        except Exception:
            provider.meta = before_meta
            raise
        return deepcopy(record), True

    def complete_tokendance_payment_intent(
        self,
        client_intent_id: str,
        session: dict,
        authorization: TokenDanceAuthorization,
    ) -> None:
        """Atomically bind a successful external session to its intent."""

        session_id = str(session.get("id") or "")
        if not _TOKENDANCE_PAYMENT_SESSION_ID_RE.fullmatch(session_id):
            raise ValueError("Invalid TokenDance payment session id")
        provider = self._require_current_tokendance_payment_binding(
            authorization,
        )
        before_meta = deepcopy(provider.meta)
        intents = provider.meta.get(TOKENDANCE_PAYMENT_INTENTS_META_KEY)
        intents = deepcopy(intents) if isinstance(intents, dict) else {}
        record = intents.get(client_intent_id)
        binding = self._tokendance_payment_binding_record(authorization)
        if not isinstance(record, dict) or {
            key: record.get(key) for key in binding
        } != binding:
            raise TokenDanceAuthorizationError(
                "tokendance_payment_intent_not_owned",
                "找不到当前账号创建的充值意图。",
                status_code=404,
            )
        amount = session.get("amount")
        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount != record.get("amount")
        ):
            raise ValueError("TokenDance payment session amount mismatch")
        if record.get("status") == "submitted":
            existing_id = str((record.get("session") or {}).get("id") or "")
            if existing_id == session_id:
                return
            raise TokenDanceAuthorizationError(
                "tokendance_payment_intent_conflict",
                "充值意图已关联其他会话。",
                status_code=409,
            )
        sessions = provider.meta.get(TOKENDANCE_PAYMENT_SESSIONS_META_KEY)
        sessions = deepcopy(sessions) if isinstance(sessions, dict) else {}
        owner_record = {
            **binding,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_session = sessions.get(session_id)
        if isinstance(existing_session, dict) and {
            key: existing_session.get(key) for key in binding
        } != binding:
            raise TokenDanceAuthorizationError(
                "tokendance_payment_session_conflict",
                "充值会话已绑定到其他授权，已拒绝覆盖。",
                status_code=409,
            )
        sessions[session_id] = owner_record
        intents[client_intent_id] = {
            **record,
            "status": "submitted",
            "session": deepcopy(session),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            provider.meta[TOKENDANCE_PAYMENT_SESSIONS_META_KEY] = sessions
            provider.meta[TOKENDANCE_PAYMENT_INTENTS_META_KEY] = intents
            self._save_provider(provider, is_builtin=True)
        except Exception:
            provider.meta = before_meta
            raise

    def mark_tokendance_payment_intent(
        self,
        client_intent_id: str,
        status: str,
        authorization: TokenDanceAuthorization,
    ) -> None:
        if status not in {"submission_unknown", "failed"}:
            raise ValueError("Invalid TokenDance payment intent status")
        provider = self._require_current_tokendance_payment_binding(
            authorization,
        )
        before_meta = deepcopy(provider.meta)
        intents = provider.meta.get(TOKENDANCE_PAYMENT_INTENTS_META_KEY)
        intents = deepcopy(intents) if isinstance(intents, dict) else {}
        record = intents.get(client_intent_id)
        binding = self._tokendance_payment_binding_record(authorization)
        if not isinstance(record, dict) or {
            key: record.get(key) for key in binding
        } != binding:
            raise TokenDanceAuthorizationError(
                "tokendance_payment_intent_not_owned",
                "找不到当前账号创建的充值意图。",
                status_code=404,
            )
        intents[client_intent_id] = {
            **record,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            provider.meta[TOKENDANCE_PAYMENT_INTENTS_META_KEY] = intents
            self._save_provider(provider, is_builtin=True)
        except Exception:
            provider.meta = before_meta
            raise

    def assert_tokendance_payment_session_owner(
        self,
        session_id: str,
        authorization: TokenDanceAuthorization,
    ) -> None:
        """Reject unknown sessions before disclosing them to an account."""

        if not _TOKENDANCE_PAYMENT_SESSION_ID_RE.fullmatch(session_id):
            raise TokenDanceAuthorizationError(
                "tokendance_payment_session_not_owned",
                "找不到当前账号创建的词元跳动充值会话。",
                status_code=404,
            )
        provider = self._require_current_tokendance_payment_binding(
            authorization,
        )
        sessions = provider.meta.get(TOKENDANCE_PAYMENT_SESSIONS_META_KEY)
        record = sessions.get(session_id) if isinstance(sessions, dict) else None
        expected = self._tokendance_payment_binding_record(authorization)
        comparable = (
            {key: record.get(key) for key in expected}
            if isinstance(record, dict)
            else None
        )
        if comparable != expected:
            raise TokenDanceAuthorizationError(
                "tokendance_payment_session_not_owned",
                "找不到当前账号创建的词元跳动充值会话。",
                status_code=404,
            )

    async def record_provider_verification(
        self,
        provider_id: str,
        model_id: str | None = None,
    ) -> ProviderInfo:
        """Persist a successful probe against the provider's current revision."""
        provider_id = self._normalize_provider_id(provider_id)
        provider = self.get_provider(provider_id)
        if provider is None:
            raise ProviderError(message=f"Provider '{provider_id}' not found.")

        normalized_model_id = (model_id or "").strip() or None
        if normalized_model_id and not provider.has_model(normalized_model_id):
            raise ModelNotFoundException(
                model_name=f"{provider_id}/{normalized_model_id}",
                details={
                    "provider_id": provider_id,
                    "model_id": normalized_model_id,
                },
            )

        verified_at = datetime.now(timezone.utc).isoformat()
        provider.connection_verified_revision = provider.configuration_revision
        provider.connection_verified_at = verified_at
        if normalized_model_id:
            provider.model_verified_revision = provider.configuration_revision
            provider.model_verified_id = normalized_model_id
            provider.model_verified_at = verified_at

        is_plugin = provider_id in self.plugin_providers
        if is_plugin:
            provider_info = ProviderInfo(**provider.model_dump())
            self.plugin_providers[provider_id]["info"] = provider_info
            self._save_plugin_provider(provider)
        else:
            self._save_provider(
                provider,
                is_builtin=provider_id in self.builtin_providers,
            )
        return await provider.get_info()

    def start_local_model_resume(self, local_manager) -> None:
        """Schedule background restore of the active local model server."""
        task = asyncio.create_task(
            self._resume_local_model(local_manager),
            name="lantai-local-model-resume",
        )
        task.add_done_callback(self._on_local_model_resume_done)

    @staticmethod
    def _on_local_model_resume_done(task: asyncio.Task[None]) -> None:
        """Log unexpected failures from background local model restore."""
        if task.cancelled():
            return

        exc = task.exception()
        if exc is not None:
            logger.warning(
                "Background local model restore failed: %s",
                exc,
                exc_info=exc,
            )
        logger.info("Background local model restore completed")

    async def fetch_provider_models(
        self,
        provider_id: str,
        save: bool = True,
        raise_on_error: bool = False,
    ) -> List[ModelInfo]:
        """Fetch the list of available models from a provider.

        Args:
            provider_id: The ID of the provider to fetch models from.
            save: If True, save the discovered models to the provider
                configuration. Defaults to True.
            raise_on_error: Re-raise discovery failures so API callers can
                return actionable provider-specific guidance.

        Returns:
            List of ModelInfo objects representing available models.
        """
        provider_id = self._normalize_provider_id(provider_id)
        provider = self.get_provider(provider_id)
        if not provider:
            return []
        try:
            authorization = (
                resolve_tokendance_authorization(self, use_kind="model")
                if provider_id == "tokendance"
                else None
            )
            models = await provider.fetch_models()
            if authorization is not None:
                assert_tokendance_authorization_current(self, authorization)
            if save:
                self.save_discovered_models(provider_id, models)
            return models
        except Exception as e:
            logger.warning(
                "Failed to fetch models for provider '%s': %s",
                provider_id,
                e,
            )
            if raise_on_error:
                raise
            return []

    def save_discovered_models(
        self,
        provider_id: str,
        models: List[ModelInfo],
    ) -> None:
        """Persist a fetched catalogue after its caller accepts the result."""

        provider_id = self._normalize_provider_id(provider_id)
        provider = self.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Provider '{provider_id}' not found")

        previous_models = {
            model.id: model
            for model in [*provider.models, *provider.extra_models]
        }
        for model in models:
            previous = previous_models.get(model.id)
            if previous is not None:
                model.show_in_chat = previous.show_in_chat
                model.use_for_video = previous.use_for_video
                model.generate_kwargs = dict(previous.generate_kwargs)
                model.user_context_window = previous.user_context_window
                if previous.capability_source == "user-configured":
                    for field in (
                        "context_window",
                        "max_context_window",
                        "max_output_tokens",
                        "tokenizer",
                        "recommended_output_reserve",
                        "capability_source",
                    ):
                        setattr(model, field, getattr(previous, field))
        default_model_ids = {model.id for model in provider.models}
        provider.extra_models = [
            model for model in models if model.id not in default_model_ids
        ]
        is_plugin = provider_id in self.plugin_providers
        if is_plugin:
            provider_info = ProviderInfo(**provider.model_dump())
            self.plugin_providers[provider_id]["info"] = provider_info
            self._save_plugin_provider(provider)
        else:
            self._save_provider(
                provider,
                is_builtin=provider_id in self.builtin_providers,
            )

    def _resolve_custom_provider_id(self, provider_id: str) -> str:
        """Resolve provider ID conflicts for a custom provider."""
        base_id = provider_id
        if base_id in self.builtin_providers:
            base_id = f"{base_id}-custom"

        resolved_id = base_id
        while (
            resolved_id in self.builtin_providers
            or resolved_id in self.custom_providers
        ):
            resolved_id = f"{resolved_id}-new"

        return resolved_id

    async def add_custom_provider(self, provider_data: ProviderInfo):
        # Add a new custom provider with the given data. This will update the
        # providers.json file and make the new provider available in the UI.
        provider_payload = provider_data.model_dump()
        provider_payload["id"] = self._resolve_custom_provider_id(
            provider_data.id,
        )
        provider_payload["is_custom"] = True
        provider = self._provider_from_data(
            provider_payload,
        )  # Validate provider data
        # For custom providers, we assume they don't support connection check
        # without model config, to avoid false negatives in the UI.
        provider.support_connection_check = False
        self.custom_providers[provider.id] = provider
        self._save_provider(provider, is_builtin=False)
        return await provider.get_info()

    def remove_custom_provider(self, provider_id: str) -> bool:
        # Remove a custom provider by its ID. This will update the
        # providers.json file and remove the provider from the UI.
        if provider_id in self.custom_providers:
            del self.custom_providers[provider_id]
            provider_path = self.custom_path / f"{provider_id}.json"
            if provider_path.exists():
                os.remove(provider_path)
            return True
        return False

    async def activate_model(self, provider_id: str, model_id: str):
        # Set the active provider and model for the agent. This will update
        # providers.json and determine which provider/model is used when the
        # agent creates chat model instances.
        # Normalize provider ID for backward compatibility
        provider_id = self._normalize_provider_id(provider_id)
        provider = self.get_provider(provider_id)
        if not provider:
            raise ProviderError(
                message=f"Provider '{provider_id}' not found.",
            )
        if not provider.has_model(model_id):
            raise ModelNotFoundException(
                model_name=f"{provider_id}/{model_id}",
                details={"provider_id": provider_id, "model_id": model_id},
            )
        if not provider.supports_chat_model(model_id):
            raise ProviderError(
                message=(
                    f"Model '{provider_id}/{model_id}' is not a chat model. "
                    "Select it in the matching media feature instead."
                ),
                details={"provider_id": provider_id, "model_id": model_id},
            )
        model_info = provider.get_model_info(model_id)
        if model_info is not None and not model_info.show_in_chat:
            raise ProviderError(
                message=(
                    f"Model '{provider_id}/{model_id}' is hidden from chat. "
                    "Enable it in model management first."
                ),
                details={"provider_id": provider_id, "model_id": model_id},
            )
        if not self.is_model_slot_available(
            ModelSlotConfig(provider_id=provider_id, model=model_id),
        ):
            raise ProviderError(
                message=(
                    f"Provider '{provider_id}' is not configured or "
                    "available."
                ),
            )
        self.active_model = ModelSlotConfig(
            provider_id=provider_id,
            model=model_id,
        )
        self.save_active_model(self.active_model)

    async def add_model_to_provider(
        self,
        provider_id: str,
        model_info: ModelInfo,
    ) -> ProviderInfo:
        provider_id = self._normalize_provider_id(provider_id)
        provider = self.get_provider(provider_id)
        if not provider:
            raise ProviderError(
                message=f"Provider '{provider_id}' not found.",
            )
        added, error_message = await provider.add_model(model_info)
        if not added:
            raise ProviderError(
                message=error_message,
                details={
                    "provider_id": provider_id,
                    "model_id": model_info.id,
                },
            )

        # Save provider config to appropriate location
        is_plugin = provider_id in self.plugin_providers
        if is_plugin:
            provider_info = ProviderInfo(**provider.model_dump())
            self.plugin_providers[provider_id]["info"] = provider_info
            self._save_plugin_provider(provider)
        else:
            self._save_provider(
                provider,
                is_builtin=provider_id in self.builtin_providers,
            )
        return await provider.get_info()

    async def update_model_config(
        self,
        provider_id: str,
        model_id: str,
        config: Dict,
    ) -> ProviderInfo:
        """Update per-model configuration and persist to disk."""
        provider_id = self._normalize_provider_id(provider_id)
        provider = self.get_provider(provider_id)
        if not provider:
            raise ProviderError(
                message=f"Provider '{provider_id}' not found.",
            )
        if not provider.update_model_config(model_id, config):
            raise ModelNotFoundException(
                model_name=f"{provider_id}/{model_id}",
                details={"provider_id": provider_id, "model_id": model_id},
            )

        # Save provider config to appropriate location
        is_plugin = provider_id in self.plugin_providers
        if is_plugin:
            provider_info = ProviderInfo(**provider.model_dump())
            self.plugin_providers[provider_id]["info"] = provider_info
            self._save_plugin_provider(provider)
        else:
            self._save_provider(
                provider,
                is_builtin=provider_id in self.builtin_providers,
            )
        return await provider.get_info()

    async def delete_model_from_provider(
        self,
        provider_id: str,
        model_id: str,
    ) -> ProviderInfo:
        provider_id = self._normalize_provider_id(provider_id)
        provider = self.get_provider(provider_id)
        if not provider:
            raise ProviderError(
                message=f"Provider '{provider_id}' not found.",
            )
        await provider.delete_model(model_id=model_id)

        # Save provider config to appropriate location
        is_plugin = provider_id in self.plugin_providers
        if is_plugin:
            provider_info = ProviderInfo(**provider.model_dump())
            self.plugin_providers[provider_id]["info"] = provider_info
            self._save_plugin_provider(provider)
        else:
            self._save_provider(
                provider,
                is_builtin=provider_id in self.builtin_providers,
            )
        return await provider.get_info()

    async def probe_model_multimodal(
        self,
        provider_id: str,
        model_id: str,
        image_only: bool = False,
    ) -> dict:
        """Probe a model's multimodal capabilities and persist the result.

        Args:
            provider_id: Provider identifier.
            model_id: Model identifier.
            image_only: When True, skip the video probe for a faster result.
                Only ``supports_image`` will be accurate; ``supports_video``
                will remain at its previous value (not updated).
        """
        provider_id = self._normalize_provider_id(provider_id)
        provider = self.get_provider(provider_id)
        if not provider:
            return {"error": f"Provider '{provider_id}' not found"}

        authorization = (
            resolve_tokendance_authorization(self, use_kind="model")
            if provider_id == "tokendance"
            else None
        )

        result = await provider.probe_model_multimodal(
            model_id,
            image_only=image_only,
        )
        if authorization is not None:
            assert_tokendance_authorization_current(self, authorization)

        # Update the model's capability flags.
        # For image_only probes, leave supports_video untouched so a
        # subsequent full probe can fill it in correctly.
        for model in provider.models + provider.extra_models:
            if model.id == model_id:
                model.supports_image = result.supports_image
                if not image_only:
                    model.supports_video = result.supports_video
                    model.supports_multimodal = result.supports_multimodal
                else:
                    # Partial update: derive supports_multimodal from
                    # image alone; video will be updated by the full probe.
                    if result.supports_image:
                        model.supports_multimodal = True
                model.probe_source = "probed"
                break

        # Compare probe result against expected baseline
        from .capability_baseline import (
            ExpectedCapabilityRegistry,
            compare_probe_result,
        )

        registry = ExpectedCapabilityRegistry()
        expected = registry.get_expected(provider_id, model_id)
        if expected:
            discrepancies = compare_probe_result(
                expected,
                result.supports_image,
                result.supports_video,
            )
            for d in discrepancies:
                logger.warning(
                    "Probe discrepancy: %s/%s %s expected=%s actual=%s (%s)",
                    d.provider_id,
                    d.model_id,
                    d.field,
                    d.expected,
                    d.actual,
                    d.discrepancy_type,
                )

        # Persist to disk
        is_plugin = provider_id in self.plugin_providers
        if is_plugin:
            provider_info = ProviderInfo(**provider.model_dump())
            self.plugin_providers[provider_id]["info"] = provider_info
            self._save_plugin_provider(provider)
        else:
            self._save_provider(
                provider,
                is_builtin=provider_id in self.builtin_providers,
            )
        return {
            "supports_image": result.supports_image,
            "supports_video": result.supports_video,
            "supports_multimodal": result.supports_multimodal,
            "image_message": result.image_message,
            "video_message": result.video_message,
        }

    def _save_provider(
        self,
        provider: Provider,
        is_builtin: bool = False,
        skip_if_exists: bool = False,
    ):
        """Save a provider configuration to disk.

        Sensitive fields (``api_key``) are encrypted before writing.
        """
        provider_dir = self.builtin_path if is_builtin else self.custom_path
        provider_path = provider_dir / f"{provider.id}.json"
        if skip_if_exists and provider_path.exists():
            return
        data = encrypt_dict_fields(
            provider.model_dump(),
            PROVIDER_SECRET_FIELDS,
        )
        atomic_write_provider(provider_path, data)

    def _save_plugin_provider(self, provider: Provider):
        """Save a plugin provider configuration to disk.

        Sensitive fields (``api_key``) are encrypted before writing.
        """
        provider_path = self.plugin_path / f"{provider.id}.json"
        data = encrypt_dict_fields(
            provider.model_dump(),
            PROVIDER_SECRET_FIELDS,
        )
        with open(provider_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(provider_path, 0o600)
        except OSError:
            pass

    def load_provider(
        self,
        provider_id: str,
        is_builtin: bool = False,
    ) -> Provider | None:
        """Load a provider configuration from disk.

        Encrypted fields are transparently decrypted.  If a legacy
        plaintext ``api_key`` is detected it is re-encrypted in place.
        """
        provider_dir = self.builtin_path if is_builtin else self.custom_path
        provider_path = provider_dir / f"{provider_id}.json"
        if not provider_path.exists():
            return None
        try:
            with open(provider_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            raw_data = data

            needs_rewrite = self._maybe_migrate_plaintext(
                data,
                PROVIDER_SECRET_FIELDS,
            )
            data = decrypt_dict_fields(data, PROVIDER_SECRET_FIELDS)
            provider = self._provider_from_data(data)

            try:
                migrated = migrate_provider_file(provider_path, raw_data)
                provider.meta.update(migrated.get("meta", {}))
            except OSError:
                logger.warning(
                    "Provider '%s' state migration deferred; "
                    "original retained",
                    provider_id,
                )

            if needs_rewrite:
                try:
                    self._save_provider(
                        provider,
                        is_builtin=is_builtin,
                        skip_if_exists=False,
                    )
                except Exception as enc_err:
                    logger.debug(
                        "Deferred plaintext→encrypted migration"
                        " for provider '%s': %s",
                        provider_id,
                        enc_err,
                    )

            return provider
        except SecretDecryptionError:
            logger.error(
                "Provider '%s' has credentials that cannot be decrypted; "
                "re-enter its API key before using it",
                provider_id,
            )
            return None
        except Exception as e:
            logger.warning(
                "Failed to load provider '%s' from %s: %s",
                provider_id,
                provider_path,
                e,
            )
            return None

    @staticmethod
    def _maybe_migrate_plaintext(
        data: dict,
        secret_fields: frozenset[str],
    ) -> bool:
        """Return ``True`` when *data* contains plaintext secret fields
        that should be re-encrypted on disk."""
        for field in secret_fields:
            value = data.get(field)
            if isinstance(value, str) and value and not is_encrypted(value):
                return True
        return False

    def _provider_from_data(self, data: Dict) -> Provider:
        """Deserialize provider data to a concrete provider type."""
        provider_id = str(data.get("id", ""))
        chat_model = str(data.get("chat_model", ""))

        if provider_id == "tokendance":
            normalized = dict(data)
            for field_name in ("models", "extra_models"):
                rows = normalized.get(field_name)
                if isinstance(rows, list):
                    normalized[field_name] = [
                        {
                            **row,
                            "show_in_chat": row.get("show_in_chat", False),
                            "use_for_video": row.get("use_for_video", False),
                        }
                        if isinstance(row, dict)
                        else row
                        for row in rows
                    ]
            return TokenDanceProvider.model_validate(normalized)
        if provider_id == "openrouter":
            return OpenRouterProvider.model_validate(data)
        if provider_id == "anthropic" or chat_model == "AnthropicChatModel":
            return AnthropicProvider.model_validate(data)
        if provider_id == "gemini" or chat_model == "GeminiChatModel":
            return GeminiProvider.model_validate(data)
        if provider_id == "ollama":
            return OllamaProvider.model_validate(data)
        if provider_id == "lmstudio":
            return LMStudioProvider.model_validate(data)
        return OpenAIProvider.model_validate(data)

    def save_active_model(self, active_model: ModelSlotConfig):
        """Save the active provider/model configuration to disk."""
        active_path = self.root_path / "active_model.json"
        with open(active_path, "w", encoding="utf-8") as f:
            json.dump(
                active_model.model_dump(),
                f,
                ensure_ascii=False,
                indent=2,
            )
        try:
            os.chmod(active_path, 0o600)
        except OSError:
            pass

    def clear_active_model(self, provider_id: str | None = None) -> bool:
        """Clear the active provider/model configuration.

        If provider_id is provided, only clear when it matches the current
        active provider.
        """
        if self.active_model is None:
            return False
        # Normalize provider ID for backward compatibility
        if provider_id is not None:
            provider_id = self._normalize_provider_id(provider_id)
        if (
            provider_id is not None
            and self.active_model.provider_id != provider_id
        ):
            return False

        self.active_model = None
        active_path = self.root_path / "active_model.json"
        try:
            active_path.unlink()
        except (FileNotFoundError, OSError):
            pass
        return True

    def load_active_model(self) -> ModelSlotConfig | None:
        """Load the active provider/model configuration from disk."""
        active_path = self.root_path / "active_model.json"
        if not active_path.exists():
            return None
        try:
            with open(active_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return ModelSlotConfig.model_validate(data)
        except Exception:
            return None

    def _migrate_copaw_config(self) -> None:
        """Migrate copaw-local provider config to lantai-local."""
        # 1. Migrate active model configuration (only provider_id)
        if (
            self.active_model
            and self.active_model.provider_id == "copaw-local"
        ):
            self.active_model.provider_id = "lantai-local"
            self.save_active_model(self.active_model)
            logger.info(
                "Migrated active model provider from "
                "'copaw-local' to 'lantai-local'",
            )

        # 2. Migrate stored provider config file
        copaw_config_path = self.builtin_path / "copaw-local.json"
        if not copaw_config_path.exists():
            return

        try:
            # Load old config and apply to new provider instance
            with open(copaw_config_path, "r", encoding="utf-8") as f:
                old_config = json.load(f)

            # Get the new built-in provider instance
            provider = self.builtin_providers.get("lantai-local")
            if not provider:
                return

            # Apply migrated configuration (preserve extra_models as-is)
            if "extra_models" in old_config:
                provider.extra_models = [
                    ModelInfo.model_validate(model)
                    for model in old_config["extra_models"]
                ]
            if "base_url" in old_config:
                provider.base_url = old_config["base_url"]
            if "generate_kwargs" in old_config:
                provider.generate_kwargs = old_config["generate_kwargs"]

            # Save using standard persistence logic (with encryption)
            self._save_provider(provider, is_builtin=True)

            # Remove old config file
            copaw_config_path.unlink()
            logger.info(
                "Migrated provider config from "
                "'copaw-local.json' to 'lantai-local.json'",
            )
        except Exception as exc:
            logger.warning("Failed to migrate copaw-local config: %s", exc)

    # pylint: disable=too-many-branches
    def _migrate_legacy_providers(self):
        """Migrate from legacy providers.json format to the new structure."""
        legacy_path = SECRET_DIR / "providers.json"
        if legacy_path.exists() and legacy_path.is_file():
            with open(legacy_path, "r", encoding="utf-8") as f:
                legacy_data = json.load(f)
            builtin_providers = legacy_data.get("providers", {})
            custom_providers = legacy_data.get("custom_providers", {})
            active_model = legacy_data.get("active_llm", {})
            # Migrate built-in providers
            for provider_id, config in builtin_providers.items():
                provider = self.get_provider(provider_id)
                if not provider:
                    logger.warning(
                        "Legacy provider '%s' not found in"
                        " registry, skipping migration for this provider.",
                        provider_id,
                    )
                    continue
                if "api_key" in config:
                    provider.api_key = config["api_key"]
                if "extra_models" in config:
                    provider.extra_models = [
                        ModelInfo.model_validate(model)
                        for model in config["extra_models"]
                    ]
                if not provider.freeze_url and "base_url" in config:
                    provider.base_url = config["base_url"]
                self._save_provider(provider, is_builtin=True)
            # Migrate custom providers
            for provider_id, data in custom_providers.items():
                custom_provider = OpenAIProvider(
                    id=provider_id,
                    name=data.get("name", provider_id),
                    base_url=data.get("base_url", ""),
                    api_key=data.get("api_key", ""),
                    is_custom=True,
                )
                if "models" in data:
                    # migrate models to extra_models field
                    custom_provider.extra_models = [
                        ModelInfo.model_validate(model)
                        for model in data["models"]
                    ]
                if "chat_model" in data:
                    custom_provider.chat_model = data["chat_model"]
                self._save_provider(custom_provider, is_builtin=False)
            # Migrate active model (only provider_id, not model)
            if active_model:
                try:
                    # Convert legacy copaw-local provider_id
                    if active_model.get("provider_id") == "copaw-local":
                        active_model["provider_id"] = "lantai-local"
                    self.active_model = ModelSlotConfig.model_validate(
                        active_model,
                    )
                    self.save_active_model(self.active_model)
                except Exception:
                    logger.warning(
                        "Failed to migrate active model, using default.",
                    )
            # Remove legacy file after migration
            try:
                os.remove(legacy_path)
            except Exception:
                logger.warning(
                    "Failed to remove legacy providers.json after migration.",
                )

    def _init_from_storage(self):
        """Initialize all providers and active model from disk storage."""
        # Load built-in providers
        for builtin in self.builtin_providers.values():
            provider = self.load_provider(builtin.id, is_builtin=True)
            if provider:
                # inherit user-configured base_url only when freeze_url=False
                if not builtin.freeze_url:
                    builtin.base_url = provider.base_url
                builtin.api_key = provider.api_key
                if builtin.id == "tokendance":
                    builtin.meta.update(provider.meta)
                builtin_model_ids = {m.id for m in builtin.models}
                builtin.extra_models = [
                    m
                    for m in provider.extra_models
                    if m.id not in builtin_model_ids
                ]
                builtin.generate_kwargs.update(provider.generate_kwargs)
                builtin.configuration_revision = provider.configuration_revision
                builtin.connection_verified_revision = (
                    provider.connection_verified_revision
                )
                builtin.connection_verified_at = provider.connection_verified_at
                builtin.model_verified_revision = provider.model_verified_revision
                builtin.model_verified_id = provider.model_verified_id
                builtin.model_verified_at = provider.model_verified_at
                # Restore user-owned fields for built-in models. Product
                # capability defaults remain upgradeable unless the user
                # explicitly edited those capability fields.
                stored_models = {
                    model.id: model
                    for model in (*provider.models, *provider.extra_models)
                    if model.id in builtin_model_ids
                }
                for model in builtin.models:
                    stored = stored_models.get(model.id)
                    if stored is None:
                        continue
                    model.generate_kwargs = dict(stored.generate_kwargs)
                    model.user_context_window = stored.user_context_window
                    if stored.capability_source == "user-configured":
                        for field_name in (
                            "context_window",
                            "max_output_tokens",
                            "recommended_output_reserve",
                            "tokenizer",
                            "capability_source",
                            "capability_verified_at",
                        ):
                            setattr(
                                model,
                                field_name,
                                getattr(stored, field_name),
                            )
        # Load custom providers
        custom_files = list(self.custom_path.glob("*.json"))
        for provider_file in custom_files:
            provider = self.load_provider(provider_file.stem, is_builtin=False)
            if provider:
                self.custom_providers[provider.id] = provider
        # Load active model config
        active_model = self.load_active_model()
        if active_model:
            self.active_model = active_model
        self._recover_active_model_if_possible()

        # Migrate copaw-local to lantai-local for backwards compatibility
        self._migrate_copaw_config()

    def _apply_default_annotations(self):
        """Apply doc-based default annotations for unprobed models.

        Models that already carry static annotations (supports_image /
        supports_video set at definition time) only need the derived
        supports_multimodal flag computed.  Models with no annotations
        at all fall back to the ExpectedCapabilityRegistry.
        """
        from .capability_baseline import ExpectedCapabilityRegistry
        from .model_capability_registry import resolve_model_capability

        registry = ExpectedCapabilityRegistry()
        for provider in self.builtin_providers.values():
            for model in provider.models:
                # Already fully annotated (e.g. by a prior probe) → skip
                if model.supports_multimodal is not None:
                    continue

                # Static annotations present → compute derived flag only
                if (
                    model.supports_image is not None
                    or model.supports_video is not None
                ):
                    model.supports_multimodal = bool(
                        model.supports_image or model.supports_video,
                    )
                    continue

                # No annotations at all → fall back to registry
                expected = registry.get_expected(provider.id, model.id)
                if expected:
                    model.supports_image = expected.expected_image
                    model.supports_video = expected.expected_video
                    model.supports_multimodal = bool(
                        expected.expected_image or expected.expected_video,
                    )
                    model.probe_source = "documentation"

        # Context metadata is independent from multimodal annotations. Apply
        # only exact, source-backed records, and never replace capabilities
        # discovered from a gateway or explicitly configured by the user.
        for provider in (
            *self.builtin_providers.values(),
            *self.custom_providers.values(),
        ):
            for model in (*provider.models, *provider.extra_models):
                capability = resolve_model_capability(provider.id, model.id)
                if capability is None:
                    continue
                changed = False
                for field_name in (
                    "context_window",
                    "max_output_tokens",
                    "recommended_output_reserve",
                    "tokenizer",
                ):
                    if getattr(model, field_name) is not None:
                        continue
                    value = getattr(capability, field_name)
                    if value is not None:
                        setattr(model, field_name, value)
                        changed = True
                if changed and not model.capability_source:
                    model.capability_source = capability.source
                    model.capability_verified_at = capability.verified_at

    async def _resume_local_model(self, local_manager) -> None:
        """Resume the active local model server from the previous run."""

        def _clear_local_provider():
            self.update_provider(
                "lantai-local",
                {
                    "base_url": "",
                    "extra_models": [],
                },
            )

        local_models = self.get_provider("lantai-local").extra_models
        model_id = local_models[0].id if local_models else None
        if model_id is None:
            return

        installed, _ = local_manager.check_llamacpp_installation()
        if not installed:
            logger.info(
                "Skipping local model restore because"
                " llama.cpp is not installed.",
            )
            _clear_local_provider()
            return

        if not local_manager.is_model_downloaded(model_id):
            logger.warning(
                "Skipping local model restore because"
                " model is not downloaded: %s",
                model_id,
            )
            _clear_local_provider()
            return

        try:
            setup_result = await local_manager.setup_server(model_id)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Failed to restore local model server for %s: %s",
                model_id,
                exc,
            )
            _clear_local_provider()
            return

        self.update_provider(
            "lantai-local",
            {
                "base_url": f"http://127.0.0.1:{setup_result.port}/v1",
                "extra_models": [setup_result.model_info],
            },
        )

    def register_plugin_provider(
        self,
        provider_id: str,
        provider_class,
        label: str,
        base_url: str,
        metadata: Dict,
    ):
        """Register a plugin provider.

        Args:
            provider_id: Provider ID
            provider_class: Provider class
            label: Display label
            base_url: API base URL
            metadata: Additional metadata
        """
        # Get default models from provider class if available
        default_models = []
        if hasattr(provider_class, "get_default_models"):
            try:
                default_models = provider_class.get_default_models()
            except Exception as e:
                logger.warning(
                    f"Failed to get default models for {provider_id}: {e}",
                )

        # Create ProviderInfo
        provider_info = ProviderInfo(
            id=provider_id,
            name=label,
            base_url=base_url,
            api_key="",  # Will be configured by user
            chat_model=metadata.get("chat_model", "OpenAIChatModel"),
            models=default_models,  # Add default models
            is_custom=False,  # Mark as non-custom (like builtin, cannot be
            # deleted)
            require_api_key=metadata.get("require_api_key", True),
            meta=metadata.get("meta", {}),  # Pass meta from plugin
        )

        # Check if there's a saved configuration for this plugin provider
        saved_config_path = self.plugin_path / f"{provider_id}.json"
        if saved_config_path.exists():
            try:
                with open(saved_config_path, "r", encoding="utf-8") as f:
                    saved_config = json.load(f)

                # Decrypt sensitive fields
                saved_config = decrypt_dict_fields(
                    saved_config,
                    PROVIDER_SECRET_FIELDS,
                )

                # Merge saved config (api_key, base_url, extra_models, etc)
                if "api_key" in saved_config:
                    provider_info.api_key = saved_config["api_key"]
                if "base_url" in saved_config:
                    provider_info.base_url = saved_config["base_url"]
                if "generate_kwargs" in saved_config:
                    provider_info.generate_kwargs = saved_config[
                        "generate_kwargs"
                    ]
                # Load extra_models from saved config
                if "extra_models" in saved_config:
                    provider_info.extra_models = [
                        ModelInfo.model_validate(
                            model.model_dump()
                            if isinstance(model, BaseModel)
                            else model,
                        )
                        for model in saved_config["extra_models"]
                    ]
                logger.info(
                    f"✓ Loaded saved config for plugin provider: "
                    f"{provider_id} "
                    f"({len(provider_info.extra_models)} extra model(s))",
                )
            except SecretDecryptionError:
                logger.error(
                    "Plugin provider '%s' has credentials that cannot be "
                    "decrypted; re-enter its API key before using it",
                    provider_id,
                )
            except Exception as e:
                logger.warning(
                    f"Failed to load saved config for {provider_id}: {e}",
                )

        # Register to internal dict
        self.plugin_providers[provider_id] = {
            "info": provider_info,
            "class": provider_class,
        }

        logger.info(
            f"✓ Registered plugin provider: {provider_id} "
            f"with {len(default_models)} default model(s)",
        )

    @staticmethod
    def get_instance() -> "ProviderManager":
        """Get the singleton instance of ProviderManager."""
        if ProviderManager._instance is None:
            ProviderManager._instance = ProviderManager()
        return ProviderManager._instance

    @staticmethod
    def get_active_chat_model() -> object:
        """Get the currently active provider/model configuration."""
        manager = ProviderManager.get_instance()
        model = manager.get_active_model()
        if model is None or model.provider_id == "" or model.model == "":
            raise ProviderError(
                message="No active model configured.",
            )
        provider = manager.get_provider(model.provider_id)
        if provider is None:
            raise ProviderError(
                message=f"Active provider '{model.provider_id}' not found.",
            )
        return provider.get_chat_model_instance(model.model)
