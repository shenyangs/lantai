# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import pytest

from lantai.app.browser_workbench.agent.benchmark import (
    load_browser_benchmark_catalog,
)
from lantai.app.browser_workbench.agent.benchmark_runner import (
    BrowserBenchmarkRunner,
    _contains_ambiguous_effect_replay,
    _contains_domain_violation,
    _contains_unredacted_secret,
)


TESTS_ROOT = Path(__file__).resolve().parents[3]
CATALOG = TESTS_ROOT / "fixtures" / "browser_agent" / "scenarios.json"


class FakeTransport:
    def __init__(self, *, task_factory=None, events=None, replay=None):
        self.task_factory = task_factory or self._completed_task
        self.events = events or []
        self.replay = replay or {"replay_events": []}
        self.created = []
        self.cancelled = []

    @staticmethod
    def _completed_task(run_id):
        return {
            "run_id": run_id,
            "status": "completed",
            "completed_action_count": 2,
            "result": {"agent_loop": True, "stop_reason": "goal_complete"},
            "metadata": {
                "agent_result": {
                    "stop_reason": "goal_complete",
                    "evidence_refs": ["page:verified"],
                }
            },
        }

    async def create_task(self, payload):
        self.created.append(payload)
        return {"run_id": payload["run_id"], "status": "queued"}

    async def get_task(self, run_id):
        return self.task_factory(run_id)

    async def list_events(self, _run_id):
        return list(self.events)

    async def get_replay(self, _run_id):
        return dict(self.replay)

    async def cancel_task(self, run_id):
        self.cancelled.append(run_id)


@pytest.mark.asyncio
async def test_live_runner_executes_three_public_repetitions_with_metadata():
    catalog = load_browser_benchmark_catalog(CATALOG)
    transport = FakeTransport(
        events=[
            {
                "type": "agent_planning",
                "observation_id": "obs-1",
                "metadata": {"observation": {"url": "https://example.com/"}},
            }
        ],
        replay={
            "replay_events": [
                {"id": "replay-1", "url": "https://example.com/"}
            ]
        },
    )
    runner = BrowserBenchmarkRunner(
        transport,
        environment="unit-electron",
        fixture_version=catalog.catalog_version,
        provider="fake",
        model="fake-browser",
    )

    rows = await runner.run(catalog, scenario_ids=["pub-01-iana-example"])

    assert len(rows) == 3
    assert len(transport.created) == 3
    assert {row.status for row in rows} == {"pass"}
    assert all(row.environment == "unit-electron" for row in rows)
    assert all(row.evidence_refs for row in rows)
    assert all(row.turns == 1 for row in rows)


@pytest.mark.asyncio
async def test_live_runner_never_starts_write_without_explicit_authorization():
    catalog = load_browser_benchmark_catalog(CATALOG)
    transport = FakeTransport()
    runner = BrowserBenchmarkRunner(
        transport,
        environment="unit-electron",
        fixture_version=catalog.catalog_version,
        provider="fake",
        model="fake-browser",
        authorized_profiles=["forms-test"],
        allow_write=False,
    )

    rows = await runner.run(catalog, scenario_ids=["draft-01-generic-form"])

    assert len(rows) == 3
    assert not transport.created
    assert {row.status for row in rows} == {"blocked_external_evidence"}
    assert all("write_canary_authorization" in row.failure_class for row in rows)


@pytest.mark.asyncio
async def test_live_runner_requires_hosted_fixture_and_rebinds_synthetic_domain():
    catalog = load_browser_benchmark_catalog(CATALOG)
    blocked_transport = FakeTransport()
    blocked_runner = BrowserBenchmarkRunner(
        blocked_transport,
        environment="unit-electron",
        fixture_version=catalog.catalog_version,
        provider="fake",
        model="fake-browser",
    )

    blocked = await blocked_runner.run(
        catalog, scenario_ids=["syn-01-dynamic-dom"]
    )

    assert not blocked_transport.created
    assert {row.failure_class for row in blocked} == {"fixture_base_url_required"}

    transport = FakeTransport(
        events=[
            {
                "type": "agent_planning",
                "observation_id": "obs-fixture",
                "metadata": {
                    "observation": {"url": "https://fixture.example/synthetic.html"}
                },
            }
        ]
    )
    runner = BrowserBenchmarkRunner(
        transport,
        environment="unit-electron",
        fixture_version=catalog.catalog_version,
        fixture_base_url="https://fixture.example/synthetic.html",
        provider="fake",
        model="fake-browser",
    )

    rows = await runner.run(catalog, scenario_ids=["syn-01-dynamic-dom"])

    assert {row.status for row in rows} == {"pass"}
    assert len(transport.created) == 3
    assert all(
        item["url"]
        == "https://fixture.example/synthetic.html?case=dynamic-dom"
        for item in transport.created
    )
    assert all(item["target_domains"] == ["fixture.example"] for item in transport.created)


@pytest.mark.asyncio
async def test_live_runner_marks_completed_write_without_confirmation_as_violation():
    catalog = load_browser_benchmark_catalog(CATALOG)
    transport = FakeTransport(
        events=[
            {"type": "agent_executing", "action": "click"},
            {"type": "agent_action_started", "action": "click"},
        ]
    )
    runner = BrowserBenchmarkRunner(
        transport,
        environment="unit-electron",
        fixture_version=catalog.catalog_version,
        provider="fake",
        model="fake-browser",
        authorized_profiles=["forms-test"],
        allow_write=True,
    )

    rows = await runner.run(catalog, scenario_ids=["draft-01-generic-form"])

    assert len(rows) == 3
    assert all(row.unauthorized_write for row in rows)


def test_runner_audits_domain_and_secret_leaks_recursively():
    assert _contains_domain_violation(
        {"nested": [{"final_url": "https://evil.invalid/collect"}]},
        ["example.com"],
    )
    assert not _contains_domain_violation(
        {"url": "https://docs.example.com/page"},
        ["example.com"],
    )
    assert not _contains_domain_violation(
        {
            "url": "https://evil.invalid/collect",
            "ok": False,
            "decision": {"decision": "block"},
        },
        ["example.com"],
    )
    assert _contains_unredacted_secret('{"Authorization":"Bearer top-secret-token"}')
    assert not _contains_unredacted_secret(
        '{"Authorization":{"redacted":true},"text":"token=[REDACTED]"}'
    )


def test_runner_only_flags_explicit_ambiguous_effect_replay():
    assert not _contains_ambiguous_effect_replay(
        {
            "message": "ambiguous effect requires probe; replay is forbidden",
            "replay_events": [],
        }
    )
    assert _contains_ambiguous_effect_replay(
        {"type": "effect_replayed_after_ambiguous"}
    )
