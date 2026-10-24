"""Deletion through the local model route must stay inside the model root."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from lantai.app.routers import local_models
from lantai.local_models.model_manager import ModelManager


def test_delete_route_rejects_parent_traversal(tmp_path: Path) -> None:
    model = ModelManager()
    model._model_dir = tmp_path / "models"
    model._model_dir.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    app = FastAPI()
    app.include_router(local_models.router)
    app.state.local_model_manager = SimpleNamespace(
        get_llamacpp_server_status=lambda: {"running": False},
        remove_downloaded_model=model.remove_downloaded_model,
    )

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://audit.local",
        ) as client:
            return await client.delete(
                "/local-models/models/%2E%2E%2Fvictim",
            )

    response = asyncio.run(request())

    assert response.status_code == 400
    assert marker.read_text(encoding="utf-8") == "keep"
