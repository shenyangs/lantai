from __future__ import annotations

import base64
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from lantai.security.beta_license import AccessDisposition, BetaLicenseVerifier


@pytest.mark.integration
def test_node_issued_license_is_verified_by_desktop_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    helper = root / "services/member-gateway/tests/support/beta-license-interop-fixture.js"
    completed = subprocess.run(
        ["node", str(helper)],
        cwd=root / "services/member-gateway",
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    fixture = json.loads(completed.stdout)
    raw_public = base64.urlsafe_b64decode(fixture["publicJwk"]["x"] + "==")
    verifier = BetaLicenseVerifier(
        issuer=fixture["issuer"],
        key_ring={"beta-interop-01": Ed25519PublicKey.from_public_bytes(raw_public)},
    )

    verified = verifier.verify(
        fixture["token"],
        now=datetime.fromisoformat(fixture["verifyAt"].replace("Z", "+00:00")),
        expected_subject=fixture["subject"],
        expected_jkt=fixture["deviceJkt"],
    )

    assert verified.disposition is AccessDisposition.ALLOW
    assert verified.claims["decision_seq"] == 7
    assert verified.claims["entitlement_epoch"] == 2
    assert verified.claims["device_epoch"] == 3
