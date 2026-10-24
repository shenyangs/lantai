# -*- coding: utf-8 -*-
"""Approval service for sensitive tool execution.

The ``ApprovalService`` is the single central store for pending /
completed approval records.  Approval is granted exclusively via
the ``/daemon approve`` command in the chat interface.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ...security.tool_guard.approval import ApprovalDecision

if TYPE_CHECKING:
    from ...security.tool_guard.models import ToolGuardResult

logger = logging.getLogger(__name__)

_GC_MAX_AGE_SECONDS = 3600.0
_GC_MAX_COMPLETED = 500
_GC_MAX_PENDING = 200


def _policy_decision_from_extra_or_result(
    extra: dict[str, Any],
    result: "ToolGuardResult",
) -> dict[str, Any] | None:
    raw = extra.get("policy_decision")
    if isinstance(raw, dict):
        return raw
    for finding in getattr(result, "findings", []) or []:
        metadata = getattr(finding, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        nested = metadata.get("policy_decision")
        if isinstance(nested, dict):
            return nested
    return None


def _approval_group_key(
    *,
    root_session_id: str,
    tool_name: str,
    severity: str,
    policy_decision: dict[str, Any] | None,
) -> str:
    policy = policy_decision or {}
    raw_rules = policy.get("matched_rules") or policy.get("matched_rule_ids")
    if isinstance(raw_rules, list):
        rule_part = ",".join(sorted(str(item) for item in raw_rules if item))
    else:
        rule_part = str(policy.get("matched_rule_id") or "")
    category = str(policy.get("command_category") or "")
    command_head = str(policy.get("command_head") or "")
    mode = str(policy.get("permission_mode") or "")
    scope = str(policy.get("permission_scope_suggestion") or "")
    return "|".join(
        [
            root_session_id,
            tool_name,
            severity,
            rule_part,
            category,
            command_head,
            mode,
            scope,
        ],
    )


# ------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------


@dataclass
class PendingApproval:
    """In-memory record for one pending approval."""

    request_id: str
    session_id: str
    root_session_id: str  # Root session for cross-session approval routing
    user_id: str
    channel: str
    agent_id: str  # Which agent is requesting approval
    tool_name: str
    created_at: float
    future: asyncio.Future[ApprovalDecision]
    timeout_seconds: float | None = None
    status: str = "pending"
    resolved_at: float | None = None
    result_summary: str = ""
    findings_count: int = 0
    severity: str = "medium"  # For frontend display
    policy_decision: dict[str, Any] | None = None
    approval_options: list[str] = field(default_factory=list)
    session_rememberable: bool = False
    rememberable: bool = False
    permission_scope: str | None = None
    permission_group_key: str | None = None
    source_run_id: str | None = None
    source_agent_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ApprovalQueueChangedError(ValueError):
    """Raised when a reviewed approval queue changed before applying access."""


# ------------------------------------------------------------------
# Service
# ------------------------------------------------------------------


class ApprovalService:
    """Global singleton approval service.

    Manages all tool approval requests across sessions and agents.
    Approval is resolved via ``/approval`` control command.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pending: dict[str, PendingApproval] = {}
        self._session_approval_memory: dict[str, list[dict[str, Any]]] = {}
        self._session_permission_overrides: dict[
            tuple[str, str],
            dict[str, Any],
        ] = {}
        self._channel_manager: Any | None = None

    def set_channel_manager(self, channel_manager: Any) -> None:
        """Store a reference to the channel manager for push notifications."""
        self._channel_manager = channel_manager

    # ------------------------------------------------------------------
    # Core approval lifecycle
    # ------------------------------------------------------------------

    async def create_pending(
        self,
        *,
        session_id: str,
        root_session_id: str,
        user_id: str,
        channel: str,
        agent_id: str,
        tool_name: str,
        result: "ToolGuardResult",
        timeout_seconds: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> PendingApproval:
        """Create a pending approval record and return it."""
        from ...security.tool_guard.approval import format_findings_summary

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        extra_data = dict(extra or {})
        policy_decision = _policy_decision_from_extra_or_result(
            extra_data,
            result,
        )
        permission_scope = (
            str(policy_decision.get("permission_scope_suggestion") or "")
            if isinstance(policy_decision, dict)
            else ""
        )
        approval_options = extra_data.get("approval_options")
        if not isinstance(approval_options, list) or not approval_options:
            approval_options = [
                "allow_once",
                "allow_session",
                "allow_workspace",
                "deny",
                "view_rules",
            ]
        approval_options = [str(item) for item in approval_options]
        rememberable = bool(
            extra_data.get("rememberable")
            or (
                isinstance(policy_decision, dict)
                and policy_decision.get("rememberable")
            )
        )
        raw_session_rememberable = extra_data.get("session_rememberable")
        if raw_session_rememberable is None:
            session_rememberable = not bool(
                isinstance(policy_decision, dict)
                and (
                    policy_decision.get("workspace_escape")
                    or policy_decision.get("hard_denied")
                )
            )
        else:
            session_rememberable = bool(raw_session_rememberable)
        if not session_rememberable:
            approval_options = [
                option
                for option in approval_options
                if option != "allow_session"
            ]
        if not rememberable:
            approval_options = [
                option
                for option in approval_options
                if option != "allow_workspace"
            ]

        pending = PendingApproval(
            request_id=request_id,
            session_id=session_id,
            root_session_id=root_session_id,
            user_id=user_id,
            channel=channel,
            agent_id=agent_id,
            tool_name=tool_name,
            created_at=time.time(),
            future=loop.create_future(),
            timeout_seconds=timeout_seconds,
            result_summary=format_findings_summary(result),
            findings_count=result.findings_count,
            severity=result.max_severity.value,
            policy_decision=policy_decision,
            approval_options=approval_options,
            session_rememberable=session_rememberable,
            rememberable=rememberable,
            permission_scope=permission_scope or None,
            permission_group_key=str(
                extra_data.get("permission_group_key")
                or _approval_group_key(
                    root_session_id=root_session_id,
                    tool_name=tool_name,
                    severity=result.max_severity.value,
                    policy_decision=policy_decision,
                ),
            ),
            source_run_id=(
                str(extra_data.get("source_run_id"))
                if extra_data.get("source_run_id")
                else None
            ),
            source_agent_name=(
                str(extra_data.get("source_agent_name"))
                if extra_data.get("source_agent_name")
                else None
            ),
            extra=extra_data,
        )

        auto_approved_by = ""
        try:
            permission_snapshot_at = float(
                extra_data.get("permission_snapshot_at") or 0.0,
            )
        except (TypeError, ValueError):
            permission_snapshot_at = 0.0
        tool_call = extra_data.get("tool_call")
        tool_input = (
            tool_call.get("input", {})
            if isinstance(tool_call, dict)
            and isinstance(tool_call.get("input"), dict)
            else {}
        )
        workspace_dir = str(extra_data.get("workspace_dir") or "").strip()

        # This check and insertion share the same lock as live permission
        # updates and session-memory writes. That closes the race where a
        # policy evaluation requested approval just before the user granted
        # the session, but the pending Future was inserted just after the
        # grant's queue drain.
        async with self._lock:
            override = self._session_permission_overrides.get(
                (root_session_id, user_id),
            )
            if (
                override is not None
                and float(override.get("_updated_at") or 0.0)
                >= permission_snapshot_at
                and override.get("permission_mode") == "full_auto"
                and override.get("permission_profile") == "full"
                and not bool((policy_decision or {}).get("hard_denied"))
            ):
                auto_approved_by = "full_access"

            if not auto_approved_by and pending.session_rememberable:
                from ...security.tool_permission_memory import (
                    session_approval_rule_matches,
                )

                for rule in self._session_approval_memory.get(
                    root_session_id,
                    [],
                ):
                    rule_user_id = str(rule.get("user_id") or "").strip()
                    if rule_user_id != user_id:
                        continue
                    if session_approval_rule_matches(
                        rule,
                        workspace_dir=workspace_dir,
                        tool_name=tool_name,
                        policy_decision=policy_decision,
                        source_agent_id=agent_id,
                    ):
                        auto_approved_by = "session_approval"
                        break

            if auto_approved_by:
                pending.status = ApprovalDecision.APPROVED.value
                pending.resolved_at = time.time()
                pending.extra["decision_scope"] = "session"
                pending.extra["auto_approved_by"] = auto_approved_by
            else:
                self._pending[request_id] = pending
                self._gc_pending_locked()

        if auto_approved_by:
            pending.future.set_result(ApprovalDecision.APPROVED)
            logger.info(
                "Approval auto-resolved after live session grant: "
                "request_id=%s root=%s source=%s",
                request_id[:8],
                root_session_id[:8],
                auto_approved_by,
            )
        else:
            logger.info(
                "Approval pending created: request_id=%s agent_id=%s tool=%s "
                "severity=%s session=%s root=%s",
                request_id[:8],
                agent_id,
                tool_name,
                pending.severity,
                session_id[:8],
                root_session_id[:8],
            )

        return pending

    async def resolve_request(
        self,
        request_id: str,
        decision: ApprovalDecision,
        *,
        decision_scope: str | None = None,
    ) -> PendingApproval | None:
        """Resolve pending approval by setting Future result."""
        async with self._lock:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                logger.warning(
                    "Approval request %s not found (already resolved?)",
                    request_id[:8],
                )
                return None

            pending.status = decision.value
            pending.resolved_at = time.time()
            if decision_scope:
                pending.extra["decision_scope"] = decision_scope

        # Set Future result outside lock
        if not pending.future.done():
            pending.future.set_result(decision)

        logger.info(
            "Approval request %s resolved: decision=%s tool=%s",
            request_id[:8],
            decision.value,
            pending.tool_name,
        )

        return pending

    async def approve_pending_for_root_session(
        self,
        *,
        root_session_id: str,
        user_id: str,
        session_rememberable_only: bool,
    ) -> list[str]:
        """Approve an eligible pending queue after a session-wide grant."""

        root_id = str(root_session_id or "").strip()
        owner_id = str(user_id or "").strip()
        if not root_id or not owner_id:
            return []

        approved: list[PendingApproval]
        async with self._lock:
            eligible = self._eligible_pending_for_root_session_locked(
                root_session_id=root_id,
                user_id=owner_id,
                session_rememberable_only=session_rememberable_only,
            )
            approved = self._approve_pending_locked(eligible)

        for pending in approved:
            if not pending.future.done():
                pending.future.set_result(ApprovalDecision.APPROVED)

        if approved:
            logger.info(
                "Approved %d queued request(s) for root session %s",
                len(approved),
                root_id[:12],
            )
        return [pending.request_id for pending in approved]

    def _eligible_pending_for_root_session_locked(
        self,
        *,
        root_session_id: str,
        user_id: str,
        session_rememberable_only: bool,
    ) -> list[PendingApproval]:
        eligible: list[PendingApproval] = []
        for pending in self._pending.values():
            if (
                pending.root_session_id != root_session_id
                or pending.user_id != user_id
                or pending.status != "pending"
            ):
                continue
            policy = pending.policy_decision or {}
            if bool(policy.get("hard_denied")):
                continue
            if session_rememberable_only and not pending.session_rememberable:
                continue
            eligible.append(pending)
        return sorted(
            eligible,
            key=lambda pending: (pending.created_at, pending.request_id),
        )

    @staticmethod
    def _queue_revision(pending: list[PendingApproval]) -> str:
        payload = "\n".join(item.request_id for item in pending)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _approve_pending_locked(
        self,
        pending_items: list[PendingApproval],
    ) -> list[PendingApproval]:
        now = time.time()
        approved: list[PendingApproval] = []
        for pending in pending_items:
            current = self._pending.get(pending.request_id)
            if current is not pending or pending.status != "pending":
                continue
            self._pending.pop(pending.request_id, None)
            pending.status = ApprovalDecision.APPROVED.value
            pending.resolved_at = now
            pending.extra["decision_scope"] = "session"
            approved.append(pending)
        return approved

    async def preview_pending_for_root_session(
        self,
        *,
        root_session_id: str,
        user_id: str,
        session_rememberable_only: bool,
    ) -> dict[str, Any]:
        """Return the exact safe summary used by a later guarded drain."""

        root_id = str(root_session_id or "").strip()
        owner_id = str(user_id or "").strip()
        if not root_id or not owner_id:
            raise ValueError("root_session_id and user_id are required")
        async with self._lock:
            eligible = self._eligible_pending_for_root_session_locked(
                root_session_id=root_id,
                user_id=owner_id,
                session_rememberable_only=session_rememberable_only,
            )
            return {
                "queue_revision": self._queue_revision(eligible),
                "requests": [
                    {
                        "request_id": pending.request_id,
                        "tool_name": pending.tool_name,
                        "severity": str(pending.severity).lower(),
                    }
                    for pending in eligible
                ],
            }

    async def set_session_permission_override(
        self,
        *,
        root_session_id: str,
        user_id: str,
        permission_mode: str,
        permission_profile: str,
        expected_queue_revision: str | None = None,
    ) -> dict[str, Any]:
        """Update the permission policy read by an already-running turn."""

        root_id = str(root_session_id or "").strip()
        owner_id = str(user_id or "").strip()
        if not root_id or not owner_id:
            raise ValueError("root_session_id and user_id are required")
        override = {
            "permission_mode": str(permission_mode or "").strip(),
            "permission_profile": str(permission_profile or "").strip(),
            "_updated_at": time.time(),
        }
        grants_full_access = (
            override["permission_mode"] == "full_auto"
            and override["permission_profile"] == "full"
        )
        approved: list[PendingApproval] = []
        async with self._lock:
            eligible = (
                self._eligible_pending_for_root_session_locked(
                    root_session_id=root_id,
                    user_id=owner_id,
                    session_rememberable_only=False,
                )
                if grants_full_access
                else []
            )
            if (
                grants_full_access
                and expected_queue_revision is not None
                and self._queue_revision(eligible) != expected_queue_revision
            ):
                raise ApprovalQueueChangedError(
                    "pending approval queue changed; review the current queue",
                )
            self._session_permission_overrides[(root_id, owner_id)] = override
            if grants_full_access:
                approved = self._approve_pending_locked(eligible)

        for pending in approved:
            if not pending.future.done():
                pending.future.set_result(ApprovalDecision.APPROVED)

        return {
            "permission_mode": override["permission_mode"],
            "permission_profile": override["permission_profile"],
            "resolved_request_ids": [
                pending.request_id for pending in approved
            ],
        }

    async def get_session_permission_override(
        self,
        *,
        root_session_id: str,
        user_id: str,
        updated_after: float | None = None,
    ) -> dict[str, str] | None:
        """Return the latest live permission choice for this session owner."""

        key = (
            str(root_session_id or "").strip(),
            str(user_id or "").strip(),
        )
        if not all(key):
            return None
        async with self._lock:
            override = self._session_permission_overrides.get(key)
            if override is None:
                return None
            if (
                updated_after is not None
                and float(override.get("_updated_at") or 0.0) < updated_after
            ):
                return None
            return {
                "permission_mode": str(override["permission_mode"]),
                "permission_profile": str(override["permission_profile"]),
            }

    async def remember_approval_scope(
        self,
        pending: PendingApproval,
        *,
        decision_scope: str | None,
    ) -> dict[str, Any] | None:
        """Remember an approved prompt for the selected session/workspace."""

        from ...security.tool_permission_memory import (
            build_session_approval_rule,
            normalise_decision_scope,
            remember_workspace_approval,
        )

        scope = normalise_decision_scope(decision_scope)
        pending.extra["decision_scope"] = scope
        if scope == "once":
            return None

        tool_call = pending.extra.get("tool_call")
        tool_input = (
            tool_call.get("input", {})
            if isinstance(tool_call, dict)
            and isinstance(tool_call.get("input"), dict)
            else {}
        )
        workspace_dir = str(pending.extra.get("workspace_dir") or "").strip()
        if scope == "session":
            if not pending.session_rememberable:
                return None
            memory_payload = build_session_approval_rule(
                workspace_dir=workspace_dir,
                tool_name=pending.tool_name,
                policy_decision=pending.policy_decision,
                source_agent_id=pending.agent_id,
            )
            if memory_payload is None:
                return None
            memory_payload["user_id"] = pending.user_id
            async with self._lock:
                rules = self._session_approval_memory.setdefault(
                    pending.root_session_id,
                    [],
                )
                rules[:] = [
                    item
                    for item in rules
                    if not (
                        item.get("rule_kind") == "session_access"
                        and str(item.get("user_id") or "").strip()
                        == pending.user_id
                        and str(item.get("source_agent_id") or "").strip()
                        == pending.agent_id
                    )
                ]
                rules.append(memory_payload)
            pending.extra["remembered_approval"] = memory_payload
            return memory_payload

        if not pending.rememberable:
            return None
        if not workspace_dir or not pending.policy_decision:
            return None
        remembered = remember_workspace_approval(
            workspace_dir=workspace_dir,
            tool_name=pending.tool_name,
            tool_input=tool_input,
            policy_decision=pending.policy_decision,
            source_agent_id=pending.agent_id,
            workspace_grant_id=str(
                pending.extra.get("workspace_grant_id") or "",
            ),
        )
        if remembered is None:
            return None
        memory_payload = remembered.model_dump(mode="json")
        pending.extra["remembered_approval"] = memory_payload
        return memory_payload

    async def session_approval_matches(
        self,
        *,
        root_session_id: str,
        user_id: str = "",
        workspace_dir: str,
        tool_name: str,
        tool_input: dict[str, Any],
        policy_decision: Any,
        source_agent_id: str = "",
    ) -> dict[str, Any] | None:
        """Return a session-scoped remembered approval that covers a prompt."""

        from ...security.tool_permission_memory import (
            approval_rule_matches,
            session_approval_rule_matches,
        )

        async with self._lock:
            rules = list(self._session_approval_memory.get(root_session_id) or [])
        for rule in rules:
            rule_user_id = str(rule.get("user_id") or "").strip()
            if user_id and rule_user_id != user_id:
                continue
            if session_approval_rule_matches(
                rule,
                workspace_dir=workspace_dir,
                tool_name=tool_name,
                policy_decision=policy_decision,
                source_agent_id=source_agent_id,
            ):
                return dict(rule)
            if approval_rule_matches(
                rule,
                workspace_dir=workspace_dir,
                tool_name=tool_name,
                tool_input=tool_input,
                policy_decision=policy_decision,
                source_agent_id=source_agent_id,
            ):
                return dict(rule)
        return None

    async def approval_memory_lifecycle_report(
        self,
        *,
        workspace_dir: str,
        source_agent_id: str = "",
        operation: str = "archive",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Archive and optionally remove remembered approvals for a workspace."""

        from ...security.tool_permission_memory import (
            _normalise_workspace_dir,
            approval_memory_lifecycle_cascade,
        )

        normalised_workspace = _normalise_workspace_dir(workspace_dir)
        source_agent = str(source_agent_id or "").strip()
        persistent = approval_memory_lifecycle_cascade(
            workspace_dir=normalised_workspace,
            source_agent_id=source_agent,
            operation=operation,  # type: ignore[arg-type]
            dry_run=dry_run,
        )
        session_matches: list[dict[str, Any]] = []
        session_removed = 0
        async with self._lock:
            for root_session_id, rules in list(self._session_approval_memory.items()):
                matching = [
                    rule
                    for rule in rules
                    if str(rule.get("workspace_dir") or "") == normalised_workspace
                    and (
                        not source_agent
                        or str(rule.get("source_agent_id") or "") == source_agent
                    )
                ]
                if not matching:
                    continue
                session_matches.append(
                    {
                        "root_session_id": root_session_id,
                        "matched_count": len(matching),
                        "matched_rule_ids": [
                            str(rule.get("id") or "")
                            for rule in matching
                            if rule.get("id")
                        ],
                    }
                )
                if operation == "delete" and not dry_run:
                    kept = [rule for rule in rules if rule not in matching]
                    session_removed += len(rules) - len(kept)
                    if kept:
                        self._session_approval_memory[root_session_id] = kept
                    else:
                        self._session_approval_memory.pop(root_session_id, None)

        return {
            "kind": "approval_memory_lifecycle",
            "operation": operation,
            "dry_run": bool(dry_run),
            "workspace_dir": normalised_workspace,
            "source_agent_id": source_agent,
            "persistent": persistent,
            "session": {
                "matched_count": sum(
                    item["matched_count"]
                    for item in session_matches
                ),
                "removed_count": session_removed,
                "matches": session_matches,
            },
        }

    async def get_request(self, request_id: str) -> PendingApproval | None:
        """Get a pending request by id."""
        async with self._lock:
            return self._pending.get(request_id)

    async def get_pending_by_session(
        self,
        session_id: str,
    ) -> PendingApproval | None:
        """Return the next pending approval for *session_id* (FIFO).

        Pending approvals are consumed in creation order, so repeated
        ``/approve`` inputs walk the queue from oldest to newest.
        """
        async with self._lock:
            for pending in self._pending.values():
                if (
                    pending.session_id == session_id
                    and pending.status == "pending"
                ):
                    return pending
        return None

    async def get_all_pending_by_session(
        self,
        session_id: str,
    ) -> list[PendingApproval]:
        """Return all pending approvals for *session_id* (FIFO order)."""
        async with self._lock:
            return [
                p
                for p in self._pending.values()
                if p.session_id == session_id and p.status == "pending"
            ]

    async def list_pending_by_session(
        self,
        session_id: str,
        include_subagents: bool = True,  # pylint: disable=unused-argument
    ) -> list[PendingApproval]:
        """List all pending approvals for a session (FIFO order).

        Args:
            session_id: Session ID to filter
            include_subagents: If False, exclude sub-agent approvals (future)

        Returns:
            List of pending approvals sorted by creation time
        """
        async with self._lock:
            result = [
                p
                for p in self._pending.values()
                if p.session_id == session_id and p.status == "pending"
            ]
            return sorted(result, key=lambda p: p.created_at)

    async def get_pending_by_root_session(
        self,
        root_session_id: str,
    ) -> list[PendingApproval]:
        """Get all pending approvals for root session and its children.

        Args:
            root_session_id: Root session ID

        Returns:
            List of pending approvals sorted by creation time (FIFO)
        """
        async with self._lock:
            result = [
                p
                for p in self._pending.values()
                if p.root_session_id == root_session_id
                and p.status == "pending"
            ]
            return sorted(result, key=lambda p: p.created_at)

    async def get_all_pending_by_agent(
        self,
        agent_id: str,
    ) -> list[PendingApproval]:
        """Get all pending approvals for an agent (across all sessions).

        Used by /approval list --all command.

        Args:
            agent_id: Agent ID

        Returns:
            List of pending approvals sorted by creation time (FIFO)
        """
        async with self._lock:
            result = [
                p
                for p in self._pending.values()
                if p.agent_id == agent_id and p.status == "pending"
            ]
            return sorted(result, key=lambda p: p.created_at)

    async def wait_for_approval(
        self,
        request_id: str,
        timeout_seconds: float,
    ) -> ApprovalDecision:
        """Block and wait for approval decision with timeout.

        Args:
            request_id: Approval request ID
            timeout_seconds: Maximum wait time in seconds

        Returns:
            ApprovalDecision (APPROVED/DENIED/TIMEOUT)

        Raises:
            ValueError: If request_id not found
        """
        async with self._lock:
            pending = self._pending.get(request_id)

        if pending is None:
            raise ValueError(f"Approval request {request_id} not found")

        try:
            decision = await asyncio.wait_for(
                pending.future,
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            decision = ApprovalDecision.TIMEOUT
            await self.resolve_request(request_id, decision)

        return decision

    async def cancel_stale_pending_for_tool_call(
        self,
        session_id: str,
        tool_call_id: str,
    ) -> int:
        """Cancel pending approvals whose stored tool_call id matches.

        When a tool call is replayed (e.g. after approval triggers
        sibling replay), the guard may create a *new* pending for the
        same logical tool call.  This method cancels the old pending
        first so orphaned records don't accumulate.

        Returns the number of records cancelled.
        """
        now = time.time()
        cancelled = 0
        async with self._lock:
            to_cancel = [
                k
                for k, p in self._pending.items()
                if p.session_id == session_id
                and p.status == "pending"
                and isinstance(p.extra.get("tool_call"), dict)
                and p.extra["tool_call"].get("id") == tool_call_id
            ]
            for k in to_cancel:
                pending = self._pending.pop(k)
                if not pending.future.done():
                    pending.future.set_result(ApprovalDecision.TIMEOUT)
                pending.status = "superseded"
                pending.resolved_at = now
                cancelled += 1
        if cancelled:
            logger.info(
                "Tool guard: cancelled %d stale pending approval(s) "
                "for tool_call %s (session %s)",
                cancelled,
                tool_call_id,
                session_id[:8],
            )
        return cancelled

    async def cancel_all_pending_by_root_session(
        self,
        root_session_id: str,
    ) -> int:
        """Cancel all pending approvals for root session and its children.

        Called when user stops/cancels a task (e.g., /stop command or
        SSE disconnect). Auto-denies all pending approvals to unblock
        waiting tasks.

        Args:
            root_session_id: Root session ID

        Returns:
            Number of approvals cancelled
        """
        now = time.time()
        cancelled = 0
        async with self._lock:
            to_cancel = [
                k
                for k, p in self._pending.items()
                if p.root_session_id == root_session_id
                and p.status == "pending"
            ]
            for k in to_cancel:
                pending = self._pending.pop(k)
                if not pending.future.done():
                    pending.future.set_result(ApprovalDecision.DENIED)
                pending.status = "cancelled"
                pending.resolved_at = now
                cancelled += 1
        if cancelled:
            logger.info(
                "Cancelled %d pending approval(s) for root session %s",
                cancelled,
                root_session_id[:8]
                if len(root_session_id) >= 8
                else root_session_id,
            )
        return cancelled

    # ------------------------------------------------------------------
    # Garbage collection
    # ------------------------------------------------------------------

    def _gc_pending_locked(self) -> None:
        """Evict only approvals that have an explicit expired deadline.

        Caller must hold ``_lock``.
        """
        now = time.time()
        expired = [
            k
            for k, v in self._pending.items()
            if v.timeout_seconds is not None
            and now - v.created_at > max(0.0, v.timeout_seconds)
        ]
        for k in expired:
            pending = self._pending.pop(k)
            if not pending.future.done():
                pending.future.set_result(ApprovalDecision.TIMEOUT)
            pending.status = "timeout"
            pending.resolved_at = now

        overflow = len(self._pending) - _GC_MAX_PENDING
        if overflow <= 0:
            return
        ordered = sorted(
            (
                item
                for item in self._pending.items()
                if item[1].timeout_seconds is not None
            ),
            key=lambda item: item[1].created_at,
        )
        for key, pending in ordered[:overflow]:
            del self._pending[key]
            if not pending.future.done():
                pending.future.set_result(ApprovalDecision.TIMEOUT)
            pending.status = "timeout"
            pending.resolved_at = now


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_approval_service: ApprovalService | None = None


def get_approval_service() -> ApprovalService:
    """Return the process-wide approval service singleton."""
    global _approval_service
    if _approval_service is None:
        _approval_service = ApprovalService()
    return _approval_service
