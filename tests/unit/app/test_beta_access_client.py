from __future__ import annotations

import pytest

from lantai.app.beta_access.client import BetaAccessClient, BetaAccessClientError
from lantai.security.beta_device_key import DeviceKeyUnavailableError


class MissingDeviceKey:
    def load_or_create(self):
        raise DeviceKeyUnavailableError("keychain locked")


class Identity:
    public_jwk = {"kty": "OKP", "crv": "Ed25519", "x": "public"}


class RejectingChallengeKey:
    def load_or_create(self):
        return Identity()

    def sign_server_challenge(self, _challenge):
        raise ValueError("untrusted audience")


class ChallengeClient(BetaAccessClient):
    async def _request(self, path, **kwargs):
        assert path == "/api/beta/challenges"
        return {
            "challenge": {
                "challengeId": "challenge-1",
                "nonce": "nonce",
                "purpose": "activate",
                "subject": "7c8468d0-54aa-4665-ad51-2f47c38c1151",
                "requestId": "request-1",
                "intentCommitment": "a" * 64,
                "deviceJkt": "device",
                "audience": "https://auth.lantai.guanlan.xin/api/beta",
                "expiresAt": "2030-01-01T00:00:00Z",
            }
        }


@pytest.mark.asyncio
async def test_keychain_failure_is_reported_without_starting_activation_request():
    client = BetaAccessClient(
        base_url="https://auth.lantai.guanlan.xin",
        device_keys=MissingDeviceKey(),
    )

    with pytest.raises(BetaAccessClientError) as raised:
        await client.activate(code="春", request_id="request-1", access_token="member")

    assert raised.value.code == "device_key_unavailable"


@pytest.mark.asyncio
async def test_untrusted_server_challenge_is_a_closed_invalid_response():
    client = ChallengeClient(
        base_url="https://auth.lantai.guanlan.xin",
        device_keys=RejectingChallengeKey(),
    )

    with pytest.raises(BetaAccessClientError) as raised:
        await client.activate(code="春", request_id="request-1", access_token="member")

    assert raised.value.code == "invalid_response"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://auth.lantai.guanlan.xin",
        "https://user@auth.lantai.guanlan.xin",
        "https://auth.lantai.guanlan.xin/unpinned",
        "https://auth.lantai.guanlan.xin?redirect=evil",
    ],
)
def test_client_requires_a_fixed_https_origin(base_url):
    with pytest.raises(ValueError):
        BetaAccessClient(base_url=base_url, device_keys=MissingDeviceKey())
