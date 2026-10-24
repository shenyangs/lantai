# -*- coding: utf-8 -*-
"""Behavior locks for isolated background utility intelligence."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from lantai.app import utility_intelligence, utility_model
from lantai.app.utility_intelligence import (
    ArtifactClassificationRequest,
    ArtifactClassificationResponse,
    _ResolvedModel,
    _extract_json_object,
    _validate_artifacts,
    _validate_research_advisory,
    classify_text_artifacts,
)
from lantai.app.utility_model import (
    ResolvedUtilityModel,
    UtilityTaskTimeoutError,
    consume_model_response,
    run_active_model_text_task,
    run_memory_text_task,
    run_utility_content_task,
    run_utility_text_task,
)
from lantai.config.config import Config, ModelSlotConfig, UtilityIntelligenceConfig
from lantai.app.utility_model import utility_capability_matrix
from lantai.config.utils import load_config, save_config


UtilityMode = Literal["follow_active", "dedicated", "disabled"]


def test_utility_message_supports_formatter_serialization() -> None:
    message = utility_model._UtilityMessage(
        role="user",
        content=[{"type": "text", "text": "检查演示文稿"}],
    )

    assert message.to_dict() == {
        "name": "",
        "role": "user",
        "content": [{"type": "text", "text": "检查演示文稿"}],
    }


def _root_config(
    *,
    mode: UtilityMode = "follow_active",
    timeout_seconds: float = 12.0,
    background_processing_enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        utility_intelligence=UtilityIntelligenceConfig(
            mode=mode,
            timeout_seconds=timeout_seconds,
            background_processing_enabled=background_processing_enabled,
        ),
    )


def test_extract_artifact_json_tolerates_model_wrapper_text() -> None:
    payload = _extract_json_object(
        'Result follows:\n```json\n{"artifacts":[]}\n```',
    )

    assert payload == {"artifacts": []}


def test_validate_artifacts_keeps_only_exact_unique_work_boundaries() -> None:
    source = """好，再来一首五言绝句

《溪边独坐》

流水过青苔，
斜阳入酒杯。
山风知我意，
先遣落花来。

——押“苔、杯、来”韵。
还要再来一首吗？"""
    work = """《溪边独坐》

流水过青苔，
斜阳入酒杯。
山风知我意，
先遣落花来。"""

    artifacts = _validate_artifacts(
        source,
        {
            "artifacts": [
                {
                    "kind": "creative",
                    "title": "溪边独坐",
                    "source_text": work,
                },
            ],
        },
    )

    assert len(artifacts) == 1
    assert artifacts[0].source_text == work
    assert artifacts[0].start == source.index(work)
    assert artifacts[0].end == source.index(work) + len(work)


def test_validate_artifacts_resolves_compact_unique_markers() -> None:
    source = """开场说明。

你不必再教我一遍。
那些反复说过的偏好，我都记得。
然后 quietly 把活干完的搭档。

要不要再写一首？"""
    work = """你不必再教我一遍。
那些反复说过的偏好，我都记得。
然后 quietly 把活干完的搭档。"""

    artifacts = _validate_artifacts(
        source,
        {
            "artifacts": [
                {
                    "kind": "creative",
                    "title": "灵犀",
                    "start_marker": "你不必再教我一遍。",
                    "end_marker": "然后 quietly 把活干完的搭档。",
                },
            ],
        },
    )

    assert [item.source_text for item in artifacts] == [work]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "artifacts": [
                {"kind": "analysis", "title": "错误类型", "source_text": "风来。"},
            ],
        },
        {
            "artifacts": [
                {"kind": "creative", "title": "", "source_text": "风来。"},
            ],
        },
        {
            "artifacts": [
                {
                    "kind": "creative",
                    "title": "代码",
                    "source_text": "```text\n风来。\n```",
                },
            ],
        },
        {
            "artifacts": [
                {
                    "kind": "creative",
                    "title": "倒置边界",
                    "start_marker": "后来。",
                    "end_marker": "从前。",
                },
            ],
        },
    ],
)
def test_validate_artifacts_rejects_untrusted_model_payloads(
    payload: dict,
) -> None:
    assert _validate_artifacts("从前。\n后来。", payload) == []


def test_validate_artifacts_rejects_ambiguous_repeated_source() -> None:
    payload = {
        "artifacts": [
            {"kind": "creative", "title": "无题", "source_text": "风来。"},
        ],
    }

    assert _validate_artifacts("风来。\n\n风来。", payload) == []


def test_validate_research_advisory_keeps_one_specific_plain_text_note() -> None:
    advisory = _validate_research_advisory(
        {
            "advisory": {
                "label": "注意",
                "body": (
                    "  竞品价格来自 2026 年 8 月官方页面，"
                    "发布前需重新核对套餐和地区限制。  "
                ),
            },
        },
    )

    assert advisory is not None
    assert advisory.label == "注意"
    assert advisory.body == (
        "竞品价格来自 2026 年 8 月官方页面，"
        "发布前需重新核对套餐和地区限制。"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"advisory": None},
        {"advisory": {"label": "警告", "body": "请注意。"}},
        {"advisory": {"label": "提示", "body": "以上内容仅供参考。"}},
        {
            "advisory": {
                "label": "注意",
                "body": "```text\n不应展示代码块\n```",
            },
        },
    ],
)
def test_validate_research_advisory_rejects_empty_or_generic_model_output(
    payload: dict,
) -> None:
    assert _validate_research_advisory(payload) is None


async def test_artifact_classifier_background_off_never_calls_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_model = AsyncMock()
    monkeypatch.setattr(
        utility_intelligence,
        "load_config",
        lambda: _root_config(background_processing_enabled=False),
    )
    monkeypatch.setattr(utility_intelligence, "_resolve_model", resolve_model)

    response = await classify_text_artifacts("风来。", agent_id="default")

    assert response.status == "disabled"
    assert response.artifacts == []
    resolve_model.assert_not_called()


async def test_artifact_classifier_returns_validated_exact_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "开场说明。\n\n《无题》\n\n风来。\n\n还要一首吗？"
    work = "《无题》\n\n风来。"
    consume_response = AsyncMock(
        return_value=json.dumps(
            {
                "artifacts": [
                    {
                        "kind": "creative",
                        "title": "无题",
                        "start_marker": "《无题》",
                        "end_marker": "风来。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(utility_intelligence, "load_config", _root_config)
    monkeypatch.setattr(
        utility_intelligence,
        "_resolve_model",
        lambda config, agent_id: _ResolvedModel(
            model=AsyncMock(),
            provider_id="test-provider",
            model_id="test-model",
        ),
    )
    monkeypatch.setattr(
        utility_intelligence,
        "_consume_model_response",
        consume_response,
    )

    response = await classify_text_artifacts(source, agent_id="agent-1")

    assert response.status == "classified"
    assert response.model == "test-provider/test-model"
    assert [item.source_text for item in response.artifacts] == [work]
    consume_response.assert_awaited_once()


async def test_artifact_classifier_falls_back_after_accelerator_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accelerator = AsyncMock(name="accelerator")
    primary = AsyncMock(name="primary")

    async def consume(model, _messages) -> str:
        if model is accelerator:
            raise TimeoutError("accelerator timed out")
        return '{"artifacts":[],"advisory":null}'

    monkeypatch.setattr(
        utility_intelligence,
        "load_config",
        lambda: _root_config(mode="dedicated"),
    )
    monkeypatch.setattr(
        utility_intelligence,
        "_resolve_model",
        lambda _config, _agent_id: _ResolvedModel(
            model=accelerator,
            provider_id="utility-provider",
            model_id="utility-model",
        ),
    )
    monkeypatch.setattr(
        utility_intelligence,
        "_resolve_active_model",
        lambda _agent_id: _ResolvedModel(
            model=primary,
            provider_id="primary-provider",
            model_id="primary-model",
        ),
    )
    monkeypatch.setattr(
        utility_intelligence,
        "_consume_model_response",
        consume,
    )

    response = await classify_text_artifacts("普通回答。", agent_id="agent-1")

    assert response.status == "classified"
    assert response.model == "primary-provider/primary-model"


async def test_artifact_classifier_returns_research_advisory_in_same_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """# 竞品研究报告

