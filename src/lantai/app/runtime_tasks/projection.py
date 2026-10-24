# -*- coding: utf-8 -*-
"""Authoritative RuntimeRun -> task-board projection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from ...runtime.runs import RuntimeRunContract
from ...runtime.store import RuntimeRunStore
from ..experience.events import (
    _runtime_summary_from_contract,
    _runtime_summary_with_effect_recovery,
)
from ..experience.schemas import RuntimeRunSummary
from .schemas import (
    RuntimeTaskBoardCounts,
    RuntimeTaskBoardItem,
    RuntimeTaskBoardResponse,
    RuntimeTaskLane,
    RuntimeTaskOutcome,
)


TaskBoardView = Literal["board", "history", "all"]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _lane_for(
    contract: RuntimeRunContract,
    summary: RuntimeRunSummary,
) -> RuntimeTaskLane:
    status = contract.status
    recovery = summary.recovery or {}
    if (
        status == "waiting_approval"
        or summary.pending_approval
        or bool(recovery.get("effect_requires_human"))
    ):
        return "needs_user"
    if status in {"queued", "claimed"}:
        return "pending"
    if status == "running":
        return "active"
    if status == "succeeded":
        if contract.delivery_status == "verified":
            return "done"
        return "needs_action"
    return "needs_action"


def _outcome_for(
    contract: RuntimeRunContract,
    lane: RuntimeTaskLane,
) -> RuntimeTaskOutcome:
    if lane == "pending":
        return "queued"
    if lane == "active":
        return "in_progress"
    if lane == "needs_user":
        return "needs_input"
    if lane == "done":
        return "delivered"
    if contract.status in {"failed", "stalled", "paused", "stop_requested"}:
        return "blocked"
    if contract.status == "cancelled" or contract.delivery_status in {
        "partial",
        "not_delivered",
    }:
        return "incomplete"
    return "unknown"


def _next_action(
    contract: RuntimeRunContract,
    summary: RuntimeRunSummary,
    lane: RuntimeTaskLane,
) -> str | None:
    if lane == "needs_user":
        if summary.ambiguous_effect:
            return "确认外部操作是否已经生效"
        return "处理审批或补充任务所需信息"
    if lane == "needs_action":
        if contract.status == "succeeded":
            return "检查交付结果；当前缺少已验证的交付证据"
        if contract.status == "paused":
            return "恢复运行或结束任务"
        if contract.status == "stop_requested":
            return "等待停止完成；若长时间未结束请接管"
        if contract.status == "cancelled":
            return "按需重试或归档"
        if contract.status == "failed":
            return "查看失败原因并重试"
        if contract.status == "stalled":
            return "从检查点继续或人工接管"
        return "打开详情检查运行状态"
    return None


def _is_history(contract: RuntimeRunContract, lane: RuntimeTaskLane) -> bool:
    del lane
    # Cancelled runs stay auditable but do not occupy the normal five-lane
    # board unless another recovery projection has promoted them.
    return contract.status == "cancelled"


def project_runtime_task(
    contract: RuntimeRunContract,
    summary: RuntimeRunSummary,
    *,
    chat_meta: dict[str, Any] | None = None,
) -> RuntimeTaskBoardItem:
    """Project one RuntimeRun without mutating its source contract."""

    meta = chat_meta or {}
    task_contract = contract.task_contract
    objective = task_contract.objective if task_contract is not None else ""
    title = str(meta.get("task_center_title") or "").strip()
    if not title:
        title = objective.splitlines()[0][:80].strip() if objective else ""
    if not title:
        title = summary.title or f"{contract.source} task"
    event_summary = summary.last_event_summary or summary.summary
    summary_text = event_summary if summary.event_count else objective
    lane = _lane_for(contract, summary)
    recovery = dict(summary.recovery or {})
    chat_id = str(meta.get("chat_id") or summary.chat_id or "").strip() or None
    updated_at = (
        contract.settled_at
        or contract.heartbeat_at
        or contract.started_at
        or contract.created_at
    )
    return RuntimeTaskBoardItem(
        id=contract.run_id,
        display_id=contract.run_id[:8],
        title=title,
        summary=summary_text or contract.status,
        source=contract.source,
        lane=lane,
        outcome_status=_outcome_for(contract, lane),
        authority_status=contract.status,
        transport_status=summary.transport_status,
        delivery_status=contract.delivery_status,
        is_history=_is_history(contract, lane),
        pending_approval=summary.pending_approval,
        ambiguous_effect=summary.ambiguous_effect,
        next_action=_next_action(contract, summary, lane),
        agent_id=contract.owner.agent_id,
        chat_id=chat_id,
        session_id=contract.owner.session_id,
        workspace_dir=summary.workspace_dir,
        attempt_id=contract.attempt_id,
        revision=contract.revision,
        created_at=_iso(contract.created_at),
        started_at=_iso(contract.started_at),
        updated_at=_iso(updated_at),
        ended_at=_iso(contract.settled_at),
        current_step=summary.current_step,
        checkpoint=summary.checkpoint,
        budget=dict(summary.budget or {}),
        usage=summary.usage.model_dump(mode="json"),
        recovery=recovery,
        autonomy_grant=(
            task_contract.autonomy_grant.model_dump(mode="json")
            if task_contract is not None
            and task_contract.autonomy_grant is not None
            else None
        ),
        progress_report=(
            contract.progress_report.model_dump(mode="json")
            if contract.progress_report is not None
            else None
        ),
        completion_evidence=(
            contract.completion_evidence.model_dump(mode="json")
            if contract.completion_evidence is not None
            else None
        ),
        can_start=contract.status == "queued",
        can_interrupt=contract.status in {
            "claimed",
            "running",
            "waiting_approval",
            "paused",
            "stop_requested",
        },
        can_retry=summary.can_retry,
        can_resume=bool(recovery.get("can_resume")) or contract.status in {
            "paused",
            "stalled",
        },
        can_open_chat=chat_id is not None,
        event_count=summary.event_count,
        last_event_summary=summary.last_event_summary,
        debug_refs=dict(summary.debug_refs or {}),
    )


def _contract_rows_by_id(
    workspace_dirs: Iterable[Path | str],
) -> dict[str, tuple[RuntimeRunContract, str]]:
    contracts: dict[str, tuple[RuntimeRunContract, str]] = {}
    for raw_dir in workspace_dirs:
        workspace_dir = Path(raw_dir).expanduser()
        for contract in RuntimeRunStore(workspace_dir).list_contracts():
            existing = contracts.get(contract.run_id)
            if existing is None or contract.revision >= existing[0].revision:
                contracts[contract.run_id] = (contract, str(workspace_dir))
    return contracts


def build_runtime_task_board(
    workspace_dirs: list[Path | str],
    *,
    chat_meta_by_run_id: dict[str, dict[str, Any]] | None = None,
    view: TaskBoardView = "board",
    source: str | None = None,
    lane: RuntimeTaskLane | None = None,
    query: str | None = None,
    limit: int = 200,
) -> RuntimeTaskBoardResponse:
    """Build the task board from compact authoritative RuntimeRun contracts.

    Event ledgers can grow to hundreds of megabytes.  Replaying them belongs
    to an explicitly opened run detail, never to this frequently-polled list
    surface.  The contract already contains the status, checkpoint, budget,
    delivery evidence, and recovery reason required by the board.
    """

    contract_rows = _contract_rows_by_id(workspace_dirs)
    items: list[RuntimeTaskBoardItem] = []
    for run_id, (contract, workspace_dir) in contract_rows.items():
        if source and contract.source != source:
            continue
        summary = _runtime_summary_from_contract(contract, workspace_dir)
        summary = _runtime_summary_with_effect_recovery(
            summary,
            contract,
            workspace_dir,
        )
        item = project_runtime_task(
            contract,
            summary,
            chat_meta=(chat_meta_by_run_id or {}).get(run_id),
        )
        if lane and item.lane != lane:
            continue
        normalized_query = str(query or "").strip().casefold()
        if normalized_query and normalized_query not in (
            f"{item.title}\n{item.summary}\n{item.id}".casefold()
        ):
            continue
        items.append(item)

    items.sort(key=lambda item: item.updated_at or item.created_at or "", reverse=True)
    all_items = list(items)
    terminal_statuses = {"succeeded", "failed", "cancelled", "stalled"}
    history_count = sum(
        1 for item in all_items if item.authority_status in terminal_statuses
    )
    if view == "board":
        items = [item for item in items if not item.is_history]
    elif view == "history":
        items = [
            item for item in items if item.authority_status in terminal_statuses
        ]
    items = items[: max(1, limit)]

    counts_payload = {key: 0 for key in RuntimeTaskLane.__args__}
    for item in all_items:
        if not item.is_history:
            counts_payload[item.lane] += 1
    counts = RuntimeTaskBoardCounts(
        **counts_payload,
        history=history_count,
    )
    return RuntimeTaskBoardResponse(
        total=len(all_items),
        visible_total=len(items),
        generated_at=datetime.now(timezone.utc).isoformat(),
        counts=counts,
        items=items,
    )
