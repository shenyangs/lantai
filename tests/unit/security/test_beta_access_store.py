from __future__ import annotations

import json
import base64
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lantai.security.beta_access import (
    AccessBoundary,
    AccessSnapshot,
    BetaAccessStatus,
    evaluate_admission,
)
from lantai.security.beta_access_store import BetaLicenseStore, LicenseCandidateRejected
from lantai.security.beta_license import BetaLicenseVerifier
from lantai.security.beta_status import BetaStatusVerifier


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token(private: Ed25519PrivateKey, payload: dict, *, header: dict) -> str:
    signing_input = (
        f"{_b64(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    return f"{signing_input}.{_b64(private.sign(signing_input.encode('ascii')))}"


class DeviceIdentity:
    jkt = "device-jkt"

    def prove_local_possession(self) -> bool:
        return True


def claims(now: datetime, *, seq: int = 1, subject: str = "11111111-2222-4333-8444-555555555555"):
    return {
        "ver": 1,
        "iss": "issuer",
        "aud": "lantai-desktop-beta",
        "sub": subject,
        "jti": f"issuance-{seq}",
        "license_id": "license-1",
        "entitlement_id": "entitlement-1",
        "device_id": "device-1",
        "cnf": {"jkt": "device-jkt"},
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "refresh_after": int((now + timedelta(days=7)).timestamp()),
        "renew_by": int((now + timedelta(days=90)).timestamp()),
        "exp": int((now + timedelta(days=120)).timestamp()),
        "decision_seq": seq,
        "entitlement_epoch": 1,
        "device_epoch": 1,
        "edition": "private_beta",
        "features": ["application.use"],
        "policy_version": 1,
    }


def test_candidate_is_verified_before_atomic_replace_and_bad_response_keeps_old_license(tmp_path):
    private = Ed25519PrivateKey.generate()
    verifier = BetaLicenseVerifier(issuer="issuer", key_ring={"kid": private.public_key()})
    store = BetaLicenseStore(root=tmp_path, verifier=verifier, device_identity=DeviceIdentity())
    now = datetime.now(timezone.utc)
    original = _token(private, claims(now), header={"alg": "Ed25519", "typ": "lantai-beta-license+jwt", "kid": "kid"})
    installed = store.install_candidate(original, account_id=claims(now)["sub"], account_epoch=4, now=now)
    assert installed.status is BetaAccessStatus.ACTIVE

    with pytest.raises(LicenseCandidateRejected):
        store.install_candidate(original + "broken", account_id=claims(now)["sub"], account_epoch=4, now=now)
    assert (tmp_path / "license.jws").read_text() == original
    assert json.loads((tmp_path / "state.json").read_text())["highest_decision_seq"] == 1


def test_account_epoch_and_higher_decision_fence_late_allow(tmp_path):
    private = Ed25519PrivateKey.generate()
    verifier = BetaLicenseVerifier(issuer="issuer", key_ring={"kid": private.public_key()})
    store = BetaLicenseStore(root=tmp_path, verifier=verifier, device_identity=DeviceIdentity())
    now = datetime.now(timezone.utc)
    newer = _token(private, claims(now, seq=2), header={"alg": "Ed25519", "typ": "lantai-beta-license+jwt", "kid": "kid"})
    store.install_candidate(newer, account_id=claims(now)["sub"], account_epoch=8, now=now)
    older = _token(private, claims(now, seq=1), header={"alg": "Ed25519", "typ": "lantai-beta-license+jwt", "kid": "kid"})
    with pytest.raises(LicenseCandidateRejected, match="older"):
        store.install_candidate(older, account_id=claims(now)["sub"], account_epoch=8, now=now)
    snapshot = store.snapshot(account_id=claims(now)["sub"], account_epoch=9, now=now)
    assert snapshot.status is BetaAccessStatus.ACCOUNT_SUSPENDED


def test_installation_license_remains_active_across_logout_and_account_switch(tmp_path):
    private = Ed25519PrivateKey.generate()
    verifier = BetaLicenseVerifier(issuer="issuer", key_ring={"kid": private.public_key()})
    store = BetaLicenseStore(root=tmp_path, verifier=verifier, device_identity=DeviceIdentity())
    now = datetime.now(timezone.utc)
    subject = "installation:device-jkt"
    token = _token(
        private,
        claims(now, subject=subject),
        header={"alg": "Ed25519", "typ": "lantai-beta-license+jwt", "kid": "kid"},
    )

    installed = store.install_candidate(
        token,
        subject=subject,
        account_id=None,
        account_epoch=0,
        now=now,
    )
    assert installed.status is BetaAccessStatus.ACTIVE
    assert installed.subject == subject
    assert store.snapshot(account_id=None, account_epoch=0, now=now).status is BetaAccessStatus.ACTIVE
    assert store.snapshot(
        account_id="99999999-2222-4333-8444-555555555555",
        account_epoch=22,
        now=now,
    ).status is BetaAccessStatus.ACTIVE
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["version"] == 3
    assert state["active_subject"] == subject
    assert state["active_account_id"] is None


def test_legacy_global_sequence_state_migrates_to_active_subject_ledger(tmp_path):
    private = Ed25519PrivateKey.generate()
    verifier = BetaLicenseVerifier(issuer="issuer", key_ring={"kid": private.public_key()})
    store = BetaLicenseStore(root=tmp_path, verifier=verifier, device_identity=DeviceIdentity())
    now = datetime.now(timezone.utc)
    subject = "11111111-2222-4333-8444-555555555555"
    header = {"alg": "Ed25519", "typ": "lantai-beta-license+jwt", "kid": "kid"}
    first = _token(private, claims(now, seq=1, subject=subject), header=header)
    store.install_candidate(first, account_id=subject, account_epoch=4, now=now)
    state = json.loads((tmp_path / "state.json").read_text())
    state["version"] = 2
    state.pop("subject_decisions")
    (tmp_path / "state.json").write_text(json.dumps(state))

    refreshed = _token(private, claims(now, seq=2, subject=subject), header=header)
    installed = store.install_candidate(refreshed, account_id=subject, account_epoch=4, now=now)

    assert installed.status is BetaAccessStatus.ACTIVE
    migrated = json.loads((tmp_path / "state.json").read_text())
    assert migrated["version"] == 3
    assert migrated["subject_decisions"][subject]["highest_decision_seq"] == 2


@pytest.mark.parametrize(
    ("first_subject", "second_subject"),
    [
        ("11111111-2222-4333-8444-555555555555", "installation:device-jkt"),
        ("installation:device-jkt", "11111111-2222-4333-8444-555555555555"),
        ("11111111-2222-4333-8444-555555555555", "99999999-2222-4333-8444-555555555555"),
    ],
)
def test_equal_decision_sequences_are_isolated_between_license_subjects(
    tmp_path,
    first_subject,
    second_subject,
):
    private = Ed25519PrivateKey.generate()
    verifier = BetaLicenseVerifier(issuer="issuer", key_ring={"kid": private.public_key()})
    store = BetaLicenseStore(root=tmp_path, verifier=verifier, device_identity=DeviceIdentity())
    now = datetime.now(timezone.utc)
    header = {"alg": "Ed25519", "typ": "lantai-beta-license+jwt", "kid": "kid"}

    first = _token(private, claims(now, seq=1, subject=first_subject), header=header)
    store.install_candidate(
        first,
        subject=first_subject,
        account_id=None if first_subject.startswith("installation:") else first_subject,
        account_epoch=0 if first_subject.startswith("installation:") else 1,
        now=now,
    )
    second = _token(private, claims(now, seq=1, subject=second_subject), header=header)
    installed = store.install_candidate(
        second,
        subject=second_subject,
        account_id=None if second_subject.startswith("installation:") else second_subject,
        account_epoch=0 if second_subject.startswith("installation:") else 2,
        now=now,
    )

    assert installed.status is BetaAccessStatus.ACTIVE
    assert installed.subject == second_subject


def test_revocation_tombstone_for_old_subject_does_not_block_new_subject(tmp_path):
    private = Ed25519PrivateKey.generate()
    verifier = BetaLicenseVerifier(issuer="issuer", key_ring={"kid": private.public_key()})
    store = BetaLicenseStore(root=tmp_path, verifier=verifier, device_identity=DeviceIdentity())
    now = datetime.now(timezone.utc)
    account_subject = "11111111-2222-4333-8444-555555555555"
    installation_subject = "installation:device-jkt"
    header = {"alg": "Ed25519", "typ": "lantai-beta-license+jwt", "kid": "kid"}

    account_token = _token(private, claims(now, seq=1, subject=account_subject), header=header)
    store.install_candidate(account_token, account_id=account_subject, account_epoch=3, now=now)
    store.record_revocation(
        decision_seq=5,
        account_id=account_subject,
        account_epoch=3,
        subject=account_subject,
    )

    installation_token = _token(private, claims(now, seq=1, subject=installation_subject), header=header)
    installed = store.install_candidate(
        installation_token,
        subject=installation_subject,
        account_id=None,
        account_epoch=0,
        now=now,
    )

    assert installed.status is BetaAccessStatus.ACTIVE
    assert installed.subject == installation_subject
    with pytest.raises(LicenseCandidateRejected, match="older"):
        store.install_candidate(
            account_token,
            subject=account_subject,
            account_id=account_subject,
            account_epoch=3,
            now=now,
        )


def test_admission_is_local_and_preserves_history_and_export_on_hard_denial():
    expired = AccessSnapshot(
        enforcement_required=True,
        status=BetaAccessStatus.EXPIRED,
        subject="account",
        decision_seq=3,
        snapshot_revision=9,
    )
    assert not evaluate_admission(expired, boundary=AccessBoundary.WORK_START, capability="application.use").allowed
    assert evaluate_admission(expired, boundary=AccessBoundary.PROTECTED_WRITE, capability="history.read").allowed
    assert evaluate_admission(expired, boundary=AccessBoundary.PROTECTED_WRITE, capability="data.export").allowed
    ordinary = AccessSnapshot.unrestricted()
    assert evaluate_admission(ordinary, boundary=AccessBoundary.WORK_START, capability="application.use").allowed


def test_only_a_bound_higher_signed_status_creates_revocation_tombstone(tmp_path):
    private = Ed25519PrivateKey.generate()
    verifier = BetaLicenseVerifier(issuer="issuer", key_ring={"kid": private.public_key()})
    status_verifier = BetaStatusVerifier(issuer="issuer", key_ring={"kid": private.public_key()})
    store = BetaLicenseStore(root=tmp_path, verifier=verifier, device_identity=DeviceIdentity())
    now = datetime.now(timezone.utc)
    license_token = _token(private, claims(now, seq=3), header={"alg": "Ed25519", "typ": "lantai-beta-license+jwt", "kid": "kid"})
    store.install_candidate(license_token, account_id=claims(now)["sub"], account_epoch=6, now=now)
    revocation_claims = {
        "iss": "issuer",
        "aud": "lantai-desktop-beta",
        "sub": claims(now)["sub"],
        "device_id": "device-1",
        "license_id": "license-1",
        "decision": "revoked",
        "decision_seq": 4,
        "request_id": "request-1",
        "challenge_id": "challenge-1",
        "issued_at": int(now.timestamp()),
        "reason_code": "device_revoked",
    }
    status = _token(private, revocation_claims, header={"alg": "Ed25519", "typ": "lantai-beta-status+jwt", "kid": "kid"})
    snapshot = store.apply_signed_revocation(
        status,
        status_verifier=status_verifier,
        account_id=claims(now)["sub"],
        account_epoch=6,
        request_id="request-1",
        challenge_id="challenge-1",
        now=now,
    )
    assert snapshot.status is BetaAccessStatus.REVOKED
    assert snapshot.decision_seq == 4
    with pytest.raises(LicenseCandidateRejected, match="older"):
        store.install_candidate(license_token, account_id=claims(now)["sub"], account_epoch=6, now=now)
