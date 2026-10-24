# -*- coding: utf-8 -*-
"""Behavior locks for AgentRunner's current runtime-facing seams."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import lantai.app.runner.runner as runner_module
import lantai.app.approvals.service as approvals_service_module
import lantai.app.post_answer_reflection_service as reflection_service_module
import lantai.runtime.adapters.legacy_turn_assembly as legacy_turn_assembly_module
from lantai.app.beta_access.admission import (
    BetaAccessAdmissionDenied,
    BetaAccessAdmissionGuard,
)
from lantai.app.completion_gate import finalize_deliverable
from lantai.app.channels.runtime_schema import (
    ContentType,
    Message as ChannelMessage,
    MessageType,
    Role,
    RunStatus,
    TextContent,
)
from lantai.agents.persona.post_answer_reflection import (
    PostAnswerReflectionRequest,
)
from lantai.exceptions import UnknownAgentException
from lantai.app.experience.events import (
    build_runtime_events_response,
    build_runtime_run_events_response,
    record_chat_turn_settled,
    record_chat_turn_started,
)
from lantai.app.experience.runtime_runs import record_chat_runtime_run_started
from lantai.app.runner.runner import (
    AgentRunner,
    _is_provider_content_filter_error,
    _provider_content_filter_fallback_msg,
    _runtime_events_with_terminal_boundary,
    _runtime_attachment_manifest_message,
    _sanitize_recent_tool_results_after_content_filter,
    _trusted_current_attachments_from_channel_meta,
)
from lantai.runtime import RuntimeEvent, RuntimeInput, RuntimeMessage
from lantai.runtime.converters import LegacyMsg as Msg
from lantai.runtime.runs import (
    RuntimeDeliverableContract,
    RuntimeRunContract,
    RuntimeRunOwner,
    build_chat_task_contract,
)
from lantai.runtime.store import RuntimeRunStore
from lantai.runtime.tools import LantaiToolRegistry, ToolPolicyContext
from lantai.runtime.tool_response import ToolResponse
from lantai.security.beta_access import AccessSnapshot, BetaAccessStatus
from lantai.security.file_access_grants import (
    get_current_external_file_grants,
    issue_external_file_grant,
    set_current_external_file_grants,
)


class _RuntimeUserMsg:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text_content(self) -> str:
        return self._text


class _FakeSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.loaded: list[dict[str, object]] = []
        self.saved: list[dict[str, object]] = []

    async def load_session_state(self, **kwargs) -> None:
        self.events.append("load_session")
        self.loaded.append(kwargs)

    async def save_session_state(self, **kwargs) -> None:
        self.events.append("save_session")
        self.saved.append(kwargs)


class _FakeChatManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.touched: list[str] = []

    async def get_or_create_chat(self, *args, **kwargs) -> SimpleNamespace:
        self.events.append("get_chat")
        return SimpleNamespace(id="chat-db-id")

    async def get_chat(self, chat_id: str) -> SimpleNamespace:
        self.events.append("get_chat")
        return SimpleNamespace(
            id=chat_id,
            session_id="s-standard",
            user_id="u-1",
            channel="console",
            meta={"session_state_id": f"console:chat:{chat_id}"},
        )

    async def touch_chat(self, chat_id: str) -> None:
        self.events.append("touch_chat")
        self.touched.append(chat_id)


def test_native_deliverable_declaration_is_marked_for_progressive_bootstrap(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runner_module,
        "build_lantai_tool_registry",
        lambda **_kwargs: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_memory_tool_registry",
        lambda _manager: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_mcp_client_registry",
        lambda _clients, **_kwargs: LantaiToolRegistry(),
    )
    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)

    registry = runner._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context={
            "state_workspace_dir": str(tmp_path),
            "workspace_dir": str(tmp_path),
            "run_id": "run-ppt-declaration",
            "chat_id": "chat-ppt-declaration",
        },
        mcp_clients=[],
    )

    registered = registry.get_tool("declare_deliverable_contract")
    assert registered is not None
    spec, _executor = registered
    assert spec.metadata == {
        "runtime_owned": True,
        "deliverable_contract": True,
    }
    assert spec.input_schema["required"] == [
        "kind",
        "accepted_extensions",
        "visual_verification_required",
    ]
    normalized_description = " ".join(spec.description.split())
    assert "before calling equip_lantai_tools" in normalized_description
    assert 'accepted_extensions=[".pptx"]' in normalized_description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "decision",
        "job_owner_run_id",
        "expected_job_status",
        "expected_cancelled",
    ),
    [
        ("replace", "run-ppt-source", "cancelled", True),
        ("cancel", "run-ppt-source", "cancelled", True),
        ("continue", "run-ppt-source", "pending", False),
        ("replace", "run-ppt-original", "cancelled", True),
    ],
)
async def test_pending_ppt_choice_controls_only_its_exact_active_job(
    tmp_path,
    monkeypatch,
    decision,
    job_owner_run_id,
    expected_job_status,
    expected_cancelled,
) -> None:
    from lantai.presentation.jobs import (
        JobStatus,
        PresentationJobRequest,
        PresentationJobStore,
    )

    monkeypatch.setattr(
        runner_module,
        "build_lantai_tool_registry",
        lambda **_kwargs: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_memory_tool_registry",
        lambda _manager: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_mcp_client_registry",
        lambda _clients, **_kwargs: LantaiToolRegistry(),
    )

    now = datetime.now(timezone.utc)
    inherited = RuntimeDeliverableContract(
        kind="presentation",
        accepted_extensions=(".pptx",),
        visual_verification_required=True,
    )
    source_task = build_chat_task_contract(
        objective="生成一份 19 页 PPT",
        client_turn_id="turn-source",
        complexity="standard",
        work_method_id="standard",
        run_id="run-ppt-source",
        attempt_id="run-ppt-source:attempt-1",
    ).model_copy(update={"deliverable_contract": inherited})
    current_task = build_chat_task_contract(
        objective="改成只要提纲",
        client_turn_id="turn-replace",
        complexity="standard",
        work_method_id="standard",
        run_id="run-outline",
        attempt_id="run-outline:attempt-1",
    )
    owner = RuntimeRunOwner(
        user_id="user",
        agent_id="default",
        session_id="console:chat",
        workspace_id="workspace",
        channel="console",
    )
    run_store = RuntimeRunStore(tmp_path)
    await run_store.save_contract(
        RuntimeRunContract(
            run_id="run-ppt-source",
            attempt_id="run-ppt-source:attempt-1",
            source="chat",
            owner=owner,
            status="running",
            revision=3,
            created_at=now,
            started_at=now,
            heartbeat_at=now,
            task_contract=source_task,
            release_edition="official",
        ),
        expected_revision=None,
    )
    await run_store.save_contract(
        RuntimeRunContract(
            run_id="run-outline",
            attempt_id="run-outline:attempt-1",
            source="chat",
            owner=owner,
            status="running",
            revision=0,
            created_at=now,
            started_at=now,
            heartbeat_at=now,
            task_contract=current_task,
            release_edition="official",
        ),
        expected_revision=None,
    )
    job_store = PresentationJobStore(tmp_path / "presentation-jobs")
    pending = job_store.create(
        PresentationJobRequest(
            conversation_id="conversation-ppt",
            user_request="生成一份 19 页 PPT",
            slide_count=19,
        ),
        runtime_run_id=job_owner_run_id,
    )
    unrelated = job_store.create(
        PresentationJobRequest(
            conversation_id="conversation-ppt",
            user_request="另一份 PPT",
            slide_count=3,
        ),
        runtime_run_id="run-ppt-other",
    )

    registry = AgentRunner(
        agent_id="default",
        workspace_dir=tmp_path,
    )._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context={
            "state_workspace_dir": str(tmp_path),
            "workspace_dir": str(tmp_path),
            "run_id": "run-outline",
            "chat_id": "chat-outline",
            "pending_deliverable_context": {
                "previous_run_id": "run-ppt-source",
                "source_contract_revision": 3,
                "source_task_revision": source_task.revision,
                "pending_job_id": pending.job_id,
                "pending_job_owner_run_id": job_owner_run_id,
                "pending_job_conversation_id": "conversation-ppt",
                "deliverable_contract": inherited.model_dump(mode="json"),
            },
        },
        mcp_clients=[],
    )

    registered = registry.get_tool("resolve_pending_deliverable_choice")
    assert registered is not None
    _spec, executor = registered
    result = await executor(
        source_run_id="run-ppt-source",
        decision=decision,
    )

    assert result["decision"] == decision
    assert result["cancelled_pending_job_id"] == (
        pending.job_id if expected_cancelled else None
    )
    assert job_store.get(pending.job_id).status.value == expected_job_status
    assert job_store.get(unrelated.job_id).status == JobStatus.PENDING
    stored = await run_store.load_contract("run-outline")
    assert stored is not None and stored.task_contract is not None
    assert stored.task_contract.deliverable_contract == (
        inherited if decision == "continue" else None
    )


def test_runtime_finalize_tool_requires_reconciliation_after_unknown_outcome(
    tmp_path,
    monkeypatch,
) -> None:
    """A partial Works commit cannot be treated as crash-idempotent."""

    monkeypatch.setattr(
        runner_module,
        "build_lantai_tool_registry",
        lambda **_kwargs: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_memory_tool_registry",
        lambda _manager: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_mcp_client_registry",
        lambda _clients, **_kwargs: LantaiToolRegistry(),
    )
    registry = AgentRunner(
        agent_id="default",
        workspace_dir=tmp_path,
    )._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context={
            "state_workspace_dir": str(tmp_path),
            "workspace_dir": str(tmp_path),
            "run_id": "run-finalize-recovery",
            "chat_id": "chat-finalize-recovery",
        },
        mcp_clients=[],
    )

    registered = registry.get_tool("finalize_deliverable")
    assert registered is not None
    spec, _executor = registered
    assert spec.effect_recovery.idempotency == "not_idempotent"
    assert spec.effect_recovery.probe == "tool_specific"
    assert spec.effect_recovery.on_ambiguity == "probe_required"
    assert spec.metadata["runtime_owned"] is True
    assert spec.metadata["trusted_effect_settlement"] is True


def test_private_editor_mutation_tools_require_a_trusted_server_binding(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runner_module,
        "build_lantai_tool_registry",
        lambda **_kwargs: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_memory_tool_registry",
        lambda _manager: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_mcp_client_registry",
        lambda _clients, **_kwargs: LantaiToolRegistry(),
    )
    registry = AgentRunner(
        agent_id="default",
        workspace_dir=tmp_path,
    )._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context={
            "state_workspace_dir": str(tmp_path),
            "workspace_dir": str(tmp_path),
            "works_workspace_dir": str(tmp_path),
            "work_method_id": "private_editor",
            "private_editor_project_id": "client-forged-project",
            "private_editor_current_work_id": "client-forged-work",
            "private_editor_current_digest": "sha256:forged",
            "private_editor_record_revision": 0,
            "private_editor_revocation_epoch": 0,
        },
        current_user_text="请改稿。",
        mcp_clients=[],
    )

    assert registry.get_tool("stage_private_editor_candidate") is None
    assert registry.get_tool("record_private_editor_context") is None


def test_private_editor_runtime_binding_requires_complete_server_attestation() -> None:
    forged = {
        "work_method_id": "private_editor",
        "private_editor_project_id": "project-1",
        "private_editor_original_work_id": "work-original",
        "private_editor_current_work_id": "work-current",
        "private_editor_current_digest": "sha256:current",
        "private_editor_record_revision": 2,
        "private_editor_revocation_epoch": 1,
        "private_editor_project_status": "active",
    }
    assert runner_module._trusted_private_editor_binding_from_channel_meta(forged) == {}

    trusted = {
        **forged,
        "_trusted_private_editor_binding": True,
    }
    assert runner_module._trusted_private_editor_binding_from_channel_meta(trusted) == {
        **trusted,
        "work_method_id": "private_editor",
    }

    assert (
        runner_module._trusted_private_editor_binding_from_channel_meta(
            {**trusted, "private_editor_record_revision": True}
        )
        == {}
    )
    assert (
        runner_module._trusted_private_editor_binding_from_channel_meta(
            {**trusted, "private_editor_project_status": "disabled"}
        )
        == {}
    )


def test_private_editor_tools_fail_closed_when_bound_project_is_stale(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runner_module,
        "build_lantai_tool_registry",
        lambda **_kwargs: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_memory_tool_registry",
        lambda _manager: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_mcp_client_registry",
        lambda _clients, **_kwargs: LantaiToolRegistry(),
    )

    registry = AgentRunner(
        agent_id="default",
        workspace_dir=tmp_path,
    )._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context={
            "state_workspace_dir": str(tmp_path),
            "workspace_dir": str(tmp_path),
            "works_workspace_dir": str(tmp_path),
            "run_id": "run-stale-private-editor",
            "chat_id": "chat-private-editor",
            "work_method_id": "private_editor",
            "private_editor_project_id": "deleted-project",
            "private_editor_original_work_id": "work-original",
            "private_editor_current_work_id": "work-current",
            "private_editor_current_digest": "sha256:current",
            "private_editor_record_revision": 0,
            "private_editor_revocation_epoch": 0,
            "private_editor_project_status": "active",
            "_trusted_private_editor_binding": True,
        },
        current_user_text="请改稿。",
        mcp_clients=[],
    )

    assert registry.get_tool("stage_private_editor_candidate") is None
    assert registry.get_tool("record_private_editor_context") is None
    assert registry.get_tool("adopt_private_editor_candidate") is None


@pytest.fixture
def private_editor_turn(tmp_path, monkeypatch):
    from lantai.app.private_editor import PrivateEditorStore
    from lantai.app.works import add_work

    monkeypatch.setattr(runner_module, "build_lantai_tool_registry", lambda **_kw: LantaiToolRegistry())
    monkeypatch.setattr(runner_module, "build_memory_tool_registry", lambda _manager: LantaiToolRegistry())
    monkeypatch.setattr(runner_module, "build_mcp_client_registry", lambda _clients, **_kw: LantaiToolRegistry())
    source = tmp_path / "draft.md"
    source.write_text("第一段。\n\n第二段太长，第二段重复。", encoding="utf-8")
    work = add_work(tmp_path, source_path=source, allowed_roots=[tmp_path])
    store = PrivateEditorStore(tmp_path)
    store.update_settings(enabled=True)
    project = store.create_project(work.id, title="同轮连续编辑")
    working_copy = tmp_path / ".lantai" / "works" / "edits" / "continuity" / "draft.md"
    working_copy.parent.mkdir(parents=True)
    working_copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    project = store.bind_session(
        project.id,
        chat_id="chat-continuity",
        working_copy_path=working_copy,
        owned_by_experiment=True,
    )
    registry = AgentRunner(agent_id="default", workspace_dir=tmp_path)._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context={
            "state_workspace_dir": str(tmp_path),
            "workspace_dir": str(tmp_path),
            "works_workspace_dir": str(tmp_path),
            "run_id": "run-continuity",
            "chat_id": project.chat_id,
            "work_method_id": "private_editor",
            "private_editor_project_id": project.id,
            "private_editor_current_work_id": project.current_work_id,
            "private_editor_current_digest": project.current_digest,
            "private_editor_record_revision": project.record_revision,
            "private_editor_revocation_epoch": project.revocation_epoch,
            "private_editor_project_status": "active",
            "_trusted_private_editor_binding": True,
        },
        current_user_text="只压缩第二段。这篇不要第一人称。也找一个新切口。",
        mcp_clients=[],
    )
    return SimpleNamespace(
        registry=registry,
        store=store,
        project=project,
        record=registry.get_tool("record_private_editor_context")[1],
        stage=registry.get_tool("stage_private_editor_candidate")[1],
        angle=registry.get_tool("record_private_editor_angle_result")[1],
        statements=[{
            "kind": "author_intent",
            "text": "这篇不要第一人称。",
            "exact_user_words": "这篇不要第一人称。",
            "supersedes": [],
        }],
        groups=[{
            "target_anchor": "paragraph:2",
            "before": "第二段太长，第二段重复。",
            "after": "第二段更短。",
            "observation": "同一层意思重复。",
            "impact": "推进变慢。",
            "rationale": "压缩重复信息。",
            "evidence_refs": [f"current-work:{work.id}"],
            "costs": ["语气更直接"],
            "depends_on": [],
        }],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["author_paraphrase", "anchor", "overlap", "source"])
async def test_private_editor_validation_rejection_proves_no_effect(
    private_editor_turn, monkeypatch, invalid,
) -> None:
    from lantai.app.private_editor import PrivateEditorStore

    turn = private_editor_turn
    before = turn.store.get_project(turn.project.id).model_dump()
    writes = []
    original_write = PrivateEditorStore._write

    def track_write(store, data):
        writes.append(True)
        return original_write(store, data)

    monkeypatch.setattr(PrivateEditorStore, "_write", track_write)
    if invalid == "author_paraphrase":
        tool_name = "record_private_editor_context"
        result = await turn.record(
            author_statements=[{**turn.statements[0], "text": "不使用第一人称。"}],
            model_proposals=[],
        )
    elif invalid == "source":
        tool_name = "record_private_editor_angle_result"
        result = await turn.angle(proposals=[{
            "source_refs": ["source:other-project"],
            "key_idea": "另一个项目的方向", "why_now": "测试", "differs_from": "测试",
            "next_action": "测试", "open_questions": [],
        }], empty_reason="")
    else:
        tool_name = "stage_private_editor_candidate"
        groups = ([{**turn.groups[0], "before": "不存在的原文。"}] if invalid == "anchor"
                  else [{**turn.groups[0], "group_key": "one"},
                        {**turn.groups[0], "group_key": "two"}])
        result = await turn.stage(change_groups=groups, author_statements=[], model_proposals=[])

    assert isinstance(result, ToolResponse)
    assert result.is_error is True
    assert result.metadata["side_effect_outcome"] == "known_not_applied"
    assert result.metadata["error_code"] == "validation_failed"
    assert turn.registry.get_tool(tool_name)[0].metadata["trusted_effect_settlement"] is True
    assert writes == []
    assert turn.store.get_project(turn.project.id).model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["io", "value", "conflict"])
async def test_private_editor_post_commit_failure_remains_ambiguous(
    private_editor_turn, monkeypatch, failure,
) -> None:
    from lantai.app.private_editor import PrivateEditorConflictError, PrivateEditorStore

    turn = private_editor_turn
    original_write = PrivateEditorStore._write
    error_type = {"io": OSError, "value": ValueError, "conflict": PrivateEditorConflictError}[failure]

    def write_then_fail(store, data):
        original_write(store, data)
        raise error_type("lost response after commit")

    monkeypatch.setattr(PrivateEditorStore, "_write", write_then_fail)
    with pytest.raises(error_type, match="lost response after commit") as caught:
        await turn.stage(change_groups=turn.groups, author_statements=[], model_proposals=[])

    assert not getattr(caught.value, "_private_editor_known_not_applied", False)
    assert len(turn.store.get_project(turn.project.id).candidates) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first_tool", ["record", "stage", "angle"])
async def test_private_editor_own_mutation_can_be_followed_by_candidate_in_same_turn(
    private_editor_turn,
    first_tool,
) -> None:
    turn = private_editor_turn
    if first_tool == "record":
        first = await turn.record(author_statements=turn.statements, model_proposals=[])
    elif first_tool == "stage":
        first = await turn.stage(
            change_groups=turn.groups, author_statements=turn.statements, model_proposals=[]
        )
    else:
        first = await turn.angle(
            proposals=[{
                "source_refs": [f"current-work:{turn.project.current_work_id}"],
                "key_idea": "从重复信息如何影响阅读切入。",
                "why_now": "第二段存在重复。",
                "differs_from": "从读者核对成本展开。",
                "next_action": "先核对当前第二段。",
                "open_questions": [],
            }],
            empty_reason="",
        )
    second = await turn.stage(
        change_groups=turn.groups, author_statements=turn.statements, model_proposals=[]
    )
    saved = turn.store.get_project(turn.project.id)

    assert first["record_revision"] == 1
    assert second["record_revision"] == saved.record_revision == 2
    assert second["candidate"]["record_revision"] == 2
    assert saved.current_work_id == turn.project.current_work_id
    assert saved.current_digest == turn.project.current_digest
    assert turn.store.get_current_body(saved.id) == "第一段。\n\n第二段太长，第二段重复。"


@pytest.mark.asyncio
async def test_private_editor_own_revision_does_not_absorb_external_author_mutation(
    private_editor_turn,
) -> None:
    from lantai.app.private_editor import RecordEntryDraft

    turn = private_editor_turn
    await turn.record(author_statements=turn.statements, model_proposals=[])
    current = turn.store.get_project(turn.project.id)
    turn.store.add_record_entries(
        current.id,
        expected_current_work_id=current.current_work_id,
        expected_current_digest=current.current_digest,
        expected_record_revision=current.record_revision,
        expected_revocation_epoch=current.revocation_epoch,
        drafts=[RecordEntryDraft(
            kind="decision", text="先停止编辑。", source="user_explicit",
            source_ref="button:author-record", status="accepted",
        )],
    )
    before = turn.store.get_project(current.id)

    rejection = await turn.stage(change_groups=turn.groups, author_statements=[], model_proposals=[])
    assert isinstance(rejection, ToolResponse)
    assert rejection.is_error is True
    assert "state changed" in str(rejection.content)
    assert rejection.metadata["side_effect_outcome"] == "known_not_applied"

    assert turn.store.get_project(current.id).model_dump() == before.model_dump()


@pytest.mark.asyncio
async def test_private_editor_failed_candidate_does_not_advance_turn_revision(
    private_editor_turn,
) -> None:
    turn = private_editor_turn
    rejection = await turn.stage(
        change_groups=[{**turn.groups[0], "before": "不在原稿中的文字。"}],
        author_statements=turn.statements,
        model_proposals=[],
    )
    assert isinstance(rejection, ToolResponse)
    assert rejection.is_error is True
    assert "anchor matched" in str(rejection.content)
    assert rejection.metadata["side_effect_outcome"] == "known_not_applied"

    result = await turn.stage(
        change_groups=turn.groups, author_statements=turn.statements, model_proposals=[]
    )
    assert result["record_revision"] == 1
    assert len(turn.store.get_project(turn.project.id).candidates) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "ambiguous", "overlap"])
async def test_private_editor_anchor_failure_points_back_to_authorized_current_draft(
    private_editor_turn, invalid,
) -> None:
    from lantai.app.works import get_managed_work_file
    from lantai.security.file_access_grants import authorize_external_file_path

    turn = private_editor_turn
    await turn.record(author_statements=turn.statements, model_proposals=[])
    current = turn.store.get_project(turn.project.id)
    _work, current_path = get_managed_work_file(turn.store.workspace_dir, current.current_work_id)
    grant = issue_external_file_grant(current_path, operations={"read"})
    groups = {
        "missing": [{**turn.groups[0], "before": "旧副本中的句子。"}],
        "ambiguous": [{**turn.groups[0], "before": "第二段"}],
        "overlap": [{**turn.groups[0], "group_key": "one"},
                    {**turn.groups[0], "group_key": "two"}],
    }[invalid]
    set_current_external_file_grants([grant])
    try:
        rejection = await turn.stage(change_groups=groups, author_statements=[], model_proposals=[])

        assert isinstance(rejection, ToolResponse)
        assert rejection.is_error is True
        assert rejection.metadata["side_effect_outcome"] == "known_not_applied"
        assert rejection.metadata["current_work_recovery"] == {
            "current_work_id": current.current_work_id,
            "current_digest": current.current_digest,
            "read_only_path": str(current_path),
            "read_tool": "read_file",
        }
        assert "read_file" in str(rejection.content)
        assert str(current_path) in str(rejection.content)
        assert turn.store.get_project(current.id).model_dump() == current.model_dump()
        verified_path = authorize_external_file_path(current_path, operation="read")
        body = verified_path.read_text(encoding="utf-8")
        assert turn.groups[0]["before"] in body

        candidate = await turn.stage(
            change_groups=turn.groups, author_statements=[], model_proposals=[],
        )
        assert candidate["candidate"]["base_work_id"] == current.current_work_id
        assert candidate["record_revision"] == current.record_revision
        assert turn.store.get_current_body(current.id) == body
    finally:
        set_current_external_file_grants(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["no_grant", "expired", "body", "author_record", "disabled", "deleted"])
async def test_private_editor_anchor_recovery_never_renews_or_expands_read_authority(
    private_editor_turn, monkeypatch, changed,
) -> None:
    from dataclasses import replace
    from lantai.app.private_editor import PrivateEditorConflictError, PrivateEditorNotFoundError, PrivateEditorStore, RecordEntryDraft
    from lantai.app.works import get_managed_work_file

    turn = private_editor_turn
    _work, current_path = get_managed_work_file(turn.store.workspace_dir, turn.project.current_work_id)
    grant = issue_external_file_grant(current_path, operations={"read"})
    grants = [] if changed == "no_grant" else [replace(grant, expires_at=0) if changed == "expired" else grant]
    original_stage = PrivateEditorStore.stage_candidate

    def stage_then_change_authority(store, *args, **kwargs):
        try:
            return original_stage(store, *args, **kwargs)
        except PrivateEditorConflictError:
            if changed == "body":
                current_path.write_text("正文在验证之后被外部修改。", encoding="utf-8")
            elif changed == "disabled":
                turn.store.update_settings(enabled=False)
            elif changed == "deleted":
                turn.store.delete_project(turn.project.id)
            elif changed == "author_record":
                turn.store.add_record_entries(
                    turn.project.id,
                    expected_current_work_id=turn.project.current_work_id,
                    expected_current_digest=turn.project.current_digest,
                    expected_record_revision=turn.project.record_revision,
                    expected_revocation_epoch=turn.project.revocation_epoch,
                    drafts=[RecordEntryDraft(
                        kind="decision", text="暂时停止编辑。", source="user_explicit",
                        source_ref="button:author-record", status="accepted",
                    )],
                )
            raise

    monkeypatch.setattr(PrivateEditorStore, "stage_candidate", stage_then_change_authority)
    set_current_external_file_grants(grants)
    try:
        rejection = await turn.stage(
            change_groups=[{**turn.groups[0], "before": "旧副本中的句子。"}],
            author_statements=[], model_proposals=[],
        )
        assert isinstance(rejection, ToolResponse)
        assert rejection.is_error is True
        assert "current_work_recovery" not in rejection.metadata
        assert str(current_path) not in str(rejection.content)
        assert get_current_external_file_grants() == tuple(grants)
        if changed == "deleted":
            with pytest.raises(PrivateEditorNotFoundError):
                turn.store.get_project(turn.project.id)
        else:
            assert not turn.store.get_project(turn.project.id).candidates
    finally:
        set_current_external_file_grants(None)


@pytest.mark.asyncio
async def test_private_editor_parallel_own_records_preserve_both_results(
    private_editor_turn,
) -> None:
    turn = private_editor_turn
    results = await asyncio.gather(*(
        turn.record(
            author_statements=[],
            model_proposals=[{"kind": "decision", "text": text, "supersedes": []}],
        )
        for text in ("可能需要压缩第二段。", "可能需要调整段落衔接。")
    ))
    saved = turn.store.get_project(turn.project.id)

    assert sorted(result["record_revision"] for result in results) == [1, 2]
    assert saved.record_revision == len(saved.record_entries) == 2
    assert all(entry.source == "model_inferred" and entry.status == "proposed" for entry in saved.record_entries)


@pytest.mark.asyncio
async def test_private_editor_candidate_tool_preserves_authority_and_current_draft(
    tmp_path,
    monkeypatch,
) -> None:
    from lantai.app.private_editor import PrivateEditorStore
    from lantai.app.works import add_work

    monkeypatch.setattr(
        runner_module,
        "build_lantai_tool_registry",
        lambda **_kwargs: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_memory_tool_registry",
        lambda _manager: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_mcp_client_registry",
        lambda _clients, **_kwargs: LantaiToolRegistry(),
    )
    source = tmp_path / "draft.md"
    source.write_text("第一段。\n\n第二段太长，第二段重复。", encoding="utf-8")
    work = add_work(tmp_path, source_path=source, allowed_roots=[tmp_path])
    store = PrivateEditorStore(tmp_path)
    store.update_settings(enabled=True)
    project = store.create_project(work.id, title="测试作品")
    working_copy = tmp_path / ".lantai" / "works" / "edits" / "test" / "draft.md"
    working_copy.parent.mkdir(parents=True)
    working_copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    project = store.bind_session(
        project.id,
        chat_id="chat-private-editor",
        working_copy_path=working_copy,
        owned_by_experiment=True,
    )
    request_context = {
        "state_workspace_dir": str(tmp_path),
        "workspace_dir": str(tmp_path),
        "works_workspace_dir": str(tmp_path),
        "run_id": "run-private-editor",
        "chat_id": "chat-private-editor",
        "work_method_id": "private_editor",
        "private_editor_project_id": project.id,
        "private_editor_current_work_id": project.current_work_id,
        "private_editor_current_digest": project.current_digest,
        "private_editor_record_revision": project.record_revision,
        "private_editor_revocation_epoch": project.revocation_epoch,
        "private_editor_project_status": "active",
        "_trusted_private_editor_binding": True,
    }
    registry = AgentRunner(
        agent_id="default",
        workspace_dir=tmp_path,
    )._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context=request_context,
        current_user_text="只压缩第二段。这篇不要第一人称。",
        mcp_clients=[],
    )
    registered = registry.get_tool("stage_private_editor_candidate")
    assert registered is not None
    spec, executor = registered
    assert registry.get_tool("record_private_editor_context") is not None
    assert spec.metadata["private_editor_project_scoped"] is True
    assert spec.effect_recovery.on_ambiguity == "probe_required"

    rejection = await executor(
        change_groups=[
            {
                "target_anchor": "paragraph:2",
                "before": "第二段太长，第二段重复。",
                "after": "第二段更短。",
                "observation": "同一层意思重复。",
                "impact": "推进变慢。",
                "rationale": "压缩重复信息。",
                "evidence_refs": ["current-draft:paragraph:2"],
                "costs": ["语气更直接"],
                "depends_on": [],
            }
        ],
        author_statements=[
            {
                "kind": "author_intent",
                "text": "作者希望全文更客观。",
                "exact_user_words": "这篇不要第一人称。",
                "supersedes": [],
            }
        ],
        model_proposals=[],
    )
    assert isinstance(rejection, ToolResponse)
    assert rejection.is_error is True
    assert "作者原话" in str(rejection.content)
    assert rejection.metadata["side_effect_outcome"] == "known_not_applied"

    result = await executor(
        change_groups=[
            {
                "target_anchor": "paragraph:2",
                "before": "第二段太长，第二段重复。",
                "after": "第二段更短。",
                "observation": "同一层意思重复。",
                "impact": "推进变慢。",
                "rationale": "压缩重复信息。",
                "evidence_refs": ["current-draft:paragraph:2"],
                "costs": ["语气更直接"],
                "depends_on": [],
            }
        ],
        author_statements=[
            {
                "kind": "author_intent",
                "text": "这篇不要第一人称。",
                "exact_user_words": "这篇不要第一人称。",
                "supersedes": [],
            }
        ],
        model_proposals=[
            {
                "kind": "author_intent",
                "text": "作者可能希望语气更直接。",
                "supersedes": [],
            }
        ],
    )

    saved = store.get_project(project.id)
    assert result["current_draft_changed"] is False
    assert saved.current_work_id == work.id
    assert saved.current_digest == work.source_digest
    assert saved.record_revision == 1
    assert saved.candidates[-1].record_revision == 1
    assert saved.record_entries[0].source == "user_explicit"
    assert saved.record_entries[0].text == "这篇不要第一人称。"
    assert saved.record_entries[1].source == "model_inferred"
    assert saved.record_entries[1].status == "proposed"
    assert result["candidate_receipt"] == {
        "candidate_id": saved.candidates[-1].id,
        "base_work_id": work.id,
        "base_digest": work.source_digest,
        "current_draft_changed": False,
        "change_groups": [
            {
                "target_anchor": "paragraph:2",
                "before": "第二段太长，第二段重复。",
                "after": "第二段更短。",
                "before_character_count": 12,
                "after_character_count": 6,
                "character_delta": -6,
                "before_non_whitespace_character_count": 12,
                "after_non_whitespace_character_count": 6,
                "non_whitespace_character_delta": -6,
                "changed": True,
            }
        ],
    }


@pytest.mark.asyncio
async def test_private_editor_angle_tool_accepts_empty_or_grounded_current_project_result(
    tmp_path,
    monkeypatch,
) -> None:
    from lantai.app.private_editor import PrivateEditorStore, RecordEntryDraft
    from lantai.app.works import add_work

    monkeypatch.setattr(
        runner_module,
        "build_lantai_tool_registry",
        lambda **_kwargs: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_memory_tool_registry",
        lambda _manager: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_mcp_client_registry",
        lambda _clients, **_kwargs: LantaiToolRegistry(),
    )
    source = tmp_path / "draft.md"
    source.write_text("夜校文章原稿。", encoding="utf-8")
    work = add_work(tmp_path, source_path=source, allowed_roots=[tmp_path])
    store = PrivateEditorStore(tmp_path)
    store.update_settings(enabled=True)
    project = store.create_project(work.id, title="夜校")
    material = store.add_record_entries(
        project.id,
        expected_current_work_id=project.current_work_id,
        expected_current_digest=project.current_digest,
        expected_record_revision=project.record_revision,
        expected_revocation_epoch=project.revocation_epoch,
        drafts=[
            RecordEntryDraft(
                kind="material_note",
                text="受访者反复提到下班后的时间。",
                source="system_observed",
                source_ref="source:interview-01",
                status="open",
            )
        ],
    )[0]
    project = store.get_project(project.id)
    working_copy = tmp_path / ".lantai" / "works" / "edits" / "angle" / "draft.md"
    working_copy.parent.mkdir(parents=True)
    working_copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    project = store.bind_session(
        project.id,
        chat_id="chat-angle",
        working_copy_path=working_copy,
        owned_by_experiment=True,
    )
    registry = AgentRunner(
        agent_id="default",
        workspace_dir=tmp_path,
    )._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context={
            "state_workspace_dir": str(tmp_path),
            "workspace_dir": str(tmp_path),
            "works_workspace_dir": str(tmp_path),
            "run_id": "run-angle",
            "chat_id": "chat-angle",
            "work_method_id": "private_editor",
            "private_editor_project_id": project.id,
            "private_editor_current_work_id": project.current_work_id,
            "private_editor_current_digest": project.current_digest,
            "private_editor_record_revision": project.record_revision,
            "private_editor_revocation_epoch": project.revocation_epoch,
            "private_editor_project_status": "active",
            "_trusted_private_editor_binding": True,
        },
        current_user_text="找一个新切口",
        mcp_clients=[],
    )
    registered = registry.get_tool("record_private_editor_angle_result")
    assert registered is not None
    spec, executor = registered
    assert spec.metadata["private_editor_project_scoped"] is True

    empty = await executor(proposals=[], empty_reason="当前材料没有支撑新方向的新信息。")
    assert empty["proposals"] == []
    assert empty["current_draft_changed"] is False
    assert store.get_project(project.id).record_revision == project.record_revision

    rejection = await executor(
        proposals=[
            {
                "source_refs": ["source:other-project"],
                "key_idea": "跨项目切口",
                "why_now": "无。",
                "differs_from": "无。",
                "next_action": "无。",
                "open_questions": [],
            }
        ],
        empty_reason="",
    )
    assert isinstance(rejection, ToolResponse)
    assert rejection.is_error is True
    assert "source" in str(rejection.content)
    assert rejection.metadata["side_effect_outcome"] == "known_not_applied"

    result = await executor(
        proposals=[
            {
                "source_refs": [material.source_ref, f"record:{material.id}"],
                "key_idea": "从下班后时间的归属切入。",
                "why_now": "采访材料出现了重复线索。",
                "differs_from": "不从课程本身展开。",
                "next_action": "先对照三位受访者的原话。",
                "open_questions": ["样本是否足够？"],
            }
        ],
        empty_reason="",
    )
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["source_refs"] == [
        material.source_ref,
        f"record:{material.id}",
    ]
    assert result["current_draft_changed"] is False
    assert store.get_current_body(project.id) == "夜校文章原稿。"


@pytest.mark.asyncio
async def test_private_editor_natural_language_adoption_uses_server_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    from lantai.app.private_editor import ChangeGroupDraft, PrivateEditorStore
    from lantai.app.works import add_work

    monkeypatch.setattr(
        runner_module,
        "build_lantai_tool_registry",
        lambda **_kwargs: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_memory_tool_registry",
        lambda _manager: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_mcp_client_registry",
        lambda _clients, **_kwargs: LantaiToolRegistry(),
    )
    source = tmp_path / "draft.md"
    source.write_text("第一段。\n\n第二段太长，第二段重复。", encoding="utf-8")
    work = add_work(tmp_path, source_path=source, allowed_roots=[tmp_path])
    store = PrivateEditorStore(tmp_path)
    store.update_settings(enabled=True)
    project = store.create_project(work.id, title="测试作品")
    working_copy = tmp_path / ".lantai" / "works" / "edits" / "test" / "draft.md"
    working_copy.parent.mkdir(parents=True)
    working_copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    project = store.bind_session(
        project.id,
        chat_id="chat-private-editor",
        working_copy_path=working_copy,
        owned_by_experiment=True,
    )
    candidate = store.stage_candidate(
        project.id,
        expected_base_work_id=project.current_work_id,
        expected_base_digest=project.current_digest,
        expected_record_revision=project.record_revision,
        expected_revocation_epoch=project.revocation_epoch,
        change_groups=[
            ChangeGroupDraft(
                target_anchor="paragraph:2",
                before="第二段太长，第二段重复。",
                after="第二段更短。",
                observation="同一层意思重复。",
                impact="推进变慢。",
                rationale="压缩重复信息。",
                evidence_refs=["current-draft:paragraph:2"],
                costs=["语气更直接"],
            )
        ],
    )
    bound = store.get_project(project.id)
    registry = AgentRunner(
        agent_id="default",
        workspace_dir=tmp_path,
    )._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context={
            "state_workspace_dir": str(tmp_path),
            "workspace_dir": str(tmp_path),
            "works_workspace_dir": str(tmp_path),
            "run_id": "run-adopt-candidate",
            "chat_id": "chat-private-editor",
            "work_method_id": "private_editor",
            "private_editor_project_id": bound.id,
            "private_editor_original_work_id": bound.original_work_id,
            "private_editor_current_work_id": bound.current_work_id,
            "private_editor_current_digest": bound.current_digest,
            "private_editor_record_revision": bound.record_revision,
            "private_editor_revocation_epoch": bound.revocation_epoch,
            "private_editor_project_status": "active",
            "_trusted_private_editor_binding": True,
        },
        current_user_text="采纳这个候选的压缩改动。",
        mcp_clients=[],
    )

    registered = registry.get_tool("adopt_private_editor_candidate")
    assert registered is not None
    spec, executor = registered
    assert "confirmed" not in spec.input_schema["properties"]
    assert spec.input_schema["properties"]["candidate_id"]["enum"] == [
        candidate.id
    ]
    assert spec.destructive is True

    result = await executor(
        candidate_id=candidate.id,
        selected_change_groups=[candidate.change_groups[0].id],
    )
    repeated = await executor(
        candidate_id=candidate.id,
        selected_change_groups=[candidate.change_groups[0].id],
    )

    saved = store.get_project(project.id)
    assert result == repeated
    assert result["authorization_source"] == "user-message:run-adopt-candidate"
    assert result["status"] == "acknowledged"
    assert saved.current_work_id == result["resulting_work_id"]
    assert store.get_current_body(project.id) == "第一段。\n\n第二段更短。"


@pytest.mark.asyncio
async def test_runtime_finalize_tool_projects_system_visual_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runner_module,
        "build_lantai_tool_registry",
        lambda **_kwargs: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_memory_tool_registry",
        lambda _manager: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_mcp_client_registry",
        lambda _clients, **_kwargs: LantaiToolRegistry(),
    )
    receipt = {
        "schema_version": 1,
        "run_id": "run-finalize-receipt",
        "attempt_id": "attempt-finalize-receipt",
        "primary_path": "deck.pptx",
        "primary_sha256": "sha256:deck",
        "visual_evidence": [
            {
                "relative_path": "renders/deck.png",
                "size_bytes": 12,
                "sha256": "sha256:preview",
                "media_type": "image/png",
            }
        ],
    }

    async def fake_finalize_deliverable(**_kwargs):
        return SimpleNamespace(
            work_item=SimpleNamespace(
                artifact_id="work-artifact",
                id="work-id",
            ),
            primary_path=str(tmp_path / "deck.pptx"),
            delivery_status="verified",
            acceptance_results=(),
            verification_receipt=receipt,
        )

    import lantai.app.completion_gate as completion_gate_module

    monkeypatch.setattr(
        completion_gate_module,
        "finalize_deliverable",
        fake_finalize_deliverable,
    )
    registry = AgentRunner(
        agent_id="default",
        workspace_dir=tmp_path,
    )._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context={
            "state_workspace_dir": str(tmp_path),
            "workspace_dir": str(tmp_path),
            "run_id": "run-finalize-receipt",
            "chat_id": "chat-finalize-receipt",
        },
        mcp_clients=[],
    )
    registered = registry.get_tool("finalize_deliverable")
    assert registered is not None
    _spec, executor = registered

    result = await executor(
        primary_path="deck.pptx",
        title="Deck",
        kind="presentation",
        entrypoint="deck.pptx",
        verification_paths=["renders/deck.png"],
        acceptance_evidence=[],
    )

    assert result["verification_receipt"] == receipt


@pytest.mark.asyncio
async def test_runtime_finalize_tool_returns_structured_candidate_failure(
    tmp_path,
    monkeypatch,
) -> None:
    """A failed verification keeps the file and its unmet checks observable."""

    monkeypatch.setattr(
        runner_module,
        "build_lantai_tool_registry",
        lambda **_kwargs: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_memory_tool_registry",
        lambda _manager: LantaiToolRegistry(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_mcp_client_registry",
        lambda _clients, **_kwargs: LantaiToolRegistry(),
    )
    candidate = tmp_path / "deck.pptx"
    candidate.write_bytes(b"candidate")

    async def fake_finalize_deliverable(**_kwargs):
        return SimpleNamespace(
            work_item=None,
            primary_path=str(candidate),
            delivery_status="candidate",
            acceptance_results=(
                {
                    "check": "visual_verification",
                    "status": "failed",
                    "detail": "No bound preview was provided.",
                },
            ),
            verification_receipt=None,
            failure_summary="Visual verification is required.",
        )

    import lantai.app.completion_gate as completion_gate_module

    monkeypatch.setattr(
        completion_gate_module,
        "finalize_deliverable",
        fake_finalize_deliverable,
    )
    registry = AgentRunner(
        agent_id="default",
        workspace_dir=tmp_path,
    )._build_native_tool_registry(
        agent_config=SimpleNamespace(),
        request_context={
            "state_workspace_dir": str(tmp_path),
            "workspace_dir": str(tmp_path),
            "run_id": "run-finalize-candidate",
            "chat_id": "chat-finalize-candidate",
        },
        mcp_clients=[],
    )
    registered = registry.get_tool("finalize_deliverable")
    assert registered is not None
    _spec, executor = registered

    result = await executor(
        primary_path="deck.pptx",
        title="Deck",
        kind="presentation",
        entrypoint="deck.pptx",
        verification_paths=[],
        acceptance_evidence=[],
    )

    assert isinstance(result, ToolResponse)
    assert result.is_error is True
    assert result.metadata == {
        "error_code": "deliverable_verification_failed",
        "delivery_status": "candidate",
        "primary_path": str(candidate),
        "acceptance_results": [
            {
                "check": "visual_verification",
                "status": "failed",
                "detail": "No bound preview was provided.",
            }
        ],
        "verification_receipt": None,
        "candidate_preserved": True,
        "side_effect_outcome": "known_not_applied",
    }


def test_subagent_message_projection_removes_private_thinking() -> None:
    event = RuntimeEvent(
        id="child-message",
        seq=2,
        type="message.completed",
        status="completed",
        source="assistant",
        payload={
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "private chain of thought"},
                    {"type": "text", "text": "public result"},
                ],
            }
        },
    )

    projected = AgentRunner._safe_subagent_message_payload(
        event,
        dict(event.payload),
    )

    assert "private chain of thought" not in str(projected)
    assert "public result" in str(projected)


@pytest.mark.asyncio
async def test_terminal_failure_event_is_authoritative_over_following_exception():
    async def failing_stream():
        yield RuntimeEvent(
            id="terminal-failure",
            seq=9,
            type="turn.failed",
            status="failed",
            summary="同一工具连续失败 2 次，本轮已停止，避免无效重试。",
        )
        raise RuntimeError("_RepeatedToolFailure sha256:private-fingerprint")

    events = [
        event
        async for event in _runtime_events_with_terminal_boundary(
            failing_stream(),
        )
    ]

    assert [event.type for event in events] == ["turn.failed"]
    assert "sha256" not in events[0].summary


@pytest.mark.asyncio
async def test_terminal_boundary_keeps_content_filter_fallback_reachable():
    async def filtered_stream():
        yield RuntimeEvent(
            id="terminal-filter",
            seq=3,
            type="turn.failed",
            status="failed",
            summary="provider rejected prompt: content_filter",
        )
        raise RuntimeError("provider rejected prompt: content_filter")

    with pytest.raises(RuntimeError, match="content_filter"):
        _ = [
            event
            async for event in _runtime_events_with_terminal_boundary(
                filtered_stream(),
            )
        ]


def _first_text(msg: Msg) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    assert isinstance(content, list)
    if not content:
        return ""
    block = content[0]
    if isinstance(block, dict):
        return str(block.get("text") or "")
    return str(getattr(block, "text", ""))


@pytest.mark.asyncio
async def test_native_turn_uses_the_same_memory_pre_reply_seam_as_legacy():
    calls: list[list[str]] = []

    async def pre_reply(_agent, kwargs):
        messages = kwargs["msg"]
        calls.append([message.get_text_content() for message in messages])
        return {
            "msg": [
                *messages,
                Msg(name="memory", role="user", content="relevant memory only"),
            ],
        }

    agent = SimpleNamespace(
        context_manager=SimpleNamespace(pre_reply=pre_reply),
    )

    recalled = await AgentRunner._native_memory_recall_messages(
        agent,
        [RuntimeMessage.text("user", "current case")],
    )

    assert calls == [["current case"]]
    assert [message.text_content() for message in recalled] == [
        "relevant memory only",
    ]


@pytest.mark.asyncio
async def test_native_memory_recall_times_out_without_blocking_reply(monkeypatch):
    release = asyncio.Event()

    async def pre_reply(_agent, _kwargs):
        await release.wait()

    monkeypatch.setattr(
        runner_module,
        "_FOREGROUND_MEMORY_RECALL_TIMEOUT_SECONDS",
        0.01,
    )
    agent = SimpleNamespace(
        context_manager=SimpleNamespace(pre_reply=pre_reply),
    )

    recalled = await AgentRunner._native_memory_recall_messages(
        agent,
        [RuntimeMessage.text("user", "current case")],
    )

    assert recalled == []


def test_parse_skill_query_plain_and_bracket_forms() -> None:
    assert AgentRunner._parse_skill_query("/guanlan 搜索 AI 新闻") == (
        "guanlan",
        "搜索 AI 新闻",
    )
    assert AgentRunner._parse_skill_query("/[微信读书] 同步划线") == (
        "微信读书",
        "同步划线",
    )
    assert AgentRunner._parse_skill_query("/[微信读书") is None
    assert AgentRunner._parse_skill_query("不是命令") is None


@pytest.mark.asyncio
async def test_native_turn_consumes_detached_snapshot_input_once(
    tmp_path,
    monkeypatch,
) -> None:
    """Native receives one detached input rehydrated from the S7 snapshot."""

    events: list[str] = []
    registry = LantaiToolRegistry()
    policy = ToolPolicyContext(
        agent_id="default",
        session_id="session-order",
        workspace_dir=str(tmp_path),
    )
    approval_service = object()
    native_kwargs: dict[str, object] = {}
    shadow_kwargs: dict[str, object] = {}
    runtime_inputs: list[object] = []
    rehydrated_inputs: list[RuntimeInput] = []
    input_matches: list[bool] = []
    effect_journal = object()
    artifact_effect_journal = object()
    shadow_snapshots: list[object] = []
    shadow_results: list[object] = []
    oracle_observation = object()
    prepared_source_observation = object()
    policy_receipt = object()
    oracle_report = object()
    real_capture_shadow = runner_module.capture_shadow_turn_assembly
    real_rehydrate = runner_module.runtime_input_from_turn_assembly_snapshot
    real_exact_match = runner_module.runtime_inputs_are_exactly_equivalent

    class FakeMemory:
        content: list[object] = []

        async def add(self, _message) -> None:
            events.append("memory_add")

    class FakeAgent:
        def __init__(self) -> None:
            self.memory = FakeMemory()
            self.ququ_hard_required = True
            self.persona_turn_plan = SimpleNamespace(to_dict=lambda: {})
            self._sys_prompt_reads = 0
            self._manifest_reads = 0

        def prepare_persona_turn(self, _messages) -> None:
            events.append("prepare_persona")

        @property
        def sys_prompt(self) -> str:
            self._sys_prompt_reads += 1
            events.append("sys_prompt")
            return "trusted persona prompt"

        @property
        def prompt_manifest(self) -> dict[str, object]:
            self._manifest_reads += 1
            events.append("prompt_manifest")
            return {
                "schema_version": "1",
                "sections": [
                    {
                        "id": "compiled.base",
                        "order": 0,
                        "owner": "agent_runtime",
                        "sha256": "sha256:base",
                    }
                ],
                "prompt_sha256": "sha256:prompt",
            }

    class FakeRuntimeRunner:
        def __init__(self, adapter) -> None:
            events.append("runtime_runner")
            assert adapter.label == "native"

        async def run_turn(self, runtime_input):
            events.append("run_turn")
            runtime_inputs.append(runtime_input)
            yield RuntimeEvent(
                id="turn-complete",
                seq=1,
                type="turn.completed",
                status="completed",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )

    def build_registry(self, **kwargs):
        events.append("registry")
        assert kwargs["approval_service"] is approval_service
        return registry

    def build_policy(**_kwargs):
        events.append("policy")
        return policy

    def capture_shadow(**kwargs):
        events.append("shadow")
        shadow_kwargs.update(kwargs)
        assert kwargs["tool_registry"] is registry
        assert kwargs["tool_policy_context"] is policy
        result = real_capture_shadow(**kwargs)
        shadow_results.append(result)
        if result.snapshot is not None:
            shadow_snapshots.append(result.snapshot)
        return result

    def rehydrate(snapshot):
        events.append("rehydrate")
        assert snapshot is shadow_snapshots[0]
        rehydrated = real_rehydrate(snapshot)
        rehydrated_inputs.append(rehydrated)
        return rehydrated

    def exact_input_match(actual, expected):
        events.append("input_compare")
        assert actual is rehydrated_inputs[0]
        assert expected is shadow_kwargs["runtime_input"]
        matched = real_exact_match(actual, expected)
        input_matches.append(matched)
        return matched

    def build_native(**kwargs):
        events.append("native")
        native_kwargs.update(kwargs)

        class FakeNative:
            label = "native"

            def request_policy_construction_receipt(self):
                events.append("policy_receipt")
                return policy_receipt

        return FakeNative()

    def observe_oracle(**kwargs):
        events.append("oracle_observe")
        assert kwargs["runtime_input"] is rehydrated_inputs[0]
        assert kwargs["tool_registry"] is registry
        assert kwargs["tool_policy_context"] is policy
        return oracle_observation

    def observe_prepared_sources(**kwargs):
        events.append("source_oracle_observe")
        assert kwargs["runtime_input"] is rehydrated_inputs[0]
        assert kwargs["memory_messages"] is shadow_kwargs["memory_messages"]
        assert kwargs["recall_messages"] is shadow_kwargs["recall_messages"]
        assert kwargs["plan_hint_message"] is shadow_kwargs["plan_hint_message"]
        assert kwargs["turn_messages"] is shadow_kwargs["turn_messages"]
        assert (
            kwargs["attachment_manifest_message"]
            is shadow_kwargs["attachment_manifest_message"]
        )
        return prepared_source_observation

    def compare_oracle(**kwargs):
        events.append("oracle_compare")
        assert kwargs["observation"] is oracle_observation
        assert kwargs["snapshot"] is shadow_snapshots[0]
        assert kwargs["prepared_sources"] is prepared_source_observation
        assert kwargs["request_policy_construction_receipt"] is policy_receipt
        return oracle_report

    def log_oracle(report):
        events.append("oracle_log")
        assert report is oracle_report

    async def preflight(_agent, _messages):
        events.append("preflight")
        if False:
            yield None

    async def load_memory(_agent):
        events.append("memory")
        return []

    async def recall_memory(_agent, _messages):
        events.append("recall")
        return []

    monkeypatch.setattr(
        approvals_service_module,
        "get_approval_service",
        lambda: events.append("approval") or approval_service,
    )
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        build_registry,
    )
    monkeypatch.setattr(runner_module, "build_tool_policy_context", build_policy)
    monkeypatch.setattr(runner_module, "capture_shadow_turn_assembly", capture_shadow)
    monkeypatch.setattr(
        runner_module,
        "runtime_input_from_turn_assembly_snapshot",
        rehydrate,
    )
    monkeypatch.setattr(
        runner_module,
        "runtime_inputs_are_exactly_equivalent",
        exact_input_match,
    )
    monkeypatch.setattr(runner_module, "NativeLantaiRuntime", build_native)
    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(runner_module, "observe_native_input_boundary", observe_oracle)
    monkeypatch.setattr(
        runner_module,
        "observe_prepared_turn_sources",
        observe_prepared_sources,
    )
    monkeypatch.setattr(runner_module, "compare_turn_assembly_snapshot", compare_oracle)
    monkeypatch.setattr(runner_module, "log_turn_assembly_oracle_report", log_oracle)
    monkeypatch.setattr(
        AgentRunner,
        "_stream_native_context_preflight",
        staticmethod(preflight),
    )
    monkeypatch.setattr(
        AgentRunner,
        "_runtime_messages_from_agent_memory",
        staticmethod(load_memory),
    )
    monkeypatch.setattr(
        AgentRunner,
        "_native_memory_recall_messages",
        staticmethod(recall_memory),
    )
    monkeypatch.setattr(
        runner_module,
        "RuntimeEffectJournal",
        lambda _path: events.append("effect_journal") or effect_journal,
    )
    monkeypatch.setattr(
        runner_module,
        "RuntimeArtifactEffectJournal",
        lambda _path: (
            events.append("artifact_effect_journal")
            or artifact_effect_journal
        ),
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    runner.memory_manager = SimpleNamespace(
        schedule_capture_turn=lambda **_kwargs: None,
    )
    agent = FakeAgent()
    output = [
        item
        async for item in runner._stream_standard_runtime_turn(
            agent=agent,
            agent_config=SimpleNamespace(
                plan=SimpleNamespace(enabled=False),
                runtime=SimpleNamespace(engine="native", harness_mode="off"),
                running=SimpleNamespace(max_iters=5),
            ),
            mcp_clients=[],
            msgs=[Msg("user", "current turn", "user")],
            session_id="session-order",
            user_id="default",
            channel="console",
            request_context={
                "runtime_max_iters": "7",
                "workspace_access_mode": "read_write",
                "workspace_trust_state": "trusted",
            },
            workspace_dir=tmp_path,
        )
    ]

    assert output
    assert shadow_results[0].status == "captured", (
        shadow_results[0].reason_code
    )
    assert input_matches == [True]
    assert events.count("prepare_persona") == 1
    assert events.count("sys_prompt") == 1
    assert events.count("prompt_manifest") == 1
    assert events.count("registry") == 1
    assert events.count("policy") == 1
    assert events.count("shadow") == 1
    assert events.count("rehydrate") == 1
    assert events.count("input_compare") == 1
    assert events.count("native") == 1
    assert events.count("oracle_observe") == 1
    assert events.count("source_oracle_observe") == 1
    assert events.count("oracle_compare") == 1
    assert events.count("oracle_log") == 1
    assert events.count("run_turn") == 1
    trace_through_runtime_start = events[: events.index("run_turn") + 1]
    assert trace_through_runtime_start == [
        "preflight",
        "prepare_persona",
        "memory",
        "recall",
        "sys_prompt",
        "prompt_manifest",
        "effect_journal",
        "artifact_effect_journal",
        "approval",
        "registry",
        "policy",
        "shadow",
        "rehydrate",
        "input_compare",
        "native",
        "policy_receipt",
        "runtime_runner",
        "oracle_observe",
        "source_oracle_observe",
        "oracle_compare",
        "oracle_log",
        "run_turn",
    ]
    old_control_trace = [
        event
        for event in trace_through_runtime_start
        if event
        not in {
            "shadow",
            "rehydrate",
            "input_compare",
            "policy_receipt",
            "oracle_observe",
            "source_oracle_observe",
            "oracle_compare",
            "oracle_log",
        }
    ]
    old_control_trace_702411e = [
        "preflight",
        "prepare_persona",
        "memory",
        "recall",
        "sys_prompt",
        "prompt_manifest",
        "effect_journal",
        "artifact_effect_journal",
        "approval",
        "registry",
        "policy",
        "native",
        "runtime_runner",
        "run_turn",
    ]
    assert old_control_trace == old_control_trace_702411e
    assert events.index("approval") < events.index("registry")
    assert events.index("registry") < events.index("policy")
    assert events.index("policy") < events.index("shadow")
    assert events.index("shadow") < events.index("rehydrate")
    assert events.index("rehydrate") < events.index("input_compare")
    assert events.index("input_compare") < events.index("native")
    assert events.index("native") < events.index("run_turn")
    assert native_kwargs["tool_registry"] is registry
    assert native_kwargs["tool_policy_context"] is policy
    assert native_kwargs["approval_service"] is approval_service
    assert native_kwargs["effect_journal"] is effect_journal
    assert native_kwargs["artifact_effect_journal"] is artifact_effect_journal
    assert native_kwargs["max_iters"] == 7
    assert runtime_inputs == rehydrated_inputs
    assert runtime_inputs[0] is not shadow_kwargs["runtime_input"]
    assert runtime_inputs[0].model_dump(mode="json") == (
        shadow_kwargs["runtime_input"].model_dump(mode="json")
    )
    assert shadow_kwargs["runtime_input"].messages[0].text_content() == (
        "trusted persona prompt"
    )
    assert shadow_kwargs["turn_messages"][0].text_content() == "current turn"


async def _exercise_native_shadow_boundary(
    *,
    tmp_path,
    monkeypatch,
    state: dict[str, object],
    failure_stage: str | None = None,
    failure: BaseException | None = None,
    real_shadow_failure: bool = False,
    effective_policy_observation: bool = False,
) -> None:
    events: list[str] = []
    state["events"] = events
    registry = LantaiToolRegistry()
    policy = ToolPolicyContext(
        agent_id="default",
        session_id="session-boundary",
        workspace_dir=str(tmp_path),
    )
    state["registry"] = registry
    state["policy"] = policy
    approval_service = object()
    shadow_snapshot = object()
    oracle_observation = object()
    prepared_source_observation = object()
    policy_receipt = object()
    effective_policy_token = object()
    effective_policy_invocation = object()
    effective_policy_receipt = object()
    policy_dispatch_receipt = object()
    policy_consumption_receipt = object()
    oracle_report = object()

    class FakeMemory:
        content: list[object] = []

        async def add(self, _message) -> None:
            return None

    class FakeAgent:
        memory = FakeMemory()
        sys_prompt = "trusted system prompt"
        prompt_manifest = {
            "schema_version": "1",
            "sections": [
                {
                    "id": "compiled.base",
                    "order": 0,
                    "owner": "agent_runtime",
                    "sha256": "sha256:base",
                }
            ],
            "prompt_sha256": "sha256:prompt",
        }
        persona_turn_plan = SimpleNamespace(to_dict=lambda: {})
        persona_preparation_receipt = object()
        ququ_hard_required = True

    state["expected_persona_preparation_receipt"] = (
        FakeAgent.persona_preparation_receipt
    )

    class FakeRuntimeRunner:
        def __init__(self, _adapter, **kwargs) -> None:
            events.append("runtime_runner")
            state["runtime_runner_kwargs"] = kwargs
            self.request_policy_observation_invocation = (
                effective_policy_invocation
                if kwargs.get("request_policy_observation_token")
                is effective_policy_token
                else None
            )

        async def run_turn(self, runtime_input):
            events.append("run_turn")
            state["runtime_input"] = runtime_input
            if failure_stage == "runtime":
                assert failure is not None
                raise failure
            yield RuntimeEvent(
                id="turn-complete",
                seq=1,
                type="turn.completed",
                status="completed",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )

    def build_registry(self, **_kwargs):
        events.append("registry")
        if failure_stage == "registry":
            assert failure is not None
            raise failure
        return registry

    def build_policy(**_kwargs):
        events.append("policy")
        if failure_stage == "policy":
            assert failure is not None
            raise failure
        return policy

    real_capture = runner_module.capture_shadow_turn_assembly
    real_input_match = runner_module.runtime_inputs_are_exactly_equivalent

    def capture_shadow(**kwargs):
        events.append("shadow")
        state["shadow_input"] = kwargs["runtime_input"]
        state["shadow_registry"] = kwargs["tool_registry"]
        state["shadow_policy"] = kwargs["tool_policy_context"]
        if real_shadow_failure:
            result = real_capture(**kwargs)
            state["shadow_result"] = result
            return result
        if failure_stage == "shadow":
            assert failure is not None
            raise failure
        return SimpleNamespace(status="captured", snapshot=shadow_snapshot)

    def rehydrate_snapshot(snapshot):
        events.append("snapshot_input")
        assert snapshot is shadow_snapshot
        if failure_stage == "snapshot_input":
            assert failure is not None
            raise failure
        if failure_stage == "snapshot_input_mismatch":
            source = state["shadow_input"]
            assert type(source) is RuntimeInput
            return source.model_copy(update={"model_slot": "changed"})
        return state["shadow_input"]

    def compare_snapshot_input(actual, expected):
        events.append("snapshot_input_compare")
        if failure_stage == "snapshot_input_compare":
            assert failure is not None
            raise failure
        return real_input_match(actual, expected)

    def build_native(**kwargs):
        events.append("native")
        state["native_kwargs"] = kwargs
        if failure_stage == "native":
            assert failure is not None
            raise failure

        class FakeObserver:
            def begin(self, observed_input):
                events.append("effective_policy_begin")
                assert observed_input is state["shadow_input"]
                return effective_policy_token

            def take(self, invocation, **take_kwargs):
                events.append("effective_policy_take")
                assert invocation is effective_policy_invocation
                assert take_kwargs == {
                    "terminal_type": "turn.completed",
                    "terminal_turn_id": "run-private",
                }
                return effective_policy_receipt

            def take_dispatch(self, invocation, **take_kwargs):
                events.append("policy_dispatch_take")
                assert invocation is effective_policy_invocation
                assert take_kwargs == {
                    "terminal_type": "turn.completed",
                    "terminal_turn_id": "run-private",
                }
                return policy_dispatch_receipt

            def take_consumption(self, invocation, **take_kwargs):
                events.append("policy_consumption_take")
                if failure_stage == "policy_consumption":
                    assert failure is not None
                    raise failure
                assert invocation is effective_policy_invocation
                assert take_kwargs == {
                    "terminal_type": "turn.completed",
                    "terminal_turn_id": "run-private",
                }
                return policy_consumption_receipt

        class FakeNative:
            label = "native"
            if effective_policy_observation:
                request_policy_observer = FakeObserver()

            def request_policy_construction_receipt(self):
                events.append("policy_receipt")
                if failure_stage == "policy_receipt":
                    assert failure is not None
                    raise failure
                return policy_receipt

        return FakeNative()

    def observe_oracle(**kwargs):
        events.append("oracle_observe")
        state["oracle_input"] = kwargs["runtime_input"]
        state["oracle_registry"] = kwargs["tool_registry"]
        state["oracle_policy"] = kwargs["tool_policy_context"]
        if failure_stage == "oracle_observe":
            assert failure is not None
            raise failure
        return oracle_observation

    def observe_prepared_sources(**kwargs):
        events.append("source_oracle_observe")
        state["source_oracle_input"] = kwargs["runtime_input"]
        state["source_memory_messages"] = kwargs["memory_messages"]
        state["source_recall_messages"] = kwargs["recall_messages"]
        state["source_plan_hint_message"] = kwargs["plan_hint_message"]
        state["source_turn_messages"] = kwargs["turn_messages"]
        state["source_attachment_manifest_message"] = kwargs[
            "attachment_manifest_message"
        ]
        state["source_persona_preparation_receipt"] = kwargs[
            "persona_preparation_receipt"
        ]
        if failure_stage == "source_oracle_observe":
            assert failure is not None
            raise failure
        return prepared_source_observation

    def compare_oracle(**kwargs):
        is_effective = "request_policy_effective_receipt" in kwargs
        events.append(
            "oracle_compare_effective" if is_effective else "oracle_compare"
        )
        if failure_stage == "oracle_compare":
            assert failure is not None
            raise failure
        assert kwargs["observation"] is oracle_observation
        assert kwargs["snapshot"] is shadow_snapshot
        if failure_stage == "source_oracle_observe":
            assert kwargs["prepared_sources"] is None
        else:
            assert kwargs["prepared_sources"] is prepared_source_observation
        assert kwargs["request_policy_construction_receipt"] is (
            None if failure_stage == "policy_receipt" else policy_receipt
        )
        if is_effective:
            assert effective_policy_observation is True
            assert (
                kwargs["request_policy_effective_receipt"]
                is effective_policy_receipt
            )
            assert (
                kwargs["request_policy_dispatch_receipt"]
                is policy_dispatch_receipt
            )
            assert kwargs["request_policy_consumption_receipt"] is (
                None
                if failure_stage == "policy_consumption"
                else policy_consumption_receipt
            )
        return oracle_report

    def unavailable_oracle(reason_code):
        events.append("oracle_unavailable")
        state["oracle_unavailable_reason"] = reason_code
        return oracle_report

    def log_oracle(report):
        events.append("oracle_log")
        assert report is oracle_report
        if failure_stage == "oracle_log":
            assert failure is not None
            raise failure

    monkeypatch.setattr(
        approvals_service_module,
        "get_approval_service",
        lambda: approval_service,
    )
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        build_registry,
    )
    monkeypatch.setattr(runner_module, "build_tool_policy_context", build_policy)
    monkeypatch.setattr(runner_module, "capture_shadow_turn_assembly", capture_shadow)
    monkeypatch.setattr(
        runner_module,
        "runtime_input_from_turn_assembly_snapshot",
        rehydrate_snapshot,
    )
    monkeypatch.setattr(
        runner_module,
        "runtime_inputs_are_exactly_equivalent",
        compare_snapshot_input,
    )
    monkeypatch.setattr(runner_module, "NativeLantaiRuntime", build_native)
    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(runner_module, "observe_native_input_boundary", observe_oracle)
    monkeypatch.setattr(
        runner_module,
        "observe_prepared_turn_sources",
        observe_prepared_sources,
    )
    monkeypatch.setattr(runner_module, "compare_turn_assembly_snapshot", compare_oracle)
    monkeypatch.setattr(
        runner_module,
        "unavailable_turn_assembly_oracle_report",
        unavailable_oracle,
    )
    monkeypatch.setattr(runner_module, "log_turn_assembly_oracle_report", log_oracle)
    monkeypatch.setattr(runner_module, "RuntimeEffectJournal", lambda _path: object())
    monkeypatch.setattr(
        runner_module,
        "RuntimeArtifactEffectJournal",
        lambda _path: object(),
    )

    async def no_control_event(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        AgentRunner,
        "_persist_native_runtime_control_event",
        no_control_event,
    )
    monkeypatch.setattr(
        AgentRunner,
        "_close_native_runtime_control_events",
        no_control_event,
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    runner.memory_manager = SimpleNamespace(
        schedule_capture_turn=lambda **_kwargs: None,
    )
    state["output"] = [
        item
        async for item in runner._stream_standard_runtime_turn(
            agent=FakeAgent(),
            agent_config=SimpleNamespace(
                plan=SimpleNamespace(enabled=False),
                runtime=SimpleNamespace(engine="native", harness_mode="off"),
                running=SimpleNamespace(max_iters=5),
            ),
            mcp_clients=[],
            msgs=[Msg("user", "current turn", "user")],
            session_id="session-boundary",
            user_id="default",
            channel="console",
            request_context={
                "workspace_access_mode": "read_write",
                "workspace_trust_state": "trusted",
                **(
                    {"run_id": "run-private"}
                    if effective_policy_observation
                    else {}
                ),
            },
            workspace_dir=tmp_path,
        )
    ]


@pytest.mark.asyncio
async def test_shadow_exception_fails_open_without_rebuilding_old_inputs(
    tmp_path,
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    await _exercise_native_shadow_boundary(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        state=state,
        failure_stage="shadow",
        failure=RuntimeError("private shadow failure"),
    )

    events = state["events"]
    assert events.count("registry") == 1
    assert events.count("policy") == 1
    assert events.count("shadow") == 1
    assert events.count("native") == 1
    assert events.count("oracle_unavailable") == 1
    assert state["oracle_unavailable_reason"] == "shadow_snapshot_unavailable"
    assert events.count("run_turn") == 1
    native_kwargs = state["native_kwargs"]
    assert native_kwargs["tool_registry"] is state["registry"]
    assert native_kwargs["tool_policy_context"] is state["policy"]
    assert state["runtime_input"] is state["shadow_input"]


@pytest.mark.asyncio
async def test_s7_runtime_input_carries_effective_native_tool_policy(
    tmp_path,
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    await _exercise_native_shadow_boundary(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        state=state,
    )

    runtime_input = state["shadow_input"]
    policy = state["policy"]
    assert isinstance(runtime_input, RuntimeInput)
    assert isinstance(policy, ToolPolicyContext)
    assert runtime_input.tool_policy_context == policy.model_dump(mode="json")


@pytest.mark.asyncio
async def test_runner_publishes_sanitized_runtime_events_to_plugins(
    tmp_path,
    monkeypatch,
) -> None:
    from lantai.plugins.api import PluginApi
    from lantai.plugins.registry import PluginRegistry

    monkeypatch.setattr(PluginRegistry, "_instance", None)
    registry = PluginRegistry()
    observed = []
    api = PluginApi(
        "observer",
        {},
        {"permissions": ["runtime:event:read"]},
    )
    api.set_registry(registry)
    api.register_runtime_event_observer(
        "terminal",
        observed.append,
        event_types={"turn.completed"},
    )
    state: dict[str, object] = {}

    await _exercise_native_shadow_boundary(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        state=state,
    )
    await registry.drain_runtime_event_observers()

    assert [event.type for event in observed] == ["turn.completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_stage", "failure"),
    [
        ("snapshot_input", RuntimeError("PRIVATE SNAPSHOT INPUT FAILURE")),
        (
            "snapshot_input_compare",
            RuntimeError("PRIVATE SNAPSHOT INPUT COMPARE FAILURE"),
        ),
        ("snapshot_input_mismatch", None),
    ],
)
async def test_snapshot_input_failure_falls_back_once_and_is_unverified(
    tmp_path,
    monkeypatch,
    failure_stage: str,
    failure: BaseException | None,
) -> None:
    state: dict[str, object] = {}
    await _exercise_native_shadow_boundary(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        state=state,
        failure_stage=failure_stage,
        failure=failure,
    )

    events = state["events"]
    assert events.count("snapshot_input") == 1
    assert events.count("snapshot_input_compare") == (
        0 if failure_stage == "snapshot_input" else 1
    )
    assert events.count("native") == 1
    assert events.count("run_turn") == 1
    assert events.count("oracle_unavailable") == 1
    assert state["oracle_unavailable_reason"] == (
        "snapshot_runtime_input_unavailable"
    )
    assert state["runtime_input"] is state["shadow_input"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage",
    ["snapshot_input", "snapshot_input_compare"],
)
async def test_snapshot_input_base_exception_preserves_identity(
    tmp_path,
    monkeypatch,
    failure_stage: str,
) -> None:
    state: dict[str, object] = {}
    failure = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt) as captured:
        await _exercise_native_shadow_boundary(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            state=state,
            failure_stage=failure_stage,
            failure=failure,
        )

    assert captured.value is failure
    assert state["events"].count("snapshot_input") == 1
    assert "native" not in state["events"]


@pytest.mark.asyncio
async def test_real_shadow_extraction_and_diagnostic_failure_reaches_native_once(
    tmp_path,
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    extraction_calls = 0
    diagnostic_calls = 0

    def fail_projection():
        nonlocal extraction_calls
        extraction_calls += 1
        raise RuntimeError("PRIVATE EXTRACTION")

    def fail_diagnostic(*_args, **_kwargs):
        nonlocal diagnostic_calls
        diagnostic_calls += 1
        raise RuntimeError("PRIVATE DIAGNOSTIC")

    monkeypatch.setattr(
        LantaiToolRegistry,
        "tool_items",
        lambda _self: fail_projection(),
    )
    monkeypatch.setattr(
        legacy_turn_assembly_module.logger,
        "info",
        fail_diagnostic,
    )
    await _exercise_native_shadow_boundary(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        state=state,
        real_shadow_failure=True,
    )

    assert state["shadow_result"].status == "unavailable"
    assert state["shadow_result"].reason_code == "shadow_extraction_failed"
    assert extraction_calls == 1
    assert diagnostic_calls == 1
    assert state["events"].count("native") == 1
    assert state["events"].count("oracle_unavailable") == 1
    assert state["events"].count("run_turn") == 1
    assert state["runtime_input"] is state["shadow_input"]
    native_kwargs = state["native_kwargs"]
    assert native_kwargs["tool_registry"] is state["registry"]
    assert native_kwargs["tool_policy_context"] is state["policy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["registry", "policy"])
async def test_old_registry_and_policy_failures_are_not_swallowed(
    tmp_path,
    monkeypatch,
    failure_stage: str,
) -> None:
    state: dict[str, object] = {}
    with pytest.raises(RuntimeError, match="old pipeline failure"):
        await _exercise_native_shadow_boundary(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            state=state,
            failure_stage=failure_stage,
            failure=RuntimeError("old pipeline failure"),
        )

    events = state["events"]
    assert "shadow" not in events
    assert "native" not in events
    if failure_stage == "registry":
        assert "policy" not in events
    else:
        assert events.count("registry") == 1


@pytest.mark.asyncio
async def test_native_constructor_failure_does_not_repeat_prepared_seams(
    tmp_path,
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    with pytest.raises(RuntimeError, match="native constructor failure"):
        await _exercise_native_shadow_boundary(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            state=state,
            failure_stage="native",
            failure=RuntimeError("native constructor failure"),
        )

    events = state["events"]
    for seam in ("registry", "policy", "shadow", "native"):
        assert events.count(seam) == 1
    assert "runtime_runner" not in events
    assert "run_turn" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_failure",
    [RuntimeError("runtime failure"), asyncio.CancelledError()],
)
async def test_runtime_failure_or_cancellation_does_not_repeat_prepared_seams(
    tmp_path,
    monkeypatch,
    runtime_failure: BaseException,
) -> None:
    state: dict[str, object] = {}
    with pytest.raises(type(runtime_failure)):
        await _exercise_native_shadow_boundary(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            state=state,
            failure_stage="runtime",
            failure=runtime_failure,
        )

    events = state["events"]
    for seam in ("registry", "policy", "shadow", "native", "run_turn"):
        assert events.count(seam) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage",
    [
        "oracle_observe",
        "source_oracle_observe",
        "oracle_compare",
        "oracle_log",
    ],
)
async def test_oracle_or_diagnostic_exception_fails_open_before_runtime(
    tmp_path,
    monkeypatch,
    failure_stage: str,
) -> None:
    state: dict[str, object] = {}
    await _exercise_native_shadow_boundary(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        state=state,
        failure_stage=failure_stage,
        failure=RuntimeError("private oracle failure"),
    )

    events = state["events"]
    assert events.count("registry") == 1
    assert events.count("policy") == 1
    assert events.count("shadow") == 1
    assert events.count("native") == 1
    assert events.count("run_turn") == 1
    assert state["runtime_input"] is state["shadow_input"]
    if failure_stage == "oracle_observe":
        assert events.count("oracle_unavailable") == 1
        assert state["oracle_unavailable_reason"] == "oracle_projection_failed"
    elif failure_stage == "oracle_compare":
        assert events.count("oracle_unavailable") == 1
        assert state["oracle_unavailable_reason"] == "oracle_comparison_failed"
    elif failure_stage == "source_oracle_observe":
        assert events.count("source_oracle_observe") == 1
        assert events.count("oracle_compare") == 1
        assert events.count("oracle_unavailable") == 0


@pytest.mark.asyncio
async def test_policy_receipt_exception_fails_open_before_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    await _exercise_native_shadow_boundary(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        state=state,
        failure_stage="policy_receipt",
        failure=RuntimeError("private receipt failure"),
    )

    events = state["events"]
    assert events.count("policy_receipt") == 1
    assert events.count("oracle_compare") == 1
    assert events.count("run_turn") == 1


@pytest.mark.asyncio
async def test_policy_receipt_base_exception_is_not_swallowed_by_runner(
    tmp_path,
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    with pytest.raises(KeyboardInterrupt):
        await _exercise_native_shadow_boundary(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            state=state,
            failure_stage="policy_receipt",
            failure=KeyboardInterrupt(),
        )

    assert state["events"].count("policy_receipt") == 1
    assert "runtime_runner" not in state["events"]
    assert "run_turn" not in state["events"]


@pytest.mark.asyncio
async def test_policy_consumption_receipt_exception_fails_open_after_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    await _exercise_native_shadow_boundary(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        state=state,
        failure_stage="policy_consumption",
        failure=RuntimeError("private consumption failure"),
        effective_policy_observation=True,
    )

    events = state["events"]
    assert events.count("run_turn") == 1
    assert events.count("policy_consumption_take") == 1
    assert events.count("oracle_compare_effective") == 1


@pytest.mark.asyncio
async def test_effective_policy_receipt_is_compared_only_after_stream_exhaustion(
    tmp_path,
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    await _exercise_native_shadow_boundary(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        state=state,
        effective_policy_observation=True,
    )

    events = state["events"]
    assert events.count("effective_policy_begin") == 1
    assert events.count("effective_policy_take") == 1
    assert events.count("policy_dispatch_take") == 1
    assert events.count("policy_consumption_take") == 1
    assert events.count("oracle_compare") == 1
    assert events.count("oracle_compare_effective") == 1
    assert events.count("oracle_log") == 2
    assert events.index("run_turn") < events.index("effective_policy_take")
    assert events.index("effective_policy_take") < events.index(
        "oracle_compare_effective"
    )
    assert events.index("effective_policy_take") < events.index(
        "policy_dispatch_take"
    )
    assert events.index("policy_dispatch_take") < events.index(
        "oracle_compare_effective"
    )
    assert events.index("policy_dispatch_take") < events.index(
        "policy_consumption_take"
    )
    assert events.index("policy_consumption_take") < events.index(
        "oracle_compare_effective"
    )
    runtime_runner_kwargs = state["runtime_runner_kwargs"]
    assert (
        runtime_runner_kwargs["request_policy_observation_token"] is not None
    )
    assert callable(runtime_runner_kwargs["diagnostic_run_turn"])


@pytest.mark.asyncio
async def test_prepared_source_oracle_exception_text_is_not_logged(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    state: dict[str, object] = {}
    with caplog.at_level(logging.DEBUG):
        await _exercise_native_shadow_boundary(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            state=state,
            failure_stage="source_oracle_observe",
            failure=RuntimeError(
                "PRIVATE SOURCE EXCEPTION /private/source-id"
            ),
        )

    assert state["events"].count("source_oracle_observe") == 1
    assert state["events"].count("run_turn") == 1
    assert "PRIVATE SOURCE EXCEPTION" not in caplog.text
    assert "/private/source-id" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_oracle_cancellation_is_not_swallowed_by_runner(
    tmp_path,
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    with pytest.raises(asyncio.CancelledError):
        await _exercise_native_shadow_boundary(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            state=state,
            failure_stage="oracle_observe",
            failure=asyncio.CancelledError(),
        )

    events = state["events"]
    for seam in ("registry", "policy", "shadow", "native", "oracle_observe"):
        assert events.count(seam) == 1
    assert "run_turn" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_failure",
    [asyncio.CancelledError(), KeyboardInterrupt()],
)
async def test_prepared_source_oracle_never_swallows_terminal_failures(
    tmp_path,
    monkeypatch,
    terminal_failure: BaseException,
) -> None:
    state: dict[str, object] = {}
    with pytest.raises(type(terminal_failure)):
        await _exercise_native_shadow_boundary(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            state=state,
            failure_stage="source_oracle_observe",
            failure=terminal_failure,
        )

    events = state["events"]
    assert events.count("oracle_observe") == 1
    assert events.count("source_oracle_observe") == 1
    assert state["source_persona_preparation_receipt"] is state[
        "expected_persona_preparation_receipt"
    ]
    assert "oracle_compare" not in events
    assert "run_turn" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_failure",
    [asyncio.CancelledError(), KeyboardInterrupt()],
)
async def test_shadow_never_swallows_cancellation_or_base_exception_in_runner(
    tmp_path,
    monkeypatch,
    terminal_failure: BaseException,
) -> None:
    state: dict[str, object] = {}
    with pytest.raises(type(terminal_failure)):
        await _exercise_native_shadow_boundary(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            state=state,
            failure_stage="shadow",
            failure=terminal_failure,
        )

    events = state["events"]
    assert events.count("registry") == 1
    assert events.count("policy") == 1
    assert events.count("shadow") == 1
    assert "native" not in events
    assert "run_turn" not in events


def test_runner_binds_only_typed_external_file_grants(tmp_path) -> None:
    from lantai.app.runner.runner import _bind_request_external_file_grants

    uploaded = tmp_path / "report.pdf"
    uploaded.write_bytes(b"%PDF")
    grant = issue_external_file_grant(uploaded, operations={"read"})
    try:
        assert (
            _bind_request_external_file_grants(
                {
                    "_external_file_grants": [
                        {"path": str(uploaded)},
                        grant,
                    ],
                }
            )
            == 1
        )
        assert get_current_external_file_grants() == (grant,)

        assert _bind_request_external_file_grants({}) == 0
        assert get_current_external_file_grants() == ()
    finally:
        set_current_external_file_grants(None)


def test_runner_projects_only_server_granted_current_attachment(
    tmp_path,
) -> None:
    attachment = tmp_path / "客户空格（A B）.xlsx"
    attachment.write_bytes(b"xlsx-sentinel")
    grant = issue_external_file_grant(attachment, operations={"read"})
    forged_path = str(tmp_path / "forged-secret.xlsx")
    channel_meta = {
        "_external_file_grants": [grant],
        "_trusted_current_attachments": [
            {
                "display_name": "客户原始名（A B）.xlsx",
                "canonical_path": str(attachment.resolve()),
                "media_type": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                "size_bytes": 999999,
                "workspace_staged": True,
            },
            {
                "display_name": "forged-secret.xlsx",
                "canonical_path": forged_path,
                "media_type": "application/octet-stream",
            },
        ],
    }

    manifest = _trusted_current_attachments_from_channel_meta(channel_meta)
    assert manifest == [
        {
            "display_name": "客户原始名（A B）.xlsx",
            "canonical_path": str(attachment.resolve()),
            "media_type": (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            "size_bytes": attachment.stat().st_size,
            "schema_version": 1,
            "digest": f"sha256:{grant.sha256}",
            "version": 1,
            "workspace_staged": True,
        },
    ]

    message = _runtime_attachment_manifest_message(
        {"trusted_current_attachments": manifest},
    )
    assert message is not None
    assert message.role == "system"
    assert str(attachment.resolve()) in message.text_content()
    assert "客户原始名（A B）.xlsx" in message.text_content()
    assert "do not search" in message.text_content()
    assert "do not copy it again" in message.text_content()
    assert forged_path not in message.text_content()


@pytest.mark.asyncio
async def test_runner_start_initializes_session_store(tmp_path) -> None:
    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)

    await runner.start()

    assert runner._started is True
    assert runner.session is not None
    assert runner.session.save_dir == str(tmp_path / "sessions")
    assert (tmp_path / "sessions").is_dir()

    await runner.stop()
    assert runner._started is False


@pytest.mark.asyncio
async def test_stream_query_adapts_channel_request_to_query_handler(tmp_path) -> None:
    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    seen: dict[str, object] = {}
    request = SimpleNamespace(input=[_RuntimeUserMsg("hello")])

    async def fake_query_handler(msgs, request=None, **_kwargs):
        seen["msgs"] = msgs
        seen["request"] = request
        yield "event", True

    runner.query_handler = fake_query_handler  # type: ignore[method-assign]

    events = [item async for item in runner.stream_query(request)]

    assert events == [("event", True)]
    assert seen == {"msgs": request.input, "request": request}


@pytest.mark.asyncio
async def test_stream_query_accepts_dict_request_from_cron_or_heartbeat(
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    seen: dict[str, object] = {}
    request = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "heartbeat"}],
            },
        ],
        "session_id": "main",
        "user_id": "main",
        "channel": "console",
    }

    async def fake_query_handler(msgs, request=None, **_kwargs):
        seen["msgs"] = msgs
        seen["request_session_id"] = request.session_id
        seen["request_user_id"] = request.user_id
        seen["request_channel"] = request.channel
        yield "event", True

    runner.query_handler = fake_query_handler  # type: ignore[method-assign]

    events = [item async for item in runner.stream_query(request)]

    assert events == [("event", True)]
    assert seen["request_session_id"] == "main"
    assert seen["request_user_id"] == "main"
    assert seen["request_channel"] == "console"
    assert len(seen["msgs"]) == 1


def test_rewrite_last_message_text_replaces_or_inserts_text_block() -> None:
    with_text = Msg(
        "user",
        [
            {"type": "text", "text": "old"},
            {"type": "image", "source": {"type": "url", "url": "file:///tmp/a.png"}},
        ],
        "user",
    )
    AgentRunner._rewrite_last_message_text([with_text], "new")
    assert _first_text(with_text) == "new"

    without_text = Msg(
        "user",
        [{"type": "image", "source": {"type": "url", "url": "file:///tmp/a.png"}}],
        "user",
    )
    AgentRunner._rewrite_last_message_text([without_text], "inserted")
    assert _first_text(without_text) == "inserted"
    assert len(without_text.content) == 2

    string_content = Msg("user", "old string", "user")
    AgentRunner._rewrite_last_message_text([string_content], "new string")
    assert string_content.content == "new string"


def test_maybe_inject_skill_returns_info_or_rewrites_query(tmp_path) -> None:
    skill_dir = tmp_path / "WeChat Read"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: WeChat Read\n"
        "description: Sync bookshelf and highlights.\n"
        "---\n"
        "Use the official skill API and keep data local.\n",
        encoding="utf-8",
    )
    skills = {"wechat-read": {"dir": str(skill_dir)}}

    info_msg = AgentRunner._maybe_inject_skill(
        "/[WeChat Read]",
        [Msg("user", "/[WeChat Read]", "user")],
        skills,
    )
    assert info_msg is not None
    assert "Sync bookshelf and highlights." in _first_text(info_msg)
    assert str(skill_dir) in _first_text(info_msg)

    user_msg = Msg("user", "/[WeChat Read] 同步最近划线", "user")
    rewritten = AgentRunner._maybe_inject_skill(
        "/[WeChat Read] 同步最近划线",
        [user_msg],
        skills,
    )
    assert rewritten is None
    merged = _first_text(user_msg)
    assert "Use the [WeChat Read] skill" in merged
    assert "同步最近划线" in merged
    assert "Use the official skill API and keep data local." in merged


def test_provider_content_filter_detection_walks_exception_chain() -> None:
    provider_error = RuntimeError(
        "request was rejected because prompt triggered content_filter",
    )
    wrapped = ValueError("model call failed")
    wrapped.__cause__ = provider_error

    assert _is_provider_content_filter_error(wrapped) is True
    assert _is_provider_content_filter_error(ValueError("network timeout")) is False


def test_sanitize_recent_tool_results_only_rewrites_current_turn() -> None:
    older_tool = {
        "type": "tool_result",
        "id": "old-tool",
        "output": [{"type": "text", "text": "old raw output"}],
    }
    recent_tool = {
        "type": "tool_result",
        "id": "new-tool",
        "output": [{"type": "text", "text": "recent raw output"}],
    }
    agent = SimpleNamespace(
        memory=SimpleNamespace(
            content=[
                (Msg("assistant", [older_tool], "assistant"), None),
                (Msg("user", "新的问题", "user"), None),
                (Msg("assistant", [recent_tool], "assistant"), None),
            ],
        ),
    )

    assert _sanitize_recent_tool_results_after_content_filter(agent) == 1
    assert older_tool["output"][0]["text"] == "old raw output"
    assert "工具结果已收起" in recent_tool["output"][0]["text"]


def test_provider_content_filter_fallback_is_user_facing_assistant_msg() -> None:
    msg = _provider_content_filter_fallback_msg()

    assert msg.role == "assistant"
    assert "模型供应商拒绝" in _first_text(msg)
    assert "工具调用是通的" in _first_text(msg)


@pytest.mark.asyncio
async def test_query_handler_command_path_short_circuits_agent_creation(
    monkeypatch,
) -> None:
    calls: list[str] = []
    snapshot_reads: list[str] = []

    async def fake_run_command_path(request, msgs, runner):
        calls.append(f"{request.session_id}:{msgs[-1].get_text_content()}")
        assert runner.agent_id == "default"
        yield Msg("Friday", "command ok", "assistant"), True

    def fail_agent_creation(*_args, **_kwargs):
        raise AssertionError("command path must not create LegacyLantaiAgent")

    monkeypatch.setattr(runner_module, "_is_command", lambda query: True)
    monkeypatch.setattr(runner_module, "run_command_path", fake_run_command_path)
    monkeypatch.setattr(runner_module, "LegacyLantaiAgent", fail_agent_creation)

    runner = AgentRunner(
        agent_id="default",
        beta_access_admission_guard=BetaAccessAdmissionGuard(
            lambda: snapshot_reads.append("snapshot")
            or AccessSnapshot(
                True,
                BetaAccessStatus.EXPIRED,
                "account-1",
                1,
                9,
            )
        ),
    )
    request = SimpleNamespace(session_id="s-command", user_id="u-1")

    output = [
        (msg.content, last)
        async for msg, last in runner.query_handler(
            [_RuntimeUserMsg("/new")],
            request,
        )
    ]

    assert calls == ["s-command:/new"]
    assert output == [("command ok", True)]
    assert snapshot_reads == []


@pytest.mark.asyncio
async def test_query_handler_denies_new_work_before_agent_creation(
    monkeypatch,
) -> None:
    def fail_agent_creation(*_args, **_kwargs):
        raise AssertionError("denied work must not create an agent")

    monkeypatch.setattr(runner_module, "_is_command", lambda _query: False)
    monkeypatch.setattr(runner_module, "LegacyLantaiAgent", fail_agent_creation)
    runner = AgentRunner(
        agent_id="default",
        beta_access_admission_guard=BetaAccessAdmissionGuard(
            lambda: AccessSnapshot(
                True,
                BetaAccessStatus.REVOKED,
                "account-1",
                3,
                10,
            )
        ),
    )
    request = SimpleNamespace(
        session_id="s-denied",
        user_id="u-1",
        channel="console",
        channel_meta={},
    )

    with pytest.raises(BetaAccessAdmissionDenied) as raised:
        _ = [
            item
            async for item in runner.query_handler(
                [_RuntimeUserMsg("开始新工作")],
                request,
            )
        ]

    assert raised.value.reason == "beta_access_revoked:work_start"
    assert raised.value.snapshot_revision == 10


@pytest.mark.asyncio
async def test_query_handler_collaboration_member_is_persona_free(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []
    agents: list[object] = []
    runtime_inputs = []
    runtime_workspace = tmp_path / "runtime-workspace"

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            events.append("create_agent")
            agents.append(self)
            self.request_context = kwargs["request_context"]
            self.runtime_workspace_dir = kwargs["runtime_workspace_dir"]
            self.toolkit = SimpleNamespace(skills={})
            self.sys_prompt = "INTERNAL_WORKER_PROMPT"

        def set_turn_intelligence_decision(self, _decision) -> None:
            raise AssertionError("internal workers must skip turn intelligence")

        def prepare_persona_turn(self, _msgs) -> None:
            raise AssertionError("internal workers must skip persona projection")

        async def register_mcp_clients(self) -> None:
            events.append("register_mcp")

        def rebuild_sys_prompt(self) -> None:
            events.append("rebuild_sys_prompt")

    class FakeRuntimeRunner:
        def __init__(self, adapter) -> None:
            assert getattr(adapter, "label", "") == "native"

        async def run_turn(self, runtime_input):
            runtime_inputs.append(runtime_input)
            assert runtime_input.messages[-1].text_content() == "你好"
            yield RuntimeEvent(
                id="evt-standard-final",
                type="message.completed",
                status="completed",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                payload={
                    "message": RuntimeMessage.text(
                        "assistant",
                        "final",
                        name="Friday",
                    ).model_dump(mode="json"),
                },
            )
            yield RuntimeEvent(
                id="evt-standard-turn",
                type="turn.completed",
                status="completed",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )

    monkeypatch.setattr(runner_module, "_is_command", lambda query: False)
    monkeypatch.setattr(runner_module, "LegacyLantaiAgent", FakeAgent)
    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(
        runner_module,
        "NativeLantaiRuntime",
        lambda **_kwargs: SimpleNamespace(label="native"),
    )
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        lambda self, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_tool_policy_context",
        lambda **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(
            id=agent_id,
            plan=SimpleNamespace(enabled=False),
            runtime=SimpleNamespace(engine="legacy"),
            running=SimpleNamespace(max_iters=5),
        ),
    )

    async def no_mission_command(**_kwargs):
        return None

    monkeypatch.setattr(
        runner_module,
        "maybe_handle_mission_command",
        no_mission_command,
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    runner.session = _FakeSession(events)
    runner.set_chat_manager(_FakeChatManager(events))
    request = SimpleNamespace(
        session_id="s-standard",
        user_id="u-1",
        channel="console",
        channel_meta={
            "chat_id": "chat-runtime",
            "run_id": "run-123",
            "runtime_generation": 7,
            "workspace_dir": str(runtime_workspace),
        },
        root_session_id="root-123",
        metadata={
            "collaboration_batch_id": "batch-safe",
            "collaboration_member_id": "member-1",
            "presentation_context": "subagent_internal",
            "delegation_root_run_id": "root-run-exact",
            "delegation_parent_run_id": "parent-run-exact",
            "delegation_parent_attempt_id": "parent-attempt-exact",
            "delegation_parent_effect_id": "parent-effect-exact",
            "delegation_task_id": "t" * 134 + ":member-1:0123456789abcdef",
            "collaboration_read_only": True,
            "execution_team_mode": "scoped_execute",
            "delegation_allowed_tools": ["read_file", "write_file"],
            "delegation_nested_allowed": False,
            "native_response_max_tokens": 1024,
            "untrusted_private_value": "must-not-propagate",
        },
    )

    output = [
        (_first_text(msg), last, msg.metadata.get("runtime_event_type"))
        async for msg, last in runner.query_handler(
            [_RuntimeUserMsg("你好")],
            request,
        )
    ]

    assert output == [
        ("final", False, "message.completed"),
        ("", True, "turn.completed"),
    ]
    assert events == [
        "get_chat",
        "create_agent",
        "register_mcp",
        "load_session",
        "rebuild_sys_prompt",
        "save_session",
        "touch_chat",
    ]
    assert runtime_workspace.is_dir()
    agent = agents[0]
    assert agent.request_context["chat_id"] == "chat-runtime"
    assert agent.request_context["run_id"] == "run-123"
    assert agent.request_context["turn_id"] == "run-123"
    assert agent.request_context["runtime_generation"] == 7
    assert agent.request_context["root_session_id"] == "root-123"
    assert agent.request_context["collaboration_batch_id"] == "batch-safe"
    assert agent.request_context["collaboration_member_id"] == "member-1"
    assert agent.request_context["presentation_context"] == "subagent_internal"
    assert agent.request_context["delegation_root_run_id"] == "root-run-exact"
    assert agent.request_context["delegation_parent_run_id"] == "parent-run-exact"
    assert agent.request_context["delegation_parent_attempt_id"] == (
        "parent-attempt-exact"
    )
    assert agent.request_context["delegation_parent_effect_id"] == (
        "parent-effect-exact"
    )
    assert agent.request_context["delegation_task_id"] == (
        "t" * 134 + ":member-1:0123456789abcdef"
    )
    assert agent.request_context["collaboration_read_only"] is True
    assert agent.request_context["execution_team_mode"] == "scoped_execute"
    assert agent.request_context["delegation_allowed_tools"] == [
        "read_file",
        "write_file",
    ]
    assert agent.request_context["delegation_nested_allowed"] is False
    assert agent.request_context["native_response_max_tokens"] == 1024
    assert "untrusted_private_value" not in agent.request_context
    assert str(agent.runtime_workspace_dir) == str(runtime_workspace)
    assert len(runtime_inputs) == 1
    runtime_input = runtime_inputs[0]
    assert [message.text_content() for message in runtime_input.messages] == ["你好"]
    runtime_metadata = runtime_input.request.metadata
    assert runtime_metadata["runtime_engine"] == "native"
    assert runtime_metadata["collaboration_member_id"] == "member-1"
    assert runtime_metadata["presentation_context"] == "subagent_internal"
    assert runtime_metadata["delegation_root_run_id"] == "root-run-exact"
    assert runtime_metadata["delegation_parent_run_id"] == "parent-run-exact"
    assert runtime_metadata["delegation_parent_attempt_id"] == (
        "parent-attempt-exact"
    )
    assert runtime_metadata["delegation_parent_effect_id"] == (
        "parent-effect-exact"
    )
    assert runtime_metadata["delegation_task_id"] == (
        "t" * 134 + ":member-1:0123456789abcdef"
    )
    assert runtime_metadata["collaboration_read_only"] is True
    assert runtime_metadata["execution_team_mode"] == "scoped_execute"
    assert runtime_metadata["delegation_allowed_tools"] == [
        "read_file",
        "write_file",
    ]
    assert runtime_metadata["delegation_nested_allowed"] is False
    assert runtime_metadata["native_response_max_tokens"] == 1024
    assert runtime_metadata["foreground_latency_ms"]["native_context"] == 0.0
    assert runtime_metadata["foreground_latency_ms"]["persona_projection"] == 0.0
    assert "persona_turn_plan" not in runtime_metadata
    assert "ququ_hard_required" not in runtime_metadata
    assert runner.session.loaded[0]["session_id"] == (
        "console:chat:chat-runtime"
    )
    assert runner.session.saved[0]["session_id"] == (
        "console:chat:chat-runtime"
    )
    assert runner._chat_manager.touched == ["chat-runtime"]


@pytest.mark.asyncio
async def test_query_handler_standard_path_uses_runtime_runner_seam(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []
    runtime_inputs = []

    class FakeAgent:
        def __init__(self, **_kwargs) -> None:
            events.append("create_agent")
            self.toolkit = SimpleNamespace(skills={})

        async def register_mcp_clients(self) -> None:
            events.append("register_mcp")

        def rebuild_sys_prompt(self) -> None:
            events.append("rebuild_sys_prompt")

    class FakeRuntimeRunner:
        def __init__(self, _adapter, **_kwargs) -> None:
            events.append("create_runtime_runner")
            self.request_policy_observation_invocation = None

        async def run_turn(self, runtime_input):
            runtime_inputs.append(runtime_input)
            yield RuntimeEvent(
                id="evt-1",
                type="message.delta",
                status="running",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                payload={
                    "message": RuntimeMessage.text(
                        "assistant",
                        "runtime partial",
                        name="Friday",
                    ).model_dump(mode="json"),
                },
            )
            yield RuntimeEvent(
                id="evt-2",
                type="message.completed",
                status="completed",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                payload={
                    "message": RuntimeMessage.text(
                        "assistant",
                        "runtime final",
                        name="Friday",
                    ).model_dump(mode="json"),
                },
            )
            yield RuntimeEvent(
                id="evt-3",
                type="turn.completed",
                status="completed",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )

    monkeypatch.setattr(runner_module, "_is_command", lambda query: False)
    monkeypatch.setattr(runner_module, "LegacyLantaiAgent", FakeAgent)
    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(
        runner_module,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(
            id=agent_id,
            plan=SimpleNamespace(enabled=False),
            runtime=SimpleNamespace(engine="legacy"),
        ),
    )

    async def no_mission_command(**_kwargs):
        return None

    monkeypatch.setattr(
        runner_module,
        "maybe_handle_mission_command",
        no_mission_command,
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    runner.session = _FakeSession(events)
    request = SimpleNamespace(
        session_id="s-runtime",
        user_id="u-1",
        channel="console",
        channel_meta={"run_id": "run-runtime"},
        root_session_id="root-runtime",
    )

    output = [
        (_first_text(msg), last, msg.metadata.get("runtime_event_type"))
        async for msg, last in runner.query_handler(
            [_RuntimeUserMsg("走 runtime")],
            request,
        )
    ]

    assert output == [
        ("runtime partial", False, "message.delta"),
        ("runtime final", False, "message.completed"),
        ("", True, "turn.completed"),
    ]
    assert "create_runtime_runner" in events
    assert runtime_inputs[0].request.run_id == "run-runtime"
    assert runtime_inputs[0].request.root_session_id == "root-runtime"
    assert runtime_inputs[0].messages[-1].text_content() == "走 runtime"


@pytest.mark.asyncio
async def test_stream_query_exposes_channel_schema_events(
    tmp_path,
    monkeypatch,
) -> None:
    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    partial = Msg(
        "Friday",
        [{"type": "text", "text": "partial"}],
        "assistant",
        metadata={"runtime_event_type": "message.delta"},
    )
    partial.id = "channel-msg-1"
    final = Msg(
        "Friday",
        [{"type": "text", "text": "partial final"}],
        "assistant",
        metadata={"runtime_event_type": "message.completed"},
    )
    final.id = "channel-msg-1"

    async def fake_query_handler(_msgs, request=None, **_kwargs):
        yield partial, False
        yield final, True

    monkeypatch.setattr(runner, "query_handler", fake_query_handler)
    request = SimpleNamespace(input=[_RuntimeUserMsg("hi")])

    output = [event async for event in runner.stream_query(request)]

    assert [event.object for event in output] == [
        "message",
        "content",
        "message",
        "response",
    ]
    assert output[0].id == "channel-msg-1"
    assert output[0].status == "in_progress"
    assert output[1].msg_id == "channel-msg-1"
    assert output[1].delta is True
    assert output[2].id == "channel-msg-1"
    assert output[2].status == "completed"
    assert output[3].status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("private_editor", [False, True])
async def test_query_handler_native_path_reads_and_writes_agent_memory(
    tmp_path,
    monkeypatch,
    private_editor,
) -> None:
    events: list[str] = []
    runtime_inputs = []
    added_messages: list[Msg] = []
    turn_decisions = []
    understanding_contexts = []

    class FakeMemory:
        def __init__(self) -> None:
            self.content = [
                (Msg("user", "历史用户", "user"), None),
                (Msg("Friday", "历史回答", "assistant"), None),
            ]

        async def add(self, msg: Msg) -> None:
            events.append(f"memory_add:{msg.role}:{_first_text(msg)}")
            added_messages.append(msg)
            self.content.append((msg, None))

    class FakeAgent:
        def __init__(self, **_kwargs) -> None:
            events.append("create_agent")
            self.toolkit = SimpleNamespace(skills={})
            self.memory = FakeMemory()
            self.sys_prompt = "你是沈行知。始终保持温柔、直接、简单、克制。"
            self.ququ_hard_required = True

        def prepare_persona_turn(self, messages) -> None:
            events.append("prepare_persona_turn")
            assert messages[-1].get_text_content() == "现在问题"
            self.sys_prompt += (
                '\n<subjective-presence mode="model-discretion">'
                "本轮主体反应"
                "</subjective-presence>"
            )

        def set_turn_intelligence_decision(self, decision) -> None:
            turn_decisions.append(decision)

        def set_user_understanding_turn_context(self, context) -> None:
            understanding_contexts.append(context)

        async def register_mcp_clients(self) -> None:
            events.append("register_mcp")

        def rebuild_sys_prompt(self) -> None:
            events.append("rebuild_sys_prompt")

    class FakeRuntimeRunner:
        def __init__(self, adapter) -> None:
            events.append(f"create_runtime_runner:{getattr(adapter, 'label', '')}")

        async def run_turn(self, runtime_input):
            runtime_inputs.append(runtime_input)
            yield RuntimeEvent(
                id="evt-native-final",
                type="message.completed",
                status="completed",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                payload={
                    "message": RuntimeMessage.text(
                        "assistant",
                        "native final",
                        name="Friday",
                    ).model_dump(mode="json"),
                },
            )
            yield RuntimeEvent(
                id="evt-native-turn",
                type="turn.completed",
                status="completed",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )

    monkeypatch.setattr(runner_module, "_is_command", lambda query: False)
    monkeypatch.setattr(runner_module, "LegacyLantaiAgent", FakeAgent)
    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(
        runner_module,
        "NativeLantaiRuntime",
        lambda **_kwargs: SimpleNamespace(label="native"),
    )

    from lantai.agents import turn_intelligence

    async def classify_turn(*_args, **_kwargs):
        raise AssertionError(
            "Auxiliary turn intelligence must not run before the primary turn"
        )

    monkeypatch.setattr(
        turn_intelligence,
        "classify_turn_intent",
        classify_turn,
    )
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        lambda self, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(
            id=agent_id,
            plan=SimpleNamespace(enabled=False),
            runtime=SimpleNamespace(engine="native"),
            running=SimpleNamespace(max_iters=5),
        ),
    )

    async def no_mission_command(**_kwargs):
        return None

    monkeypatch.setattr(
        runner_module,
        "maybe_handle_mission_command",
        no_mission_command,
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    from lantai.agents.persona.user_understanding import (
        TurnUnderstandingProjection,
        UnderstandingAtom,
        UserUnderstandingTurnContext,
    )

    supplied_atom = UnderstandingAtom.create(
        kind="preference",
        statement="用户希望人格化体现在正文判断，不只是蛐蛐",
        status="confirmed",
        confidence=0.99,
        evidence_ids=("message-persona-1",),
        future_relevance="影响每轮表达面选择",
    )
    understanding_projection = TurnUnderstandingProjection(
        confirmed=(supplied_atom,),
    )
    supplied_understanding = UserUnderstandingTurnContext(
        prompt=understanding_projection.compile_prompt("zh"),
        projection=understanding_projection,
    )
    runner.memory_manager = SimpleNamespace(
        project_user_understanding_context=(
            lambda *_args, **_kwargs: supplied_understanding
        ),
        schedule_capture_turn=lambda **_kwargs: None,
    )
    runner.session = _FakeSession(events)
    pending_deliverable_context = {
        "previous_run_id": "run-ppt-continued",
        "source_contract_revision": 2,
        "source_task_revision": 1,
        "pending_job_id": "presentation-job-original",
        "pending_job_owner_run_id": "run-ppt-original",
        "deliverable_contract": {
            "kind": "presentation",
            "accepted_extensions": [".pptx"],
            "visual_verification_required": True,
        },
    }
    request = SimpleNamespace(
        session_id="s-native-memory",
        user_id="u-1",
        channel="console",
        channel_meta={
            "run_id": "run-native-memory",
            "pending_deliverable_context": pending_deliverable_context,
            "coding_mode": True,
            "coding_context": "当前工作区上下文",
            "workspace_dir": str(tmp_path / "topic-workspace"),
        },
        root_session_id="root-native-memory",
    )
    last_decision = "最后一条作者明确确认的作品取舍。"
    work_context = "历史作品记录。\n" * 1300 + last_decision
    request.channel_meta["work_edit_context"] = work_context
    if private_editor:
        request.channel_meta.update({
            "work_method_id": "private_editor",
            "_trusted_private_editor_binding": True,
            "private_editor_project_id": "project-long",
            "private_editor_original_work_id": "work-original",
            "private_editor_current_work_id": "work-current",
            "private_editor_current_digest": "sha256:current",
            "private_editor_record_revision": 1301,
            "private_editor_revocation_epoch": 0,
            "private_editor_project_status": "active",
        })

    output = [
        (_first_text(msg), last)
        async for msg, last in runner.query_handler(
            [_RuntimeUserMsg("现在问题")],
            request,
        )
    ]

    assert output == [("native final", False), ("", True)]
    assert "create_runtime_runner:native" in events
    assert [msg.role for msg in added_messages] == ["user", "assistant"]
    assert [_first_text(msg) for msg in added_messages] == ["现在问题", "native final"]
    assert added_messages[0].id
    assert added_messages[0].id == runtime_inputs[0].messages[-1].id
    assert [message.role for message in runtime_inputs[0].messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert runtime_inputs[0].messages[0].metadata["instruction_trust"] == (
        "trusted_system"
    )
    assert runtime_inputs[0].messages[0].metadata["instruction_origin"] == (
        "prompt.compiled_agent"
    )
    assert "prepare_persona_turn" in events
    assert [message.text_content() for message in runtime_inputs[0].messages] == [
        (
            "你是沈行知。始终保持温柔、直接、简单、克制。"
            '\n<subjective-presence mode="model-discretion">'
            "本轮主体反应"
            "</subjective-presence>"
        ),
        "历史用户",
        "历史回答",
        "现在问题",
    ]
    assert runtime_inputs[0].request.metadata["runtime_engine"] == "native"
    assert runtime_inputs[0].request.metadata["ququ_hard_required"] is True
    assert len(turn_decisions) == 1
    assert understanding_contexts == [supplied_understanding]
    assert turn_decisions[0].status == "unavailable"
    assert runtime_inputs[0].request.metadata["turn_intelligence"]["status"] == (
        "unavailable"
    )
    assert runtime_inputs[0].request.metadata[
        "pending_deliverable_context"
    ] == pending_deliverable_context
    latency = runtime_inputs[0].request.metadata["foreground_latency_ms"]
    assert latency["schema_version"] == "1"
    assert latency["turn_intelligence"] == 0
    assert runtime_inputs[0].request.root_session_id == "root-native-memory"
    assert runtime_inputs[0].request.workspace_dir == str(tmp_path / "topic-workspace")
    legacy_context = runtime_inputs[0].request.metadata["legacy_request_context"]
    assert legacy_context["coding_mode"] == "true"
    assert legacy_context["coding_context"] == "当前工作区上下文"
    assert legacy_context["workspace_dir"] == str(tmp_path / "topic-workspace")
    assert legacy_context["work_edit_context"] == (
        work_context if private_editor else work_context[:8000]
    )
    from lantai.agents.work_edit import build_work_edit_prompt

    assert (last_decision in build_work_edit_prompt(legacy_context)) is private_editor


@pytest.mark.asyncio
async def test_native_turn_captures_completed_turn_for_memory(
    tmp_path,
    monkeypatch,
) -> None:
    """Native replies must cross the same durable memory seam as legacy replies."""

    captured: list[dict[str, object]] = []
    projected_session_ids: list[str] = []
    terminal_observed = False

    class FakeMemory:
        content: list[object] = []

        async def add(self, _msg: Msg) -> None:
            return None

    class FakeMemoryManager:
        def project_subject_continuity_context(self, *_args, **kwargs):
            projected_session_ids.append(kwargs["session_id"])
            return None

        def schedule_capture_turn(self, **kwargs):
            assert terminal_observed is True
            captured.append(kwargs)

        async def capture_turn(self, **_kwargs):
            raise AssertionError("native memory capture must be scheduled")

    class FakeRuntimeRunner:
        def __init__(self, _adapter) -> None:
            pass

        async def run_turn(self, runtime_input):
            yield RuntimeEvent(
                id="evt-native-document",
                seq=1,
                type="tool.call.completed",
                status="completed",
                source="tool",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                payload={
                    "id": "call-document",
                    "name": "read_document",
                    "ok": True,
                    "output": "材料说明：该项目面向普通用户，首要目标是可靠记忆。",
                },
            )
            yield RuntimeEvent(
                id="evt-native-final",
                seq=2,
                type="message.completed",
                status="completed",
                source="assistant",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                payload={
                    "message": RuntimeMessage.text(
                        "assistant",
                        "我记住了你的工作与生活近况。",
                        name="Friday",
                    ).model_dump(mode="json"),
                },
            )
            yield RuntimeEvent(
                id="evt-native-turn",
                seq=3,
                type="turn.completed",
                status="completed",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )

    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(
        runner_module,
        "NativeLantaiRuntime",
        lambda **_kwargs: SimpleNamespace(label="native"),
    )
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        lambda self, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_tool_policy_context",
        lambda **_kwargs: SimpleNamespace(),
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    runner.memory_manager = FakeMemoryManager()
    output = []
    async for item in runner._stream_standard_runtime_turn(
            agent=SimpleNamespace(
                memory=FakeMemory(),
                set_turn_intelligence_decision=lambda _decision: None,
                set_subject_continuity_projection=lambda _projection: None,
            ),
            agent_config=SimpleNamespace(
                runtime=SimpleNamespace(engine="native"),
                running=SimpleNamespace(max_iters=5),
            ),
            mcp_clients=[],
            msgs=[
                ChannelMessage(
                    id="msg-user-disclosure",
                    type=MessageType.MESSAGE,
                    role=Role.USER,
                    status=RunStatus.Completed,
                    content=[
                        TextContent(
                            type=ContentType.TEXT,
                            text="我刚接手 C 端公关一号位，现在管理七八个人。",
                        )
                    ],
                ),
            ],
            session_id="session-disclosure",
            user_id="default",
            channel="console",
            request_context={
                "chat_id": "chat-disclosure",
                "run_id": "run-disclosure",
                "root_session_id": "session-disclosure",
                "session_state_id": "console:chat:chat-disclosure",
            },
            workspace_dir=tmp_path,
        ):
        output.append(item)
        if item[1] is True:
            terminal_observed = True

    assert output
    assert projected_session_ids == ["console:chat:chat-disclosure"]
    assert len(captured) == 1
    assert captured[0]["session_id"] == "console:chat:chat-disclosure"
    assert captured[0]["source"] == "native_post_reply"
    messages = captured[0]["messages"]
    assert [messages[0].get_text_content(), messages[-1].get_text_content()] == [
        "我刚接手 C 端公关一号位，现在管理七八个人。",
        "我记住了你的工作与生活近况。",
    ]
    tool_blocks = messages[1].get_content_blocks("tool_result")
    assert tool_blocks[0]["name"] == "read_document"
    assert "首要目标是可靠记忆" in tool_blocks[0]["output"]


@pytest.mark.asyncio
async def test_native_turn_schedules_post_answer_sidecar_only_after_terminal_yield(
    tmp_path,
    monkeypatch,
) -> None:
    terminal_observed = False
    scheduled: list[PostAnswerReflectionRequest] = []
    recovered: list[dict[str, object]] = []
    request = PostAnswerReflectionRequest(
        agent_id="default",
        workspace_dir=str(tmp_path),
        chat_id="chat-sidecar",
        session_id="session-sidecar",
        run_id="run-sidecar",
        client_turn_id="client-sidecar",
        answer_message_id="answer-sidecar",
        user_message="你好",
        final_answer="你好呀。",
        plan={},
        subject_context={},
        need_persona=False,
        need_ququ=True,
        assistant_history=[],
    )

    class FakeMemory:
        async def add(self, _message) -> None:
            return None

    class FakeNativeAdapter:
        def take_post_answer_reflection_request(self):
            return request

    class FakeRuntimeRunner:
        def __init__(self, adapter) -> None:
            self.adapter = adapter

        async def run_turn(self, runtime_input):
            yield RuntimeEvent(
                id="evt-sidecar-answer",
                seq=1,
                type="message.completed",
                status="completed",
                source="assistant",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                payload={
                    "message": RuntimeMessage.text(
                        "assistant",
                        "你好呀。",
                    ).model_dump(mode="json"),
                },
            )
            yield RuntimeEvent(
                id="evt-sidecar-terminal",
                seq=2,
                type="turn.completed",
                status="completed",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )

    class FakeReflectionService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def recover_pending(self, **kwargs):
            assert terminal_observed is True
            recovered.append(kwargs)
            return []

        def schedule(self, sidecar_request, **_kwargs):
            assert terminal_observed is True
            scheduled.append(sidecar_request)
            return None

    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(
        runner_module,
        "NativeLantaiRuntime",
        lambda **_kwargs: FakeNativeAdapter(),
    )
    monkeypatch.setattr(
        reflection_service_module,
        "PostAnswerReflectionService",
        FakeReflectionService,
    )
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        lambda self, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_tool_policy_context",
        lambda **_kwargs: SimpleNamespace(),
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    async for _message, last in runner._stream_standard_runtime_turn(
        agent=SimpleNamespace(memory=FakeMemory()),
        agent_config=SimpleNamespace(
            runtime=SimpleNamespace(engine="native"),
            running=SimpleNamespace(max_iters=5),
        ),
        mcp_clients=[],
        msgs=[_RuntimeUserMsg("你好")],
        session_id="session-sidecar",
        user_id="default",
        channel="console",
        request_context={
            "chat_id": "chat-sidecar",
            "run_id": "run-sidecar",
            "root_session_id": "session-sidecar",
            "session_state_id": "console:chat:chat-sidecar",
        },
        workspace_dir=tmp_path,
    ):
        if last:
            terminal_observed = True

    assert recovered[0]["session_id"] == "console:chat:chat-sidecar"
    assert scheduled == [
        PostAnswerReflectionRequest(
            **{
                **request.__dict__,
                "session_id": "console:chat:chat-sidecar",
            }
        )
    ]


@pytest.mark.asyncio
async def test_native_turn_binds_effect_journal_to_chat_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    """A Native tool effect must share the chat run's durable identity."""

    captured: dict[str, object] = {}

    class FakeMemory:
        content: list[object] = []

        async def add(self, _msg: Msg) -> None:
            return None

    class FakeRuntimeRunner:
        def __init__(self, _adapter) -> None:
            pass

        async def run_turn(self, runtime_input):
            captured["runtime_input"] = runtime_input
            yield RuntimeEvent(
                id="evt-native-turn",
                seq=2,
                type="turn.completed",
                status="completed",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )

    def fake_native_runtime(**kwargs):
        captured["native_kwargs"] = kwargs
        return SimpleNamespace(label="native")

    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(runner_module, "NativeLantaiRuntime", fake_native_runtime)
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        lambda self, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_tool_policy_context",
        lambda **_kwargs: SimpleNamespace(),
    )

    run_id = "run-native-effect"
    state_workspace = tmp_path / "topic-state"
    project_workspace = tmp_path / "external-project"
    await record_chat_turn_started(
        workspace_dir=state_workspace,
        agent_id="default",
        chat_id="chat-native-effect",
        session_id="session-native-effect",
        text_preview="use a tool",
        effective_workspace_dir=str(project_workspace),
        run_id=run_id,
    )

    runner = AgentRunner(agent_id="default", workspace_dir=state_workspace)
    output = [
        item
        async for item in runner._stream_standard_runtime_turn(
            agent=SimpleNamespace(memory=FakeMemory()),
            agent_config=SimpleNamespace(
                runtime=SimpleNamespace(engine="native"),
                running=SimpleNamespace(max_iters=5),
            ),
            mcp_clients=[],
            msgs=[_RuntimeUserMsg("use a tool")],
            session_id="session-native-effect",
            user_id="default",
            channel="console",
            request_context={
                "chat_id": "chat-native-effect",
                "run_id": run_id,
                "root_session_id": "session-native-effect",
                "state_workspace_dir": str(state_workspace),
            },
            workspace_dir=project_workspace,
        )
    ]

    assert output
    contract = await RuntimeRunStore(state_workspace).load_contract(run_id)
    assert contract is not None
    native_kwargs = captured["native_kwargs"]
    # Product turns are progress-bounded instead of quota-bounded. The
    # configured finite value remains available to explicit bounded flows.
    assert native_kwargs["max_iters"] is None
    assert native_kwargs["effect_journal"].workspace_dir == state_workspace
    assert (
        native_kwargs["artifact_effect_journal"].workspace_dir
        == state_workspace
    )
    runtime_input = captured["runtime_input"]
    assert runtime_input.request.workspace_dir == str(project_workspace)
    assert (
        runtime_input.request.metadata["state_workspace_dir"]
        == str(state_workspace)
    )
    assert runtime_input.request.metadata["attempt_id"] == contract.attempt_id
    assert runtime_input.request.metadata["contract_hash"] == contract.contract_hash
    assert (
        runtime_input.request.metadata["policy_snapshot_hash"]
        == contract.policy_snapshot_hash
    )


