# -*- coding: utf-8 -*-
"""API routes for LLM providers and models."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
from copy import deepcopy
from typing import Any, List, Literal, Optional
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
)
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from lantai.exceptions import AppBaseException

from ...config.config import (
    DEFAULT_AGENT_MAX_INPUT_LENGTH,
    ActiveModelsInfo,
    ModelSlotConfig,
    load_agent_config,
    save_agent_config,
)
from ...config.utils import load_config
from ...providers.compatibility_probe import (
    ModelCompatibilityReport,
    run_model_compatibility_probe,
)
from ...providers.openrouter_provider import OpenRouterProvider
from ...providers.provider import ModelInfo, ProviderInfo
from ...providers.provider_manager import ProviderManager
from ...providers.reasoning import get_reasoning_capability
from ...providers.tokendance_oauth import (
    TokenDanceOAuthStart,
    TokenDanceOAuthStatus,
    exchange_authorization_code,
    resolve_tokendance_callback_base_url,
    tokendance_oauth_registry,
)
from ...providers.tokendance_account import (
    TokenDanceAccountClient,
    TokenDanceAccountError,
)
from ...providers.tokendance_authorization import (
    TokenDanceAuthorization,
    TokenDanceAuthorizationError,
    assert_tokendance_authorization_current,
    resolve_tokendance_authorization,
)
from ...security.cloud_account_store import (
    is_watcha_account_connected,
    load_watcha_account_context,
)
from ..agent_context import get_agent_for_request
from ..utils import schedule_agent_reload
from ..workspace_access import should_defer_desktop_workspace_io

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])

ChatModelName = Literal[
    "OpenAIChatModel",
    "AnthropicChatModel",
    "GeminiChatModel",
]

# effective: agent-specific if set, otherwise global
# global: the global model only, ignoring any agent-specific setting
# agent: a specific agent's model only, error if not set
ActiveModelReadScope = Literal["effective", "global", "agent"]
ActiveModelWriteScope = Literal["global", "agent"]


def get_provider_manager(request: Request) -> ProviderManager:
    """Get the provider manager from app state.

    Args:
        request: FastAPI request object
    """
    provider_manager = getattr(request.app.state, "provider_manager", None)
    if provider_manager is None:
        provider_manager = ProviderManager.get_instance()
    return provider_manager


class ProviderConfigRequest(BaseModel):
    api_key: Optional[str] = Field(default=None)
    credential_ownership_attested: bool = Field(
        default=False,
        description=(
            "Explicit confirmation that a manually entered TokenDance key "
            "belongs to the current Watcha subject"
        ),
    )
    base_url: Optional[str] = Field(default=None)
    chat_model: Optional[ChatModelName] = Field(
        default=None,
        description="Chat model class name for protocol selection",
    )
    generate_kwargs: Optional[dict] = Field(
        default_factory=dict,
        description=(
            "Configuration in json format, will be expanded "
            "and passed to generation calls "
            "(e.g., openai.chat.completions, anthropic.messages)."
        ),
    )


class ModelSlotRequest(BaseModel):
    provider_id: str = Field(..., description="Provider to use")
    model: str = Field(..., description="Model identifier")
    scope: ActiveModelWriteScope = Field(
        ...,
        description="Whether to update the global model or a specific agent",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="Target agent ID when scope is 'agent'",
    )


class CreateCustomProviderRequest(BaseModel):
    id: str = Field(...)
    name: str = Field(...)
    default_base_url: str = Field(default="")
    api_key: str = Field(
        default="",
        description="API key to encrypt and persist for this provider",
    )
    api_key_prefix: str = Field(default="")
    chat_model: ChatModelName = Field(default="OpenAIChatModel")
    models: List[ModelInfo] = Field(default_factory=list)
    support_model_discovery: bool = Field(default=False)
    require_api_key: bool = Field(default=True)
    meta: dict[str, Any] = Field(default_factory=dict)


class AddModelRequest(BaseModel):
    id: str = Field(...)
    name: str = Field(...)
    is_free: bool = Field(
        default=False,
        description="Whether this model is free to use",
    )
    supports_multimodal: Optional[bool] = Field(
        default=None,
        description="Whether the model supports multimodal input",
    )
    supports_image: Optional[bool] = Field(
        default=None,
        description="Whether the model supports image input",
    )
    supports_video: Optional[bool] = Field(
        default=None,
        description="Whether the model supports video input",
    )
    probe_source: Optional[str] = Field(
        default=None,
        description="Source of capability metadata",
    )
    context_window: Optional[int] = Field(default=None, ge=1000)
    user_context_window: Optional[int] = Field(default=None, ge=1000)
    max_output_tokens: Optional[int] = Field(default=None, ge=1)
    recommended_output_reserve: Optional[int] = Field(default=None, ge=1)
    tokenizer: Optional[str] = Field(default=None)


class ModelConfigRequest(BaseModel):
    generate_kwargs: Optional[dict] = Field(
        default_factory=dict,
        description=(
            "Per-model generation parameters in JSON format. "
            "These override provider-level generate_kwargs."
        ),
    )
    context_window: Optional[int] = Field(default=None, ge=1000)
    user_context_window: Optional[int] = Field(default=None, ge=1000)
    max_output_tokens: Optional[int] = Field(default=None, ge=1)
    recommended_output_reserve: Optional[int] = Field(default=None, ge=1)
    tokenizer: Optional[str] = Field(default=None)
    show_in_chat: Optional[bool] = Field(default=None)
    use_for_video: Optional[bool] = Field(default=None)


class TokenDancePaymentRequest(BaseModel):
    amount: int = Field(..., ge=1, le=100_000)
    client_intent_id: str = Field(
        ...,
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


def _micro_yuan(value: Any) -> str:
    try:
        amount = int(value)
    except (TypeError, ValueError):
        amount = 0
    return f"{amount / 1_000_000:.6f}"


def _require_provider_account_access(provider_id: str) -> None:
    if provider_id == "tokendance" and not is_watcha_account_connected():
        raise HTTPException(
            status_code=403,
            detail="Watcha account login is required for TokenDance.",
        )


def _validate_model_slot(
    manager: ProviderManager,
    provider_id: str,
    model_id: str,
) -> None:
    """Validate that the provider and model exist without mutating state."""
    _require_provider_account_access(provider_id)
    provider = manager.get_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found.",
        )
    if not provider.has_model(model_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model_id}' not found in provider '{provider_id}'."
            ),
        )
    get_model_info = getattr(provider, "get_model_info", None)
    model_info = get_model_info(model_id) if callable(get_model_info) else None
    if model_info is not None and not model_info.show_in_chat:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model_id}' is hidden from chat. Enable it in model "
                "management first."
            ),
        )
    if not manager.is_model_slot_available(
        ModelSlotConfig(provider_id=provider_id, model=model_id),
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Provider '{provider_id}' is not configured or available."
            ),
        )


async def _validate_model_access(
    manager: ProviderManager,
    provider_id: str,
    model_id: str,
) -> None:
    """Confirm the selected account can call the model before persisting it."""
    provider = manager.get_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found.",
        )

    authorization = _tokendance_model_authorization(manager, provider_id)

    try:
        success, message = await provider.check_model_connection(
            model_id,
            timeout=8,
        )
        _assert_tokendance_model_authorization(manager, authorization)
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        AppBaseException,
    ) as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"无法验证模型 '{model_id}' 是否可用，已保留原模型。"
                f"{exc}"
            ),
        ) from exc

    if success:
        return

    reason = str(message or "").strip() or "请检查当前账号的模型权限。"
    raise HTTPException(
        status_code=400,
        detail=(
            f"当前账号无法使用模型 '{model_id}'，已保留原模型。"
            f"{reason}"
        ),
    )


def _available_model_slot(
    manager: ProviderManager,
    slot: ModelSlotConfig | None,
) -> ModelSlotConfig | None:
    """Return *slot* only when its provider/model can actually be used."""
    if slot is None:
        return None
    if slot.provider_id == "tokendance" and not is_watcha_account_connected():
        return None
    provider = manager.get_provider(slot.provider_id)
    get_model_info = getattr(provider, "get_model_info", None)
    model_info = get_model_info(slot.model) if callable(get_model_info) else None
    if model_info is not None and not model_info.show_in_chat:
        return None
    return slot if manager.is_model_slot_available(slot) else None


def _model_context_policy_payload(
    manager: ProviderManager,
    *,
    provider_id: str,
    model_id: str,
    user_window: int | None,
    mode: str = "smart",
) -> dict[str, Any]:
    """Return the post-switch policy for the exact selected model route."""

    from ...model_context_profile import ModelContextProfile

    local_window = None
    if provider_id in {"lantai-local", "copaw-local"}:
        try:
            from ...local_models.manager import LocalModelManager

            local_window = (
                LocalModelManager.get_instance()
                .get_config()
                .max_context_length
            )
        except Exception:
            logger.debug(
                "Unable to resolve local window during model switch",
                exc_info=True,
            )
    profile = ModelContextProfile.from_provider_selection(
        provider_manager=manager,
        provider_id=provider_id,
        model_id=model_id,
        user_window=user_window,
        local_runtime_window=local_window,
    )
    decision = profile.policy(mode)  # type: ignore[arg-type]
    return {
        "mode": decision.mode,
        "hard_window": decision.hard_window,
        "usable_input_tokens": decision.usable_input_tokens,
        "target_input_tokens": decision.target_input_tokens,
        "output_reserve": decision.output_reserve,
        "safety_margin": decision.safety_margin,
        "used_unknown_fallback": decision.used_unknown_fallback,
        "status": profile.status,
        "source": profile.provider_window.source,
        "switch_action": "adapted",
    }


async def _load_agent_model(
    request: Request,
    agent_id: str,
) -> ModelSlotConfig | None:
    """Load the model configured for a specific agent."""
    del request
    config = load_config()
    agent_ref = config.agents.profiles.get(agent_id)
    if agent_ref is None:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_id}' not found",
        )
    if should_defer_desktop_workspace_io(agent_ref.workspace_dir):
        return None
    agent_config = await asyncio.to_thread(load_agent_config, agent_id)
    return agent_config.active_model


@router.get(
    "",
    response_model=List[ProviderInfo],
    summary="List all providers",
)
async def list_all_providers(
    manager: ProviderManager = Depends(get_provider_manager),
) -> List[ProviderInfo]:
    providers = await manager.list_provider_info()
    if is_watcha_account_connected():
        return providers
    return [provider for provider in providers if provider.id != "tokendance"]


def _tokendance_account_client(
    manager: ProviderManager,
    *,
    use_kind: str,
) -> tuple[TokenDanceAccountClient, TokenDanceAuthorization]:
    try:
        authorization = resolve_tokendance_authorization(
            manager,
            use_kind=use_kind,
        )
    except TokenDanceAuthorizationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return (
        TokenDanceAccountClient(
            authorization.api_key,
            authorization_context_id=authorization.context_id,
        ),
        authorization,
    )


def _assert_tokendance_account_authorization(
    manager: ProviderManager,
    authorization: TokenDanceAuthorization,
) -> None:
    try:
        assert_tokendance_authorization_current(manager, authorization)
    except TokenDanceAuthorizationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


def _raise_tokendance_authorization_error(
    exc: TokenDanceAuthorizationError,
) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


def _tokendance_model_authorization(
    manager: ProviderManager,
    provider_id: str,
) -> TokenDanceAuthorization | None:
    """Capture the trusted identity/key revision for a model-side request."""

    if provider_id != "tokendance":
        return None
    try:
        return resolve_tokendance_authorization(manager, use_kind="model")
    except TokenDanceAuthorizationError as exc:
        _raise_tokendance_authorization_error(exc)


def _assert_tokendance_model_authorization(
    manager: ProviderManager,
    authorization: TokenDanceAuthorization | None,
) -> None:
    if authorization is None:
        return
    _assert_tokendance_account_authorization(manager, authorization)


def _raise_tokendance_account_error(exc: TokenDanceAccountError) -> None:
    logger.warning("TokenDance account request failed: %s", exc.code)
    raise HTTPException(
        status_code=exc.status_code,
        detail={
            "code": exc.code,
            "kind": exc.kind,
            "message": exc.message,
            "outcome_unknown": exc.outcome_unknown,
        },
    ) from exc


_TOKENDANCE_PAYMENT_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TOKENDANCE_PAYMENT_STATUSES = {
    "pending",
    "paid",
    "failed",
    "closed",
    "refunded",
}


def _public_https_url(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "tokendance_invalid_payment_session",
                "message": f"词元跳动充值会话的 {field} 无效。",
            },
        )
    parsed = urlsplit(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    valid = (
        parsed.scheme == "https"
        and bool(host)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and host not in {"localhost", "localhost.localdomain"}
        and not host.endswith((".local", ".internal", ".localhost"))
    )
    if valid:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            valid = address.is_global
    if not valid:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "tokendance_invalid_payment_session",
                "message": f"词元跳动充值会话的 {field} 无效。",
            },
        )
    return value


def _tokendance_payment_session(
    payload: dict[str, Any],
    *,
    expected_amount: int | None = None,
    expected_session_id: str | None = None,
    require_payment_details: bool = False,
) -> dict[str, Any]:
    session = payload.get("session")
    if not isinstance(session, dict):
        raise HTTPException(status_code=502, detail="词元跳动充值数据格式异常。")
    session_id = session.get("id")
    status = session.get("status")
    amount = session.get("amount")
    valid = (
        isinstance(session_id, str)
        and _TOKENDANCE_PAYMENT_SESSION_ID_RE.fullmatch(session_id) is not None
        and (expected_session_id is None or session_id == expected_session_id)
        and status in _TOKENDANCE_PAYMENT_STATUSES
    )
    if amount is not None:
        valid = valid and (
            isinstance(amount, int)
            and not isinstance(amount, bool)
            and 1 <= amount <= 100_000
        )
    if expected_amount is not None:
        valid = valid and amount == expected_amount
    if not valid:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "tokendance_invalid_payment_session",
                "message": "词元跳动返回了无法验证的充值会话。",
            },
        )

    payment_url = session.get("payment_url")
    if require_payment_details or payment_url is not None:
        _public_https_url(payment_url, field="payment_url")

    status_url = session.get("status_url")
    if require_payment_details or status_url is not None:
        status_url = _public_https_url(status_url, field="status_url")
        expected_status_url = (
            "https://tokendance.space/portal/api/v1/payment/sessions/"
            f"{session_id}"
        )
        if status_url != expected_status_url:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "tokendance_invalid_payment_session",
                    "message": "词元跳动充值会话的 status_url 无效。",
                },
            )
    return dict(session)


@router.get("/tokendance/account/balance", summary="Read TokenDance balance")
async def get_tokendance_balance(
    manager: ProviderManager = Depends(get_provider_manager),
) -> dict[str, Any]:
    client, authorization = _tokendance_account_client(
        manager,
        use_kind="balance",
    )
    try:
        payload = await client.balance()
    except TokenDanceAccountError as exc:
        _raise_tokendance_account_error(exc)
    _assert_tokendance_account_authorization(manager, authorization)
    balance = payload.get("balance") if isinstance(payload, dict) else None
    if not isinstance(balance, dict):
        raise HTTPException(status_code=502, detail="词元跳动余额数据格式异常。")
    return {
        "authorization_context_id": authorization.context_id,
        "balance": balance,
        "balance_cny": _micro_yuan(balance.get("balance")),
        "credits_cny": _micro_yuan(balance.get("credits")),
        "credits_used_cny": _micro_yuan(balance.get("credits_used")),
    }


@router.post(
    "/tokendance/account/redemption/redeem",
    summary="Redeem a TokenDance code for the bound account",
)
async def redeem_tokendance_code(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
) -> dict[str, Any]:
    # Parse privately: FastAPI's default 422 response can include rejected
    # input values, and a redemption code must never be echoed to the UI.
    raw = await request.body()
    if len(raw) > 1024:
        raise HTTPException(status_code=400, detail="请输入有效的兑换码。")
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="请输入有效的兑换码。",
        ) from exc
    value = body.get("code") if isinstance(body, dict) else None
    code = value.strip() if isinstance(value, str) else ""
    if not 1 <= len(code) <= 256 or any(ord(char) < 32 for char in code):
        raise HTTPException(status_code=400, detail="请输入有效的兑换码。")

    client, authorization = _tokendance_account_client(
        manager,
        use_kind="payment",
    )
    try:
        payload = await client.redeem(code)
    except TokenDanceAccountError as exc:
        _raise_tokendance_account_error(exc)
    _assert_tokendance_account_authorization(manager, authorization)
    credits = payload.get("credits")
    if not isinstance(credits, int) or isinstance(credits, bool) or credits <= 0:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "tokendance_invalid_redemption_response",
                "message": "兑换结果无法确认，请刷新余额或前往 TokenDance 核对。",
                "outcome_unknown": True,
            },
        )
    return {
        "authorization_context_id": authorization.context_id,
        "credits": credits,
        "credits_cny": f"{credits // 1_000_000}.{credits % 1_000_000:06d}",
    }


@router.get("/tokendance/account/usage", summary="Read TokenDance usage")
async def get_tokendance_usage(
    manager: ProviderManager = Depends(get_provider_manager),
    limit: int = Query(default=20, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    window: str = Query(default="7d", min_length=1, max_length=32),
    model_id: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset, "window": window}
    if model_id:
        params["model_id"] = model_id
    client, authorization = _tokendance_account_client(
        manager,
        use_kind="usage",
    )
    try:
        payload = await client.usage(params)
    except TokenDanceAccountError as exc:
        _raise_tokendance_account_error(exc)
    _assert_tokendance_account_authorization(manager, authorization)
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise HTTPException(status_code=502, detail="词元跳动用量数据格式异常。")
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["cost_cny"] = _micro_yuan(item.get("cost"))
        normalized.append(row)
    return {
        "authorization_context_id": authorization.context_id,
        "items": normalized,
        "offset": int(payload.get("offset", offset) or 0),
        "limit": int(payload.get("limit", limit) or limit),
        "has_more": len(items) >= limit,
    }


@router.post(
    "/tokendance/account/payment/sessions",
    status_code=201,
    summary="Create a TokenDance payment session",
)
async def create_tokendance_payment_session(
    body: TokenDancePaymentRequest,
    manager: ProviderManager = Depends(get_provider_manager),
) -> dict[str, Any]:
    client, authorization = _tokendance_account_client(
        manager,
        use_kind="payment",
    )
    try:
        intent, created = manager.prepare_tokendance_payment_intent(
            body.client_intent_id,
            body.amount,
            authorization,
        )
    except TokenDanceAuthorizationError as exc:
        _raise_tokendance_authorization_error(exc)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.exception("Failed to reserve TokenDance payment intent")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "tokendance_payment_intent_tracking_failed",
                "message": "本机未能保存充值意图，尚未向词元跳动提交。",
            },
        ) from exc
    if not created:
        if intent.get("status") == "submitted" and isinstance(
            intent.get("session"),
            dict,
        ):
            return {
                **intent["session"],
                "authorization_context_id": authorization.context_id,
            }
        raise HTTPException(
            status_code=409,
            detail={
                "code": "tokendance_payment_intent_unresolved",
                "message": (
                    "这次充值是否已提交无法安全确认，请勿重复创建；"
                    "先到词元跳动核对后再发起新的充值。"
                ),
            },
        )
    try:
        payload = await client.create_payment_session(body.amount)
    except TokenDanceAccountError as exc:
        try:
            manager.mark_tokendance_payment_intent(
                body.client_intent_id,
                "submission_unknown" if exc.outcome_unknown else "failed",
                authorization,
            )
        except Exception:
            logger.exception("Failed to update TokenDance payment intent")
        _raise_tokendance_account_error(exc)
    _assert_tokendance_account_authorization(manager, authorization)
    session = _tokendance_payment_session(
        payload,
        expected_amount=body.amount,
        require_payment_details=True,
    )
    try:
        manager.complete_tokendance_payment_intent(
            body.client_intent_id,
            session,
            authorization,
        )
    except TokenDanceAuthorizationError as exc:
        _raise_tokendance_authorization_error(exc)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.exception("Failed to record TokenDance payment session owner")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "tokendance_payment_session_tracking_failed",
                "message": (
                    "充值会话已向词元跳动提交，但本机未能保存"
                    "归属。请勿重复创建，先到词元跳动核对。"
                ),
            },
        ) from exc
    return {**session, "authorization_context_id": authorization.context_id}


@router.get(
    "/tokendance/account/payment/sessions/{session_id}",
    summary="Read a TokenDance payment session",
)
async def get_tokendance_payment_session(
    session_id: str = Path(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    manager: ProviderManager = Depends(get_provider_manager),
) -> dict[str, Any]:
    client, authorization = _tokendance_account_client(
        manager,
        use_kind="payment",
    )
    try:
        manager.assert_tokendance_payment_session_owner(
            session_id,
            authorization,
        )
    except TokenDanceAuthorizationError as exc:
        _raise_tokendance_authorization_error(exc)
    try:
        payload = await client.payment_session(session_id)
    except TokenDanceAccountError as exc:
        _raise_tokendance_account_error(exc)
    _assert_tokendance_account_authorization(manager, authorization)
    return {
        **_tokendance_payment_session(
            payload,
            expected_session_id=session_id,
        ),
        "authorization_context_id": authorization.context_id,
    }


@router.put(
    "/{provider_id}/config",
    response_model=ProviderInfo,
    summary="Configure a provider",
)
async def configure_provider(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: ProviderConfigRequest = Body(...),
) -> ProviderInfo:
    api_key_for_generic_update = body.api_key
    if provider_id == "tokendance" and str(body.api_key or "").strip():
        if not body.credential_ownership_attested:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ownership_attestation_required",
                    "message": "请确认该词元跳动密钥属于当前观猹账号。",
                },
            )
        context = load_watcha_account_context()
        if context.subject_id is None or context.account_epoch < 1:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "watcha_account_required",
                    "message": "请先登录观猹账号，再绑定词元跳动密钥。",
                },
            )
        try:
            manager.bind_tokendance_credential(
                body.api_key,
                expected_owner_subject_id=context.subject_id,
                expected_account_epoch=context.account_epoch,
                ownership_evidence="user_attested",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "tokendance_binding_failed",
                    "message": "词元跳动账号绑定未完成，请重试。",
                },
            ) from exc
        api_key_for_generic_update = None
    ok = manager.update_provider(
        provider_id,
        {
            "api_key": api_key_for_generic_update,
            "base_url": body.base_url,
            "chat_model": body.chat_model,
            "generate_kwargs": body.generate_kwargs,
        },
    )
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found",
        )

    provider_info = await manager.get_provider_info(provider_id)
    if provider_info is None:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found after update",
        )
    return provider_info


def _require_tokendance_oauth_provider(
    manager: ProviderManager,
    provider_id: str,
):
    _require_provider_account_access(provider_id)
    provider = manager.get_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found",
        )
    if provider_id != "tokendance" or (
        getattr(provider, "meta", {}).get("oauth_mode") != "api_key_pkce"
    ):
        raise HTTPException(
            status_code=400,
            detail="This provider does not support API-key OAuth.",
        )
    return provider


@router.post(
    "/{provider_id}/oauth/start",
    response_model=TokenDanceOAuthStart,
    summary="Start provider API-key OAuth with S256 PKCE",
)
async def start_provider_oauth(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
) -> TokenDanceOAuthStart:
    """Start TokenDance authorization without exposing the PKCE verifier."""

    _require_tokendance_oauth_provider(manager, provider_id)
    try:
        callback_base_url = resolve_tokendance_callback_base_url(
            str(request.url),
            os.environ.get("LANTAI_TOKENDANCE_OAUTH_CALLBACK_URL"),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "tokendance_callback_invalid",
                "message": "TokenDance 授权回调未配置或访问地址不匹配，请联系实例管理员。",
            },
        ) from exc
    context = load_watcha_account_context()
    if context.subject_id is None or context.account_epoch < 1:
        raise HTTPException(
            status_code=403,
            detail="Watcha account login is required for TokenDance.",
        )
    return tokendance_oauth_registry.create(
        callback_base_url=callback_base_url,
        owner_subject_id=context.subject_id,
        account_epoch=context.account_epoch,
    )


@router.get(
    "/{provider_id}/oauth/status/{flow_id}",
    response_model=TokenDanceOAuthStatus,
    summary="Read provider OAuth completion status",
)
async def get_provider_oauth_status(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    flow_id: str = Path(...),
) -> TokenDanceOAuthStatus:
    _require_tokendance_oauth_provider(manager, provider_id)
    context = load_watcha_account_context()
    if context.subject_id is None or context.account_epoch < 1:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "watcha_account_required",
                "message": "请先登录观猹账号。",
            },
        )
    try:
        return tokendance_oauth_registry.status(
            flow_id,
            owner_subject_id=context.subject_id,
            account_epoch=context.account_epoch,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "oauth_context_stale",
                "message": "账号已变化，请重新发起词元跳动授权。",
            },
        ) from exc


_OAUTH_SUCCESS_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>澜台授权完成</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body><main><h1>TokenDance 已连接到澜台</h1>
<p>API Key 已安全保存到{storage_location}。现在可以关闭这个页面并返回澜台。</p>
</main></body></html>"""

