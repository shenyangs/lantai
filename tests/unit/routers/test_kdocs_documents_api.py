from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lantai.app import kdocs_documents
from lantai.app.routers import skills as skills_router


class _FakeDocumentsService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_documents(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("list", kwargs))
        return {
            "view": kwargs["view"],
            "items": [],
            "next_page_token": "",
            "total": 0,
            "parent": {"drive_id": "drive-1", "file_id": "0"},
        }

    def read_document(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("read", kwargs))
        return {
            "status": "done",
            "task_id": "",
            "content": "正文",
            "content_format": "markdown",
            "name": "周报.docx",
            "truncated": False,
            "warnings": [],
        }

    def file_info(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("info", kwargs))
        return {"key": "drive-1:file-1", "drive_id": "drive-1", "file_id": "file-1"}

    def file_link(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("link", kwargs))
        return {"file_id": "file-1", "url": "https://www.kdocs.cn/l/example"}


def _client(monkeypatch, service: object) -> TestClient:
    async def fake_service(_request):
        return service

    monkeypatch.setattr(skills_router, "_kdocs_document_service", fake_service)
    app = FastAPI()
    app.include_router(skills_router.router)
    return TestClient(app)


def test_kdocs_documents_api_forwards_bounded_browse_and_read_requests(monkeypatch) -> None:
    service = _FakeDocumentsService()
    client = _client(monkeypatch, service)

    listed = client.get(
        "/skills/integrations/kdocs/documents",
        params={
            "view": "folder",
            "drive_id": "drive-1",
            "parent_id": "folder-1",
            "page_token": "next-1",
            "page_size": 25,
        },
    )
    read = client.post(
        "/skills/integrations/kdocs/documents/read",
        json={
            "file_id": "file-1",
            "task_id": "task-1",
            "format": "markdown",
            "max_chars": 12345,
            "sheet_name": "Sheet1",
            "sheet_range": {"row_from": 1, "row_to": 20},
        },
    )

    assert listed.status_code == 200
    assert listed.json()["parent"] == {"drive_id": "drive-1", "file_id": "0"}
    assert read.status_code == 200
    assert read.json()["content"] == "正文"
    assert service.calls == [
        (
            "list",
            {
                "view": "folder",
                "drive_id": "drive-1",
                "parent_id": "folder-1",
                "query": "",
                "page_token": "next-1",
                "page_size": 25,
            },
        ),
        (
            "read",
            {
                "file_id": "file-1",
                "task_id": "task-1",
                "format": "markdown",
                "max_chars": 12345,
                "sheet_name": "Sheet1",
                "sheet_range": {"row_from": 1, "row_to": 20, "col_from": None, "col_to": None},
            },
        ),
    ]


def test_kdocs_documents_api_preserves_recoverable_error_code(monkeypatch) -> None:
    class _FailingService:
        def list_documents(self, **_kwargs: object) -> dict[str, object]:
            raise kdocs_documents.KdocsDocumentError(
                "authentication_required",
                "金山文档登录已失效，请重新连接后再试。",
                status_code=401,
            )

    response = _client(monkeypatch, _FailingService()).get(
        "/skills/integrations/kdocs/documents",
        params={"view": "recent"},
    )

    assert response.status_code == 401
    assert response.json() == {
        "detail": {
            "code": "authentication_required",
            "message": "金山文档登录已失效，请重新连接后再试。",
        }
    }


def test_kdocs_documents_api_exposes_info_and_safe_original_link(monkeypatch) -> None:
    service = _FakeDocumentsService()
    client = _client(monkeypatch, service)

    info = client.post(
        "/skills/integrations/kdocs/documents/info",
        json={"file_id": "file-1"},
    )
    link = client.post(
        "/skills/integrations/kdocs/documents/link",
        json={"file_id": "file-1"},
    )

    assert info.status_code == 200
    assert info.json()["key"] == "drive-1:file-1"
    assert link.status_code == 200
    assert link.json()["url"] == "https://www.kdocs.cn/l/example"
    assert service.calls == [
        ("info", {"file_id": "file-1"}),
        ("link", {"file_id": "file-1"}),
    ]
