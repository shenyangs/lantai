# -*- coding: utf-8 -*-
"""Locks for numeric-only observation of actual model invocation usage."""
# Tests exercise the private wrapper callback and child reporting boundaries.
# pylint: disable=protected-access
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "payload,expected",
    [
        (None, (None, None, None, "unknown")),
        ({}, (None, None, None, "unknown")),
        (
            {"input_tokens": 0, "output_tokens": 0},
            (0, 0, 0, "sum_reported_fields"),
        ),
        (
            {"input_tokens": 7, "output_tokens": 3},
            (7, 3, 10, "sum_reported_fields"),
        ),
        ({"input_tokens": 7}, (7, None, None, "unknown")),
        ({"total_tokens": 11}, (None, None, 11, "provider_reported")),
        (
            {"input_tokens": True, "output_tokens": -3},
            (None, None, None, "unknown"),
        ),
        (
            {"input_tokens": "7", "output_tokens": 3.5},
            (None, None, None, "unknown"),
        ),
        (
            {"metadata": {"prompt_tokens": 8, "completion_tokens": 2}},
            (8, 2, 10, "sum_reported_fields"),
        ),
        (
            {
                "input_tokens": 0,
                "metadata": {"prompt_tokens": 8},
                "output_tokens": 1,
            },
            (0, 1, 1, "sum_reported_fields"),
        ),
        (
            SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=5),
            (2, 3, 5, "provider_reported"),
        ),
    ],
)
def test_reported_tokens_preserve_missing_and_explicit_zero(payload, expected):
    module = load_script("presentation_provider_measurement")
    result = module.read_reported_usage(payload)
    assert (
        tuple(
            result[key]
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "total_tokens_source",
            )
        )
        == expected
    )


@pytest.mark.parametrize(
    "amount,expected",
    [
        (None, None),
        (0, 0),
        (0.125, 125000),
        ("0.125", None),
        (True, None),
        (-1, None),
        (float("nan"), None),
        (0.0000001, None),
    ],
)
def test_provider_cost_requires_reported_exact_numeric_amount(
    amount,
    expected,
):
    module = load_script("presentation_provider_measurement")
    result = module.read_reported_usage({"metadata": {"cost_cny": amount}})
    assert result["provider_cost_micro_cny"] == expected
    assert result["provider_cost_status"] == (
        "unknown" if expected is None else "exact"
    )


def test_cached_zero_conflicting_total_and_private_text():
    module = load_script("presentation_provider_measurement")
    result = module.read_reported_usage(
        {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 11,
            "metadata": {
                "prompt_tokens_details": {"cached_tokens": 0},
                "prompt": "PRIVATE_SENTINEL",
            },
            "reply": "PRIVATE_SENTINEL",
        },
    )
    assert result["cached_tokens"] == 0
    assert result["total_tokens"] == 11
    assert result["reported_total_consistent"] is False
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


def fake_wrapper():
    class Wrapper:
        def __init__(self, result):
            self.result = result
            self.invocations = 0
            self.recorded = []

        def _record_usage(self, usage):
            self.recorded.append(usage)

        async def __call__(self, *args, **kwargs):
            self.invocations += 1
            if isinstance(self.result, BaseException):
                raise self.result
            if hasattr(self.result, "__aiter__"):
                return self.result
            self._record_usage(self.result)
            return self.result

    return Wrapper


def test_nonstream_result_identity_unknown_usage_and_restoration():
    module = load_script("presentation_provider_measurement")
    wrapper = fake_wrapper()
    original_call, original_record = wrapper.__call__, wrapper._record_usage
    payload = {
        "input_tokens": 7,
        "output_tokens": 3,
        "reply": "PRIVATE_SENTINEL",
    }
    good, missing = wrapper(payload), wrapper(None)
    with module.ProviderMeasurement(wrapper) as observer:
        assert asyncio.run(good("private prompt")) is payload
        assert asyncio.run(missing("private prompt")) is None
    assert (
        wrapper.__call__ is original_call
        and wrapper._record_usage is original_record
    )
    assert good.invocations == missing.invocations == 1
    report = observer.summary()
    assert report["model_invocation_count"] == 2
    assert report["tokens"]["input"]["total"] is None
    assert report["tokens"]["input"]["known_sum"] == 7
    assert report["tokens"]["input"]["unknown_count"] == 1
    assert report["duration_ms"]["sample_count"] == 2
    assert "PRIVATE_SENTINEL" not in json.dumps(report)