_OAUTH_FAILURE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>澜台授权未完成</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body><main><h1>TokenDance 授权未完成</h1>
<p>请关闭这个页面，返回澜台后重新授权。</p></main></body></html>"""

_OAUTH_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@router.get(
    "/tokendance/oauth/callback",
    response_class=HTMLResponse,
    summary="Complete TokenDance API-key OAuth",
)
async def complete_tokendance_oauth(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    flow: str = Query(...),
    code: str = Query(...),
) -> HTMLResponse:
    """Exchange one code, encrypt the resulting key, and echo no secret."""

    try:
        callback_base_url = resolve_tokendance_callback_base_url(
            str(request.url),
            os.environ.get("LANTAI_TOKENDANCE_OAUTH_CALLBACK_URL"),
        )
    except ValueError:
        return HTMLResponse(
            _OAUTH_FAILURE_HTML,
            status_code=400,
            headers=_OAUTH_RESPONSE_HEADERS,
        )
    _require_tokendance_oauth_provider(manager, "tokendance")
    try:
        pending = tokendance_oauth_registry.begin_exchange(
            flow,
            callback_base_url=callback_base_url,
        )
    except ValueError:
        # Reject an invalid origin or replay without changing the valid flow.
        return HTMLResponse(
            _OAUTH_FAILURE_HTML,
            status_code=400,
            headers=_OAUTH_RESPONSE_HEADERS,
        )
    try:
        current = load_watcha_account_context()
        if (
            current.subject_id != pending.owner_subject_id
            or current.account_epoch != pending.account_epoch
        ):
            raise ValueError("Watcha account changed before OAuth exchange")
        api_key = await exchange_authorization_code(
            code=code,
            code_verifier=pending.code_verifier,
        )
        current = load_watcha_account_context()
        if (
            current.subject_id != pending.owner_subject_id
            or current.account_epoch != pending.account_epoch
        ):
            raise ValueError("Watcha account changed during OAuth exchange")
        manager.bind_tokendance_credential(
            api_key,
            expected_owner_subject_id=pending.owner_subject_id,
            expected_account_epoch=pending.account_epoch,
            ownership_evidence="oauth_pkce",
        )
        tokendance_oauth_registry.complete(flow)
        return HTMLResponse(
            _OAUTH_SUCCESS_HTML.format(
                storage_location=(
                    "你的云端实例"
                    if callback_base_url.startswith("https:")
                    else "本机"
                ),
            ),
            headers=_OAUTH_RESPONSE_HEADERS,
        )
    except Exception:  # keep upstream and credential details out of the page
        logger.warning("TokenDance OAuth completion failed")
        tokendance_oauth_registry.fail(
            flow,
            "TokenDance authorization failed; please try again.",
        )
        return HTMLResponse(
            _OAUTH_FAILURE_HTML,
            status_code=400,
            headers=_OAUTH_RESPONSE_HEADERS,
        )


@router.post(
    "/custom-providers",
    response_model=ProviderInfo,
    summary="Create a custom provider",
    status_code=201,
)
async def create_custom_provider_endpoint(
    manager: ProviderManager = Depends(get_provider_manager),
    body: CreateCustomProviderRequest = Body(...),
) -> ProviderInfo:
    try:
        provider_info = await manager.add_custom_provider(
            ProviderInfo(
                id=body.id,
                name=body.name,
                base_url=body.default_base_url,
                api_key=body.api_key,
                api_key_prefix=body.api_key_prefix,
                chat_model=body.chat_model,
                extra_models=body.models,
                support_model_discovery=body.support_model_discovery,
                require_api_key=body.require_api_key,
                meta=body.meta,
            ),
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return provider_info


class TestConnectionResponse(BaseModel):
    success: bool = Field(..., description="Whether the test passed")
    message: str = Field(..., description="Human-readable result message")
    receipt_recorded: bool = Field(
        default=False,
        description="Whether this success was saved for the current config",
    )
    configuration_revision: int | None = Field(default=None)
    verified_model_id: str | None = Field(default=None)
    verified_at: str | None = Field(default=None)


class TestProviderRequest(BaseModel):
    api_key: Optional[str] = Field(
        default=None,
        description="Optional API key to test",
    )
    base_url: Optional[str] = Field(
        default=None,
        description="Optional Base URL to test",
    )
    chat_model: Optional[ChatModelName] = Field(
        default=None,
        description="Optional chat model class to test protocol behavior",
    )
    model_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional model ID to test when provider-level connection "
            "checks require a model"
        ),
    )
    generate_kwargs: Optional[dict] = Field(
        default=None,
        description="Optional generation kwargs to test with",
    )


class TestModelRequest(BaseModel):
    model_id: str = Field(..., description="Model ID to test")


def _select_provider_test_model(
    provider: ProviderInfo,
    requested_model_id: Optional[str] = None,
) -> Optional[str]:
    """Choose a model for provider tests that cannot list models directly."""
    target = (requested_model_id or "").strip()
    if target:
        return target

    for model in [*provider.models, *provider.extra_models]:
        model_id = (model.id or "").strip()
        if model_id and model.supports_chat:
            return model_id
    return None


def _test_uses_stored_provider_config(
    provider: ProviderInfo,
    body: Optional[TestProviderRequest],
) -> bool:
    """Return whether a draft test exactly represents persisted config."""
    if body is None:
        return True
    comparisons = (
        (body.api_key, provider.api_key),
        (body.base_url, provider.base_url),
        (body.chat_model, provider.chat_model),
    )
    for requested, stored in comparisons:
        if requested and str(requested).strip() != str(stored).strip():
            return False
    if (
        body.generate_kwargs is not None
        and body.generate_kwargs != provider.generate_kwargs
    ):
        return False
    return True


async def _record_probe_receipt(
    manager: ProviderManager,
    provider_id: str,
    *,
    success: bool,
    tests_stored_config: bool,
    model_id: str | None = None,
) -> dict:
    if not success or not tests_stored_config:
        return {}
    provider = manager.get_provider(provider_id)
    if provider is None:
        return {}
    if model_id and not provider.has_model(model_id):
        # A one-off diagnostic can successfully reach an arbitrary model name,
        # but it must not certify the saved model catalogue or active model.
        return {}
    info = await manager.record_provider_verification(provider_id, model_id)
    verified_at = (
        info.model_verified_at if model_id else info.connection_verified_at
    )
    return {
        "receipt_recorded": True,
        "configuration_revision": info.configuration_revision,
        "verified_model_id": model_id,
        "verified_at": verified_at,
    }


class DiscoverModelsRequest(BaseModel):
    api_key: Optional[str] = Field(
        default=None,
        description="Optional API key to use for discovery",
    )
    base_url: Optional[str] = Field(
        default=None,
        description="Optional Base URL to use for discovery",
    )
    chat_model: Optional[ChatModelName] = Field(
        default=None,
        description="Optional chat model class to use for discovery",
    )


class DiscoverModelsResponse(BaseModel):
    success: bool = Field(..., description="Whether discovery succeeded")
    models: List[ModelInfo] = Field(
        default_factory=list,
        description="Discovered models",
    )
    message: str = Field(
        default="",
        description="Human-readable result message",
    )
    added_count: int = Field(
        default=0,
        description="How many new models were added into provider config",
    )


@router.post(
    "/{provider_id}/test",
    response_model=TestConnectionResponse,
    summary="Test provider connection",
)
async def test_provider(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: Optional[TestProviderRequest] = Body(default=None),
) -> TestConnectionResponse:
    """Test if a provider's URL and API key are valid."""
    try:
        provider = manager.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Provider '{provider_id}' not found")
        if (
            provider_id == "tokendance"
            and body is not None
            and str(body.api_key or "").strip()
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "tokendance_key_not_bound",
                    "message": (
                        "请先把该密钥绑定到当前观猹账号，"
                        "再测试连接。"
                    ),
                },
            )
        authorization = _tokendance_model_authorization(manager, provider_id)
        tests_stored_config = _test_uses_stored_provider_config(provider, body)
        # Ensure we don't accidentally modify provider config during test
        tmp_provider = deepcopy(provider)
        if body:
            if body.api_key:
                tmp_provider.api_key = body.api_key
            if body.base_url:
                tmp_provider.base_url = body.base_url
            if body.chat_model:
                tmp_provider.chat_model = body.chat_model
            if body.generate_kwargs is not None:
                tmp_provider.generate_kwargs = body.generate_kwargs

        test_model_id = _select_provider_test_model(
            tmp_provider,
            body.model_id if body else None,
        )

        if tmp_provider.support_connection_check:
            ok, msg = await tmp_provider.check_connection()
            _assert_tokendance_model_authorization(manager, authorization)
            if ok or not test_model_id:
                return TestConnectionResponse(
                    success=ok,
                    message=(
                        msg or "Connection successful"
                        if ok
                        else f"Connection failed: {msg}"
                    ),
                    **await _record_probe_receipt(
                        manager,
                        provider_id,
                        success=ok,
                        tests_stored_config=tests_stored_config,
                    ),
                )
        elif not test_model_id:
            return TestConnectionResponse(
                success=False,
                message="Connection failed: no model available to test",
            )

        ok, msg = await tmp_provider.check_model_connection(test_model_id)
        _assert_tokendance_model_authorization(manager, authorization)
        return TestConnectionResponse(
            success=ok,
            message=(
                "Connection successful" if ok else f"Connection failed: {msg}"
            ),
            **await _record_probe_receipt(
                manager,
                provider_id,
                success=ok,
                tests_stored_config=tests_stored_config,
                model_id=test_model_id,
            ),
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{provider_id}/discover",
    response_model=DiscoverModelsResponse,
    summary="Discover available models from provider",
)
async def discover_models(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: Optional[DiscoverModelsRequest] = Body(default=None),
    save: bool = Query(
        default=True,
        description="Save discovered models to provider",
    ),
) -> DiscoverModelsResponse:
    try:
        provider = manager.get_provider(provider_id)
        if provider is None:
            raise HTTPException(
                status_code=404,
                detail=f"Provider '{provider_id}' not found",
            )

        existing_model_ids = {
            model.id for model in provider.models + provider.extra_models
        }
        authorization = _tokendance_model_authorization(manager, provider_id)
        if provider_id == "tokendance":
            if body is not None and str(body.api_key or "").strip():
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "tokendance_key_not_bound",
                        "message": (
                            "请先把该密钥绑定到当前观猹账号，"
                            "再刷新模型目录。"
                        ),
                    },
                )
        else:
            ok = manager.update_provider(
                provider_id,
                {
                    "api_key": body.api_key if body else None,
                    "base_url": body.base_url if body else None,
                },
            )
            if not ok:
                raise HTTPException(
                    status_code=404,
                    detail=f"Provider '{provider_id}' not found",
                )
        try:
            result = await manager.fetch_provider_models(
                provider_id,
                save=save and authorization is None,
                raise_on_error=True,
            )
            _assert_tokendance_model_authorization(manager, authorization)
            if save and authorization is not None:
                manager.save_discovered_models(provider_id, result)
            success = True
            failure_message = ""
        except TokenDanceAuthorizationError as exc:
            _raise_tokendance_authorization_error(exc)
        except HTTPException:
            raise
        except Exception as exc:
            result = []
            success = False
            failure_message = str(
                getattr(exc, "message", None) or exc,
            ).strip()

        added_count = 0
        if save and success:
            added_count = sum(
                1 for model in result if model.id not in existing_model_ids
            )

        return DiscoverModelsResponse(
            success=success,
            models=result,
            message=failure_message,
            added_count=added_count,
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{provider_id}/models/test",
    response_model=TestConnectionResponse,
    summary="Test a specific model",
)
async def test_model(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: TestModelRequest = Body(...),
) -> TestConnectionResponse:
    """Test if a specific model works with the configured provider."""
    try:
        provider = manager.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Provider '{provider_id}' not found")
        authorization = _tokendance_model_authorization(manager, provider_id)
        ok, msg = await provider.check_model_connection(model_id=body.model_id)
        _assert_tokendance_model_authorization(manager, authorization)
        return TestConnectionResponse(
            success=ok,
            message=(
                "Model connection successful"
                if ok
                else f"Model connection failed: {msg}"
            ),
            **await _record_probe_receipt(
                manager,
                provider_id,
                success=ok,
                tests_stored_config=True,
                model_id=body.model_id,
            ),
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _create_compatibility_model_client(provider_id: str, model_id: str):
    """Resolve the runtime client lazily to keep the settings router light."""

    from ...agents.model_factory import create_model_diagnostic_client

    return create_model_diagnostic_client(
        model_slot=ModelSlotConfig(provider_id=provider_id, model=model_id),
    )


@router.post(
    "/{provider_id}/models/compatibility-check",
    response_model=ModelCompatibilityReport,
    summary="Check model protocol compatibility",
)
async def test_model_compatibility(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: TestModelRequest = Body(...),
) -> ModelCompatibilityReport:
    """Probe a configured model without changing provider or model settings."""

    _validate_model_slot(manager, provider_id, body.model_id)
    authorization = _tokendance_model_authorization(manager, provider_id)
    try:
        client = _create_compatibility_model_client(
            provider_id,
            body.model_id,
        )
    except Exception as exc:  # provider/runtime boundary; keep details private
        logger.warning(
            "Compatibility probe client is unavailable for "
            "provider=%s model=%s",
            provider_id,
            body.model_id,
        )
        raise HTTPException(
            status_code=400,
            detail="Model client is not available for probing.",
        ) from exc
    report = await run_model_compatibility_probe(
        provider_id=provider_id,
        model_id=body.model_id,
        client=client,
    )
    _assert_tokendance_model_authorization(manager, authorization)
    return report


@router.delete(
    "/custom-providers/{provider_id}",
    response_model=List[ProviderInfo],
    summary="Delete a custom provider",
)
async def delete_custom_provider_endpoint(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
) -> List[ProviderInfo]:
    try:
        ok = manager.remove_custom_provider(provider_id)
        if not ok:
            raise ValueError(f"Custom Provider '{provider_id}' not found")
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await manager.list_provider_info()


@router.post(
    "/{provider_id}/models",
    response_model=ProviderInfo,
    summary="Add a model to a provider",
    status_code=201,
)
async def add_model_endpoint(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: AddModelRequest = Body(...),
) -> ProviderInfo:
    try:
        provider = await manager.add_model_to_provider(
            provider_id=provider_id,
            model_info=ModelInfo(
                id=body.id,
                name=body.name,
                supports_multimodal=body.supports_multimodal,
                supports_image=body.supports_image,
                supports_video=body.supports_video,
                probe_source=body.probe_source,
                is_free=body.is_free,
                context_window=body.context_window,
                user_context_window=body.user_context_window,
                max_output_tokens=body.max_output_tokens,
                recommended_output_reserve=body.recommended_output_reserve,
                tokenizer=body.tokenizer,
                capability_source=(
                    "user-configured"
                    if any(
                        value is not None
                        for value in (
                            body.context_window,
                            body.max_output_tokens,
                            body.recommended_output_reserve,
                            body.tokenizer,
                        )
                    )
                    else None
                ),
            ),
        )  # Validate provider exists and add model
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return provider


class ProbeMultimodalResponse(BaseModel):
    supports_image: bool = Field(
        default=False,
        description="Whether the model supports image input",
    )
    supports_video: bool = Field(
        default=False,
        description="Whether the model supports video input",
    )
    supports_multimodal: bool = Field(
        default=False,
        description="Whether the model supports any multimodal input",
    )
    image_message: str = Field(
        default="",
        description="Probe result message for image support",
    )
    video_message: str = Field(
        default="",
        description="Probe result message for video support",
    )


class ReasoningCapabilityResponse(BaseModel):
    supported: bool = Field(
        default=False,
        description="Whether this model supports a reasoning/thinking toggle",
    )
    mode: str = Field(
        default="",
        description="Provider-specific reasoning control mode",
    )
    fixed: bool = Field(
        default=False,
        description="Whether reasoning is fixed by the selected model",
    )
    default_enabled: bool = Field(
        default=False,
        description="Whether the upstream model usually reasons by default",
    )
    note: str = Field(default="", description="Optional capability note")


@router.get(
    "/{provider_id}/models/{model_id:path}/reasoning-capability",
    response_model=ReasoningCapabilityResponse,
    summary="Get model reasoning capability",
)
async def get_model_reasoning_capability(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
) -> ReasoningCapabilityResponse:
    """Return whether the selected model has a known thinking control."""
    provider = manager.get_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found.",
        )
    capability = get_reasoning_capability(
        provider_id,
        model_id,
        base_url=getattr(provider, "base_url", ""),
        chat_model=getattr(provider, "chat_model", ""),
    )
    return ReasoningCapabilityResponse(
        supported=capability.supported,
        mode=capability.mode,
        fixed=capability.fixed,
        default_enabled=capability.default_enabled,
        note=capability.note,
    )


