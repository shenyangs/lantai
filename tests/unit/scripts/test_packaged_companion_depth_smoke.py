from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib import request

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "packaged_companion_depth_smoke.py"


def _load_module():
    scripts_dir = str(SCRIPT_PATH.parent)
    sys.path.insert(0, scripts_dir)
    try:
        spec = importlib.util.spec_from_file_location(
            "packaged_companion_depth_smoke", SCRIPT_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(scripts_dir)


def test_latest_turn_reads_only_the_latest_user_sentinel() -> None:
    module = _load_module()
    payload = {
        "messages": [
            {"role": "user", "content": "LANTAI_DEPTH_USER_001_abc"},
            {"role": "assistant", "content": "answer"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "LANTAI_DEPTH_USER_012_abc"}],
            },
        ]
    }

    assert module._latest_turn(payload) == 12


def test_long_history_requires_every_user_and_answer_exactly_once() -> None:
    module = _load_module()
    messages = []
    for turn in range(1, 5):
        messages.extend(
            [
                {
                    "role": "user",
                    "content": f"LANTAI_DEPTH_USER_{turn:03d}_abc",
                },
                {
                    "role": "assistant",
                    "content": f"LANTAI_DEPTH_ANSWER_{turn:03d}_abc",
                },
            ]
        )
    payload = {
        "status": "idle",
        "pending_user_message": None,
        "messages": messages,
    }

    result = module._validate_long_history(payload, turns=4, run_token="abc")

    assert result["role_counts"] == {"user": 4, "assistant": 4}
    payload["messages"].append(
        {"role": "assistant", "content": "LANTAI_DEPTH_ANSWER_004_abc"}
    )
    with pytest.raises(AssertionError, match="duplicates"):
        module._validate_long_history(payload, turns=4, run_token="abc")


def _ququ_records() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for turn in range(1, 13):
        records.append(
            {
                "turn": turn,
                "repair": False,
                "baseline_selected": turn <= 6,
            }
        )
    modes = ["unique", "repeat_previous", "empty", "unique", "unique", "unique"]
    for turn, mode in enumerate(modes, 1):
        records.append(
            {
                "turn": turn,
                "repair": True,
                "baseline_selected": True,
                "repair_mode": mode,
            }
        )
    return records


def test_ququ_outcomes_prove_probability_bypass_and_no_fallback() -> None:
    module = _load_module()
    history = "\n".join(
        [
            "~>[echo] LANTAI_DEPTH_QUQU_FIRST_abc",
            "~>[echo] LANTAI_DEPTH_QUQU_004_abc",
            "~>[echo] LANTAI_DEPTH_QUQU_005_abc",
            "~>[echo] LANTAI_DEPTH_QUQU_006_abc",
            "~>[echo] LANTAI_DEPTH_QUQU_BYPASS_abc",
        ]
    )

    result = module._validate_ququ_outcomes(
        records=_ququ_records(),
        history_text=history,
        turns=12,
        run_token="abc",
    )

    assert result["baseline_ratio"] == 0.5
    assert result["visible_ququ_count"] == 5
    assert result["repeat_rejected"] is True
    assert result["empty_repair_has_no_fallback"] is True


def test_ququ_outcomes_reject_non_provider_local_fallback() -> None:
    module = _load_module()
    history = "\n".join(
        [
            "~>[echo] LANTAI_DEPTH_QUQU_FIRST_abc",
            "~>[echo] LANTAI_DEPTH_QUQU_004_abc",
            "~>[echo] LANTAI_DEPTH_QUQU_005_abc",
            "~>[echo] LANTAI_DEPTH_QUQU_006_abc",
            "~>[echo] LANTAI_DEPTH_QUQU_BYPASS_abc",
            "~>[echo] 一条本地兜底",
        ]
    )

    with pytest.raises(AssertionError, match="local Ququ fallback"):
        module._validate_ququ_outcomes(
            records=_ququ_records(),
            history_text=history,
            turns=12,
            run_token="abc",
        )


def test_loopback_provider_uses_current_post_answer_reflection_json_contract() -> None:
    module = _load_module()
    from lantai.agents.persona.post_answer_reflection import (
        parse_post_answer_reflection,
    )

    payload = {
        "model": "smoke",
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": "正文已经完成；请返回一条 ~>[lead] 或 ~>[echo]。",
            },
            {"role": "user", "content": "LANTAI_DEPTH_USER_001_abc"},
        ],
    }

    with module._DepthProviderServer(run_token="abc") as provider:
        call = request.Request(
            f"{provider.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(call, timeout=5) as response:
            completion = json.loads(response.read().decode("utf-8"))

    reflection = parse_post_answer_reflection(
        completion["choices"][0]["message"]["content"]
    )
    assert reflection is not None
    assert reflection.ququ_line == "~>[echo] LANTAI_DEPTH_QUQU_FIRST_abc"
    assert provider.records[0]["repair_mode"] == "unique"


def test_work_lineage_requires_v1_v2_v3_parent_chain() -> None:
    module = _load_module()
    works = [
        {
            "id": "v1",
            "version": 1,
            "parent_work_id": None,
            "lineage_root_id": "v1",
        },
        {
            "id": "v2",
            "version": 2,
            "parent_work_id": "v1",
            "lineage_root_id": "v1",
        },
        {
            "id": "v3",
            "version": 3,
            "parent_work_id": "v2",
            "lineage_root_id": "v1",
        },
    ]

    result = module._validate_work_lineage(
        works,
        original_id="v1",
        version_two_id="v2",
        version_three_id="v3",
    )

    assert result["versions"] == [1, 2, 3]
    assert result["parent_chain"] == [None, "v1", "v2"]


def test_loopback_provider_classifies_all_semantic_artifact_types() -> None:
    module = _load_module()
    payload = {
        "model": "smoke",
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": "You are an invisible JSON classifier.",
            },
            {"role": "user", "content": module.ARTIFACT_RESPONSE},
        ],
    }

    with module._DepthProviderServer(run_token="abc") as provider:
        call = request.Request(
            f"{provider.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(call, timeout=5) as response:
            completion = json.loads(response.read().decode("utf-8"))

    classified = json.loads(completion["choices"][0]["message"]["content"])
    assert [item["kind"] for item in classified["artifacts"]] == [
        "creative",
        "correspondence",
        "copy",
        "script",
    ]
    for item in classified["artifacts"]:
        assert module.ARTIFACT_RESPONSE.count(item["source_text"]) == 1
    assert provider.records[0]["request_kind"] == "artifact_classifier"


def test_loopback_provider_returns_one_specific_research_advisory() -> None:
    module = _load_module()
    payload = {
        "model": "smoke",
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": "You are an invisible JSON presentation analyst.",
            },
            {"role": "user", "content": module.RESEARCH_REPORT},
        ],
    }

    with module._DepthProviderServer(run_token="abc") as provider:
        call = request.Request(
            f"{provider.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(call, timeout=5) as response:
            completion = json.loads(response.read().decode("utf-8"))

    classified = json.loads(completion["choices"][0]["message"]["content"])
    assert classified["artifacts"] == []
    assert classified["advisory"] == {
        "label": "注意",
        "body": module.RESEARCH_ADVISORY_BODY,
    }
    assert provider.records[0]["request_kind"] == "research_advisory_classifier"
