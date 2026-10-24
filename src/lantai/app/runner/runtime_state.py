# -*- coding: utf-8 -*-
"""Helpers for deriving one typed runtime state from chat fragments."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ChatRuntimeState, ChatSpec
from ..experience.schemas import TurnEventPreview
from ..experience import resolve_effective_workspace_dir_from_meta
from ...runtime.runs import ACTIVE_RUNTIME_RUN_STATUSES, RuntimeRunContract
from ...runtime.store import RuntimeRunStore, RuntimeRunStoreError
from ...runtime.turn_outcomes import normalize_turn_outcome_status


@dataclass(frozen=True)
class CanonicalChatRuntimeIdentity:
    """Validated Runtime identity for one chat-owned run attempt.

    ``TaskTracker`` is indexed by ``chat_id`` and its legacy ``attempt_id``
    slot currently carries the public Run ID.  Neither value is therefore an
    ownership authority.  Callers must obtain this projection from the
    checksum-protected RuntimeRunStore before exposing canonical identities.
    """

    run_id: str
    attempt_id: str
    control_revision: int
    generation: int | None
    task_revision: int | None
    lease_owner: str
    workspace_dir: str
    source: str
    owner_session_id: str
    owner_agent_id: str


def iso_string(value: Any) -> str | None:
    """Return a stable string timestamp when *value* is timestamp-like."""

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def pending_turn_dict(chat_spec: ChatSpec) -> dict[str, Any]:
    """Return the pending turn meta dict, or an empty dict."""

    raw = (chat_spec.meta or {}).get("pending_turn")
    return raw if isinstance(raw, dict) else {}


def pending_turn_text(pending_turn: dict[str, Any]) -> str | None:
    """Return pending user text when the pending turn still carries one."""

    text = pending_turn.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return None


def pending_turn_run_id(pending_turn: dict[str, Any]) -> str | None:
    """Return the best available run id from pending turn metadata."""

    for key in ("run_id", "last_turn_run_id", "turn_id"):
        value = pending_turn.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _same_workspace(left: str, right: Path) -> bool:
    """Compare persisted workspace identity without requiring it to exist."""

    try:
        return Path(left).expanduser().resolve(strict=False) == right.resolve(
            strict=False,
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _chat_contract_matches_owner(
    contract: RuntimeRunContract,
    *,
    chat_spec: ChatSpec,
    agent_id: str,
    workspace_dir: Path,
    run_hint: str | None,
    active_run: dict[str, Any] | None,
) -> bool:
    """Return whether a store contract authoritatively owns this chat run."""

    if contract.source != "chat":
        return False
    if contract.status not in ACTIVE_RUNTIME_RUN_STATUSES:
        return False
    if contract.lease_owner != f"chat:{chat_spec.id}":
        return False
    if contract.lease_expires_at is not None:
        now = datetime.now(contract.lease_expires_at.tzinfo)
        if contract.lease_expires_at <= now:
            return False
    owner = contract.owner
    if (
        owner.agent_id != agent_id
        or owner.session_id != chat_spec.session_id
        or owner.user_id != chat_spec.user_id
        or owner.channel != chat_spec.channel
        or owner.workspace_id is None
        or not _same_workspace(owner.workspace_id, workspace_dir)
    ):
        return False
    root_session_id = iso_string((active_run or {}).get("root_session_id"))
    if root_session_id is not None and root_session_id != chat_spec.session_id:
        return False
    if run_hint is not None and run_hint not in {
        contract.run_id,
        contract.attempt_id,
    }:
        return False
    task_contract = contract.task_contract
    if task_contract is not None and (
        task_contract.run_id not in {None, contract.run_id}
        or task_contract.attempt_id not in {None, contract.attempt_id}
    ):
        return False
    return True


async def resolve_canonical_chat_runtime_identity(
    *,
    chat_spec: ChatSpec,
    tracker_status: str,
    active_run: dict[str, Any] | None,
    run_hint: str | None,
    agent_id: str,
    root_workspace_dir: Path | str | None = None,
    state_workspace_dir: Path | str | None = None,
) -> CanonicalChatRuntimeIdentity | None:
    """Resolve active chat identity from RuntimeRunStore, failing closed.

    Candidate IDs may come from server-persisted chat metadata or an explicit
    suspicious hint.  The hint may be either a Run ID or an Attempt ID, but it
    is never returned until a live contract proves source, owner, workspace,
    lease, and task identity.  Tracker ``run_key`` is deliberately ignored.
    """

    if tracker_status != "running":
        return None
    active_status = str((active_run or {}).get("status") or "").strip().lower()
    if active_run is not None and active_status != "running":
        return None

    workspace_dir = resolve_effective_workspace_dir_from_meta(
        chat_spec.meta or {},
        root_workspace_dir=root_workspace_dir,
    )
    if workspace_dir is None:
        return None
    workspace_dir = Path(workspace_dir)

    pending_turn = pending_turn_dict(chat_spec)
    candidates: list[str] = []
    for value in (
        iso_string(run_hint),
        pending_turn_run_id(pending_turn),
        iso_string((chat_spec.meta or {}).get("last_turn_run_id")),
    ):
        if value is not None and value not in candidates:
            candidates.append(value)
    if not candidates:
        return None

    store = RuntimeRunStore(state_workspace_dir or workspace_dir)
    contracts: list[RuntimeRunContract] = []
    for candidate in candidates:
        try:
            contract = await store.load_contract(candidate)
        except (OSError, RuntimeRunStoreError):
            continue
        if contract is not None and all(
            existing.run_id != contract.run_id for existing in contracts
        ):
            contracts.append(contract)

    # An explicit hint can be the canonical Attempt ID rather than the store's
    # Run-keyed path.  Scan only after direct candidate lookup did not find it.
    normalized_hint = iso_string(run_hint)
    if normalized_hint is not None and not any(
        normalized_hint in {contract.run_id, contract.attempt_id}
        for contract in contracts
    ):
        try:
            listed = await asyncio.to_thread(store.list_contracts)
        except (OSError, RuntimeRunStoreError):
            listed = []
        contracts.extend(
            contract
            for contract in listed
            if normalized_hint in {contract.run_id, contract.attempt_id}
            and all(existing.run_id != contract.run_id for existing in contracts)
        )

    for contract in contracts:
        if not _chat_contract_matches_owner(
            contract,
            chat_spec=chat_spec,
            agent_id=agent_id,
            workspace_dir=workspace_dir,
            run_hint=normalized_hint,
            active_run=active_run,
        ):
            continue
        raw_generation = (active_run or {}).get("generation")
        generation = (
            raw_generation
            if isinstance(raw_generation, int)
            and not isinstance(raw_generation, bool)
            and raw_generation >= 0
            else None
        )
        task_revision = (
            contract.task_contract.revision
            if contract.task_contract is not None
            else None
        )
        return CanonicalChatRuntimeIdentity(
            run_id=contract.run_id,
            attempt_id=contract.attempt_id,
            control_revision=contract.revision,
            generation=generation,
            task_revision=task_revision,
            lease_owner=contract.lease_owner,
            workspace_dir=str(workspace_dir),
            source=contract.source,
            owner_session_id=str(contract.owner.session_id),
            owner_agent_id=contract.owner.agent_id,
        )
    return None


def runtime_status_from_preview(
    kind: str,
    status: str,
    event_type: str = "",
) -> str:
    """Map one preview row to runtime status without promoting item status.

    Tool, message and step rows also use ``completed``/``failed`` statuses.
    Only an explicit turn terminal (or the stop control) may settle the whole
    chat turn; item-level statuses remain progress while a pending turn exists.
    """

    if event_type == "turn.stalled" or (
        kind == "chat.turn.settled" and status == "stalled"
    ):
        return "stalled"
    if event_type == "turn.failed" or (
        kind == "chat.turn.settled" and status == "failed"
    ):
        return "failed"
    if (
        event_type == "turn.interrupted"
        or kind == "chat.turn.stop_requested"
        or (kind == "chat.turn.settled" and status == "interrupted")
    ):
        return "interrupted"
    if event_type == "turn.completed" or (
        kind == "chat.turn.settled" and status in {"finished", "completed"}
    ):
        return "completed"
    if status == "running":
        return "running"
    return "idle"


def _preview_event_run_id(event: Any) -> str | None:
    """Read the event's own run id without treating parent lineage as identity."""

    direct = iso_string(getattr(event, "run_id", None))
    if direct is not None:
        return direct
    payload = (
        event.payload
        if isinstance(getattr(event, "payload", None), dict)
        else {}
    )
    return iso_string(payload.get("run_id") or payload.get("turn_id"))


