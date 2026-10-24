from __future__ import annotations

import json
from pathlib import Path

from lantai.analysis import AnalysisService
from lantai.runtime.analysis_context_projection import (
    current_analysis_context_message,
)


def _result(state_root: Path, *, scope: str) -> dict:
    source = state_root.parent / f"{scope}-sales.csv"
    source.write_text("region,amount\nEast,847263\n", encoding="utf-8")
    service = AnalysisService(state_root, scope=scope)
    dataset = service.ingest(source)
    return service.run(
        dataset["dataset_id"],
        {
            "group_by": ["region"],
            "metrics": [
                {"column": "amount", "op": "sum", "as": "total"},
            ],
        },
    )


def test_current_analysis_context_projects_only_minimal_sealed_metadata(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    result = _result(state_root, scope="chat-a")

    message = current_analysis_context_message(
        state_root=state_root,
        scope="chat-a",
    )

    assert message is not None
    assert message.role == "system"
    assert message.metadata == {
        "instruction_trust": "trusted_system",
        "instruction_origin": "runtime.current_analysis_context",
        "current_analysis_context": True,
        "analysis_id": result["analysis_id"],
        "source_sha256": result["evidence"]["source_sha256"],
    }
    text = message.text_content()
    assert result["analysis_id"] in text
    assert result["evidence"]["source_sha256"] in text
    assert "get_analysis_result" in text
    assert "refresh_analysis_result" in text
    assert "do not call equip_lantai_tools" in text
    assert "get_presentation_generation_status" in text
    assert "never use create_presentation" in text
    assert "omit sections" in text
    assert "numeric report facts" in text.lower()
    assert "full conversation" in text
    assert "847263" not in text
    assert str(tmp_path) not in text
    assert "rows" not in text
    assert "source_path" not in text


def test_current_analysis_context_never_crosses_conversation_scope(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    _result(state_root, scope="chat-a")

    assert current_analysis_context_message(
        state_root=state_root,
        scope="chat-b",
    ) is None


def test_current_analysis_context_fails_safe_on_corrupt_scoped_state(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    result = _result(state_root, scope="chat-a")
    service = AnalysisService(state_root, scope="chat-a")
    record = service.root / "results" / f"{result['analysis_id']}.json"
    record.write_text(
        json.dumps({"analysis_id": result["analysis_id"]}),
        encoding="utf-8",
    )

    assert current_analysis_context_message(
        state_root=state_root,
        scope="chat-a",
    ) is None