def test_interleaved_streams_preserve_chunks_and_last_usage_per_invocation():
    module = load_script("presentation_provider_measurement")
    wrapper = fake_wrapper()
    one, two = wrapper(None), wrapper(None)

    async def stream(owner, count):
        for index in range(3):
            await asyncio.sleep(0)
            yield (count, index)
        owner._record_usage({"input_tokens": count, "output_tokens": 1})

    one.result, two.result = stream(one, 2), stream(two, 9)

    async def collect(owner):
        response = await owner()
        return [chunk async for chunk in response]

    async def run():
        return await asyncio.gather(collect(one), collect(two))

    with module.ProviderMeasurement(wrapper) as observer:
        result = asyncio.run(run())
    assert result == [[(2, 0), (2, 1), (2, 2)], [(9, 0), (9, 1), (9, 2)]]
    report = observer.summary()
    assert [call["usage"]["input_tokens"] for call in report["calls"]] == [
        2,
        9,
    ]
    assert report["tokens"]["input"]["total"] == 11
    assert report["tokens"]["total"]["total"] == 13
    assert report["model_invocation_count"] == 2
    assert report["duration_ms"]["sample_count"] == 2


def test_exception_identity_cancellation_and_early_stream_close():
    module = load_script("presentation_provider_measurement")
    wrapper = fake_wrapper()
    failure = RuntimeError("PRIVATE_SENTINEL")
    broken, cancelled, early = (
        wrapper(failure),
        wrapper(asyncio.CancelledError()),
        wrapper(None),
    )
    closed = []

    async def stream():
        try:
            yield "PRIVATE_SENTINEL"
            yield "unconsumed"
        finally:
            early._record_usage({"input_tokens": 0, "output_tokens": 0})
            closed.append(True)

    early.result = stream()

    async def run():
        with pytest.raises(RuntimeError) as caught:
            await broken()
        assert caught.value is failure
        with pytest.raises(asyncio.CancelledError):
            await cancelled()
        response = await early()
        assert await response.__anext__() == "PRIVATE_SENTINEL"
        await response.aclose()

    with module.ProviderMeasurement(wrapper) as observer:
        asyncio.run(run())
    report = observer.summary()
    assert [call["outcome"] for call in report["calls"]] == [
        "failed",
        "cancelled",
        "closed",
    ]
    assert closed == [True]
    assert report["calls"][2]["usage"]["input_tokens"] == 0
    assert report["tokens"]["input"]["total"] is None
    assert "PRIVATE_SENTINEL" not in json.dumps(report)


def test_open_invocation_is_not_claimed_settled_or_zero_cost():
    module = load_script("presentation_provider_measurement")
    wrapper = fake_wrapper()

    async def stream():
        yield "private"

    owner = wrapper(stream())
    with module.ProviderMeasurement(wrapper) as observer:
        response = asyncio.run(owner())
        report = observer.summary()
        assert report["settled_invocation_count"] == 0
        assert report["duration_ms"]["sample_count"] == 0
        assert report["billing"]["provider_cost_micro_cny"] is None
        asyncio.run(response.aclose())
    assert observer.summary()["calls"][0]["outcome"] == "closed"


def test_nested_observer_rejected_and_original_methods_restored():
    module = load_script("presentation_provider_measurement")
    wrapper = fake_wrapper()
    original_call, original_record = wrapper.__call__, wrapper._record_usage
    with pytest.raises(RuntimeError, match="test exit"):
        with module.ProviderMeasurement(wrapper):
            with pytest.raises(RuntimeError, match="already observed"):
                with module.ProviderMeasurement(wrapper):
                    pass
            raise RuntimeError("test exit")
    assert (
        wrapper.__call__ is original_call
        and wrapper._record_usage is original_record
    )


def test_failure_evidence_keeps_reported_usage_without_arbitrary_text():
    smoke = load_script("source_presentation_full_generation_smoke")
    call = {
        "ordinal": 1,
        "outcome": "failed",
        "duration_ms": 12.5,
        "usage_record_count": 1,
        "private": "PRIVATE_SENTINEL",
        "usage": {
            "input_tokens": 4,
            "output_tokens": 0,
            "total_tokens": 4,
            "total_tokens_source": "provider_reported",
            "provider_cost_status": "unknown",
            "provider_cost_micro_cny": None,
            "private": "PRIVATE_SENTINEL",
        },
    }
    evidence = smoke._safe_child_failure(
        source_head="abc",
        provider_id="test",
        model="test",
        request_summary={},
        material_summaries=[],
        elapsed_ms=20,
        returncode=1,
        stdout="PRIVATE_SENTINEL",
        stderr="PRIVATE_SENTINEL",
        child={
            "provider_measurement": {
                "observed": True,
                "calls": [call],
                "private": "PRIVATE_SENTINEL",
            },
        },
    )
    measured = evidence["provider_measurement"]
    assert measured["tokens"]["input"]["total"] == 4
    assert measured["tokens"]["output"]["total"] == 0
    assert measured["billing"]["provider_cost_micro_cny"] is None
    assert "PRIVATE_SENTINEL" not in json.dumps(evidence)