根据官方定价页和两篇实测，A 方案当前更适合小团队。"""
    consume_response = AsyncMock(
        return_value=json.dumps(
            {
                "artifacts": [],
                "advisory": {
                    "label": "注意",
                    "body": (
                        "定价与套餐范围会变动，这份结论只对应报告中"
                        "核对的当前版本和小团队场景。"
                    ),
                },
            },
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(utility_intelligence, "load_config", _root_config)
    monkeypatch.setattr(
        utility_intelligence,
        "_resolve_model",
        lambda config, agent_id: _ResolvedModel(
            model=AsyncMock(),
            provider_id="test-provider",
            model_id="test-model",
        ),
    )
    monkeypatch.setattr(
        utility_intelligence,
        "_consume_model_response",
        consume_response,
    )

    response = await classify_text_artifacts(source, agent_id="agent-1")

    assert response.status == "classified"
    assert response.artifacts == []
    assert response.advisory is not None
    assert response.advisory.label == "注意"
    assert "小团队场景" in response.advisory.body
    consume_response.assert_awaited_once()


async def test_artifact_classifier_timeout_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def consume_response(*_args, **_kwargs) -> str:
        return ""

    async def raise_timeout(awaitable, *, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        utility_intelligence,
        "load_config",
        lambda: _root_config(timeout_seconds=1.0),
    )
    monkeypatch.setattr(
        utility_intelligence,
        "_resolve_model",
        lambda config, agent_id: _ResolvedModel(
            model=AsyncMock(),
            provider_id="test-provider",
            model_id="test-model",
        ),
    )
    monkeypatch.setattr(
        utility_intelligence,
        "_consume_model_response",
        consume_response,
    )
    monkeypatch.setattr(utility_intelligence.asyncio, "wait_for", raise_timeout)

    response = await classify_text_artifacts("风来。", agent_id="agent-1")

    assert response.status == "failed"
    assert response.artifacts == []


async def test_artifact_router_prefers_decoded_agent_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_module = importlib.import_module(
        "lantai.app.routers.utility_intelligence",
    )
    classify = AsyncMock(
        return_value=ArtifactClassificationResponse(status="classified"),
    )
    monkeypatch.setattr(router_module, "classify_text_artifacts", classify)
    request = SimpleNamespace(
        state=SimpleNamespace(),
        headers={"X-Agent-Id": "%E6%B2%88%E8%A1%8C%E7%9F%A5"},
    )

    await router_module.classify_artifacts(
        request,
        ArtifactClassificationRequest(text="风来。"),
    )

    classify.assert_awaited_once_with("风来。", agent_id="沈行知")


async def test_consume_model_response_joins_openai_delta_chunks() -> None:
    class DeltaStreamingModel:
        async def __call__(self, _messages):
            async def stream():
                for text in ('{"atoms":[', '{"content":"用户负责公关"}', "]}"):
                    yield SimpleNamespace(text=text)

            return stream()

    result = await consume_model_response(DeltaStreamingModel(), [])

    assert result == '{"atoms":[{"content":"用户负责公关"}]}'


async def test_consume_model_response_forwards_a_bounded_generation_budget() -> None:
    calls: list[dict[str, object]] = []

    class CapturingModel:
        async def __call__(self, _messages, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text="bounded")

    result = await consume_model_response(
        CapturingModel(),
        [],
        generation_kwargs={"max_tokens": 321},
    )

    assert result == "bounded"
    assert calls == [{"max_tokens": 321}]


@pytest.mark.parametrize("answer", ["", '{"complete_looking":true}'])
async def test_utility_never_accepts_length_terminated_output(answer) -> None:
    from lantai.app.utility_model import UtilityTaskIncompleteError

    class TruncatedStreamingModel:
        async def __call__(self, _messages):
            async def stream():
                yield SimpleNamespace(content=[{"type": "thinking", "thinking": "private reasoning"}])
                yield SimpleNamespace(text=answer, finish_reason="length")
                yield SimpleNamespace(content=[], usage=SimpleNamespace(completion_tokens=8192))
            return stream()

    with pytest.raises(UtilityTaskIncompleteError) as captured:
        await consume_model_response(TruncatedStreamingModel(), [])
    assert captured.value.finish_reason == "length"
    assert answer not in str(captured.value) or not answer
    assert "private reasoning" not in str(captured.value)


async def test_utility_keeps_complete_answer_after_reasoning_and_finish_marker() -> None:
    class CompleteStreamingModel:
        async def __call__(self, _messages):
            async def stream():
                yield SimpleNamespace(content=[{"type": "thinking", "thinking": "private reasoning"}])
                yield SimpleNamespace(text='{"complete":true}')
                yield SimpleNamespace(content=[], finish_reason="stop")
            return stream()

    assert await consume_model_response(CompleteStreamingModel(), []) == '{"complete":true}'


@pytest.mark.parametrize("reason", ["length", "content_filter"])
@pytest.mark.parametrize("tail", ["idle_timeout", "total_timeout", "transport_error"])
async def test_incomplete_finish_survives_tail_failure_and_drains_usage(reason, tail) -> None:
    from lantai.app.utility_model import UtilityTaskIncompleteError

    drained = []

    class IncompleteModel:
        async def __call__(self, _messages):
            async def stream():
                yield SimpleNamespace(text='{"complete_looking":true}', finish_reason=reason)
                yield SimpleNamespace(content=[], usage=SimpleNamespace(completion_tokens=8192))
                drained.append("usage")
                if tail == "transport_error":
                    raise RuntimeError("private transport detail")
                await asyncio.sleep(0.03)
            return stream()

    deadlines = ({"idle_timeout_seconds": 0.005} if tail == "idle_timeout"
                 else {"total_timeout_seconds": 0.01} if tail == "total_timeout"
                 else {})
    with pytest.raises(UtilityTaskIncompleteError) as captured:
        await consume_model_response(IncompleteModel(), [], **deadlines)
    assert captured.value.finish_reason == reason
    assert drained == ["usage"]
    assert "private transport detail" not in str(captured.value)


@pytest.mark.parametrize("reason", ["length", "content_filter"])
async def test_incomplete_finish_cannot_be_cleared_by_later_stop(reason) -> None:
    from lantai.app.utility_model import UtilityTaskIncompleteError

    drained = []

    class IncompleteModel:
        async def __call__(self, _messages):
            async def stream():
                yield SimpleNamespace(text='{"complete_looking":true}', finish_reason=reason)
                yield SimpleNamespace(content=[], finish_reason="stop")
                yield SimpleNamespace(content=[], usage=SimpleNamespace(completion_tokens=8192))
                drained.append("usage")
            return stream()

    with pytest.raises(UtilityTaskIncompleteError) as captured:
        await consume_model_response(IncompleteModel(), [])
    assert captured.value.finish_reason == reason
    assert drained == ["usage"]


async def test_complete_finish_still_drains_terminal_usage() -> None:
    drained = []

    class CompleteModel:
        async def __call__(self, _messages):
            async def stream():
                yield SimpleNamespace(text='{"complete":true}', finish_reason="stop")
                yield SimpleNamespace(content=[], usage=SimpleNamespace(completion_tokens=12))
                drained.append("usage")
            return stream()

    assert await consume_model_response(CompleteModel(), []) == '{"complete":true}'
    assert drained == ["usage"]


def test_utility_model_config_round_trips_without_touching_chat_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config(
        utility_intelligence=UtilityIntelligenceConfig(
            mode="dedicated",
            model=ModelSlotConfig(
                provider_id="utility-provider",
                model="utility-model",
            ),
            timeout_seconds=9,
        ),
    )

    save_config(config, config_path)
    loaded = load_config(config_path)

    assert loaded.utility_intelligence.mode == "dedicated"
    assert loaded.utility_intelligence.model.provider_id == "utility-provider"
    assert loaded.utility_intelligence.model.model == "utility-model"
    assert loaded.utility_intelligence.timeout_seconds == 9


async def _call_public_utility_entry(entry, *, timeout_seconds):
    common = dict(
        agent_id="agent-1", system_prompt="isolated task", purpose="tail-lock",
        timeout_seconds=timeout_seconds,
    )
    if entry == "content":
        return await run_utility_content_task(user_content="input", **common)
    functions = {
        "text": run_utility_text_task,
        "active": run_active_model_text_task,
        "memory": run_memory_text_task,
    }
    return await functions[entry](user_prompt="input", **common)


def _install_public_tail_model(monkeypatch, model):
    resolved = ResolvedUtilityModel(
        model=model, provider_id="test-provider", model_id="test-model",
    )
    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model, "resolve_utility_model", lambda *_args: resolved,
    )
    monkeypatch.setattr(
        utility_model, "resolve_active_model", lambda *_args: resolved,
    )

    async def idle(_agent_id, operation):
        return await operation()

    monkeypatch.setattr(utility_model, "run_primary_when_idle", idle)


@pytest.mark.parametrize("entry", ["text", "content", "active", "memory"])
@pytest.mark.parametrize("reason", ["length", "content_filter"])
async def test_public_utility_entry_retains_incomplete_finish_at_total_deadline(
    monkeypatch, entry, reason,
):
    class IncompleteModel:
        async def __call__(self, _messages, **_kwargs):
            async def stream():
                yield SimpleNamespace(content=[], finish_reason=reason)
                await asyncio.Event().wait()
            return stream()

    _install_public_tail_model(monkeypatch, IncompleteModel())
    with pytest.raises(utility_model.UtilityTaskIncompleteError) as captured:
        await _call_public_utility_entry(entry, timeout_seconds=0.01)
    assert captured.value.finish_reason == reason


@pytest.mark.parametrize("entry", ["text", "content", "active", "memory"])
@pytest.mark.parametrize("reason", [None, "length"])
async def test_public_utility_entry_preserves_real_cancellation(
    monkeypatch, entry, reason,
):
    stalled = asyncio.Event()

    class StalledModel:
        async def __call__(self, _messages, **_kwargs):
            async def stream():
                yield SimpleNamespace(content=[], finish_reason=reason)
                stalled.set()
                await asyncio.Event().wait()
            return stream()

    _install_public_tail_model(monkeypatch, StalledModel())
    task = asyncio.create_task(
        _call_public_utility_entry(entry, timeout_seconds=1),
    )
    await asyncio.wait_for(stalled.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_legacy_disabled_mode_migrates_to_primary_background_fallback() -> None:
    config = UtilityIntelligenceConfig(mode="disabled")

    assert config.mode == "follow_active"
    assert config.background_processing_enabled is True


def test_background_semantic_processing_has_an_independent_off_switch() -> None:
    config = UtilityIntelligenceConfig(
        mode="dedicated",
        background_processing_enabled=False,
    )

    assert config.mode == "dedicated"
    assert config.background_processing_enabled is False


async def test_utility_text_task_receives_only_its_isolated_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consume = AsyncMock(return_value='{"memories":[]}')
    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: _root_config(),
    )
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda config, agent_id: ResolvedUtilityModel(
            model=AsyncMock(),
            provider_id="utility-provider",
            model_id="utility-model",
        ),
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    result = await run_utility_text_task(
        agent_id="agent-1",
        system_prompt="extract only",
        user_prompt="one completed turn",
        purpose="memory_extraction",
    )

    assert result is not None
    assert result.model_label == "utility-provider/utility-model"
    messages = consume.await_args.args[1]
    assert messages == [
        {"role": "system", "content": "extract only"},
        {"role": "user", "content": "one completed turn"},
    ]


async def test_utility_text_task_rejects_a_different_authorized_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consume = AsyncMock(return_value="must not run")
    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=AsyncMock(),
            provider_id="active-provider",
            model_id="active-model",
        ),
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    with pytest.raises(
        utility_model.UtilityModelPolicyError,
        match="authorized provider",
    ):
        await run_utility_text_task(
            agent_id="agent-1",
            system_prompt="synthesize cited evidence",
            user_prompt="evidence only",
            purpose="daily_brief_synthesis",
            expected_provider_id="approved-provider",
        )

    consume.assert_not_awaited()


async def test_utility_text_task_rejects_a_paid_or_unknown_cost_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consume = AsyncMock(return_value="must not run")
    provider = SimpleNamespace(
        is_local=False,
        get_model_info=lambda _model_id: SimpleNamespace(is_free=False),
    )
    manager = SimpleNamespace(get_provider=lambda _provider_id: provider)
    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=AsyncMock(),
            provider_id="remote-provider",
            model_id="paid-model",
        ),
    )
    monkeypatch.setattr(
        utility_model.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    with pytest.raises(
        utility_model.UtilityModelPolicyError,
        match="local or explicitly free",
    ):
        await run_utility_text_task(
            agent_id="agent-1",
            system_prompt="synthesize cited evidence",
            user_prompt="evidence only",
            purpose="daily_brief_synthesis",
            expected_provider_id="remote-provider",
            require_no_api_cost=True,
        )

    consume.assert_not_awaited()


async def test_utility_text_task_applies_the_authorized_output_token_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consume = AsyncMock(return_value="bounded")
    provider = SimpleNamespace(
        is_local=True,
        get_model_info=lambda _model_id: SimpleNamespace(is_free=False),
    )
    manager = SimpleNamespace(get_provider=lambda _provider_id: provider)
    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=AsyncMock(),
            provider_id="local-provider",
            model_id="local-model",
        ),
    )
    monkeypatch.setattr(
        utility_model.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    result = await run_utility_text_task(
        agent_id="agent-1",
        system_prompt="synthesize cited evidence",
        user_prompt="evidence only",
        purpose="daily_brief_synthesis",
        expected_provider_id="local-provider",
        require_no_api_cost=True,
        max_output_tokens=777,
    )

    assert result is not None
    assert consume.await_args.kwargs["generation_kwargs"] == {
        "max_tokens": 777,
    }


@pytest.mark.parametrize(
    ("provider_id", "is_local", "expects_wire_schema"),
    [("lmstudio", True, True), ("lmstudio", False, False), ("other", True, False)],
)
async def test_structured_utility_uses_supported_local_schema_without_losing_budget(
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    is_local: bool,
    expects_wire_schema: bool,
) -> None:
    consume = AsyncMock(return_value='{"ready":true}')
    schema = {
        "type": "object",
        "properties": {"ready": {"type": "boolean"}},
        "required": ["ready"],
    }
    provider = SimpleNamespace(is_local=is_local)
    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=AsyncMock(), provider_id=provider_id, model_id="configured-model",
        ),
    )
    monkeypatch.setattr(
        utility_model.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(get_provider=lambda _id: provider),
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    result = await run_utility_text_task(
        agent_id="agent-1", system_prompt="schema", user_prompt="ready",
        purpose="daily_brief_synthesis", max_output_tokens=777,
        structured_output_schema=schema,
    )

    assert result is not None
    expected = {"max_tokens": 777}
    if expects_wire_schema:
        expected["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "utility_result", "strict": True, "schema": schema},
        }
    assert consume.await_args.kwargs["generation_kwargs"] == expected


async def test_utility_text_task_isolates_and_restores_usage_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lantai.app.agent_context import (
        get_current_session_id,
        set_current_session_id,
    )

    seen_session_ids: list[str | None] = []

    async def consume(_model, _messages, **_kwargs) -> str:
        seen_session_ids.append(get_current_session_id())
        return "isolated"

    set_current_session_id("active-chat")
    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=AsyncMock(),
            provider_id="local-provider",
            model_id="local-model",
        ),
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    result = await run_utility_text_task(
        agent_id="agent-1",
        system_prompt="synthesize cited evidence",
        user_prompt="evidence only",
        purpose="daily_brief_synthesis",
        usage_session_id="daily-brief:cycle-1",
    )

    assert result is not None
    assert seen_session_ids == ["daily-brief:cycle-1"]
    assert get_current_session_id() == "active-chat"


async def test_utility_content_task_formats_provider_neutral_image_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lantai.agents.openai_chat_formatter import LantaiOpenAIChatFormatter

    captured: list[list[dict]] = []

    class CapturingModel:
        async def __call__(self, messages):
            captured.append(messages)
            return SimpleNamespace(text="checked")

    resolved = ResolvedUtilityModel(
        model=CapturingModel(),
        provider_id="openai-compatible",
        model_id="vision-model",
        formatter=LantaiOpenAIChatFormatter(),
    )
    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: resolved,
    )

    result = await run_utility_content_task(
        agent_id="agent-1",
        system_prompt="inspect the rendered slide",
        user_content=[
            {"type": "text", "text": "check this page"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "aW1hZ2U=",
                },
            },
        ],
        purpose="presentation_visual_review",
    )

    assert result is not None
    assert captured[0][1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
    }


@pytest.mark.parametrize("route", ["dedicated", "follow_active"])
@pytest.mark.parametrize("active_supports_images", [False, True])
@pytest.mark.parametrize("selected_supports_images", [True, False, None])
async def test_visual_review_uses_selected_model_capability(
    monkeypatch, active_supports_images, selected_supports_images, route,
) -> None:
    """Production formatting must not silently remove the critic's image."""
    from lantai.agents import model_factory

    captured = []
    raw_model = object()
    provider = SimpleNamespace(
        get_chat_model_instance=lambda _model: raw_model,
        get_model_info=lambda _model: SimpleNamespace(
            supports_image=selected_supports_images,
            supports_multimodal=None,
        ),
    )
    manager = SimpleNamespace(
        is_model_slot_available=lambda _slot: True,
        get_provider=lambda _provider: provider,
    )
    monkeypatch.setattr(
        utility_model.ProviderManager, "get_instance", lambda: manager,
    )
    monkeypatch.setattr(
        utility_model, "TokenRecordingModelWrapper", lambda _id, model: model,
    )
    monkeypatch.setattr(
        model_factory, "_supports_multimodal_for_current_model",
        lambda: active_supports_images,
    )
    config = _root_config(mode=route)
    config.utility_intelligence.model = ModelSlotConfig(
        provider_id="synthetic", model="selected-critic",
    )
    monkeypatch.setattr(utility_model, "load_config", lambda: config)
    monkeypatch.setattr(
        model_factory, "create_model_and_formatter",
        lambda **_kwargs: (raw_model, model_factory.create_formatter_for_chat_model(raw_model)),
    )
    from lantai.config import config as agent_config_module
    monkeypatch.setattr(
        agent_config_module, "load_agent_config",
        lambda _agent_id: SimpleNamespace(active_model=config.utility_intelligence.model),
    )

    async def consume(model, messages, **_kwargs):
        assert model is raw_model
        captured.extend(messages)
        return "checked"

    monkeypatch.setattr(utility_model, "consume_model_response", consume)
    content = [
        {"type": "text", "text": "synthetic visual review"},
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "aW1hZ2U=",
        }},
    ]
    result = await run_utility_content_task(
        agent_id="synthetic-agent", system_prompt="inspect",
        user_content=content, purpose="presentation_visual_review",
        allow_follow_active=route == "follow_active", wait_for_idle=False,
    )
    if selected_supports_images is False:
        assert result is None
        assert captured == []
    else:
        assert result is not None
        blocks = [block for msg in captured
                  if isinstance(msg.get("content"), list)
                  for block in msg["content"]]
        assert {"type": "image_url", "image_url": {
            "url": "data:image/png;base64,aW1hZ2U=",
        }} in blocks
    assert content[1]["type"] == "image"


