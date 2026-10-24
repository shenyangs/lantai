from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.mac_use import MacUseActionResult, MacUseService
from lantai.app.routers import mac_use as mac_use_router

app = FastAPI()
app.include_router(mac_use_router.router, prefix="/api")


class FakeBridge:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        data = (
            {"windows": []}
            if request.action == "list_windows"
            else {"selection_receipt": "macsel_fixture"}
            if request.action == "select_target"
            else {}
        )
        return MacUseActionResult(
            ok=True,
            action=request.action,
            run_id=request.run_id,
            action_id=request.action_id,
            effect_id=request.effect_id,
            dispatch_status="not_sent",
            effect_status=(
                "verified"
                if request.action in {"pause", "resume", "stop", "session_status"}
                else "not_applicable"
            ),
            data=data,
        )


async def test_window_picker_uses_read_only_bridge_action(monkeypatch) -> None:
    bridge = FakeBridge()
    monkeypatch.setattr(mac_use_router, "_service", MacUseService(bridge=bridge))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/mac-use/windows")

    assert response.status_code == 200
    assert response.json()["dispatch_status"] == "not_sent"
    assert bridge.requests[0].action == "list_windows"
    assert bridge.requests[0].metadata["source"] == "desktop_window_picker"


async def test_observe_requires_concrete_bundle_and_window() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/mac-use/observe",
            json={"app_bundle_id": "", "window_id": ""},
        )

    assert response.status_code == 422


async def test_stop_session_uses_server_owned_action_identity(monkeypatch) -> None:
    bridge = FakeBridge()
    monkeypatch.setattr(mac_use_router, "_service", MacUseService(bridge=bridge))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/mac-use/sessions/run-42/stop")

    assert response.status_code == 200
    request = bridge.requests[0]
    assert request.action == "stop"
    assert request.run_id == "run-42"
    assert request.action_id
    assert request.effect_id