@pytest.mark.parametrize(
    "outcome",
    ["completed", "failed", "cancelled", "closed"],
)
def test_actual_wrapper_stream_keeps_last_reported_partial_usage(
    monkeypatch,
    outcome,
):
    from lantai.token_usage import model_wrapper

    module = load_script("presentation_provider_measurement")
    recorded = []
    manager = SimpleNamespace(enqueue=recorded.append)
    monkeypatch.setattr(
        model_wrapper,
        "get_token_usage_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        model_wrapper.TokenRecordingModelWrapper,
        "_store_usage",
        lambda self, usage: None,
    )
    failure = RuntimeError("PRIVATE_SENTINEL")
    chunk = SimpleNamespace(
        usage={
            "input_tokens": 4,
            "output_tokens": 2,
            "metadata": {"cost_cny": 0},
        },
        content="PRIVATE_SENTINEL",
    )

    async def stream():
        yield chunk
        if outcome == "failed":
            raise failure
        if outcome == "cancelled":
            raise asyncio.CancelledError()

    class Model:
        model_name = "unit-measurement"
        stream = True
        invocations = 0

        async def __call__(self, **kwargs):
            self.invocations += 1
            return stream()

    raw = Model()
    owner = model_wrapper.TokenRecordingModelWrapper("unit-measurement", raw)
    original_call = type(owner).__call__

    async def run():
        response = await owner(messages=[])
        assert await response.__anext__() is chunk
        if outcome == "closed":
            await response.aclose()
        elif outcome == "failed":
            with pytest.raises(RuntimeError) as caught:
                await response.__anext__()
            assert caught.value is failure
        elif outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await response.__anext__()
        else:
            with pytest.raises(StopAsyncIteration):
                await response.__anext__()

    with module.ProviderMeasurement(type(owner)) as observer:
        asyncio.run(run())
    report = observer.summary()
    call = report["calls"][0]
    assert raw.invocations == 1 and type(owner).__call__ is original_call
    assert len(recorded) == (1 if outcome == "completed" else 0)
    assert call["outcome"] == outcome
    assert call["usage"]["input_tokens"] == 4
    assert call["usage"]["output_tokens"] == 2
    assert call["usage_origin"] == (
        "wrapper_recorded" if outcome == "completed" else "stream_snapshot"
    )
    assert report["billing"]["provider_cost_micro_cny"] == (
        0 if outcome == "completed" else None
    )
    assert report["tokens"]["input"]["total"] == (
        4 if outcome == "completed" else None
    )
    assert report["tokens"]["input"]["known_sum"] == 4
    assert "PRIVATE_SENTINEL" not in json.dumps(report)


@pytest.mark.parametrize("fails", [False, True])
def test_generation_child_retains_measurement_and_exception_identity(
    monkeypatch,
    tmp_path,
    fails,
):
    from lantai.config import config
    from lantai.presentation import cli
    from lantai.providers.provider_manager import ProviderManager
    from lantai import token_usage

    smoke = load_script("source_presentation_full_generation_smoke")
    wrapper = fake_wrapper()
    monkeypatch.setattr(token_usage, "TokenRecordingModelWrapper", wrapper)
    active = SimpleNamespace(provider_id="unit", model="unit")
    monkeypatch.setattr(
        config,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(active_model=active),
    )
    monkeypatch.setattr(
        ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(
            is_chat_model_slot_selectable=lambda slot: slot is active,
        ),
    )
    failure = RuntimeError("PRIVATE_SENTINEL")
    owner = wrapper({"input_tokens": 4, "output_tokens": 2})

    def generate(**_kwargs):
        asyncio.run(owner())
        if fails:
            raise failure
        return {"job_id": "unit-job", "status": "passed"}

    monkeypatch.setattr(cli, "generate", generate)
    kwargs = {
        "request_path": tmp_path / "request.json",
        "job_root": tmp_path,
        "output_dir": tmp_path,
        "workspace_root": tmp_path,
        "agent_id": "unit",
        "expected_provider_id": "unit",
        "expected_model": "unit",
    }
    if fails:
        with pytest.raises(RuntimeError) as caught:
            smoke._inner_run(**kwargs)
        assert caught.value is failure
        result = smoke._safe_exception(caught.value)
    else:
        result = smoke._inner_run(**kwargs)
    assert owner.invocations == 1
    assert result["provider_measurement"]["tokens"]["input"]["total"] == 4
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
    # A rejected route must not reuse measurement from the preceding run.
    kwargs["expected_model"] = "wrong"
    with pytest.raises(RuntimeError) as caught:
        smoke._inner_run(**kwargs)
    assert owner.invocations == 1
    assert "provider_measurement" not in smoke._safe_exception(caught.value)


