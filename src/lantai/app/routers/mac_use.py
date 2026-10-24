"""Read-only selection and session controls for the Mac Use UI."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from ..mac_use import MacUseActionRequest, MacUseActionResult, MacUseService
from ..mac_use.models import MacUseTarget

router = APIRouter(prefix="/mac-use", tags=["mac-use"])
_service = MacUseService()


def get_mac_use_service() -> MacUseService:
    return _service


class MacUseObserveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_bundle_id: str = Field(min_length=1, max_length=300)
    window_id: str = Field(min_length=1, max_length=80)
    process_id: int = Field(ge=1)
    process_started_at: float = Field(gt=0)
    window_title: str = Field(default="", max_length=500)
    selection_receipt: str = Field(min_length=1, max_length=128)
    run_id: str = Field(default="ui-selection", min_length=1, max_length=128)


@router.get("/status", response_model=MacUseActionResult)
async def mac_use_status() -> MacUseActionResult:
    return await _service.execute(MacUseActionRequest(action="status"))


@router.get("/windows", response_model=MacUseActionResult)
async def list_mac_use_windows() -> MacUseActionResult:
    return await _service.execute(
        MacUseActionRequest(
            action="list_windows",
            metadata={"source": "desktop_window_picker", "include_status": True},
        ),
    )


@router.post("/observe", response_model=MacUseActionResult)
async def observe_mac_use_window(
    request: MacUseObserveRequest,
) -> MacUseActionResult:
    return await _service.execute(
        MacUseActionRequest(
            action="observe_window",
            run_id=request.run_id,
            target=MacUseTarget(
                app_bundle_id=request.app_bundle_id,
                window_id=request.window_id,
                process_id=request.process_id,
                process_started_at=request.process_started_at,
                window_title=request.window_title,
            ),
            selection_receipt=request.selection_receipt,
            metadata={"source": "desktop_window_picker"},
        ),
    )


@router.post("/sessions/{run_id}/{operation}", response_model=MacUseActionResult)
async def control_mac_use_session(
    run_id: str,
    operation: Literal["pause", "resume", "stop", "session_status"],
) -> MacUseActionResult:
    return await _service.execute(
        MacUseActionRequest(action=operation, run_id=run_id),
    )
