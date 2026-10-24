# -*- coding: utf-8 -*-
"""Read-only World Ledger and least-context view behavior locks."""

from datetime import datetime, timezone

import pytest

from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.policy import MemoryPolicyResult
from lantai.app.capabilities.artifacts import register_artifact
from lantai.app.capabilities.models import Artifact
from lantai.app.wiki.fact_graph import (
    ExtractedEntity,
    ExtractedRelation,
    GraphDocument,
    GraphExtraction,
)
from lantai.app.wiki.fact_ingest import open_workspace_fact_graph
from lantai.runtime.coherence import (
    AuthorityGrant,
    AuthorityProvenance,
    ContextViewCompiler,
    ContextViewRequest,
    PermissionEnvelope,
    WorldLedgerCompiler,
)
from lantai.runtime.runs import (
    RuntimeRunContract,
    RuntimeRunOwner,
    build_chat_task_contract,
)
from lantai.runtime.store import RuntimeRunStore


NOW = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)


async def _seed_world(tmp_path) -> None:
    memory = CanonicalMemoryLedger(tmp_path)
    memory_event = memory.record_event(
        "请记住我默认喜欢简洁回答",
        source_message_id="message-1",
        session_id="session-1",
        source_role="user",
    )
    assert memory_event is not None
    memory.capture_fact(
        "用户默认喜欢简洁回答",
        memory_type="preference",
        event=memory_event,
        explicit_user_request=True,
        confidence=0.95,
        policy_result=MemoryPolicyResult(
            decision="confirm",
            scope="global",
            subject="user",
            utility_score=0.9,
            reason="explicit durable preference",
        ),
    )

    graph = open_workspace_fact_graph(tmp_path)
    document = GraphDocument(
        source_id="wiki-project",
        source_path="notes/project.md",
        content="澜台使用 Native Runtime。",
    )
    graph.sync_document(
        document,
        GraphExtraction(
            entities=[
                ExtractedEntity(local_id="lantai", name="澜台", kind="project"),
                ExtractedEntity(
                    local_id="runtime",
                    name="Native Runtime",
                    kind="runtime",
                ),
            ],
            relations=[
                ExtractedRelation(
                    subject_id="lantai",
                    predicate="使用",
                    object_id="runtime",
                    evidence_quote="澜台使用 Native Runtime",
                ),
            ],
        ),
    )

    contract = RuntimeRunContract(
        run_id="run-1",
        attempt_id="attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            user_id="user-1",
            agent_id="agent-1",
            session_id="session-1",
            workspace_id="alpha",
            channel="console",
        ),
        status="running",
        created_at=NOW,
        started_at=NOW,
        heartbeat_at=NOW,
        task_contract=build_chat_task_contract(
            objective="完成 Harness 改造",
            client_turn_id="client-1",
            complexity="light",
            work_method_id="standard",
            run_id="run-1",
            attempt_id="attempt-1",
        ),
        release_edition="official",
    )
    await RuntimeRunStore(tmp_path).save_contract(
        contract,
        expected_revision=None,
    )
    register_artifact(
        tmp_path,
        Artifact(
            id="artifact-1",
            title="Harness 方案",
            path="docs/harness.md",
            sha256="a" * 64,
            run_id="run-1",
            attempt_id="attempt-1",
            step_id="step-1",
            effect_id="effect-1",
            created_at=NOW,
        ),
    )


@pytest.mark.asyncio
async def test_world_ledger_projects_four_authorities_without_owning_them(
    tmp_path,
) -> None:
    await _seed_world(tmp_path)

    snapshot = WorldLedgerCompiler(
        tmp_path,
        workspace_id="alpha",
    ).compile_workspace()

    assert snapshot.verify_integrity()
    assert {row.source_kind for row in snapshot.evidence} >= {
        "memory",
        "wiki",
        "runtime",
        "artifact",
    }
    predicates = {claim.predicate for claim in snapshot.claims}
    assert "memory.preference" in predicates
    assert "使用" in predicates
    assert "runtime.status" in predicates
    assert "runtime.objective" in predicates
    assert "artifact.current_version" in predicates
    assert all(
        row.instruction_trust == "untrusted_external_evidence"
        for row in snapshot.evidence
    )


@pytest.mark.asyncio
async def test_context_view_is_task_scoped_and_does_not_leak_global_memory(
    tmp_path,
) -> None:
    await _seed_world(tmp_path)
    snapshot = WorldLedgerCompiler(
        tmp_path,
        workspace_id="alpha",
    ).compile_workspace()

    view = ContextViewCompiler(tmp_path).compile(
        snapshot,
        ContextViewRequest(
            task_id="continue-run-1",
            objective="继续 Harness 改造",
            allowed_scopes=("workspace:alpha", "run:run-1"),
        ),
    )

    assert view.verify_integrity()
    assert any(item.category == "goal" for item in view.items)
    assert any(item.category == "open_item" for item in view.items)
    assert any(item.category == "artifact" for item in view.items)
    assert all("用户默认喜欢简洁回答" not in item.statement for item in view.items)
    assert view.withheld_by_scope >= 1
    assert all(
        item.instruction_trust == "untrusted_external_evidence"
        for item in view.items
    )


@pytest.mark.asyncio
async def test_context_view_permission_envelope_can_only_narrow_scope(
    tmp_path,
) -> None:
    await _seed_world(tmp_path)
    snapshot = WorldLedgerCompiler(
        tmp_path,
        workspace_id="alpha",
    ).compile_workspace()
    authority = AuthorityProvenance(
        authority_id="policy-1",
        source_kind="runtime_policy",
        actor_kind="policy",
        actor_id="context-policy",
        source_ref="policy:snapshot-1",
        scopes=("workspace:alpha", "run:run-1"),
    )
    envelope = PermissionEnvelope(
        grants=(
            AuthorityGrant(
                capability="context.read",
                decision="allow",
                scope="workspace:alpha",
                authority=authority,
            ),
            AuthorityGrant(
                capability="context.read",
                decision="deny",
                scope="run:run-1",
                authority=authority,
            ),
        ),
    )

    view = ContextViewCompiler(tmp_path).compile(
        snapshot,
        ContextViewRequest(
            task_id="workspace-only",
            objective="查看工作区事实",
            allowed_scopes=("workspace:alpha", "run:run-1"),
            permission_envelope=envelope,
        ),
    )

    assert all(
        "runtime.objective" not in item.statement for item in view.items
    )
    assert all("runtime.status" not in item.statement for item in view.items)
    assert any("使用" in item.statement for item in view.items)


@pytest.mark.asyncio
async def test_temporary_context_view_is_not_persisted(tmp_path) -> None:
    await _seed_world(tmp_path)
    snapshot = WorldLedgerCompiler(
        tmp_path,
        workspace_id="alpha",
    ).compile_workspace(persist=False)
    view = ContextViewCompiler(tmp_path).compile(
        snapshot,
        ContextViewRequest(
            task_id="temporary",
            objective="临时查看",
            allowed_scopes=("workspace:alpha",),
            temporary=True,
        ),
    )

    assert view.temporary is True
    context_view_dir = (
        tmp_path / ".lantai" / "runtime" / "v1" / "context-views"
    )
    assert not context_view_dir.exists()