@pytest.mark.parametrize(
    "fails_at,expected_calls",
    [
        ("none", 3),
        ("constructor", 1),
        ("dispatch", 3),
        ("future", 3),
        ("shutdown", 3),
    ],
)
def test_resume_observes_constructor_worker_and_shutdown_before_report(
    monkeypatch,
    tmp_path,
    fails_at,
    expected_calls,
):
    from lantai.config import config
    from lantai.presentation import intelligence, runtime
    from lantai.providers.provider_manager import ProviderManager
    from lantai import token_usage

    smoke = load_script("source_presentation_full_generation_smoke")
    wrapper = fake_wrapper()
    original_call = wrapper.__call__
    monkeypatch.setattr(token_usage, "TokenRecordingModelWrapper", wrapper)
    active = SimpleNamespace(provider_id="unit", model="unit")
    monkeypatch.setattr(
        config,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(active_model=active),
    )
    monkeypatch.setattr(
        ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(
            is_chat_model_slot_selectable=lambda slot: slot is active,
        ),
    )
    for name in (
        "UtilityPresentationSemanticPlanner",
        "UtilityPresentationPageAuthor",
        "UtilityPresentationObjectEditPlanner",
        "UtilityPresentationVisualCritic",
        "UtilityPresentationRepairDesigner",
    ):
        monkeypatch.setattr(intelligence, name, lambda agent_id: object())
    failure = RuntimeError("PRIVATE_SENTINEL")
    owner = wrapper({"input_tokens": 4, "output_tokens": 2})
    phases = []

    def observe(phase):
        phases.append(phase)
        asyncio.run(owner())
        if fails_at == phase:
            raise failure

    class Runtime:
        def __init__(self, *_args, **_kwargs):
            observe("constructor")
            record = SimpleNamespace(conversation_id="unit", timeout_seconds=1)
            self.service = SimpleNamespace(
                store=SimpleNamespace(get=lambda job_id: record),
            )
            self._futures = {
                "unit-job": SimpleNamespace(
                    result=lambda **kwargs: observe("future"),
                ),
            }

        def get(self, *_args):
            return SimpleNamespace(status="failed")

        def artifacts(self, *_args):
            return {}

        def shutdown(self):
            observe("shutdown")

    monkeypatch.setattr(runtime, "PresentationJobRuntime", Runtime)

    def dispatch(**_kwargs):
        if fails_at == "dispatch":
            observe("dispatch")

    monkeypatch.setattr(smoke, "_dispatch_resume_strategy", dispatch)
    monkeypatch.setattr(smoke, "_failed_job_summary", lambda path: {})
    kwargs = {
        "job_root": tmp_path,
        "job_id": "unit-job",
        "agent_id": "unit",
        "expected_provider_id": "unit",
        "expected_model": "unit",
        "resume_strategy": "automatic",
        "external_recovery_basis": None,
        "external_recovery_max_attempts": None,
        "implementation_recovery_basis": "unit",
        "implementation_fix_identity": "unit",
        "implementation_failure_category": "content",
        "implementation_recovery_action": "revise_content",
        "implementation_issue_code": "unit",
        "implementation_retry_stage": "plan",
        "implementation_recovery_max_attempts": None,
    }
    if fails_at == "none":
        result = smoke._inner_resume(**kwargs)
    else:
        with pytest.raises(RuntimeError) as caught:
            smoke._inner_resume(**kwargs)
        assert caught.value is failure
        result = smoke._safe_exception(caught.value)
    report = result["provider_measurement"]
    assert wrapper.__call__ is original_call
    assert (
        report["model_invocation_count"] == owner.invocations == expected_calls
    )
    assert report["tokens"]["input"]["total"] == 4 * expected_calls
    assert "shutdown" in phases or fails_at == "constructor"
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
