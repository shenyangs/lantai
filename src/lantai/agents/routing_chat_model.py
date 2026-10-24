# -*- coding: utf-8 -*-
"""ChatModel router for local/cloud model selection."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Type

from pydantic import BaseModel

from ..config.config import AgentsLLMRoutingConfig
from ..runtime.models import (
    LantaiModelClient,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)

logger = logging.getLogger(__name__)


Route = Literal["local", "cloud"]


@dataclass(frozen=True)
class RoutingDecision:
    route: Route
    reasons: tuple[str, ...] = field(default_factory=tuple)
    selected_provider: str | None = None
    selected_model: str | None = None
    mode: str | None = None
    previous_route_inherited: bool = False
    estimated_context_tokens: int | None = None
    estimated_cost_points: int | float | None = None
    fallback_reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "selected_provider": self.selected_provider,
            "selected_model": self.selected_model,
            "reasons": list(self.reasons),
            "mode": self.mode,
            "previous_route_inherited": self.previous_route_inherited,
            "estimated_context_tokens": self.estimated_context_tokens,
            "estimated_cost_points": self.estimated_cost_points,
            "fallback_reason": self.fallback_reason,
        }


class RoutingPolicy:
    """Select a route using the configured default mode."""

    def __init__(self, cfg: AgentsLLMRoutingConfig):
        self.cfg = cfg

    def decide(
        self,
        *,
        text: str = "",
        channel: str = "",
        tools_available: bool = True,
        previous_decision: RoutingDecision | dict[str, Any] | None = None,
    ) -> RoutingDecision:
        del channel, tools_available
        previous = _coerce_routing_decision(previous_decision)

        short_continuation = str(text or "").strip().lower() in {
            "继续",
            "好",
            "好的",
            "开始",
            "执行",
            "冲",
            "可以",
            "行",
            "嗯",
            "ok",
            "go",
            "next",
            "continue",
        }
        if short_continuation and previous is not None:
            return RoutingDecision(
                route=previous.route,
                reasons=(
                    "short_continuation:inherit_previous_route",
                    *previous.reasons,
                ),
                mode=previous.mode,
                previous_route_inherited=True,
                estimated_context_tokens=previous.estimated_context_tokens,
                estimated_cost_points=previous.estimated_cost_points,
                fallback_reason=previous.fallback_reason,
            )

        if getattr(self.cfg, "mode", "local_first") == "cloud_first":
            return RoutingDecision(
                route="cloud",
                reasons=("mode:cloud_first",),
                mode="cloud_first",
            )

        return RoutingDecision(
            route="local",
            reasons=("mode:local_first",),
            mode="local_first",
        )


def _coerce_routing_decision(
    value: RoutingDecision | dict[str, Any] | None,
) -> RoutingDecision | None:
    if isinstance(value, RoutingDecision):
        return value
    if not isinstance(value, dict):
        return None
    route = str(value.get("route") or "")
    if route not in {"local", "cloud"}:
        return None
    reasons = tuple(
        str(reason).strip()
        for reason in value.get("reasons", ())
        if str(reason).strip()
    )
    return RoutingDecision(
        route=route,  # type: ignore[arg-type]
        reasons=reasons,
        selected_provider=value.get("selected_provider") or value.get("provider"),
        selected_model=value.get("selected_model") or value.get("model"),
        mode=value.get("mode"),
        previous_route_inherited=bool(value.get("previous_route_inherited")),
        estimated_context_tokens=value.get("estimated_context_tokens"),
        estimated_cost_points=value.get("estimated_cost_points"),
        fallback_reason=value.get("fallback_reason"),
    )


def _routing_scope_from_mapping(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict):
        return None
    agent_id = str(metadata.get("agent_id") or "").strip()
    principal = str(metadata.get("user_id") or "").strip()
    session_id = str(
        metadata.get("root_session_id")
        or metadata.get("chat_id")
        or metadata.get("session_id")
        or "",
    ).strip()
    if not session_id:
        return None
    return "|".join(part for part in (agent_id, principal, session_id) if part)


def _routing_scope_from_kwargs(kwargs: dict[str, Any]) -> str | None:
    request_context = kwargs.get("request_context")
    if isinstance(request_context, dict):
        scope = _routing_scope_from_mapping(request_context)
        if scope:
            return scope
    metadata = kwargs.get("metadata")
    if isinstance(metadata, dict):
        scope = _routing_scope_from_mapping(metadata)
        if scope:
            return scope
    session_id = str(kwargs.get("session_id") or "").strip()
    if session_id:
        return session_id
    return None


class _RoutingDecisionStore:
    def __init__(self, max_entries: int = 256) -> None:
        self._max_entries = max(1, max_entries)
        self._decisions: OrderedDict[str, RoutingDecision] = OrderedDict()

    def get(self, scope: str | None) -> RoutingDecision | None:
        if not scope:
            return None
        decision = self._decisions.get(scope)
        if decision is not None:
            self._decisions.move_to_end(scope)
        return decision

    def remember(self, scope: str | None, decision: RoutingDecision) -> None:
        if not scope:
            return
        self._decisions[scope] = decision
        self._decisions.move_to_end(scope)
        while len(self._decisions) > self._max_entries:
            self._decisions.popitem(last=False)


@dataclass(frozen=True)
class RoutingEndpoint:
    provider_id: str
    model_name: str
    model: Any
    formatter: Any
    formatter_family: Type[Any]


class RoutingChatModel:
    """Route between local and cloud chat model-like endpoints."""

    def __init__(
        self,
        *,
        local_endpoint: RoutingEndpoint,
        cloud_endpoint: RoutingEndpoint,
        routing_cfg: AgentsLLMRoutingConfig,
    ) -> None:
        self.model_name = "routing"
        self.stream = bool(getattr(local_endpoint.model, "stream", True))
        self.local_endpoint = local_endpoint
        self.cloud_endpoint = cloud_endpoint
        self.routing_cfg = routing_cfg
        self.policy = RoutingPolicy(routing_cfg)
        self._decision_store = _RoutingDecisionStore()

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        structured_model: Type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> Any | AsyncGenerator[Any, None]:
        text = " ".join(
            message["content"]
            for message in messages
            if message.get("role") == "user"
            and isinstance(message.get("content"), str)
        )
        scope = _routing_scope_from_kwargs(kwargs)
        decision = self.policy.decide(
            text=text,
            tools_available=tools is not None,
            previous_decision=self._decision_store.get(scope),
        )
        endpoint = (
            self.local_endpoint
            if decision.route == "local"
            else self.cloud_endpoint
        )
        decision = replace(
            decision,
            selected_provider=endpoint.provider_id,
            selected_model=endpoint.model_name,
            estimated_context_tokens=max(1, int(len(text) / 1.8)) if text else 0,
        )
        self._decision_store.remember(scope, decision)

        logger.debug(
            "LLM routing decision: route=%s provider=%s model=%s reasons=%s",
            decision.route,
            endpoint.provider_id,
            endpoint.model_name,
            ",".join(decision.reasons),
        )

        return await endpoint.model(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            structured_model=structured_model,
            **kwargs,
        )


@dataclass(frozen=True)
class RoutingModelClientEndpoint:
    provider_id: str
    model_name: str
    client: LantaiModelClient


class RoutingLantaiModelClient:
    """Route Native Runtime requests between local and cloud model clients."""

    def __init__(
        self,
        *,
        local_endpoint: RoutingModelClientEndpoint,
        cloud_endpoint: RoutingModelClientEndpoint,
        routing_cfg: AgentsLLMRoutingConfig,
    ) -> None:
        self.local_endpoint = local_endpoint
        self.cloud_endpoint = cloud_endpoint
        self.routing_cfg = routing_cfg
        self.policy = RoutingPolicy(routing_cfg)
        self._decision_store = _RoutingDecisionStore()
        self.provider_id = "routing"
        self.model_name = "routing"

    def _select_endpoint(
        self,
        request: ModelRequest,
    ) -> tuple[RoutingModelClientEndpoint, RoutingDecision]:
        text = " ".join(
            message.text_content()
            for message in request.messages
            if message.role == "user"
        )
        scope = _routing_scope_from_mapping(dict(request.metadata or {}))
        decision = self.policy.decide(
            text=text,
            tools_available=bool(request.tools),
            previous_decision=self._decision_store.get(scope),
        )
        endpoint = (
            self.local_endpoint
            if decision.route == "local"
            else self.cloud_endpoint
        )
        decision = replace(
            decision,
            selected_provider=endpoint.provider_id,
            selected_model=endpoint.model_name,
            estimated_context_tokens=max(1, int(len(text) / 1.8)) if text else 0,
        )
        self._decision_store.remember(scope, decision)
        logger.debug(
            "Native LLM routing decision: route=%s provider=%s model=%s reasons=%s",
            decision.route,
            endpoint.provider_id,
            endpoint.model_name,
            ",".join(decision.reasons),
        )
        return endpoint, decision

    async def stream(self, request: ModelRequest):
        endpoint, decision = self._select_endpoint(request)
        async for event in endpoint.client.stream(request):
            if event.kind == "message_start":
                event.metadata.setdefault("routing_decision", decision.to_payload())
            yield event

    async def complete(self, request: ModelRequest) -> ModelResponse:
        endpoint, _decision = self._select_endpoint(request)
        return await endpoint.client.complete(request)
