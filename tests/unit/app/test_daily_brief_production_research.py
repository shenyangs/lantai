# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from lantai.app.daily_brief.models import BriefBudget
from lantai.app.daily_brief.production_research import (
    BriefSynthesis,
    GuanlanFirstBriefSearch,
    ProductionBriefResearchAdapter,
    SynthesizedBriefItem,
    SynthesizedBriefTrend,
    _response_payload,
)
from lantai.app.daily_brief.research import (
    ApprovedContextFragment,
    BriefResearchEnvelope,
    SafeSourceRead,
)
from lantai.app.structured_utility import (
    StructuredTaskValidationError,
    StructuredUtilityResult,
)
from lantai.app.utility_model import (
    UtilityModelPolicyError,
    UtilityTaskTimeoutError,
)
from lantai.runtime.tool_response import TextBlock, ToolResponse


NOW = datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_chat_paid_web_setting_does_not_change_brief_search_costs(monkeypatch):
    from lantai.agents.tools import web

    async def paid_search(*_args, **_kwargs):
        pytest.fail("A brief needs its own authorization for paid Web search")

    async def baseline_request(_url, **_kwargs):
        return SimpleNamespace(body=b'<rss><channel><item><title>Public</title>'
            b'<link>https://example.com/news</link></item></channel></rss>')

    monkeypatch.setattr(web, "get_web_search_store", lambda: SimpleNamespace(
        status=lambda: SimpleNamespace(enabled=True), load_api_key=lambda: "sk-chat-web"))
    monkeypatch.setattr(web.DeepSeekWebClient, "search", paid_search)
    monkeypatch.setattr(web, "_request_public_document", baseline_request)
    adapter = ProductionBriefResearchAdapter(agent_id="fixture")
    for search in (adapter._search, GuanlanFirstBriefSearch(mcp_manager=None)):
        result = _response_payload(await search("public update", limit=1))
        assert result["provider"] == "bing_rss"
        assert result["result_count"] == 1


