# -*- coding: utf-8 -*-
"""Admit one persistent Run Inbox item at a Native model boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import time
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from .coherence.contracts import ExecutionFence
from .continuation import continuation_source_messages
from .model_messages import model_message_from_runtime_message
from .models import ModelMessage
from .runs import RuntimeContinuationInputRef
from .turn_control import (
    RunInboxItemV2,
    RuntimeInputConflictError,
    RuntimeTurnControlStore,
    control_input_grant_ref,
)


@dataclass(frozen=True, slots=True)
class RuntimeInboxClaim:
    """One durable Inbox claim awaiting model-surface settlement."""

    item_seq: int
    client_turn_id: str
    actor_id: str
    fence: ExecutionFence
    message: ModelMessage


@dataclass(frozen=True, slots=True)
class RuntimeInboxConflict:
    """A rejected Inbox item whose cursor has still advanced."""

    item_seq: int
    client_turn_id: str
    reason: str
    kind: str = "run_inbox_fence_rejected"


@dataclass(frozen=True, slots=True)
class RuntimeInboxWakeup:
    """One compatible input asking the active model request to yield."""

    item_seq: int
    client_turn_id: str
    command_id: str
    intent: str


@dataclass(frozen=True, slots=True)
class RuntimeInboxAdmissionOutcome:
    """Result of examining at most one FIFO Inbox item."""

    next_after_seq: int
    claim: RuntimeInboxClaim | None = None
    conflict: RuntimeInboxConflict | None = None


@dataclass(frozen=True, slots=True)
class _AttachmentIntegrityFailure:
    status: Literal["rejected", "expired"]
    reason: str
    kind: str = "run_inbox_attachment_conflict"


def _attachment_integrity_error(
    item: RunInboxItemV2,
    *,
    store: RuntimeTurnControlStore,
) -> _AttachmentIntegrityFailure | None:
    """Return a stable rejection reason when a local snapshot changed.

    Remote provider URLs are outside this local snapshot boundary. Console
    control inputs use a local content-addressed workspace path, a full SHA-256
    digest, and an immutable version. Rechecking here closes the gap between
    durable admission and the actual model-request binding.
    """

    for block in item.message.content:
        if block.type not in {"image", "file"}:
            continue
        source = block.data.get("source")
        if not isinstance(source, dict) or source.get("type") != "url":
            continue
        source_url = str(source.get("url") or "").strip()
        if not source_url:
            continue
        parsed = urlsplit(source_url)
        if parsed.scheme not in {"", "file"}:
            continue
        version = block.data.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            return _AttachmentIntegrityFailure(
                status="rejected",
                reason="attachment immutable version is missing or invalid",
            )
        expected = str(block.data.get("digest") or "").strip().lower()
        expected_hex = expected.removeprefix("sha256:")
        if (
            not expected.startswith("sha256:")
            or len(expected_hex) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_hex
            )
        ):
            return _AttachmentIntegrityFailure(
                status="rejected",
                reason="attachment digest is missing or invalid",
            )
        local_value = (
            unquote(parsed.path or parsed.netloc)
            if parsed.scheme == "file"
            else source_url
        )
        path = Path(local_value).expanduser()
        try:
            if path.is_symlink():
                return _AttachmentIntegrityFailure(
                    status="rejected",
                    reason="attachment snapshot became a symlink",
                )
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return _AttachmentIntegrityFailure(
                status="rejected",
                reason="attachment snapshot is no longer available",
            )
        if not resolved.is_file():
            return _AttachmentIntegrityFailure(
                status="rejected",
                reason="attachment snapshot is no longer a regular file",
            )
        expected_grant_ref = control_input_grant_ref(
            chat_id=item.chat_id,
            run_id=item.run_id,
            canonical_path=str(resolved),
            digest=expected,
        )
        actual_grant_ref = str(block.data.get("grant_ref") or "").strip()
        if not hmac.compare_digest(actual_grant_ref, expected_grant_ref):
            return _AttachmentIntegrityFailure(
                status="rejected",
                reason=(
                    "attachment grant does not match the owning chat and run"
                ),
            )
        issued_at = block.data.get("grant_issued_at")
        expires_at = block.data.get("grant_expires_at")
        has_grant_timing = issued_at is not None or expires_at is not None
        if has_grant_timing and (
            not isinstance(issued_at, (int, float))
            or isinstance(issued_at, bool)
            or not isinstance(expires_at, (int, float))
            or isinstance(expires_at, bool)
            or issued_at <= 0
            or expires_at <= issued_at
        ):
            return _AttachmentIntegrityFailure(
                status="rejected",
                reason="attachment grant timing is missing or invalid",
            )
        if has_grant_timing and time.time() >= float(expires_at):
            return _AttachmentIntegrityFailure(
                status="expired",
                reason="attachment grant expired before model binding",
                kind="run_inbox_attachment_grant_expired",
            )
        if store.attachment_grant_is_revoked(
            chat_id=item.chat_id,
            command_id=item.command_id,
            grant_ref=actual_grant_ref,
        ):
            return _AttachmentIntegrityFailure(
                status="rejected",
                reason="attachment grant was revoked before model binding",
                kind="run_inbox_attachment_grant_revoked",
            )
        digest = hashlib.sha256()
        try:
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return _AttachmentIntegrityFailure(
                status="rejected",
                reason="attachment snapshot could not be verified",
            )
        if digest.hexdigest() != expected_hex:
            return _AttachmentIntegrityFailure(
                status="rejected",
                reason=(
                    "attachment snapshot digest changed before model binding"
                ),
            )
    return None


class RuntimeInboxAdmission:
    """Apply the current attempt fence to the existing persistent Inbox."""

    def __init__(
        self,
        *,
        store: RuntimeTurnControlStore,
        run_id: str,
        runtime_metadata: dict[str, Any],
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._runtime_metadata = dict(runtime_metadata)
        attempt_id = str(
            self._runtime_metadata.get("attempt_id") or run_id,
        ).strip()
        generation = max(
            0,
            int(self._runtime_metadata.get("runtime_generation") or 0),
        )
        revision_value = self._runtime_metadata.get("runtime_revision")
        self._fence = ExecutionFence(
            attempt_id=attempt_id,
            generation=generation,
            expected_revision=(
                max(0, int(revision_value))
                if revision_value is not None
                else None
            ),
            lease_owner=(
                str(
                    self._runtime_metadata.get("runtime_lease_owner") or "",
                ).strip()
                or None
            ),
        )
        self._claimant_id = self._actor_id(self._fence)

    @staticmethod
    def _actor_id(fence: ExecutionFence) -> str:
        return f"native:{fence.attempt_id}:{fence.generation}"

    def _current_run_items(self, *, after_seq: int) -> list[Any]:
        """Return only inputs owned by this Run's execution loop.

        ``queue_next`` records deliberately share the durable chat/run inbox
        used for admission receipts, but their stable target is the next turn.
        The current Run must leave those records untouched for an explicit
        next-task starter instead of rejecting or accidentally consuming them.
        """

        items = self._store.read_admissible_inbox(
            self._run_id,
            after_seq=after_seq,
            claimant_id=self._claimant_id,
        )
        return [
            item
            for item in items
            if item.target is None
            or (
                item.target.kind == "run"
                and item.target.target_id == self._run_id
            )
        ]

    @staticmethod
    def _model_message_for_item(item: RunInboxItemV2) -> ModelMessage:
        converted = model_message_from_runtime_message(item.message)
        return converted.model_copy(
            update={
                "metadata": {
                    **converted.metadata,
                    "client_turn_id": item.client_turn_id,
                    "run_inbox_seq": item.seq,
                    "operation": "steer",
                    "command_id": item.command_id,
                    "payload_digest": item.payload_digest,
                    "source_attempt_id": item.attempt_id,
                    "intent": item.intent,
                },
            },
        )

    def _canonical_source_item(
        self,
        *,
        inbox_seq: int,
        command_id: str,
        client_turn_id: str,
        payload_digest: str,
        source_attempt_id: str | None = None,
    ) -> tuple[RunInboxItemV2 | None, RuntimeInboxConflict | None]:
        item = next(
            (
                candidate
                for candidate in self._store.read_inbox(self._run_id)
                if candidate.seq == inbox_seq
                and isinstance(candidate, RunInboxItemV2)
            ),
            None,
        )
        if item is None:
            return None, RuntimeInboxConflict(
                item_seq=inbox_seq,
                client_turn_id=client_turn_id,
                reason=(
                    "continuation source is missing from the canonical Run Inbox"
                ),
                kind="run_inbox_continuation_source_missing",
            )
        if (
            item.command_id != command_id
            or item.client_turn_id != client_turn_id
            or item.payload_digest != payload_digest
            or item.run_id != self._run_id
            or (
                source_attempt_id is not None
                and item.attempt_id != source_attempt_id
            )
        ):
            return None, RuntimeInboxConflict(
                item_seq=inbox_seq,
                client_turn_id=client_turn_id,
                reason=(
                    "continuation source identity does not match the canonical "
                    "Run Inbox record"
                ),
                kind="run_inbox_continuation_source_mismatch",
            )
        disposition = self._store.current_dispositions(self._run_id).get(
            item.seq
        )
        source_is_available = disposition is not None and (
            disposition.status in {"claimed", "bound", "consumed"}
            or (
                disposition.status == "cancelled"
                and bool(disposition.superseded_by_command_id)
                and disposition.reason.startswith(
                    "superseded before provider surface by immediate redirect "
                )
            )
        )
        if not source_is_available:
            return None, RuntimeInboxConflict(
                item_seq=item.seq,
                client_turn_id=item.client_turn_id,
                reason=(
                    "continuation source is not an admitted or consumed input"
                ),
                kind="run_inbox_continuation_source_unavailable",
            )
        integrity_error = _attachment_integrity_error(
            item,
            store=self._store,
        )
        if integrity_error is not None:
            return None, RuntimeInboxConflict(
                item_seq=item.seq,
                client_turn_id=item.client_turn_id,
                reason=integrity_error.reason,
                kind=integrity_error.kind,
            )
        return item, None

    def validate_continuation_sources(
        self,
        messages: Sequence[ModelMessage],
    ) -> RuntimeInboxConflict | None:
        """Recheck canonical source bytes and authority before every resend."""

        for message in continuation_source_messages(messages):
            metadata = message.metadata
            inbox_seq = int(metadata["run_inbox_seq"])
            command_id = str(metadata["command_id"])
            client_turn_id = str(metadata.get("client_turn_id") or "")
            payload_digest = str(metadata["payload_digest"])
            source_attempt_id = (
                str(metadata.get("source_attempt_id") or "").strip() or None
            )
            item, conflict = self._canonical_source_item(
                inbox_seq=inbox_seq,
                command_id=command_id,
                client_turn_id=client_turn_id,
                payload_digest=payload_digest,
                source_attempt_id=source_attempt_id,
            )
            if conflict is not None or item is None:
                return conflict
            canonical = self._model_message_for_item(item)
            if (
                message.role != canonical.role
                or message.id != canonical.id
                or message.name != canonical.name
                or message.content != canonical.content
            ):
                return RuntimeInboxConflict(
                    item_seq=item.seq,
                    client_turn_id=item.client_turn_id,
                    reason=(
                        "continuation source content does not match the "
                        "canonical Run Inbox record"
                    ),
                    kind="run_inbox_continuation_source_mismatch",
                )
        return None

    def recover_continuation_sources(
        self,
        refs: Sequence[RuntimeContinuationInputRef],
    ) -> tuple[tuple[ModelMessage, ...], RuntimeInboxConflict | None]:
        """Reload consumed sources by reference without re-admitting them."""

        recovered: list[ModelMessage] = []
        for ref in refs:
            item, conflict = self._canonical_source_item(
                inbox_seq=ref.inbox_seq,
                command_id=ref.command_id,
                client_turn_id=ref.client_turn_id,
                payload_digest=ref.payload_digest,
                source_attempt_id=ref.source_attempt_id,
            )
            if conflict is not None or item is None:
                return (), conflict
            message = self._model_message_for_item(item)
            actual_types = tuple(
                dict.fromkeys(
                    block.type
                    for block in message.content
                    if block.type in {"image", "file"}
                )
            )
            if isinstance(
                message.metadata.get("artifact_edit_target"), Mapping
            ):
                actual_types = (*actual_types, "artifact_edit_target")
            if actual_types != ref.content_types or message.role != ref.role:
                return (), RuntimeInboxConflict(
                    item_seq=item.seq,
                    client_turn_id=item.client_turn_id,
                    reason=(
                        "continuation source type does not match its sealed "
                        "capsule reference"
                    ),
                    kind="run_inbox_continuation_source_mismatch",
                )
            recovered.append(message)
        return tuple(recovered), None

    def claim_next(self, *, after_seq: int) -> RuntimeInboxAdmissionOutcome:
        """Claim at most one FIFO item and preserve legacy-fence recovery."""

        items = self._current_run_items(after_seq=after_seq)[:1]
        if not items:
            return RuntimeInboxAdmissionOutcome(next_after_seq=after_seq)
        item = items[0]
        next_after_seq = max(after_seq, item.seq)
        claim_fence = self._fence
        if (
            not str(self._runtime_metadata.get("attempt_id") or "").strip()
            and item.fence is not None
        ):
            claim_fence = item.fence
        actor_id = self._actor_id(claim_fence)
        try:
            self._store.claim_input(
                run_id=self._run_id,
                item_seq=item.seq,
                actor_id=actor_id,
                fence=claim_fence,
            )
        except RuntimeInputConflictError as exc:
            return RuntimeInboxAdmissionOutcome(
                next_after_seq=next_after_seq,
                conflict=RuntimeInboxConflict(
                    item_seq=item.seq,
                    client_turn_id=item.client_turn_id,
                    reason=str(exc),
                ),
            )
        if isinstance(item, RunInboxItemV2):
            integrity_error = _attachment_integrity_error(
                item,
                store=self._store,
            )
            if integrity_error is not None:
                self._store.settle_input(
                    run_id=self._run_id,
                    item_seq=item.seq,
                    actor_id=actor_id,
                    fence=claim_fence,
                    status=integrity_error.status,
                    reason=integrity_error.reason,
                )
                return RuntimeInboxAdmissionOutcome(
                    next_after_seq=next_after_seq,
                    conflict=RuntimeInboxConflict(
                        item_seq=item.seq,
                        client_turn_id=item.client_turn_id,
                        reason=integrity_error.reason,
                        kind=integrity_error.kind,
                    ),
                )
        if isinstance(item, RunInboxItemV2):
            message = self._model_message_for_item(item)
        else:
            message_metadata = {
                "client_turn_id": item.client_turn_id,
                "run_inbox_seq": item.seq,
                "operation": "steer",
            }
            message = ModelMessage.text(
                "user",
                item.text,
                metadata={
                    **message_metadata,
                    **(
                        item.message_metadata.model_dump(
                            mode="json",
                            exclude_none=True,
                        )
                        if item.message_metadata is not None
                        else {}
                    ),
                },
            )
        return RuntimeInboxAdmissionOutcome(
            next_after_seq=next_after_seq,
            claim=RuntimeInboxClaim(
                item_seq=item.seq,
                client_turn_id=item.client_turn_id,
                actor_id=actor_id,
                fence=claim_fence,
                message=message,
            ),
        )

    def settle_consumed(
        self,
        claim: RuntimeInboxClaim,
    ) -> None:
        """Settle after the request has produced its first model surface."""

        self._store.settle_input(
            run_id=self._run_id,
            item_seq=claim.item_seq,
            actor_id=claim.actor_id,
            fence=claim.fence,
            status="consumed",
            reason=(
                "model surface persisted before provider stream became visible"
            ),
        )

    def bind_to_model_request(
        self,
        claim: RuntimeInboxClaim,
        *,
        model_request_id: str,
        model_epoch: int,
        intent_revision: int,
    ) -> None:
        """Durably bind a claim before the provider request can start."""

        self._store.bind_input_to_model_request(
            run_id=self._run_id,
            item_seq=claim.item_seq,
            actor_id=claim.actor_id,
            fence=claim.fence,
            model_request_id=model_request_id,
            model_epoch=model_epoch,
            intent_revision=intent_revision,
        )

    def has_pending(self, *, after_seq: int) -> bool:
        """Check for work without claiming it at the terminal boundary."""

        return bool(
            self._current_run_items(after_seq=after_seq)[:1],
        )

    def close_if_drained(self, *, after_seq: int) -> bool:
        """Win the terminal admission handshake or report pending work."""

        return (
            self._store.close_current_run_admission_if_drained(
                run_id=self._run_id,
                after_seq=after_seq,
                actor_id=self._claimant_id,
                fence=self._fence,
            )
            is not None
        )

    def peek_immediate_redirect(
        self,
        *,
        after_seq: int,
    ) -> RuntimeInboxWakeup | None:
        """Observe, but never claim, the next safe immediate redirect."""

        items = self._current_run_items(after_seq=after_seq)
        # A boundary-only supplement must not hide a later urgent redirect.
        # This is observation only: FIFO claiming/disposition still happens at
        # the owner model boundary, so scanning ahead cannot consume or reorder
        # accepted inputs.
        for item in items:
            if (
                not isinstance(item, RunInboxItemV2)
                or item.intent != "redirect"
                or item.wakeup != "immediate"
            ):
                continue
            try:
                self._store.validate_input_fence(item=item, fence=self._fence)
            except RuntimeInputConflictError:
                continue
            return RuntimeInboxWakeup(
                item_seq=item.seq,
                client_turn_id=item.client_turn_id,
                command_id=item.command_id,
                intent=item.intent,
            )
        return None

    def settle_superseded_before_surface(
        self,
        claim: RuntimeInboxClaim,
        *,
        superseding_command_id: str,
    ) -> None:
        """Close a claimed input whose request never produced a surface."""

        self._store.settle_input(
            run_id=self._run_id,
            item_seq=claim.item_seq,
            actor_id=claim.actor_id,
            fence=claim.fence,
            status="cancelled",
            reason=(
                "superseded before provider surface by immediate redirect "
                f"{superseding_command_id}"
            ),
            superseded_by_command_id=superseding_command_id,
        )


__all__ = [
    "RuntimeInboxAdmission",
    "RuntimeInboxAdmissionOutcome",
    "RuntimeInboxClaim",
    "RuntimeInboxConflict",
    "RuntimeInboxWakeup",
]
