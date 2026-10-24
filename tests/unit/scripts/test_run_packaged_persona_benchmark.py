# -*- coding: utf-8 -*-
# Script-unit tests deliberately exercise the benchmark's private seams.
# pylint: disable=protected-access

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.agents.persona.perceptible_presence import (
    PresenceState,
    plan_persona_turn,
)
from lantai.agents.persona.user_understanding import (
    TurnUnderstandingProjection,
)


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "run_packaged_persona_benchmark.py"
)
SPEC = importlib.util.spec_from_file_location(
    "run_packaged_persona_benchmark",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_visible_answer_excludes_reasoning_and_accumulates_answer() -> None:
    payloads = [
        {"object": "message", "id": "reason", "type": "reasoning"},
        {"object": "content", "msg_id": "reason", "text": "内部推理"},
        {
            "object": "message",
            "id": "answer",
            "type": "message",
            "role": "assistant",
        },
        {"object": "content", "msg_id": "answer", "text": "你"},
        {"object": "content", "msg_id": "answer", "text": "你好"},
    ]

    assert MODULE._visible_answer(payloads) == "你好"


def test_prompt_injects_versioned_memory_only_on_first_turn() -> None:
    assert MODULE._prompt_with_memory((), "你好").endswith("你好")
    assert "不调用工具" in MODULE._prompt_with_memory((), "你好")
    prompt = MODULE._prompt_with_memory(("事实一", "事实二"), "你好")
    assert "事实一；事实二" in prompt
    assert prompt.endswith("用户现在说：你好")


def test_dialogue_only_wrapper_does_not_turn_grief_into_work() -> None:
    prompt = MODULE._prompt_with_memory(
        ("用户多次提到陪伴十五年的猫",),
        "陪了我十五年的猫今天走了。",
    )

    plan = plan_persona_turn(
        prompt,
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
    )

    assert plan.conversation_mode == "casual"
    assert plan.beat == "subjective_notice"


def test_packaged_benchmark_source_declares_versioned_suite_hash() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert '"suite"' in text
    assert '"constitution_version": "1.6.1"' in text
    assert "_sha256_file(SCENARIO_SUITE)" in text


def test_override_staged_model_is_isolated_and_requires_secret(
    tmp_path: Path,
) -> None:
    working = tmp_path / "working"
    secret = tmp_path / "working.secret"
    agent_path = working / "workspaces" / "default" / "agent.json"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_text(
        json.dumps({"active_model": {"provider_id": "kimi", "model": "old"}}),
        encoding="utf-8",
    )
    (secret / "providers" / "builtin").mkdir(parents=True)
    (secret / "providers" / "builtin" / "deepseek.json").write_text(
        "{}",
        encoding="utf-8",
    )

    configured = MODULE._override_staged_model(
        working_dir=working,
        secret_dir=secret,
        configured={"provider_id": "kimi", "model": "old"},
        provider_id="deepseek",
        model="deepseek-v4-pro",
    )

    assert configured["provider_id"] == "deepseek"
    assert json.loads(agent_path.read_text(encoding="utf-8"))[
        "active_model"
    ] == {
        "provider_id": "deepseek",
        "model": "deepseek-v4-pro",
    }


def test_select_scenarios_supports_targeted_failure_replay() -> None:
    scenarios = [SimpleNamespace(id="alpha"), SimpleNamespace(id="beta")]

    assert MODULE._select_scenarios(scenarios, ("beta",)) == [scenarios[1]]
    assert MODULE._select_scenarios(scenarios, ()) == scenarios

    with pytest.raises(ValueError, match="unknown persona scenario"):
        MODULE._select_scenarios(scenarios, ("missing",))


def test_terminal_failure_diagnostic_is_bounded_and_redacted() -> None:
    diagnostic = MODULE._terminal_failure_diagnostic(
        200,
        {
            "status": "failed",
            "error": {
                "type": "ProtocolError",
                "code": "invalid_tool_arguments",
                "message": (
                    "provider rejected sk-abcdefghijklmnop at "
                    "/Users/example/private"
                ),
            },
        },
    )

    assert diagnostic == {
        "http_status": 200,
        "status": "failed",
        "error_type": "ProtocolError",
        "error_code": "invalid_tool_arguments",
        "message": "provider rejected [redacted_key] at [redacted_path]",
    }


def test_failure_artifacts_are_retained_but_success_artifacts_are_removed(
    tmp_path: Path,
) -> None:
    failure_root = tmp_path / "failure"
    success_root = tmp_path / "success"
    failure_root.mkdir()
    success_root.mkdir()

    MODULE._cleanup_smoke_root(failure_root, succeeded=False)
    MODULE._cleanup_smoke_root(success_root, succeeded=True)

    assert failure_root.is_dir()
    assert not success_root.exists()
