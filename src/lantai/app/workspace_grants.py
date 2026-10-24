"""Persistent authorization for user-selected project workspaces.

Paths describe resources; a :class:`WorkspaceGrant` is the authority to use
one.  Desktop selection receipts are deliberately short lived and single use.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from lantai.constant import WORKING_DIR


AccessMode = Literal["read_only", "read_write"]
TrustState = Literal["trusted", "restricted"]
GrantOperation = Literal["read", "write"]

_RECEIPT_MAX_AGE = timedelta(minutes=5)
_FUTURE_CLOCK_SKEW = timedelta(minutes=1)
_STORE_VERSION = 1
_MAX_CONSUMED_RECEIPTS = 512


class WorkspaceGrantError(ValueError):
    """A stable, user-safe Workspace Grant failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkspaceGrant:
    id: str
    canonical_path: str
    display_name: str
    agent_ids: tuple[str, ...]
    access_mode: AccessMode
    trust_state: TrustState
    status: Literal["active", "revoked"]
    distribution: str
    security_scope_ref: str | None
    device: int
    inode: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkspaceBinding:
    grant_id: str
    project_root: Path
    access_mode: AccessMode
    trust_state: TrustState
    distribution: str
    security_scope_ref: str | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise WorkspaceGrantError(
            "workspace_selection_receipt_invalid",
            "The workspace selection receipt is invalid.",
        ) from exc


def create_workspace_selection_receipt(
    *,
    project_root: Path | str,
    agent_id: str,
    desktop_token: str,
    issued_at: datetime | None = None,
    nonce: str | None = None,
    access_mode: AccessMode = "read_write",
    trust_state: TrustState = "trusted",
    distribution: str = "official",
    security_scope_ref: str | None = None,
) -> str:
    """Create the receipt format shared with the trusted Electron main process."""

    if not desktop_token:
        raise WorkspaceGrantError(
            "desktop_auth_unavailable",
            "Desktop authentication is unavailable.",
        )
    payload = {
        "access_mode": access_mode,
        "agent_id": agent_id,
        "distribution": distribution,
        "issued_at": _as_utc(issued_at or _utc_now()).isoformat(),
        "nonce": nonce or secrets.token_urlsafe(18),
        "project_root": str(Path(project_root).expanduser().resolve()),
        "security_scope_ref": security_scope_ref,
        "trust_state": trust_state,
    }
    payload_segment = _b64url_encode(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    )
    signature = hmac.new(
        desktop_token.encode("utf-8"),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_segment}.{signature}"