@pytest.mark.parametrize("agent_slot", [
    None, ModelSlotConfig(),
    ModelSlotConfig(provider_id="synthetic"),
    ModelSlotConfig(model="text"),
    ModelSlotConfig(provider_id="synthetic", model="text"),
])
@pytest.mark.parametrize("supports_image", [False, True])
def test_active_utility_capability_matches_real_factory_slot(
    monkeypatch, agent_slot, supports_image,
) -> None:
    from lantai.agents import model_factory
    from lantai.config import config as config_module

    slot = ModelSlotConfig(provider_id="synthetic", model="text")
    running = SimpleNamespace(
        llm_retry_enabled=False, llm_max_retries=0, llm_backoff_base=1,
        llm_backoff_cap=1, llm_max_concurrent=1, llm_max_qpm=10,
        llm_rate_limit_pause=0, llm_rate_limit_jitter=0, llm_acquire_timeout=1,
    )
    raw_model = object()
    provider = SimpleNamespace(
        get_chat_model_instance=lambda _model: raw_model,
        get_model_info=lambda _model: SimpleNamespace(supports_image=supports_image),
    )
    manager = SimpleNamespace(
        get_provider=lambda provider_id: provider if provider_id == "synthetic" else None,
        get_active_model=lambda: slot,
    )
    monkeypatch.setattr(config_module, "load_agent_config", lambda _id: SimpleNamespace(
        active_model=agent_slot, running=running,
    ))
    monkeypatch.setattr(model_factory.ProviderManager, "get_instance", lambda: manager)
    monkeypatch.setattr(model_factory.ProviderManager, "get_active_chat_model", lambda: raw_model)
    monkeypatch.setattr(model_factory, "TokenRecordingModelWrapper", lambda _id, model: model)
    monkeypatch.setattr(model_factory, "RetryChatModel", lambda model, **_kwargs: model)
    first = utility_model.resolve_active_model("synthetic-agent")
    second = utility_model.resolve_active_model("synthetic-agent")
    assert first.formatter is not second.formatter
    assert first.model is raw_model
    assert (first.provider_id, first.model_id) == (slot.provider_id, slot.model)
    assert first.supports_image is supports_image
    assert first.formatter._lantai_supports_multimodal is supports_image


