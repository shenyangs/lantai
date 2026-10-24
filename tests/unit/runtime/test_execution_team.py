from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from lantai.runtime.execution_team import (
    ExecutionArtifactContract,
    ExecutionResourceBusy,
    ExecutionResourceLeaseStore,
    ExecutionResourceOwnershipLost,
    ExecutionTeamStore,
    build_execution_team_contract,
    infer_execution_resource_claims,
    verify_execution_artifacts,
)


def _tasks() -> list[dict]:
    return [
        {
            "to_agent": "researcher",
            "label": "资料核验",
            "text": "查证资料并写入证据清单。",
            "allowed_tools": ["web_search", "write_file"],
            "expected_artifacts": [
                {
                    "path": "evidence/sources.json",
                    "media_type": "application/json",
                    "minimum_bytes": 2,
                },
            ],
        },
        {
            "to_agent": "designer",
            "label": "图形交付",
            "text": "根据证据生成图形。",
            "depends_on": ["researcher"],
            "allowed_tools": ["read_file", "generate_image"],
            "expected_artifacts": [
                {
                    "path": "figures/result.png",
                    "media_type": "image/png",
                    "minimum_bytes": 8,
                },
            ],
        },
    ]


def _execution_identity() -> dict[str, str]:
    return {
        "root_run_id": "run-root",
        "parent_run_id": "run-parent",
        "parent_attempt_id": "attempt-parent",
        "parent_effect_id": "effect-parent",
    }


def test_execution_contract_is_scoped_and_keeps_private_fields_private() -> None:
    contract = build_execution_team_contract(
        contract_id="exec-team-1",
        **_execution_identity(),
        goal="交付一份有证据的图形",
        why_parallel="查证和制图可分工",
        tasks=_tasks(),
        acceptance_criteria=["证据可追溯", "图形文件存在"],
    )

    assert contract.mode == "execution"
    assert contract.effective_permissions == "scoped_execute"
    assert contract.synthesis.owner == "parent_agent"
    assert contract.round_policy.allow_nested_collaboration is False
    assert contract.parent_run_id == "run-parent"
    assert contract.root_run_id == "run-root"
    assert contract.parent_attempt_id == "attempt-parent"
    assert contract.parent_effect_id == "effect-parent"
    assert contract.members[0].task_id == "exec-team-1:member-1"
    assert contract.budget.max_member_attempts == 4
    assert contract.budget.reserved_host_seconds > 0
    assert contract.members[0].workspace_write_access is True
    assert contract.members[0].external_effect_policy == "inherit_runtime_policy"
    public = contract.public_dump()
    assert public["members"][0]["allowed_tools"] == [
        "web_search",
        "write_file",
    ]
    assert "assignment" not in public["members"][0]
    assert "workspace_root" not in json.dumps(public)


def test_execution_contract_rejects_artifact_floor_above_root_budget() -> None:
    with pytest.raises(ValueError, match="artifact budget"):
        build_execution_team_contract(
            contract_id="exec-budget-invalid",
            **_execution_identity(),
            goal="交付",
            why_parallel="分工",
            tasks=[
                {
                    **_tasks()[0],
                    "expected_artifacts": [
                        {"path": "huge.bin", "minimum_bytes": 11},
                    ],
                }
            ],
            max_artifact_bytes=10,
        )


def test_long_team_id_keeps_member_task_identities_distinct_and_bounded() -> None:
    contract = build_execution_team_contract(
        contract_id="t" * 160,
        **_execution_identity(),
        goal="交付",
        why_parallel="分工",
        tasks=_tasks(),
    )

    task_ids = [member.task_id for member in contract.members]
    assert len(set(task_ids)) == 2
    assert all(len(task_id) <= 160 for task_id in task_ids)
    assert ":member-1:" in task_ids[0]
    assert ":member-2:" in task_ids[1]


def test_execution_resource_claims_are_explicit_and_inferred() -> None:
    assert infer_execution_resource_claims(
        ["read_file", "browser_use", "mac_act"],
        ["service:4173", "browser"],
    ) == ["browser", "desktop-input", "service:4173"]


def test_execution_resource_lease_conflict_expiry_and_fencing(
    tmp_path: Path,
) -> None:
    store = ExecutionResourceLeaseStore(tmp_path)
    first = store.acquire(
        ["browser", "service:4173"],
        owner_id="team-a:member-1:attempt-1",
        ttl_seconds=10,
        now_epoch_seconds=100.0,
    )
    assert [item["resource"] for item in first] == [
        "browser",
        "service:4173",
    ]

    with pytest.raises(ExecutionResourceBusy, match="browser"):
        store.acquire(
            ["browser"],
            owner_id="team-b:member-1:attempt-1",
            ttl_seconds=10,
            now_epoch_seconds=105.0,
        )

    second = store.acquire(
        ["browser"],
        owner_id="team-b:member-1:attempt-1",
        ttl_seconds=10,
        now_epoch_seconds=111.0,
    )
    assert second[0]["generation"] == first[0]["generation"] + 1
    with pytest.raises(ExecutionResourceOwnershipLost):
        store.release(first, now_epoch_seconds=112.0)
    assert store.assert_ownership(second, now_epoch_seconds=112.0)
    renewed = store.renew(
        second,
        ttl_seconds=20,
        now_epoch_seconds=112.0,
    )
    assert renewed[0]["expires_at_epoch_seconds"] == 132.0
    store.release(renewed, now_epoch_seconds=112.0)


