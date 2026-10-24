from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lantai.app.beta_access.router import create_beta_access_router
from lantai.app.beta_access.service import BetaAccessApplicationError


class Service:
    def __init__(self):
        self.codes = []

    def local_status(self):
        return {"status": "not_activated", "enforcement_required": True}

    async def activate(self, *, code, request_id=None, device_label=None):
        self.codes.append(code)
        return {"status": "active", "operation_id": request_id}

    async def issue(self, *, request_id=None, device_label=None):
        return {"status": "active", "operation_id": request_id}

    async def recover(self, *, operation_id, recovery_token=None):
        if operation_id == "denied":
            raise BetaAccessApplicationError("operation_not_found", "没有找到该恢复记录。", status_code=404)
        return {"status": "active", "operation_id": operation_id}


def _client(service: Service) -> TestClient:
    app = FastAPI()
    app.include_router(create_beta_access_router(service=service))
    return TestClient(app)


def test_activation_api_preserves_custom_invite_exactly_without_format_schema():
    service = Service()
    client = _client(service)

    response = client.post(
        "/beta-access/activate",
        json={"code": "朋友 请坐", "request_id": "stable-request"},
    )

    assert response.status_code == 200
    assert service.codes == ["朋友 请坐"]
    schema = client.get("/openapi.json").json()
    serialized = str(schema["components"]["schemas"])
    assert "maxLength" not in serialized
    assert "pattern" not in serialized


def test_router_returns_sanitized_application_error_contract():
    response = _client(Service()).post(
        "/beta-access/recover",
        json={"operation_id": "denied"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "operation_not_found", "message": "没有找到该恢复记录。"}
    }
