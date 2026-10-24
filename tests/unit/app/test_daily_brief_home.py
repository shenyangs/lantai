# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lantai.app.daily_brief import api as api_module
from lantai.app.daily_brief.models import (
    BriefActivationResult,
    BriefGrant,
    BriefSubscription,
    DailyBriefHomeCard,
    DailyBriefHomeSnapshot,
)
from lantai.app.daily_brief.service import DailyBriefHomeService


@pytest.mark.asyncio
async def test_home_snapshot_does_not_upgrade_legacy_flags_into_authorization(
    tmp_path,
) -> None:
    reminders_dir = tmp_path / "reminders"
    reminders_dir.mkdir(parents=True)
    (reminders_dir / "reminders.json").write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {"daily_brief_enabled": True},
                "watch_topics": [
                    {
                        "id": "legacy-topic",
                        "title": "旧关注主题",
                        "enabled": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = await DailyBriefHomeService(tmp_path).get_home_snapshot()

    assert snapshot == DailyBriefHomeSnapshot(
        state="not_configured",
        visible=True,
        card=DailyBriefHomeCard(
            status_line="尚未开启",
            hint="开启后由澜台自动发现并整理",
        ),
    )


def test_home_snapshot_api_is_read_only_and_fail_closed(monkeypatch, tmp_path) -> None:
    async def fake_service(_request):
        return DailyBriefHomeService(tmp_path)

    monkeypatch.setattr(api_module, "_service_for_request", fake_service)
    app = FastAPI()
    app.include_router(api_module.router)

    response = TestClient(app).get("/daily-brief/home")

    assert response.status_code == 200
    assert response.json() == {
        "state": "not_configured",
        "visible": True,
        "card": {
            "title": "澜台简报",
            "status_line": "尚未开启",
            "hint": "开启后由澜台自动发现并整理",
            "route": "/daily-brief",
            "edition_id": None,
            "edition_revision": None,
            "edition_date": None,
            "item_count": None,
            "has_update": False,
            "is_stale": False,
        },
    }


def test_home_snapshot_schema_cannot_carry_brief_body_or_trends() -> None:
    with pytest.raises(ValueError):
        DailyBriefHomeSnapshot.model_validate(
            {
                "state": "ready",
                "visible": True,
                "card": {
                    "status_line": "2026-09-20",
                    "body": "不得在首页载入正文",
                    "trends": ["不得在首页载入趋势"],
                },
            }
        )


def test_activation_api_is_one_product_action_and_wakes_existing_worker(
    monkeypatch,
    tmp_path,
) -> None:
    activation = SimpleNamespace(
        activate=AsyncMock(
            return_value=BriefActivationResult(
                grant=BriefGrant(
                    id="grant-1",
                    topic_ids=["topic-1"],
                    public_source_discovery=True,
                    public_source_read=True,
                    cloud_processing="public_only",
                    search_provider="lantai_auto",
                    model_provider="auto",
                ),
                topic_ids=["topic-1"],
                discovered_topic_ids=["topic-1"],
                subscription=BriefSubscription(
                    id="subscription-1",
                    grant_id="grant-1",
                    grant_revision=1,
                    topic_revisions={"topic-1": 1},
                ),
            )
        )
    )
    wake = MagicMock()

    async def fake_workspace(_request):
        return SimpleNamespace(
            workspace_dir=tmp_path,
            agent_id="agent-1",
            daily_brief=SimpleNamespace(wake=wake),
        )

    monkeypatch.setattr(api_module, "get_agent_for_request", fake_workspace)
    monkeypatch.setattr(
        api_module,
        "DailyBriefActivationService",
        lambda *_args, **_kwargs: activation,
    )
    app = FastAPI()
    app.include_router(api_module.router)

    response = TestClient(app).post(
        "/daily-brief/activate",
        json={
            "explicit_consent": True,
            "mode": "daily",
            "topic_ids": [],
            "use_memory_clues": True,
            "memory_context_processing_consent": True,
            "timezone": "Asia/Shanghai",
            "local_time": "08:00:00",
            "home_visible": True,
            "native_notifications": False,
            "action_id": "activate-daily-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["topic_ids"] == ["topic-1"]
    activation.activate.assert_awaited_once()
    wake.assert_called_once()