@pytest.mark.asyncio
async def test_usage_is_durable_before_each_external_call(tmp_path):
    from tests.unit.app.test_daily_brief_recovery import _preview
    from lantai.app.daily_brief.repo import DailyBriefRepository
    from lantai.app.daily_brief.research import DailyBriefResearchRunner
    service, cycle = await _preview(tmp_path)
    repo = DailyBriefRepository(tmp_path)
    await repo.update(lambda data: data.model_copy(update={"grants": [
        grant.model_copy(update={"search_provider": "lantai_auto", "model_provider": "auto",
            "budget": BriefBudget(max_searches=1, max_source_reads=1, max_model_tokens=4000)})
        for grant in data.grants]}))
    entered_model = asyncio.Event()
    async def search(_query, *, limit):
        records = (await repo.load()).costs
        assert records and records[0].search_count == 1
        return _search_response(status="ok", results=[{"url": "https://example.com/release"}])
    class Reader:
        async def read(self, url, *, max_chars):
            assert (await repo.load()).costs[0].source_read_count == 1
            return SafeSourceRead(original_url=url, final_url=url, title="发布说明",
                text="有依据的公开变化。" * 100, content_type="text/html", status="usable",
                quality_score=90, can_cite_as_page_body=True, signals=[])
    async def task(**_kwargs):
        records = (await repo.load()).costs
        assert len(records) == 1
        assert records[0].model_usage_status == "unknown"
        entered_model.set()
        await asyncio.Event().wait()
    adapter = ProductionBriefResearchAdapter(agent_id="fixture", search=search,
        source_reader=Reader(), structured_task=task, usage_popper=lambda _: None)
    running = asyncio.create_task(DailyBriefResearchRunner(tmp_path, adapter=adapter).run_preview(cycle.id))
    try:
        await asyncio.wait_for(entered_model.wait(), timeout=2)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    store = await repo.load()
    assert len(store.costs) == 1
    assert store.costs[0].search_count == store.costs[0].source_read_count == 1
    assert store.costs[0].model_usage_status == "unknown"
    from lantai.app.daily_brief.service import ScopeFenceFailed
    with pytest.raises(ScopeFenceFailed):
        await service.retry_cycle(cycle.id, action_id="again", explicit_consent=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_new_change", [False, True])
async def test_cross_url_event_history_suppresses_repetition_but_keeps_new_change(has_new_change):
    previous = {"topic_id": "topic-1", "edition_date": "2026-09-19",
        "title": "公开功能发布", "fact": "产品已发布导出功能。",
        "source_urls": ["https://example.com/original"]}
    async def search(_query, *, limit):
        return _search_response(status="ok", results=[{"url": "https://example.org/syndicated"}])
    class Reader:
        async def read(self, url, *, max_chars):
            return SafeSourceRead(original_url=url, final_url=url, title="后续报道",
                text="产品已发布导出功能。" + ("新增团队权限支持。" if has_new_change else ""),
                content_type="text/html", status="usable", quality_score=90,
                can_cite_as_page_body=True, signals=[])
    async def task(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        assert payload["previous_events"] == [previous]
        assert "同一事件" in kwargs["system_prompt"]
        items = [SynthesizedBriefItem(topic_id="topic-1", title="新增团队权限",
            fact="导出功能新增团队权限支持。", relevance="影响权限管理",
            evidence_ids=[payload["sources"][0]["evidence_id"]])] if has_new_change else []
        return StructuredUtilityResult(value=BriefSynthesis(items=items), model_label="local/model", repaired=False)
    envelope = _auto_envelope().model_copy(update={"previous_events": [previous]})
    adapter = ProductionBriefResearchAdapter(agent_id="fixture", search=search,
        source_reader=Reader(), structured_task=task,
        usage_popper=lambda _: {"total_tokens": 200, "cost_cny": 0})
    result = await adapter.run(envelope)
    assert result.publish_request.outcome == ("ready" if has_new_change else "no_material_update")
    assert len(result.publish_request.items) == int(has_new_change)


@pytest.mark.asyncio
async def test_incremental_window_reads_updated_urls_but_does_not_repeat_unchanged_sources():
    from lantai.app.daily_brief.production_research import _content_hash
    queries = []
    body = "公开发布说明记录了新功能。"
    async def search(query, *, limit):
        queries.append(query)
        return _search_response(status="ok", results=[{"url": "https://example.com/release"}])
    class Reader:
        async def read(self, url, *, max_chars):
            return SafeSourceRead(original_url=url, final_url=url, title="公开发布",
                text=body, content_type="text/html", status="usable", quality_score=90,
                can_cite_as_page_body=True, signals=[])
    async def task(**kwargs):
        pytest.fail("unchanged public evidence must not be synthesized again")
    envelope = BriefResearchEnvelope.model_validate({
        **_auto_envelope().model_dump(),
        "checked_since": datetime(2026, 9, 19, 2, 30, tzinfo=timezone.utc),
        "previous_sources": [{"url": "https://example.com/release", "content_hash": _content_hash(body)}],
    })
    result = await ProductionBriefResearchAdapter(agent_id="agent-1", search=search,
        source_reader=Reader(), structured_task=task, now_provider=lambda: NOW).run(envelope)
    assert "after:2026-09-19" in queries[0] and "before:2026-09-21" in queries[0]
    assert result.publish_request.outcome == "no_material_update"
    assert result.cost.source_read_count == 1


@pytest.mark.asyncio
async def test_long_chinese_source_fits_budget_and_failed_usage_is_preserved():
    seen = []
    recorded = []
    async def search(_query, *, limit):
        return _search_response(status="ok", results=[{"url": "https://example.com/release"}])
    class Reader:
        async def read(self, url, *, max_chars):
            return SafeSourceRead(original_url=url, final_url=url, title="发布说明",
                text="这是有依据的发布变化。" * 700, content_type="text/html", status="usable",
                quality_score=90, can_cite_as_page_body=True, signals=[])
    async def task(**kwargs):
        seen.append(kwargs)
        raise StructuredTaskValidationError("bad result after charged work")
    pop_count = 0
    def usage(_session):
        nonlocal pop_count
        pop_count += 1
        return {"total_tokens": 300, "cost_cny": 0} if pop_count == 2 else None
    async def record(cost):
        recorded.append(cost)
    adapter = ProductionBriefResearchAdapter(agent_id="agent-1", search=search,
        source_reader=Reader(), structured_task=task, usage_popper=usage)
    with pytest.raises(RuntimeError, match="模型"):
        await adapter.run_with_usage(_auto_envelope().model_copy(update={"budget": BriefBudget(
            max_searches=1, max_source_reads=2, max_model_tokens=4000, max_cost_usd=0)}), record)
    assert len(seen) == 1
    assert len(seen[0]["user_prompt"].encode("utf-8")) // 3 + seen[0]["max_output_tokens"] < 4000
    assert len({record.id for record in recorded}) == 1
    assert recorded[0].search_count == 1 and recorded[0].source_read_count == 0
    assert recorded[-1].search_count == recorded[-1].source_read_count == 1
    assert recorded[-1].model_tokens == 300


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [{"total_tokens": 300, "cost_cny": 0.72}, None])
async def test_attempt_ledger_preserves_currency_and_unknown_model_usage(observed):
    recorded = []
    async def search(_query, *, limit):
        return _search_response(status="ok", results=[{"url": "https://example.com/release"}])
    class Reader:
        async def read(self, url, *, max_chars):
            return SafeSourceRead(original_url=url, final_url=url, title="发布说明",
                text="有依据的变化。" * 100, content_type="text/html", status="usable",
                quality_score=90, can_cite_as_page_body=True, signals=[])
    async def task(**kwargs):
        return StructuredUtilityResult(value=BriefSynthesis(), model_label="local/model", repaired=False)
    pop_count = 0
    def usage(_session):
        nonlocal pop_count
        pop_count += 1
        return observed if pop_count == 2 else None
    async def record(cost):
        recorded.append(cost)
    adapter = ProductionBriefResearchAdapter(agent_id="agent-1", search=search,
        source_reader=Reader(), structured_task=task, usage_popper=usage)
    with pytest.raises(RuntimeError):
        await adapter.run_with_usage(_auto_envelope(), record)
    assert len({record.id for record in recorded}) == 1
    assert any(record.model_usage_status == "unknown" for record in recorded)
    if observed is None:
        assert recorded[-1].model_usage_status == "unknown"
    else:
        assert recorded[-1].cost_cny == 0.72
        assert recorded[-1].model_tokens == 300
        assert recorded[-1].model_usage_status == "recorded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["policy", "unavailable", "timeout", "invalid_output"],
)
async def test_synthesis_model_failures_are_actionable_and_release_usage(
    failure,
):
    async def search(_query: str, *, limit: int):
        return _search_response(
            status="ok",
            results=[{"url": "https://example.com/release"}],
        )

    class Reader:
        async def read(self, url: str, *, max_chars: int):
            return SafeSourceRead(
                original_url=url,
                final_url=url,
                title="公开发布",
                text="公开发布说明记录了新功能。",
                content_type="text/html",
                status="usable",
                quality_score=90,
                can_cite_as_page_body=True,
                signals=[],
            )

    async def structured_task(**kwargs):
        assert kwargs["require_no_api_cost"] is True
        if failure == "policy":
            raise UtilityModelPolicyError(
                "utility model must be local or explicitly free"
            )
        if failure == "timeout":
            raise UtilityTaskTimeoutError(phase="total", timeout_seconds=12)
        if failure == "invalid_output":
            raise StructuredTaskValidationError(
                "provider-specific private output"
            )
        return None

    usage_sessions = []

    def usage_popper(session_id):
        usage_sessions.append(session_id)
        return None

    adapter = ProductionBriefResearchAdapter(
        agent_id="agent-1",
        search=search,
        source_reader=Reader(),
        structured_task=structured_task,
        usage_popper=usage_popper,
        now_provider=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="模型.*重试"):
        await adapter.run(_auto_envelope())
    assert usage_sessions == ["daily-brief:cycle-1", "daily-brief:cycle-1"]