class WorkspaceGrantRegistry:
    """A small, atomic JSON registry kept under application-managed state."""

    def __init__(
        self,
        store_path: Path | str,
        *,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store_path = Path(store_path).expanduser()
        self._clock = clock
        self._id_factory = id_factory or (
            lambda: f"wsg_{secrets.token_urlsafe(18)}"
        )
        self._lock = threading.RLock()

    def claim_selection(
        self,
        receipt: str,
        *,
        desktop_token: str,
        agent_id: str,
    ) -> WorkspaceGrant:
        payload = self._verify_receipt(receipt, desktop_token=desktop_token)
        if payload.get("agent_id") != agent_id:
            raise WorkspaceGrantError(
                "workspace_grant_agent_mismatch",
                "The selected workspace belongs to another agent.",
            )

        access_mode = payload.get("access_mode")
        trust_state = payload.get("trust_state")
        if access_mode not in {"read_only", "read_write"}:
            raise WorkspaceGrantError(
                "workspace_grant_access_mode_invalid",
                "The workspace access mode is invalid.",
            )
        if trust_state not in {"trusted", "restricted"}:
            raise WorkspaceGrantError(
                "workspace_grant_trust_state_invalid",
                "The workspace trust state is invalid.",
            )

        project_root = Path(str(payload.get("project_root") or "")).expanduser()
        if not project_root.is_absolute():
            raise WorkspaceGrantError(
                "workspace_path_invalid",
                "The selected workspace path is invalid.",
            )
        project_root = project_root.resolve(strict=True)
        if not project_root.is_dir():
            raise WorkspaceGrantError(
                "workspace_not_directory",
                "The selected workspace is not a directory.",
            )
        stat = project_root.stat()
        now_text = _as_utc(self._clock()).isoformat()
        distribution = str(payload.get("distribution") or "official")
        security_scope_ref = payload.get("security_scope_ref")
        if distribution in {"mas", "app_store"} and not security_scope_ref:
            raise WorkspaceGrantError(
                "mas_security_scoped_bookmark_missing",
                "The selected folder did not provide a security-scoped bookmark.",
            )

        nonce = str(payload["nonce"])
        with self._lock:
            store = self._load()
            if nonce in store["consumed_receipts"]:
                raise WorkspaceGrantError(
                    "workspace_selection_receipt_replayed",
                    "This workspace selection has already been used.",
                )
            existing = self._find_active_grant(
                store,
                canonical_path=str(project_root),
                agent_id=agent_id,
            )
            if existing is None:
                grant = WorkspaceGrant(
                    id=self._id_factory(),
                    canonical_path=str(project_root),
                    display_name=project_root.name or str(project_root),
                    agent_ids=(agent_id,),
                    access_mode=access_mode,
                    trust_state=trust_state,
                    status="active",
                    distribution=distribution,
                    security_scope_ref=(
                        str(security_scope_ref) if security_scope_ref else None
                    ),
                    device=stat.st_dev,
                    inode=stat.st_ino,
                    created_at=now_text,
                    updated_at=now_text,
                )
                store["grants"].append(self._grant_to_json(grant))
            else:
                grant = WorkspaceGrant(
                    **{
                        **asdict(existing),
                        "access_mode": access_mode,
                        "trust_state": trust_state,
                        "distribution": distribution,
                        "security_scope_ref": (
                            str(security_scope_ref)
                            if security_scope_ref
                            else existing.security_scope_ref
                        ),
                        "device": stat.st_dev,
                        "inode": stat.st_ino,
                        "updated_at": now_text,
                    },
                )
                store["grants"] = [
                    self._grant_to_json(grant)
                    if item.get("id") == grant.id
                    else item
                    for item in store["grants"]
                ]
            store["consumed_receipts"].append(nonce)
            store["consumed_receipts"] = store["consumed_receipts"][
                -_MAX_CONSUMED_RECEIPTS:
            ]
            self._save(store)
            return grant

    def resolve(
        self,
        grant_id: str,
        *,
        agent_id: str,
        operation: GrantOperation = "read",
    ) -> WorkspaceBinding:
        with self._lock:
            grant = next(
                (item for item in self._grants() if item.id == grant_id),
                None,
            )
        if grant is None or grant.status != "active":
            raise WorkspaceGrantError(
                "workspace_grant_not_found",
                "The workspace authorization is unavailable.",
            )
        if agent_id not in grant.agent_ids:
            raise WorkspaceGrantError(
                "workspace_grant_agent_mismatch",
                "The workspace authorization belongs to another agent.",
            )
        if operation == "write" and grant.access_mode != "read_write":
            raise WorkspaceGrantError(
                "workspace_grant_read_only",
                "This workspace authorization is read-only.",
            )

        project_root = Path(grant.canonical_path)
        try:
            resolved = project_root.resolve(strict=True)
            stat = resolved.stat()
        except OSError as exc:
            raise WorkspaceGrantError(
                "workspace_grant_path_unavailable",
                "The authorized workspace is no longer available.",
            ) from exc
        if (
            not resolved.is_dir()
            or stat.st_dev != grant.device
            or stat.st_ino != grant.inode
        ):
            raise WorkspaceGrantError(
                "workspace_grant_identity_changed",
                "The authorized workspace has changed and must be selected again.",
            )
        return WorkspaceBinding(
            grant_id=grant.id,
            project_root=resolved,
            access_mode=grant.access_mode,
            trust_state=grant.trust_state,
            distribution=grant.distribution,
            security_scope_ref=grant.security_scope_ref,
        )

    def list(self, *, agent_id: str) -> list[WorkspaceGrant]:
        with self._lock:
            return [
                grant
                for grant in self._grants()
                if grant.status == "active" and agent_id in grant.agent_ids
            ]

    def revoke(self, grant_id: str, *, agent_id: str) -> WorkspaceGrant:
        with self._lock:
            store = self._load()
            grant = next(
                (
                    self._grant_from_json(item)
                    for item in store["grants"]
                    if item.get("id") == grant_id
                ),
                None,
            )
            if grant is None or agent_id not in grant.agent_ids:
                raise WorkspaceGrantError(
                    "workspace_grant_not_found",
                    "The workspace authorization is unavailable.",
                )
            revoked = WorkspaceGrant(
                **{
                    **asdict(grant),
                    "status": "revoked",
                    "updated_at": _as_utc(self._clock()).isoformat(),
                },
            )
            store["grants"] = [
                self._grant_to_json(revoked)
                if item.get("id") == grant_id
                else item
                for item in store["grants"]
            ]
            self._save(store)
            return revoked

    def _verify_receipt(
        self,
        receipt: str,
        *,
        desktop_token: str,
    ) -> dict[str, object]:
        if not desktop_token:
            raise WorkspaceGrantError(
                "desktop_auth_unavailable",
                "Desktop authentication is unavailable.",
            )
        try:
            payload_segment, signature = receipt.split(".", 1)
        except ValueError as exc:
            raise WorkspaceGrantError(
                "workspace_selection_receipt_invalid",
                "The workspace selection receipt is invalid.",
            ) from exc
        expected = hmac.new(
            desktop_token.encode("utf-8"),
            payload_segment.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise WorkspaceGrantError(
                "workspace_selection_receipt_invalid",
                "The workspace selection receipt is invalid.",
            )
        try:
            payload = json.loads(_b64url_decode(payload_segment))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WorkspaceGrantError(
                "workspace_selection_receipt_invalid",
                "The workspace selection receipt is invalid.",
            ) from exc
        if not isinstance(payload, dict) or not payload.get("nonce"):
            raise WorkspaceGrantError(
                "workspace_selection_receipt_invalid",
                "The workspace selection receipt is invalid.",
            )
        try:
            issued_at = _as_utc(datetime.fromisoformat(str(payload["issued_at"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceGrantError(
                "workspace_selection_receipt_invalid",
                "The workspace selection receipt is invalid.",
            ) from exc
        now = _as_utc(self._clock())
        if issued_at > now + _FUTURE_CLOCK_SKEW or now - issued_at > _RECEIPT_MAX_AGE:
            raise WorkspaceGrantError(
                "workspace_selection_receipt_expired",
                "The workspace selection has expired. Select the folder again.",
            )
        return payload

    def _find_active_grant(
        self,
        store: dict[str, object],
        *,
        canonical_path: str,
        agent_id: str,
    ) -> WorkspaceGrant | None:
        for item in store["grants"]:  # type: ignore[index]
            grant = self._grant_from_json(item)
            if (
                grant.status == "active"
                and grant.canonical_path == canonical_path
                and agent_id in grant.agent_ids
            ):
                return grant
        return None

    def _grants(self) -> list[WorkspaceGrant]:
        return [
            self._grant_from_json(item)
            for item in self._load()["grants"]
        ]

    @staticmethod
    def _grant_to_json(grant: WorkspaceGrant) -> dict[str, object]:
        value = asdict(grant)
        value["agent_ids"] = list(grant.agent_ids)
        return value

    @staticmethod
    def _grant_from_json(value: dict[str, object]) -> WorkspaceGrant:
        return WorkspaceGrant(
            id=str(value["id"]),
            canonical_path=str(value["canonical_path"]),
            display_name=str(value["display_name"]),
            agent_ids=tuple(str(item) for item in value.get("agent_ids", [])),
            access_mode=str(value["access_mode"]),  # type: ignore[arg-type]
            trust_state=str(value["trust_state"]),  # type: ignore[arg-type]
            status=str(value["status"]),  # type: ignore[arg-type]
            distribution=str(value.get("distribution") or "official"),
            security_scope_ref=(
                str(value["security_scope_ref"])
                if value.get("security_scope_ref")
                else None
            ),
            device=int(value["device"]),
            inode=int(value["inode"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
        )

    def _load(self) -> dict[str, object]:
        if not self._store_path.exists():
            return {
                "version": _STORE_VERSION,
                "grants": [],
                "consumed_receipts": [],
            }
        try:
            value = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceGrantError(
                "workspace_grant_store_invalid",
                "The workspace authorization store is unreadable.",
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("version") != _STORE_VERSION
            or not isinstance(value.get("grants"), list)
            or not isinstance(value.get("consumed_receipts"), list)
        ):
            raise WorkspaceGrantError(
                "workspace_grant_store_invalid",
                "The workspace authorization store is invalid.",
            )
        return value

    def _save(self, value: dict[str, object]) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._store_path.with_suffix(
            f"{self._store_path.suffix}.{secrets.token_hex(6)}.tmp",
        )
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._store_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class WorkspaceGrantClaimRequest(BaseModel):
    selection_receipt: str = Field(min_length=16)


class WorkspaceGrantResponse(BaseModel):
    id: str
    display_name: str
    project_root: str
    access_mode: AccessMode
    trust_state: TrustState
    status: Literal["active", "revoked"]


def workspace_grant_http_exception(error: WorkspaceGrantError) -> HTTPException:
    if error.code == "desktop_auth_unavailable":
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif error.code in {
        "workspace_selection_receipt_invalid",
        "workspace_selection_receipt_expired",
    }:
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "workspace_selection_receipt_replayed":
        status_code = status.HTTP_409_CONFLICT
    elif error.code in {
        "workspace_grant_agent_mismatch",
        "workspace_grant_read_only",
    }:
        status_code = status.HTTP_403_FORBIDDEN
    elif error.code in {
        "workspace_grant_not_found",
        "workspace_grant_path_unavailable",
    }:
        status_code = status.HTTP_404_NOT_FOUND
    else:
        status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": str(error)},
    )


def create_workspace_grants_router(
    *,
    registry: WorkspaceGrantRegistry | None = None,
    desktop_token_provider: Callable[[], str] | None = None,
) -> APIRouter:
    """Build the API router with injectable persistence for contract tests."""

    grant_registry = registry or WorkspaceGrantRegistry(
        WORKING_DIR / "workspace-grants.json",
    )
    token_provider = desktop_token_provider or (
        lambda: os.getenv("LANTAI_DESKTOP_TOKEN", "")
    )
    api = APIRouter(prefix="/workspace-grants", tags=["workspace-grants"])

    def response_for(grant: WorkspaceGrant) -> WorkspaceGrantResponse:
        return WorkspaceGrantResponse(
            id=grant.id,
            display_name=grant.display_name,
            project_root=grant.canonical_path,
            access_mode=grant.access_mode,
            trust_state=grant.trust_state,
            status=grant.status,
        )

    @api.post(
        "/claim",
        response_model=WorkspaceGrantResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def claim_workspace_grant(
        request: WorkspaceGrantClaimRequest,
        agent_id: str = Header(default="default", alias="X-Agent-Id"),
    ) -> WorkspaceGrantResponse:
        try:
            grant = grant_registry.claim_selection(
                request.selection_receipt,
                desktop_token=token_provider(),
                agent_id=agent_id,
            )
        except WorkspaceGrantError as error:
            raise workspace_grant_http_exception(error) from error
        return response_for(grant)

    @api.get("", response_model=list[WorkspaceGrantResponse])
    async def list_workspace_grants(
        agent_id: str = Header(default="default", alias="X-Agent-Id"),
    ) -> list[WorkspaceGrantResponse]:
        return [
            response_for(grant)
            for grant in grant_registry.list(agent_id=agent_id)
        ]

    @api.delete("/{grant_id}", response_model=WorkspaceGrantResponse)
    async def revoke_workspace_grant(
        grant_id: str,
        agent_id: str = Header(default="default", alias="X-Agent-Id"),
    ) -> WorkspaceGrantResponse:
        try:
            grant = grant_registry.revoke(grant_id, agent_id=agent_id)
        except WorkspaceGrantError as error:
            raise workspace_grant_http_exception(error) from error
        return response_for(grant)

    return api


default_workspace_grant_registry = WorkspaceGrantRegistry(
    WORKING_DIR / "workspace-grants.json",
)
workspace_grants_router = create_workspace_grants_router(
    registry=default_workspace_grant_registry,
)
