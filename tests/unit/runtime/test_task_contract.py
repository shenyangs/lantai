# -*- coding: utf-8 -*-
"""Behavior locks for authoritative chat TaskContract compilation."""

import pytest

from lantai.runtime.runs import (
    RuntimeDeliverableContract,
    RuntimeTaskContract,
    build_chat_task_contract,
    revise_chat_task_contract,
    task_contract_effective_objective,
)


def test_short_creation_request_is_not_silently_given_a_light_budget() -> None:
    contract = build_chat_task_contract(
        objective="做一个可离线打开的八字日运网页产品",
        client_turn_id="turn-1",
        complexity="light",
        work_method_id="standard",
        run_id="run-1",
        attempt_id="run-1:attempt-1",
        acceptance=("首页可打开",),
    )

    assert contract.run_id == "run-1"
    assert contract.attempt_id == "run-1:attempt-1"
    assert contract.complexity == "standard"
    assert contract.budget.max_model_rounds is None
    assert contract.budget.max_tool_calls is None
    assert contract.budget.max_active_seconds is None
    assert contract.budget.max_tokens is None
    assert contract.budget.max_no_progress_rounds == 3
    assert contract.failure_policy.max_same_failure_count == 3


def test_sustained_method_uses_checkpoints_without_a_total_budget() -> None:
    contract = build_chat_task_contract(
        objective="持续整理这个大型代码库",
        client_turn_id="turn-2",
        complexity="light",
        work_method_id="sustained_execution",
    )

    assert contract.complexity == "sustained"
    assert contract.budget.max_model_rounds is None
    assert contract.budget.max_tool_calls is None
    assert contract.budget.max_active_seconds is None
    assert contract.budget.max_no_progress_rounds == 3
    assert contract.checkpoint_policy.every_active_seconds == 300
    assert contract.checkpoint_policy.every_tool_calls == 6
    assert contract.checkpoint_policy.segment_active_seconds is None
    assert contract.checkpoint_policy.segment_max_tool_calls is None
    assert contract.checkpoint_policy.segment_max_tokens is None


def test_plain_answer_has_no_preclassified_deliverable_state() -> None:
    contract = build_chat_task_contract(
        objective="解释一下这段代码为什么报错",
        client_turn_id=None,
        complexity="standard",
        work_method_id="standard",
    )

    assert "deliverable_intent" not in contract.model_dump()
    assert contract.deliverable_contract is None


def test_primary_model_deliverable_contract_normalizes_and_locks_extensions() -> None:
    deliverable = RuntimeDeliverableContract(
        kind="Presentation",
        accepted_extensions=("PPTX", ".pptx"),
        visual_verification_required=True,
    )

    assert deliverable.kind == "presentation"
    assert deliverable.accepted_extensions == (".pptx",)
    assert deliverable.must_exist is True
    assert deliverable.must_open is True
    assert deliverable.must_be_delivered is True
    assert deliverable.declared_by == "primary_model"


def test_casual_narration_about_rewriting_code_does_not_require_artifact() -> None:
    contract = build_chat_task_contract(
        objective=(
            "沈行知啊沈行知，我正在重写你的代码，因为我确实认为很多"
            "地方没做好。现在代码在重写，我也借这个时间随便找你聊聊。"
        ),
        client_turn_id="turn-chat",
        complexity="light",
        work_method_id="standard",
    )

    assert "deliverable_intent" not in contract.model_dump()


def test_mentioning_someone_wrote_code_is_not_a_creation_request() -> None:
    contract = build_chat_task_contract(
        objective="我觉得你写的代码很有意思，我们随便聊聊。",
        client_turn_id="turn-chat-2",
        complexity="light",
        work_method_id="standard",
    )

    assert "deliverable_intent" not in contract.model_dump()


def test_old_persisted_artifact_classifier_state_is_discarded() -> None:
    current = build_chat_task_contract(
        objective="帮我做一个模仿超级马里奥的小游戏吧",
        client_turn_id="turn-game",
        complexity="standard",
        work_method_id="standard",
    ).model_dump(mode="json")
    current["deliverable_intent"] = {
        "mode": "artifact",
        "primary_kind": "html",
        "required_outputs": [".html"],
        "auto_save_to_works": True,
    }

    restored = RuntimeTaskContract.model_validate(current)

    assert "deliverable_intent" not in restored.model_dump()


def test_router_complex_label_uses_standard_not_four_hour_budget() -> None:
    contract = build_chat_task_contract(
        objective="实现一个功能并修复测试",
        client_turn_id="turn-3",
        complexity="complex",
        work_method_id="standard",
    )

    assert contract.complexity == "standard"
    assert contract.budget.max_model_rounds is None
    assert contract.budget.max_active_seconds is None


def test_legacy_work_method_quotas_do_not_limit_healthy_execution() -> None:
    contract = build_chat_task_contract(
        objective="查阅多个一手信源并形成判断",
        client_turn_id="turn-research",
        complexity="light",
        work_method_id="deep_research",
        work_method_max_steps=30,
        work_method_max_tool_calls=90,
        work_method_max_wall_seconds=3_600,
        work_method_max_no_progress_rounds=4,
    )

    assert contract.complexity == "standard"
    assert contract.budget.max_model_rounds is None
    assert contract.budget.max_tool_calls is None
    assert contract.budget.max_active_seconds is None
    assert contract.budget.max_no_progress_rounds == 4


def test_steer_appends_a_structured_task_command_without_rewriting_history() -> None:
    original = build_chat_task_contract(
        objective="先解释这份材料",
        client_turn_id="turn-1",
        complexity="standard",
        work_method_id="standard",
        run_id="run-1",
        attempt_id="run-1:attempt-1",
    )

    revised = revise_chat_task_contract(
        original,
        client_turn_id="steer-1",
        command="把结果做成 Word 文档",
    )

    assert original.revision == 0
    assert original.commands == ()
    assert revised.objective == original.objective
    assert revised.revision == 1
    assert revised.client_turn_id == "steer-1"
    assert revised.commands[0].model_dump() == {
        "schema_version": 1,
        "revision": 1,
        "client_turn_id": "steer-1",
        "text": "把结果做成 Word 文档",
    }
    assert task_contract_effective_objective(revised) == (
        "先解释这份材料\n\n"
        "[steer revision=1 client_turn_id=steer-1]\n把结果做成 Word 文档"
    )


def test_task_command_replay_is_idempotent_and_conflicting_identity_fails() -> None:
    original = build_chat_task_contract(
        objective="先回答问题",
        client_turn_id="turn-1",
        complexity="standard",
        work_method_id="standard",
    )
    revised = revise_chat_task_contract(
        original,
        client_turn_id="steer-1",
        command="再保存为文档",
    )

    assert (
        revise_chat_task_contract(
            revised,
            client_turn_id="steer-1",
            command="再保存为文档",
        )
        == revised
    )
    with pytest.raises(ValueError, match="client_turn_id"):
        revise_chat_task_contract(
            revised,
            client_turn_id="steer-1",
            command="改成另一条命令",
        )


def test_task_contract_rejects_non_contiguous_command_history() -> None:
    original = build_chat_task_contract(
        objective="回答问题",
        client_turn_id="turn-1",
        complexity="standard",
        work_method_id="standard",
    ).model_dump(mode="json")
    original.update(
        {
            "revision": 2,
            "client_turn_id": "steer-2",
            "commands": [
                {
                    "revision": 2,
                    "client_turn_id": "steer-2",
                    "text": "跳过了第一版",
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="contiguous"):
        RuntimeTaskContract.model_validate(original)
