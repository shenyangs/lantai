# -*- coding: utf-8 -*-
"""Contracts and durable evidence for scoped execution teams."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
    write_text_atomic_no_follow,
)
from ..utils.file_lock import locked_file_descriptor
from .collaboration import (
    CollaborationConvergencePolicy,
    CollaborationHostContract,
    CollaborationSynthesisContract,
)

FORBIDDEN_DELEGATION_TOOLS = frozenset(
    {
        "chat_with_agent",
        "check_agent_task",
        "collaborate_with_agents",
        "list_agents",
        "submit_to_agent",
    },
)

_WRITER_LOCKS_GUARD = threading.Lock()
_WRITER_LOCKS: dict[str, threading.RLock] = {}
_MAX_EXECUTION_IDENTITY_CHARS = 160


class ExecutionResourceBusy(RuntimeError):
    """Raised when an execution resource has a live foreign owner."""


class ExecutionResourceOwnershipLost(RuntimeError):
    """Raised when a fenced resource lease no longer belongs to its owner."""


def _process_writer_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _WRITER_LOCKS_GUARD:
        lock = _WRITER_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _WRITER_LOCKS[key] = lock
        return lock


def _normalized_relative_path(value: object) -> str:
    text = str(value or "").strip().replace(os.sep, "/")
    if not text or "\\" in text:
        raise ValueError(
            "artifact path must be a non-empty POSIX relative path",
        )
    path = PurePosixPath(text)
    if path.is_absolute() or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ValueError("artifact path must not escape its workspace")
    return path.as_posix()


def _normalized_resource_claim(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text or len(text) > 120:
        raise ValueError("resource claim must be a short non-empty name")
    if re.fullmatch(r"[a-z0-9][a-z0-9._:-]*", text) is None:
        raise ValueError("resource claim contains unsupported characters")
    return text


def infer_execution_resource_claims(
    allowed_tools: list[str],
    explicit_claims: list[str] | None = None,
) -> list[str]:
    """Return deterministic exclusive claims for scarce local resources."""

    claims = {
        _normalized_resource_claim(item) for item in (explicit_claims or [])
    }
    normalized_tools = {
        str(tool or "").strip().lower() for tool in allowed_tools
    }
    if any("browser" in tool for tool in normalized_tools):
        claims.add("browser")
    desktop_tools = {
        "mac_act",
        "mac_session",
        "desktop_act",
        "desktop_session",
        "computer_use",
    }
    if normalized_tools.intersection(desktop_tools):
        claims.add("desktop-input")
    return sorted(claims)


def _stable_member_task_id(contract_id: str, member_id: str) -> str:
    """Keep the member suffix and collision resistance inside Runner limits."""

    candidate = f"{contract_id}:{member_id}"
    if len(candidate) <= _MAX_EXECUTION_IDENTITY_CHARS:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:16]
    suffix = f":{member_id}:{digest}"
    prefix_chars = _MAX_EXECUTION_IDENTITY_CHARS - len(suffix)
    return f"{contract_id[:prefix_chars]}{suffix}"


class ExecutionArtifactContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    media_type: str = "application/octet-stream"
    minimum_bytes: int = Field(default=1, ge=0, le=2_000_000_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _normalized_relative_path(value)


class ExecutionMemberContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    label: str
    assignment: str
    depends_on: list[str] = Field(default_factory=list, max_length=3)
    minimum_output_chars: int = Field(default=0, ge=0, le=100_000)
    required_output_markers: list[str] = Field(
        default_factory=list,
        max_length=12,
    )
    effective_permissions: Literal["scoped_execute"] = "scoped_execute"
    permission_profile: Literal["delegated_workspace"] = "delegated_workspace"
    allowed_tools: list[str] = Field(min_length=1, max_length=64)
    resource_claims: list[str] = Field(default_factory=list, max_length=24)
    workspace_scope: str
    expected_artifacts: list[ExecutionArtifactContract] = Field(
        default_factory=list,
        max_length=24,
    )
    can_spawn_collaboration: Literal[False] = False
    workspace_write_access: Literal[True] = True
    external_effect_policy: Literal[
        "inherit_runtime_policy"
    ] = "inherit_runtime_policy"


class ExecutionRoundPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rounds: Literal[1] = 1
    allow_nested_collaboration: Literal[False] = False
    member_write_access: Literal[True] = True
    replacement_limit: Literal[1] = 1


class ExecutionTeamBudgetContract(BaseModel):
    """Root budget shared by every member attempt and host synthesis."""

    model_config = ConfigDict(extra="forbid")

    max_member_attempts: int = Field(ge=1, le=8)
    max_wall_seconds: int = Field(default=1800, ge=60, le=14_400)
    reserved_host_seconds: int = Field(default=30, ge=5, le=600)
    max_artifact_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1,
        le=2_000_000_000,
    )
    max_member_response_tokens: Literal[8192] = 8192


class ExecutionTeamContract(BaseModel):
    """Public team agreement plus private per-member assignments."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: Literal["execution"] = "execution"
    contract_id: str = Field(min_length=1)
    root_run_id: str = Field(min_length=1)
    parent_run_id: str = Field(min_length=1)
    parent_attempt_id: str = Field(min_length=1)
    parent_effect_id: str = Field(min_length=1)
    goal: str
    why_parallel: str
    effective_permissions: Literal["scoped_execute"] = "scoped_execute"
    participation_mode: Literal[
        "autonomous",
        "checkpoint",
        "interactive",
    ] = "autonomous"
    protocol: Literal["parallel_consultation"] = "parallel_consultation"
    host: CollaborationHostContract = Field(
        default_factory=CollaborationHostContract,
    )
    round_policy: ExecutionRoundPolicy = Field(
        default_factory=ExecutionRoundPolicy,
    )
    budget: ExecutionTeamBudgetContract
    convergence: CollaborationConvergencePolicy = Field(
        default_factory=CollaborationConvergencePolicy,
    )
    members: list[ExecutionMemberContract] = Field(min_length=1, max_length=4)
    acceptance_criteria: list[str] = Field(min_length=1)
    synthesis: CollaborationSynthesisContract = Field(
        default_factory=CollaborationSynthesisContract,
    )

    def public_dump(self) -> dict[str, object]:
        value = self.model_dump(mode="json")
        for member in value.get("members", []):
            if isinstance(member, dict):
                member.pop("assignment", None)
        return value


