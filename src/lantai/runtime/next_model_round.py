# -*- coding: utf-8 -*-
"""Prepare model rounds from one immutable RuntimeInput lineage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Sequence, cast

from .input import RuntimeInput
from .models import ModelMessage, ModelRequest
from .tools import ToolSpec


class ModelRoundLineageError(ValueError):
    """Raised when a prepared round drifts from its initial turn binding."""


def _sha256_json(value: object) -> str:
    try:
        canonical = _canonical_json_value(value)
        encoded = json.dumps(
            canonical,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ModelRoundLineageError(
            "Model round lineage contains an unsupported value",
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_value(value: object) -> object:
    if value is None or type(value) in {str, bool, int, float}:
        return value
    if type(value) in {list, tuple}:
        return [
            _canonical_json_value(item)
            for item in value  # type: ignore[union-attr]
        ]
    if type(value) is dict:
        result: dict[str, object] = {}
        for key, item in value.items():  # type: ignore[union-attr]
            if type(key) is not str:
                raise TypeError("lineage object keys must be strings")
            result[key] = _canonical_json_value(item)
        return result
    raise TypeError("unsupported lineage value")


def _detached_json_object(value: dict[str, Any]) -> dict[str, Any]:
    try:
        canonical = _canonical_json_value(value)
    except TypeError:
        raise ModelRoundLineageError(
            "Model round request contains an unsupported value",
        ) from None
    if type(canonical) is not dict:
        raise ModelRoundLineageError(
            "Model round request metadata must be an object",
        )
    return cast(dict[str, Any], canonical)


@dataclass(frozen=True, slots=True)
class ModelRoundLineage:
    """Stable identity and policy binding for every round in one turn."""

    session_id: str
    turn_id: str
    run_id: str
    attempt_id: str | None
    runtime_generation: int | None
    runtime_revision: int | None
    contract_hash: str | None
    policy_snapshot_hash: str | None
    model_slot: str | None
    model_route: dict[str, Any]
    model_route_sha256: str | None
    tool_policy_sha256: str
    allowed_tool_bindings: tuple[tuple[str, str], ...]
    binding_sha256: str

    def protected_metadata(self) -> dict[str, object]:
        pairs = {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "runtime_generation": self.runtime_generation,
            "runtime_revision": self.runtime_revision,
            "contract_hash": self.contract_hash,
            "policy_snapshot_hash": self.policy_snapshot_hash,
            "model_route": (
                _detached_json_object(self.model_route)
                if self.model_route
                else None
            ),
            "model_route_sha256": self.model_route_sha256,
        }
        return {
            key: value
            for key, value in pairs.items()
            if value is not None and value != ""
        }


@dataclass(frozen=True, slots=True)
class PreparedModelRound:
    """Detached request plus the lineage used to prepare it."""

    request: ModelRequest
    lineage_sha256: str


def _optional_text(value: object) -> str | None:
    if type(value) is not str:
        return None
    text = value.strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if type(value) is int:
        return value
    if type(value) is not str:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def capture_model_round_lineage(
    runtime_input: RuntimeInput,
    *,
    allowed_tools: Sequence[ToolSpec],
    model_slot: str,
    tool_policy_context: dict[str, Any],
    turn_id: str | None = None,
    model_route: dict[str, Any] | None = None,
) -> ModelRoundLineage:
    """Capture the initial request/policy identity without mutable messages."""

    request = runtime_input.request
    metadata = dict(runtime_input.request.metadata)
    metadata.update(runtime_input.metadata)
    effective_turn_id = str(turn_id or request.run_id or request.session_id)
    run_id = str(request.run_id or effective_turn_id)
    detached_route = _detached_json_object(model_route or {})
    if detached_route:
        existing_run_id = detached_route.get("run_id")
        if existing_run_id not in (None, "", run_id):
            raise ModelRoundLineageError(
                "Model route snapshot belongs to a different run",
            )
        detached_route["run_id"] = run_id
    model_route_sha256 = (
        _sha256_json(detached_route) if detached_route else None
    )
    tool_policy_sha256 = _sha256_json(tool_policy_context)
    tool_bindings = tuple(
        (
            tool.name,
            _sha256_json(tool.model_dump(mode="json")),
        )
        for tool in allowed_tools
    )
    if len({name for name, _digest in tool_bindings}) != len(tool_bindings):
        raise ModelRoundLineageError(
            "Model round tool catalog contains duplicate names",
        )
    attempt_id = _optional_text(metadata.get("attempt_id"))
    runtime_generation = _optional_int(
        metadata.get("runtime_generation"),
    )
    runtime_revision = _optional_int(metadata.get("runtime_revision"))
    contract_hash = _optional_text(metadata.get("contract_hash"))
    policy_snapshot_hash = _optional_text(
        metadata.get("policy_snapshot_hash"),
    )
    payload = {
        "agent_id": request.agent_id,
        "session_id": request.session_id,
        "user_id": request.user_id,
        "channel": request.channel,
        "chat_id": request.chat_id,
        "run_id": run_id,
        "root_session_id": request.root_session_id,
        "workspace_dir": request.workspace_dir,
        "attempt_id": attempt_id,
        "runtime_generation": runtime_generation,
        "runtime_revision": runtime_revision,
        "contract_hash": contract_hash,
        "policy_snapshot_hash": policy_snapshot_hash,
        "input_model_slot": runtime_input.model_slot,
        "model_slot": model_slot,
        "model_route": detached_route,
        "model_route_sha256": model_route_sha256,
        "tool_policy_sha256": tool_policy_sha256,
        "allowed_tool_bindings": tool_bindings,
    }
    return ModelRoundLineage(
        session_id=request.session_id,
        turn_id=effective_turn_id,
        run_id=run_id,
        attempt_id=attempt_id,
        runtime_generation=runtime_generation,
        runtime_revision=runtime_revision,
        contract_hash=contract_hash,
        policy_snapshot_hash=policy_snapshot_hash,
        model_slot=model_slot,
        model_route=detached_route,
        model_route_sha256=model_route_sha256,
        tool_policy_sha256=tool_policy_sha256,
        allowed_tool_bindings=tool_bindings,
        binding_sha256=_sha256_json(payload),
    )


def prepare_next_model_round(
    *,
    lineage: ModelRoundLineage,
    messages: Sequence[ModelMessage],
    tools: Sequence[ToolSpec],
    tool_choice: str | None,
    model_slot: str | None,
    tool_policy_context: dict[str, Any],
    generation: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> PreparedModelRound:
    """Build one detached request while enforcing the initial turn binding."""

    bound_metadata = _detached_json_object(metadata or {})
    for key, expected in lineage.protected_metadata().items():
        actual = bound_metadata.get(key)
        if actual is None or (type(actual) is str and actual == ""):
            bound_metadata[key] = expected
            continue
        if type(actual) is not type(expected) or actual != expected:
            raise ModelRoundLineageError(
                f"Model round metadata changed protected field: {key}",
            )
    existing_lineage = bound_metadata.get("model_round_lineage_sha256")
    if existing_lineage not in (None, lineage.binding_sha256):
        raise ModelRoundLineageError(
            "Model round metadata changed its lineage binding",
        )
    bound_metadata["model_round_lineage_sha256"] = lineage.binding_sha256
    if model_slot != lineage.model_slot:
        raise ModelRoundLineageError(
            "Model round changed the initial model slot",
        )
    if _sha256_json(tool_policy_context) != lineage.tool_policy_sha256:
        raise ModelRoundLineageError(
            "Model round changed the initial tool policy",
        )
    allowed_by_name = dict(lineage.allowed_tool_bindings)
    current_names: set[str] = set()
    for tool in tools:
        if tool.name in current_names:
            raise ModelRoundLineageError(
                "Model round tool request contains duplicate names",
            )
        current_names.add(tool.name)
        expected_digest = allowed_by_name.get(tool.name)
        if expected_digest is None or expected_digest != _sha256_json(
            tool.model_dump(mode="json"),
        ):
            raise ModelRoundLineageError(
                f"Model round changed protected tool: {tool.name}",
            )
    request = ModelRequest(
        messages=[message.model_copy(deep=True) for message in messages],
        tools=[tool.model_copy(deep=True) for tool in tools],
        tool_choice=tool_choice,
        generation=_detached_json_object(generation or {}),
        metadata=bound_metadata,
    )
    return PreparedModelRound(
        request=request,
        lineage_sha256=lineage.binding_sha256,
    )


__all__ = [
    "ModelRoundLineage",
    "ModelRoundLineageError",
    "PreparedModelRound",
    "capture_model_round_lineage",
    "prepare_next_model_round",
]
