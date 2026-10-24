"""Application service joining the existing account, beta API, and local store."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from ...security.beta_access import AccessSnapshot, BetaAccessStatus
from ...security.beta_access_store import LicenseCandidateRejected
from ..cloud_account import CloudAccountError
from .account import BetaMemberSession
from .client import BetaAccessClientError
from .refresh import BetaAccountContext, RefreshOutcome


class BetaAccessApplicationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class BetaAccessApplicationService:
    """Expose sanitized beta operations without returning license material to UI."""

    def __init__(self, *, account_service: Any, session_loader: Callable[[], BetaMemberSession], client: Any, store: Any, refresh: Any) -> None:
        self.account_service = account_service
        self.session_loader = session_loader
        self.client = client
        self.store = store
        self.refresh = refresh

    def local_status(self, *, schedule_refresh: bool = True) -> dict[str, Any]:
        session = self.session_loader()
        snapshot = self.store.snapshot(
            account_id=session.context.account_id,
            account_epoch=session.context.account_epoch,
        )
        outcome = RefreshOutcome.NOT_DUE
        if schedule_refresh:
            outcome = self.refresh.maybe_refresh(snapshot)
        return self._public_snapshot(snapshot, refresh_scheduled=outcome is RefreshOutcome.STARTED)

    async def activate(self, *, code: str, request_id: str | None = None, device_label: str | None = None) -> dict[str, Any]:
        if not isinstance(code, str) or not code:
            raise BetaAccessApplicationError("invalid_invite", "请输入内测邀请码。")
        selected = await self._activation_session()
        expected_subject = selected.context.account_id or self.store.installation_subject()
        try:
            response = await self.client.activate(
                code=code,
                request_id=request_id or str(uuid4()),
                access_token=selected.access_token or "",
                device_label=device_label,
            )
        except BetaAccessClientError as exc:
            raise BetaAccessApplicationError(exc.code, str(exc), status_code=exc.status_code) from exc
        return self._accept_issuance(response, selected, expected_subject=expected_subject)

    async def issue(self, *, request_id: str | None = None, device_label: str | None = None) -> dict[str, Any]:
        selected = await self._authenticated_session()
        try:
            response = await self.client.issue(
                request_id=request_id or str(uuid4()),
                access_token=selected.access_token,
                device_label=device_label,
            )
        except BetaAccessClientError as exc:
            raise BetaAccessApplicationError(exc.code, str(exc), status_code=exc.status_code) from exc
        return self._accept_issuance(response, selected, expected_subject=selected.context.account_id)

    async def recover(self, *, operation_id: str, recovery_token: str | None = None) -> dict[str, Any]:
        if not isinstance(operation_id, str) or not operation_id:
            raise BetaAccessApplicationError("invalid_operation", "恢复标识无效。")
        if recovery_token is not None and (not isinstance(recovery_token, str) or not recovery_token):
            raise BetaAccessApplicationError("invalid_operation", "恢复凭证无效。")
        if recovery_token:
            selected = BetaMemberSession(BetaAccountContext(account_id=None, account_epoch=0), None)
            expected_subject = self.store.installation_subject()
        else:
            selected = await self._authenticated_session()
            expected_subject = selected.context.account_id
        try:
            response = await self.client.operation_status(
                operation_id=operation_id,
                access_token=selected.access_token or "",
                recovery_token=recovery_token,
            )
        except BetaAccessClientError as exc:
            raise BetaAccessApplicationError(exc.code, str(exc), status_code=exc.status_code) from exc
        return self._accept_issuance(response, selected, expected_subject=expected_subject)

    async def _activation_session(self) -> BetaMemberSession:
        initial = self.session_loader()
        if not initial.context.account_id or not initial.access_token:
            return BetaMemberSession(BetaAccountContext(account_id=None, account_epoch=0), None)
        return await self._authenticated_session()

    async def _authenticated_session(self) -> BetaMemberSession:
        # Existing CloudAccountService.status refreshes the short-lived member
        # bearer when needed. Reload only after that call finishes.
        try:
            await self.account_service.status()
        except CloudAccountError as exc:
            raise BetaAccessApplicationError(
                exc.code,
                "澜台账号暂时无法确认，请稍后再试。",
                status_code=exc.status_code,
            ) from exc
        session = self.session_loader()
        if not session.context.account_id or not session.access_token:
            raise BetaAccessApplicationError("login_required", "请先登录当前澜台账号。", status_code=401)
        return session

    def _accept_issuance(
        self,
        response: Any,
        selected: BetaMemberSession,
        *,
        expected_subject: str | None,
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise BetaAccessApplicationError("invalid_response", "内测访问服务返回了无法识别的响应。")
        operation_id = response.get("operationId")
        if response.get("status") == "issuance_pending":
            if not isinstance(operation_id, str) or not operation_id:
                raise BetaAccessApplicationError("invalid_response", "内测访问服务未返回恢复标识。")
            result = {"status": "issuance_pending", "operation_id": operation_id}
            if selected.context.account_id is None:
                recovery_token = response.get("recoveryToken")
                if not isinstance(recovery_token, str) or not recovery_token:
                    raise BetaAccessApplicationError("invalid_response", "内测访问服务未返回匿名恢复凭证。")
                result["recovery_token"] = recovery_token
            return result
        token = response.get("license")
        if response.get("status") != "issued" or not isinstance(token, str) or not token:
            raise BetaAccessApplicationError("invalid_response", "内测访问服务未返回可验签的许可证。")
        current = self.session_loader()
        if selected.context.account_id is not None and current.context != selected.context:
            raise BetaAccessApplicationError("account_changed", "账号已经切换，旧请求结果未写入。")
        try:
            snapshot = self.store.install_candidate(
                token,
                account_id=selected.context.account_id,
                account_epoch=selected.context.account_epoch,
                subject=expected_subject,
            )
        except LicenseCandidateRejected as exc:
            raise BetaAccessApplicationError("license_rejected", "许可证本地验签失败，旧许可证已保留。") from exc
        result = self._public_snapshot(snapshot, refresh_scheduled=False)
        if isinstance(operation_id, str) and operation_id:
            result["operation_id"] = operation_id
        return result

    @staticmethod
    def _public_snapshot(snapshot: AccessSnapshot, *, refresh_scheduled: bool) -> dict[str, Any]:
        return {
            "enforcement_required": snapshot.enforcement_required,
            "status": snapshot.status.value,
            "decision_seq": snapshot.decision_seq,
            "snapshot_revision": snapshot.snapshot_revision,
            "refresh_due": snapshot.refresh_due,
            "refresh_scheduled": refresh_scheduled,
            "renew_by": snapshot.renew_by,
            "expires_at": snapshot.expires_at,
        }