@router.post(
    "/{provider_id}/models/{model_id:path}/probe-multimodal",
    response_model=ProbeMultimodalResponse,
    summary="Probe model multimodal capability",
)
async def probe_model_multimodal(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
) -> ProbeMultimodalResponse:
    """Probe image and video support by sending lightweight test requests."""
    authorization = _tokendance_model_authorization(manager, provider_id)
    try:
        result = await manager.probe_model_multimodal(provider_id, model_id)
    except TokenDanceAuthorizationError as exc:
        _raise_tokendance_authorization_error(exc)
    _assert_tokendance_model_authorization(manager, authorization)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return ProbeMultimodalResponse(**result)


@router.delete(
    "/{provider_id}/models/{model_id:path}",
    response_model=ProviderInfo,
    summary="Remove a model from a provider",
)
async def remove_model_endpoint(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
) -> ProviderInfo:
    try:
        provider = await manager.delete_model_from_provider(
            provider_id=provider_id,
            model_id=model_id,
        )  # Validate provider and model exist and delete
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return provider


@router.put(
    "/{provider_id}/models/{model_id:path}/config",
    response_model=ProviderInfo,
    summary="Configure per-model generation parameters",
)
async def configure_model(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
    body: ModelConfigRequest = Body(...),
) -> ProviderInfo:
    """Update per-model generate_kwargs that override provider-level
    settings."""
    try:
        config: dict[str, Any] = {}
        for field_name in (
            "generate_kwargs",
            "context_window",
            "user_context_window",
            "max_output_tokens",
            "recommended_output_reserve",
            "tokenizer",
            "show_in_chat",
            "use_for_video",
        ):
            if field_name in body.model_fields_set:
                config[field_name] = getattr(body, field_name)
        provider_info = await manager.update_model_config(
            provider_id=provider_id,
            model_id=model_id,
            config=config,
        )
    except (ValueError, AppBaseException) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return provider_info