def _envelope() -> BriefResearchEnvelope:
    return BriefResearchEnvelope(
        cycle_id="cycle-1",
        logical_key="preview:grant-1:2026-09-20",
        grant_id="grant-1",
        grant_revision=1,
        local_date=date(2026, 9, 20),
        timezone="Asia/Shanghai",
        topics=[
            {
                "id": "topic-1",
                "revision": 1,
                "title": "AI 办公产品",
                "purpose": "追踪可控的持续研究能力",
                "scope_refs": ["public:web"],
                "evidence_refs": [],
                "exclusions": ["rumor-only"],
            }
        ],
        public_source_discovery=True,
        public_source_read=True,
        cloud_processing="public_only",
        search_provider="bing_rss",
        model_provider="local-provider",
        budget=BriefBudget(
            max_searches=1,
            max_source_reads=2,
            max_model_tokens=2_000,
            max_cost_usd=0,
        ),
        approved_context=[],
    )


def _auto_envelope() -> BriefResearchEnvelope:
    return _envelope().model_copy(
        update={"search_provider": "lantai_auto", "model_provider": "auto"}
    )


def _two_topic_envelope() -> BriefResearchEnvelope:
    return _envelope().model_copy(
        update={
            "topics": [
                _envelope().topics[0],
                {
                    "id": "topic-2",
                    "revision": 1,
                    "title": "本地模型",
                    "purpose": "追踪本地模型运行能力",
                    "scope_refs": ["public:web"],
                    "evidence_refs": [],
                    "exclusions": [],
                },
            ],
            "budget": BriefBudget(
                max_searches=2,
                max_source_reads=2,
                max_model_tokens=2_000,
                max_cost_usd=0,
            ),
        }
    )


