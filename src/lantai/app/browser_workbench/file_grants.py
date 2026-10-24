# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from secrets import token_urlsafe
from typing import Any, Literal

from ..capabilities.policy import is_app_store_distribution
from .models import BrowserAction
from ...runtime.sanitizer import sanitize_runtime_payload


@dataclass(frozen=True)
class BrowserFileGrant:
    id: str
    grant_ref: str
    security_scope_ref: str
    security_scoped_bookmark: bool
    security_scope_bookmark_sha256: str
    path_identity_hash: str
    path: str
    profile_id: str
    run_id: str = ""
    page_id: str = ""
    url: str = ""
    selector: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class BrowserFileGrantError:
    reason: str
    code: str


class BrowserFileGrantService:
    """Short-lived one-time upload grants for Browser Workbench file inputs."""

    def __init__(self, *, default_ttl_seconds: int = 600) -> None:
        self.default_ttl_seconds = max(1, int(default_ttl_seconds or 600))
        self._grants: dict[str, BrowserFileGrant] = {}

    def issue(
        self,
        *,
        path: str | Path,
        profile_id: str,
        run_id: str = "",
        page_id: str = "",
        url: str = "",
        selector: str = "",
        ttl_seconds: int | None = None,
        security_scoped_bookmark: bool = False,
        security_scope_ref: str = "",
        security_scope_bookmark_sha256: str = "",
    ) -> BrowserFileGrant:
        if is_app_store_distribution() and not security_scoped_bookmark:
            raise ValueError(
                "App Store browser file grants require a restored security-scoped bookmark.",
            )
        resolved = self._resolve_upload_path(path)
        now = datetime.now(timezone.utc)
        grant_id = token_urlsafe(32)
        path_identity_hash = self._path_identity_hash(resolved)
        normalized_scope_ref = str(security_scope_ref or "").strip()
        if not normalized_scope_ref:
            normalized_scope_ref = self._security_scope_ref(
                resolved,
                path_identity_hash=path_identity_hash,
            )
        grant = BrowserFileGrant(
            id=grant_id,
            grant_ref=f"browser-file-grant:{grant_id}",
            security_scope_ref=normalized_scope_ref,
            security_scoped_bookmark=bool(security_scoped_bookmark),
            security_scope_bookmark_sha256=str(security_scope_bookmark_sha256 or ""),
            path_identity_hash=path_identity_hash,
            path=str(resolved),
            profile_id=str(profile_id or "default"),
            run_id=str(run_id or ""),
            page_id=str(page_id or ""),
            url=str(url or ""),
            selector=str(selector or ""),
            created_at=now,
            expires_at=now + timedelta(seconds=max(1, int(ttl_seconds or self.default_ttl_seconds))),
        )
        self._grants[grant.id] = grant
        return grant

    def validate(
        self,
        grant_id: str,
        action: BrowserAction,
    ) -> BrowserFileGrantError | None:
        grant = self._grants.get(str(grant_id or ""))
        if grant is None:
            return BrowserFileGrantError(
                "Browser file upload grant is unknown or already consumed.",
                "browser_file_grant_invalid",
            )
        expired = datetime.now(timezone.utc) >= grant.expires_at
        if expired:
            return BrowserFileGrantError(
                "Browser file upload grant has expired.",
                "browser_file_grant_expired",
            )
        return self._binding_error(grant, action)

    def consume(
        self,
        grant_id: str,
        action: BrowserAction,
    ) -> BrowserFileGrantError | None:
        error = self.validate(grant_id, action)
        if error is not None:
            return error
        self._grants.pop(str(grant_id or ""), None)
        return None

    def lifecycle_report(
        self,
        *,
        operation: Literal["archive", "delete"] = "archive",
        dry_run: bool = True,
        run_id: str = "",
        profile_id: str = "",
    ) -> dict[str, Any]:
        """Report and optionally revoke grants matching a run/profile scope."""

        if operation not in {"archive", "delete"}:
            raise ValueError(f"unsupported file grant lifecycle operation: {operation}")
        run_scope = str(run_id or "").strip()
        profile_scope = str(profile_id or "").strip()
        if operation == "delete" and not dry_run and not (run_scope or profile_scope):
            raise ValueError(
                "destructive browser file grant lifecycle requires run_id or profile_id scope",
            )
        grants = list(self._grants.values())
        matching = [
            grant
            for grant in grants
            if (not run_scope or grant.run_id == run_scope)
            and (not profile_scope or grant.profile_id == profile_scope)
        ]
        summaries = [self._grant_summary(grant) for grant in matching]
        revoked = 0
        if operation == "delete" and not dry_run:
            for grant in matching:
                if self._grants.pop(grant.id, None) is not None:
                    revoked += 1
        return {
            "kind": "browser_file_grant_lifecycle",
            "operation": operation,
            "dry_run": bool(dry_run),
            "run_id": run_scope,
            "profile_id": profile_scope,
            "matched_count": len(matching),
            "revoked_count": revoked,
            "remaining_count": len(self._grants),
            "grants": summaries,
        }

    @staticmethod
    def _resolve_upload_path(path: str | Path) -> Path:
        original = Path(path).expanduser()
        if original.is_symlink():
            raise ValueError("Browser file upload grant must reference a regular file.")
        resolved = original.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("Browser file upload grant must reference a regular file.")
        return resolved

    @staticmethod
    def _binding_error(
        grant: BrowserFileGrant,
        action: BrowserAction,
    ) -> BrowserFileGrantError | None:
        try:
            original_action_path = Path(action.path).expanduser()
            if original_action_path.is_symlink():
                raise ValueError("symlink upload path")
            action_path = original_action_path.resolve(strict=True)
        except Exception:
            return BrowserFileGrantError(
                "Browser file upload path does not match the grant.",
                "browser_file_grant_path_mismatch",
            )
        if str(action_path) != grant.path:
            return BrowserFileGrantError(
                "Browser file upload path does not match the grant.",
                "browser_file_grant_path_mismatch",
            )
        if BrowserFileGrantService._path_identity_hash(action_path) != grant.path_identity_hash:
            return BrowserFileGrantError(
                "Browser file upload path identity no longer matches the grant.",
                "browser_file_grant_identity_mismatch",
            )
        checks = {
            "profile_id": (grant.profile_id, action.profile_id),
            "run_id": (grant.run_id, action.run_id),
            "page_id": (grant.page_id, action.page_id),
            "url": (grant.url, action.url),
            "selector": (grant.selector, action.selector),
        }
        for _name, (expected, actual) in checks.items():
            if expected and str(actual or "") != expected:
                return BrowserFileGrantError(
                    "Browser file upload grant is bound to a different action.",
                    "browser_file_grant_owner_mismatch",
                )
        return None

    @staticmethod
    def _path_identity_hash(path: Path) -> str:
        stat = path.stat()
        payload = "|".join(
            [
                str(path.resolve()),
                str(stat.st_dev),
                str(stat.st_ino),
                str(stat.st_size),
                str(stat.st_mtime_ns),
            ]
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _grant_summary(grant: BrowserFileGrant) -> dict[str, Any]:
        path_digest = hashlib.sha256(grant.path.encode("utf-8")).hexdigest()
        return sanitize_runtime_payload(
            {
                "id": grant.id,
                "grant_ref": grant.grant_ref,
                "security_scope_ref": grant.security_scope_ref,
                "security_scoped_bookmark_restored": grant.security_scoped_bookmark,
                "security_scope_bookmark_sha256": grant.security_scope_bookmark_sha256,
                "path_identity_hash": grant.path_identity_hash,
                "path_sha256": f"sha256:{path_digest}",
                "profile_id": grant.profile_id,
                "run_id": grant.run_id,
                "page_id": grant.page_id,
                "url": grant.url,
                "selector": grant.selector,
                "created_at": grant.created_at.isoformat(),
                "expires_at": grant.expires_at.isoformat(),
            },
        )

    @staticmethod
    def _security_scope_ref(
        path: Path,
        *,
        path_identity_hash: str,
    ) -> str:
        payload = f"{path.resolve()}|{path_identity_hash}"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"browser-security-scope:{digest}"