@pytest.mark.asyncio
async def test_native_runtime_tool_and_approval_events_reach_chat_event_ledger(
    tmp_path,
    monkeypatch,
) -> None:
    """The frontend Runtime stream must see Native tool/approval lifecycle."""

    class FakeMemory:
        content: list[object] = []

        async def add(self, _msg: Msg) -> None:
            return None

    class FakeRuntimeRunner:
        def __init__(self, _adapter) -> None:
            pass

        async def run_turn(self, runtime_input):
            common = {
                "session_id": runtime_input.request.session_id,
                "turn_id": runtime_input.request.run_id,
                "item_id": "call-1",
            }
            yield RuntimeEvent(
                id="evt-tool-call-started",
                seq=2,
                type="tool.call.started",
                status="running",
                source="assistant",
                summary="lookup",
                payload={
                    "id": "call-1",
                    "name": "lookup",
                    "arguments": {"query": "澜台"},
                },
                **common,
            )
            yield RuntimeEvent(
                id="evt-approval-requested",
                seq=3,
                type="approval.requested",
                status="running",
                source="system",
                summary="需要审批",
                payload={
                    "request_id": "approval-1",
                    "tool_name": "lookup",
                    "arguments": {"query": "澜台"},
                },
                **common,
            )
            yield RuntimeEvent(
                id="evt-approval-resolved",
                seq=4,
                type="approval.resolved",
                status="completed",
                source="system",
                summary="审批通过",
                payload={
                    "request_id": "approval-1",
                    "tool_name": "lookup",
                    "decision": "allow",
                },
                **common,
            )
            yield RuntimeEvent(
                id="evt-tool-call-completed",
                seq=5,
                type="tool.call.completed",
                status="completed",
                source="tool",
                summary="lookup completed",
                payload={
                    "attempt_id": runtime_input.request.metadata["attempt_id"],
                    "effect_id": "effect:call-1",
                    "id": "call-1",
                    "name": "lookup",
                    "output": "found:澜台",
                    "ok": True,
                },
                **common,
            )
            yield RuntimeEvent(
                id="evt-native-final",
                seq=6,
                type="message.completed",
                status="completed",
                source="assistant",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                item_id="msg-1",
                payload={
                    "message": RuntimeMessage.text(
                        "assistant",
                        "native final",
                        name="Friday",
                    ).model_dump(mode="json"),
                },
            )
            yield RuntimeEvent(
                id="evt-native-turn",
                seq=7,
                type="turn.completed",
                status="completed",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )

    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(
        runner_module,
        "NativeLantaiRuntime",
        lambda **_kwargs: SimpleNamespace(label="native"),
    )
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        lambda self, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_tool_policy_context",
        lambda **_kwargs: SimpleNamespace(),
    )

    run_id = "run-native-contract"
    await record_chat_turn_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        text_preview="use lookup",
        effective_workspace_dir=str(tmp_path),
        run_id=run_id,
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    agent = SimpleNamespace(memory=FakeMemory())
    agent_config = SimpleNamespace(
        runtime=SimpleNamespace(engine="native"),
        running=SimpleNamespace(max_iters=5),
    )
    output = [
        (msg, last)
        async for msg, last in runner._stream_standard_runtime_turn(
            agent=agent,
            agent_config=agent_config,
            mcp_clients=[],
            msgs=[_RuntimeUserMsg("use lookup")],
            session_id="session-1",
            user_id="default",
            channel="console",
            request_context={
                "chat_id": "chat-1",
                "run_id": run_id,
                "root_session_id": "session-1",
            },
            workspace_dir=tmp_path,
        )
    ]

    assert output
    runtime_events = build_runtime_events_response(tmp_path, "chat-1").events
    event_types = [event.type for event in runtime_events]
    assert "tool.call.started" in event_types
    assert "approval.requested" in event_types
    assert "approval.resolved" in event_types
    assert "tool.call.completed" in event_types
    tool_started = next(
        event for event in runtime_events if event.type == "tool.call.started"
    )
    assert tool_started.turn_id == run_id
    assert tool_started.item_id == "call-1"
    assert (
        tool_started.payload["details"]["native_runtime_event_id"]
        == "evt-tool-call-started"
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    control_events = [
        event
        for event in runtime_events
        if event.type
        in {
            "tool.call.started",
            "approval.requested",
            "approval.resolved",
            "tool.call.completed",
        }
    ]
    assert control_events
    assert all(
        event.payload.get("attempt_id") == contract.attempt_id
        for event in control_events
    )
    attempt_events = build_runtime_run_events_response(
        [tmp_path],
        run_id,
        attempt_id=contract.attempt_id,
    ).events
    assert {
        "tool.call.started",
        "approval.requested",
        "approval.resolved",
        "tool.call.completed",
    }.issubset({event.type for event in attempt_events})
    assert contract.checkpoint is not None
    assert contract.checkpoint.effect_ids == ("effect:call-1",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side_effect_outcome", "effect_status"),
    [("unknown", "running"), ("committed", "committed")],
)
async def test_rejected_previous_effect_remains_in_current_attempt_completion(
    tmp_path,
    side_effect_outcome,
    effect_status,
) -> None:
    run_id = "run-restarted-effect-check"
    session_id = "console:chat-restarted-effect-check"
    chat_id = "chat-restarted-effect-check"
    await record_chat_runtime_run_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id=chat_id,
        session_id=session_id,
        run_id=run_id,
        effective_workspace_dir=str(tmp_path),
        task_contract=build_chat_task_contract(
            objective="检查上一次操作的结果。",
            client_turn_id="client-restarted-effect-check",
            complexity="light",
            work_method_id="standard",
        ),
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(run_id)
    assert contract is not None
    old_attempt_id = "run-before-restart:chat-attempt-1"
    canonical_tool = {
        "effect_id": "effect:previous-write",
        "effect_attempt_id": old_attempt_id,
        "effect_status": effect_status,
        "effect_revision": 1,
    }
    event = RuntimeEvent(
        id="event-rejected-previous-effect",
        seq=3,
        type="tool.call.completed",
        status="failed",
        source="tool",
        session_id=session_id,
        turn_id=run_id,
        item_id="call-restarted-effect-check",
        payload={
            "attempt_id": old_attempt_id,
            "id": "call-restarted-effect-check",
            "name": "execute_shell_command",
            "ok": False,
            "metadata": {
                "invocation_status": "not_started",
                "side_effect_outcome": side_effect_outcome,
                "automatic_retry_allowed": False,
                "requires_human": True,
            },
            "canonical_tool": canonical_tool,
        },
    )
    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    await runner._persist_native_runtime_control_event(
        event=event.model_copy(
            update={
                "id": "event-started-restarted-effect-check",
                "seq": 2,
                "type": "tool.call.started",
                "status": "running",
                "payload": {
                    "attempt_id": contract.attempt_id,
                    "id": event.item_id,
                    "name": "execute_shell_command",
                },
            },
        ),
        request_context={"chat_id": chat_id, "run_id": run_id},
        workspace_dir=tmp_path,
        session_id=session_id,
        attempt_id=contract.attempt_id,
    )
    await runner._persist_native_runtime_control_event(
        event=event,
        request_context={"chat_id": chat_id, "run_id": run_id},
        workspace_dir=tmp_path,
        session_id=session_id,
        attempt_id=contract.attempt_id,
    )
    attempt_events = build_runtime_run_events_response(
        [tmp_path], run_id, attempt_id=contract.attempt_id
    ).events
    result = await finalize_deliverable(
        state_workspace_dir=tmp_path,
        effective_workspace_dir=tmp_path,
        run_id=run_id,
        chat_id=chat_id,
        response_status="completed",
        assistant_text="检查结束。",
        final_response_id="response-restarted-effect-check",
        runtime_events=attempt_events,
        durable_artifact_required=False,
    )

    assert result.allow_success is False
    assert result.delivery_status == "not_delivered"
    settlement = next(
        item
        for item in result.acceptance_results
        if item["id"] == "effects_settled"
    )
    assert settlement["passed"] is False
    assert canonical_tool["effect_id"] in settlement["detail"]
    persisted = next(
        item
        for item in attempt_events
        if item.payload.get("details", {}).get("native_runtime_event_id")
        == event.id
    )
    assert persisted.payload["attempt_id"] == contract.attempt_id
    assert persisted.payload["canonical_tool"] == canonical_tool
    assert event.payload["attempt_id"] == old_attempt_id
    old_attempt_events = build_runtime_run_events_response(
        [tmp_path], run_id, attempt_id=old_attempt_id
    ).events
    assert not any(
        item.payload.get("details", {}).get("native_runtime_event_id") == event.id
        for item in old_attempt_events
    )


@pytest.mark.asyncio
async def test_native_runtime_interrupt_closes_open_tool_and_approval_events(
    tmp_path,
    monkeypatch,
) -> None:
    """Stopping during approval must still leave a valid terminal sequence."""

    class FakeMemory:
        content: list[object] = []

        async def add(self, _msg: Msg) -> None:
            return None

    class InterruptingRuntimeRunner:
        def __init__(self, _adapter) -> None:
            pass

        async def run_turn(self, runtime_input):
            yield RuntimeEvent(
                id="evt-tool-open",
                seq=2,
                type="tool.call.started",
                status="running",
                source="tool",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                item_id="call-open",
                summary="dangerous_tool",
                payload={
                    "id": "call-open",
                    "name": "dangerous_tool",
                    "arguments": {},
                },
            )
            yield RuntimeEvent(
                id="evt-approval-open",
                seq=3,
                type="approval.requested",
                status="running",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                item_id="approval-open",
                summary="需要审批",
                payload={
                    "request_id": "approval-open",
                    "id": "call-open",
                    "tool_name": "dangerous_tool",
                    "arguments": {},
                },
            )
            yield RuntimeEvent(
                id="evt-tool-progress",
                seq=4,
                type="tool.call.delta",
                status="running",
                source="tool",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                item_id="call-open",
                summary="dangerous_tool",
                payload={
                    "id": "call-open",
                    "name": "dangerous_tool",
                    "arguments": {},
                    "collaboration": {
                        "batch_id": "batch-open",
                        "revision": 7,
                        "status": "running",
                        "members": [
                            {
                                "member_id": "member-1",
                                "agent_id": "agent-1",
                                "display_name": "抹茶",
                                "role": "写作",
                                "label": "结构",
                                "status": "completed",
                                "attempt": 1,
                                "duration_ms": 1200,
                                "error_code": None,
                            },
                            {
                                "member_id": "member-2",
                                "agent_id": "agent-2",
                                "display_name": "可乐",
                                "role": "文案",
                                "label": "素材",
                                "status": "running",
                                "attempt": 1,
                                "duration_ms": 0,
                                "error_code": None,
                            },
                        ],
                    },
                },
            )
            yield RuntimeEvent(
                id="evt-turn-interrupted",
                seq=5,
                type="turn.interrupted",
                status="interrupted",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                summary="用户已停止",
            )
            raise asyncio.CancelledError

    monkeypatch.setattr(
        runner_module,
        "RuntimeRunner",
        InterruptingRuntimeRunner,
    )
    monkeypatch.setattr(
        runner_module,
        "NativeLantaiRuntime",
        lambda **_kwargs: SimpleNamespace(label="native"),
    )
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        lambda self, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_tool_policy_context",
        lambda **_kwargs: SimpleNamespace(),
    )

    run_id = "run-native-interrupted"
    await record_chat_turn_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-interrupted",
        session_id="session-interrupted",
        text_preview="run dangerous tool",
        effective_workspace_dir=str(tmp_path),
        run_id=run_id,
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    with pytest.raises(asyncio.CancelledError):
        async for _item in runner._stream_standard_runtime_turn(
            agent=SimpleNamespace(memory=FakeMemory()),
            agent_config=SimpleNamespace(
                runtime=SimpleNamespace(engine="native"),
                running=SimpleNamespace(max_iters=5),
            ),
            mcp_clients=[],
            msgs=[_RuntimeUserMsg("run dangerous tool")],
            session_id="session-interrupted",
            user_id="default",
            channel="console",
            request_context={
                "chat_id": "chat-interrupted",
                "run_id": run_id,
                "root_session_id": "session-interrupted",
            },
            workspace_dir=tmp_path,
        ):
            pass

    await record_chat_turn_settled(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-interrupted",
        session_id="session-interrupted",
        final_task_status="interrupted",
        pending_turn_cleared=True,
        active_run_event_count=4,
        run_id=run_id,
    )

    runtime_events = build_runtime_events_response(
        tmp_path,
        "chat-interrupted",
    ).events
    event_types = [event.type for event in runtime_events]
    assert "tool.call.delta" in event_types
    assert event_types[-3:] == [
        "approval.resolved",
        "tool.call.completed",
        "turn.interrupted",
    ]
    completed = next(
        event
        for event in reversed(runtime_events)
        if event.type == "tool.call.completed"
    )
    collaboration = completed.payload["collaboration"]
    assert collaboration["revision"] == 8
    assert collaboration["status"] == "partial"
    assert collaboration["phase"] == "closed"
    assert collaboration["closure"]["outcome"] == "partial"
    assert collaboration["controls"] == {
        "can_takeover": False,
        "can_stop": False,
        "can_pause": False,
    }
    assert [
        member["status"] for member in collaboration["members"]
    ] == ["completed", "cancelled"]
    assert collaboration["members"][1]["error_code"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_failure_type",
    [RuntimeError, asyncio.CancelledError],
    ids=["native-error", "user-cancellation"],
)
async def test_query_handler_native_failure_does_not_replay_through_legacy(
    tmp_path,
    monkeypatch,
    runtime_failure_type,
) -> None:
    events: list[str] = []
    runtime_inputs = []
    added_messages: list[Msg] = []
    cancelled_roots: list[str] = []
    runtime_failure = runtime_failure_type("native boot failed")

    async def cancel_pending_approvals(root_session_id: str) -> int:
        cancelled_roots.append(root_session_id)
        return 1

    monkeypatch.setattr(
        approvals_service_module,
        "get_approval_service",
        lambda: SimpleNamespace(
            cancel_all_pending_by_root_session=cancel_pending_approvals,
        ),
    )

    class FakeMemory:
        def __init__(self) -> None:
            self.content = [
                (Msg("user", "历史用户", "user"), None),
                (Msg("Friday", "历史回答", "assistant"), None),
            ]

        async def add(self, msg: Msg) -> None:
            events.append(f"memory_add:{msg.role}:{_first_text(msg)}")
            added_messages.append(msg)
            self.content.append((msg, None))

    class FakeAgent:
        def __init__(self, **_kwargs) -> None:
            events.append("create_agent")
            self.toolkit = SimpleNamespace(skills={})
            self.memory = FakeMemory()

        async def register_mcp_clients(self) -> None:
            events.append("register_mcp")

        def rebuild_sys_prompt(self) -> None:
            events.append("rebuild_sys_prompt")

        def set_msg_queue_enabled(self, enabled: bool, queue: asyncio.Queue) -> None:
            assert enabled is True
            self.queue = queue

        async def __call__(self, messages) -> Msg:
            for message in messages:
                await self.memory.add(message)
            response = Msg("Friday", "legacy fallback final", "assistant")
            await self.memory.add(response)
            return response

    class FailingNativeRuntime:
        label = "native"

        def __init__(self, **_kwargs) -> None:
            events.append("create_native")

        async def run_turn(self, runtime_input):
            runtime_inputs.append(runtime_input)
            yield RuntimeEvent(
                id="evt-native-started",
                seq=1,
                type="turn.started",
                status="running",
                source="user",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )
            raise runtime_failure

    monkeypatch.setattr(runner_module, "_is_command", lambda query: False)
    monkeypatch.setattr(runner_module, "LegacyLantaiAgent", FakeAgent)
    monkeypatch.setattr(runner_module, "NativeLantaiRuntime", FailingNativeRuntime)
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        lambda self, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(
            id=agent_id,
            plan=SimpleNamespace(enabled=False),
            runtime=SimpleNamespace(engine="native"),
            running=SimpleNamespace(max_iters=5),
        ),
    )

    async def no_mission_command(**_kwargs):
        return None

    monkeypatch.setattr(
        runner_module,
        "maybe_handle_mission_command",
        no_mission_command,
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    runner.session = _FakeSession(events)
    chat_manager = _FakeChatManager(events)
    runner.set_chat_manager(chat_manager)
    request = SimpleNamespace(
        session_id="s-standard",
        user_id="u-1",
        channel="console",
        channel_meta={
            "chat_id": "chat-prefallback",
            "run_id": "run-prefallback",
            "workspace_dir": str(tmp_path / "topic-workspace"),
        },
        root_session_id="root-prefallback",
    )

    expected_exception = (
        asyncio.CancelledError
        if runtime_failure_type is asyncio.CancelledError
        else UnknownAgentException
    )
    with pytest.raises(expected_exception, match="native boot failed") as caught:
        _ = [
            (_first_text(msg), last)
            async for msg, last in runner.query_handler(
                [_RuntimeUserMsg("现在问题")],
                request,
            )
        ]

    assert [message.text_content() for message in runtime_inputs[0].messages] == [
        "历史用户",
        "历史回答",
        "现在问题",
    ]
    assert added_messages == []
    assert len(runner.session.loaded) == 1
    assert len(runner.session.saved) == 1
    assert events[-2:] == ["save_session", "touch_chat"]
    assert chat_manager.touched == ["chat-prefallback"]
    if runtime_failure_type is asyncio.CancelledError:
        assert caught.value is runtime_failure
        assert cancelled_roots == ["root-prefallback"]
    else:
        assert cancelled_roots == []


@pytest.mark.asyncio
async def test_query_handler_mission_path_uses_runtime_runner_streamer(
    tmp_path,
    monkeypatch,
) -> None:
    import lantai.agents.mission.mission_runner as mission_runner_module

    events: list[str] = []
    runtime_inputs = []
    runtime_adapter_labels: list[str] = []
    registry_request_contexts: list[dict] = []

    class FakeAgent:
        def __init__(self, **_kwargs) -> None:
            events.append("create_agent")
            self.toolkit = SimpleNamespace(skills={})

        async def register_mcp_clients(self) -> None:
            events.append("register_mcp")

        def rebuild_sys_prompt(self) -> None:
            events.append("rebuild_sys_prompt")

    class FakeRuntimeRunner:
        def __init__(self, adapter) -> None:
            events.append("create_runtime_runner")
            runtime_adapter_labels.append(getattr(adapter, "label", ""))

        async def run_turn(self, runtime_input):
            runtime_inputs.append(runtime_input)
            yield RuntimeEvent(
                id="evt-mission",
                type="message.completed",
                status="completed",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
                payload={
                    "message": RuntimeMessage.text(
                        "assistant",
                        "mission runtime final",
                        name="Friday",
                    ).model_dump(mode="json"),
                },
            )
            yield RuntimeEvent(
                id="evt-mission-turn",
                type="turn.completed",
                status="completed",
                source="system",
                session_id=runtime_input.request.session_id,
                turn_id=runtime_input.request.run_id,
            )

    async def mission_command(**_kwargs):
        return {
            "mission_phase": 2,
            "loop_dir": str(tmp_path),
            "max_iterations": 1,
        }

    async def fake_run_mission_phase2(**kwargs):
        assert callable(kwargs["stream_turn"])
        assert kwargs["agent_id"] == "default"
        async for item in kwargs["stream_turn"](
            [Msg("user", "mission inner turn", "user")],
        ):
            yield item

    monkeypatch.setattr(runner_module, "_is_command", lambda query: False)
    monkeypatch.setattr(runner_module, "LegacyLantaiAgent", FakeAgent)
    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(
        runner_module,
        "NativeLantaiRuntime",
        lambda **_kwargs: SimpleNamespace(label="native"),
    )
    def fake_build_native_tool_registry(self, **kwargs):
        registry_request_contexts.append(dict(kwargs["request_context"]))
        return SimpleNamespace()

    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        fake_build_native_tool_registry,
    )
    monkeypatch.setattr(
        runner_module,
        "build_tool_policy_context",
        lambda **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(
            id=agent_id,
            plan=SimpleNamespace(enabled=False),
            runtime=SimpleNamespace(engine="native"),
            running=SimpleNamespace(max_iters=5),
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "maybe_handle_mission_command",
        mission_command,
    )
    monkeypatch.setattr(
        mission_runner_module,
        "run_mission_phase2",
        fake_run_mission_phase2,
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    runner.session = _FakeSession(events)
    request = SimpleNamespace(
        session_id="s-mission",
        user_id="u-1",
        channel="console",
        channel_meta={"run_id": "run-mission"},
        root_session_id="root-mission",
    )

    output = [
        (_first_text(msg), last)
        async for msg, last in runner.query_handler(
            [_RuntimeUserMsg("开始 mission")],
            request,
        )
    ]

    assert output == [("mission runtime final", False), ("", True)]
    assert "create_runtime_runner" in events
    assert runtime_adapter_labels == ["native"]
    assert runtime_inputs[0].request.session_id == "s-mission"
    assert runtime_inputs[0].request.root_session_id == "root-mission"
    assert runtime_inputs[0].messages[-1].text_content() == "mission inner turn"
    assert registry_request_contexts[0]["mission_phase"] == 2
    assert set(registry_request_contexts[0]["runtime_blocked_tool_names"]) == {
        "edit_file",
        "browser_use",
        "desktop_screenshot",
    }


@pytest.mark.asyncio
async def test_query_handler_content_filter_returns_fallback_and_saves_state(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []
    recent_tool = {
        "type": "tool_result",
        "id": "tool-risk",
        "output": [{"type": "text", "text": "raw risky content"}],
    }
    added_messages: list[Msg] = []

    class FakeMemory:
        content = [
            (Msg("user", "查热榜", "user"), None),
            (Msg("assistant", [recent_tool], "assistant"), None),
        ]

        async def add(self, msg: Msg) -> None:
            events.append("memory_add")
            added_messages.append(msg)

    class FakeAgent:
        memory = FakeMemory()

        def __init__(self, **_kwargs) -> None:
            events.append("create_agent")
            self.toolkit = SimpleNamespace(skills={})

        async def register_mcp_clients(self) -> None:
            events.append("register_mcp")

        def rebuild_sys_prompt(self) -> None:
            events.append("rebuild_sys_prompt")

    class FakeRuntimeRunner:
        def __init__(self, adapter) -> None:
            assert getattr(adapter, "label", "") == "native"

        async def run_turn(self, _runtime_input):
            raise RuntimeError("provider rejected prompt: content_filter")
            yield  # pragma: no cover

    monkeypatch.setattr(runner_module, "_is_command", lambda query: False)
    monkeypatch.setattr(runner_module, "LegacyLantaiAgent", FakeAgent)
    monkeypatch.setattr(runner_module, "RuntimeRunner", FakeRuntimeRunner)
    monkeypatch.setattr(
        runner_module,
        "NativeLantaiRuntime",
        lambda **_kwargs: SimpleNamespace(label="native"),
    )
    monkeypatch.setattr(
        AgentRunner,
        "_build_native_tool_registry",
        lambda self, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_tool_policy_context",
        lambda **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        runner_module,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(
            id=agent_id,
            plan=SimpleNamespace(enabled=False),
            runtime=SimpleNamespace(engine="legacy"),
            running=SimpleNamespace(max_iters=5),
        ),
    )

    async def no_mission_command(**_kwargs):
        return None

    monkeypatch.setattr(
        runner_module,
        "maybe_handle_mission_command",
        no_mission_command,
    )

    runner = AgentRunner(agent_id="default", workspace_dir=tmp_path)
    runner.session = _FakeSession(events)
    request = SimpleNamespace(
        session_id="s-filter",
        user_id="u-1",
        channel="console",
        channel_meta={},
        root_session_id="",
    )

    output = [
        (msg, last)
        async for msg, last in runner.query_handler(
            [_RuntimeUserMsg("查热榜")],
            request,
        )
    ]

    assert len(output) == 1
    fallback, last = output[0]
    assert last is True
    assert "模型供应商拒绝" in _first_text(fallback)
    assert added_messages == [fallback]
    assert "工具结果已收起" in recent_tool["output"][0]["text"]
    assert events == [
        "create_agent",
        "register_mcp",
        "load_session",
        "rebuild_sys_prompt",
        "memory_add",
        "save_session",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('plan_enabled', [False, True])
@pytest.mark.parametrize('error_type', [KeyError, TypeError, ValueError])
async def test_query_schema_failure_preserves_session_and_never_executes(
    tmp_path, monkeypatch, plan_enabled, error_type,
):
    import json
    from pathlib import Path
    from lantai.app.runner.session import SafeJSONSession

    events = []

    class InvalidAgent:
        def __init__(self, **kwargs):
            self.toolkit = SimpleNamespace(skills={})
            self.plan_notebook = kwargs.get('plan_notebook')

        async def register_mcp_clients(self):
            pass

        def load_state_dict(self, data):
            events.append('load')
            raise error_type('memory')

        def state_dict(self):
            events.append('save')
            return {'memory': []}

        def rebuild_sys_prompt(self):
            events.append('execute')
            raise AssertionError('failed history must never reach execution')

    async def no_mission(**kwargs):
        return None

    monkeypatch.setattr(runner_module, '_is_command', lambda query: False)
    monkeypatch.setattr(runner_module, 'LegacyLantaiAgent', InvalidAgent)
    monkeypatch.setattr(runner_module, 'maybe_handle_mission_command', no_mission)
    monkeypatch.setattr(runner_module, 'load_agent_config', lambda agent_id: SimpleNamespace(
        id=agent_id, plan=SimpleNamespace(enabled=plan_enabled),
        runtime=SimpleNamespace(engine='native'), running=SimpleNamespace(max_iters=5),
    ))
    runner = AgentRunner(agent_id='default', workspace_dir=tmp_path)
    runner.session = SafeJSONSession(str(tmp_path / 'sessions'))
    path = Path(runner.session._get_save_path('schema-session', 'u', 'console'))
    original = json.dumps({'agent': {'_sys_prompt': 'private legacy prompt', 'history': ['keep']}}).encode()
    path.write_bytes(original)
    request = SimpleNamespace(session_id='schema-session', user_id='u', channel='console',
        channel_meta={}, metadata={'presentation_context': 'subagent_internal'})
    from lantai.exceptions import AgentStateError
    with pytest.raises(AgentStateError, match="原始记录已保留"):
        _ = [item async for item in runner.query_handler([_RuntimeUserMsg('continue')], request)]
    assert events == ['load']
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize('plan_enabled', [False, True])
@pytest.mark.parametrize('failure_kind', [
    'timeout', 'conversion', 'cancel', 'timeout_empty', 'timeout_error',
])
async def test_query_memory_timeout_preserves_session_and_never_executes(
    tmp_path, monkeypatch, plan_enabled, failure_kind,
):
    import json
    from pathlib import Path
    from lantai.app.runner.session import SafeJSONSession

    events = []

    reading = asyncio.Event()

    class SlowMemory:
        async def get_memory(self, **kwargs):
            reading.set()
            if failure_kind == 'conversion':
                return [object()]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if failure_kind == 'timeout_empty':
                    return []
                if failure_kind == 'timeout_error':
                    raise RuntimeError('cleanup failed')
                raise

    class InvalidAgent:
        def __init__(self, **kwargs):
            self.memory = SlowMemory()
            self.toolkit = SimpleNamespace(skills={})
            self.plan_notebook = kwargs.get('plan_notebook')

        async def register_mcp_clients(self):
            pass

        def load_state_dict(self, data):
            events.append('load')


        def state_dict(self):
            events.append('save')
            return {'memory': []}

        def rebuild_sys_prompt(self):
            events.append('rebuild')

    async def no_mission(**kwargs):
        return None

    monkeypatch.setattr(runner_module, '_is_command', lambda query: False)
    monkeypatch.setattr(runner_module, 'LegacyLantaiAgent', InvalidAgent)
    monkeypatch.setattr(runner_module, 'maybe_handle_mission_command', no_mission)
    monkeypatch.setattr(runner_module, 'load_agent_config', lambda agent_id: SimpleNamespace(
        id=agent_id, plan=SimpleNamespace(enabled=plan_enabled),
        runtime=SimpleNamespace(engine='native'), running=SimpleNamespace(max_iters=5),
    ))
    async def memory_then_execute(self, *, agent, **kwargs):
        await self._runtime_messages_from_agent_memory(agent)
        events.append('execute')
        yield None, True

    monkeypatch.setattr(AgentRunner, '_stream_standard_runtime_turn', memory_then_execute)
    monkeypatch.setattr(runner_module, '_FOREGROUND_MEMORY_LOAD_TIMEOUT_SECONDS', 0.01)
    runner = AgentRunner(agent_id='default', workspace_dir=tmp_path)
    runner.session = SafeJSONSession(str(tmp_path / 'sessions'))
    path = Path(runner.session._get_save_path('schema-session', 'u', 'console'))
    original = json.dumps({'agent': {'history': ['keep']}}).encode()
    path.write_bytes(original)
    request = SimpleNamespace(session_id='schema-session', user_id='u', channel='console',
        channel_meta={}, metadata={'presentation_context': 'subagent_internal'})
    from lantai.exceptions import AgentStateError
    async def consume():
        return [item async for item in runner.query_handler([_RuntimeUserMsg('continue')], request)]

    if failure_kind == 'cancel':
        monkeypatch.setattr(runner_module, '_FOREGROUND_MEMORY_LOAD_TIMEOUT_SECONDS', 60)
        task = asyncio.create_task(consume())
        await asyncio.wait_for(reading.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(AgentStateError, match="历史记录.*本轮已停止"):
            await consume()
    assert events == ['load', 'rebuild']
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize('server_binding', [False, True])
@pytest.mark.parametrize('cleanup_failure', [False, True])
async def test_research_chat_binding_uses_server_metadata_only(tmp_path, monkeypatch, server_binding, cleanup_failure):
    from lantai.research.projects import ResearchService
    from lantai.research.context import current_binding
    from lantai.config.context import WorkspaceContext
    from lantai.app import workspace_context as workspace_module
    monkeypatch.setattr(workspace_module, 'resolve_workspace_context', lambda **kwargs:
        WorkspaceContext(tmp_path, tmp_path / 'topic-state', tmp_path / 'external-project'))
    events, passed = [], []
    project = ResearchService(tmp_path).create_project('q', [], idempotency_key='p')
    class FakeAgent:
        def __init__(self, **kwargs):
            self.toolkit = SimpleNamespace(skills={})
        async def register_mcp_clients(self):
            pass
        def rebuild_sys_prompt(self):
            pass
    class Chats(_FakeChatManager):
        async def get_or_create_chat(self, *args, **kwargs):
            return SimpleNamespace(id='research-chat', session_id='s-runtime', user_id='u-1', channel='console',
                meta={'session_state_id':'console:chat:research-chat', **({'research_project_id': project['project_id']} if server_binding else {})})
    async def no_mission(**kwargs):
        return None
    async def seam(**kwargs):
        passed.append(kwargs.get('research_binding'))
        assert current_binding() is passed[-1]
        if False:
            yield None
    monkeypatch.setattr(runner_module, '_is_command', lambda query: False)
    monkeypatch.setattr(runner_module, 'LegacyLantaiAgent', FakeAgent)
    monkeypatch.setattr(runner_module, 'load_agent_config', lambda _: SimpleNamespace(
        id='default', plan=SimpleNamespace(enabled=False), runtime=SimpleNamespace(engine='native')))
    monkeypatch.setattr(runner_module, 'maybe_handle_mission_command', no_mission)
    runner = AgentRunner(agent_id='default', workspace_dir=tmp_path)
    runner.session = _FakeSession(events)
    runner.set_chat_manager(Chats(events))
    if cleanup_failure:
        async def failed_touch(_chat_id):
            raise RuntimeError('cleanup failed')
        monkeypatch.setattr(runner._chat_manager, 'touch_chat', failed_touch)
    monkeypatch.setattr(runner, '_stream_standard_runtime_turn', seam)
    request = SimpleNamespace(session_id='s-runtime', user_id='u-1', channel='console',
        channel_meta={'research_project_id':'forged-project', 'run_id':'research-test'}, root_session_id='s-runtime')
    if cleanup_failure:
        with pytest.raises(RuntimeError, match='cleanup failed'):
            _ = [item async for item in runner.query_handler([_RuntimeUserMsg('research')], request)]
    else:
        _ = [item async for item in runner.query_handler([_RuntimeUserMsg('research')], request)]
    assert current_binding() is None
    assert len(passed) == 1
    assert (passed[0].project if passed[0] else None) == (project['project_id'] if server_binding else None)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['completed', 'failed', 'cancelled', 'exhausted'])
async def test_recovered_research_request_preserves_durable_budget(
    tmp_path, monkeypatch, outcome,
):
    """Recovery request + real Runner admission reuse the persisted project ledger.

    Task scheduling and the model are local doubles; this is not a provider or
    delivery-quality test. The recovery request builder, ownership validation,
    binding, reservation and SQLite settlement are production implementations.
    """
    import hashlib
    import json
    from lantai.app.recovery_coordinator import RecoveryCoordinator
    from lantai.research.chat import bind_chat
    from lantai.research.context import current_binding
    from lantai.research.projects import ResearchService
    from lantai.runtime.models import ModelRequest, ModelResponse, ModelMessage, ModelUsage
    from lantai.providers import ProviderManager

    monkeypatch.setattr(ProviderManager, 'get_instance', lambda: SimpleNamespace(
        get_provider=lambda _: None))
    service = ResearchService(tmp_path)
    project = service.create_project('recovery', [], idempotency_key='recovery',
                                     budget={'model_calls': 1})
    chat = SimpleNamespace(id='research-recovery-chat', session_id='s-recovery',
        user_id='u-1', channel='console', meta={'research_project_id': project['project_id'],
            'session_state_id': 'console:chat:research-recovery-chat'})
    dispatched = []

    class Client:
        provider_id = 'local-test-provider'
        model_name = 'recovery-test-model'
        async def complete(self, request):
            dispatched.append(request.generation)
            if len(dispatched) > 1:
                if outcome == 'failed':
                    raise RuntimeError('controlled recovery failure')
                if outcome == 'cancelled':
                    raise asyncio.CancelledError()
            return ModelResponse(message=ModelMessage.text('assistant', 'PRIVATE-REPLY'),
                                 usage=ModelUsage(total_tokens=7))

    raw = Client()
    await bind_chat(chat, tmp_path).wrap_native(raw).complete(ModelRequest())
    if outcome != 'exhausted':
        service.revise_project(project['project_id'], 1, budget={'model_calls': 2},
                               idempotency_key='raise-after-restart')
    events, requests = [], []

    class Chats(_FakeChatManager):
        async def get_chat(self, chat_id):
            assert chat_id == chat.id
            return chat
        async def merge_chat_meta(self, chat_id, values, remove_keys=()):
            assert chat_id == chat.id
            chat.meta.update(values)
            for key in remove_keys:
                chat.meta.pop(key, None)
            return chat

    class Tracker:
        async def attach_or_start(self, chat_id, request, stream, **kwargs):
            requests.append(request)
            return None, False

    class FakeAgent:
        def __init__(self, **kwargs):
            self.toolkit = SimpleNamespace(skills={})
        async def register_mcp_clients(self):
            pass
        def rebuild_sys_prompt(self):
            pass

    async def no_mission(**kwargs):
        return None

    async def seam(**kwargs):
        binding = kwargs['research_binding']
        assert current_binding() is binding
        assert binding.project == project['project_id']
        await binding.wrap_native(raw).complete(ModelRequest(
            messages=[ModelMessage.text('user', 'PRIVATE-PROMPT')],
            generation={'temperature': 0.25}))
        if False:
            yield None

    monkeypatch.setattr(runner_module, '_is_command', lambda query: False)
    monkeypatch.setattr(runner_module, 'LegacyLantaiAgent', FakeAgent)
    monkeypatch.setattr(runner_module, 'load_agent_config', lambda _: SimpleNamespace(
        id='default', plan=SimpleNamespace(enabled=False), runtime=SimpleNamespace(engine='native')))
    monkeypatch.setattr(runner_module, 'maybe_handle_mission_command', no_mission)
    monkeypatch.setattr(runner_module, 'write_query_error_dump', lambda **kwargs: None)
    runner = AgentRunner(agent_id='default', workspace_dir=tmp_path)
    runner.session = _FakeSession(events)
    chats = Chats(events)
    runner.set_chat_manager(chats)
    monkeypatch.setattr(runner, '_stream_standard_runtime_turn', seam)
    workspace = SimpleNamespace(workspace_dir=tmp_path, agent_id='default',
        chat_manager=chats, runner=runner, task_tracker=Tracker())
    contract = SimpleNamespace(run_id='research-recovered-run', attempt_id='resume-1',
        task_contract=SimpleNamespace(objective='resume research', work_method_id='standard',
                                      client_turn_id='original-turn'))
    await RecoveryCoordinator(workspace)._dispatch(chat, contract)
    assert len(requests) == 1
    request = requests[0]
    assert request.channel_meta['recovery_reason'] == 'resume'
    # Untrusted metadata must never override the persisted association.
    request.channel_meta['research_project_id'] = 'forged-project'
    if outcome == 'cancelled':
        with pytest.raises(asyncio.CancelledError):
            _ = [event async for event in runner.stream_query(request)]
    elif outcome == 'failed':
        with pytest.raises(Exception, match='controlled recovery failure'):
            _ = [event async for event in runner.stream_query(request)]
    elif outcome == 'exhausted':
        with pytest.raises(Exception) as failure:
            _ = [event async for event in runner.stream_query(request)]
        assert isinstance(failure.value.__cause__, ValueError)
        assert 'budget exhausted' in str(failure.value.__cause__)
    else:
        _ = [event async for event in runner.stream_query(request)]
    assert current_binding() is None
    reopened = ResearchService(tmp_path)
    with reopened.store.transaction() as db:
        rows = db.execute('SELECT * FROM research_model_calls ORDER BY started,id').fetchall()
    expected_calls = 1 if outcome == 'exhausted' else 2
    assert len(rows) == len(dispatched) == expected_calls
    assert all(row['project'] == project['project_id'] for row in rows)
    assert all(row['surface'] == 'main_chat' for row in rows)
    assert all(row['finished'] is not None and row['seconds'] >= 0 for row in rows)
    assert all(row['config_hash'] == hashlib.sha256(row['config'].encode()).hexdigest()
               for row in rows)
    if outcome != 'exhausted':
        assert [row['revision'] for row in rows] == [1, 2]
        assert rows[-1]['status'] == ('completed' if outcome == 'completed' else 'interrupted')
        assert json.loads(rows[-1]['config'])['generation'] == {'temperature': 0.25}
        assert rows[0]['config_hash'] != rows[1]['config_hash']
    summary = reopened.project(project['project_id'])['inference']
    assert summary['calls'] == expected_calls
    assert summary['known_tokens'] == (14 if outcome == 'completed' else 7)
    assert summary['unknown_token_calls'] == int(outcome in {'failed', 'cancelled'})
    assert summary['unknown_cost_calls'] == expected_calls
    assert summary['known_cost_cny'] is None
    assert b'PRIVATE-PROMPT' not in reopened.store.path.read_bytes()
    assert b'PRIVATE-REPLY' not in reopened.store.path.read_bytes()