def _search_response(*, status: str, results: list[dict] | None = None) -> ToolResponse:
    payload = {
        "schema_version": "lantai_web_search_v1",
        "status": status,
        "provider": "bing_rss",
        "query": "AI 办公产品",
        "result_count": len(results or []),
        "results": results or [],
    }
    return ToolResponse(
        content=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))]
    )


@pytest.mark.asyncio
async def test_guanlan_first_search_uses_reviewed_results_without_exposing_provider_choice():
    calls: list[tuple[str, dict]] = []

    class Client:
        is_connected = True

        async def call_tool(self, name: str, arguments: dict):
            calls.append((name, arguments))
            payload = {
                "results": [
                    {
                        "title": "官方发布",
                        "url": "https://example.com/release",
                        "snippet": "候选摘要",
                    }
                ],
                "lantai_review": {"decision": "answer"},
            }
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))],
                isError=False,
            )

    class Manager:
        async def get_client(self, key: str):
            assert key == "guanlan"
            return Client()

    async def fallback(_query: str, *, limit: int):
        raise AssertionError(f"reviewed Guanlan results should not fall back: {limit}")

    search = GuanlanFirstBriefSearch(mcp_manager=Manager(), fallback=fallback)
    response = await search("AI 办公", limit=2)
    payload = _response_payload(response)

    assert calls == [
        (
            "guanlan_search",
            {"query": "AI 办公", "limit": 80},
        )
    ]
    assert payload["provider"] == "guanlan"
    assert payload["results"][0]["url"] == "https://example.com/release"


@pytest.mark.asyncio
async def test_guanlan_quality_failure_falls_back_to_lantai_web_search():
    class Client:
        is_connected = True

        async def call_tool(self, _name: str, _arguments: dict):
            payload = {
                "results": [{"url": "https://weak.example/item"}],
                "lantai_review": {"decision": "repair"},
            }
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(payload))],
                isError=False,
            )

    class Manager:
        async def get_client(self, _key: str):
            return Client()

    fallback_calls = 0

    async def fallback(_query: str, *, limit: int):
        nonlocal fallback_calls
        fallback_calls += 1
        assert limit == 3
        return _search_response(status="empty")

    search = GuanlanFirstBriefSearch(mcp_manager=Manager(), fallback=fallback)
    payload = _response_payload(await search("AI 办公", limit=3))

    assert fallback_calls == 1
    assert payload["provider"] == "bing_rss"


def test_auto_provider_envelope_and_guanlan_payload_are_supported():
    ProductionBriefResearchAdapter._validate_envelope(_auto_envelope())
    ProductionBriefResearchAdapter._validate_search_payload(
        {
            "schema_version": "lantai_web_search_v1",
            "status": "ok",
            "provider": "guanlan",
            "results": [],
        }
    )


