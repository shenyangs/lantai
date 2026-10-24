"""Identity-bound authorization for every TokenDance credential use.

The encrypted provider file remains the only owner of the API key.  This
module resolves that secret together with the current trusted Lantai subject
and the persisted binding metadata, so callers cannot accidentally treat a
machine-global key as belonging to whichever user logged in most recently.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..security.cloud_account_store import load_watcha_account_context

TOKENDANCE_BINDING_META_KEY = "tokendance_credential_binding"
TOKENDANCE_PAYMENT_SESSIONS_META_KEY = "tokendance_payment_sessions"
TOKENDANCE_PAYMENT_INTENTS_META_KEY = "tokendance_payment_intents"
TOKENDANCE_BINDING_SCHEMA_VERSION = 1


class TokenDanceAuthorizationError(RuntimeError):
    """A sanitized local authorization failure safe for an API response."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class TokenDanceAuthorization:
    """Immutable snapshot captured before an external TokenDance request."""

    owner_subject_id: str
    account_epoch: int
    credential_ref: str
    payment_binding_id: str
    binding_revision: int
    use_kind: str
    api_key: str = field(repr=False)

    @property
    def context_id(self) -> str:
        """Opaque UI correlation id for one account/credential revision.

        The id deliberately excludes ``use_kind`` so balance, usage and
        payment responses from the same authorization context can be joined.
        It is never accepted as authorization input by the backend.
        """

        material = "\x1f".join(
            (
                self.owner_subject_id,
                str(self.account_epoch),
                self.credential_ref,
                self.payment_binding_id,
                str(self.binding_revision),
            ),
        ).encode("utf-8")
        return f"tdctx_v1_{hashlib.sha256(material).hexdigest()[:32]}"


def _trusted_watcha_context() -> tuple[str, int]:
    context = load_watcha_account_context()
    if context.subject_id is None:
        raise TokenDanceAuthorizationError(
            "watcha_account_required",
            "请先登录观猹账号，再使用词元跳动服务。",
            status_code=403,
        )
    return context.subject_id, context.account_epoch


def resolve_tokendance_authorization(
    manager: Any,
    *,
    use_kind: str,
) -> TokenDanceAuthorization:
    """Resolve one authorized use without exposing the key to the caller UI."""

    owner_subject_id, account_epoch = _trusted_watcha_context()
    provider = manager.get_provider("tokendance")
    if provider is None:
        raise TokenDanceAuthorizationError(
            "tokendance_provider_unavailable",
            "词元跳动模型服务不可用。",
            status_code=404,
        )
    api_key = str(getattr(provider, "api_key", "") or "").strip()
    if not api_key:
        raise TokenDanceAuthorizationError(
            "tokendance_credential_missing",
            "请先连接词元跳动账号，再使用余额、充值或模型服务。",
        )

    meta = getattr(provider, "meta", None)
    binding = (
        meta.get(TOKENDANCE_BINDING_META_KEY)
        if isinstance(meta, dict)
        else None
    )
    if not isinstance(binding, dict):
        raise TokenDanceAuthorizationError(
            "tokendance_credential_reauthorization_required",
            "本机已有的词元跳动密钥尚未确认归属，请重新授权。",
        )
    if binding.get("schema_version") != TOKENDANCE_BINDING_SCHEMA_VERSION:
        raise TokenDanceAuthorizationError(
            "tokendance_binding_version_unsupported",
            "词元跳动账号绑定版本无法识别，请重新授权。",
        )
    if binding.get("status") != "active":
        raise TokenDanceAuthorizationError(
            "tokendance_credential_reauthorization_required",
            "词元跳动账号绑定尚未生效，请重新授权。",
        )
    if binding.get("owner_subject_id") != owner_subject_id:
        raise TokenDanceAuthorizationError(
            "credential_owner_mismatch",
            "当前观猹账号不能使用另一账号绑定的词元跳动授权。",
            status_code=403,
        )
    if binding.get("account_epoch_at_binding") != account_epoch:
        raise TokenDanceAuthorizationError(
            "tokendance_credential_reauthorization_required",
            "观猹登录会话已变化，请重新授权词元跳动账号。",
        )

    scopes = binding.get("scopes")
    if not isinstance(scopes, list) or use_kind not in scopes:
        raise TokenDanceAuthorizationError(
            "tokendance_scope_missing",
            "当前词元跳动授权不包含这项用途，请重新授权。",
            status_code=403,
        )
    credential_ref = str(binding.get("credential_ref") or "").strip()
    payment_binding_id = str(binding.get("payment_binding_id") or "").strip()
    revision = binding.get("binding_revision")
    if (
        not credential_ref
        or not payment_binding_id
        or not isinstance(revision, int)
        or revision < 1
    ):
        raise TokenDanceAuthorizationError(
            "tokendance_binding_incomplete",
            "词元跳动账号绑定不完整，请重新授权。",
        )

    return TokenDanceAuthorization(
        owner_subject_id=owner_subject_id,
        account_epoch=account_epoch,
        credential_ref=credential_ref,
        payment_binding_id=payment_binding_id,
        binding_revision=revision,
        use_kind=use_kind,
        api_key=api_key,
    )


def assert_tokendance_authorization_current(
    manager: Any,
    authorization: TokenDanceAuthorization,
) -> None:
    """Reject an external response whose authorization snapshot went stale.

    External account and payment requests can finish after the user has
    switched Watcha accounts or re-bound the TokenDance key.  Callers must run
    this check after every await and before returning or persisting the result.
    """

    try:
        current = resolve_tokendance_authorization(
            manager,
            use_kind=authorization.use_kind,
        )
    except TokenDanceAuthorizationError as exc:
        raise TokenDanceAuthorizationError(
            "authorization_context_stale",
            "账号或词元跳动授权已变化，本次返回结果已丢弃，请重试。",
            status_code=409,
        ) from exc

    expected = (
        authorization.owner_subject_id,
        authorization.account_epoch,
        authorization.credential_ref,
        authorization.payment_binding_id,
        authorization.binding_revision,
        authorization.use_kind,
    )
    actual = (
        current.owner_subject_id,
        current.account_epoch,
        current.credential_ref,
        current.payment_binding_id,
        current.binding_revision,
        current.use_kind,
    )
    if actual != expected:
        raise TokenDanceAuthorizationError(
            "authorization_context_stale",
            "账号或词元跳动授权已变化，本次返回结果已丢弃，请重试。",
            status_code=409,
        )