@router.get(
    "/active",
    response_model=ActiveModelsInfo,
    summary="Get effective active LLM",
)
async def get_active_models(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    scope: ActiveModelReadScope = Query(default="effective"),
    agent_id: Optional[str] = Query(default=None),
) -> ActiveModelsInfo:
    """Get active model by scope.

    - effective: agent-specific first, otherwise global fallback
    - global: ProviderManager global model only
    - agent: a specific agent's configured model only
    """
    if scope == "global":
        return ActiveModelsInfo(
            active_llm=_available_model_slot(
                manager,
                manager.get_active_model(),
            ),
        )

    if scope == "agent":
        if not agent_id:
            raise HTTPException(
                status_code=400,
                detail="agent_id is required when scope is 'agent'",
            )
        return ActiveModelsInfo(
            active_llm=_available_model_slot(
                manager,
                await _load_agent_model(request, agent_id),
            ),
        )

    try:
        target_agent_id = agent_id
        if target_agent_id is None:
            workspace = await get_agent_for_request(request)
            target_agent_id = workspace.agent_id

        agent_model = await _load_agent_model(request, target_agent_id)
        available_agent_model = _available_model_slot(manager, agent_model)
        if available_agent_model:
            logger.info(
                "Returning agent-specific model for %s: %s",
                target_agent_id,
                available_agent_model,
            )
            return ActiveModelsInfo(active_llm=available_agent_model)
        if agent_model:
            logger.info(
                "Ignoring unavailable agent-specific model for %s: %s",
                target_agent_id,
                agent_model,
            )
    except (
        HTTPException,
        OSError,
        ValueError,
        TypeError,
        AppBaseException,
    ) as exc:
        logger.warning(
            "Failed to get agent-specific model: %s",
            exc,
            exc_info=True,
        )

    global_model = _available_model_slot(manager, manager.get_active_model())
    logger.info("Returning global model: %s", global_model)
    return ActiveModelsInfo(active_llm=global_model)


