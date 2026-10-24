# -*- coding: utf-8 -*-
"""HTTP control-plane locks for explicit long-horizon authority."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lantai.app.routers import runtime as runtime_router
from lantai.runtime.runs import (
    RuntimeBudget,
    RuntimeRunContract,
    RuntimeRunOwner,
    build_chat_task_contract,
)
from lantai.runtime.store import RuntimeRunStore


class _ChatManager:
    def __init__(self, chats) -> None:
        self._chats = list(chats)

    async def list_chats(self):
        return list(self._chats)


class _VerifiedProvider:
    def __init__(self, *, verified: bool = True) -> None:
        self.configuration_revision = 4
        self.connection_verified_revision = 4 if verified else None
        self.connection_verified_at = "2026-09-20T00:00:00+00:00" if verified else None
        self.model_verified_revision = 4 if verified else None
        self.model_verified_id = "model-a" if verified else None
        self.model_verified_at = "2026-09-20T00:00:00+00:00" if verified else None
        self._model = SimpleNamespace(
            supports_chat=True,
            supported_protocols=("openai:chat-completions",),
            context_window=128_000,
            capability_source="test-probe",
            probe_source="probed",
        )

    def get_model_info(self, model_id: str):
        return self._model if model_id == "model-a" else None


class _ProviderManager:
    def __init__(self, *, verified: bool = True) -> None:
        self.provider = _VerifiedProvider(verified=verified)

    def get_provider(self, provider_id: str):
        return self.provider if provider_id == "provider-a" else None

    def is_model_slot_available(self, slot) -> bool:
        return slot.provider_id == "provider-a" and slot.model == "model-a"


def _request(*, capabilities=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                runtime_autonomy_capabilities=capabilities or {},
            )
        )
    )


async def _install_runtime(monkeypatch, workspace_dir):
    run_id = "autonomy-run"
    attempt_id = f"{run_id}:attempt-1"
    chat = SimpleNamespace(
        id="chat-autonomy",
        session_id="console:chat-autonomy",
        user_id="default",
        channel="console",
        meta={"last_turn_run_id": run_id},
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=workspace_dir,
        chat_manager=_ChatManager([chat]),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)
    monkeypatch.setattr(
        runtime_router,
        "_runtime_autonomy_provider_manager",
        lambda: _ProviderManager(),
    )
    task = build_chat_task_contract(
        objective="prepare a verified overnight deliverable",
        client_turn_id="turn-autonomy",
        complexity=None,
        work_method_id="standard",
        run_id=run_id,
        attempt_id=attempt_id,
        acceptance=("deliverable reopens",),
    ).model_copy(update={"budget": RuntimeBudget(max_cost_points=10)})
    now = datetime.now(timezone.utc)
    contract = RuntimeRunContract(
        run_id=run_id,
        attempt_id=attempt_id,
        source="chat",
        owner=RuntimeRunOwner(
            user_id="default",
            agent_id="default",
            session_id=chat.session_id,
            workspace_id=str(workspace_dir),
            channel="console",
        ),
        status="running",
        revision=3,
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        budget=task.budget,
        task_contract=task,
        release_edition="official",
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )
    return contract, chat


def _grant_request(contract, chat, workspace_dir, **updates):
    request = runtime_router.RuntimeAutonomyGrantRequest(
        approved=True,
        expected_attempt_id=contract.attempt_id,
        expected_revision=contract.revision,
        chat_id=chat.id,
        workspace_dir=str(workspace_dir),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
        allowed_operations=(
            "automatic_resume",
            "model_call",
            "local_read",
            "local_write_new",
        ),
        allowed_paths=(str(workspace_dir),),
        allowed_provider_ids=("provider-a",),
        allowed_model_ids=("model-a",),
        allow_network=False,
        allow_gui=False,
        allow_external_delivery=False,
        auto_resume_conditions=("app_reopen", "network_recovery"),
        budget_currency="CNY",
        max_cost_micro_cny=10_000_000,
        model_price_bounds=(
            {
                "provider_id": "provider-a",
                "model_id": "model-a",
                "pricing_version": "provider-a-2026-09-20",
                "max_dispatch_micro_cny": 1_000_000,
                "upper_bound_proven": True,
            },
        ),
        reason="User enabled long-horizon execution for this task.",
    )
    return request.model_copy(update=updates)


@pytest.mark.asyncio
async def test_user_can_grant_then_revoke_task_scoped_autonomy(
    monkeypatch,
    tmp_path,
) -> None:
    contract, chat = await _install_runtime(monkeypatch, tmp_path)
    granted = await runtime_router.grant_runtime_autonomy(
        _request(),
        contract.run_id,
        _grant_request(contract, chat, tmp_path),
    )

    stored = await RuntimeRunStore(tmp_path).load_contract(contract.run_id)
    assert stored is not None
    assert stored.task_contract is not None
    active_grant = stored.task_contract.autonomy_grant
    assert active_grant is not None
    assert active_grant.chat_id == chat.id
    assert active_grant.workspace_id == str(tmp_path)
    assert active_grant.target_task_revision == stored.task_contract.revision
    assert active_grant.auto_resume_conditions == (
        "app_reopen",
        "network_recovery",
    )
    assert active_grant.budget_currency == "CNY"
    assert active_grant.max_cost_micro_cny == 10_000_000
    assert active_grant.model_price_bounds[0].model_id == "model-a"
    assert active_grant.preflight_checks
    assert all(check.status == "passed" for check in active_grant.preflight_checks)
    assert {
        "user_grant",
        "workspace_access",
        "disk_capacity",
        "provider_authentication",
        "model_protocol",
        "model_context",
    }.issubset({check.capability for check in active_grant.preflight_checks})
    assert active_grant.revocation_generation == 0
    assert granted["changed"] is True
    assert granted["autonomy_grant"]["approved"] is True

    revoked = await runtime_router.revoke_runtime_autonomy(
        SimpleNamespace(),
        contract.run_id,
        runtime_router.RuntimeAutonomyRevokeRequest(
            expected_attempt_id=stored.attempt_id,
            expected_revision=stored.revision,
            chat_id=chat.id,
            workspace_dir=str(tmp_path),
            reason="User stopped unattended execution.",
        ),
    )

    final = await RuntimeRunStore(tmp_path).load_contract(contract.run_id)
    assert final is not None
    assert final.task_contract is not None
    final_grant = final.task_contract.autonomy_grant
    assert final_grant is not None
    assert final_grant.revocation_generation == 1
    assert final_grant.revision == active_grant.revision + 1
    assert final_grant.revocation_reason == "User stopped unattended execution."
    assert revoked["changed"] is True
    assert revoked["autonomy_grant"]["revoked_at"] is not None


@pytest.mark.asyncio
async def test_grant_rejects_chat_that_does_not_own_the_run(
    monkeypatch,
    tmp_path,
) -> None:
    contract, _chat = await _install_runtime(monkeypatch, tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.grant_runtime_autonomy(
            SimpleNamespace(),
            contract.run_id,
            runtime_router.RuntimeAutonomyGrantRequest(
                approved=True,
                expected_attempt_id=contract.attempt_id,
                expected_revision=contract.revision,
                chat_id="chat-other",
                workspace_dir=str(tmp_path),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
                allowed_operations=("automatic_resume",),
                allowed_paths=(str(tmp_path),),
                auto_resume_conditions=("app_reopen",),
                reason="This chat does not own the run.",
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "runtime_autonomy_chat_mismatch"


@pytest.mark.asyncio
async def test_grant_blocks_unverified_provider_without_persisting_authority(
    monkeypatch,
    tmp_path,
) -> None:
    contract, chat = await _install_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime_router,
        "_runtime_autonomy_provider_manager",
        lambda: _ProviderManager(verified=False),
    )

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.grant_runtime_autonomy(
            _request(),
            contract.run_id,
            _grant_request(contract, chat, tmp_path),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "runtime_autonomy_preflight_failed"
    provider_check = next(
        check
        for check in exc_info.value.detail["checks"]
        if check["capability"] == "provider_authentication"
    )
    assert provider_check["status"] == "blocked"
    stored = await RuntimeRunStore(tmp_path).load_contract(contract.run_id)
    assert stored is not None
    assert stored.task_contract is not None
    assert stored.task_contract.autonomy_grant is None


@pytest.mark.asyncio
async def test_grant_blocks_low_disk_without_deleting_workspace_content(
    monkeypatch,
    tmp_path,
) -> None:
    contract, chat = await _install_runtime(monkeypatch, tmp_path)
    sentinel = tmp_path / "keep-me.txt"
    sentinel.write_text("user content", encoding="utf-8")
    monkeypatch.setattr(
        runtime_router,
        "_runtime_autonomy_disk_usage",
        lambda _path: SimpleNamespace(total=1024, used=1023, free=1),
    )

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.grant_runtime_autonomy(
            _request(),
            contract.run_id,
            _grant_request(contract, chat, tmp_path),
        )

    assert exc_info.value.detail["code"] == "runtime_autonomy_preflight_failed"
    disk_check = next(
        check
        for check in exc_info.value.detail["checks"]
        if check["capability"] == "disk_capacity"
    )
    assert disk_check["status"] == "blocked"
    assert sentinel.read_text(encoding="utf-8") == "user content"


@pytest.mark.asyncio
async def test_grant_blocks_gui_without_observed_permission_and_unlock(
    monkeypatch,
    tmp_path,
) -> None:
    contract, chat = await _install_runtime(monkeypatch, tmp_path)
    body = _grant_request(
        contract,
        chat,
        tmp_path,
        allowed_operations=("automatic_resume", "gui"),
        allowed_provider_ids=(),
        allowed_model_ids=(),
        budget_currency=None,
        max_cost_micro_cny=None,
        model_price_bounds=(),
        allow_gui=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.grant_runtime_autonomy(
            _request(),
            contract.run_id,
            body,
        )

    gui_check = next(
        check
        for check in exc_info.value.detail["checks"]
        if check["capability"] == "gui"
    )
    assert gui_check["status"] == "blocked"


@pytest.mark.asyncio
async def test_grant_blocks_required_renderer_without_server_evidence(
    monkeypatch,
    tmp_path,
) -> None:
    contract, chat = await _install_runtime(monkeypatch, tmp_path)
    body = _grant_request(
        contract,
        chat,
        tmp_path,
        required_capabilities=("renderer",),
    )

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.grant_runtime_autonomy(
            _request(),
            contract.run_id,
            body,
        )

    renderer_check = next(
        check
        for check in exc_info.value.detail["checks"]
        if check["capability"] == "renderer"
    )
    assert renderer_check["status"] == "blocked"
