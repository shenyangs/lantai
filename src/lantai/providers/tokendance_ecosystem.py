"""TokenDance catalog layers and immutable per-run routing decisions.

The public catalog says what TokenDance currently advertises.  Lantai's
verified snapshot says what this release has actually exercised.  A run
snapshot binds their intersection before the first external effect so a
refresh cannot silently change protocol, owner, or disclosure policy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Iterable, Mapping


_CHAT_PROTOCOL_PRIORITY = (
    "openai:chat-completions",
    "openai:responses",
    "anthropic:messages",
)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class LiveModel:
    model_id: str
    protocols: tuple[str, ...]
    context_window: int | None = None

    def __init__(
        self,
        *,
        model_id: str,
        protocols: Iterable[str],
        context_window: int | None = None,
    ) -> None:
        object.__setattr__(self, "model_id", str(model_id).strip())
        object.__setattr__(
            self,
            "protocols",
            tuple(dict.fromkeys(str(item).strip() for item in protocols if item)),
        )
        object.__setattr__(self, "context_window", context_window)
        if not self.model_id:
            raise ValueError("live model id is required")


@dataclass(frozen=True, slots=True)
class LiveCatalogSnapshot:
    models: Mapping[str, LiveModel]
    source: str
    fetched_at: str
    source_version: str
    catalog_hash: str
    stale: bool = False
    refresh_error: str | None = None

    @classmethod
    def create(
        cls,
        models: Iterable[LiveModel],
        *,
        source: str,
        fetched_at: datetime,
        source_version: str,
    ) -> "LiveCatalogSnapshot":
        index = {model.model_id: model for model in models}
        canonical = [
            {
                "id": model.model_id,
                "protocols": list(model.protocols),
                "context_window": model.context_window,
            }
            for model in sorted(index.values(), key=lambda item: item.model_id)
        ]
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(
            models=index,
            source=str(source),
            fetched_at=_utc_iso(fetched_at),
            source_version=str(source_version),
            catalog_hash=digest,
        )


@dataclass(frozen=True, slots=True)
class VerifiedCapability:
    model_id: str
    protocols: tuple[str, ...]
    purposes: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __init__(
        self,
        *,
        model_id: str,
        protocols: Iterable[str],
        purposes: Iterable[str],
        evidence_ids: Iterable[str],
    ) -> None:
        object.__setattr__(self, "model_id", str(model_id).strip())
        object.__setattr__(self, "protocols", tuple(dict.fromkeys(protocols)))
        object.__setattr__(self, "purposes", tuple(dict.fromkeys(purposes)))
        object.__setattr__(self, "evidence_ids", tuple(dict.fromkeys(evidence_ids)))
        if not self.model_id or not self.evidence_ids:
            raise ValueError("verified capability requires model and evidence")


@dataclass(frozen=True, slots=True)
class VerifiedCapabilitiesSnapshot:
    models: Mapping[str, VerifiedCapability]
    revision: str

    @classmethod
    def create(
        cls,
        models: Iterable[VerifiedCapability],
        *,
        revision: str,
    ) -> "VerifiedCapabilitiesSnapshot":
        return cls(
            models={model.model_id: model for model in models},
            revision=str(revision),
        )


@dataclass(frozen=True, slots=True)
class CatalogLayers:
    live: LiveCatalogSnapshot
    verified: VerifiedCapabilitiesSnapshot

    def recommendable(self, model_id: str, *, purpose: str) -> bool:
        live = self.live.models.get(model_id)
        verified = self.verified.models.get(model_id)
        if live is None or verified is None or purpose not in verified.purposes:
            return False
        return bool(set(live.protocols).intersection(verified.protocols))


class CatalogRefreshState:
    """Keep the last good public snapshot when refresh fails."""

    def __init__(self) -> None:
        self._last_good: LiveCatalogSnapshot | None = None

    def accept(self, snapshot: LiveCatalogSnapshot) -> LiveCatalogSnapshot:
        clean = replace(snapshot, stale=False, refresh_error=None)
        self._last_good = clean
        return clean

    def failed(
        self,
        error: str,
        *,
        checked_at: datetime,
    ) -> LiveCatalogSnapshot:
        del checked_at  # The source fetch time remains the last-good timestamp.
        if self._last_good is None:
            raise RuntimeError("no last-good TokenDance catalog is available")
        return replace(
            self._last_good,
            stale=True,
            refresh_error=str(error)[:240],
        )


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    allowed_data_scope: str
    allow_provider_fallback: bool = False


@dataclass(frozen=True, slots=True)
class ProtocolDecision:
    model_id: str
    protocol: str
    endpoint_family: str
    native_kind: str
    purpose: str
    adapter_version: str
    allowed_data_scope: str
    allow_provider_fallback: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """One bounded retry owner for a routed operation.

    Provider SDK and HTTP transport retries stay disabled.  Runtime owns the
    explainable attempt count, and an ambiguous side effect is replayable only
    when the upstream idempotency contract has been independently verified.
    """

    owner: str
    operation_kind: str
    sdk_max_attempts: int
    transport_max_attempts: int
    runtime_max_attempts: int
    provider_idempotency_verified: bool
    on_ambiguous: str


def resolve_retry_ownership(
    *,
    operation_kind: str,
    runtime_max_attempts: int,
    provider_idempotency_verified: bool,
) -> RetryDecision:
    if operation_kind not in {"read", "model_call", "side_effect"}:
        raise ValueError("unsupported retry operation kind")
    if (
        not isinstance(runtime_max_attempts, int)
        or isinstance(runtime_max_attempts, bool)
        or not 1 <= runtime_max_attempts <= 3
    ):
        raise ValueError("runtime retry attempts must be between one and three")
    effective_attempts = runtime_max_attempts
    on_ambiguous = "fail_without_replay"
    if operation_kind == "side_effect":
        if provider_idempotency_verified:
            on_ambiguous = "retry_same_idempotency_key"
        else:
            effective_attempts = 1
            on_ambiguous = "probe_or_pause"
    return RetryDecision(
        owner="runtime",
        operation_kind=operation_kind,
        sdk_max_attempts=1,
        transport_max_attempts=1,
        runtime_max_attempts=effective_attempts,
        provider_idempotency_verified=provider_idempotency_verified,
        on_ambiguous=on_ambiguous,
    )


def resolve_protocol(
    *,
    layers: CatalogLayers,
    model_id: str,
    purpose: str,
    policy: RoutingPolicy,
) -> ProtocolDecision:
    if policy.allow_provider_fallback and policy.allowed_data_scope != "any_provider":
        raise ValueError("provider fallback would broaden data disclosure")
    live = layers.live.models.get(model_id)
    verified = layers.verified.models.get(model_id)
    if live is None:
        raise ValueError(f"model is absent from live catalog: {model_id}")
    if verified is None or purpose not in verified.purposes:
        raise ValueError(f"model capability is not verified: {model_id}/{purpose}")
    common = set(live.protocols).intersection(verified.protocols)
    priorities = _CHAT_PROTOCOL_PRIORITY if purpose == "chat" else live.protocols
    protocol = next((item for item in priorities if item in common), None)
    if protocol is None:
        raise ValueError(f"no verified live protocol for {model_id}/{purpose}")
    if protocol == "anthropic:messages":
        family, native_kind = "anthropic", "anthropic_messages"
    elif protocol == "openai:responses":
        family, native_kind = "openai", "openai_responses"
    else:
        family, native_kind = "openai", "openai_compatible"
    return ProtocolDecision(
        model_id=model_id,
        protocol=protocol,
        endpoint_family=family,
        native_kind=native_kind,
        purpose=purpose,
        adapter_version="tokendance-routing-v1",
        allowed_data_scope=policy.allowed_data_scope,
        allow_provider_fallback=policy.allow_provider_fallback,
        reasons=("live_catalog", "verified_capability", "fixed_priority"),
    )


@dataclass(frozen=True, slots=True)
class RunModelSnapshot:
    run_id: str
    provider_id: str
    model_id: str
    protocol: str
    endpoint_family: str
    native_kind: str
    adapter_version: str
    catalog_revision: str
    verification_revision: str
    owner_binding_id: str
    binding_revision: int
    allowed_data_scope: str
    allow_provider_fallback: bool
    resolved_at: str
    snapshot_revision: int = 1


def pin_run_model(
    *,
    run_id: str,
    owner_binding_id: str,
    binding_revision: int,
    decision: ProtocolDecision,
    layers: CatalogLayers,
) -> RunModelSnapshot:
    if not run_id or not owner_binding_id or binding_revision < 1:
        raise ValueError("run and owner binding are required")
    return RunModelSnapshot(
        run_id=run_id,
        provider_id="tokendance",
        model_id=decision.model_id,
        protocol=decision.protocol,
        endpoint_family=decision.endpoint_family,
        native_kind=decision.native_kind,
        adapter_version=decision.adapter_version,
        catalog_revision=layers.live.catalog_hash,
        verification_revision=layers.verified.revision,
        owner_binding_id=owner_binding_id,
        binding_revision=binding_revision,
        allowed_data_scope=decision.allowed_data_scope,
        allow_provider_fallback=decision.allow_provider_fallback,
        resolved_at=_utc_iso(datetime.now(timezone.utc)),
    )


@dataclass(frozen=True, slots=True)
class PinnedModelValidation:
    action: str
    reason: str
    replacement_model_id: str | None = None


def validate_pinned_model(
    snapshot: RunModelSnapshot,
    layers: CatalogLayers,
) -> PinnedModelValidation:
    live = layers.live.models.get(snapshot.model_id)
    verified = layers.verified.models.get(snapshot.model_id)
    if live is None or verified is None:
        return PinnedModelValidation(
            action="pause_for_confirmation",
            reason="pinned model is no longer available",
        )
    if snapshot.protocol not in live.protocols or snapshot.protocol not in verified.protocols:
        return PinnedModelValidation(
            action="pause_for_confirmation",
            reason="pinned protocol is no longer available",
        )
    return PinnedModelValidation(action="continue", reason="pinned route remains valid")


__all__ = [
    "CatalogLayers",
    "CatalogRefreshState",
    "LiveCatalogSnapshot",
    "LiveModel",
    "PinnedModelValidation",
    "ProtocolDecision",
    "RoutingPolicy",
    "RetryDecision",
    "RunModelSnapshot",
    "VerifiedCapabilitiesSnapshot",
    "VerifiedCapability",
    "pin_run_model",
    "resolve_protocol",
    "resolve_retry_ownership",
    "validate_pinned_model",
]
