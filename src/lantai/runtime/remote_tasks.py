# -*- coding: utf-8 -*-
"""Durable sidecar facts for asynchronous provider tasks.

This journal never owns the Runtime terminal state.  It only records the
external submission fact and the identifier needed to reconcile an existing
Runtime Effect after a crash.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

from ..utils.file_lock import locked_file_descriptor
from .effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from .effect_context import RuntimeEffectContext


class RemoteTaskState(str, Enum):
    PREPARED = "prepared"
    SUBMISSION_UNKNOWN = "submission_unknown"
    SUBMITTED = "submitted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RemoteTaskRecord:
    operation_id: str
    run_id: str
    attempt_id: str
    effect_id: str
    owner_binding_id: str
    binding_revision: int
    credential_ref: str
    model_snapshot_hash: str
    state: RemoteTaskState
    revision: int
    remote_task_id: str | None = None
    recovery_worker_id: str | None = None
    local_cancel_requested: bool = False
    result_hash: str | None = None


_PROCESS_LOCK = threading.RLock()


class RemoteTaskJournal:
    def __init__(self, root: str | Path) -> None:
        directory = Path(root) / ".lantai" / "runtime" / "v1"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "remote-tasks.json"
        self.lock_path = directory / "remote-tasks.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with _PROCESS_LOCK, self.lock_path.open("a+b") as lock:
            with locked_file_descriptor(lock.fileno()):
                yield

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("invalid remote task journal")
        return data

    def _write(self, rows: list[dict]) -> None:
        fd, temporary = tempfile.mkstemp(
            prefix=".remote-tasks.",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _decode(row: dict) -> RemoteTaskRecord:
        return RemoteTaskRecord(
            **{**row, "state": RemoteTaskState(row["state"])},
        )

    @staticmethod
    def _encode(record: RemoteTaskRecord) -> dict:
        row = asdict(record)
        row["state"] = record.state.value
        return row

    def _latest(
        self,
        rows: list[dict],
        operation_id: str,
    ) -> tuple[int, RemoteTaskRecord]:
        matches = [
            (index, self._decode(row))
            for index, row in enumerate(rows)
            if row.get("operation_id") == operation_id
        ]
        if not matches:
            raise KeyError(operation_id)
        return matches[-1]

    def prepare_submission(self, **facts: object) -> RemoteTaskRecord:
        operation_id = str(facts["operation_id"])
        with self._locked():
            rows = self._read()
            try:
                _, existing = self._latest(rows, operation_id)
            except KeyError:
                existing = None
            if existing is not None:
                expected = {key: getattr(existing, key) for key in facts}
                if expected != facts:
                    raise ValueError(
                        "operation id was reused with different facts",
                    )
                return existing
            record = RemoteTaskRecord(
                **facts,  # type: ignore[arg-type]
                state=RemoteTaskState.PREPARED,
                revision=1,
            )
            rows.append(self._encode(record))
            self._write(rows)
            return record

    def _transition(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        state: RemoteTaskState | None = None,
        allowed_states: tuple[RemoteTaskState, ...] | None = None,
        **changes: Any,
    ) -> RemoteTaskRecord:
        with self._locked():
            rows = self._read()
            _, current = self._latest(rows, operation_id)
            if current.revision != expected_revision:
                raise ValueError("remote task revision conflict")
            if (
                allowed_states is not None
                and current.state not in allowed_states
            ):
                raise ValueError(
                    (
                        "remote task cannot transition from "
                        f"{current.state.value}"
                    ),
                )
            updated = replace(
                current,
                state=state or current.state,
                revision=current.revision + 1,
                **changes,
            )
            rows.append(self._encode(updated))
            self._write(rows)
            return updated

    def begin_submission(
        self,
        operation_id: str,
        *,
        expected_revision: int,
    ) -> RemoteTaskRecord:
        """Persist the no-replay fence immediately before the network POST.

        Once this returns, a crash may have happened during or after the
        external call, so the operation can only be reconciled.  It must never
        return to the automatic submission path.
        """

        return self._transition(
            operation_id,
            expected_revision=expected_revision,
            state=RemoteTaskState.SUBMISSION_UNKNOWN,
            allowed_states=(RemoteTaskState.PREPARED,),
        )

    def mark_submission_unknown(
        self,
        operation_id: str,
        *,
        expected_revision: int,
    ) -> RemoteTaskRecord:
        with self._locked():
            _, current = self._latest(self._read(), operation_id)
        if current.revision != expected_revision:
            raise ValueError("remote task revision conflict")
        if current.state is RemoteTaskState.SUBMISSION_UNKNOWN:
            return current
        return self._transition(
            operation_id,
            expected_revision=expected_revision,
            state=RemoteTaskState.SUBMISSION_UNKNOWN,
            allowed_states=(RemoteTaskState.PREPARED,),
        )

    def record_submitted(
        self,
        *,
        operation_id: str,
        expected_revision: int,
        remote_task_id: str,
    ) -> RemoteTaskRecord:
        if not str(remote_task_id).strip():
            raise ValueError("remote task id is required")
        return self._transition(
            operation_id,
            expected_revision=expected_revision,
            state=RemoteTaskState.SUBMITTED,
            allowed_states=(RemoteTaskState.SUBMISSION_UNKNOWN,),
            remote_task_id=str(remote_task_id),
        )

    def record_succeeded(
        self,
        *,
        operation_id: str,
        expected_revision: int,
        result_hash: str,
    ) -> RemoteTaskRecord:
        with self._locked():
            _, current = self._latest(self._read(), operation_id)
        if current.revision != expected_revision:
            raise ValueError("remote task revision conflict")
        if current.state is RemoteTaskState.SUCCEEDED:
            if current.result_hash == result_hash:
                return current
            raise ValueError(
                "remote task success result conflicts with journal",
            )
        return self._transition(
            operation_id,
            expected_revision=expected_revision,
            state=RemoteTaskState.SUCCEEDED,
            allowed_states=(RemoteTaskState.SUBMITTED,),
            result_hash=result_hash,
        )

    def record_failed(
        self,
        *,
        operation_id: str,
        expected_revision: int,
        failure_hash: str | None = None,
    ) -> RemoteTaskRecord:
        with self._locked():
            _, current = self._latest(self._read(), operation_id)
        if current.revision != expected_revision:
            raise ValueError("remote task revision conflict")
        if current.state is RemoteTaskState.FAILED:
            if current.result_hash == failure_hash:
                return current
            raise ValueError(
                "remote task failure result conflicts with journal",
            )
        return self._transition(
            operation_id,
            expected_revision=expected_revision,
            state=RemoteTaskState.FAILED,
            allowed_states=(RemoteTaskState.SUBMITTED,),
            result_hash=failure_hash,
        )

    def poll_reference(self, operation_id: str) -> RemoteTaskRecord:
        with self._locked():
            _, current = self._latest(self._read(), operation_id)
        if not current.remote_task_id:
            raise ValueError(
                "remote task id is not known; reconcile upstream first",
            )
        return current

    def claim_recovery(
        self,
        operation_id: str,
        *,
        worker_id: str,
        owner_binding_id: str,
        binding_revision: int,
        credential_ref: str,
    ) -> RemoteTaskRecord:
        with self._locked():
            rows = self._read()
            _, current = self._latest(rows, operation_id)
            if (
                current.owner_binding_id != owner_binding_id
                or current.binding_revision != binding_revision
                or current.credential_ref != credential_ref
            ):
                raise ValueError(
                    "authorization changed; remote task cannot be queried",
                )
            if (
                current.recovery_worker_id
                and current.recovery_worker_id != worker_id
            ):
                raise ValueError(
                    (
                        "remote task is already claimed by another recovery w"
                        "orker"
                    ),
                )
            if current.recovery_worker_id == worker_id:
                return current
            updated = replace(
                current,
                recovery_worker_id=worker_id,
                revision=current.revision + 1,
            )
            rows.append(self._encode(updated))
            self._write(rows)
            return updated

    def record_local_cancel(
        self,
        operation_id: str,
        *,
        expected_revision: int,
    ) -> RemoteTaskRecord:
        return self._transition(
            operation_id,
            expected_revision=expected_revision,
            local_cancel_requested=True,
        )

    def records_for(self, operation_id: str) -> list[RemoteTaskRecord]:
        with self._locked():
            return [
                self._decode(row)
                for row in self._read()
                if row.get("operation_id") == operation_id
            ]


class RuntimeRemoteTaskCoordinator:
    """Bind provider task facts to the existing Runtime Effect state machine.

    The remote journal records only reconciliation facts.  Admission and the
    terminal result always pass through the source Runtime Effect, which stays
    the single owner of executable and terminal state.
    """

    def __init__(
        self,
        *,
        effect_journal: RuntimeEffectJournal,
        remote_journal: RemoteTaskJournal,
    ) -> None:
        self.effect_journal = effect_journal
        self.remote_journal = remote_journal

    def _source_effect(self, record: RemoteTaskRecord):
        effect = self.effect_journal.latest_effect(
            record.run_id,
            record.attempt_id,
            record.effect_id,
        )
        if effect is None:
            raise ValueError("remote task requires an existing Runtime Effect")
        if (
            effect.run_id != record.run_id
            or effect.attempt_id != record.attempt_id
            or effect.effect_id != record.effect_id
        ):
            raise ValueError("remote task does not match its Runtime Effect")
        return effect

    def prepare_submission(self, **facts: object) -> RemoteTaskRecord:
        probe = RemoteTaskRecord(
            **facts,  # type: ignore[arg-type]
            state=RemoteTaskState.PREPARED,
            revision=0,
        )
        effect = self._source_effect(probe)
        if effect.status != "running":
            raise ValueError("remote task Runtime Effect must be running")
        return self.remote_journal.prepare_submission(**facts)

    def begin_submission(
        self,
        operation_id: str,
        *,
        expected_revision: int,
    ) -> RemoteTaskRecord:
        record = self.remote_journal.records_for(operation_id)[-1]
        effect = self._source_effect(record)
        if effect.status != "running":
            raise ValueError(
                "ambiguous or completed operation must not be resubmitted",
            )
        return self.remote_journal.begin_submission(
            operation_id,
            expected_revision=expected_revision,
        )

    def mark_submission_unknown(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> RemoteTaskRecord:
        record = self.remote_journal.records_for(operation_id)[-1]
        effect = self._source_effect(record)
        if effect.status == "running":
            ambiguous = reduce_runtime_effect(
                effect,
                RuntimeEffectReducerAction(
                    type="mark_ambiguous",
                    ambiguity_reason=reason,
                ),
            ).effect
            self.effect_journal.append_effect(ambiguous)
        elif effect.status != "ambiguous":
            raise ValueError(
                "remote submission is no longer eligible for reconciliation",
            )
        return self.remote_journal.mark_submission_unknown(
            operation_id,
            expected_revision=expected_revision,
        )

    def record_submitted(
        self,
        *,
        operation_id: str,
        expected_revision: int,
        remote_task_id: str,
    ) -> RemoteTaskRecord:
        current = self.remote_journal.records_for(operation_id)[-1]
        if self._source_effect(current).status not in {"running", "ambiguous"}:
            raise ValueError("remote task Runtime Effect cannot be reconciled")
        return self.remote_journal.record_submitted(
            operation_id=operation_id,
            expected_revision=expected_revision,
            remote_task_id=remote_task_id,
        )

    def record_succeeded(
        self,
        *,
        operation_id: str,
        expected_revision: int,
        result_hash: str,
    ) -> RemoteTaskRecord:
        current = self.remote_journal.records_for(operation_id)[-1]
        effect = self._source_effect(current)
        if effect.status == "running":
            committed = reduce_runtime_effect(
                effect,
                RuntimeEffectReducerAction(
                    type="commit",
                    result_hash=result_hash,
                ),
            ).effect
            self.effect_journal.append_effect(committed)
        elif effect.status == "ambiguous":
            committed = reduce_runtime_effect(
                effect,
                RuntimeEffectReducerAction(
                    type="probe_committed",
                    result_hash=result_hash,
                ),
            ).effect
            self.effect_journal.append_effect(committed)
        elif effect.status not in {"committed", "probed_committed"} or (
            effect.result_hash != result_hash
        ):
            raise ValueError("remote result conflicts with the Runtime Effect")
        return self.remote_journal.record_succeeded(
            operation_id=operation_id,
            expected_revision=expected_revision,
            result_hash=result_hash,
        )

    def record_failed(
        self,
        *,
        operation_id: str,
        expected_revision: int,
        failure_hash: str | None = None,
    ) -> RemoteTaskRecord:
        current = self.remote_journal.records_for(operation_id)[-1]
        effect = self._source_effect(current)
        if effect.status == "running":
            failed = reduce_runtime_effect(
                effect,
                RuntimeEffectReducerAction(type="fail"),
            ).effect
            self.effect_journal.append_effect(failed)
        elif effect.status == "ambiguous":
            if not failure_hash:
                raise ValueError(
                    (
                        "verified failure evidence is required to reconcile a"
                        "mbiguity"
                    ),
                )
            reconciled = reduce_runtime_effect(
                effect,
                RuntimeEffectReducerAction(
                    type="probe_committed",
                    result_hash=failure_hash,
                ),
            ).effect
            self.effect_journal.append_effect(reconciled)
        elif effect.status == "probed_committed":
            if not failure_hash or effect.result_hash != failure_hash:
                raise ValueError(
                    "remote failure conflicts with the Runtime Effect",
                )
        elif effect.status != "failed":
            raise ValueError(
                "remote failure conflicts with the Runtime Effect",
            )
        return self.remote_journal.record_failed(
            operation_id=operation_id,
            expected_revision=expected_revision,
            failure_hash=failure_hash,
        )


@dataclass(slots=True)
class TokenDanceRemoteSubmissionObserver:
    """Persist a TokenDance submission fence around the provider POST.

    The observer keeps the provider layer free of Runtime storage concerns.
    Its terminal rows are sidecar evidence only; the surrounding
    ``ToolTransaction`` remains the sole owner of the Runtime Effect terminal
    state.
    """

    coordinator: RuntimeRemoteTaskCoordinator
    context: RuntimeEffectContext
    owner_binding_id: str
    binding_revision: int
    credential_ref: str
    model_snapshot_hash: str
    operation_id: str
    assert_authorization_current: Callable[[], None]
    _record: RemoteTaskRecord | None = field(
        default=None,
        init=False,
        repr=False,
    )

    @classmethod
    def create(
        cls,
        *,
        coordinator: RuntimeRemoteTaskCoordinator,
        context: RuntimeEffectContext,
        authorization: Any,
        model_id: str,
        protocol: str,
        prompt: str,
        request_options: Mapping[str, object],
        assert_authorization_current: Callable[[], None],
    ) -> "TokenDanceRemoteSubmissionObserver":
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        snapshot_payload = {
            "provider_id": "tokendance",
            "model_id": str(model_id),
            "protocol": str(protocol),
            "prompt_sha256": prompt_sha256,
            "request_options": dict(request_options),
        }
        snapshot_json = json.dumps(
            snapshot_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        model_snapshot_hash = (
            "sha256:"
            + hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        )
        operation_material = "\x1f".join(
            (
                context.run_id,
                context.attempt_id,
                context.effect_id,
                str(authorization.context_id),
                model_snapshot_hash,
            ),
        )
        operation_id = (
            "tokendance-remote-v1:"
            + hashlib.sha256(operation_material.encode("utf-8")).hexdigest()
        )
        return cls(
            coordinator=coordinator,
            context=context,
            owner_binding_id=str(authorization.context_id),
            binding_revision=int(authorization.binding_revision),
            credential_ref=str(authorization.credential_ref),
            model_snapshot_hash=model_snapshot_hash,
            operation_id=operation_id,
            assert_authorization_current=assert_authorization_current,
        )

    def before_submit(self) -> None:
        """Verify ownership and persist the no-replay fence before POST."""

        self.assert_authorization_current()
        prepared = self.coordinator.prepare_submission(
            operation_id=self.operation_id,
            run_id=self.context.run_id,
            attempt_id=self.context.attempt_id,
            effect_id=self.context.effect_id,
            owner_binding_id=self.owner_binding_id,
            binding_revision=self.binding_revision,
            credential_ref=self.credential_ref,
            model_snapshot_hash=self.model_snapshot_hash,
        )
        self._record = self.coordinator.begin_submission(
            self.operation_id,
            expected_revision=prepared.revision,
        )

    def response_received(self) -> None:
        """Reject every late response after an account or binding change."""

        self.assert_authorization_current()

    def submission_unknown(self) -> None:
        (
            "Keep the pre-POST fence unknown; the outer transaction s"
            "ettles Effect."
        )

        if self._record is None:
            return
        self._record = self.coordinator.remote_journal.mark_submission_unknown(
            self.operation_id,
            expected_revision=self._record.revision,
        )

    def submitted(self, task_id: str) -> None:
        if self._record is None:
            raise ValueError("remote submission was not fenced before POST")
        self._record = self.coordinator.record_submitted(
            operation_id=self.operation_id,
            expected_revision=self._record.revision,
            remote_task_id=task_id,
        )

    def succeeded(self, result_hash: str) -> None:
        if self._record is None:
            raise ValueError("remote submission was not recorded")
        self._record = self.coordinator.remote_journal.record_succeeded(
            operation_id=self.operation_id,
            expected_revision=self._record.revision,
            result_hash=result_hash,
        )

    def failed(self, failure_hash: str) -> None:
        if self._record is None:
            raise ValueError("remote submission was not recorded")
        self._record = self.coordinator.remote_journal.record_failed(
            operation_id=self.operation_id,
            expected_revision=self._record.revision,
            failure_hash=failure_hash,
        )


def validate_remote_artifact_url(url: str, *, allowed_hosts: set[str]) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("remote artifact URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("remote artifact URL cannot contain credentials")
    host = parsed.hostname.lower().rstrip(".")
    allowed = {item.lower().rstrip(".") for item in allowed_hosts}
    if host not in allowed:
        raise ValueError("remote artifact host is not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("remote artifact URL cannot target a private address")
    if parsed.fragment:
        raise ValueError("remote artifact URL cannot contain a fragment")
    return url


@dataclass(frozen=True, slots=True)
class RemoteArtifactDownload:
    """A download admitted without forwarding provider credentials."""

    url: str
    content_type: str
    content_length: int
    destination: Path
    forward_headers: Mapping[str, str] = field(default_factory=dict)


def prepare_remote_artifact_download(
    url: str,
    *,
    allowed_hosts: set[str],
    allowed_content_types: set[str],
    content_type: str,
    content_length: int,
    max_bytes: int,
    destination_root: str | Path,
    relative_path: str | Path,
) -> RemoteArtifactDownload:
    """Validate response metadata and constrain the destination path.

    Redirect targets must be passed through this function again.  The returned
    request intentionally contains no authorization headers, so a provider key
    is never forwarded to object storage.
    """

    safe_url = validate_remote_artifact_url(url, allowed_hosts=allowed_hosts)
    normalized_type = str(content_type).split(";", 1)[0].strip().lower()
    allowed_types = {item.strip().lower() for item in allowed_content_types}
    if normalized_type not in allowed_types:
        raise ValueError("remote artifact content type is not allowed")
    # pylint: disable=R0916
    if (
        not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length < 0
        or not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes < 1
        or content_length > max_bytes
    ):
        raise ValueError("remote artifact size exceeds the allowed boundary")
    # pylint: enable=R0916
    root = Path(destination_root).resolve()
    candidate = (root / Path(relative_path)).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise ValueError("remote artifact destination escapes its root")
    return RemoteArtifactDownload(
        url=safe_url,
        content_type=normalized_type,
        content_length=content_length,
        destination=candidate,
    )


__all__ = [
    "RemoteTaskJournal",
    "RemoteArtifactDownload",
    "RemoteTaskRecord",
    "RemoteTaskState",
    "RuntimeRemoteTaskCoordinator",
    "TokenDanceRemoteSubmissionObserver",
    "prepare_remote_artifact_download",
    "validate_remote_artifact_url",
]
