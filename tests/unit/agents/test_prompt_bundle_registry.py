# -*- coding: utf-8 -*-
"""Release, canary, diff and rollback contracts for Prompt Bundles."""

import json

import pytest

from lantai.agents.prompt_bundle import PromptBundleRegistry
from lantai.agents.prompt_system import PromptRegistry, PromptSection


def _build(version: str, content: str):
    registry = PromptRegistry(language="zh")
    section = PromptSection(
        id="kernel.policy",
        content=content,
        owner="platform",
        version=version,
        required=True,
        cache_tier="kernel",
        source_kind="platform_policy",
    )
    registry.add(section)
    return registry.compile(), [section]


def test_prompt_bundle_requires_passed_gate_then_supports_rollback(tmp_path) -> None:
    store = PromptBundleRegistry(tmp_path)
    first, first_sections = _build("1", "policy one")
    second, second_sections = _build("2", "policy two")
    store.stage(first, first_sections, gate_evidence={"status": "pass"})
    store.stage(second, second_sections, gate_evidence={"status": "pass"})

    store.promote(first.manifest["prompt_build_id"], rollout="canary")
    store.promote(
        second.manifest["prompt_build_id"],
        rollout="stable",
        gate_evidence={"status": "pass", "evidence_id": "gate-4"},
    )
    rolled_back = store.rollback(first.manifest["prompt_build_id"])

    assert rolled_back["active_build_id"] == first.manifest["prompt_build_id"]
    assert rolled_back["previous_build_id"] == second.manifest["prompt_build_id"]
    diff = store.diff(
        first.manifest["prompt_build_id"],
        second.manifest["prompt_build_id"],
    )
    assert diff["changed"][0]["id"] == "kernel.policy"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["history"][-1]["action"] == "rollback"
    assert store.resolve_active_platform_sections()[0].content == "policy one"


def test_prompt_bundle_fails_closed_and_never_persists_user_overlay(tmp_path) -> None:
    store = PromptBundleRegistry(tmp_path)
    registry = PromptRegistry(language="zh")
    platform = PromptSection(
        id="kernel",
        content="platform canon",
        owner="platform",
        source_kind="platform_canon",
    )
    overlay = PromptSection(
        id="workspace",
        content="private user overlay canary",
        owner="user",
        source_kind="user_overlay",
    )
    registry.add(platform)
    registry.add(overlay)
    build = registry.compile()

    with pytest.raises(ValueError, match="passed gate evidence"):
        store.stage(build, [platform, overlay], gate_evidence={"status": "fail"})

    path = store.stage(
        build,
        [platform, overlay],
        gate_evidence={"status": "pass"},
    )
    persisted = path.read_text(encoding="utf-8")
    assert "platform canon" in persisted
    assert "private user overlay canary" not in persisted


def test_stable_promotion_requires_fresh_passed_release_gate(tmp_path) -> None:
    store = PromptBundleRegistry(tmp_path)
    build, sections = _build("1", "stable policy")
    build_id = build.manifest["prompt_build_id"]
    store.stage(
        build,
        sections,
        gate_evidence={"status": "pass", "evidence_id": "gate-3"},
    )

    with pytest.raises(ValueError, match="stable promotion requires"):
        store.promote(build_id, rollout="stable")
    with pytest.raises(ValueError, match="stable promotion requires"):
        store.promote(
            build_id,
            rollout="stable",
            gate_evidence={"status": "fail", "evidence_id": "gate-4"},
        )

    state = store.promote(
        build_id,
        rollout="stable",
        gate_evidence={"status": "pass", "evidence_id": "gate-4"},
    )
    assert state["release_gate"]["evidence_id"] == "gate-4"


def test_same_build_canary_to_stable_does_not_point_rollback_to_itself(
    tmp_path,
) -> None:
    store = PromptBundleRegistry(tmp_path)
    build, sections = _build("1", "stable policy")
    build_id = build.manifest["prompt_build_id"]
    store.stage(build, sections, gate_evidence={"status": "pass"})

    store.promote(build_id, rollout="canary")
    state = store.promote(
        build_id,
        rollout="stable",
        gate_evidence={"status": "pass", "evidence_id": "gate-4"},
    )

    assert state["active_build_id"] == build_id
    assert state["previous_build_id"] is None
    assert state["history"][-1]["previous_build_id"] is None


def test_active_bundle_resolution_fails_closed_on_content_tampering(tmp_path) -> None:
    store = PromptBundleRegistry(tmp_path)
    build, sections = _build("1", "trusted policy")
    build_id = build.manifest["prompt_build_id"]
    path = store.stage(
        build,
        sections,
        gate_evidence={"status": "pass", "evidence_id": "gate-3"},
    )
    store.promote(build_id, rollout="canary")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["platform_sections"][0]["content"] = "tampered policy"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="section hash mismatch"):
        store.resolve_active_platform_sections()