async def test_utility_content_task_attributes_usage_to_original_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lantai.app.agent_context import (
        current_session_id_scope,
        get_current_session_id,
    )

    seen_session_ids: list[str | None] = []

    class CapturingModel:
        async def __call__(self, messages):
            seen_session_ids.append(get_current_session_id())
            return SimpleNamespace(text="checked")

    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=CapturingModel(),
            provider_id="test-provider",
            model_id="test-model",
            formatter=None,
        ),
    )
    with current_session_id_scope("active-chat"):
        await run_utility_content_task(
            agent_id="agent-1",
            system_prompt="review",
            user_content="page",
            purpose="presentation-visual-review",
            usage_session_id="original-task",
        )
        assert seen_session_ids == ["original-task"]
        assert get_current_session_id() == "active-chat"


@pytest.mark.parametrize("active_supports_image", [True, False])
async def test_content_task_never_sends_images_to_known_text_only_model(
    monkeypatch,
    active_supports_image,
) -> None:
    calls = []
    utility = SimpleNamespace(
        model=object(),
        provider_id="text-provider",
        model_id="text-model",
        formatter=None,
        supports_image=False,
        label="text-provider/text-model",
    )
    active = SimpleNamespace(
        model=object(),
        provider_id="vision-provider",
        model_id="vision-model",
        formatter=None,
        supports_image=active_supports_image,
        label="vision-provider/vision-model",
    )

    async def consume(model, messages, **_kwargs):
        calls.append((model, messages))
        return "checked"

    monkeypatch.setattr(
        utility_model, "load_config", lambda: _root_config(mode="dedicated")
    )
    monkeypatch.setattr(
        utility_model, "resolve_utility_model", lambda *args: utility
    )
    monkeypatch.setattr(
        utility_model, "resolve_active_model", lambda *args: active
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)
    result = await run_utility_content_task(
        agent_id="vision-route-test",
        system_prompt="inspect",
        purpose="presentation-visual-review",
        user_content=[
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "aW1hZ2U=",
                },
            }
        ],
    )
    if active_supports_image:
        assert result.model_label == active.label
        assert [model for model, _ in calls] == [active.model]
    else:
        assert result is None
        assert calls == []