def build_execution_team_contract(
    *,
    contract_id: str,
    root_run_id: str,
    parent_run_id: str,
    parent_attempt_id: str,
    parent_effect_id: str,
    goal: str,
    why_parallel: str,
    tasks: list[dict[str, Any]],
    acceptance_criteria: list[str] | None = None,
    conflict_policy: Literal[
        "surface_conflicts",
        "prefer_stronger_evidence",
    ] = "surface_conflicts",
    participation_mode: Literal[
        "autonomous",
        "checkpoint",
        "interactive",
    ] = "autonomous",
    max_member_attempts: int | None = None,
    max_team_wall_seconds: int = 1800,
    max_artifact_bytes: int = 512 * 1024 * 1024,
) -> ExecutionTeamContract:
    execution_identity = {
        "contract_id": contract_id,
        "root_run_id": root_run_id,
        "parent_run_id": parent_run_id,
        "parent_attempt_id": parent_attempt_id,
        "parent_effect_id": parent_effect_id,
    }
    normalized_identity = {
        name: str(value or "").strip()
        for name, value in execution_identity.items()
    }
    missing_identity = [
        name for name, value in normalized_identity.items() if not value
    ]
    if missing_identity:
        raise ValueError(
            "execution team identity is incomplete: "
            + ", ".join(missing_identity),
        )
    contract_id = normalized_identity["contract_id"]
    normalized_goal = str(goal or "").strip() or "分工完成可验证交付"
    normalized_reason = str(why_parallel or "").strip() or "任务可按专长和依赖拆分"
    criteria = [
        str(item).strip()
        for item in (acceptance_criteria or [])
        if str(item).strip()
    ] or ["每个成员提交可核验结果", "父 Agent 验收产物并统一交付"]
    storage_key = safe_storage_key("execution-team", contract_id)
    members: list[ExecutionMemberContract] = []
    artifact_floor_bytes = 0
    for index, task in enumerate(tasks):
        member_id = f"member-{index + 1}"
        allowed_tools = list(
            dict.fromkeys(
                str(name).strip()
                for name in (task.get("allowed_tools") or [])
                if str(name).strip()
            ),
        )
        if not allowed_tools:
            raise ValueError(
                "execution members require at least one allowed tool",
            )
        forbidden = sorted(
            FORBIDDEN_DELEGATION_TOOLS.intersection(allowed_tools),
        )
        if forbidden:
            raise ValueError(
                "execution members cannot receive delegation tools: "
                + ", ".join(forbidden),
            )
        artifacts = []
        for raw in task.get("expected_artifacts") or []:
            if not isinstance(raw, dict):
                raise ValueError("expected_artifacts entries must be objects")
            artifacts.append(
                ExecutionArtifactContract(
                    path=_normalized_relative_path(raw.get("path")),
                    media_type=(
                        str(
                            raw.get("media_type")
                            or "application/octet-stream",
                        ).strip()
                    ),
                    minimum_bytes=int(raw.get("minimum_bytes", 1)),
                ),
            )
            artifact_floor_bytes += artifacts[-1].minimum_bytes
        members.append(
            ExecutionMemberContract(
                member_id=member_id,
                task_id=_stable_member_task_id(contract_id, member_id),
                agent_id=str(task["to_agent"]),
                label=str(task["label"]),
                assignment=str(task["text"]),
                depends_on=list(task.get("depends_on") or []),
                minimum_output_chars=int(
                    task.get("minimum_output_chars") or 0,
                ),
                required_output_markers=list(
                    task.get("required_output_markers") or [],
                ),
                allowed_tools=allowed_tools,
                resource_claims=infer_execution_resource_claims(
                    allowed_tools,
                    list(task.get("resource_claims") or []),
                ),
                workspace_scope=(
                    f".lantai/execution-teams/{storage_key}/{member_id}"
                ),
                expected_artifacts=artifacts,
            ),
        )
    if artifact_floor_bytes > max_artifact_bytes:
        raise ValueError(
            "expected artifact minimums exceed the root artifact budget",
        )
    attempt_budget = (
        int(max_member_attempts)
        if max_member_attempts is not None
        else min(8, max(1, len(members) * 2))
    )
    return ExecutionTeamContract(
        contract_id=contract_id,
        root_run_id=normalized_identity["root_run_id"],
        parent_run_id=normalized_identity["parent_run_id"],
        parent_attempt_id=normalized_identity["parent_attempt_id"],
        parent_effect_id=normalized_identity["parent_effect_id"],
        goal=normalized_goal,
        why_parallel=normalized_reason,
        participation_mode=participation_mode,
        budget=ExecutionTeamBudgetContract(
            max_member_attempts=attempt_budget,
            max_wall_seconds=max_team_wall_seconds,
            max_artifact_bytes=max_artifact_bytes,
        ),
        members=members,
        acceptance_criteria=criteria,
        synthesis=CollaborationSynthesisContract(
            conflict_policy=conflict_policy,
        ),
    )


