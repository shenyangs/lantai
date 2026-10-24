"""Locks for the real finalizer schema and presentation recovery instructions."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lantai.app.runner import runner as runner_module
from lantai.app.runner.runner import AgentRunner
from lantai.runtime.model_round import ModelRoundProtocolError
from lantai.runtime.native_turn_support import _validate_tool_call_arguments
from lantai.runtime.tool_public_progress import without_public_progress_argument
from lantai.runtime.tools import LantaiToolRegistry, ToolCall


@pytest.fixture
def finalizer(tmp_path, monkeypatch):
    for name in ("build_lantai_tool_registry", "build_memory_tool_registry", "build_mcp_client_registry"):
        monkeypatch.setattr(runner_module, name, lambda *_, **__: LantaiToolRegistry())
    registry = AgentRunner(agent_id="default", workspace_dir=tmp_path)._build_native_tool_registry(
        agent_config=SimpleNamespace(), mcp_clients=[],
        request_context={"state_workspace_dir": str(tmp_path), "workspace_dir": str(tmp_path),
                         "run_id": "run-ppt-recovery", "chat_id": "chat-ppt-recovery"},
    )
    return registry.get_tool("finalize_deliverable")


def _arguments():
    return {"primary_path": "deck.pptx", "title": "报告", "kind": "presentation",
            "entrypoint": "deck.pptx", "verification_paths": [], "acceptance_evidence": []}


def test_undeclared_progress_field_is_rejected_before_python_dispatch(finalizer):
    spec, _executor = finalizer
    call = ToolCall(id="extra-progress", name=spec.name,
                    arguments={**_arguments(), "public_progress": "正在核对文件"})
    with pytest.raises(ModelRoundProtocolError, match="unexpected properties") as caught:
        _validate_tool_call_arguments(call, specs_by_name={spec.name: spec})
    # Correctable transport/schema feedback, not an unexpected-keyword crash.
    assert "primary_path" in str(caught.value)
    assert "正在核对文件" not in str(caught.value)


def test_reserved_progress_still_strips_before_strict_finalizer_validation(finalizer):
    spec, _executor = finalizer
    call = ToolCall(id="reserved-progress", name=spec.name, arguments={
        **_arguments(), "_lantai_public_progress": "正在核对文件", "_public_progress": "兼容别名",
    })
    cleaned = without_public_progress_argument(call)
    _validate_tool_call_arguments(cleaned, specs_by_name={spec.name: spec})
    assert cleaned.arguments == _arguments()
    assert spec.input_schema["additionalProperties"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("render_passed", [False, True])
async def test_visual_failure_returns_canonical_recovery_not_a_view_image_bypass(
    tmp_path, monkeypatch, finalizer, render_passed
):
    import lantai.app.completion_gate as gate

    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"preserved candidate")
    checks = (
        {"id": "visual_render_evidence", "passed": render_passed, "outcome": "passed" if render_passed else "not_run"},
        {"id": "visual_verification_evidence", "passed": False, "outcome": "not_run"},
    )

    async def rejected(**_kwargs):
        return SimpleNamespace(work_item=None, failure_summary="Visual verification incomplete.",
                               delivery_status="partial", primary_path=str(deck),
                               acceptance_results=checks, verification_receipt=None)

    monkeypatch.setattr(gate, "finalize_deliverable", rejected)
    _spec, executor = finalizer
    response = await executor(**{**_arguments(), "primary_path": str(deck)})
    assert response.is_error
    assert response.metadata["candidate_preserved"] is True
    assert response.metadata["side_effect_outcome"] == "known_not_applied"
    assert response.metadata["acceptance_results"] == list(checks)
    repair = response.metadata["delivery_repair"]
    text = response.content[0]["text"]
    assert "create_presentation" in text
    assert "get_presentation_generation_status" in text
    assert "finalize_presentation_generation" in text
    assert "view_image" in text
    assert "not" in text
    assert "every" in text
    assert repair["candidate_preserved"] is True
    assert deck.read_bytes() == b"preserved candidate"
