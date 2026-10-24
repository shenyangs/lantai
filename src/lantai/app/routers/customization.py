"""HTTP control plane for versioned, recoverable Lantai customization."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ...constant import WORKING_DIR
from ...customization.models import ChangePlan
from ...customization.import_export import export_template, import_template
from ...customization.registry import RegistryError
from ...customization.service import CustomizationService
from ...customization.store import (
    CustomizationStoreError,
    ProfileNotFoundError,
    RevisionConflictError,
)

router = APIRouter(prefix="/customization", tags=["customization"])


def _service() -> CustomizationService:
    return CustomizationService(
        WORKING_DIR / "customization",
        settings_path=WORKING_DIR / "settings.json",
    )


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, ProfileNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, RevisionConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (RegistryError, CustomizationStoreError, ValueError)):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/registry")
async def get_registry() -> dict[str, Any]:
    return _service().registry.public_payload()


@router.get("/effective-state")
async def get_effective_state() -> dict[str, Any]:
    service = _service()
    return service.store.get_effective_state(service.registry).model_dump(mode="json")


@router.get("/state")
async def get_state() -> dict[str, Any]:
    return _service().store.get_state().model_dump(mode="json")


@router.get("/health")
async def get_health() -> dict[str, Any]:
    return _service().health()


@router.post("/boot-health/accept")
async def accept_boot_health() -> dict[str, Any]:
    return _service().store.accept_boot_health().model_dump(mode="json")


@router.get("/profiles")
async def list_profiles() -> list[dict[str, Any]]:
    return [
        profile.model_dump(mode="json") for profile in _service().store.list_profiles()
    ]


@router.post("/profiles")
async def create_profile(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return _service().create_profile(body).model_dump(mode="json")
    except Exception as exc:  # normalized into stable HTTP errors
        _raise_http(exc)


@router.get("/profiles/{profile_id}")
async def get_profile(profile_id: str) -> dict[str, Any]:
    try:
        return _service().store.get_profile(profile_id).model_dump(mode="json")
    except Exception as exc:
        _raise_http(exc)


@router.patch("/profiles/{profile_id}")
async def patch_profile(
    profile_id: str,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    allowed = {"name", "description", "icon", "color"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported fields: {sorted(unknown)}")
    try:
        return _service().store.save_profile_metadata(
            profile_id,
            **body,
        ).model_dump(mode="json")
    except Exception as exc:
        _raise_http(exc)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str) -> None:
    try:
        _service().store.delete_profile(profile_id)
    except Exception as exc:
        _raise_http(exc)


@router.post("/profiles/{profile_id}/copy")
async def copy_profile(
    profile_id: str,
    body: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    try:
        return _service().store.copy_profile(
            profile_id,
            name=str(body.get("name") or "") or None,
        ).model_dump(mode="json")
    except Exception as exc:
        _raise_http(exc)


@router.get("/profiles/{profile_id}/export")
async def export_profile(profile_id: str) -> dict[str, Any]:
    try:
        service = _service()
        return export_template(
            service.store.get_profile(profile_id),
            service.registry,
        )
    except Exception as exc:
        _raise_http(exc)


@router.post("/profiles/import")
async def import_profile(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        service = _service()
        profile, report = import_template(
            body,
            store=service.store,
            registry=service.registry,
        )
        return {"profile": profile.model_dump(mode="json"), "report": report}
    except Exception as exc:
        _raise_http(exc)


@router.get("/profiles/{profile_id}/revisions")
async def list_revisions(profile_id: str) -> list[dict[str, Any]]:
    try:
        service = _service()
        service.store.get_profile(profile_id)
        return [
            record.model_dump(mode="json")
            for record in service.store.list_revisions(profile_id)
        ]
    except Exception as exc:
        _raise_http(exc)


@router.post("/profiles/{profile_id}/revisions/{revision}/restore")
async def restore_revision(profile_id: str, revision: int) -> dict[str, Any]:
    try:
        service = _service()
        return service.store.restore_revision(
            profile_id,
            revision,
            service.registry,
        ).model_dump(mode="json")
    except Exception as exc:
        _raise_http(exc)


@router.post("/profiles/{profile_id}/undo")
async def undo_last_change(profile_id: str) -> dict[str, Any]:
    try:
        service = _service()
        return service.store.undo_last(
            profile_id,
            service.registry,
        ).model_dump(mode="json")
    except Exception as exc:
        _raise_http(exc)


@router.get("/slots")
async def list_slots() -> list[dict[str, Any]]:
    return [slot.model_dump(mode="json") for slot in _service().store.list_slots()]


@router.put("/slots/{index}")
async def put_slot(index: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return _service().store.set_slot(
            index,
            profile_id=body.get("profile_id"),
            label=body.get("label"),
            icon=body.get("icon"),
            color=body.get("color"),
        ).model_dump(mode="json")
    except Exception as exc:
        _raise_http(exc)


@router.post("/profiles/{profile_id}/activate")
async def activate_profile(profile_id: str) -> dict[str, Any]:
    try:
        service = _service()
        service.dismiss_boot_recovery_override()
        return service.store.activate_profile(profile_id).model_dump(mode="json")
    except Exception as exc:
        _raise_http(exc)


@router.post("/plans/preview")
async def preview_change_plan(plan: ChangePlan) -> dict[str, Any]:
    try:
        return _service().preview(plan).model_dump(mode="json")
    except Exception as exc:
        _raise_http(exc)


@router.post("/plans/from-intent")
async def plan_from_intent(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    intent = str(body.get("intent") or "").strip()
    target_profile_id = str(body.get("target_profile_id") or "").strip()
    agent_id = str(body.get("agent_id") or "default").strip() or "default"
    if not intent or not target_profile_id:
        raise HTTPException(
            status_code=400,
            detail="intent and target_profile_id are required",
        )
    try:
        return await _service().plan_from_intent(
            intent=intent,
            target_profile_id=target_profile_id,
            agent_id=agent_id,
        )
    except Exception as exc:
        _raise_http(exc)


@router.post("/plans/apply")
async def apply_change_plan(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        plan_id = str(body.get("plan_id") or "").strip()
        if not plan_id:
            raise ValueError("plan_id is required")
        return _service().apply_saved(
            plan_id,
            actor="user",
            confirmation_token=(
                str(body.get("confirmation_token"))
                if body.get("confirmation_token")
                else None
            ),
        ).model_dump(mode="json")
    except Exception as exc:
        _raise_http(exc)


@router.post("/plans/{plan_id}/confirm")
async def confirm_change_plan(plan_id: str) -> dict[str, Any]:
    try:
        return _service().confirm(plan_id, actor="user")
    except Exception as exc:
        _raise_http(exc)


@router.post("/recovery/factory-mode")
async def enable_factory_mode() -> dict[str, Any]:
    return _service().store.set_mode("factory").model_dump(mode="json")


@router.post("/recovery/safe-mode")
async def enable_safe_mode() -> dict[str, Any]:
    return _service().store.set_mode("safe").model_dump(mode="json")


@router.post("/recovery/normal-mode")
async def enable_normal_mode() -> dict[str, Any]:
    service = _service()
    state = service.store.get_state()
    if state.active_profile_id is None:
        raise HTTPException(status_code=400, detail="No active Profile to resume")
    service.dismiss_boot_recovery_override()
    return service.store.set_mode("normal").model_dump(mode="json")


@router.post("/recovery/last-known-good/{profile_id}")
async def restore_last_known_good(profile_id: str) -> dict[str, Any]:
    try:
        service = _service()
        return service.store.restore_last_known_good(
            profile_id,
            service.registry,
        ).model_dump(mode="json")
    except Exception as exc:
        _raise_http(exc)