@pytest.mark.parametrize("capability", [None, True])
async def test_unknown_or_image_capable_custom_slot_keeps_current_route(
    monkeypatch,
    capability,
) -> None:
    resolved = ResolvedUtilityModel(
        model=object(),
        provider_id="custom",
        model_id="image-model",
        supports_image=capability,
    )
    consume = AsyncMock(return_value="checked")
    fallback = AsyncMock()
    monkeypatch.setattr(
        utility_model, "load_config", lambda: _root_config(mode="dedicated")
    )
    monkeypatch.setattr(
        utility_model, "resolve_utility_model", lambda *args: resolved
    )
    monkeypatch.setattr(utility_model, "resolve_active_model", fallback)
    monkeypatch.setattr(utility_model, "consume_model_response", consume)
    result = await run_utility_content_task(
        agent_id="custom-route",
        system_prompt="inspect",
        user_content=[
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
            }
        ],
        purpose="review",
    )
    assert result.model_label == resolved.label
    consume.assert_awaited_once()
    fallback.assert_not_called()


async def test_text_only_dedicated_slot_cannot_borrow_primary_when_forbidden(
    monkeypatch,
):
    resolved = ResolvedUtilityModel(
        model=object(),
        provider_id="text",
        model_id="text-model",
        supports_image=False,
    )
    consume = AsyncMock()
    fallback = AsyncMock()
    monkeypatch.setattr(
        utility_model, "load_config", lambda: _root_config(mode="dedicated")
    )
    monkeypatch.setattr(
        utility_model, "resolve_utility_model", lambda *args: resolved
    )
    monkeypatch.setattr(utility_model, "resolve_active_model", fallback)
    monkeypatch.setattr(utility_model, "consume_model_response", consume)
    assert (
        await run_utility_content_task(
            agent_id="no-borrow",
            system_prompt="inspect",
            user_content=[{"type": "image"}],
            purpose="review",
            allow_follow_active=False,
        )
        is None
    )
    consume.assert_not_awaited()
    fallback.assert_not_called()


