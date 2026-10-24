# -*- coding: utf-8 -*-
"""Behavior locks for the R1 protected intelligence baseline gate."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_legacy_intelligence_assembly_baseline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_legacy_intelligence_assembly_baseline",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_transitions(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "transitions.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_checked_in_r1_is_immutable_and_chain_reaches_head() -> None:
    module = _load_module()

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        module.DEFAULT_TRANSITIONS,
    )

    assert report["passed"] is True
    assert report["checked_source_count"] == 8
    assert report["reviewed_transition_ids"][:26] == [
        "R4",
        "R6",
        "S1",
        "S3-CAPABILITIES",
        "PERSONA-CONTINUITY",
        "PERSONA-PRESENCE-S6-INTEGRATION",
        "SKILLS-BUILTIN-UPGRADE",
        "S7-A4.2-PERSONA-RECEIPT",
        "CHROME-DEVTOOLS-MCP-C2",
        "KNOWLEDGE-EXACT-SCOPE",
        "MEMORY-OWNERSHIP-PROJECTION",
        "PERSONA-WORK-PRESENCE",
        "MAC-USE-CONTROLLED-AUTOMATION",
        "CALLCHAIN-SKILL-CHANNEL-CONTEXT",
        "PI-AGENT-LOOP-PLUGIN-RUNTIME",
        "TOKENDANCE-VIDEO-GATED-ROUTING",
        "PRESENTATION-TRUTHFUL-DELIVERY",
        "MEMORY-FALLBACK-RELIABILITY",
        "COLLABORATION-WORKER-PERSONA-BOUNDARY",
        "PRIVATE-EDITOR-MEMORY-CAPTURE-SCOPE",
        "KDOCS-REFERENCE-PROJECTION",
        "EXTERNAL-ASSISTANT-RETIREMENT",
        "PERSONA-PROFESSIONAL-GROWTH-WORKSPACE-SYNC",
        "BETA-ACCESS-MEMORY-ADMISSION",
        "INTEGRATION-ATTACHMENT-WIRE-PROJECTION",
        "CROSS-AUDIT-FIVE-PROTECTED-SOURCE-20261005",
    ]
    assert report["reviewed_transition_ids"][26:] == [
        "CROSS-AUDIT-COLD-MEMORY-BOOTSTRAP-20261005",
    ]
    assert report["terminal_commit"] == (
        "251cc707cd34bcedf6be76c90c720219c3941768"
    )
    assert report["baseline_integrity_errors"] == []
    assert report["transition_errors"] == []
    assert report["mismatches"] == []


def test_intelligence_baseline_rejects_unreviewed_source_drift(
    tmp_path: Path,
) -> None:
    module = _load_module()
    baseline = json.loads(module.DEFAULT_BASELINE.read_text(encoding="utf-8"))
    protected_path = "src/lantai/agents/legacy_agent_components.py"
    baseline["source_objects"][protected_path] = "bad-object"
    drifted = tmp_path / "drifted.json"
    drifted.write_text(json.dumps(baseline), encoding="utf-8")
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    transitions["immutable_baseline"]["path"] = str(drifted.resolve())
    transition_path = tmp_path / "transitions.json"
    transition_path.write_text(json.dumps(transitions), encoding="utf-8")

    report = module.check_baseline(ROOT, drifted, transition_path)

    assert report["passed"] is False
    assert report["baseline_integrity_errors"] == [
        "immutable baseline sha256 does not match reviewed manifest",
        "immutable baseline Git blob does not match reviewed manifest",
    ]
    assert report["transition_errors"] == [
        "R4: immutable baseline objects do not match "
        "the reviewed R4 recapture",
    ]
    assert report["mismatches"] == []


def test_intelligence_baseline_rejects_unapproved_transition(
    tmp_path: Path,
) -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    transitions["transitions"][25]["review"]["status"] = "pending"

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        _write_transitions(tmp_path, transitions),
    )

    assert report["passed"] is False
    assert report["reviewed_transition_ids"][:25] == [
        "R4",
        "R6",
        "S1",
        "S3-CAPABILITIES",
        "PERSONA-CONTINUITY",
        "PERSONA-PRESENCE-S6-INTEGRATION",
        "SKILLS-BUILTIN-UPGRADE",
        "S7-A4.2-PERSONA-RECEIPT",
        "CHROME-DEVTOOLS-MCP-C2",
        "KNOWLEDGE-EXACT-SCOPE",
        "MEMORY-OWNERSHIP-PROJECTION",
        "PERSONA-WORK-PRESENCE",
        "MAC-USE-CONTROLLED-AUTOMATION",
        "CALLCHAIN-SKILL-CHANNEL-CONTEXT",
        "PI-AGENT-LOOP-PLUGIN-RUNTIME",
        "TOKENDANCE-VIDEO-GATED-ROUTING",
        "PRESENTATION-TRUTHFUL-DELIVERY",
        "MEMORY-FALLBACK-RELIABILITY",
        "COLLABORATION-WORKER-PERSONA-BOUNDARY",
        "PRIVATE-EDITOR-MEMORY-CAPTURE-SCOPE",
        "KDOCS-REFERENCE-PROJECTION",
        "EXTERNAL-ASSISTANT-RETIREMENT",
        "PERSONA-PROFESSIONAL-GROWTH-WORKSPACE-SYNC",
        "BETA-ACCESS-MEMORY-ADMISSION",
        "INTEGRATION-ATTACHMENT-WIRE-PROJECTION",
    ]
    assert report["reviewed_transition_ids"][25:] == [
        "CROSS-AUDIT-COLD-MEMORY-BOOTSTRAP-20261005",
    ]
    assert report["transition_errors"] == [
        "CROSS-AUDIT-FIVE-PROTECTED-SOURCE-20261005: "
        "transition is not explicitly approved",
    ]


def test_intelligence_baseline_rejects_stale_evidence_binding(
    tmp_path: Path,
) -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    transitions["transitions"][1]["evidence"]["sha256"] = "stale"

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        _write_transitions(tmp_path, transitions),
    )

    assert report["passed"] is False
    assert report["transition_errors"] == [
        "R6: verification evidence digest mismatch",
    ]


def test_intelligence_baseline_rejects_tampered_terminal_evidence(
    tmp_path: Path,
) -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    terminal = transitions["transitions"][25]
    evidence_path = ROOT / terminal["evidence"]["path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["validation_revision"] = "tampered"
    tampered_path = tmp_path / "tampered-terminal-evidence.json"
    tampered_path.write_text(json.dumps(evidence), encoding="utf-8")
    terminal["evidence"]["path"] = str(tampered_path)

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        _write_transitions(tmp_path, transitions),
    )

    assert report["passed"] is False
    assert report["transition_errors"] == [
        "CROSS-AUDIT-FIVE-PROTECTED-SOURCE-20261005: "
        "verification evidence digest mismatch",
    ]


def test_chrome_transition_changes_only_the_protected_tool_registry() -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    chrome = next(
        transition
        for transition in transitions["transitions"]
        if transition["id"] == "CHROME-DEVTOOLS-MCP-C2"
    )

    assert chrome["id"] == "CHROME-DEVTOOLS-MCP-C2"
    assert chrome["changed_paths"] == ["src/lantai/agents/tool_registry.py"]
    changed = [
        path
        for path, before in chrome["source_objects_before"].items()
        if chrome["source_objects_after"][path] != before
    ]
    assert changed == ["src/lantai/agents/tool_registry.py"]


def test_knowledge_transition_changes_only_registry() -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    knowledge = next(
        transition
        for transition in transitions["transitions"]
        if transition["id"] == "KNOWLEDGE-EXACT-SCOPE"
    )

    assert knowledge["id"] == "KNOWLEDGE-EXACT-SCOPE"
    assert knowledge["from_commit"] == (
        "d5b21a2370671a7348717b7a1847b07477bf4033"
    )
    assert knowledge["to_commit"] == (
        "c589b9798038e811987416447984893c25267065"
    )
    assert knowledge["changed_paths"] == ["src/lantai/agents/tool_registry.py"]
    changed = [
        path
        for path, before in knowledge["source_objects_before"].items()
        if knowledge["source_objects_after"][path] != before
    ]
    assert changed == ["src/lantai/agents/tool_registry.py"]


def test_memory_ownership_transition_changes_only_memory() -> None:
    transitions = json.loads(
        _load_module().DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    memory = next(
        transition
        for transition in transitions["transitions"]
        if transition["id"] == "MEMORY-OWNERSHIP-PROJECTION"
    )

    assert memory["from_commit"] == (
        "c589b9798038e811987416447984893c25267065"
    )
    assert memory["to_commit"] == ("84819e1a382b61ebf5e172789bcefb8a39fe4715")
    assert memory["changed_paths"] == ["src/lantai/agents/memory"]


def test_persona_work_presence_changes_only_prompt_and_persona() -> None:
    transitions = json.loads(
        _load_module().DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    presence = next(
        transition
        for transition in transitions["transitions"]
        if transition["id"] == "PERSONA-WORK-PRESENCE"
    )

    assert presence["from_commit"] == (
        "84819e1a382b61ebf5e172789bcefb8a39fe4715"
    )
    assert presence["to_commit"] == (
        "0a6f8aee53c9e8c073f679d11479ec89b6700f5f"
    )
    assert presence["changed_paths"] == [
        "src/lantai/agents/legacy_agent_components.py",
        "src/lantai/agents/persona",
    ]


def test_mac_use_transition_changes_only_target_prompt_and_tool_registry() -> (
    None
):
    transitions = json.loads(
        _load_module().DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    mac_use = next(
        transition
        for transition in transitions["transitions"]
        if transition["id"] == "MAC-USE-CONTROLLED-AUTOMATION"
    )

    assert mac_use["from_commit"] == (
        "0a6f8aee53c9e8c073f679d11479ec89b6700f5f"
    )
    assert mac_use["to_commit"] == ("93be97eeb30670b4606b90e6110d0685b6b8b7c3")
    assert mac_use["changed_paths"] == [
        "src/lantai/agents/legacy_lantai_agent.py",
        "src/lantai/agents/tool_registry.py",
    ]


def test_callchain_transition_changes_only_host_skill_channel_binding() -> (
    None
):
    transitions = json.loads(
        _load_module().DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    transition = next(
        item
        for item in transitions["transitions"]
        if item["id"] == "CALLCHAIN-SKILL-CHANNEL-CONTEXT"
    )
    assert transition["id"] == "CALLCHAIN-SKILL-CHANNEL-CONTEXT"
    assert transition["changed_paths"] == [
        "src/lantai/agents/tool_registry.py",
    ]
    before = transition["source_objects_before"]
    after = transition["source_objects_after"]
    assert [path for path in before if before[path] != after[path]] == (
        transition["changed_paths"]
    )
    assert len(before) == len(after) == 8


def test_pi_transition_is_contiguous_and_registry_only() -> None:
    transitions = json.loads(
        _load_module().DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    pi_plugin = next(
        transition
        for transition in transitions["transitions"]
        if transition["id"] == "PI-AGENT-LOOP-PLUGIN-RUNTIME"
    )

    assert pi_plugin["id"] == "PI-AGENT-LOOP-PLUGIN-RUNTIME"
    assert pi_plugin["from_commit"] == (
        "fb3d0c00edcd00241c39eb5ebdc2b3a0dee948ec"
    )
    assert pi_plugin["to_commit"] == (
        "8aafbe40313ff8153add83f218fc357c6c509bec"
    )
    assert pi_plugin["changed_paths"] == ["src/lantai/agents/tool_registry.py"]
    before = pi_plugin["source_objects_before"]
    after = pi_plugin["source_objects_after"]
    changed = [path for path in before if before[path] != after[path]]
    assert changed == pi_plugin["changed_paths"]
    assert len(before) == len(after) == 8


def test_post_pi_chain_records_only_actual_protected_changes() -> None:
    transitions = json.loads(
        _load_module().DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    expected = [
        (
            "TOKENDANCE-VIDEO-GATED-ROUTING",
            "8aafbe40313ff8153add83f218fc357c6c509bec",
            "f79bf29f8dd745a776aa6e00f5324a2e9f7d71cb",
            ["src/lantai/agents/tool_registry.py"],
        ),
        (
            "PRESENTATION-TRUTHFUL-DELIVERY",
            "f79bf29f8dd745a776aa6e00f5324a2e9f7d71cb",
            "9c56cb1cf282afaf1590fd957e831204269d641c",
            ["src/lantai/agents/tool_registry.py"],
        ),
        (
            "MEMORY-FALLBACK-RELIABILITY",
            "9c56cb1cf282afaf1590fd957e831204269d641c",
            "55fc2ff20c58378abd9086c4fb256ae3cdbaaf59",
            ["src/lantai/agents/memory"],
        ),
        (
            "COLLABORATION-WORKER-PERSONA-BOUNDARY",
            "55fc2ff20c58378abd9086c4fb256ae3cdbaaf59",
            "06d2fd464a3aba2fd2f3090c0801de75da45fb5c",
            ["src/lantai/agents/legacy_lantai_agent.py"],
        ),
        (
            "PRIVATE-EDITOR-MEMORY-CAPTURE-SCOPE",
            "06d2fd464a3aba2fd2f3090c0801de75da45fb5c",
            "33654adba1bf789e896a5777e8889c42c693a381",
            ["src/lantai/agents/context"],
        ),
        (
            "KDOCS-REFERENCE-PROJECTION",
            "33654adba1bf789e896a5777e8889c42c693a381",
            "dffde52995b7bda7e57d1e727556bc10875f7f92",
            ["src/lantai/agents/legacy_lantai_agent.py"],
        ),
        (
            "EXTERNAL-ASSISTANT-RETIREMENT",
            "dffde52995b7bda7e57d1e727556bc10875f7f92",
            "247aa76be9036971a82044aff4bcca8be5232a5f",
            ["src/lantai/agents/tool_registry.py"],
        ),
        (
            "PERSONA-PROFESSIONAL-GROWTH-WORKSPACE-SYNC",
            "247aa76be9036971a82044aff4bcca8be5232a5f",
            "9dc9e7930f40e7d7a52c72397c5001086ab5bc7e",
            [
                "src/lantai/agents/legacy_agent_components.py",
                "src/lantai/agents/persona",
            ],
        ),
        (
            "BETA-ACCESS-MEMORY-ADMISSION",
            "9dc9e7930f40e7d7a52c72397c5001086ab5bc7e",
            "42a171aa40a4238b19c46f6535087ad702557456",
            ["src/lantai/agents/memory"],
        ),
        (
            "INTEGRATION-ATTACHMENT-WIRE-PROJECTION",
            "42a171aa40a4238b19c46f6535087ad702557456",
            "aff0bfa9a23b4ec0b8155d6405e6c23972e5aeb3",
            ["src/lantai/runtime/converters"],
        ),
    ]

    for transition_id, from_commit, to_commit, changed_paths in expected:
        transition = next(
            item
            for item in transitions["transitions"]
            if item["id"] == transition_id
        )
        assert transition["from_commit"] == from_commit
        assert transition["to_commit"] == to_commit
        assert transition["changed_paths"] == changed_paths
        before = transition["source_objects_before"]
        after = transition["source_objects_after"]
        assert [path for path in before if before[path] != after[path]] == (
            changed_paths
        )
        assert len(before) == len(after) == 8


def test_intelligence_baseline_still_rejects_unapproved_historical_25(
    tmp_path: Path,
) -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    transitions["transitions"][24]["review"]["status"] = "pending"

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        _write_transitions(tmp_path, transitions),
    )

    assert report["passed"] is False
    assert report["reviewed_transition_ids"][:25] == [
        "R4",
        "R6",
        "S1",
        "S3-CAPABILITIES",
        "PERSONA-CONTINUITY",
        "PERSONA-PRESENCE-S6-INTEGRATION",
        "SKILLS-BUILTIN-UPGRADE",
        "S7-A4.2-PERSONA-RECEIPT",
        "CHROME-DEVTOOLS-MCP-C2",
        "KNOWLEDGE-EXACT-SCOPE",
        "MEMORY-OWNERSHIP-PROJECTION",
        "PERSONA-WORK-PRESENCE",
        "MAC-USE-CONTROLLED-AUTOMATION",
        "CALLCHAIN-SKILL-CHANNEL-CONTEXT",
        "PI-AGENT-LOOP-PLUGIN-RUNTIME",
        "TOKENDANCE-VIDEO-GATED-ROUTING",
        "PRESENTATION-TRUTHFUL-DELIVERY",
        "MEMORY-FALLBACK-RELIABILITY",
        "COLLABORATION-WORKER-PERSONA-BOUNDARY",
        "PRIVATE-EDITOR-MEMORY-CAPTURE-SCOPE",
        "KDOCS-REFERENCE-PROJECTION",
        "EXTERNAL-ASSISTANT-RETIREMENT",
        "PERSONA-PROFESSIONAL-GROWTH-WORKSPACE-SYNC",
        "BETA-ACCESS-MEMORY-ADMISSION",
        "CROSS-AUDIT-FIVE-PROTECTED-SOURCE-20261005",
    ]
    assert report["reviewed_transition_ids"][25:] == [
        "CROSS-AUDIT-COLD-MEMORY-BOOTSTRAP-20261005",
    ]
    assert report["transition_errors"] == [
        "INTEGRATION-ATTACHMENT-WIRE-PROJECTION: "
        "transition is not explicitly approved",
    ]


def test_intelligence_baseline_still_rejects_tampered_historical_25(
    tmp_path: Path,
) -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    terminal = transitions["transitions"][24]
    evidence_path = ROOT / terminal["evidence"]["path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["validation_revision"] = "tampered"
    tampered_path = tmp_path / "tampered-terminal-evidence.json"
    tampered_path.write_text(json.dumps(evidence), encoding="utf-8")
    terminal["evidence"]["path"] = str(tampered_path)

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        _write_transitions(tmp_path, transitions),
    )

    assert report["passed"] is False
    assert report["transition_errors"] == [
        "INTEGRATION-ATTACHMENT-WIRE-PROJECTION: "
        "verification evidence digest mismatch",
    ]


def test_reviewed_26_extends_25_and_changes_only_five_surfaces() -> None:
    transitions = json.loads(
        _load_module().DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )["transitions"][:26]
    assert len(transitions) == 26
    assert transitions[24]["id"] == "INTEGRATION-ATTACHMENT-WIRE-PROJECTION"
    assert (
        transitions[24]["to_commit"]
        == "aff0bfa9a23b4ec0b8155d6405e6c23972e5aeb3"
    )
    terminal = transitions[25]
    assert terminal["id"] == "CROSS-AUDIT-FIVE-PROTECTED-SOURCE-20261005"
    assert (
        terminal["from_commit"] == "aff0bfa9a23b4ec0b8155d6405e6c23972e5aeb3"
    )
    assert terminal["to_commit"] == "b2dce4bd0004aebff8d94ce6501512c8a1604870"
    assert terminal["changed_paths"] == [
        "src/lantai/agents/legacy_agent_components.py",
        "src/lantai/agents/legacy_lantai_agent.py",
        "src/lantai/agents/memory",
        "src/lantai/agents/persona",
        "src/lantai/agents/tool_registry.py",
    ]
    before = terminal["source_objects_before"]
    after = terminal["source_objects_after"]
    assert [
        path for path in before if before[path] != after[path]
    ] == terminal["changed_paths"]
    assert len(before) == len(after) == 8


def test_reviewed_27_extends_26_only_for_cold_memory_registration() -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )["transitions"]
    assert len(transitions) == 27
    previous = transitions[25]
    terminal = transitions[26]
    assert previous["id"] == "CROSS-AUDIT-FIVE-PROTECTED-SOURCE-20261005"
    assert terminal["id"] == "CROSS-AUDIT-COLD-MEMORY-BOOTSTRAP-20261005"
    assert terminal["from_commit"] == previous["to_commit"]
    assert terminal["to_commit"] == "251cc707cd34bcedf6be76c90c720219c3941768"
    before = terminal["source_objects_before"]
    after = terminal["source_objects_after"]
    assert before == previous["source_objects_after"]
    assert len(before) == len(after) == 8
    assert [path for path in before if before[path] != after[path]] == [
        "src/lantai/agents/memory",
    ]
    assert terminal["changed_paths"] == ["src/lantai/agents/memory"]
    assert after["src/lantai/agents/memory"] == (
        "2730fb5f176c973448502a5ebd27cdd270c0ebf8"
    )
    evidence = json.loads(
        (ROOT / terminal["evidence"]["path"]).read_text(encoding="utf-8"),
    )
    assert evidence["source_commit"] == terminal["to_commit"]
    assert evidence["changed_leaf_paths"] == [
        "src/lantai/agents/memory/base_memory_manager.py",
    ]
    assert evidence["source_objects_before"] == before
    assert evidence["source_objects_after"] == after
    assert evidence["golden_string_count"] == 994
    assert evidence["review"]["status"] == "approved"


def test_intelligence_baseline_rejects_missing_cold_memory_transition(
    tmp_path: Path,
) -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    assert len(transitions["transitions"]) == 27
    transitions["transitions"].pop()

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        _write_transitions(tmp_path, transitions),
    )

    assert report["passed"] is False
    assert report["transition_errors"] == []
    assert report["mismatches"] == [
        {
            "path": "src/lantai/agents/memory",
            "expected": "018aac0279df806077b30c72f8492d6c0f2ef2fa",
            "actual": "2730fb5f176c973448502a5ebd27cdd270c0ebf8",
        },
    ]


def test_intelligence_baseline_rejects_unapproved_cold_memory_transition(
    tmp_path: Path,
) -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    assert len(transitions["transitions"]) == 27
    transitions["transitions"][26]["review"]["status"] = "pending"

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        _write_transitions(tmp_path, transitions),
    )

    assert report["passed"] is False
    assert len(report["reviewed_transition_ids"]) == 26
    assert report["transition_errors"] == [
        "CROSS-AUDIT-COLD-MEMORY-BOOTSTRAP-20261005: "
        "transition is not explicitly approved",
    ]
    assert report["mismatches"] == []


def test_intelligence_baseline_rejects_tampered_cold_memory_evidence(
    tmp_path: Path,
) -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    assert len(transitions["transitions"]) == 27
    transitions["transitions"][26]["evidence"]["sha256"] = "tampered"

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        _write_transitions(tmp_path, transitions),
    )

    assert report["passed"] is False
    assert report["transition_errors"] == [
        "CROSS-AUDIT-COLD-MEMORY-BOOTSTRAP-20261005: "
        "verification evidence digest mismatch",
    ]
    assert report["mismatches"] == []


def test_intelligence_baseline_rejects_rehashed_wrong_cold_memory_commit(
    tmp_path: Path,
) -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    assert len(transitions["transitions"]) == 27
    terminal = transitions["transitions"][26]
    evidence = json.loads(
        (ROOT / terminal["evidence"]["path"]).read_text(encoding="utf-8"),
    )
    evidence["source_commit"] = terminal["from_commit"]
    wrong_path = tmp_path / "rehashed-wrong-commit.json"
    wrong_path.write_text(json.dumps(evidence), encoding="utf-8")
    terminal["evidence"]["path"] = str(wrong_path)
    terminal["evidence"]["sha256"] = hashlib.sha256(
        wrong_path.read_bytes(),
    ).hexdigest()

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        _write_transitions(tmp_path, transitions),
    )

    assert report["passed"] is False
    assert report["transition_errors"] == [
        "CROSS-AUDIT-COLD-MEMORY-BOOTSTRAP-20261005: "
        "verification evidence commit mismatch",
    ]
    assert report["mismatches"] == []


def test_intelligence_baseline_rejects_narrowed_cold_memory_protected_set(
    tmp_path: Path,
) -> None:
    module = _load_module()
    transitions = json.loads(
        module.DEFAULT_TRANSITIONS.read_text(encoding="utf-8"),
    )
    assert len(transitions["transitions"]) == 27
    terminal = transitions["transitions"][26]
    terminal["source_objects_before"].pop("src/lantai/agents/persona")
    terminal["source_objects_after"].pop("src/lantai/agents/persona")

    report = module.check_baseline(
        ROOT,
        module.DEFAULT_BASELINE,
        _write_transitions(tmp_path, transitions),
    )

    assert report["passed"] is False
    assert report["transition_errors"] == [
        "CROSS-AUDIT-COLD-MEMORY-BOOTSTRAP-20261005: "
        "protected object chain is not contiguous",
        "CROSS-AUDIT-COLD-MEMORY-BOOTSTRAP-20261005: "
        "protected source set differs from the immutable baseline",
    ]