def _preview_event_parent_run_id(event: Any) -> str | None:
    """Read explicit parent lineage without confusing it with the child id."""

    payload = (
        event.payload
        if isinstance(getattr(event, "payload", None), dict)
        else {}
    )
    sub_run = payload.get("sub_run")
    nested_parent = (
        sub_run.get("parent_run_id") if isinstance(sub_run, dict) else None
    )
    return iso_string(payload.get("parent_run_id") or nested_parent)


def _preview_event_payload_value(event: Any, key: str) -> str | None:
    """Read one string identity value from a preview event payload."""

    payload = (
        event.payload
        if isinstance(getattr(event, "payload", None), dict)
        else {}
    )
    return iso_string(payload.get(key))


def _latest_scoped_preview_value(
    preview_events: list[Any],
    run_id: str | None,
    key: str,
) -> str | None:
    """Return the newest payload identity explicitly scoped to ``run_id``."""

    if not run_id:
        return None
    for event in reversed(preview_events):
        if _preview_event_run_id(event) != run_id:
            continue
        value = _preview_event_payload_value(event, key)
        if value is not None:
            return value
    return None


def _latest_status_event(
    preview_events: list[Any],
    run_id: str | None,
) -> Any | None:
    """Return the newest event allowed to determine ``run_id``'s status.

    Child events remain in the preview as useful stage information, but their
    status/outcome cannot settle the parent merely because they were appended
    later. Fully unscoped legacy previews retain their previous fallback.
    """

    if not preview_events:
        return None
    if not run_id:
        return preview_events[-1]
    for event in reversed(preview_events):
        if _preview_event_run_id(event) == run_id:
            return event
    if all(_preview_event_run_id(event) is None for event in preview_events):
        return preview_events[-1]
    return None


