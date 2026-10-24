# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.capabilities.evolution import build_evolution_proposals
from lantai.app.capabilities.api import task_runs_router
from lantai.app.capabilities import api as capabilities_api
from lantai.app.capabilities.models import Capability, TaskRun
from lantai.app.capabilities.repo import CapabilityRepository
from lantai.app.capabilities.service import CapabilityService


def _run(run_id: str, title: str, minutes: int) -> TaskRun:
    return TaskRun(
        id=run_id,
        agent_id="default",
        source="chat",
        title=title,
        user_intent=title,
        status="succeeded",
        started_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
        + timedelta(minutes=minutes),
    )


def test_evolution_proposals_require_repeated_evidence_and_dedupe_capabilities() -> None:
    existing = Capability(
        id="cap-weekly",
        name="生成项目周报",
        description="稳定生成项目周报",
        enabled=True,
    )

    proposals = build_evolution_proposals(
        [
            _run("run-1", "生成项目周报", 1),
            _run("run-2", "生成项目周报", 2),
            _run("run-one-off", "只做一次的海报", 3),
        ],
        [existing],
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.repeat_count == 2
    assert [item.run_id for item in proposal.evidence] == ["run-2", "run-1"]
    assert proposal.recommended_form == "extend_existing"
    assert proposal.matched_capability_id == "cap-weekly"
    assert proposal.status == "pending"


@pytest.mark.parametrize(
    "title",
    [
        "问候老沈",
        "你们商量写一首北京相关的现代诗",
        "协作写一首现代诗",
        "协作，各自提出一句关于北京的现代诗",
        "一首宜兴相关的现代诗写作",
        "Tell me a joke",
        "Write me a modern poem",
    ],
)
def test_evolution_proposals_exclude_low_value_chat_repetition(title: str) -> None:
    proposals = build_evolution_proposals(
        [_run("run-1", title, 1), _run("run-2", title, 2)],
        [],
    )

    assert proposals == []


@pytest.mark.asyncio
async def test_evolution_review_can_accept_and_recoverably_rollback(
    tmp_path: Path,
) -> None:
    repo = CapabilityRepository(tmp_path)
    service = CapabilityService(repo)
    capability = await service.create_capability(
        Capability(id="cap-1", name="生成项目周报", enabled=True),
    )
    proposal = build_evolution_proposals(
        [
            _run("run-1", "生成项目周报", 1),
            _run("run-2", "生成项目周报", 2),
        ],
        [capability],
    )[0]

    accepted = await service.review_evolution_proposal(
        proposal,
        status="accepted",
        capability_id=capability.id,
    )
    rolled_back = await service.review_evolution_proposal(
        accepted,
        status="rolled_back",
    )

    saved_capability = await service.get_capability(capability.id)
    assert accepted.status == "accepted"
    assert rolled_back.status == "rolled_back"
    assert saved_capability is not None
    assert saved_capability.enabled is False
    assert saved_capability.version == 2


@pytest.mark.asyncio
async def test_evolution_api_exposes_review_only_draft_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = CapabilityRepository(tmp_path)
    service = CapabilityService(repo)
    runs = [
        _run("run-1", "生成项目周报", 1),
        _run("run-2", "生成项目周报", 2),
    ]

    async def fake_all_task_runs(_request):
        return SimpleNamespace(agent_id="default"), service, runs

    monkeypatch.setattr(capabilities_api, "_all_task_runs", fake_all_task_runs)
    app = FastAPI()
    app.include_router(task_runs_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        listed = await client.get("/task-runs/evolution-proposals")
        proposal = listed.json()[0]
        drafted = await client.post(
            f"/task-runs/evolution-proposals/{proposal['id']}/draft",
        )
        dismissed = await client.patch(
            f"/task-runs/evolution-proposals/{proposal['id']}",
            json={"status": "dismissed"},
        )

    assert listed.status_code == 200
    assert drafted.status_code == 200
    assert drafted.json()["capability"]["enabled"] is False
    assert drafted.json()["evolution_proposal_id"] == proposal["id"]
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "dismissed"