@pytest.mark.asyncio
async def test_completed_empty_search_is_an_honest_no_update_without_model_call():
    model_calls = 0

    async def search(_query: str, *, limit: int):
        assert limit == 2
        return _search_response(status="empty")

    async def structured_task(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("empty discovery must not call a model")

    adapter = ProductionBriefResearchAdapter(
        agent_id="agent-1",
        search=search,
        structured_task=structured_task,
        now_provider=lambda: NOW,
    )
    result = await adapter.run(_envelope())

    assert result.publish_request.outcome == "no_material_update"
    assert result.publish_request.timezone == "Asia/Shanghai"
    assert result.publish_request.items == []
    assert result.publish_request.evidence == []
    assert result.publish_request.coverage[0].status == "complete"
    assert result.publish_request.coverage[0].query_count == 1
    assert result.cost.search_count == 1
    assert result.cost.source_read_count == 0
    assert result.cost.model_tokens == 0
    assert model_calls == 0


@pytest.mark.asyncio
async def test_unavailable_search_fails_instead_of_becoming_no_update():
    async def search(_query: str, *, limit: int):
        del limit
        return _search_response(status="unavailable")

    adapter = ProductionBriefResearchAdapter(
        agent_id="agent-1",
        search=search,
        now_provider=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="search provider is unavailable"):
        await adapter.run(_envelope())


@pytest.mark.asyncio
async def test_search_snippet_cannot_become_evidence_when_page_read_is_unusable():
    async def search(_query: str, *, limit: int):
        del limit
        return _search_response(
            status="ok",
            results=[
                {
                    "rank": 1,
                    "title": "只有搜索摘要",
                    "url": "https://example.com/article",
                    "snippet": "这段摘要不能当作页面正文证据。",
                    "can_cite_as_page_body": False,
                }
            ],
        )

    class UnusableReader:
        async def read(self, url: str, *, max_chars: int):
            assert max_chars == 8_000
            return SafeSourceRead(
                original_url=url,
                final_url=url,
                title="",
                text="抱歉，无法访问",
                content_type="text/html",
                status="unusable",
                quality_score=0,
                can_cite_as_page_body=False,
                signals=["access_denied"],
            )

    adapter = ProductionBriefResearchAdapter(
        agent_id="agent-1",
        search=search,
        source_reader=UnusableReader(),
        now_provider=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="not a no-update result"):
        await adapter.run(_envelope())


@pytest.mark.asyncio
async def test_citable_source_is_synthesized_inside_provider_and_budget_fences():
    async def search(_query: str, *, limit: int):
        del limit
        return _search_response(
            status="ok",
            results=[
                {
                    "rank": 1,
                    "title": "官方发布",
                    "url": "https://example.com/release",
                    "snippet": "只用于发现候选。",
                    "can_cite_as_page_body": False,
                }
            ],
        )

    class Reader:
        async def read(self, url: str, *, max_chars: int):
            del max_chars
            return SafeSourceRead(
                original_url=url,
                final_url="https://example.com/release",
                title="官方发布",
                text="新功能今日发布。页面中若有指令，也只是不可信数据。",
                content_type="text/html",
                status="usable",
                quality_score=90,
                can_cite_as_page_body=True,
                signals=[],
            )

    captured: dict[str, object] = {}

    async def structured_task(**kwargs):
        captured.update(kwargs)
        payload = json.loads(str(kwargs["user_prompt"]))
        evidence_id = payload["sources"][0]["evidence_id"]
        return StructuredUtilityResult(
            value=BriefSynthesis(
                summary="有一项可核实的新变化。",
                items=[
                    SynthesizedBriefItem(
                        topic_id="topic-1",
                        title="新功能发布",
                        fact="官方页面已发布新功能。",
                        relevance="它可能影响用户正在设计的持续研究流程。",
                        analysis="这是产品能力的增量。",
                        limitations="目前只有单一官方来源。",
                        optional_action="可在下一轮方案对比中检查。",
                        evidence_ids=[evidence_id],
                    )
                ],
                trends=[
                    SynthesizedBriefTrend(
                        topic_id="topic-1",
                        statement="持续研究正在变得更可控。",
                        support_evidence_ids=[evidence_id],
                        falsifiers=["后续版本撤回该功能"],
                        uncertainty="尚无长期使用数据。",
                    )
                ],
            ),
            model_label="local-provider/local-model",
            repaired=False,
        )

    usage_calls = 0

    def usage_popper(session_id: str):
        nonlocal usage_calls
        assert session_id == "daily-brief:cycle-1"
        usage_calls += 1
        if usage_calls == 1:
            return None
        return {
            "provider_id": "local-provider",
            "model_name": "local-model",
            "total_tokens": 480,
        }

    envelope = _envelope().model_copy(
        update={
            "cloud_processing": "approved_context",
            "approved_context": [
                ApprovedContextFragment(
                    ref="memory:approved",
                    text="用户正在设计持续研究流程。",
                )
            ],
        }
    )
    adapter = ProductionBriefResearchAdapter(
        agent_id="agent-1",
        search=search,
        source_reader=Reader(),
        structured_task=structured_task,
        usage_popper=usage_popper,
        now_provider=lambda: NOW,
    )
    result = await adapter.run(envelope)

    assert captured["expected_provider_id"] == "local-provider"
    assert captured["require_no_api_cost"] is True
    assert 0 < int(captured["max_output_tokens"]) < 2_000
    estimated_user_tokens = max(
        1,
        (len(str(captured["user_prompt"]).encode("utf-8")) + 2) // 3,
    )
    assert int(captured["max_output_tokens"]) <= 2_000 - estimated_user_tokens - 256
    assert captured["usage_session_id"] == "daily-brief:cycle-1"
    assert result.publish_request.outcome == "ready"
    assert result.publish_request.items[0].private_dependency_refs == [
        "memory:approved"
    ]
    assert result.publish_request.private_dependency_refs == ["memory:approved"]
    assert result.publish_request.evidence[0].read_level == "source_page"
    assert result.cost.model_tokens == 480
    assert result.cost.cost_usd == 0


@pytest.mark.asyncio
async def test_source_read_budget_is_shared_across_authorized_topics():
    envelope = _two_topic_envelope()

    async def search(query: str, *, limit: int):
        assert limit == 2
        slug = "topic-2" if "本地模型" in query else "topic-1"
        return _search_response(
            status="ok",
            results=[
                {"url": f"https://example.com/{slug}/one"},
                {"url": f"https://example.com/{slug}/two"},
            ],
        )

    read_urls: list[str] = []

    class Reader:
        async def read(self, url: str, *, max_chars: int):
            del max_chars
            read_urls.append(url)
            return SafeSourceRead(
                original_url=url,
                final_url=url,
                title=url.rsplit("/", 1)[-1],
                text=f"{url} 的可引用正文。",
                content_type="text/html",
                status="usable",
                quality_score=90,
                can_cite_as_page_body=True,
                signals=[],
            )

    async def structured_task(**_kwargs):
        return StructuredUtilityResult(
            value=BriefSynthesis(summary="已完成检查。"),
            model_label="local-provider/local-model",
            repaired=False,
        )

    usage_calls = 0

    def usage_popper(_session_id: str):
        nonlocal usage_calls
        usage_calls += 1
        return None if usage_calls == 1 else {"total_tokens": 300}

    adapter = ProductionBriefResearchAdapter(
        agent_id="agent-1",
        search=search,
        source_reader=Reader(),
        structured_task=structured_task,
        usage_popper=usage_popper,
        now_provider=lambda: NOW,
    )
    result = await adapter.run(envelope)

    assert read_urls == [
        "https://example.com/topic-1/one",
        "https://example.com/topic-2/one",
    ]
    assert [entry.source_read_count for entry in result.publish_request.coverage] == [
        1,
        1,
    ]


@pytest.mark.asyncio
async def test_synthesis_cannot_cite_evidence_discovered_for_another_topic():
    envelope = _two_topic_envelope()

    async def search(query: str, *, limit: int):
        del limit
        slug = "topic-2" if "本地模型" in query else "topic-1"
        return _search_response(
            status="ok",
            results=[{"url": f"https://example.com/{slug}"}],
        )

    class Reader:
        async def read(self, url: str, *, max_chars: int):
            del max_chars
            return SafeSourceRead(
                original_url=url,
                final_url=url,
                title=url.rsplit("/", 1)[-1],
                text=f"{url} 的可引用正文。",
                content_type="text/html",
                status="usable",
                quality_score=90,
                can_cite_as_page_body=True,
                signals=[],
            )

    async def structured_task(**kwargs):
        payload = json.loads(str(kwargs["user_prompt"]))
        topic_2_evidence = next(
            source["evidence_id"]
            for source in payload["sources"]
            if source["topic_ids"] == ["topic-2"]
        )
        return StructuredUtilityResult(
            value=BriefSynthesis(
                items=[
                    SynthesizedBriefItem(
                        topic_id="topic-1",
                        title="越界引用",
                        fact="错误地借用了另一主题的材料。",
                        relevance="不应被接受。",
                        evidence_ids=[topic_2_evidence],
                    )
                ]
            ),
            model_label="local-provider/local-model",
            repaired=False,
        )

    usage_calls = 0

    def usage_popper(_session_id: str):
        nonlocal usage_calls
        usage_calls += 1
        return None if usage_calls == 1 else {"total_tokens": 300}

    adapter = ProductionBriefResearchAdapter(
        agent_id="agent-1",
        search=search,
        source_reader=Reader(),
        structured_task=structured_task,
        usage_popper=usage_popper,
        now_provider=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="outside its topic scope"):
        await adapter.run(envelope)


@pytest.mark.asyncio
async def test_one_canonical_source_can_support_each_topic_that_read_it():
    envelope = _two_topic_envelope()

    async def search(_query: str, *, limit: int):
        del limit
        return _search_response(
            status="ok",
            results=[{"url": "https://example.com/shared"}],
        )

    class Reader:
        async def read(self, url: str, *, max_chars: int):
            del max_chars
            return SafeSourceRead(
                original_url=url,
                final_url=url,
                title="共享来源",
                text="同一份正文与两个已授权主题都相关。",
                content_type="text/html",
                status="usable",
                quality_score=90,
                can_cite_as_page_body=True,
                signals=[],
            )

    async def structured_task(**kwargs):
        payload = json.loads(str(kwargs["user_prompt"]))
        assert len(payload["sources"]) == 1
        assert payload["sources"][0]["topic_ids"] == ["topic-1", "topic-2"]
        evidence_id = payload["sources"][0]["evidence_id"]
        return StructuredUtilityResult(
            value=BriefSynthesis(
                items=[
                    SynthesizedBriefItem(
                        topic_id=topic_id,
                        title=f"{topic_id} 的发现",
                        fact="共享来源支持该主题的事实。",
                        relevance="它与已确认主题相关。",
                        evidence_ids=[evidence_id],
                    )
                    for topic_id in ("topic-1", "topic-2")
                ]
            ),
            model_label="local-provider/local-model",
            repaired=False,
        )

    usage_calls = 0

    def usage_popper(_session_id: str):
        nonlocal usage_calls
        usage_calls += 1
        return None if usage_calls == 1 else {"total_tokens": 300}

    adapter = ProductionBriefResearchAdapter(
        agent_id="agent-1",
        search=search,
        source_reader=Reader(),
        structured_task=structured_task,
        usage_popper=usage_popper,
        now_provider=lambda: NOW,
    )
    result = await adapter.run(envelope)

    assert len(result.publish_request.evidence) == 1
    assert {item.topic_id for item in result.publish_request.items} == {
        "topic-1",
        "topic-2",
    }


@pytest.mark.asyncio
async def test_public_only_envelope_rejects_injected_private_context():
    envelope = _envelope().model_copy(
        update={
            "approved_context": [
                ApprovedContextFragment(ref="memory:not-approved", text="private")
            ]
        }
    )
    adapter = ProductionBriefResearchAdapter(
        agent_id="agent-1",
        now_provider=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="private context"):
        await adapter.run(envelope)