def verify_execution_artifacts(
    workspace: Path,
    expected_artifacts: list[dict[str, Any] | ExecutionArtifactContract],
) -> list[dict[str, Any]]:
    """Verify required files without accepting symlinks or path escapes."""

    root = workspace.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("execution workspace is not a directory")
    verified: list[dict[str, Any]] = []
    for raw in expected_artifacts:
        artifact = (
            raw
            if isinstance(raw, ExecutionArtifactContract)
            else ExecutionArtifactContract(
                path=_normalized_relative_path(raw.get("path")),
                media_type=str(
                    raw.get("media_type") or "application/octet-stream",
                ),
                minimum_bytes=int(raw.get("minimum_bytes", 1)),
            )
        )
        relative_path = _normalized_relative_path(artifact.path)
        candidate = root.joinpath(*PurePosixPath(relative_path).parts)
        if candidate.is_symlink() or any(
            part.is_symlink()
            for part in candidate.parents
            if part != root.parent and (part == root or root in part.parents)
        ):
            raise ValueError(f"artifact is a symlink: {relative_path}")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"artifact is missing: {relative_path}") from exc
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"artifact escapes workspace: {relative_path}")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(resolved, flags)
        except OSError as exc:
            raise ValueError(
                f"artifact cannot be opened safely: {relative_path}",
            ) from exc
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError(
                    f"artifact is not a regular file: {relative_path}",
                )
            size_bytes = file_stat.st_size
            if size_bytes < artifact.minimum_bytes:
                raise ValueError(
                    "artifact is smaller than minimum_bytes: "
                    f"{relative_path}",
                )
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                for chunk in iter(
                    partial(handle.read, 1024 * 1024),
                    b"",
                ):
                    digest.update(chunk)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        verified.append(
            {
                "path": relative_path,
                "media_type": artifact.media_type,
                "size_bytes": size_bytes,
                "sha256": digest.hexdigest(),
            },
        )
    return verified