@pytest.mark.parametrize(
    "image,legacy,expected",
    [
        (False, True, False),
        (True, False, True),
        (None, True, True),
        (None, False, False),
        (None, None, None),
    ],
)
def test_slot_image_capability_prefers_exact_model_metadata(
    monkeypatch, image, legacy, expected
):
    info = SimpleNamespace(supports_image=image, supports_multimodal=legacy)
    provider = SimpleNamespace(
        get_model_info=lambda model: info if model == "exact-model" else None
    )
    monkeypatch.setattr(
        utility_model.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(
            get_provider=lambda provider_id: provider
            if provider_id == "known"
            else None,
        ),
    )
    assert (
        utility_model._slot_image_capability(
            ModelSlotConfig(
                provider_id="known",
                model="exact-model",
            )
        )
        is expected
    )
    assert (
        utility_model._slot_image_capability(
            ModelSlotConfig(
                provider_id="known",
                model="unknown-model",
            )
        )
        is None
    )
    assert (
        utility_model._slot_image_capability(
            ModelSlotConfig(
                provider_id="unknown",
                model="exact-model",
            )
        )
        is None
    )


async def test_foreground_content_child_does_not_wait_for_its_own_parent(
    monkeypatch,
) -> None:
    from lantai.app.background_semantics import foreground_model_turn

    resolved = ResolvedUtilityModel(
        model=object(), provider_id="active", model_id="vision"
    )
    consume = AsyncMock(return_value="checked")
    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model, "resolve_utility_model", lambda *args: resolved
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)
    async with foreground_model_turn("ppt-foreground-child"):
        result = await asyncio.wait_for(
            run_utility_content_task(
                agent_id="ppt-foreground-child",
                system_prompt="inspect",
                user_content="page",
                purpose="presentation-visual-review",
                wait_for_idle=False,
            ),
            timeout=0.5,
        )
    assert result.text == "checked"
    consume.assert_awaited_once()


