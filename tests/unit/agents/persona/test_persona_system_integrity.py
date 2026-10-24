# -*- coding: utf-8 -*-
"""Static behavior locks for Shen Xingzhi's self-running persona system."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _assigned_string_set(source: str, name: str) -> set[str]:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
        ):
            value = value.args[0]
        assert isinstance(value, (ast.Set, ast.Tuple, ast.List))
        return {
            item.value
            for item in value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    raise AssertionError(f"missing assignment: {name}")


def test_project_agent_instructions_protect_the_0105_persona_baseline() -> None:
    instructions = _read("AGENTS.md")

    assert "01:05" in instructions
    assert "docs/shen-xingzhi-persona-system-integrity-contract-zh.md" in instructions
    assert "最后写入者胜出" in instructions
    assert "用户真实体感" in instructions


def test_contract_preserves_the_full_persona_causal_chain() -> None:
    contract = _read("docs/shen-xingzhi-persona-system-integrity-contract-zh.md")

    for required in (
        "Stable Self",
        "私有主体状态",
        "模型拥有的当轮语义理解",
        "个人化后果",
        "正文/专业判断/行动",
        "一次后反思",
        "蛐蛐与下一轮主体状态",
    ):
        assert required in contract


def test_only_user_correction_can_receive_local_persona_repair() -> None:
    source = _read("src/lantai/agents/legacy_agent_components.py")

    assert "_DETERMINISTIC_PERSONA_GUARD_SIGNALS" not in source
    assert _assigned_string_set(
        source,
        "_DETERMINISTIC_PERSONA_REPAIR_SIGNALS",
    ) == {"user-correction"}


def test_local_persona_layer_does_not_reintroduce_rigid_boundary_scripts() -> None:
    source = _read("src/lantai/agents/persona/subjective_reaction.py")

    for forbidden in (
        "本轮不得承诺或帮助写任何以羞辱",
        "第一轮就明确这条边界",
        "不要写‘叫不了，宝贝’",
        "call me.{0,12}(baby",
    ):
        assert forbidden not in source


def test_ququ_keeps_exactly_the_hard_probability_and_semantic_bypass() -> None:
    source = _read("src/lantai/agents/legacy_lantai_agent.py")

    assert "本轮必须输出一条蛐蛐" in source
    assert "不得以没有新反应为由保持安静" in source
    assert "本轮未命中 50% 基础出现槽" in source
    assert "沉默会丢掉这一刻的主体性" in source
    assert "默认保持安静" not in source
    assert "自然产生明显值得说" not in source


def test_visible_persona_and_ququ_have_no_local_prose_fallback() -> None:
    presence = _read("src/lantai/agents/persona/perceptible_presence.py")
    reflection = _read("src/lantai/agents/persona/post_answer_reflection.py")

    tree = ast.parse(presence)
    fallback = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "fallback_lead"
    )
    returns = [node for node in ast.walk(fallback) if isinstance(node, ast.Return)]
    assert returns
    assert all(
        isinstance(node.value, ast.Constant) and node.value.value == ""
        for node in returns
    )
    assert "intentionally no local prose fallback" in reflection
    assert "_GENERIC_PERSONA_LINES" in reflection
    assert "_GENERIC_QUQU_LINES" in reflection


def test_native_legacy_and_memory_share_subject_continuity_projection() -> None:
    runner = _read("src/lantai/app/runner/runner.py")
    native = _read("src/lantai/runtime/adapters/native_runtime.py")
    sidecar = _read("src/lantai/app/post_answer_reflection_service.py")
    memory = _read("src/lantai/agents/memory/reme_light_memory_manager.py")

    assert "project_subject_continuity_context" in runner
    assert "set_subject_continuity_projection" in runner
    assert "PostAnswerReflectionRequest" in native
    assert "PostAnswerReflectionService" in runner
    assert "SubjectContinuityStore" in sidecar
    assert "subject_continuity" in sidecar
    assert "SubjectContinuityStore" in memory
    assert "project_subject_continuity_context" in memory