@router.put(
    "/active",
    response_model=ActiveModelsInfo,
    summary="Set active LLM",
)
async def set_active_model(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
    body: ModelSlotRequest = Body(...),
) -> ActiveModelsInfo:
    """Set active model by scope."""
    _validate_model_slot(manager, body.provider_id, body.model)
    await _validate_model_access(manager, body.provider_id, body.model)

    if body.scope == "global":
        try:
            await manager.activate_model(body.provider_id, body.model)
        except (
            FileNotFoundError,
            RuntimeError,
            ValueError,
            AppBaseException,
        ) as exc:
            message = str(exc)
            lower_msg = message.lower()
            if "provider" in lower_msg and "not found" in lower_msg:
                raise HTTPException(status_code=404, detail=message) from exc
            raise HTTPException(status_code=400, detail=message) from exc
        return ActiveModelsInfo(
            active_llm=manager.get_active_model(),
            context_policy=_model_context_policy_payload(
                manager,
                provider_id=body.provider_id,
                model_id=body.model,
                user_window=DEFAULT_AGENT_MAX_INPUT_LENGTH,
            ),
        )

    if not body.agent_id:
        raise HTTPException(
            status_code=400,
            detail="agent_id is required when scope is 'agent'",
        )

    try:
        workspace = await get_agent_for_request(
            request,
            agent_id=body.agent_id,
        )
        agent_config = load_agent_config(workspace.agent_id)
        agent_config.active_model = ModelSlotConfig(
            provider_id=body.provider_id,
            model=body.model,
        )
        save_agent_config(workspace.agent_id, agent_config)
        # Hot reload agent (async, non-blocking)
        schedule_agent_reload(request, workspace.agent_id)

    except HTTPException:
        raise
    except (OSError, ValueError, TypeError, AppBaseException) as exc:
        logger.warning(
            "Failed to save active model to agent config: %s",
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save active model to agent config: {exc}",
        ) from exc

    return ActiveModelsInfo(
        active_llm=ModelSlotConfig(
            provider_id=body.provider_id,
            model=body.model,
        ),
        context_policy=_model_context_policy_payload(
            manager,
            provider_id=body.provider_id,
            model_id=body.model,
            user_window=getattr(
                agent_config.running,
                "max_input_length",
                DEFAULT_AGENT_MAX_INPUT_LENGTH,
            ),
            mode=str(
                getattr(
                    agent_config.running,
                    "context_policy_mode",
                    "smart",
                )
                or "smart",
            ),
        ),
    )