async def test_background_content_task_still_waits_for_primary_idle(
    monkeypatch,
) -> None:
    from lantai.app.background_semantics import foreground_model_turn

    resolved = ResolvedUtilityModel(
        model=object(), provider_id="active", model_id="vision"
    )
    consume = AsyncMock(return_value="checked")
    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model, "resolve_utility_model", lambda *args: resolved
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)
    async with foreground_model_turn("ppt-background-child"):
        task = asyncio.create_task(
            run_utility_content_task(
                agent_id="ppt-background-child",
                system_prompt="inspect",
                user_content="page",
                purpose="background",
            )
        )
        await asyncio.sleep(0)
        consume.assert_not_awaited()
        assert not task.done()
    result = await asyncio.wait_for(task, timeout=0.5)
    assert result.text == "checked"


@pytest.mark.parametrize(
    "mode,enabled,allow_follow",
    [("dedicated", False, True), ("follow_active", True, False)],
)
async def test_content_routing_preserves_disabled_and_no_primary_borrow(
    monkeypatch, mode, enabled, allow_follow
):
    resolve = AsyncMock()
    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: _root_config(mode=mode, background_processing_enabled=enabled),
    )
    monkeypatch.setattr(utility_model, "resolve_utility_model", resolve)
    result = await run_utility_content_task(
        agent_id="policy",
        system_prompt="inspect",
        user_content=[{"type": "image"}],
        purpose="review",
        allow_follow_active=allow_follow,
    )
    assert result is None
    resolve.assert_not_awaited()


async def test_foreground_maintenance_does_not_borrow_active_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consume = AsyncMock(return_value="summary")
    resolve = AsyncMock()
    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: _root_config(mode="follow_active"),
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)
    monkeypatch.setattr(utility_model, "resolve_utility_model", resolve)

    result = await run_utility_text_task(
        agent_id="agent-1",
        system_prompt="compress safely",
        user_prompt="conversation",
        purpose="context_compaction",
        allow_follow_active=False,
    )

    assert result is None
    resolve.assert_not_awaited()
    consume.assert_not_awaited()


async def test_active_model_fallback_is_isolated_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consume = AsyncMock(return_value='{"interaction_mode":"action"}')
    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: _root_config(timeout_seconds=7),
    )
    monkeypatch.setattr(
        utility_model,
        "resolve_active_model",
        lambda _agent_id: ResolvedUtilityModel(
            model=AsyncMock(),
            provider_id="active-provider",
            model_id="active-model",
        ),
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    result = await run_active_model_text_task(
        agent_id="agent-1",
        system_prompt="classify the turn",
        user_prompt="create a slide deck",
        purpose="turn_intelligence_primary_fallback",
    )

    assert result is not None
    assert result.model_label == "active-provider/active-model"
    assert consume.await_args.args[1] == [
        {"role": "system", "content": "classify the turn"},
        {"role": "user", "content": "create a slide deck"},
    ]


async def test_background_off_switch_prevents_memory_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = AsyncMock()
    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: _root_config(background_processing_enabled=False),
    )
    monkeypatch.setattr(utility_model, "resolve_utility_model", resolve)

    result = await run_memory_text_task(
        agent_id="agent-1",
        system_prompt="extract",
        user_prompt="completed turn",
        purpose="memory",
    )

    assert result is None
    resolve.assert_not_awaited()


async def test_follow_active_memory_waits_for_primary_idle_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    idle_lane = AsyncMock(return_value='{"atoms":[]}')
    monkeypatch.setattr(utility_model, "load_config", _root_config)
    monkeypatch.setattr(
        utility_model,
        "resolve_active_model",
        lambda _agent_id: ResolvedUtilityModel(
            model=AsyncMock(),
            provider_id="active-provider",
            model_id="active-model",
        ),
    )
    monkeypatch.setattr(utility_model, "run_primary_when_idle", idle_lane)

    result = await run_memory_text_task(
        agent_id="agent-1",
        system_prompt="extract",
        user_prompt="completed turn",
        purpose="memory",
        require_json=True,
    )

    assert result is not None
    assert result.model_label == "active-provider/active-model"
    idle_lane.assert_awaited_once()


async def test_utility_text_task_allows_progressing_stream_past_short_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProgressingModel:
        async def __call__(self, _messages):
            async def stream():
                for text in (
                    "## 当前目标",
                    "## 当前目标\n保留任务",
                    "## 当前目标\n保留任务\n\n## 下一步行动\n继续执行",
                ):
                    await asyncio.sleep(0.015)
                    yield SimpleNamespace(text=text)

            return stream()

    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: SimpleNamespace(
            utility_intelligence=SimpleNamespace(
                mode="follow_active",
                timeout_seconds=0.01,
            ),
        ),
    )
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=ProgressingModel(),
            provider_id="utility-provider",
            model_id="utility-model",
        ),
    )

    result = await run_utility_text_task(
        agent_id="agent-1",
        system_prompt="compress safely",
        user_prompt="conversation",
        purpose="context_compaction",
        timeout_seconds=0.2,
        first_output_timeout_seconds=0.03,
        idle_timeout_seconds=0.03,
    )

    assert result is not None
    assert result.text.endswith("## 下一步行动\n继续执行")


async def test_utility_text_task_reports_first_output_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SilentStreamingModel:
        async def __call__(self, _messages):
            async def stream():
                await asyncio.sleep(0.1)
                yield SimpleNamespace(text="too late")

            return stream()

    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: _root_config(),
    )
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=SilentStreamingModel(),
            provider_id="utility-provider",
            model_id="utility-model",
        ),
    )

    with pytest.raises(
        UtilityTaskTimeoutError,
        match="first model output.*0.02s",
    ) as exc_info:
        await run_utility_text_task(
            agent_id="agent-1",
            system_prompt="compress safely",
            user_prompt="conversation",
            purpose="context_compaction",
            timeout_seconds=0.2,
            first_output_timeout_seconds=0.02,
            idle_timeout_seconds=0.02,
        )

    assert exc_info.value.phase == "first_output"


