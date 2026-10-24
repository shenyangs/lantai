from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from lantai.agents.memory.memory_pack import (
    MEMORY_PACK_ADOPT_CONFIRMATION,
    export_memory_pack,
)
from lantai.app.routers import workspace as workspace_router


@pytest.mark.asyncio
async def test_workspace_memory_pack_export_and_inspect(tmp_path, monkeypatch):
    (tmp_path / "MEMORY.md").write_text("portable", encoding="utf-8")
    workspace = SimpleNamespace(workspace_dir=tmp_path, agent_id="agent")

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    response = await workspace_router.export_portable_memory_pack(request=None)
    exported_path = Path(response.path)
    assert exported_path.is_file()

    upload = UploadFile(
        file=BytesIO(exported_path.read_bytes()),
        filename="memory.lantai-memory.zip",
    )
    report = await workspace_router.inspect_portable_memory_pack(upload)
    assert report["valid"] is True
    assert report["source_agent_id"] == "agent"
    assert report["categories"] == ["root_memory"]

    assert response.background is not None
    await response.background()
    assert not exported_path.exists()


@pytest.mark.asyncio
async def test_workspace_memory_pack_adopt_schedules_reload(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "MEMORY.md").write_text("source", encoding="utf-8")
    (target / "MEMORY.md").write_text("target", encoding="utf-8")
    pack = tmp_path / "memory.lantai-memory.zip"
    export_memory_pack(source, pack, agent_id="source-agent")
    workspace = SimpleNamespace(workspace_dir=target, agent_id="target-agent")
    reloads: list[str] = []

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    monkeypatch.setattr(
        workspace_router,
        "schedule_agent_reload",
        lambda _request, agent_id: reloads.append(agent_id),
    )
    upload = UploadFile(file=BytesIO(pack.read_bytes()), filename=pack.name)

    report = await workspace_router.adopt_portable_memory_pack(
        request=SimpleNamespace(),
        file=upload,
        dry_run=False,
        confirmation=MEMORY_PACK_ADOPT_CONFIRMATION,
    )

    assert report["adopted"] is True
    assert reloads == ["target-agent"]
    assert (target / "MEMORY.md").read_text(encoding="utf-8") == "source"
