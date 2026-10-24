# -*- coding: utf-8 -*-
"""Regression tests for per-turn work-state projection."""

from lantai.agents.persona.work_state import derive_work_state


def test_mentioning_code_or_files_does_not_manufacture_an_execution_contract() -> None:
    state = derive_work_state(
        "我只是想聊聊你之前写的代码和那个文件，今天不用修。"
    )

    assert state.id == "steady"
    assert state.compile_prompt("zh") == ""


def test_real_incident_still_projects_the_safety_sensitive_urgent_state() -> None:
    state = derive_work_state("线上事故，现在紧急止血。")

    assert state.id == "urgent"
    assert "先止血，再复盘" in state.compile_prompt("zh")
