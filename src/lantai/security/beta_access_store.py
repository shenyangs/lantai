"""Atomic local beta-license persistence with account and sequence fences."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ..constant import WORKING_DIR
from .beta_access import AccessSnapshot, BetaAccessStatus
from .beta_license import AccessDisposition, BetaLicenseVerifier, LicenseVerificationError
from .beta_status import BetaStatusVerifier


class LicenseCandidateRejected(ValueError):
    """A candidate license was not installed; the prior file is untouched."""


class DeviceIdentityLike(Protocol):
    jkt: str

    def prove_local_possession(self) -> bool: ...


class BetaLicenseStore:
    INSTALLATION_SUBJECT_PREFIX = "installation:"

    def __init__(
        self,
        *,
        verifier: BetaLicenseVerifier,
        device_identity: DeviceIdentityLike,
        root: Path | None = None,
        enforcement_required: bool = True,
    ) -> None:
        self.root = Path(root or (WORKING_DIR / "license"))
        self.license_path = self.root / "license.jws"
        self.state_path = self.root / "state.json"
        self.verifier = verifier
        self.device_identity = device_identity
        self.enforcement_required = enforcement_required

    def install_candidate(
        self,
        token: str,
        *,
        account_id: str | None,
        account_epoch: int,
        subject: str | None = None,
        now: datetime | None = None,
    ) -> AccessSnapshot:
        instant = now or datetime.now(timezone.utc)
        expected_subject = subject or account_id
        if not isinstance(expected_subject, str) or not expected_subject:
            raise LicenseCandidateRejected("candidate license subject is unavailable")
        if self._is_installation_subject(expected_subject):
            if expected_subject != self.installation_subject():
                raise LicenseCandidateRejected("candidate installation subject does not match this device")
            stored_account_id = None
            stored_account_epoch = 0
        else:
            if account_id != expected_subject:
                raise LicenseCandidateRejected("candidate account subject does not match the selected account")
            stored_account_id = account_id
            stored_account_epoch = account_epoch
        try:
            verified = self.verifier.verify(
                token,
                now=instant,
                expected_subject=expected_subject,
                expected_jkt=self.device_identity.jkt,
            )
        except LicenseVerificationError as exc:
            raise LicenseCandidateRejected(f"candidate license rejected: {exc}") from exc
        state = self._read_state()
        decision = self._subject_decision(state, expected_subject)
        highest = decision["highest_decision_seq"]
        revoked = decision["revoked_decision_seq"]
        sequence = verified.decision_seq
        if sequence < highest or sequence <= revoked:
            raise LicenseCandidateRejected("candidate decision is older than the trusted local decision")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if sequence == highest:
            accepted_digest = decision.get("accepted_license_digest")
            if isinstance(accepted_digest, str) and accepted_digest and accepted_digest != digest:
                raise LicenseCandidateRejected("candidate conflicts with the accepted decision sequence")
            if not accepted_digest and self._active_subject(state) == expected_subject and self.license_path.is_file():
                try:
                    current = self.license_path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise LicenseCandidateRejected("current license could not be compared") from exc
                if current != token:
                    raise LicenseCandidateRejected("candidate conflicts with the accepted decision sequence")
        subject_decisions = self._subject_decisions(state)
        subject_decisions[expected_subject] = {
            "highest_decision_seq": max(highest, sequence),
            "revoked_decision_seq": revoked,
            "accepted_license_digest": digest,
        }
        revision = self._nonnegative_int(state.get("snapshot_revision")) + 1
        next_state = {
            "version": 3,
            "active_subject": expected_subject,
            "active_account_id": stored_account_id,
            "account_epoch": stored_account_epoch,
            "highest_decision_seq": max(highest, sequence),
            "revoked_decision_seq": revoked,
            "subject_decisions": subject_decisions,
            "snapshot_revision": revision,
            "license_digest": digest,
            "updated_at": instant.astimezone(timezone.utc).isoformat(),
        }
        # Candidate verification is complete before either durable write.  The
        # public license moves first; a crash can leave an older state record,
        # which is safe and repaired on the next accepted refresh.
        self._atomic_write(self.license_path, token)
        self._atomic_write(self.state_path, json.dumps(next_state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return self.snapshot(account_id=account_id, account_epoch=account_epoch, now=instant)

    def installation_subject(self) -> str:
        return f"{self.INSTALLATION_SUBJECT_PREFIX}{self.device_identity.jkt}"

    def snapshot(
        self,
        *,
        account_id: str | None,
        account_epoch: int,
        now: datetime | None = None,
    ) -> AccessSnapshot:
        if not self.enforcement_required:
            return AccessSnapshot.unrestricted()
        state = self._read_state()
        revision = self._nonnegative_int(state.get("snapshot_revision"))
        selected = self._active_subject(state)
        decision = self._subject_decision(state, selected) if selected else None
        highest = (
            decision["highest_decision_seq"]
            if decision is not None
            else self._nonnegative_int(state.get("highest_decision_seq"))
        )
        if not self.license_path.is_file():
            return AccessSnapshot(True, BetaAccessStatus.NOT_ACTIVATED, None, highest, revision)
        if selected is None:
            return AccessSnapshot(True, BetaAccessStatus.CERT_INVALID, None, highest, revision)
        selected_epoch = self._nonnegative_int(state.get("account_epoch"))
        installation_bound = self._is_installation_subject(selected)
        if installation_bound:
            if selected != self.installation_subject():
                return AccessSnapshot(True, BetaAccessStatus.CERT_INVALID, selected, highest, revision)
        elif not account_id or selected != account_id or selected_epoch != account_epoch:
            return AccessSnapshot(True, BetaAccessStatus.ACCOUNT_SUSPENDED, selected, highest, revision)
        try:
            if not self.device_identity.prove_local_possession():
                raise RuntimeError("possession proof failed")
        except Exception:
            return AccessSnapshot(True, BetaAccessStatus.DEVICE_KEY_UNAVAILABLE, selected, highest, revision)
        try:
            token = self.license_path.read_text(encoding="utf-8")
            verified = self.verifier.verify(
                token,
                now=now or datetime.now(timezone.utc),
                expected_subject=selected,
                expected_jkt=self.device_identity.jkt,
            )
        except LicenseVerificationError as exc:
            status = BetaAccessStatus.EXPIRED if "expired" in str(exc).lower() else BetaAccessStatus.CERT_INVALID
            return AccessSnapshot(True, status, selected, highest, revision)
        except OSError:
            return AccessSnapshot(True, BetaAccessStatus.CERT_INVALID, selected, highest, revision)
        revoked = decision["revoked_decision_seq"] if decision is not None else 0
        if revoked >= verified.decision_seq:
            return AccessSnapshot(True, BetaAccessStatus.REVOKED, selected, max(highest, revoked), revision)
        status = BetaAccessStatus.GRACE if verified.disposition is AccessDisposition.GRACE else BetaAccessStatus.ACTIVE
        return AccessSnapshot(
            True,
            status,
            selected,
            verified.decision_seq,
            revision,
            refresh_due=verified.refresh_due,
            renew_by=int(verified.claims["renew_by"]),
            expires_at=int(verified.claims["exp"]),
        )

    def read_current_license(self) -> str:
        """Return the public signed credential; never device-key material."""

        try:
            token = self.license_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LicenseCandidateRejected("current license is unavailable") from exc
        if not token:
            raise LicenseCandidateRejected("current license is unavailable")
        return token

    def read_refresh_backoff(
        self,
        *,
        account_id: str | None,
        account_epoch: int,
        subject: str | None = None,
    ) -> tuple[int, float]:
        state = self._read_state()
        if not self._principal_matches(state, account_id=account_id, account_epoch=account_epoch, subject=subject):
            return (0, 0.0)
        failure_count = min(self._nonnegative_int(state.get("refresh_failure_count")), 8)
        value = state.get("refresh_next_attempt_at")
        next_attempt = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0.0
        return (failure_count, next_attempt)

    def record_refresh_backoff(
        self,
        *,
        account_id: str | None,
        account_epoch: int,
        subject: str | None = None,
        failure_count: int,
        next_attempt_at: float,
    ) -> None:
        state = self._read_state()
        if not self._principal_matches(state, account_id=account_id, account_epoch=account_epoch, subject=subject):
            raise LicenseCandidateRejected("refresh account fence mismatch")
        state["refresh_failure_count"] = min(max(int(failure_count), 0), 8)
        state["refresh_next_attempt_at"] = max(float(next_attempt_at), 0.0)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._atomic_write(self.state_path, json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    def record_revocation(
        self,
        *,
        decision_seq: int,
        account_id: str | None,
        account_epoch: int,
        subject: str | None = None,
    ) -> None:
        """Record a decision only after its signed status was verified upstream."""

        state = self._read_state()
        if not self._principal_matches(state, account_id=account_id, account_epoch=account_epoch, subject=subject):
            raise LicenseCandidateRejected("revocation account fence mismatch")
        selected = self._active_subject(state)
        if selected is None:
            raise LicenseCandidateRejected("revocation subject is unavailable")
        current = self._subject_decision(state, selected)
        highest = current["highest_decision_seq"]
        if decision_seq <= highest:
            raise LicenseCandidateRejected("revocation decision is not newer")
        subject_decisions = self._subject_decisions(state)
        subject_decisions[selected] = {
            "highest_decision_seq": decision_seq,
            "revoked_decision_seq": decision_seq,
            "accepted_license_digest": current.get("accepted_license_digest"),
        }
        state["version"] = 3
        state["subject_decisions"] = subject_decisions
        state["revoked_decision_seq"] = decision_seq
        state["highest_decision_seq"] = decision_seq
        state["snapshot_revision"] = self._nonnegative_int(state.get("snapshot_revision")) + 1
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._atomic_write(self.state_path, json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    def apply_signed_revocation(
        self,
        token: str,
        *,
        status_verifier: BetaStatusVerifier,
        account_id: str | None,
        account_epoch: int,
        subject: str | None = None,
        request_id: str,
        challenge_id: str,
        now: datetime | None = None,
    ) -> AccessSnapshot:
        """Persist a deny only after the dedicated status signature verifies."""

        state = self._read_state()
        expected_subject = subject or account_id
        if not self._principal_matches(state, account_id=account_id, account_epoch=account_epoch, subject=expected_subject):
            raise LicenseCandidateRejected("revocation account fence mismatch")
        try:
            license_claims = self.verifier.verify_historical_identity(
                self.read_current_license(),
                expected_subject=expected_subject,
                expected_jkt=self.device_identity.jkt,
            )
            decision = status_verifier.verify(
                token,
                expected_subject=expected_subject,
                expected_device_id=str(license_claims["device_id"]),
                expected_license_id=str(license_claims["license_id"]),
                expected_request_id=request_id,
                expected_challenge_id=challenge_id,
                now=now,
            )
        except LicenseVerificationError as exc:
            raise LicenseCandidateRejected(f"signed revocation rejected: {exc}") from exc
        self.record_revocation(
            decision_seq=decision.decision_seq,
            account_id=account_id,
            account_epoch=account_epoch,
            subject=expected_subject,
        )
        return self.snapshot(account_id=account_id, account_epoch=account_epoch, now=now)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _is_installation_subject(cls, subject: Any) -> bool:
        return isinstance(subject, str) and subject.startswith(cls.INSTALLATION_SUBJECT_PREFIX)

    @staticmethod
    def _active_subject(state: dict[str, Any]) -> str | None:
        subject = state.get("active_subject")
        if isinstance(subject, str) and subject:
            return subject
        account_id = state.get("active_account_id")
        return account_id if isinstance(account_id, str) and account_id else None

    def _subject_decisions(self, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw = state.get("subject_decisions")
        decisions: dict[str, dict[str, Any]] = {}
        if isinstance(raw, dict):
            for subject, value in raw.items():
                if isinstance(subject, str) and subject and isinstance(value, dict):
                    decisions[subject] = dict(value)
        active_subject = self._active_subject(state)
        if active_subject and active_subject not in decisions:
            decisions[active_subject] = {
                "highest_decision_seq": self._nonnegative_int(state.get("highest_decision_seq")),
                "revoked_decision_seq": self._nonnegative_int(state.get("revoked_decision_seq")),
                "accepted_license_digest": state.get("license_digest"),
            }
        return decisions

    def _subject_decision(self, state: dict[str, Any], subject: str) -> dict[str, Any]:
        value = self._subject_decisions(state).get(subject, {})
        digest = value.get("accepted_license_digest")
        return {
            "highest_decision_seq": self._nonnegative_int(value.get("highest_decision_seq")),
            "revoked_decision_seq": self._nonnegative_int(value.get("revoked_decision_seq")),
            "accepted_license_digest": digest if isinstance(digest, str) and digest else None,
        }

    def _principal_matches(
        self,
        state: dict[str, Any],
        *,
        account_id: str | None,
        account_epoch: int,
        subject: str | None,
    ) -> bool:
        selected = self._active_subject(state)
        expected = subject or account_id
        if self._is_installation_subject(selected):
            return selected == expected == self.installation_subject()
        return (
            selected == expected == account_id
            and self._nonnegative_int(state.get("account_epoch")) == account_epoch
        )

    @staticmethod
    def _nonnegative_int(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def _atomic_write(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if temporary.exists():
                temporary.unlink()