def build_chat_runtime_state(
    *,
    chat_spec: ChatSpec,
    tracker_status: str,
    active_run: dict[str, Any] | None,
    turn_event_preview: TurnEventPreview | None,
    pending_turn: dict[str, Any],
    canonical_identity: CanonicalChatRuntimeIdentity | None = None,
) -> ChatRuntimeState | None:
    """Merge legacy chat fragments into a single frontend recovery state."""

    preview_events = list(getattr(turn_event_preview, "events", []) or [])
    latest_event = preview_events[-1] if preview_events else None
    pending_text = pending_turn_text(pending_turn)
    pending_run = pending_turn_run_id(pending_turn)

    active_status = str((active_run or {}).get("status") or "")
    has_active_run = tracker_status == "running" or active_status == "running"
    has_recoverable_context = bool(
        has_active_run or pending_text or pending_run or preview_events,
    )
    if not has_recoverable_context:
        return None

    latest_event_run_id = (
        _preview_event_run_id(latest_event) if latest_event is not None else None
    )
    latest_parent_run_id = (
        _preview_event_parent_run_id(latest_event)
        if latest_event is not None
        else None
    )
    persisted_run_id = iso_string(
        (chat_spec.meta or {}).get("last_turn_run_id"),
    )
    run_id = (
        (canonical_identity.run_id if canonical_identity is not None else None)
        or pending_run
        or latest_parent_run_id
        or latest_event_run_id
        or persisted_run_id
    )
    pending_client_turn_id = iso_string(pending_turn.get("client_turn_id"))
    client_turn_id = (
        pending_client_turn_id
        if pending_client_turn_id and (not run_id or pending_run == run_id)
        else None
    ) or _latest_scoped_preview_value(
        preview_events,
        run_id,
        "client_turn_id",
    )
    if client_turn_id is None and persisted_run_id == run_id:
        client_turn_id = iso_string(
            (chat_spec.meta or {}).get("last_turn_client_id"),
        )
    attempt_id = (
        canonical_identity.attempt_id
        if canonical_identity is not None
        else _latest_scoped_preview_value(preview_events, run_id, "attempt_id")
    )
    status_event = _latest_status_event(preview_events, run_id)
    status_payload = (
        status_event.payload
        if status_event is not None and isinstance(status_event.payload, dict)
        else {}
    )
    status_event_run_id = (
        _preview_event_run_id(status_event) if status_event is not None else None
    )
    status_event_runtime_status = (
        runtime_status_from_preview(
            status_event.kind,
            status_event.status,
            status_event.type,
        )
        if status_event is not None
        else "idle"
    )
    status_event_can_settle_turn = status_event_runtime_status in {
        "completed",
        "failed",
        "interrupted",
        "stalled",
    }

    if has_active_run:
        runtime_status = "running"
    elif str(pending_turn.get("status") or "").lower() == "stalled":
        runtime_status = "stalled"
    elif status_event is not None:
        runtime_status = status_event_runtime_status
        if runtime_status == "running" and not (pending_text or pending_run):
            runtime_status = "idle"
        elif runtime_status == "idle" and (pending_text or pending_run):
            runtime_status = "running"
    elif pending_text or pending_run:
        runtime_status = "running"
    else:
        runtime_status = "idle"

    started_at = iso_string((active_run or {}).get("started_at"))
    if started_at is None:
        has_scoped_preview_event = any(
            _preview_event_run_id(event) is not None for event in preview_events
        )
        for event in reversed(preview_events):
            event_run_id = _preview_event_run_id(event)
            if event.kind == "chat.turn.started" and (
                not run_id
                or event_run_id == run_id
                or (event_run_id is None and not has_scoped_preview_event)
            ):
                started_at = event.ts
                break

    updated_at = latest_event.ts if latest_event is not None else None
    if updated_at is None:
        updated_at = iso_string(chat_spec.updated_at)

    workspace_dir = getattr(turn_event_preview, "workspace_dir", None)
    if workspace_dir is None:
        workspace_path = resolve_effective_workspace_dir_from_meta(
            chat_spec.meta or {},
        )
        workspace_dir = str(workspace_path) if workspace_path is not None else None

    chat_meta = chat_spec.meta or {}
    persisted_outcome_run_id = iso_string(
        chat_meta.get("last_turn_outcome_run_id")
        or chat_meta.get("last_turn_run_id"),
    )
    persisted_outcome = (
        chat_meta.get("last_turn_outcome_status")
        if persisted_outcome_run_id == run_id and not has_active_run
        else None
    )
    event_outcome = (
        status_payload.get("outcome_status")
        if status_event_can_settle_turn
        else None
    )
    if status_event_run_id is not None and status_event_run_id != run_id:
        event_outcome = None
    outcome_status = normalize_turn_outcome_status(
        event_outcome or persisted_outcome,
    )
    event_delivery = (
        status_payload.get("delivery_status")
        if status_event_can_settle_turn
        else None
    )
    if status_event_run_id is not None and status_event_run_id != run_id:
        event_delivery = None
    persisted_delivery = (
        chat_meta.get("last_turn_delivery_status")
        if persisted_outcome_run_id == run_id and not has_active_run
        else None
    )
    delivery_status = (
        "pending"
        if has_active_run
        else str(event_delivery or persisted_delivery or "pending")
    )

    summary = getattr(turn_event_preview, "summary_line", None)
    if status_event is not latest_event:
        summary = None
    if runtime_status == "running":
        summary = summary or "本轮运行中"
    elif runtime_status == "completed":
        summary = summary or "本轮已结束"
    elif runtime_status == "interrupted":
        summary = summary or "本轮已中断"
    elif runtime_status == "failed":
        summary = summary or "本轮运行失败"
    elif runtime_status == "stalled":
        summary = summary or "本轮可能卡住"

    return ChatRuntimeState(
        status=runtime_status,
        run_id=run_id,
        client_turn_id=client_turn_id,
        attempt_id=attempt_id,
        outcome_status=outcome_status,
        delivery_status=delivery_status,
        started_at=started_at,
        updated_at=updated_at,
        summary=summary,
        events_preview=preview_events,
        workspace_dir=workspace_dir,
        can_reconnect=has_active_run,
        can_stop=has_active_run,
    )