def test_execution_resource_wait_does_not_spin_model_or_steal_lease(
    tmp_path: Path,
) -> None:
    store = ExecutionResourceLeaseStore(tmp_path)
    held = store.acquire(
        ["desktop-input"],
        owner_id="holder",
        ttl_seconds=10,
        now_epoch_seconds=time.time(),
    )
    started = time.monotonic()
    with pytest.raises(ExecutionResourceBusy):
        store.wait_acquire(
            ["desktop-input"],
            owner_id="waiter",
            ttl_seconds=10,
            timeout_seconds=0.05,
            poll_interval_seconds=0.01,
        )
    assert time.monotonic() - started >= 0.04
    store.release(held)


def test_global_resource_authority_is_shared_across_chat_state_roots(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "lantai-home"
    first_chat_state = tmp_path / "chat-a"
    second_chat_state = tmp_path / "chat-b"
    first_chat_state.mkdir()
    second_chat_state.mkdir()

    first = ExecutionResourceLeaseStore.global_for_working_dir(working_dir)
    second = ExecutionResourceLeaseStore.global_for_working_dir(working_dir)
    assert first.root == second.root
    assert first.root != ExecutionResourceLeaseStore(first_chat_state).root
    assert first.root != ExecutionResourceLeaseStore(second_chat_state).root

    lease = first.acquire(
        ["browser", "desktop-input"],
        owner_id="chat-a:member-1",
        ttl_seconds=30,
        now_epoch_seconds=100.0,
    )
    with pytest.raises(ExecutionResourceBusy):
        second.acquire(
            ["browser"],
            owner_id="chat-b:member-1",
            ttl_seconds=30,
            now_epoch_seconds=101.0,
        )
    first.release(lease, now_epoch_seconds=101.0)


def test_execution_contract_allows_one_scoped_member() -> None:
    contract = build_execution_team_contract(
        contract_id="exec-team-single",
        **_execution_identity(),
        goal="交付一份证据清单",
        why_parallel="把可执行工作交给专门成员",
        tasks=[_tasks()[0]],
        acceptance_criteria=["证据可追溯"],
    )

    assert [member.agent_id for member in contract.members] == ["researcher"]


def test_execution_artifact_contract_rejects_path_escape_directly() -> None:
    with pytest.raises(ValueError, match="escape"):
        ExecutionArtifactContract(path="../escape.txt")


@pytest.mark.parametrize(
    "task_update",
    [
        {"allowed_tools": ["collaborate_with_agents"]},
        {"allowed_tools": []},
        {"expected_artifacts": [{"path": "../escape.txt"}]},
        {"expected_artifacts": [{"path": "/tmp/escape.txt"}]},
    ],
)
def test_execution_contract_rejects_nested_delegation_and_path_escape(
    task_update: dict,
) -> None:
    tasks = _tasks()
    tasks[0] = {**tasks[0], **task_update}
    with pytest.raises(ValueError):
        build_execution_team_contract(
            contract_id="exec-team-invalid",
            **_execution_identity(),
            goal="交付",
            why_parallel="分工",
            tasks=tasks,
        )


def test_artifact_verification_returns_content_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "evidence" / "sources.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"ok": true}', encoding="utf-8")

    verified = verify_execution_artifacts(
        workspace,
        [
            {
                "path": "evidence/sources.json",
                "media_type": "application/json",
                "minimum_bytes": 2,
            },
        ],
    )

    assert verified[0]["path"] == "evidence/sources.json"
    assert verified[0]["size_bytes"] == artifact.stat().st_size
    assert len(verified[0]["sha256"]) == 64


def test_artifact_verification_rejects_missing_small_and_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "small.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "linked.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="missing"):
        verify_execution_artifacts(workspace, [{"path": "missing.txt"}])
    with pytest.raises(ValueError, match="minimum"):
        verify_execution_artifacts(
            workspace,
            [{"path": "small.txt", "minimum_bytes": 2}],
        )
    with pytest.raises(ValueError, match="symlink"):
        verify_execution_artifacts(workspace, [{"path": "linked.txt"}])


def test_execution_store_is_atomic_checked_and_revision_fenced(
    tmp_path: Path,
) -> None:
    store = ExecutionTeamStore(tmp_path)
    first = store.save(
        "exec-team-1",
        {"phase": "working", "members": []},
        expected_revision=0,
    )
    assert first["store_revision"] == 1
    assert store.load("exec-team-1")["snapshot"]["phase"] == "working"

    with pytest.raises(ValueError, match="revision"):
        store.save(
            "exec-team-1",
            {"phase": "closed"},
            expected_revision=0,
        )

    path = store.snapshot_path("exec-team-1")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["snapshot"]["phase"] = "corrupted"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        store.load("exec-team-1")


def test_execution_store_serializes_same_team_writers(tmp_path: Path) -> None:
    store = ExecutionTeamStore(tmp_path)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    failures: list[BaseException] = []

    def hold_first_writer() -> None:
        try:
            with store._writer_lock("exec-team-1"):
                first_entered.set()
                assert release_first.wait(timeout=2)
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)

    def enter_second_writer() -> None:
        try:
            assert first_entered.wait(timeout=2)
            with store._writer_lock("exec-team-1"):
                second_entered.set()
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)

    first = threading.Thread(target=hold_first_writer)
    second = threading.Thread(target=enter_second_writer)
    first.start()
    second.start()

    assert first_entered.wait(timeout=2)
    assert second_entered.wait(timeout=0.1) is False
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert second_entered.is_set()
    assert failures == []