# =============================================================================
# OpenRouter-specific endpoints for model discovery with filtering
# =============================================================================


class FilterModelsRequest(BaseModel):
    """Request model for filtering OpenRouter models."""

    providers: List[str] = Field(
        default_factory=list,
        description="Filter by provider/series (e.g., ['openai', 'google'])",
    )
    input_modalities: List[str] = Field(
        default_factory=list,
        description="Required input modalities (e.g., ['image'])",
    )
    output_modalities: List[str] = Field(
        default_factory=list,
        description="Required output modalities (e.g., ['text'])",
    )
    max_prompt_price: Optional[float] = Field(
        default=None,
        description="Maximum prompt price per 1M tokens (e.g., 0.000001)",
    )
    is_free: Optional[bool] = Field(
        default=None,
        description="Whether to return only free models",
    )


class SeriesResponse(BaseModel):
    """Response model for available series/providers."""

    series: List[str] = Field(
        default_factory=list,
        description="Provider series (e.g., ['openai', 'google'])",
    )


class DiscoverExtendedResponse(BaseModel):
    """Response model for extended model discovery."""

    success: bool = Field(..., description="Whether discovery succeeded")
    models: List[dict] = Field(
        default_factory=list,
        description="Discovered models with extended metadata",
    )
    providers: List[str] = Field(
        default_factory=list,
        description="Available provider series",
    )
    total_count: int = Field(
        default=0,
        description="Total number of models discovered",
    )


