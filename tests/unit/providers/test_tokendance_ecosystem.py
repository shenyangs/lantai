"""WP-02 contracts for TokenDance catalog, verification and run snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lantai.providers.tokendance_ecosystem import (
    CatalogLayers,
    CatalogRefreshState,
    LiveCatalogSnapshot,
    LiveModel,
    RoutingPolicy,
    RetryDecision,
    RunModelSnapshot,
    VerifiedCapability,
    VerifiedCapabilitiesSnapshot,
    pin_run_model,
    resolve_protocol,
    resolve_retry_ownership,
    validate_pinned_model,
)


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _layers(*protocols: str) -> CatalogLayers:
    live = LiveCatalogSnapshot.create(
        [LiveModel(model_id="mixed", protocols=protocols, context_window=128_000)],
        source="tokendance-public-models",
        fetched_at=NOW,
        source_version="fixture-v1",
    )
    verified = VerifiedCapabilitiesSnapshot.create(
        [
            VerifiedCapability(
                model_id="mixed",
                protocols=(
                    "openai:chat-completions",
                    "openai:responses",
                    "anthropic:messages",
                ),
                purposes=("chat",),
                evidence_ids=("T025",),
            ),
        ],
        revision="verified-v1",
    )
    return CatalogLayers(live=live, verified=verified)


def test_t024_live_catalog_does_not_overwrite_verified_capabilities() -> None:
    layers = _layers("openai:responses")

    assert layers.live.models["mixed"].protocols == ("openai:responses",)
    assert layers.verified.models["mixed"].protocols == (
        "openai:chat-completions",
        "openai:responses",
        "anthropic:messages",
    )
    assert layers.recommendable("mixed", purpose="chat") is True


@pytest.mark.parametrize(
    ("protocols", "expected"),
    [
        (("openai:chat-completions",), "openai:chat-completions"),
        (("openai:responses",), "openai:responses"),
        (("anthropic:messages",), "anthropic:messages"),
        (
            ("openai:responses", "anthropic:messages"),
            "openai:responses",
        ),
        (
            (
                "openai:chat-completions",
                "openai:responses",
                "anthropic:messages",
            ),
            "openai:chat-completions",
        ),
    ],
)
def test_t025_t026_protocol_resolution_is_shared_and_deterministic(
    protocols: tuple[str, ...], expected: str
) -> None:
    decision = resolve_protocol(
        layers=_layers(*protocols),
        model_id="mixed",
        purpose="chat",
        policy=RoutingPolicy(
            allowed_data_scope="tokendance",
            allow_provider_fallback=False,
        ),
    )

    assert decision.protocol == expected
    assert decision.endpoint_family == (
        "anthropic" if expected == "anthropic:messages" else "openai"
    )
    assert decision.allow_provider_fallback is False


def test_t027_failed_refresh_keeps_last_good_snapshot_as_stale() -> None:
    state = CatalogRefreshState()
    initial = _layers("openai:responses").live
    state.accept(initial)

    stale = state.failed("network unavailable", checked_at=NOW)

    assert stale.catalog_hash == initial.catalog_hash
    assert stale.stale is True
    assert stale.refresh_error == "network unavailable"


def test_t028_removed_pinned_model_pauses_instead_of_switching() -> None:
    original = _layers("openai:responses")
    decision = resolve_protocol(
        layers=original,
        model_id="mixed",
        purpose="chat",
        policy=RoutingPolicy(allowed_data_scope="tokendance"),
    )
    pinned = pin_run_model(
        run_id="run-1",
        owner_binding_id="binding-1",
        binding_revision=3,
        decision=decision,
        layers=original,
    )
    empty_live = LiveCatalogSnapshot.create(
        [],
        source="tokendance-public-models",
        fetched_at=NOW,
        source_version="fixture-v2",
    )

    validation = validate_pinned_model(
        pinned,
        CatalogLayers(live=empty_live, verified=original.verified),
    )

    assert validation.action == "pause_for_confirmation"
    assert validation.replacement_model_id is None


def test_t029_unverified_model_is_not_recommended() -> None:
    layers = _layers("openai:responses")
    unverified = LiveCatalogSnapshot.create(
        [
            *layers.live.models.values(),
            LiveModel(model_id="new-live-model", protocols=("openai:responses",)),
        ],
        source="tokendance-public-models",
        fetched_at=NOW,
        source_version="fixture-v2",
    )

    assert CatalogLayers(live=unverified, verified=layers.verified).recommendable(
        "new-live-model", purpose="chat"
    ) is False


def test_t030_fallback_cannot_broaden_data_disclosure() -> None:
    with pytest.raises(ValueError, match="data disclosure"):
        resolve_protocol(
            layers=_layers("openai:responses"),
            model_id="mixed",
            purpose="chat",
            policy=RoutingPolicy(
                allowed_data_scope="tokendance",
                allow_provider_fallback=True,
            ),
        )


@pytest.mark.parametrize("operation_kind", ["read", "model_call"])
def test_t031_one_retry_owner_disables_nested_retries(
    operation_kind: str,
) -> None:
    decision = resolve_retry_ownership(
        operation_kind=operation_kind,
        runtime_max_attempts=3,
        provider_idempotency_verified=False,
    )

    assert isinstance(decision, RetryDecision)
    assert decision.owner == "runtime"
    assert decision.sdk_max_attempts == 1
    assert decision.transport_max_attempts == 1
    assert decision.runtime_max_attempts == 3


def test_t031_ambiguous_side_effect_is_never_replayed_without_idempotency() -> None:
    decision = resolve_retry_ownership(
        operation_kind="side_effect",
        runtime_max_attempts=3,
        provider_idempotency_verified=False,
    )

    assert decision.runtime_max_attempts == 1
    assert decision.on_ambiguous == "probe_or_pause"


def test_run_snapshot_is_bound_to_owner_and_catalog_revisions() -> None:
    layers = _layers("openai:responses")
    decision = resolve_protocol(
        layers=layers,
        model_id="mixed",
        purpose="chat",
        policy=RoutingPolicy(allowed_data_scope="tokendance"),
    )
    snapshot = pin_run_model(
        run_id="run-1",
        owner_binding_id="binding-1",
        binding_revision=7,
        decision=decision,
        layers=layers,
    )

    assert isinstance(snapshot, RunModelSnapshot)
    assert snapshot.protocol == "openai:responses"
    assert snapshot.catalog_revision == layers.live.catalog_hash
    assert snapshot.verification_revision == "verified-v1"
    assert snapshot.owner_binding_id == "binding-1"
    assert snapshot.binding_revision == 7