async def test_utility_text_task_reports_idle_timeout_after_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StalledStreamingModel:
        async def __call__(self, _messages):
            async def stream():
                yield SimpleNamespace(text="## 当前目标")
                await asyncio.sleep(0.1)
                yield SimpleNamespace(text="too late")

            return stream()

    monkeypatch.setattr(utility_model, "load_config", lambda: _root_config())
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=StalledStreamingModel(),
            provider_id="utility-provider",
            model_id="utility-model",
        ),
    )

    with pytest.raises(
        UtilityTaskTimeoutError,
        match="next model output.*0.02s",
    ) as exc_info:
        await run_utility_text_task(
            agent_id="agent-1",
            system_prompt="compress safely",
            user_prompt="conversation",
            purpose="context_compaction",
            timeout_seconds=0.2,
            first_output_timeout_seconds=0.02,
            idle_timeout_seconds=0.02,
        )

    assert exc_info.value.phase == "idle"


async def test_utility_text_task_keeps_a_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EndlessStreamingModel:
        async def __call__(self, _messages):
            async def stream():
                index = 0
                while True:
                    await asyncio.sleep(0.005)
                    index += 1
                    yield SimpleNamespace(text=f"chunk-{index}")

            return stream()

    monkeypatch.setattr(utility_model, "load_config", lambda: _root_config())
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=EndlessStreamingModel(),
            provider_id="utility-provider",
            model_id="utility-model",
        ),
    )

    with pytest.raises(
        UtilityTaskTimeoutError,
        match="task completion.*0.04s",
    ) as exc_info:
        await run_utility_text_task(
            agent_id="agent-1",
            system_prompt="compress safely",
            user_prompt="conversation",
            purpose="context_compaction",
            timeout_seconds=0.04,
            first_output_timeout_seconds=0.02,
            idle_timeout_seconds=0.02,
        )

    assert exc_info.value.phase == "total"


async def test_memory_text_task_falls_back_to_idle_primary_when_accelerator_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consume = AsyncMock(return_value='{"atoms":[{"content":"用户负责公关"}]}')
    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: _root_config(mode="dedicated"),
    )
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: None,
    )
    monkeypatch.setattr(
        utility_model,
        "resolve_active_model",
        lambda _agent_id: ResolvedUtilityModel(
            model=AsyncMock(),
            provider_id="primary-provider",
            model_id="primary-model",
        ),
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    result = await run_memory_text_task(
        agent_id="agent-1",
        system_prompt="understand durable memory",
        user_prompt="用户刚接手公关一号位",
        purpose="memory_extraction",
    )

    assert result is not None
    assert result.model_label == "primary-provider/primary-model"
    assert consume.await_count == 1


async def test_memory_text_task_fails_open_after_dedicated_utility_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stalled_model(_messages, **_kwargs):
        await asyncio.Event().wait()

    utility_model_instance = AsyncMock(
        name="utility-model", side_effect=stalled_model,
    )

    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: _root_config(mode="dedicated"),
    )
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=utility_model_instance,
            provider_id="utility-provider",
            model_id="utility-model",
        ),
    )
    monkeypatch.setattr(utility_model, "resolve_active_model", lambda *_args: None)

    result = await run_memory_text_task(
        agent_id="agent-1",
        system_prompt="understand durable memory",
        user_prompt="用户刚接手公关一号位",
        purpose="memory_extraction",
        timeout_seconds=0.08,
    )

    assert result is None
    utility_model_instance.assert_awaited_once()


async def test_memory_text_task_does_not_retry_invalid_json_on_active_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    utility_model_instance = AsyncMock(name="utility-model")
    calls: list[object] = []

    async def consume(model, _messages, **_kwargs) -> str:
        calls.append(model)
        if model is utility_model_instance:
            return "分析完成，但没有按要求输出 JSON"
        return '{"atoms":[{"content":"用户负责公关"}]}'

    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: _root_config(mode="dedicated"),
    )
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=utility_model_instance,
            provider_id="utility-provider",
            model_id="utility-model",
        ),
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    result = await run_memory_text_task(
        agent_id="agent-1",
        system_prompt="return JSON",
        user_prompt="用户刚接手公关一号位",
        purpose="memory_extraction",
        timeout_seconds=2,
        require_json=True,
    )

    assert result is None
    assert calls == [utility_model_instance]


async def test_memory_text_task_uses_follow_active_after_visible_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consume = AsyncMock(return_value='{"atoms":[]}')
    active_model = AsyncMock(name="active-memory-model")
    monkeypatch.setattr(
        utility_model,
        "load_config",
        lambda: _root_config(mode="follow_active"),
    )
    monkeypatch.setattr(
        utility_model,
        "resolve_utility_model",
        lambda _config, _agent_id: ResolvedUtilityModel(
            model=active_model,
            provider_id="active-provider",
            model_id="active-model",
        ),
    )
    monkeypatch.setattr(utility_model, "consume_model_response", consume)

    result = await run_memory_text_task(
        agent_id="agent-1",
        system_prompt="return JSON",
        user_prompt="ordinary turn",
        purpose="memory_extraction",
        require_json=True,
    )

    assert result is not None
    assert result.model_label == "active-provider/active-model"
    consume.assert_awaited_once()
    assert consume.await_args.args[0] is active_model
def test_utility_capability_matrix_reports_idle_primary_fallback() -> None:
    matrix = utility_capability_matrix(
        UtilityIntelligenceConfig(mode="dedicated"),
        model_available=False,
    )

    assert matrix["available"] is True
    assert matrix["accelerator_available"] is False
    assert matrix["memory_extraction"]["degradation"] == "active_model_idle"
    assert matrix["turn_intelligence"]["degradation"] == "primary_model_direct"
    assert matrix["structured_tasks"]["degradation"] == "active_model_idle"