class FilterModelsResponse(BaseModel):
    """Response model for filtered models."""

    success: bool = Field(..., description="Whether filtering succeeded")
    models: List[dict] = Field(
        default_factory=list,
        description="Filtered models with extended metadata",
    )
    total_count: int = Field(
        default=0,
        description="Total number of models matching filters",
    )


@router.get(
    "/openrouter/series",
    response_model=SeriesResponse,
    summary="Get available OpenRouter provider series",
)
async def get_openrouter_series(
    manager: ProviderManager = Depends(get_provider_manager),
) -> SeriesResponse:
    """Get list of available provider/series from OpenRouter."""
    provider = manager.get_provider("openrouter")
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail="OpenRouter provider not found",
        )

    if not isinstance(provider, OpenRouterProvider):
        raise HTTPException(
            status_code=400,
            detail="Provider is not an OpenRouter provider",
        )

    try:
        series = await provider.get_available_providers()
        return SeriesResponse(series=series)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch series: {str(exc)}",
        ) from exc


@router.post(
    "/openrouter/discover-extended",
    response_model=DiscoverExtendedResponse,
    summary="Discover OpenRouter models with extended metadata",
)
async def discover_openrouter_extended(
    manager: ProviderManager = Depends(get_provider_manager),
    body: Optional[DiscoverModelsRequest] = Body(default=None),
) -> DiscoverExtendedResponse:
    """Discover available models from OpenRouter with full metadata."""
    provider = manager.get_provider("openrouter")
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail="OpenRouter provider not found",
        )

    if not isinstance(provider, OpenRouterProvider):
        raise HTTPException(
            status_code=400,
            detail="Provider is not an OpenRouter provider",
        )

    if body and body.api_key:
        manager.update_provider("openrouter", {"api_key": body.api_key})

    try:
        models = await provider.fetch_extended_models()
        providers = await provider.get_available_providers()

        models_dict = [
            {
                "id": m.id,
                "name": m.name,
                "supports_multimodal": m.supports_multimodal,
                "supports_image": m.supports_image,
                "supports_video": m.supports_video,
                "probe_source": m.probe_source,
                "is_free": m.is_free,
                "provider": m.provider,
                "input_modalities": m.input_modalities,
                "output_modalities": m.output_modalities,
                "pricing": m.pricing,
            }
            for m in models
        ]

        return DiscoverExtendedResponse(
            success=True,
            models=models_dict,
            providers=providers,
            total_count=len(models_dict),
        )
    except Exception:
        return DiscoverExtendedResponse(
            success=False,
            models=[],
            providers=[],
            total_count=0,
        )


