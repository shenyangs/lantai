from __future__ import annotations

import inspect
import json

import pytest
from pydantic import ValidationError

from lantai.app.mac_use import (
    ElectronMacUseBridgeClient,
    MacUseActionRequest,
    MacUseActionResult,
    MacUseService,
)
from lantai.app.mac_use.models import MacUseTarget
from lantai.agents.tools.mac_use import _response, mac_act


def _target() -> MacUseTarget:
    return MacUseTarget(
        app_bundle_id="com.apple.TextEdit",
        process_id=101,
        process_started_at=1788700000.125,
        window_id="42",
        window_title="Mac Use fixture.txt",
    )


def test_mac_use_request_requires_bound_target_for_write() -> None:
    with pytest.raises(ValidationError, match="app_bundle_id, window_id, and process generation"):
        MacUseActionRequest(action="type_text", text="hello")


def test_mac_act_schema_requires_observation_but_not_irrelevant_predicates() -> None:
    signature = inspect.signature(mac_act)

    assert signature.parameters["observation_id"].default is inspect.Parameter.empty
    assert signature.parameters["verification_element_id"].default == ""
    assert signature.parameters["expected_value"].default == ""


def test_mac_use_request_requires_coordinates_for_click() -> None:
    with pytest.raises(ValidationError, match="normalized x and y"):
        MacUseActionRequest(
            action="click_at",
            target=_target(),
            selection_receipt="macsel_fixture",
            observation_id="obs-1",
        )


def test_mac_use_text_input_requires_an_observed_element() -> None:
    with pytest.raises(ValidationError, match="type_text requires element_id"):
        MacUseActionRequest(
            action="type_text",
            target=_target(),
            selection_receipt="macsel_fixture",
            observation_id="obs-1",
            text="hello",
            verification_element_id="0.1",
            expected_value="after",
        )


def test_mac_use_observation_requires_user_selection_receipt() -> None:
    with pytest.raises(ValidationError, match="user selection receipt"):
        MacUseActionRequest(action="observe_window", target=_target())


def test_mac_use_verification_requires_original_observation_and_effect() -> None:
    with pytest.raises(ValidationError, match="original observation_id"):
        MacUseActionRequest(
            action="observe_window",
            target=_target(),
            selection_receipt="macsel_fixture",
            element_id="0.1",
            expected_value="after",
            metadata={"verification": True},
        )


@pytest.mark.asyncio
async def test_unavailable_bridge_never_claims_execution() -> None:
    request = MacUseActionRequest(action="list_windows")
    result = await ElectronMacUseBridgeClient(base_url="", token="").execute(request)

    assert result.ok is False
    assert result.dispatch_status == "not_sent"
    assert result.effect_status == "failed"
    assert result.retry_safe is True
    assert result.error_code == "mac_use_bridge_unavailable"


@pytest.mark.asyncio
async def test_bridge_transport_failure_is_ambiguous_for_write(monkeypatch) -> None:
    request = MacUseActionRequest(
        action="type_text",
        target=_target(),
        selection_receipt="macsel_fixture",
        observation_id="obs-1",
        element_id="0.1",
        text="hello",
        verification_element_id="0.1",
        expected_value="after",
    )
    bridge = ElectronMacUseBridgeClient(
        base_url="http://127.0.0.1:9",
        token="secret",
        timeout_seconds=0.01,
    )

    def fail(_request, timeout):
        raise TimeoutError(f"timeout={timeout}")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = await bridge.execute(request)

    assert result.ok is False
    assert result.dispatch_status == "unknown"
    assert result.effect_status == "ambiguous"
    assert result.retry_safe is False


@pytest.mark.asyncio
async def test_bridge_transport_failure_is_retry_safe_for_read(monkeypatch) -> None:
    request = MacUseActionRequest(action="status")
    bridge = ElectronMacUseBridgeClient(
        base_url="http://127.0.0.1:9",
        token="secret",
        timeout_seconds=0.01,
    )

    def fail(_request, timeout):
        raise TimeoutError(timeout)

    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = await bridge.execute(request)

    assert result.ok is False
    assert result.dispatch_status == "transport_failed"
    assert result.effect_status == "failed"
    assert result.retry_safe is True


@pytest.mark.asyncio
async def test_bridge_malformed_write_response_remains_ambiguous(monkeypatch) -> None:
    request = MacUseActionRequest(
        action="type_text",
        target=_target(),
        selection_receipt="macsel_fixture",
        observation_id="obs-1",
        element_id="0.1",
        text="hello",
        verification_element_id="0.1",
        expected_value="after",
    )
    bridge = ElectronMacUseBridgeClient(
        base_url="http://127.0.0.1:1",
        token="secret",
    )
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda _request: {"ok": False, "error": "bad gateway"},
    )

    result = await bridge.execute(request)

    assert result.ok is False
    assert result.dispatch_status == "unknown"
    assert result.effect_status == "ambiguous"
    assert result.retry_safe is False
    assert result.error_code == "mac_use_bridge_invalid_response"


@pytest.mark.asyncio
async def test_bridge_malformed_read_response_is_transport_failure(monkeypatch) -> None:
    request = MacUseActionRequest(action="status")
    bridge = ElectronMacUseBridgeClient(
        base_url="http://127.0.0.1:1",
        token="secret",
    )
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda _request: {"ok": False, "error": "bad gateway"},
    )

    result = await bridge.execute(request)

    assert result.ok is False
    assert result.dispatch_status == "transport_failed"
    assert result.effect_status == "failed"
    assert result.retry_safe is True
    assert result.error_code == "mac_use_bridge_invalid_response"


@pytest.mark.asyncio
async def test_service_preserves_three_dimensional_result() -> None:
    request = MacUseActionRequest(
        action="set_value",
        target=_target(),
        selection_receipt="macsel_fixture",
        observation_id="obs-1",
        element_id="0.1",
        text="澜台",
    )

    class FakeBridge:
        async def execute(self, received):
            assert received == request
            return MacUseActionResult(
                ok=True,
                action=received.action,
                run_id=received.run_id,
                action_id=received.action_id,
                effect_id=received.effect_id,
                policy_status="allowed",
                dispatch_status="sent",
                effect_status="verified",
                observed_change=True,
                data={"value": "澜台"},
            )

    result = await MacUseService(bridge=FakeBridge()).execute(request)  # type: ignore[arg-type]

    assert result.ok is True
    assert result.dispatch_status == "sent"
    assert result.effect_status == "verified"
    assert result.data["value"] == "澜台"


def test_bridge_capability_binds_body_and_expiry() -> None:
    bridge = ElectronMacUseBridgeClient(
        base_url="http://127.0.0.1:1",
        token="secret",
    )
    header = bridge._capability(json.dumps({"action": "status"}).encode())
    parts = header.split(":")

    assert len(parts) == 5
    assert parts[0] == "v1"
    assert len(parts[2]) == 32
    assert len(parts[3]) == 64
    assert len(parts[4]) == 64


def test_tool_response_projects_external_ambiguity_into_runtime_metadata() -> None:
    response = _response(
        {
            "ok": True,
            "effect_id": "gui-effect-1",
            "dispatch_status": "sent",
            "effect_status": "ambiguous",
        },
    )

    assert response.is_error is False
    assert response.metadata == {
        "external_effect_status": "ambiguous",
        "external_effect_id": "gui-effect-1",
        "external_dispatch_status": "sent",
    }