class ExecutionResourceLeaseStore:
    """Atomic, fenced leases for exclusive execution-team resources."""

    def __init__(self, state_root: Path, *, lantai_home: bool = False) -> None:
        base = state_root.expanduser().resolve(strict=False)
        runtime_home = base if lantai_home else base / ".lantai"
        self.root = runtime_home / "runtime" / "v1" / "execution-resources"
        self.path = contained_storage_path(self.root, "leases.json")
        self.lock_path = contained_storage_path(self.root, "leases.lock")

    @classmethod
    def global_for_working_dir(
        cls,
        working_dir: Path,
    ) -> "ExecutionResourceLeaseStore":
        """Use one authority shared by every chat, topic, and agent.

        Per-team snapshots remain under their state roots. Scarce browser,
        desktop, profile, and service claims belong to the application as a
        whole, so their fenced lease ledger must live directly under the
        configured Lantai working directory.
        """

        return cls(working_dir, lantai_home=True)

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        process_lock = _process_writer_lock(self.lock_path)
        with process_lock:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.lock_path, flags, 0o600)
            try:
                with locked_file_descriptor(descriptor):
                    yield
            finally:
                os.close(descriptor)

    @staticmethod
    def _checksum(leases: dict[str, dict[str, Any]]) -> str:
        canonical = json.dumps(
            leases,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _load_unlocked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise ValueError("execution resource lease path is a symlink")
        envelope = json.loads(read_text_no_follow(self.path))
        leases = envelope.get("leases")
        if envelope.get("schema_version") != 1 or not isinstance(leases, dict):
            raise ValueError("execution resource lease envelope is invalid")
        if envelope.get("checksum") != self._checksum(leases):
            raise ValueError("execution resource lease checksum mismatch")
        return leases

    def _save_unlocked(self, leases: dict[str, dict[str, Any]]) -> None:
        envelope = {
            "schema_version": 1,
            "leases": leases,
            "checksum": self._checksum(leases),
        }
        write_text_atomic_no_follow(
            self.path,
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
        )

    def acquire(
        self,
        resources: list[str],
        *,
        owner_id: str,
        ttl_seconds: float,
        now_epoch_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        normalized = sorted(
            {_normalized_resource_claim(item) for item in resources},
        )
        if not normalized:
            return []
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("resource lease owner_id is required")
        if ttl_seconds <= 0:
            raise ValueError("resource lease ttl_seconds must be positive")
        now = time.time() if now_epoch_seconds is None else now_epoch_seconds
        with self._writer_lock():
            leases = self._load_unlocked()
            for resource in normalized:
                current = leases.get(resource)
                if (
                    current
                    and float(current["expires_at_epoch_seconds"]) > now
                    and current.get("owner_id") != owner
                ):
                    raise ExecutionResourceBusy(
                        f"execution resource is busy: {resource}",
                    )
            acquired: list[dict[str, Any]] = []
            for resource in normalized:
                current = leases.get(resource)
                current_live = bool(
                    current
                    and float(current["expires_at_epoch_seconds"]) > now,
                )
                same_owner = bool(
                    current_live and current.get("owner_id") == owner,
                )
                generation = (
                    int(current.get("generation") or 0)
                    if same_owner
                    else int((current or {}).get("generation") or 0) + 1
                )
                record = {
                    "resource": resource,
                    "owner_id": owner,
                    "generation": generation,
                    "acquired_at_epoch_seconds": (
                        cast(dict[str, Any], current)[
                            "acquired_at_epoch_seconds"
                        ]
                        if same_owner
                        else now
                    ),
                    "expires_at_epoch_seconds": now + ttl_seconds,
                }
                leases[resource] = record
                acquired.append(dict(record))
            self._save_unlocked(leases)
            return acquired

    def wait_acquire(
        self,
        resources: list[str],
        *,
        owner_id: str,
        ttl_seconds: float,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.25,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                return self.acquire(
                    resources,
                    owner_id=owner_id,
                    ttl_seconds=ttl_seconds,
                )
            except ExecutionResourceBusy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(max(0.001, poll_interval_seconds), remaining))

    def assert_ownership(
        self,
        records: list[dict[str, Any]],
        *,
        now_epoch_seconds: float | None = None,
    ) -> bool:
        now = time.time() if now_epoch_seconds is None else now_epoch_seconds
        with self._writer_lock():
            leases = self._load_unlocked()
            self._assert_records_owned(leases, records, now)
        return True

    def renew(
        self,
        records: list[dict[str, Any]],
        *,
        ttl_seconds: float,
        now_epoch_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        if ttl_seconds <= 0:
            raise ValueError("resource lease ttl_seconds must be positive")
        if not records:
            return []
        now = time.time() if now_epoch_seconds is None else now_epoch_seconds
        with self._writer_lock():
            leases = self._load_unlocked()
            self._assert_records_owned(leases, records, now)
            renewed: list[dict[str, Any]] = []
            for record in records:
                resource = _normalized_resource_claim(record["resource"])
                current = dict(leases[resource])
                current["expires_at_epoch_seconds"] = now + ttl_seconds
                leases[resource] = current
                renewed.append(dict(current))
            self._save_unlocked(leases)
            return renewed

    @staticmethod
    def _assert_records_owned(
        leases: dict[str, dict[str, Any]],
        records: list[dict[str, Any]],
        now: float,
    ) -> None:
        for record in records:
            resource = _normalized_resource_claim(record.get("resource"))
            current = leases.get(resource)
            if (
                current is None
                or current.get("owner_id") != record.get("owner_id")
                or int(current.get("generation") or 0)
                != int(record.get("generation") or 0)
                or float(current.get("expires_at_epoch_seconds") or 0) <= now
            ):
                raise ExecutionResourceOwnershipLost(
                    f"execution resource ownership was lost: {resource}",
                )

    def release(
        self,
        records: list[dict[str, Any]],
        *,
        now_epoch_seconds: float | None = None,
    ) -> None:
        if not records:
            return
        now = time.time() if now_epoch_seconds is None else now_epoch_seconds
        with self._writer_lock():
            leases = self._load_unlocked()
            self._assert_records_owned(leases, records, now)
            for record in records:
                leases.pop(
                    _normalized_resource_claim(record["resource"]),
                    None,
                )
            self._save_unlocked(leases)


class ExecutionTeamStore:
    """Checksum-protected, revision-fenced execution-team snapshots."""

    def __init__(self, state_root: Path) -> None:
        self.root = (
            state_root.expanduser().resolve(strict=False)
            / ".lantai"
            / "runtime"
            / "v1"
            / "execution-teams"
        )

    def snapshot_path(self, team_id: str) -> Path:
        key = safe_storage_key("execution-team", team_id)
        return contained_storage_path(self.root, key, "snapshot.json")

    def lock_path(self, team_id: str) -> Path:
        key = safe_storage_key("execution-team", team_id)
        return contained_storage_path(self.root, key, "snapshot.lock")

    @contextmanager
    def _writer_lock(self, team_id: str) -> Iterator[None]:
        """Serialize revision checks and writes across threads/processes."""

        path = self.lock_path(team_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        process_lock = _process_writer_lock(path)
        with process_lock:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            try:
                with locked_file_descriptor(descriptor):
                    yield
            finally:
                os.close(descriptor)

    @staticmethod
    def _checksum(
        team_id: str,
        revision: int,
        snapshot: dict[str, Any],
    ) -> str:
        canonical = json.dumps(
            {
                "team_id": team_id,
                "store_revision": revision,
                "snapshot": snapshot,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def load(self, team_id: str) -> dict[str, Any] | None:
        path = self.snapshot_path(team_id)
        if not path.exists():
            return None
        if path.is_symlink():
            raise ValueError("execution snapshot path is a symlink")
        envelope = json.loads(read_text_no_follow(path))
        revision = int(envelope.get("store_revision") or 0)
        snapshot = envelope.get("snapshot")
        if (
            envelope.get("schema_version") != 1
            or envelope.get("team_id") != team_id
            or revision < 1
            or not isinstance(snapshot, dict)
        ):
            raise ValueError("execution snapshot envelope is invalid")
        expected = self._checksum(team_id, revision, snapshot)
        if envelope.get("checksum") != expected:
            raise ValueError("execution snapshot checksum mismatch")
        return envelope

    def save(
        self,
        team_id: str,
        snapshot: dict[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self._writer_lock(team_id):
            current = self.load(team_id)
            current_revision = (
                int(current.get("store_revision") or 0) if current else 0
            )
            if current_revision != expected_revision:
                raise ValueError(
                    "execution snapshot revision mismatch: "
                    f"expected {expected_revision}, found {current_revision}",
                )
            revision = current_revision + 1
            safe_snapshot = json.loads(
                json.dumps(snapshot, ensure_ascii=False),
            )
            envelope = {
                "schema_version": 1,
                "team_id": team_id,
                "store_revision": revision,
                "snapshot": safe_snapshot,
            }
            envelope["checksum"] = self._checksum(
                team_id,
                revision,
                safe_snapshot,
            )
            path = self.snapshot_path(team_id)
            write_text_atomic_no_follow(
                path,
                json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            )
            return envelope