@router.post(
    "/openrouter/models/filter",
    response_model=FilterModelsResponse,
    summary="Filter OpenRouter models by criteria",
)
async def filter_openrouter_models(
    manager: ProviderManager = Depends(get_provider_manager),
    body: FilterModelsRequest = Body(...),
) -> FilterModelsResponse:
    """Filter OpenRouter models by provider, modalities, and price."""
    provider = manager.get_provider("openrouter")
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail="OpenRouter provider not found",
        )

    if not isinstance(provider, OpenRouterProvider):
        raise HTTPException(
            status_code=400,
            detail="Provider is not an OpenRouter provider",
        )

    try:
        models = await provider.fetch_extended_models()

        filtered_models = provider.filter_models(
            models=models,
            providers=body.providers if body.providers else None,
            input_modalities=(
                body.input_modalities if body.input_modalities else None
            ),
            output_modalities=(
                body.output_modalities if body.output_modalities else None
            ),
            max_prompt_price=body.max_prompt_price,
            is_free=body.is_free,
        )

        models_dict = [
            {
                "id": m.id,
                "name": m.name,
                "supports_multimodal": m.supports_multimodal,
                "supports_image": m.supports_image,
                "supports_video": m.supports_video,
                "probe_source": m.probe_source,
                "is_free": m.is_free,
                "provider": m.provider,
                "input_modalities": m.input_modalities,
                "output_modalities": m.output_modalities,
                "pricing": m.pricing,
            }
            for m in filtered_models
        ]

        return FilterModelsResponse(
            success=True,
            models=models_dict,
            total_count=len(models_dict),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to filter models: {str(exc)}",
        ) from exc
