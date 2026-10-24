# -*- coding: utf-8 -*-
"""Persistent receipts and inbox messages for chat RuntimeRuns."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from lantai.runtime.coherence.contracts import (
    AuthorityProvenance,
    ExecutionFence,
)
from lantai.runtime.messages import RuntimeMessage
from lantai.utils.file_lock import locked_file_descriptor

from lantai.security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
    write_text_atomic_no_follow,
)


TurnOperation = Literal["submit", "steer", "reconnect", "retry"]
TurnReceiptStatus = Literal["accepted", "steered", "queued", "rejected"]
InputTargetKind = Literal["run", "chat", "workspace", "next_turn"]
InputWakeup = Literal["immediate", "model_boundary", "next_turn", "manual"]
InputIntent = Literal["supplement", "redirect", "queue_next"]
InputAdmissionStatus = Literal["admitted"]
InputExecutionStatus = Literal[
    "pending",
    "claimed",
    "waiting_boundary",
    "waiting_approval",
    "bound_to_request",
    "applied",
    "cancelled",
    "superseded",
    "not_applied",
]
InputDispositionStatus = Literal[
    "claimed",
    "bound",
    "released",
    "consumed",
    "cancelled",
    "rerouted",
    "rejected",
    "expired",
]

MAX_PENDING_CONTROL_INPUTS_PER_RUN = 128
MAX_CONTROL_INPUT_HISTORY_PER_RUN = 4096
_TERMINAL_INPUT_DISPOSITIONS = {
    "consumed",
    "cancelled",
    "rejected",
    "expired",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def control_input_grant_ref(
    *,
    chat_id: str,
    run_id: str,
    canonical_path: str,
    digest: str,
) -> str:
    """Bind one immutable local attachment snapshot to its owning Run."""

    encoded = "\0".join(
        (chat_id, run_id, canonical_path, digest),
    ).encode("utf-8")
    return f"file-grant:sha256:{hashlib.sha256(encoded).hexdigest()}"


class TurnReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_turn_id: str
    operation: TurnOperation
    run_id: str
    attempt_id: str
    chat_id: str
    accepted_at: datetime = Field(default_factory=_utc_now)
    status: TurnReceiptStatus

    @field_validator("client_turn_id", "run_id", "attempt_id", "chat_id")
    @classmethod
    def non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("receipt identity cannot be empty")
        return text


class InputTarget(BaseModel):
    """Stable destination selected when an input is admitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: InputTargetKind
    target_id: str

    @field_validator("target_id")
    @classmethod
    def non_empty_target(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("input target cannot be empty")
        return text


class RunInboxArtifactBaseRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    digest: str | None = None


class RunInboxArtifactSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["page", "paragraph", "region"]
    page: int | None = Field(default=None, ge=1)
    paragraph: int | None = Field(default=None, ge=1)
    region: str | None = None

    @model_validator(mode="after")
    def validate_selection_value(self):
        selected = {
            "page": self.page,
            "paragraph": self.paragraph,
            "region": self.region,
        }
        if selected[self.kind] is None or any(
            value is not None
            for key, value in selected.items()
            if key != self.kind
        ):
            raise ValueError(
                "artifact selection must contain only its selected kind",
            )
        return self


class RunInboxArtifactEditTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    chat_id: str
    asset_id: str
    work_id: str
    edit_path: str
    base_revision: RunInboxArtifactBaseRevision
    selection: RunInboxArtifactSelection | None = None

    @field_validator("chat_id", "asset_id", "work_id", "edit_path")
    @classmethod
    def non_empty_target_field(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("artifact edit target field cannot be empty")
        return text


class RunInboxMessageMetadata(BaseModel):
    """Strict metadata allowlist admitted to an in-flight model message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_edit_target: RunInboxArtifactEditTarget | None = None
    work_edit_context: str | None = None

    @field_validator("work_edit_context")
    @classmethod
    def normalize_context(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("work_edit_context cannot be empty")
        return text


class RunInboxItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(ge=1)
    client_turn_id: str
    run_id: str
    chat_id: str
    text: str
    accepted_at: datetime = Field(default_factory=_utc_now)
    attempt_id: str | None = None
    target: InputTarget | None = None
    wakeup: InputWakeup = "model_boundary"
    fence: ExecutionFence | None = None
    provenance: AuthorityProvenance | None = None
    message_metadata: RunInboxMessageMetadata | None = None

    @field_validator("client_turn_id", "run_id", "chat_id", "text")
    @classmethod
    def non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("inbox field cannot be empty")
        return text

    @field_validator("attempt_id")
    @classmethod
    def normalize_attempt_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("attempt_id cannot be empty")
        return text


class RunInboxItemV2(BaseModel):
    """Authoritative structured input record stored outside the v1 inbox."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    seq: int = Field(ge=1)
    command_id: str
    client_turn_id: str
    run_id: str
    chat_id: str
    attempt_id: str
    intent: InputIntent
    message: RuntimeMessage
    payload_digest: str
    accepted_at: datetime = Field(default_factory=_utc_now)
    target: InputTarget
    wakeup: InputWakeup
    fence: ExecutionFence
    provenance: AuthorityProvenance
    replaces_command_id: str | None = None

    @field_validator(
        "command_id",
        "client_turn_id",
        "run_id",
        "chat_id",
        "attempt_id",
        "payload_digest",
        "replaces_command_id",
    )
    @classmethod
    def non_empty_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("structured inbox identity cannot be empty")
        return text

    @model_validator(mode="after")
    def valid_user_input(self) -> "RunInboxItemV2":
        if self.message.role != "user":
            raise ValueError("structured inbox message must have user role")
        if not self.message.content:
            raise ValueError("structured inbox message cannot be empty")
        if not any(
            (block.type == "text" and bool((block.text or "").strip()))
            or (block.type in {"image", "file"} and bool(block.data))
            for block in self.message.content
        ):
            raise ValueError("structured inbox message has no usable content")
        if any(
            block.type not in {"text", "image", "file"}
            for block in self.message.content
        ):
            raise ValueError("structured inbox message contains unsafe blocks")
        return self

    @property
    def text(self) -> str:
        """Compatibility projection; an image-only input stays text-free."""

        return self.message.text_content()


class ControlInputReceiptV2(BaseModel):
    """Truthful admission projection derived from one v2 command record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    command_id: str
    client_turn_id: str
    run_id: str
    attempt_id: str
    chat_id: str
    server_seq: int = Field(ge=1)
    accepted_at: datetime
    admission_status: InputAdmissionStatus = "admitted"
    execution_status: InputExecutionStatus = "pending"
    target: InputTarget
    payload_digest: str
    next_waiting_reason: str = "awaiting_owner"
    disposition_status: InputDispositionStatus | None = None
    disposition_reason: str = ""
    last_transition_at: datetime | None = None
    model_request_id: str | None = None
    model_epoch: int | None = Field(default=None, ge=0)
    intent_revision: int | None = Field(default=None, ge=0)
    replaces_command_id: str | None = None
    superseded_by_command_id: str | None = None


class RunInboxDisposition(BaseModel):
    """Append-only claim or final disposition for one admitted input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    item_seq: int = Field(ge=1)
    client_turn_id: str
    attempt_id: str
    status: InputDispositionStatus
    actor_id: str
    fence: ExecutionFence
    occurred_at: datetime = Field(default_factory=_utc_now)
    reason: str = ""
    reroute_target: InputTarget | None = None
    model_request_id: str | None = None
    model_epoch: int | None = Field(default=None, ge=0)
    intent_revision: int | None = Field(default=None, ge=0)
    superseded_by_command_id: str | None = None

    @field_validator(
        "event_id",
        "run_id",
        "client_turn_id",
        "attempt_id",
        "actor_id",
    )
    @classmethod
    def non_empty_identity(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("inbox disposition identity cannot be empty")
        return text

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return value.strip()

    @field_validator("model_request_id", "superseded_by_command_id")
    @classmethod
    def normalize_model_request_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("model_request_id cannot be empty")
        return text


class ControlAttachmentGrantRevocation(BaseModel):
    """Durable denial for one command-scoped attachment grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    chat_id: str
    run_id: str
    command_id: str
    grant_ref: str
    actor_id: str
    occurred_at: datetime = Field(default_factory=_utc_now)
    reason: str = "attachment grant revoked before model request binding"

    @field_validator(
        "chat_id",
        "run_id",
        "command_id",
        "grant_ref",
        "actor_id",
        "reason",
    )
    @classmethod
    def non_empty_value(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError(
                "attachment grant revocation field cannot be empty",
            )
        return text


class RunInputAdmissionGate(BaseModel):
    """Durable terminal handshake for inputs owned by the current Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: str
    attempt_id: str
    actor_id: str
    fence: ExecutionFence
    after_seq: int = Field(ge=0)
    status: Literal["closed"] = "closed"
    closed_at: datetime = Field(default_factory=_utc_now)
    reason: str = "current Run reached a drained terminal boundary"

    @field_validator("run_id", "attempt_id", "actor_id", "reason")
    @classmethod
    def non_empty_value(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("input admission gate field cannot be empty")
        return text


class RunInboxReplacement(BaseModel):
    (
        "One atomic record for superseding an input and admitting"
        " its replacement."
    )

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    original_command_id: str
    original_item_seq: int = Field(ge=1)
    original_payload_digest: str
    replacement: RunInboxItemV2
    supersession: RunInboxDisposition

    @field_validator("original_command_id", "original_payload_digest")
    @classmethod
    def non_empty_identity(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("replacement identity cannot be empty")
        return text

    @model_validator(mode="after")
    def valid_atomic_replacement(self) -> "RunInboxReplacement":
        if self.replacement.replaces_command_id != self.original_command_id:
            raise ValueError(
                "replacement does not identify its original command",
            )
        if self.supersession.item_seq != self.original_item_seq:
            raise ValueError(
                "supersession does not identify the original item",
            )
        if self.supersession.status != "cancelled":
            raise ValueError("replacement supersession must be cancelled")
        if (
            self.supersession.superseded_by_command_id
            != self.replacement.command_id
        ):
            raise ValueError("supersession does not identify its replacement")
        if (
            self.supersession.run_id != self.replacement.run_id
            or self.supersession.attempt_id != self.replacement.attempt_id
        ):
            raise ValueError("replacement and supersession fence do not match")
        return self


class RuntimeInputConflictError(RuntimeError):
    """Raised when an input claim or disposition violates its fence."""


class RuntimeInputClosedError(RuntimeError):
    """Raised when a new current-Run input loses the terminal handshake."""

    def __init__(self, gate: RunInputAdmissionGate):
        self.gate = gate
        self.run_id = gate.run_id
        self.attempt_id = gate.attempt_id
        super().__init__(
            f"current Run input admission is closed for {gate.run_id}",
        )


class RuntimeInputCapacityError(RuntimeError):
    """Raised before admission when a Run's bounded inbox is full."""

    def __init__(self, *, limit_kind: Literal["queue", "history"], limit: int):
        self.limit_kind = limit_kind
        self.limit = limit
        super().__init__(f"control input {limit_kind} is full (limit={limit})")


RunInboxRecord = RunInboxItem | RunInboxItemV2


def _input_payload_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _message_payload_value(message: RuntimeMessage) -> dict[str, object]:
    """Exclude server grant timing from client-payload idempotency.

    A retry must recover the first durable receipt even though the server's
    short-lived authorization envelope was issued at another wall-clock time.
    The immutable grant identity, content digest, path, and version remain in
    the payload hash.
    """

    value = message.model_dump(mode="json", exclude_none=True)
    for block in value.get("content", []):
        if not isinstance(block, dict):
            continue
        data = block.get("data")
        if not isinstance(data, dict):
            continue
        data.pop("grant_issued_at", None)
        data.pop("grant_expires_at", None)
    return value


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(fd, "a+", encoding="utf-8") as handle:
            fd = -1
            with locked_file_descriptor(handle.fileno()):
                yield
    finally:
        if fd >= 0:
            os.close(fd)


# pylint: disable=R0904
class RuntimeTurnControlStore:
    """Append-only Run Inbox plus idempotent client-turn receipts."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v1",
        )
        self.v2_root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v2",
        )

    def _receipt_path(self, chat_id: str, client_turn_id: str) -> Path:
        key = safe_storage_key(
            "turn-receipt",
            f"{chat_id}:{client_turn_id}",
            suffix=".json",
        )
        return contained_storage_path(self.root, "turn-receipts", key)

    def _inbox_path(self, run_id: str) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(self.root, "runs", key, "inbox.jsonl")

    def _disposition_path(self, run_id: str) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(
            self.root,
            "runs",
            key,
            "inbox-dispositions.jsonl",
        )

    def _input_admission_gate_path(self, run_id: str) -> Path:
        key = safe_storage_key("runtime-run", run_id, suffix="")
        return contained_storage_path(
            self.root,
            "runs",
            key,
            "current-input-admission-gate.json",
        )

    def _v2_command_path(self, chat_id: str, command_id: str) -> Path:
        key = safe_storage_key(
            "control-input-v2",
            f"{chat_id}:{command_id}",
            suffix=".json",
        )
        return contained_storage_path(self.v2_root, "commands", key)

    def _v2_replacement_path(self, chat_id: str, command_id: str) -> Path:
        key = safe_storage_key(
            "control-input-replacement-v2",
            f"{chat_id}:{command_id}",
            suffix=".json",
        )
        return contained_storage_path(self.v2_root, "replacements", key)

    def _v2_attachment_grant_revocation_path(
        self,
        *,
        chat_id: str,
        command_id: str,
        grant_ref: str,
    ) -> Path:
        key = safe_storage_key(
            "control-attachment-grant-revocation-v1",
            f"{chat_id}:{command_id}:{grant_ref}",
            suffix=".json",
        )
        return contained_storage_path(
            self.v2_root,
            "attachment-grant-revocations",
            key,
        )

    def _v2_receipt(self, item: RunInboxItemV2) -> ControlInputReceiptV2:
        waiting_reason = {
            "immediate": "awaiting_safe_yield",
            "model_boundary": "awaiting_model_boundary",
            "next_turn": "awaiting_next_turn",
            "manual": "awaiting_owner",
        }[item.wakeup]
        execution_status: InputExecutionStatus = "pending"
        disposition_status: InputDispositionStatus | None = None
        disposition_reason = ""
        last_transition_at: datetime | None = None
        model_request_id: str | None = None
        model_epoch: int | None = None
        intent_revision: int | None = None
        superseded_by_command_id: str | None = None
        disposition = self.current_dispositions(item.run_id).get(item.seq)
        if disposition is not None:
            disposition_status = disposition.status
            disposition_reason = disposition.reason
            last_transition_at = disposition.occurred_at
            model_request_id = disposition.model_request_id
            model_epoch = disposition.model_epoch
            intent_revision = disposition.intent_revision
            superseded_by_command_id = disposition.superseded_by_command_id
            if disposition.status == "claimed":
                execution_status = "claimed"
                waiting_reason = "awaiting_model_request"
            elif disposition.status == "bound":
                execution_status = "bound_to_request"
                waiting_reason = "awaiting_model_surface"
            elif disposition.status == "released":
                waiting_reason = "awaiting_owner"
            elif disposition.status == "consumed":
                execution_status = "applied"
                waiting_reason = ""
            elif disposition.status == "cancelled":
                execution_status = (
                    "superseded"
                    if disposition.superseded_by_command_id is not None
                    or "superseded" in disposition.reason.casefold()
                    else "cancelled"
                )
                waiting_reason = ""
            elif disposition.status in {"rejected", "expired"}:
                execution_status = "not_applied"
                waiting_reason = ""
            elif disposition.status == "rerouted":
                waiting_reason = (
                    "awaiting_next_turn"
                    if disposition.reroute_target is not None
                    and disposition.reroute_target.kind == "next_turn"
                    else "awaiting_owner"
                )
        return ControlInputReceiptV2(
            command_id=item.command_id,
            client_turn_id=item.client_turn_id,
            run_id=item.run_id,
            attempt_id=item.attempt_id,
            chat_id=item.chat_id,
            server_seq=item.seq,
            accepted_at=item.accepted_at,
            target=item.target,
            payload_digest=item.payload_digest,
            execution_status=execution_status,
            next_waiting_reason=waiting_reason,
            disposition_status=disposition_status,
            disposition_reason=disposition_reason,
            last_transition_at=last_transition_at,
            model_request_id=model_request_id,
            model_epoch=model_epoch,
            intent_revision=intent_revision,
            replaces_command_id=item.replaces_command_id,
            superseded_by_command_id=superseded_by_command_id,
        )

    def get_input_receipt(
        self,
        *,
        chat_id: str,
        command_id: str,
    ) -> ControlInputReceiptV2 | None:
        """Return a v2 receipt derived from its authoritative command."""

        item = self.get_input_item(chat_id=chat_id, command_id=command_id)
        if item is None:
            return None
        return self._v2_receipt(item)

    def get_input_item(
        self,
        *,
        chat_id: str,
        command_id: str,
    ) -> RunInboxItemV2 | None:
        """Return the immutable v2 command used to verify a retried payload."""

        path = self._v2_command_path(chat_id, command_id)
        replacement_path = self._v2_replacement_path(chat_id, command_id)
        direct_exists = path.is_file()
        replacement_exists = replacement_path.is_file()
        if direct_exists and replacement_exists:
            raise RuntimeInputConflictError(
                "command id has conflicting persistent records",
            )
        if direct_exists:
            return RunInboxItemV2.model_validate_json(
                read_text_no_follow(path),
            )
        if replacement_exists:
            record = RunInboxReplacement.model_validate_json(
                read_text_no_follow(replacement_path),
            )
            return record.replacement
        return None

    def get_receipt(
        self,
        *,
        chat_id: str,
        client_turn_id: str,
    ) -> TurnReceipt | None:
        path = self._receipt_path(chat_id, client_turn_id)
        if not path.is_file():
            return None
        return TurnReceipt.model_validate_json(read_text_no_follow(path))

    def create_receipt(
        self,
        *,
        client_turn_id: str,
        operation: TurnOperation,
        run_id: str,
        attempt_id: str,
        chat_id: str,
        status: TurnReceiptStatus,
    ) -> TurnReceipt:
        path = self._receipt_path(chat_id, client_turn_id)
        with _locked(path.with_name(f".{path.name}.lock")):
            if path.is_file():
                return TurnReceipt.model_validate_json(
                    read_text_no_follow(path),
                )
            receipt = TurnReceipt(
                client_turn_id=client_turn_id,
                operation=operation,
                run_id=run_id,
                attempt_id=attempt_id,
                chat_id=chat_id,
                status=status,
            )
            write_text_atomic_no_follow(
                path,
                receipt.model_dump_json(indent=2),
            )
            return receipt

    def append_steer(
        self,
        *,
        client_turn_id: str,
        run_id: str,
        attempt_id: str,
        chat_id: str,
        text: str,
        target: InputTarget | None = None,
        wakeup: InputWakeup = "model_boundary",
        fence: ExecutionFence | None = None,
        provenance: AuthorityProvenance | None = None,
        message_metadata: RunInboxMessageMetadata | None = None,
    ) -> tuple[TurnReceipt, RunInboxItem]:
        existing_receipt = self.get_receipt(
            chat_id=chat_id,
            client_turn_id=client_turn_id,
        )
        if existing_receipt is not None and (
            existing_receipt.operation != "steer"
            or existing_receipt.run_id != run_id
            or existing_receipt.attempt_id != attempt_id
            or existing_receipt.chat_id != chat_id
        ):
            raise RuntimeInputConflictError(
                "command id already admitted with a different payload",
            )
        admitted_target = target or InputTarget(
            kind="run",
            target_id=run_id,
        )
        path = self._inbox_path(run_id)
        with _locked(path.with_name(".inbox.lock")):
            existing_items = self.read_inbox(run_id)
            existing_item = next(
                (
                    item
                    for item in existing_items
                    if item.client_turn_id == client_turn_id
                ),
                None,
            )
            # pylint: disable=R0916
            if existing_item is not None and (
                existing_item.run_id != run_id
                or existing_item.attempt_id != attempt_id
                or existing_item.chat_id != chat_id
                or existing_item.text != text.strip()
                or existing_item.target != admitted_target
                or existing_item.wakeup != wakeup
            ):
                raise RuntimeInputConflictError(
                    "command id already admitted with a different payload",
                )
            # pylint: enable=R0916
            if existing_item is None:
                self._raise_if_current_run_admission_closed(
                    run_id=run_id,
                    target=admitted_target,
                )
                admitted_fence = fence or ExecutionFence(
                    attempt_id=attempt_id,
                )
                admitted_provenance = provenance or AuthorityProvenance(
                    authority_id=f"input:{chat_id}:{client_turn_id}",
                    source_kind="direct_user_input",
                    actor_kind="user",
                    actor_id=f"chat:{chat_id}",
                    source_ref=f"turn-receipt:{chat_id}:{client_turn_id}",
                    scopes=(f"chat:{chat_id}", f"run:{run_id}"),
                    delegable=False,
                )
                item = RunInboxItem(
                    seq=(existing_items[-1].seq + 1) if existing_items else 1,
                    client_turn_id=client_turn_id,
                    run_id=run_id,
                    chat_id=chat_id,
                    text=text,
                    attempt_id=attempt_id,
                    target=admitted_target,
                    wakeup=wakeup,
                    fence=admitted_fence,
                    provenance=admitted_provenance,
                    message_metadata=message_metadata,
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_APPEND
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    with os.fdopen(fd, "a", encoding="utf-8") as handle:
                        fd = -1
                        handle.write(item.model_dump_json() + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    if fd >= 0:
                        os.close(fd)
            else:
                item = existing_item
        receipt = existing_receipt or self.create_receipt(
            client_turn_id=client_turn_id,
            operation="steer",
            run_id=run_id,
            attempt_id=attempt_id,
            chat_id=chat_id,
            status="steered",
        )
        return receipt, item

    def append_input(
        self,
        *,
        command_id: str,
        run_id: str,
        attempt_id: str,
        chat_id: str,
        message: RuntimeMessage,
        intent: InputIntent,
        client_turn_id: str | None = None,
        target: InputTarget | None = None,
        wakeup: InputWakeup | None = None,
        fence: ExecutionFence | None = None,
        provenance: AuthorityProvenance | None = None,
    ) -> tuple[ControlInputReceiptV2, RunInboxItemV2]:
        """Atomically admit one structured input and derive its receipt."""

        normalized_command_id = command_id.strip()
        normalized_client_turn_id = (
            client_turn_id.strip()
            if client_turn_id is not None
            else normalized_command_id
        )
        admitted_target = target or (
            InputTarget(kind="next_turn", target_id=chat_id)
            if intent == "queue_next"
            else InputTarget(kind="run", target_id=run_id)
        )
        admitted_wakeup = (
            wakeup
            or {
                "supplement": "model_boundary",
                "redirect": "immediate",
                "queue_next": "next_turn",
            }[intent]
        )
        payload_digest = _input_payload_digest(
            {
                "schema_version": 2,
                "command_id": normalized_command_id,
                "client_turn_id": normalized_client_turn_id,
                "run_id": run_id.strip(),
                "attempt_id": attempt_id.strip(),
                "chat_id": chat_id.strip(),
                "intent": intent,
                "message": _message_payload_value(message),
                "target": admitted_target.model_dump(mode="json"),
                "wakeup": admitted_wakeup,
            },
        )
        command_path = self._v2_command_path(chat_id, normalized_command_id)
        with _locked(command_path.with_name(f".{command_path.name}.lock")):
            existing = self.get_input_item(
                chat_id=chat_id,
                command_id=normalized_command_id,
            )
            if existing is not None:
                if existing.payload_digest != payload_digest:
                    raise RuntimeInputConflictError(
                        "command id already admitted with a different payload",
                    )
                return self._v2_receipt(existing), existing

            inbox_path = self._inbox_path(run_id)
            with _locked(inbox_path.with_name(".inbox.lock")):
                self._raise_if_current_run_admission_closed(
                    run_id=run_id,
                    target=admitted_target,
                )
                existing_items = self.read_inbox(run_id)
                if len(existing_items) >= MAX_CONTROL_INPUT_HISTORY_PER_RUN:
                    raise RuntimeInputCapacityError(
                        limit_kind="history",
                        limit=MAX_CONTROL_INPUT_HISTORY_PER_RUN,
                    )
                dispositions = self.current_dispositions(run_id)
                active_items = sum(
                    1
                    for existing_item in existing_items
                    if (
                        dispositions.get(existing_item.seq) is None
                        or dispositions[existing_item.seq].status
                        not in _TERMINAL_INPUT_DISPOSITIONS
                    )
                )
                if active_items >= MAX_PENDING_CONTROL_INPUTS_PER_RUN:
                    raise RuntimeInputCapacityError(
                        limit_kind="queue",
                        limit=MAX_PENDING_CONTROL_INPUTS_PER_RUN,
                    )
                admitted_fence = fence or ExecutionFence(
                    attempt_id=attempt_id,
                )
                admitted_provenance = provenance or AuthorityProvenance(
                    authority_id=(f"input:{chat_id}:{normalized_command_id}"),
                    source_kind="direct_user_input",
                    actor_kind="user",
                    actor_id=f"chat:{chat_id}",
                    source_ref=(
                        f"control-input-v2:{chat_id}:{normalized_command_id}"
                    ),
                    scopes=(f"chat:{chat_id}", f"run:{run_id}"),
                    delegable=False,
                )
                item = RunInboxItemV2(
                    seq=(existing_items[-1].seq + 1) if existing_items else 1,
                    command_id=normalized_command_id,
                    client_turn_id=normalized_client_turn_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    chat_id=chat_id,
                    intent=intent,
                    message=message,
                    payload_digest=payload_digest,
                    target=admitted_target,
                    wakeup=admitted_wakeup,
                    fence=admitted_fence,
                    provenance=admitted_provenance,
                )
                write_text_atomic_no_follow(
                    command_path,
                    item.model_dump_json(indent=2),
                )
        return self._v2_receipt(item), item

    def attachment_grant_is_revoked(
        self,
        *,
        chat_id: str,
        command_id: str,
        grant_ref: str,
    ) -> bool:
        """Fail closed when a durable command-scoped revocation exists."""

        path = self._v2_attachment_grant_revocation_path(
            chat_id=chat_id,
            command_id=command_id,
            grant_ref=grant_ref,
        )
        if not path.is_file():
            return False
        try:
            revocation = ControlAttachmentGrantRevocation.model_validate_json(
                read_text_no_follow(path),
            )
        except Exception:
            return True
        return not (
            revocation.chat_id != chat_id
            or revocation.command_id != command_id
            or revocation.grant_ref != grant_ref
        )

    def revoke_attachment_grant(
        self,
        *,
        chat_id: str,
        command_id: str,
        grant_ref: str,
        actor_id: str,
        reason: str = "attachment grant revoked before model request binding",
    ) -> ControlInputReceiptV2:
        """Persistently deny an attachment before it enters a model request."""

        item = self.get_input_item(chat_id=chat_id, command_id=command_id)
        if item is None:
            raise RuntimeInputConflictError("control input does not exist")
        attachment_grants = {
            str(block.data.get("grant_ref") or "").strip()
            for block in item.message.content
            if block.type in {"image", "file"}
        }
        if grant_ref not in attachment_grants:
            raise RuntimeInputConflictError(
                "attachment grant does not belong to the control input",
            )
        revocation_path = self._v2_attachment_grant_revocation_path(
            chat_id=chat_id,
            command_id=command_id,
            grant_ref=grant_ref,
        )
        inbox_path = self._inbox_path(item.run_id)
        with _locked(inbox_path.with_name(".inbox.lock")):
            current = self.current_dispositions(item.run_id).get(item.seq)
            if current is not None and current.status == "rejected":
                if self.attachment_grant_is_revoked(
                    chat_id=chat_id,
                    command_id=command_id,
                    grant_ref=grant_ref,
                ):
                    return self._v2_receipt(item)
                raise RuntimeInputConflictError(
                    "control input was rejected for another reason",
                )
            if current is not None and current.status not in {
                "claimed",
                "released",
            }:
                raise RuntimeInputConflictError(
                    "attachment grant cannot revoke from " f"{current.status}",
                )
            with _locked(
                revocation_path.with_name(f".{revocation_path.name}.lock"),
            ):
                if not revocation_path.is_file():
                    revocation = ControlAttachmentGrantRevocation(
                        chat_id=item.chat_id,
                        run_id=item.run_id,
                        command_id=item.command_id,
                        grant_ref=grant_ref,
                        actor_id=actor_id,
                        reason=reason,
                    )
                    write_text_atomic_no_follow(
                        revocation_path,
                        revocation.model_dump_json(indent=2),
                    )
            disposition = RunInboxDisposition(
                run_id=item.run_id,
                item_seq=item.seq,
                client_turn_id=item.client_turn_id,
                attempt_id=item.attempt_id,
                status="rejected",
                actor_id=actor_id,
                fence=item.fence,
                reason=reason,
            )
            self._append_disposition(disposition)
        return self._v2_receipt(item)

    def cancel_input(
        self,
        *,
        chat_id: str,
        command_id: str,
        actor_id: str,
        reason: str = "cancelled before model request binding",
    ) -> ControlInputReceiptV2:
        """Cancel a v2 input only while it has not entered a model request."""

        item = self.get_input_item(chat_id=chat_id, command_id=command_id)
        if item is None:
            raise RuntimeInputConflictError("control input does not exist")
        inbox_path = self._inbox_path(item.run_id)
        with _locked(inbox_path.with_name(".inbox.lock")):
            item = self.get_input_item(
                chat_id=chat_id,
                command_id=command_id,
            )
            if item is None:
                raise RuntimeInputConflictError("control input does not exist")
            current = self.current_dispositions(item.run_id).get(item.seq)
            if current is not None and current.status == "cancelled":
                if current.superseded_by_command_id is not None:
                    raise RuntimeInputConflictError(
                        "control input is already superseded",
                    )
                return self._v2_receipt(item)
            if current is not None and current.status not in {
                "claimed",
                "released",
            }:
                raise RuntimeInputConflictError(
                    f"control input cannot cancel from {current.status}",
                )
            disposition = RunInboxDisposition(
                run_id=item.run_id,
                item_seq=item.seq,
                client_turn_id=item.client_turn_id,
                attempt_id=item.attempt_id,
                status="cancelled",
                actor_id=actor_id,
                fence=item.fence,
                reason=reason,
            )
            self._append_disposition(disposition)
        return self._v2_receipt(item)

    def replace_input(
        self,
        *,
        original_command_id: str,
        command_id: str,
        run_id: str,
        attempt_id: str,
        chat_id: str,
        message: RuntimeMessage,
        intent: InputIntent,
        client_turn_id: str | None = None,
        target: InputTarget | None = None,
        wakeup: InputWakeup | None = None,
        fence: ExecutionFence | None = None,
        provenance: AuthorityProvenance | None = None,
    ) -> tuple[ControlInputReceiptV2, ControlInputReceiptV2, RunInboxItemV2]:
        """Atomically supersede one unbound input and admit its replacement."""

        normalized_original_id = original_command_id.strip()
        normalized_command_id = command_id.strip()
        if normalized_original_id == normalized_command_id:
            raise RuntimeInputConflictError(
                "replacement command id must differ from original",
            )
        normalized_client_turn_id = (
            client_turn_id.strip()
            if client_turn_id is not None
            else normalized_command_id
        )
        admitted_target = target or (
            InputTarget(kind="next_turn", target_id=chat_id)
            if intent == "queue_next"
            else InputTarget(kind="run", target_id=run_id)
        )
        admitted_wakeup = (
            wakeup
            or {
                "supplement": "model_boundary",
                "redirect": "immediate",
                "queue_next": "next_turn",
            }[intent]
        )
        payload_digest = _input_payload_digest(
            {
                "schema_version": 2,
                "command_id": normalized_command_id,
                "client_turn_id": normalized_client_turn_id,
                "run_id": run_id.strip(),
                "attempt_id": attempt_id.strip(),
                "chat_id": chat_id.strip(),
                "intent": intent,
                "message": _message_payload_value(message),
                "target": admitted_target.model_dump(mode="json"),
                "wakeup": admitted_wakeup,
                "replaces_command_id": normalized_original_id,
            },
        )
        command_path = self._v2_command_path(chat_id, normalized_command_id)
        replacement_path = self._v2_replacement_path(
            chat_id,
            normalized_command_id,
        )
        with _locked(command_path.with_name(f".{command_path.name}.lock")):
            existing = self.get_input_item(
                chat_id=chat_id,
                command_id=normalized_command_id,
            )
            if existing is not None:
                if (
                    existing.payload_digest != payload_digest
                    or existing.replaces_command_id != normalized_original_id
                ):
                    raise RuntimeInputConflictError(
                        "command id already admitted with a different payload",
                    )
                original = self.get_input_item(
                    chat_id=chat_id,
                    command_id=normalized_original_id,
                )
                if original is None:
                    raise RuntimeInputConflictError(
                        "replacement original is missing",
                    )
                return (
                    self._v2_receipt(original),
                    self._v2_receipt(existing),
                    existing,
                )

            inbox_path = self._inbox_path(run_id)
            with _locked(inbox_path.with_name(".inbox.lock")):
                self._raise_if_current_run_admission_closed(
                    run_id=run_id,
                    target=admitted_target,
                )
                original = self.get_input_item(
                    chat_id=chat_id,
                    command_id=normalized_original_id,
                )
                if original is None:
                    raise RuntimeInputConflictError(
                        "replacement original does not exist",
                    )
                if (
                    original.run_id != run_id.strip()
                    or original.attempt_id != attempt_id.strip()
                    or original.chat_id != chat_id.strip()
                ):
                    raise RuntimeInputConflictError(
                        "replacement target does not match original input",
                    )
                current = self.current_dispositions(run_id).get(original.seq)
                if current is not None and current.status not in {
                    "claimed",
                    "released",
                }:
                    raise RuntimeInputConflictError(
                        f"control input cannot replace from {current.status}",
                    )
                existing_items = self.read_inbox(run_id)
                if len(existing_items) >= MAX_CONTROL_INPUT_HISTORY_PER_RUN:
                    raise RuntimeInputCapacityError(
                        limit_kind="history",
                        limit=MAX_CONTROL_INPUT_HISTORY_PER_RUN,
                    )
                admitted_fence = fence or original.fence
                admitted_provenance = provenance or AuthorityProvenance(
                    authority_id=(f"input:{chat_id}:{normalized_command_id}"),
                    source_kind="direct_user_input",
                    actor_kind="user",
                    actor_id=f"chat:{chat_id}",
                    source_ref=(
                        f"control-input-v2:{chat_id}:{normalized_command_id}"
                    ),
                    scopes=(f"chat:{chat_id}", f"run:{run_id}"),
                    delegable=False,
                )
                replacement = RunInboxItemV2(
                    seq=(existing_items[-1].seq + 1) if existing_items else 1,
                    command_id=normalized_command_id,
                    client_turn_id=normalized_client_turn_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    chat_id=chat_id,
                    intent=intent,
                    message=message,
                    payload_digest=payload_digest,
                    target=admitted_target,
                    wakeup=admitted_wakeup,
                    fence=admitted_fence,
                    provenance=admitted_provenance,
                    replaces_command_id=normalized_original_id,
                )
                supersession = RunInboxDisposition(
                    run_id=original.run_id,
                    item_seq=original.seq,
                    client_turn_id=original.client_turn_id,
                    attempt_id=original.attempt_id,
                    status="cancelled",
                    actor_id=f"chat:{chat_id}",
                    fence=original.fence,
                    reason=(
                        "superseded before model request binding by "
                        f"{normalized_command_id}"
                    ),
                    superseded_by_command_id=normalized_command_id,
                )
                record = RunInboxReplacement(
                    original_command_id=normalized_original_id,
                    original_item_seq=original.seq,
                    original_payload_digest=original.payload_digest,
                    replacement=replacement,
                    supersession=supersession,
                )
                write_text_atomic_no_follow(
                    replacement_path,
                    record.model_dump_json(indent=2),
                )
        return (
            self._v2_receipt(original),
            self._v2_receipt(replacement),
            replacement,
        )

    def read_inbox(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
    ) -> list[RunInboxRecord]:
        path = self._inbox_path(run_id)
        items: list[RunInboxRecord] = []
        if path.is_file():
            for line in read_text_no_follow(path).splitlines():
                if not line.strip():
                    continue
                item = RunInboxItem.model_validate(json.loads(line))
                if item.seq > after_seq:
                    items.append(item)
        commands_dir = contained_storage_path(self.v2_root, "commands")
        if commands_dir.is_dir():
            for command_path in commands_dir.glob("*.json"):
                item = RunInboxItemV2.model_validate_json(
                    read_text_no_follow(command_path),
                )
                if item.run_id == run_id and item.seq > after_seq:
                    items.append(item)
        replacements_dir = contained_storage_path(
            self.v2_root,
            "replacements",
        )
        if replacements_dir.is_dir():
            for replacement_path in replacements_dir.glob("*.json"):
                record = RunInboxReplacement.model_validate_json(
                    read_text_no_follow(replacement_path),
                )
                item = record.replacement
                if item.run_id == run_id and item.seq > after_seq:
                    items.append(item)
        return sorted(items, key=lambda item: item.seq)

    def read_dispositions(self, run_id: str) -> list[RunInboxDisposition]:
        path = self._disposition_path(run_id)
        dispositions: list[RunInboxDisposition] = []
        if path.is_file():
            for line in read_text_no_follow(path).splitlines():
                if line.strip():
                    dispositions.append(
                        RunInboxDisposition.model_validate(json.loads(line)),
                    )
        replacements_dir = contained_storage_path(
            self.v2_root,
            "replacements",
        )
        if replacements_dir.is_dir():
            for replacement_path in replacements_dir.glob("*.json"):
                record = RunInboxReplacement.model_validate_json(
                    read_text_no_follow(replacement_path),
                )
                if record.supersession.run_id == run_id:
                    dispositions.append(record.supersession)
        return sorted(
            dispositions,
            key=lambda disposition: (
                disposition.occurred_at,
                disposition.event_id,
            ),
        )

    def current_dispositions(
        self,
        run_id: str,
    ) -> dict[int, RunInboxDisposition]:
        current: dict[int, RunInboxDisposition] = {}
        for disposition in self.read_dispositions(run_id):
            current[disposition.item_seq] = disposition
        return current

    def read_admissible_inbox(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        claimant_id: str | None = None,
    ) -> list[RunInboxRecord]:
        """Read pending inputs, plus claims already owned by this consumer."""

        dispositions = self.current_dispositions(run_id)
        admissible: list[RunInboxRecord] = []
        for item in self.read_inbox(run_id, after_seq=after_seq):
            state = dispositions.get(item.seq)
            if state is None or (
                state.status == "released"
                or (
                    state.status == "claimed"
                    and claimant_id is not None
                    and state.actor_id == claimant_id
                )
            ):
                admissible.append(item)
        return admissible

    def read_current_run_admission_gate(
        self,
        run_id: str,
    ) -> RunInputAdmissionGate | None:
        """Read the durable terminal gate without changing admission state."""

        path = self._input_admission_gate_path(run_id)
        if not path.is_file():
            return None
        return RunInputAdmissionGate.model_validate_json(
            read_text_no_follow(path),
        )

    def close_current_run_admission_if_drained(
        self,
        *,
        run_id: str,
        after_seq: int,
        actor_id: str,
        fence: ExecutionFence,
    ) -> RunInputAdmissionGate | None:
        """Atomically close current-Run admission only when it is drained.

        New current-Run inputs and this terminal check share ``.inbox.lock``.
        Therefore either the input is durably visible and prevents closing, or
        the gate wins and the input is explicitly rejected. ``queue_next``
        targets are intentionally outside this Run-owner handshake.
        """

        path = self._inbox_path(run_id)
        gate_path = self._input_admission_gate_path(run_id)
        with _locked(path.with_name(".inbox.lock")):
            existing_gate = self.read_current_run_admission_gate(run_id)
            if existing_gate is not None:
                if (
                    existing_gate.actor_id != actor_id.strip()
                    or not existing_gate.fence.accepts(fence)
                ):
                    raise RuntimeInputConflictError(
                        (
                            "current Run input admission was closed by anothe"
                            "r owner"
                        ),
                    )
                return existing_gate

            dispositions = self.current_dispositions(run_id)
            for item in self.read_inbox(run_id):
                if not self._is_current_run_target(item, run_id=run_id):
                    continue
                disposition = dispositions.get(item.seq)
                if disposition is None or disposition.status not in {
                    "consumed",
                    "cancelled",
                    "rerouted",
                    "rejected",
                    "expired",
                }:
                    return None

            gate = RunInputAdmissionGate(
                run_id=run_id,
                attempt_id=fence.attempt_id,
                actor_id=actor_id,
                fence=fence,
                after_seq=after_seq,
            )
            write_text_atomic_no_follow(
                gate_path,
                gate.model_dump_json(indent=2),
            )
            return gate

    def claim_input(
        self,
        *,
        run_id: str,
        item_seq: int,
        actor_id: str,
        fence: ExecutionFence,
    ) -> RunInboxDisposition:
        """Claim one input exactly once under its admitted execution fence."""

        path = self._inbox_path(run_id)
        with _locked(path.with_name(".inbox.lock")):
            item = next(
                (
                    candidate
                    for candidate in self.read_inbox(run_id)
                    if candidate.seq == item_seq
                ),
                None,
            )
            if item is None:
                raise RuntimeInputConflictError("inbox item does not exist")
            self._validate_item_fence(item, fence)
            current = self.current_dispositions(run_id).get(item_seq)
            if current is not None:
                if (
                    current.status == "claimed"
                    and current.actor_id == actor_id
                ):
                    self._validate_disposition_fence(current, fence)
                    return current
                if current.status != "released":
                    raise RuntimeInputConflictError(
                        f"inbox item is already {current.status}",
                    )
            disposition = RunInboxDisposition(
                run_id=run_id,
                item_seq=item.seq,
                client_turn_id=item.client_turn_id,
                attempt_id=fence.attempt_id,
                status="claimed",
                actor_id=actor_id,
                fence=fence,
            )
            self._append_disposition(disposition)
            return disposition

    def validate_input_fence(
        self,
        *,
        item: RunInboxRecord,
        fence: ExecutionFence,
    ) -> None:
        """Validate a prospective owner fence without changing disposition."""

        self._validate_item_fence(item, fence)

    def release_input_claim(
        self,
        *,
        run_id: str,
        item_seq: int,
        recovery_actor_id: str,
        fence: ExecutionFence,
        reason: str,
    ) -> RunInboxDisposition:
        """Release an orphaned claim after deterministic recovery checks."""

        path = self._inbox_path(run_id)
        with _locked(path.with_name(".inbox.lock")):
            current = self.current_dispositions(run_id).get(item_seq)
            if current is None or current.status != "claimed":
                raise RuntimeInputConflictError("inbox item is not claimed")
            self._validate_disposition_fence(current, fence)
            disposition = RunInboxDisposition(
                run_id=run_id,
                item_seq=item_seq,
                client_turn_id=current.client_turn_id,
                attempt_id=fence.attempt_id,
                status="released",
                actor_id=recovery_actor_id,
                fence=fence,
                reason=reason,
            )
            self._append_disposition(disposition)
            return disposition

    def bind_input_to_model_request(
        self,
        *,
        run_id: str,
        item_seq: int,
        actor_id: str,
        fence: ExecutionFence,
        model_request_id: str,
        model_epoch: int,
        intent_revision: int,
    ) -> RunInboxDisposition:
        """Persist the exact model request that first carries an input."""

        path = self._inbox_path(run_id)
        with _locked(path.with_name(".inbox.lock")):
            current = self.current_dispositions(run_id).get(item_seq)
            if current is None:
                raise RuntimeInputConflictError("inbox item is not claimed")
            if current.status == "bound" and current.actor_id == actor_id:
                self._validate_disposition_fence(current, fence)
                if (
                    current.model_request_id != model_request_id.strip()
                    or current.model_epoch != model_epoch
                    or current.intent_revision != intent_revision
                ):
                    raise RuntimeInputConflictError(
                        "inbox item is already bound to another model request",
                    )
                return current
            if current.status != "claimed" or current.actor_id != actor_id:
                raise RuntimeInputConflictError(
                    f"inbox item cannot bind from {current.status}",
                )
            self._validate_disposition_fence(current, fence)
            disposition = RunInboxDisposition(
                run_id=run_id,
                item_seq=item_seq,
                client_turn_id=current.client_turn_id,
                attempt_id=fence.attempt_id,
                status="bound",
                actor_id=actor_id,
                fence=fence,
                reason="bound to model request",
                model_request_id=model_request_id,
                model_epoch=model_epoch,
                intent_revision=intent_revision,
            )
            self._append_disposition(disposition)
            return disposition

    def settle_input(
        self,
        *,
        run_id: str,
        item_seq: int,
        actor_id: str,
        fence: ExecutionFence,
        status: Literal["consumed", "cancelled", "rejected", "expired"],
        reason: str = "",
        superseded_by_command_id: str | None = None,
    ) -> RunInboxDisposition:
        """Append an irreversible final disposition for one input."""

        normalized_superseding_id = (
            superseded_by_command_id.strip()
            if superseded_by_command_id is not None
            else None
        )
        if normalized_superseding_id == "":
            raise ValueError("superseded_by_command_id cannot be empty")
        if normalized_superseding_id is not None and status != "cancelled":
            raise ValueError(
                "superseded_by_command_id requires cancelled status",
            )
        path = self._inbox_path(run_id)
        with _locked(path.with_name(".inbox.lock")):
            current = self.current_dispositions(run_id).get(item_seq)
            if current is None:
                raise RuntimeInputConflictError("inbox item is not claimed")
            if current.status == status and current.actor_id == actor_id:
                self._validate_disposition_fence(current, fence)
                if (
                    current.superseded_by_command_id
                    != normalized_superseding_id
                ):
                    raise RuntimeInputConflictError(
                        "inbox item is already settled with different "
                        "supersession lineage",
                    )
                return current
            if (
                current.status not in {"claimed", "bound"}
                or current.actor_id != actor_id
            ):
                raise RuntimeInputConflictError(
                    f"inbox item cannot settle from {current.status}",
                )
            self._validate_disposition_fence(current, fence)
            disposition = RunInboxDisposition(
                run_id=run_id,
                item_seq=item_seq,
                client_turn_id=current.client_turn_id,
                attempt_id=fence.attempt_id,
                status=status,
                actor_id=actor_id,
                fence=fence,
                reason=reason,
                model_request_id=current.model_request_id,
                model_epoch=current.model_epoch,
                intent_revision=current.intent_revision,
                superseded_by_command_id=normalized_superseding_id,
            )
            self._append_disposition(disposition)
            return disposition

    def reroute_input(
        self,
        *,
        run_id: str,
        item_seq: int,
        actor_id: str,
        fence: ExecutionFence,
        target: InputTarget,
        reason: str,
    ) -> RunInboxDisposition:
        """Record an explicit reroute; never silently retarget an input."""

        path = self._inbox_path(run_id)
        with _locked(path.with_name(".inbox.lock")):
            current = self.current_dispositions(run_id).get(item_seq)
            if current is None or current.status != "claimed":
                raise RuntimeInputConflictError("inbox item is not claimed")
            if current.actor_id != actor_id:
                raise RuntimeInputConflictError(
                    "inbox claim belongs to another actor",
                )
            self._validate_disposition_fence(current, fence)
            disposition = RunInboxDisposition(
                run_id=run_id,
                item_seq=item_seq,
                client_turn_id=current.client_turn_id,
                attempt_id=fence.attempt_id,
                status="rerouted",
                actor_id=actor_id,
                fence=fence,
                reason=reason,
                reroute_target=target,
            )
            self._append_disposition(disposition)
            return disposition

    def _append_disposition(self, disposition: RunInboxDisposition) -> None:
        path = self._disposition_path(disposition.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                fd = -1
                handle.write(disposition.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)

    def _raise_if_current_run_admission_closed(
        self,
        *,
        run_id: str,
        target: InputTarget,
    ) -> None:
        if target.kind != "run" or target.target_id != run_id:
            return
        gate = self.read_current_run_admission_gate(run_id)
        if gate is not None:
            raise RuntimeInputClosedError(gate)

    @staticmethod
    def _is_current_run_target(
        item: RunInboxRecord,
        *,
        run_id: str,
    ) -> bool:
        return item.target is None or (
            item.target.kind == "run" and item.target.target_id == run_id
        )

    @staticmethod
    def _validate_item_fence(
        item: RunInboxRecord,
        fence: ExecutionFence,
    ) -> None:
        if item.attempt_id is not None and item.attempt_id != fence.attempt_id:
            raise RuntimeInputConflictError(
                "inbox attempt fence does not match",
            )
        if item.fence is not None and not item.fence.accepts(fence):
            raise RuntimeInputConflictError(
                "inbox execution fence does not match",
            )
        if item.target is not None and (
            item.target.kind != "run" or item.target.target_id != item.run_id
        ):
            raise RuntimeInputConflictError("inbox target is not this run")

    @staticmethod
    def _validate_disposition_fence(
        disposition: RunInboxDisposition,
        fence: ExecutionFence,
    ) -> None:
        if not disposition.fence.accepts(fence):
            raise RuntimeInputConflictError("inbox claim fence does not match")


# pylint: enable=R0904


__all__ = [
    "ControlAttachmentGrantRevocation",
    "ControlInputReceiptV2",
    "InputAdmissionStatus",
    "InputDispositionStatus",
    "InputExecutionStatus",
    "InputIntent",
    "InputTarget",
    "InputTargetKind",
    "InputWakeup",
    "RunInboxItem",
    "RunInboxDisposition",
    "RunInboxMessageMetadata",
    "RunInputAdmissionGate",
    "RuntimeInputConflictError",
    "RuntimeInputClosedError",
    "RuntimeInputCapacityError",
    "RunInboxItemV2",
    "RunInboxRecord",
    "RuntimeTurnControlStore",
    "TurnOperation",
    "TurnReceipt",
    "TurnReceiptStatus",
    "control_input_grant_ref",
]
