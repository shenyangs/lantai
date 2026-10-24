from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.agents import legacy_lantai_agent
from lantai.agents.legacy_lantai_agent import LegacyLantaiAgent
from lantai.analysis import AnalysisService


def _agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_engine: str,
) -> LegacyLantaiAgent:
    monkeypatch.setattr(
        legacy_lantai_agent,
        "build_system_prompt_from_working_dir",
        lambda **_: "BASE PROMPT",
    )
    monkeypatch.setattr(legacy_lantai_agent, "build_multimodal_hint", lambda: "")
    monkeypatch.setattr(legacy_lantai_agent, "ququ_enabled", lambda: False)
    agent = object.__new__(LegacyLantaiAgent)
    agent._request_context = {
        "agent_id": "default",
        "runtime_engine": runtime_engine,
        "state_workspace_dir": str(tmp_path / "state"),
        "session_state_id": "state-chat-1",
    }
    agent._agent_config = SimpleNamespace(heartbeat=None, enableQuqu=False)
    agent._language = "zh"
    agent._workspace_dir = tmp_path
    agent._runtime_workspace_dir = tmp_path / "runtime-topic"
    agent._env_context = None
    agent.memory_manager = None
    return agent


def _seed_result(tmp_path: Path) -> dict:
    source = tmp_path / "private-sales.csv"
    source.write_text("region,amount\nNorth,928374\n", encoding="utf-8")
    service = AnalysisService(tmp_path / "state", scope="state-chat-1")
    dataset = service.ingest(source)
    return service.run(
        dataset["dataset_id"],
        {"metrics": [{"column": "amount", "op": "sum", "as": "total"}]},
    )


def test_legacy_prompt_projects_current_analysis_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _seed_result(tmp_path)
    agent = _agent(tmp_path, monkeypatch, runtime_engine="legacy")

    prompt = LegacyLantaiAgent._build_sys_prompt(agent)

    assert '<current-analysis-context enforcement="required">' in prompt
    assert result["analysis_id"] in prompt
    assert result["evidence"]["source_sha256"] in prompt
    assert "get_analysis_result" in prompt
    assert "928374" not in prompt
    assert str(tmp_path) not in prompt


def test_native_prompt_defers_analysis_projection_to_runtime_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _seed_result(tmp_path)
    agent = _agent(tmp_path, monkeypatch, runtime_engine="native")

    prompt = LegacyLantaiAgent._build_sys_prompt(agent)

    assert '<current-analysis-context enforcement="required">' not in prompt
    assert result["analysis_id"] not in prompt
