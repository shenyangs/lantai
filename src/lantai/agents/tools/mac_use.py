"""Model-facing Mac Use tools with read/write surfaces kept separate."""

from __future__ import annotations

import json
import hashlib
from typing import Any
from uuid import uuid4

from ...app.mac_use import MacUseActionRequest, MacUseService
from ...app.mac_use.models import MacUseTarget
from ...runtime.tool_response import TextBlock, ToolResponse


def _response(
    payload: dict[str, Any],
    *,
    resolves_effect_id: str = "",
    effect_scope_hash: str = "",
) -> ToolResponse:
    metadata = {
        "external_effect_status": str(payload.get("effect_status") or ""),
        "external_effect_id": str(payload.get("effect_id") or ""),
        "external_dispatch_status": str(payload.get("dispatch_status") or ""),
    }
    if resolves_effect_id:
        metadata["resolves_external_effect_id"] = resolves_effect_id
        metadata["resolves_external_effect_scope_hash"] = effect_scope_hash
    elif effect_scope_hash:
        metadata["external_effect_scope_hash"] = effect_scope_hash
    return ToolResponse(
        content=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))],
        metadata=metadata,
        is_error=not bool(payload.get("ok")),
    )


def _target(
    app_bundle_id: str,
    window_id: str,
    process_id: int,
    process_started_at: float,
    window_title: str,
) -> MacUseTarget:
    return MacUseTarget(
        app_bundle_id=app_bundle_id,
        window_id=window_id,
        process_id=process_id,
        process_started_at=process_started_at,
        window_title=window_title,
    )


def _runtime_run_id(requested: str) -> str:
    if requested.strip():
        return requested.strip()
    try:
        from ...app.agent_context import get_current_session_id

        current = str(get_current_session_id() or "").strip()
        if current:
            return current
    except Exception:
        pass
    return uuid4().hex


async def _run(action: str, **kwargs: Any) -> ToolResponse:
    request = MacUseActionRequest(action=action, **kwargs)
    result = await MacUseService().execute(request)
    scope_payload = {
        "app_bundle_id": request.target.app_bundle_id,
        "process_id": request.target.process_id,
        "process_started_at": request.target.process_started_at,
        "window_id": request.target.window_id,
        "observation_id": request.observation_id,
        "verification_element_id": (
            request.verification_element_id or request.element_id
        ),
        "expected_value": request.expected_value,
    }
    effect_scope_hash = hashlib.sha256(
        json.dumps(
            scope_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()
    return _response(
        result.model_dump(mode="json"),
        resolves_effect_id=request.resolves_effect_id,
        effect_scope_hash=effect_scope_hash,
    )


async def mac_inspect(run_id: str = "") -> ToolResponse:
    """Inspect Mac Use availability and native helper permissions.

    This is read-only. Application windows are listed only in the user-facing
    picker, where a real click creates a target receipt. This tool never
    activates an application or sends input.
    """
    return await _run(
        "status",
        run_id=_runtime_run_id(run_id),
        metadata={"include_status": True},
    )


async def mac_observe(
    app_bundle_id: str,
    window_id: str,
    process_id: int,
    process_started_at: float,
    selection_receipt: str,
    window_title: str = "",
    run_id: str = "",
) -> ToolResponse:
    """Read a selected Mac application window and its accessible controls.

    The target and selection receipt must come from the user's window picker.
    The observation is bound to the application/process/window generation and
    does not move focus or send input.
    """
    return await _run(
        "observe_window",
        run_id=_runtime_run_id(run_id),
        target=_target(
            app_bundle_id,
            window_id,
            process_id,
            process_started_at,
            window_title,
        ),
        selection_receipt=selection_receipt,
    )


async def mac_act(
    operation: str,
    app_bundle_id: str,
    window_id: str,
    process_id: int,
    process_started_at: float,
    selection_receipt: str,
    observation_id: str,
    verification_element_id: str = "",
    expected_value: str = "",
    element_id: str = "",
    element_role: str = "",
    element_title: str = "",
    x: float | None = None,
    y: float | None = None,
    text: str = "",
    key: str = "",
    window_title: str = "",
    run_id: str = "",
    reason: str = "",
) -> ToolResponse:
    """Perform one authorized action in a selected Mac application window.

    Supported operations are focus_window, click_at, type_text, press_key,
    set_value, and press_element. type_text requires an observed editable
    element; coordinates are normalized to the most recent observation.
    Actions whose effect cannot be read back immediately require
    a verification element and expected value that were not already true in
    the bound observation. A successful transport is not reported as a
    completed task; call mac_verify with the returned effect_id. Ambiguous
    writes are never automatically retried.
    """
    allowed = {
        "focus_window",
        "click_at",
        "type_text",
        "press_key",
        "set_value",
        "press_element",
    }
    action = operation.strip().lower()
    if action not in allowed:
        return _response(
            {
                "ok": False,
                "error_code": "mac_use_action_unsupported",
                "error": f"Unsupported Mac action: {operation}",
            },
        )
    return await _run(
        action,
        run_id=_runtime_run_id(run_id),
        target=_target(
            app_bundle_id,
            window_id,
            process_id,
            process_started_at,
            window_title,
        ),
        selection_receipt=selection_receipt,
        observation_id=observation_id,
        verification_element_id=verification_element_id,
        expected_value=expected_value,
        element_id=element_id,
        element_role=element_role,
        element_title=element_title,
        x=x,
        y=y,
        text=text,
        key=key,
        reason=reason,
    )


async def mac_verify(
    app_bundle_id: str,
    window_id: str,
    process_id: int,
    process_started_at: float,
    element_id: str,
    expected_value: str,
    effect_id_to_resolve: str,
    selection_receipt: str,
    observation_id: str,
    element_role: str = "",
    element_title: str = "",
    window_title: str = "",
    run_id: str = "",
) -> ToolResponse:
    """Read back an element or window state after a Mac application action.

    Verification is read-only. Pass the effect_id returned by mac_act and the
    exact original observation, target, element, and expected value declared
    for that action. It reports
    whether that bound state can be proven and never repeats the write.
    """
    return await _run(
        "observe_window",
        run_id=_runtime_run_id(run_id),
        target=_target(
            app_bundle_id,
            window_id,
            process_id,
            process_started_at,
            window_title,
        ),
        selection_receipt=selection_receipt,
        observation_id=observation_id,
        element_id=element_id,
        element_role=element_role,
        element_title=element_title,
        expected_value=expected_value,
        resolves_effect_id=effect_id_to_resolve,
        metadata={"verification": True},
    )


async def mac_session(operation: str, run_id: str) -> ToolResponse:
    """Pause, resume, stop, or inspect a Mac Use session.

    Stop and pause revoke queued actions immediately. Resume re-observes the
    target before later writes and does not create new permissions.
    """
    action = operation.strip().lower()
    if action not in {"pause", "resume", "stop", "session_status"}:
        return _response(
            {
                "ok": False,
                "error_code": "mac_use_session_action_unsupported",
                "error": f"Unsupported Mac session action: {operation}",
            },
        )
    return await _run(action, run_id=run_id)
