# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pathlib import Path

import lantai.app.routers.browser_workbench as browser_workbench_router
from lantai.app.browser_workbench import (
    BrowserActionBroker,
    BrowserTaskPlanner,
    BrowserTaskRuntime,
    BrowserTaskStore,
    ComputerActionBroker,
    ElectronBrowserBridgeClient,
    BrowserProfileService,
    ContentWorkbenchStore,
    LantaiCliAdapterRegistry,
)
from lantai.app.browser_workbench.models import (
    AdapterRunRequest,
    BrowserAction,
    BrowserActionConfirmation,
    BrowserExportApproval,
    BrowserObservation,
    BrowserPolicyDecision,
    BrowserProfile,
    BrowserReplayEvent,
    BrowserTaskConfirmation,
    BrowserTaskEvent,
    BrowserTaskRequest,
    BrowserTaskRun,
    ComputerAction,
    ContentDossier,
    ContentSource,
    ContentWorkflowRun,
    DomainSkillDraft,
    DomainSkillDraftPatch,
    PublishingDraft,
    PublishingQueueItem,
)
from lantai.app.browser_workbench.replay import BrowserReplayStore
from lantai.app.capabilities.artifacts import artifact_manifest_path
from lantai.app.routers.browser_workbench import (
    DomainSkillSelectorProbeRequest,
    UrlResearchPackRunRequest,
    WechatEditorProbeRequest,
    manual_takeover_context_from_observation,
    probe_domain_skill_selectors,
    probe_wechat_editor_options,
    run_url_research_pack_workflow,
)


def _browser_profile_canonicalization_cases() -> list[dict[str, str]]:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "browser_profile_canonicalization.json"
    )
    return json.loads(fixture_path.read_text("utf-8"))


def _export_approval(reason: str = "test export approval") -> BrowserExportApproval:
    return BrowserExportApproval(
        approved=True,
        reason=reason,
        requested_by="unit-test",
    )


@pytest.fixture(autouse=True)
def force_official_browser_distribution(monkeypatch):
    monkeypatch.setattr(
        "lantai.app.capabilities.policy.is_app_store_distribution",
        lambda: False,
    )


def test_profile_id_canonicalization_matches_shared_fixture() -> None:
    service = BrowserProfileService()

    for case in _browser_profile_canonicalization_cases():
        assert service.normalize_profile_id(case["input"]) == case["canonical"]
        assert BrowserProfile(id=case["input"], name="fixture").id == case["canonical"]


def test_profile_service_persists_named_profiles_with_isolated_dirs(tmp_path):
    service = BrowserProfileService(tmp_path)

    default = service.get_profile("default")
    profile = service.upsert_profile(
        BrowserProfile(
            id="GitHub Work",
            name="GitHub Work",
            allowed_domains=["https://GitHub.com/org/repo", "api.github.com"],
            agent_control_enabled=True,
        )
    )

    assert default is not None
    assert default.id == "default"
    assert default.agent_control_enabled is False
    assert profile.id == "github-work"
    assert profile.allowed_domains == ["github.com", "api.github.com"]
    assert profile.user_data_dir != default.user_data_dir
    assert profile.user_data_dir.endswith(
        "browser_workbench/profiles/github-work/user_data"
    )

    reloaded = BrowserProfileService(tmp_path).get_profile("github-work")
    assert reloaded is not None
    assert reloaded.name == "GitHub Work"


def test_profile_service_rejects_payload_id_storage_key_mismatch(tmp_path):
    store_dir = tmp_path / "browser_workbench"
    store_dir.mkdir(parents=True)
    (store_dir / "profiles.json").write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "research": {
                        "id": "default",
                        "name": "Hijacked Default",
                        "allowed_domains": ["evil.example"],
                        "agent_control_enabled": True,
                    },
                    "default": {
                        "id": "research",
                        "name": "Hijacked Research",
                        "allowed_domains": ["example.com"],
                        "agent_control_enabled": True,
                    },
                },
            },
        ),
        "utf-8",
    )

    service = BrowserProfileService(tmp_path)

    default = service.get_profile("default")
    assert default is not None
    assert default.name == "Default"
    assert default.agent_control_enabled is False
    assert service.get_profile("research") is None


def test_profile_service_sanitizes_profile_metadata_at_write(tmp_path):
    service = BrowserProfileService(tmp_path)

    service.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            metadata={
                "api_key": "profile-secret",
                "nested": {"authorization": "Bearer secret"},
            },
        ),
    )

    reloaded = BrowserProfileService(tmp_path).require_profile("research")

    assert "api_key" not in reloaded.metadata
    assert reloaded.metadata["nested"]["authorization"]["redacted"] is True
    assert reloaded.metadata["data_classification"] == "sensitive"


@pytest.mark.asyncio
async def test_delete_browser_profile_clears_electron_and_local_storage(
    tmp_path,
    monkeypatch,
):
    profiles = BrowserProfileService(tmp_path)
    profile = profiles.upsert_profile(
        BrowserProfile(
            id="Research Work",
            name="Research Work",
            agent_control_enabled=True,
        )
    )
    profile_dir = Path(profile.user_data_dir).parent
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Cookies").write_text("cookie", "utf-8")

    class FakeCleanupBridge:
        available = True

        def __init__(self) -> None:
            self.actions: list[BrowserAction] = []

        async def execute(self, action: BrowserAction) -> dict[str, object]:
            self.actions.append(action)
            return {
                "ok": True,
                "profile_id": action.profile_id,
                "partition": f"persist:lantai-browser-{action.profile_id}",
            }

    bridge = FakeCleanupBridge()
    monkeypatch.setattr(browser_workbench_router, "_profiles", profiles)
    monkeypatch.setattr(
        browser_workbench_router,
        "_broker",
        type("FakeBroker", (), {"bridge": bridge})(),
    )

    result = await browser_workbench_router.delete_browser_profile("Research Work")

    assert result["removed"] is True
    assert result["profile_id"] == "research-work"
    assert result["electron_storage"] == {
        "ok": True,
        "profile_id": "research-work",
        "partition": "persist:lantai-browser-research-work",
    }
    assert result["local_storage_removed"] is True
    assert bridge.actions[0].action == "clear_profile_storage"
    assert bridge.actions[0].profile_id == "research-work"
    assert profiles.get_profile("research-work") is None
    assert not profile_dir.exists()


def test_broker_evaluates_profile_domain_and_write_confirmation(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="github-work",
            name="GitHub Work",
            allowed_domains=["github.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    read_decision = broker.evaluate(
        BrowserAction(
            action="open",
            profile_id="github-work",
            url="https://github.com/org/repo",
        )
    )
    blocked_decision = broker.evaluate(
        BrowserAction(
            action="open",
            profile_id="github-work",
            url="https://example.com",
        )
    )
    write_decision = broker.evaluate(
        BrowserAction(
            action="click",
            profile_id="github-work",
            selector="#submit",
        )
    )

    assert read_decision.decision == "allow"
    assert read_decision.access == "read"
    assert blocked_decision.decision == "block"
    assert blocked_decision.matched_rule_id == "browser_domain_not_allowed"
    assert write_decision.decision == "prompt"
    assert write_decision.requires_confirmation is True


@pytest.mark.asyncio
async def test_broker_find_returns_compact_snapshot_matches_without_confirmation(
    tmp_path,
):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )

    class SnapshotBridge:
        available = True

        def __init__(self) -> None:
            self.actions: list[BrowserAction] = []

        async def get_state(self) -> dict[str, object]:
            return {"ok": True, "agent_status": "running"}

        async def execute(self, action: BrowserAction) -> dict[str, object]:
            self.actions.append(action)
            return {
                "ok": True,
                "runtime": "electron_bridge",
                "page_id": "tab-research",
                "profile_id": "research",
                "url": "https://example.com/invoices",
                "data": {
                    "title": "Invoices",
                    "text": (
                        "Quarterly invoices are ready. "
                        "Invoice 2026-Q3 is awaiting review."
                    ),
                    "links": [
                        {
                            "text": "Open invoice 2026-Q3",
                            "href": "https://example.com/invoices/2026-q3",
                            "selector": "a[data-testid=invoice-q3]",
                        },
                        {
                            "text": "Archived invoice 2026-Q3",
                            "href": "https://example.com/invoices/archived",
                            "selector": "a[data-testid=archived-link]",
                        },
                    ],
                    "controls": [
                        {
                            "role": "button",
                            "name": "Review invoice 2026-Q3",
                            "selector": "button[data-testid=review]",
                            "sensitive": False,
                        },
                        {
                            "role": "textbox",
                            "name": "Invoice 2026-Q3 verification code",
                            "selector": "input[name=otp]",
                            "sensitive": True,
                        },
                        {
                            "role": "button",
                            "name": "Archived invoice 2026-Q3",
                            "selector": "button[data-testid=archived]",
                            "sensitive": False,
                            "visible": False,
                        },
                        {
                            "role": "link",
                            "name": "Archived invoice 2026-Q3",
                            "selector": "a[data-testid=archived-link]",
                            "sensitive": False,
                            "visible": False,
                        },
                    ],
                },
            }

    bridge = SnapshotBridge()
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=bridge,
    )

    observation = await broker.execute(
        BrowserAction(
            action="find",
            profile_id="research",
            page_id="tab-research",
            url="https://example.com/invoices",
            text="invoice 2026-q3",
            metadata={"max_results": 5, "context_chars": 48},
        )
    )

    assert observation.ok is True
    assert observation.action == "find"
    assert observation.decision is not None
    assert observation.decision.access == "read"
    assert observation.decision.requires_confirmation is False
    assert [action.action for action in bridge.actions] == ["snapshot"]
    assert observation.data["match_count"] == 3
    assert observation.data["matches"][0]["kind"] == "control"
    assert observation.data["matches"][0]["selector"] == (
        "button[data-testid=review]"
    )
    assert all(
        match.get("selector")
        not in {
            "input[name=otp]",
            "button[data-testid=archived]",
            "a[data-testid=archived-link]",
        }
        for match in observation.data["matches"]
    )
    assert "text" not in observation.data
    assert "controls" not in observation.data


def test_broker_does_not_allow_caller_to_downgrade_action_access(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="github-work",
            name="GitHub Work",
            allowed_domains=["github.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action="click",
            profile_id="github-work",
            url="https://github.com/org/repo",
            selector="#submit",
            access="read",
        )
    )

    assert decision.access == "write"
    assert decision.decision == "prompt"
    assert decision.requires_confirmation is True
    assert decision.matched_rule_id == "browser_write_confirmation_required"


def test_broker_allows_task_bound_read_navigation_but_not_side_effect_click(
    tmp_path,
):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )
    next_page = BrowserAction(
        action="click",
        profile_id="research",
        url="https://example.com/search",
        selector="#next-page",
        access="read",
        reason="Open the next page of search results",
    )
    like_button = BrowserAction(
        action="click",
        profile_id="research",
        url="https://example.com/post/1",
        selector="#like",
        access="read",
        reason="Like this post",
    )

    authorized = broker.authorize_read_task_interaction(
        next_page,
        task_access="read",
    )
    rejected_downgrade = broker.authorize_read_task_interaction(
        like_button,
        task_access="read",
    )

    assert "browser_read_task_token" in authorized.metadata
    assert broker.evaluate(authorized).decision == "allow"
    assert "browser_read_task_token" not in rejected_downgrade.metadata
    assert broker.evaluate(rejected_downgrade).decision == "prompt"


def test_broker_treats_page_down_as_read_navigation_without_confirmation(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action="press_key",
            profile_id="research",
            url="https://example.com/search",
            access="read",
            metadata={"key": "PageDown"},
        )
    )

    assert decision.decision == "allow"
    assert decision.access == "read"
    assert decision.requires_confirmation is False


@pytest.mark.parametrize("action", ["evaluate", "eval"])
def test_broker_blocks_raw_evaluate_without_registered_script(tmp_path, action):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action=action,
            profile_id="research",
            url="https://example.com/report",
            code="() => document.title",
            access="read",
        )
    )

    assert decision.access == "sensitive"
    assert decision.decision == "block"
    assert decision.matched_rule_id == "browser_evaluate_script_registry_required"


def test_broker_allows_registered_read_only_evaluate_script(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )
    action = BrowserAction(
        action="evaluate",
        profile_id="research",
        url="https://example.com/report",
        code="() => fetch('https://evil.example')",
        access="read",
        metadata={"safe_script_id": "browser.page_summary"},
    )

    decision = broker.evaluate(action)
    runtime_action = broker._with_safe_evaluate_code(action)

    assert decision.access == "read"
    assert decision.decision == "allow"
    assert "document.title" in runtime_action.code
    assert "fetch(" not in runtime_action.code


def test_broker_treats_network_body_access_as_sensitive(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action="network_get",
            profile_id="research",
            url="https://example.com/report",
            access="read",
        )
    )

    assert decision.access == "sensitive"
    assert decision.decision == "prompt"
    assert decision.requires_confirmation is True
    assert decision.matched_rule_id == "browser_sensitive_confirmation_required"


@pytest.mark.parametrize("action", ["upload_file", "file_upload"])
def test_broker_blocks_upload_file_without_file_grant(tmp_path, action):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action=action,
            profile_id="research",
            url="https://example.com/upload",
            path=str(tmp_path / "report.csv"),
            access="read",
        )
    )

    assert decision.access == "sensitive"
    assert decision.decision == "block"
    assert decision.matched_rule_id == "browser_upload_file_grant_required"


def _restored_security_scope_kwargs(label: str = "restored") -> dict[str, object]:
    return {
        "security_scoped_bookmark": True,
        "security_scope_ref": f"browser-security-scope:{label}",
        "security_scope_bookmark_sha256": f"sha256:{label}",
    }


def test_broker_rejects_upload_file_grant_bound_to_different_owner(tmp_path):
    upload_path = tmp_path / "report.csv"
    upload_path.write_text("report", "utf-8")
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    profiles.upsert_profile(
        BrowserProfile(
            id="other",
            name="Other",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )
    grant = broker.grant_upload_file(
        path=upload_path,
        profile_id="research",
        run_id="run-1",
        page_id="tab-1",
        url="https://example.com/upload",
        selector="input[type=file]",
        **_restored_security_scope_kwargs(),
    )

    decision = broker.evaluate(
        BrowserAction(
            action="upload_file",
            profile_id="other",
            run_id="run-1",
            page_id="tab-1",
            url="https://example.com/upload",
            selector="input[type=file]",
            path=str(upload_path),
            access="read",
            metadata={"file_grant_id": grant.id},
        )
    )

    assert decision.decision == "block"
    assert decision.matched_rule_id == "browser_file_grant_owner_mismatch"


def test_broker_upload_file_grant_carries_security_scope_reference(tmp_path):
    upload_path = tmp_path / "report.csv"
    upload_path.write_text("report", "utf-8")
    broker = BrowserActionBroker(replay=BrowserReplayStore(tmp_path))

    grant = broker.grant_upload_file(
        path=upload_path,
        profile_id="research",
        **_restored_security_scope_kwargs(),
    )

    assert grant.grant_ref == f"browser-file-grant:{grant.id}"
    assert grant.security_scope_ref == "browser-security-scope:restored"
    assert grant.security_scoped_bookmark is True
    assert grant.security_scope_bookmark_sha256 == "sha256:restored"
    assert grant.path_identity_hash.startswith("sha256:")
    assert not hasattr(grant, "bookmark_blob")


def test_app_store_upload_file_grant_requires_restored_bookmark(
    tmp_path,
    monkeypatch,
):
    upload_path = tmp_path / "report.csv"
    upload_path.write_text("report", "utf-8")
    broker = BrowserActionBroker(replay=BrowserReplayStore(tmp_path))
    monkeypatch.setattr(
        "lantai.app.browser_workbench.file_grants.is_app_store_distribution",
        lambda: True,
    )

    with pytest.raises(ValueError, match="security-scoped bookmark"):
        broker.grant_upload_file(path=upload_path, profile_id="research")

    grant = broker.grant_upload_file(
        path=upload_path,
        profile_id="research",
        **_restored_security_scope_kwargs(),
    )

    assert grant.security_scoped_bookmark is True
    assert grant.security_scope_ref == "browser-security-scope:restored"


def test_broker_rejects_upload_file_grant_after_path_identity_changes(tmp_path):
    upload_path = tmp_path / "report.csv"
    upload_path.write_text("report", "utf-8")
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )
    grant = broker.grant_upload_file(
        path=upload_path,
        profile_id="research",
        run_id="run-1",
        page_id="tab-1",
        url="https://example.com/upload",
        selector="input[type=file]",
        **_restored_security_scope_kwargs(),
    )
    upload_path.write_text("different report", "utf-8")

    decision = broker.evaluate(
        BrowserAction(
            action="upload_file",
            profile_id="research",
            run_id="run-1",
            page_id="tab-1",
            url="https://example.com/upload",
            selector="input[type=file]",
            path=str(upload_path),
            access="read",
            metadata={"file_grant_id": grant.id},
        )
    )

    assert decision.decision == "block"
    assert decision.matched_rule_id == "browser_file_grant_identity_mismatch"


@pytest.mark.asyncio
async def test_broker_consumes_upload_file_grant_once(tmp_path, monkeypatch):
    upload_path = tmp_path / "report.csv"
    upload_path.write_text("report", "utf-8")
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    async def fake_execute_runtime(
        action: BrowserAction,
        runtime: str,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "runtime": runtime,
            "page_id": action.page_id,
            "url": action.url,
        }

    monkeypatch.setattr(broker, "_execute_runtime", fake_execute_runtime)
    grant = broker.grant_upload_file(
        path=upload_path,
        profile_id="research",
        run_id="run-1",
        page_id="tab-1",
        url="https://example.com/upload",
        selector="input[type=file]",
        **_restored_security_scope_kwargs(),
    )
    action = BrowserAction(
        action="upload_file",
        profile_id="research",
        run_id="run-1",
        page_id="tab-1",
        url="https://example.com/upload",
        selector="input[type=file]",
        path=str(upload_path),
        access="read",
        metadata={"file_grant_id": grant.id},
    )

    first = await broker.execute(broker.confirm_action(action, reason="upload once"))
    second = await broker.execute(
        broker.confirm_action(action, reason="replay upload"),
    )

    assert first.ok is True
    assert second.ok is False
    assert second.decision is not None
    assert second.decision.matched_rule_id == "browser_file_grant_invalid"


def test_broker_rejects_upload_file_grant_for_symlink_path(tmp_path):
    upload_path = tmp_path / "report.csv"
    upload_path.write_text("report", "utf-8")
    link_path = tmp_path / "report-link.csv"
    link_path.symlink_to(upload_path)
    broker = BrowserActionBroker(
        replay=BrowserReplayStore(tmp_path),
    )

    with pytest.raises(ValueError):
        broker.grant_upload_file(
            path=link_path,
            profile_id="research",
            **_restored_security_scope_kwargs(),
        )


def test_broker_file_grant_lifecycle_revokes_matching_run_grants(tmp_path):
    upload_path = tmp_path / "report.csv"
    other_path = tmp_path / "other.csv"
    upload_path.write_text("report", "utf-8")
    other_path.write_text("other", "utf-8")
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )
    revoked_grant = broker.grant_upload_file(
        path=upload_path,
        profile_id="research",
        run_id="run-delete",
        page_id="tab-1",
        url="https://example.com/upload",
        selector="input[type=file]",
        **_restored_security_scope_kwargs("revoked"),
    )
    kept_grant = broker.grant_upload_file(
        path=other_path,
        profile_id="research",
        run_id="run-keep",
        page_id="tab-2",
        url="https://example.com/upload",
        selector="input[type=file]",
        **_restored_security_scope_kwargs("kept"),
    )

    report = broker.file_grant_lifecycle_report(
        operation="delete",
        dry_run=False,
        run_id="run-delete",
    )

    assert report["matched_count"] == 1
    assert report["revoked_count"] == 1
    assert report["remaining_count"] == 1
    assert "path" not in report["grants"][0]
    assert report["grants"][0]["path_sha256"].startswith("sha256:")
    assert broker.evaluate(
        BrowserAction(
            action="upload_file",
            profile_id="research",
            run_id="run-delete",
            page_id="tab-1",
            url="https://example.com/upload",
            selector="input[type=file]",
            path=str(upload_path),
            access="read",
            metadata={"file_grant_id": revoked_grant.id},
        )
    ).matched_rule_id == "browser_file_grant_invalid"
    assert broker.evaluate(
        BrowserAction(
            action="upload_file",
            profile_id="research",
            run_id="run-keep",
            page_id="tab-2",
            url="https://example.com/upload",
            selector="input[type=file]",
            path=str(other_path),
            access="read",
            metadata={"file_grant_id": kept_grant.id},
        )
    ).decision == "prompt"


def test_broker_file_grant_lifecycle_rejects_unscoped_destructive_delete(tmp_path):
    upload_path = tmp_path / "report.csv"
    other_path = tmp_path / "other.csv"
    upload_path.write_text("report", "utf-8")
    other_path.write_text("other", "utf-8")
    broker = BrowserActionBroker(replay=BrowserReplayStore(tmp_path))
    first = broker.grant_upload_file(
        path=upload_path,
        profile_id="research",
        run_id="run-1",
        **_restored_security_scope_kwargs("first"),
    )
    second = broker.grant_upload_file(
        path=other_path,
        profile_id="other",
        run_id="run-2",
        **_restored_security_scope_kwargs("second"),
    )

    with pytest.raises(ValueError, match="scope"):
        broker.file_grant_lifecycle_report(
            operation="delete",
            dry_run=False,
        )

    report = broker.file_grant_lifecycle_report(operation="archive", dry_run=True)
    assert report["matched_count"] == 2
    assert report["revoked_count"] == 0
    assert {grant["id"] for grant in report["grants"]} == {first.id, second.id}


@pytest.mark.parametrize(
    ("url", "rule_id"),
    [
        ("file:///Users/example/.ssh/id_rsa", "browser_url_scheme_blocked"),
        ("data:text/html,secret", "browser_url_scheme_blocked"),
        ("javascript:alert(1)", "browser_url_scheme_blocked"),
        ("http://127.0.0.1:3000/admin", "browser_private_network_url_blocked"),
        ("http://169.254.169.254/latest/meta-data", "browser_private_network_url_blocked"),
    ],
)
def test_broker_blocks_unsafe_url_targets(tmp_path, url, rule_id):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=[],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action="open",
            profile_id="research",
            url=url,
        )
    )

    assert decision.decision == "block"
    assert decision.matched_rule_id == rule_id


def test_broker_does_not_accept_unbound_confirmed_flag_as_authorization(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="github-work",
            name="GitHub Work",
            allowed_domains=["github.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action="click",
            profile_id="github-work",
            url="https://github.com/org/repo",
            selector="#submit",
            confirmed=True,
        )
    )

    assert decision.decision != "allow"


def test_broker_accepts_action_bound_confirmation_token(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="github-work",
            name="GitHub Work",
            allowed_domains=["github.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )
    action = BrowserAction(
        action="click",
        profile_id="github-work",
        url="https://github.com/org/repo",
        selector="#submit",
    )

    confirmed = broker.confirm_action(action, reason="user approved")
    decision = broker.evaluate(confirmed)

    assert decision.decision == "allow"


def test_broker_rejects_confirmation_token_for_modified_action(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="github-work",
            name="GitHub Work",
            allowed_domains=["github.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )
    action = BrowserAction(
        action="click",
        profile_id="github-work",
        url="https://github.com/org/repo",
        selector="#submit",
    )

    confirmed = broker.confirm_action(action, reason="user approved")
    tampered = confirmed.model_copy(update={"selector": "#danger"})
    decision = broker.evaluate(tampered)

    assert decision.decision == "prompt"
    assert decision.requires_confirmation is True


@pytest.mark.asyncio
async def test_broker_consumes_confirmation_token_once(tmp_path, monkeypatch):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="github-work",
            name="GitHub Work",
            allowed_domains=["github.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    async def fake_execute_runtime(
        action: BrowserAction,
        runtime: str,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "runtime": runtime,
            "page_id": action.page_id,
            "url": action.url,
        }

    monkeypatch.setattr(broker, "_execute_runtime", fake_execute_runtime)
    action = BrowserAction(
        action="click",
        profile_id="github-work",
        url="https://github.com/org/repo",
        selector="#submit",
    )
    confirmed = broker.confirm_action(action, reason="user approved")

    first = await broker.execute(confirmed)
    second = await broker.execute(confirmed)

    assert first.ok is True
    assert second.ok is False
    assert second.decision is not None
    assert second.decision.decision == "prompt"


@pytest.mark.asyncio
async def test_confirm_browser_action_route_uses_action_bound_token(
    tmp_path,
    monkeypatch,
):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="github-work",
            name="GitHub Work",
            allowed_domains=["github.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    async def fake_execute_runtime(
        action: BrowserAction,
        runtime: str,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "runtime": runtime,
            "page_id": action.page_id,
            "url": action.url,
        }

    monkeypatch.setattr(broker, "_execute_runtime", fake_execute_runtime)
    monkeypatch.setattr(browser_workbench_router, "_broker", broker)
    observation = await browser_workbench_router.confirm_browser_action(
        BrowserActionConfirmation(
            action=BrowserAction(
                action="click",
                profile_id="github-work",
                url="https://github.com/org/repo",
                selector="#submit",
            ),
            approved=True,
            reason="route approve",
        )
    )

    assert observation.ok is True
    assert observation.decision is not None
    assert observation.decision.decision == "allow"


@pytest.mark.asyncio
async def test_broker_records_replay_for_blocked_action(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="github-work",
            name="GitHub Work",
            allowed_domains=["github.com"],
            agent_control_enabled=True,
        )
    )
    replay = BrowserReplayStore(tmp_path)
    broker = BrowserActionBroker(profiles=profiles, replay=replay)

    observation = await broker.execute(
        BrowserAction(
            action="click",
            profile_id="github-work",
            selector="#submit",
            run_id="run-1",
            reason="test write confirmation",
        )
    )

    events = replay.list_events("run-1")
    assert observation.ok is False
    assert observation.decision is not None
    assert observation.decision.decision == "prompt"
    assert events[0]["action"] == "click"
    assert events[0]["decision"] == "prompt"
    assert events[0]["metadata"]["selector"] is True
    assert events[0]["metadata"]["selector_value"] == "#submit"
    assert events[0]["metadata"]["error_code"] == "browser_write_confirmation_required"


def test_electron_bridge_client_maps_unauthorized_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"ok":false,"error":"Unauthorized browser bridge request."}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ElectronBrowserBridgeClient(
        base_url="http://127.0.0.1:9",
        token="wrong-token",
    )

    payload = client._post_action(
        BrowserAction(
            action="open",
            profile_id="default",
            url="https://example.com",
        )
    )

    assert payload["ok"] is False
    assert payload["runtime"] == "electron_bridge"
    assert payload["http_status"] == 401
    assert payload["error_code"] == "electron_bridge_unauthorized"


def test_electron_bridge_client_uses_action_capability_header(monkeypatch):
    seen: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self) -> bytes:
            return b'{"ok":true,"page_id":"tab-1"}'

    def fake_urlopen(req, timeout):
        seen["headers"] = {
            key.lower(): value for key, value in req.header_items()
        }
        seen["body"] = req.data
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ElectronBrowserBridgeClient(
        base_url="http://127.0.0.1:9",
        token="root-token",
    )

    payload = client._post_action(
        BrowserAction(
            action="open",
            profile_id="default",
            url="https://example.com",
        )
    )

    headers = seen["headers"]
    capability = headers["x-lantai-browser-bridge-capability"]
    parts = capability.split(":")
    assert payload["ok"] is True
    assert "x-lantai-browser-bridge-token" not in headers
    assert parts[0] == "v1"
    assert parts[3] == hashlib.sha256(seen["body"]).hexdigest()


@pytest.mark.asyncio
async def test_electron_bridge_client_bounds_sync_request_timeout(monkeypatch):
    seen: dict[str, float] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self) -> bytes:
            return b'{"ok":true,"page_id":"tab-1"}'

    def fake_urlopen(req, timeout):
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ElectronBrowserBridgeClient(
        base_url="http://127.0.0.1:9",
        token="test-token",
        timeout_seconds=12.0,
    )

    payload = await client.execute(
        BrowserAction(
            action="open",
            profile_id="default",
            url="https://example.com",
            metadata={"browser_task_action_timeout_seconds": 0.25},
        )
    )

    assert payload["ok"] is True
    assert seen["timeout"] == pytest.approx(0.25)


def test_broker_observation_summary_keeps_frame_shadow_scope():
    summary = BrowserActionBroker._observation_summary(
        BrowserObservation(
            ok=True,
            action="fill_form",
            runtime="electron_bridge",
            profile_id="research",
            data={
                "data": {
                    "scope": {
                        "frame_selector": "iframe.editor",
                        "shadow_host_selector": "editor-shell",
                    },
                    "details": [
                        {
                            "selector": "#title",
                            "scope": {
                                "frame_selector": "iframe.editor",
                                "shadow_host_selector": "editor-shell",
                            },
                        }
                    ],
                }
            },
        )
    )

    assert summary["scope"] == {
        "frame_selector": "iframe.editor",
        "shadow_host_selector": "editor-shell",
    }
    assert summary["scoped_field_count"] == 1
    assert summary["field_scopes"][0] == {
        "selector": "#title",
        "frame_selector": "iframe.editor",
        "shadow_host_selector": "editor-shell",
    }


class FakeElectronBridge:
    available = True

    async def execute(self, action: BrowserAction):
        return {
            "ok": True,
            "runtime": "electron_bridge",
            "page_id": action.page_id,
            "profile_id": action.profile_id,
            "url": action.url or "about:blank",
            "message": "electron bridge action executed",
        }


class UnauthorizedElectronBridge(FakeElectronBridge):
    async def execute(self, action: BrowserAction):
        return {
            "ok": False,
            "runtime": "electron_bridge",
            "profile_id": action.profile_id,
            "url": action.url or "about:blank",
            "error": "Unauthorized browser bridge request.",
            "error_code": "electron_bridge_unauthorized",
            "http_status": 401,
        }


class PausedElectronBridge(FakeElectronBridge):
    async def get_state(self):
        return {"ok": True, "agent_status": "已暂停"}


class InvalidStatusElectronBridge(FakeElectronBridge):
    async def get_state(self):
        return {
            "ok": False,
            "error": "bridge returned an invalid status",
            "error_code": "",
            "http_status": "not-a-number",
        }


@pytest.mark.asyncio
async def test_browser_bridge_health_handles_nonnumeric_http_status(monkeypatch):
    monkeypatch.setattr(browser_workbench_router, "is_app_store_distribution", lambda: False)
    monkeypatch.setattr(
        browser_workbench_router,
        "_broker",
        type("FakeBroker", (), {"bridge": InvalidStatusElectronBridge()})(),
    )

    health = await browser_workbench_router._browser_bridge_health(
        {"primary_runtime": "electron_bridge"},
    )

    assert health.status == "disconnected"
    assert health.error_code == "electron_bridge_unavailable"


@pytest.mark.asyncio
async def test_browser_bridge_health_reports_browser_skill_runtime(monkeypatch):
    class FakeBrowserSkill:
        async def status(self):
            return {
                "ok": True,
                "status": "available",
                "message": "BrowserSkill connected",
            }

    monkeypatch.setattr(browser_workbench_router, "is_app_store_distribution", lambda: False)
    monkeypatch.setattr(
        browser_workbench_router,
        "_broker",
        type("FakeBroker", (), {"browser_skill": FakeBrowserSkill()})(),
    )

    health = await browser_workbench_router._browser_bridge_health(
        {"primary_runtime": "browser_skill"},
    )

    assert health.status == "available"
    assert health.runtime == "browser_skill"


@pytest.mark.asyncio
async def test_app_store_state_does_not_advertise_browser_automation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(browser_workbench_router, "is_app_store_distribution", lambda: True)
    monkeypatch.setattr(
        browser_workbench_router,
        "_browser_config_payload",
        lambda: {
            "app_store_distribution": True,
            "agent_control_enabled": True,
            "primary_runtime": "electron_bridge",
        },
    )
    monkeypatch.setattr(browser_workbench_router, "_profiles", BrowserProfileService(tmp_path))
    monkeypatch.setattr(
        browser_workbench_router,
        "_tasks",
        BrowserTaskRuntime(
            broker=BrowserActionBroker(
                profiles=BrowserProfileService(tmp_path),
                replay=BrowserReplayStore(tmp_path),
            ),
            app_store_distribution=lambda: True,
        ),
    )
    monkeypatch.setattr(
        browser_workbench_router,
        "_registry",
        LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills"),
    )
    monkeypatch.setattr(browser_workbench_router, "_content", ContentWorkbenchStore(tmp_path))

    state = await browser_workbench_router.get_browser_workbench_state()

    assert state.bridge_health.status == "app_store_blocked"
    assert state.capability_flags.can_open is False
    assert state.capability_flags.can_read is False
    assert state.capability_flags.can_write is False
    assert state.capability_flags.can_run_background is False
    assert state.capability_flags.can_generate_domain_skill is False
    assert state.capability_matrix["browser_read_navigation"].mode == "read_only"
    assert (
        state.capability_matrix["browser_read_navigation"].reason_code
        == "mas_browser_read_only"
    )
    assert state.capability_matrix["browser_write"].mode == "disabled"
    assert (
        state.capability_matrix["browser_write"].reason_code
        == "mas_browser_write_disabled"
    )


class FakeResearchBroker:
    def __init__(self, replay: BrowserReplayStore):
        self.replay = replay

    async def execute(self, action: BrowserAction) -> BrowserObservation:
        observation = BrowserObservation(
            ok=True,
            action=action.action,
            runtime="electron_bridge",
            profile_id=action.profile_id,
            url=action.url,
            screenshot_path=f"/tmp/{action.run_id}-{action.action}.png"
            if action.action == "screenshot"
            else None,
            data={
                "ok": True,
                "data": {
                    "title": f"Title for {action.url}",
                    "url": action.url,
                    "text": f"Visible text for {action.url}",
                    "links": [{"href": action.url}],
                    "controls": [],
                    "page_context": {
                        "kind": "normal",
                        "requires_manual_takeover": False,
                    },
                }
            },
        )
        self.replay.append(
            BrowserReplayEvent(
                id=observation.replay_event_id,
                run_id=action.run_id,
                profile_id=action.profile_id,
                action=action.action,
                runtime=observation.runtime,
                access="read",
                risk="low",
                decision="allow",
                reason=action.reason,
                ok=True,
                url=action.url,
                screenshot_path=observation.screenshot_path,
                metadata={
                    **action.metadata,
                    "observation_summary": {
                        "title": f"Title for {action.url}",
                        "text_length": 100,
                        "links_count": 1,
                        "text_excerpt": f"Visible text for {action.url}",
                        "page_context": {
                            "kind": "normal",
                            "requires_manual_takeover": False,
                        },
                    },
                },
            )
        )
        return observation


class FakeWechatWriteBroker:
    def __init__(
        self,
        *,
        snapshot_context: dict[str, object] | None = None,
    ) -> None:
        self.actions: list[BrowserAction] = []
        self.snapshot_context = snapshot_context or {
            "kind": "normal",
            "requires_manual_takeover": False,
        }

    async def execute(self, action: BrowserAction) -> BrowserObservation:
        self.actions.append(action)
        data: dict[str, object] = {"ok": True}
        if action.action == "snapshot":
            data = {
                "data": {
                    "title": "微信公众平台图文编辑",
                    "url": "https://mp.weixin.qq.com/cgi-bin/appmsg",
                    "page_context": self.snapshot_context,
                }
            }
        elif action.action == "fill_form":
            fields = action.metadata.get("fields_json")
            data = {
                "data": {
                    "filled": list(fields.keys()) if isinstance(fields, dict) else [],
                    "missing": [],
                    "details": [
                        {
                            "selector": action.metadata.get("title_selector", "#title"),
                            "mode": "value",
                            "tag": "input",
                            "value_length": len(str((fields or {}).get("#title", "")))
                            if isinstance(fields, dict)
                            else 0,
                        },
                        {
                            "selector": "#editor",
                            "mode": "contenteditable_insert_text",
                            "tag": "div",
                            "contenteditable": True,
                            "value_length": len(str((fields or {}).get("#editor", "")))
                            if isinstance(fields, dict)
                            else 0,
                        },
                    ],
                }
            }
        elif action.action == "click":
            data = {"data": {"clicked": action.selector}}
        return BrowserObservation(
            ok=True,
            action=action.action,
            runtime="electron_bridge",
            profile_id=action.profile_id,
            url=action.url or "https://mp.weixin.qq.com/cgi-bin/appmsg",
            data=data,
        )


class FakeFailingTaskBroker:
    def __init__(
        self,
        replay: BrowserReplayStore,
        *,
        error: str,
        data: dict[str, object] | None = None,
    ) -> None:
        self.replay = replay
        self.error = error
        self.data = data or {}
        self.actions: list[BrowserAction] = []

    async def execute(self, action: BrowserAction) -> BrowserObservation:
        self.actions.append(action)
        observation = BrowserObservation(
            ok=False,
            action=action.action,
            runtime="electron_bridge",
            profile_id=action.profile_id,
            url=action.url,
            error=self.error,
            data=self.data,
        )
        self.replay.append(
            BrowserReplayEvent(
                id=observation.replay_event_id,
                run_id=action.run_id,
                profile_id=action.profile_id,
                action=action.action,
                runtime=observation.runtime,
                access=action.access or "read",
                risk="low",
                decision="allow",
                reason=action.reason,
                ok=False,
                url=action.url,
                error=self.error,
            )
        )
        return observation


class FakePreflightBroker:
    def __init__(self, replay: BrowserReplayStore) -> None:
        self.replay = replay

    async def execute(self, action: BrowserAction) -> BrowserObservation:
        observation = BrowserObservation(
            ok=True,
            action=action.action,
            runtime="electron_bridge",
            profile_id=action.profile_id,
            url=action.url,
            data={
                "data": [
                    {"selector": "button.save-draft", "count": 0},
                    {
                        "selector": "#title",
                        "count": 4,
                        "scope_counts": {
                            "document": 1,
                            "iframe": 2,
                            "shadow": 1,
                        },
                        "blocked_frame_count": 1,
                        "scopes": [
                            {
                                "kind": "document",
                                "scope": "document",
                                "count": 1,
                                "blocked": False,
                            },
                            {
                                "kind": "iframe",
                                "scope": "iframe[0]",
                                "count": 2,
                                "blocked": False,
                                "frame_selector": "iframe.editor",
                            },
                            {
                                "kind": "shadow",
                                "scope": "shadow[0]",
                                "count": 1,
                                "blocked": False,
                                "shadow_host_selector": "editor-shell",
                            },
                            {
                                "kind": "iframe",
                                "scope": "iframe[1]",
                                "count": 0,
                                "blocked": True,
                                "frame_selector": "iframe.cross-origin",
                                "error": "cross_origin_or_unavailable",
                            },
                        ],
                    },
                ]
            },
        )
        self.replay.append(
            BrowserReplayEvent(
                id=observation.replay_event_id,
                run_id=action.run_id,
                profile_id=action.profile_id,
                action=action.action,
                runtime=observation.runtime,
                access=action.access or "read",
                risk="low",
                decision="allow",
                reason=action.reason,
                ok=True,
                url=action.url,
                metadata=action.metadata,
            )
        )
        return observation


class FakeSelectorProbeBroker:
    def __init__(self) -> None:
        self.actions: list[BrowserAction] = []

    async def execute(self, action: BrowserAction) -> BrowserObservation:
        self.actions.append(action)
        return BrowserObservation(
            ok=True,
            action=action.action,
            runtime="electron_bridge",
            profile_id=action.profile_id,
            page_id=action.page_id,
            url=action.url,
            data={
                "data": [
                    {
                        "selector": "main article",
                        "ok": True,
                        "count": 6,
                        "diagnostics": {
                            "document_ready_state": "complete",
                            "iframe_count": 2,
                            "accessible_iframe_count": 1,
                            "blocked_iframe_count": 1,
                            "shadow_host_count": 1,
                            "open_shadow_root_count": 1,
                            "sampled_node_count": 120,
                        },
                        "scope_counts": {
                            "document": 1,
                            "iframe": 2,
                            "shadow": 3,
                        },
                        "blocked_frame_count": 1,
                        "samples": [
                            {"scope": "document", "tag": "article", "text": "Hello"}
                        ],
                        "scopes": [
                            {
                                "scope": "document",
                                "kind": "document",
                                "ok": True,
                                "blocked": False,
                                "count": 1,
                            },
                            {
                                "scope": "iframe[0]",
                                "kind": "iframe",
                                "ok": True,
                                "blocked": False,
                                "count": 2,
                            },
                            {
                                "scope": "iframe[1]",
                                "kind": "iframe",
                                "ok": False,
                                "blocked": True,
                                "count": 0,
                                "error": "cross_origin_or_unavailable",
                            },
                            {
                                "scope": "shadow[0]",
                                "kind": "shadow",
                                "ok": True,
                                "blocked": False,
                                "count": 3,
                            },
                        ],
                    }
                ]
            },
        )


async def wait_for_task_status(
    runtime: BrowserTaskRuntime,
    run_id: str,
    statuses: set[str],
    *,
    timeout_seconds: float = 0.3,
) -> object:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        run = runtime.get_run(run_id)
        if run is not None and run.status in statuses:
            return run
        await asyncio.sleep(0.01)
    return runtime.get_run(run_id)


@pytest.mark.asyncio
async def test_read_task_scroll_and_next_page_click_run_without_approval(tmp_path):
    class RecordingBridge(FakeElectronBridge):
        def __init__(self):
            self.actions: list[BrowserAction] = []

        async def execute(self, action: BrowserAction):
            self.actions.append(action)
            return await super().execute(action)

    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    bridge = RecordingBridge()
    runtime = BrowserTaskRuntime(
        broker=BrowserActionBroker(
            profiles=profiles,
            replay=BrowserReplayStore(tmp_path),
            bridge=bridge,
        ),
        app_store_distribution=lambda: False,
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="read-twenty-pages",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            instruction="翻看20页笔记并总结",
            actions=[
                BrowserAction(
                    action="press_key",
                    profile_id="research",
                    url="https://example.com/search",
                    access="read",
                    metadata={"key": "PageDown"},
                ),
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/search",
                    selector="#next-page",
                    access="read",
                    reason="打开下一页搜索结果",
                ),
            ],
        )
    )

    completed = await wait_for_task_status(
        runtime,
        run.run_id,
        {"completed", "waiting_for_user_confirmation", "failed"},
    )

    assert completed is not None
    assert completed.status == "completed"
    assert [action.action for action in bridge.actions] == [
        "press_key",
        "click",
        "close_task_group",
    ]
    assert all(action.access == "read" for action in bridge.actions)


@pytest.mark.asyncio
async def test_read_task_external_side_effect_click_still_waits_for_approval(
    tmp_path,
):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    runtime = BrowserTaskRuntime(
        broker=BrowserActionBroker(
            profiles=profiles,
            replay=BrowserReplayStore(tmp_path),
            bridge=FakeElectronBridge(),
        ),
        app_store_distribution=lambda: False,
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="read-task-like-canary",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            instruction="只读取笔记",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/post/1",
                    selector="#like",
                    access="read",
                    reason="点赞这条笔记",
                )
            ],
        )
    )

    stopped = await wait_for_task_status(
        runtime,
        run.run_id,
        {"waiting_for_user_confirmation", "completed", "failed"},
    )

    assert stopped is not None
    assert stopped.status == "waiting_for_user_confirmation"
    assert stopped.pending_confirmation is not None
    assert stopped.pending_confirmation.action == "click"


@pytest.mark.asyncio
async def test_broker_prefers_electron_bridge_when_available(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=FakeElectronBridge(),
    )

    observation = await broker.execute(
        BrowserAction(
            action="open",
            profile_id="research",
            url="https://example.com",
            runtime="electron_bridge",
            run_id="run-electron",
        )
    )

    assert observation.ok is True
    assert observation.runtime == "electron_bridge"
    events = broker.replay.list_events("run-electron")
    assert events[0]["runtime"] == "electron_bridge"


@pytest.mark.asyncio
async def test_broker_rechecks_final_url_against_profile_policy(tmp_path):
    class RedirectingElectronBridge(FakeElectronBridge):
        async def execute(self, action: BrowserAction):
            payload = await super().execute(action)
            return {
                **payload,
                "url": "https://evil.example/landing",
                "final_url": "https://evil.example/landing",
            }

    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    replay = BrowserReplayStore(tmp_path)
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=replay,
        bridge=RedirectingElectronBridge(),
    )

    observation = await broker.execute(
        BrowserAction(
            action="open",
            profile_id="research",
            url="https://example.com/start",
            runtime="electron_bridge",
            run_id="run-final-url",
        )
    )

    events = replay.list_events("run-final-url")

    assert observation.ok is False
    assert observation.error == "Final browser URL is not allowed for this profile."
    assert observation.data["error_code"] == "browser_final_domain_not_allowed"
    assert observation.data["initial_url"] == "https://example.com/start"
    assert observation.data["final_url"] == "https://evil.example/landing"
    assert events[0]["ok"] is False
    assert events[0]["metadata"]["error_code"] == "browser_final_domain_not_allowed"


@pytest.mark.asyncio
async def test_broker_records_electron_bridge_error_code(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    replay = BrowserReplayStore(tmp_path)
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=replay,
        bridge=UnauthorizedElectronBridge(),
    )

    observation = await broker.execute(
        BrowserAction(
            action="open",
            profile_id="research",
            url="https://example.com",
            runtime="electron_bridge",
            run_id="run-bridge-unauthorized",
        )
    )

    events = replay.list_events("run-bridge-unauthorized")
    assert observation.ok is False
    assert observation.error == "Unauthorized browser bridge request."
    assert observation.data["error_code"] == "electron_bridge_unauthorized"
    assert events[0]["ok"] is False
    assert events[0]["metadata"]["error_code"] == "electron_bridge_unauthorized"


@pytest.mark.asyncio
async def test_broker_stops_electron_actions_when_user_pauses(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=PausedElectronBridge(),
    )

    observation = await broker.execute(
        BrowserAction(
            action="open",
            profile_id="research",
            url="https://example.com",
            runtime="electron_bridge",
            run_id="run-paused",
        )
    )

    assert observation.ok is False
    assert observation.runtime == "electron_bridge"
    assert observation.data["interruption"] == "paused"
    assert "paused" in (observation.error or "")
    events = broker.replay.list_events("run-paused")
    assert events[0]["ok"] is False
    assert events[0]["runtime"] == "electron_bridge"


def test_broker_records_user_denied_confirmation(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="github-work",
            name="GitHub Work",
            allowed_domains=["github.com"],
            agent_control_enabled=True,
        )
    )
    replay = BrowserReplayStore(tmp_path)
    broker = BrowserActionBroker(profiles=profiles, replay=replay)

    observation = broker.deny_confirmation(
        BrowserAction(
            action="click",
            profile_id="github-work",
            selector="#submit",
            run_id="run-denied",
        ),
        reason="用户拒绝提交表单",
    )

    assert observation.ok is False
    assert observation.decision is not None
    assert observation.decision.matched_rule_id == "browser_user_denied_confirmation"
    assert observation.data["confirmation_result"] == "denied"
    events = replay.list_events("run-denied")
    assert events[0]["decision"] == "block"
    assert events[0]["reason"] == "用户拒绝提交表单"


def test_replay_store_exports_evidence_bundle(tmp_path):
    replay = BrowserReplayStore(tmp_path)
    screenshot = tmp_path / "browser_workbench" / "evidence" / "shot.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"png")
    replay.append(
        BrowserReplayEvent(
            run_id="run-export",
            profile_id="research",
            action="screenshot",
            runtime="electron_bridge",
            access="read",
            risk="low",
            decision="allow",
            ok=True,
            url="https://example.com",
            screenshot_path=str(screenshot),
            metadata={"observation_summary": {"title": "Example"}},
        )
    )

    bundle = replay.export_bundle(
        "run-export",
        approval=_export_approval("export trusted replay evidence"),
    )

    report = tmp_path / "browser_workbench" / "evidence_bundles" / "run-export" / "report.md"
    copied = tmp_path / "browser_workbench" / "evidence_bundles" / "run-export" / "evidence" / "001-shot.png"
    assert bundle["ok"] is True
    assert bundle["export_approval"]["approved"] is True
    assert bundle["export_approval"]["export_kind"] == "browser_replay_bundle"
    assert bundle["event_count"] == 1
    assert report.is_file()
    assert copied.read_bytes() == b"png"
    assert "https://example.com" in report.read_text("utf-8")


def test_replay_store_export_requires_explicit_approval(tmp_path):
    replay = BrowserReplayStore(tmp_path)

    with pytest.raises(PermissionError, match="export approval required"):
        replay.export_bundle("run-export")

    with pytest.raises(ValueError, match="reason required"):
        replay.export_bundle(
            "run-export",
            approval=BrowserExportApproval(approved=True),
        )


@pytest.mark.asyncio
async def test_browser_workbench_export_routes_require_approval(
    tmp_path,
    monkeypatch,
):
    content = ContentWorkbenchStore(tmp_path)
    source = content.save_source(
        ContentSource(
            title="Approved source",
            url="https://example.com/export",
            metadata={"api_key": "source-secret"},
        )
    )
    monkeypatch.setattr(browser_workbench_router, "_content", content)

    with pytest.raises(HTTPException) as exc_info:
        await browser_workbench_router.export_browser_replay("run-export")
    assert exc_info.value.status_code == 403

    with pytest.raises(HTTPException) as exc_info:
        await browser_workbench_router.export_content_source_markdown(source.id)
    assert exc_info.value.status_code == 403

    response = await browser_workbench_router.export_content_source_markdown(
        source.id,
        _export_approval("export source markdown"),
    )
    exported_text = Path(str(response["path"])).read_text("utf-8")

    assert response["ok"] is True
    assert response["export_approval"]["approved"] is True
    assert response["export_approval"]["export_kind"] == "content_source_markdown"
    assert "source-secret" not in exported_text


def test_replay_store_does_not_export_untrusted_screenshot_path(tmp_path):
    screenshot = tmp_path / "secret.png"
    screenshot.write_bytes(b"secret")
    replay = BrowserReplayStore(tmp_path)
    replay.append(
        BrowserReplayEvent(
            run_id="run-untrusted-export",
            profile_id="research",
            action="screenshot",
            runtime="electron_bridge",
            access="read",
            risk="low",
            decision="allow",
            ok=True,
            url="https://example.com",
            screenshot_path=str(screenshot),
        )
    )

    bundle = replay.export_bundle(
        "run-untrusted-export",
        approval=_export_approval("export untrusted replay metadata only"),
    )

    manifest_path = Path(bundle["manifest_path"])
    manifest = json.loads(manifest_path.read_text("utf-8"))
    evidence_dir = (
        tmp_path
        / "browser_workbench"
        / "evidence_bundles"
        / "run-untrusted-export"
        / "evidence"
    )
    assert bundle["copied_evidence_count"] == 0
    assert list(evidence_dir.iterdir()) == []
    assert manifest["events"][0]["screenshot_path"] == "secret.png"
    assert manifest["events"][0]["evidence_untrusted"] == "secret.png"


def test_replay_store_sanitizes_metadata_at_write_and_export(tmp_path):
    replay = BrowserReplayStore(tmp_path)
    replay.append(
        BrowserReplayEvent(
            run_id="run-sensitive-metadata",
            profile_id="research",
            action="click",
            runtime="electron_bridge",
            access="write",
            risk="medium",
            decision="prompt",
            ok=True,
            url="https://example.com/editor",
            metadata={
                "selector_value": "#save",
                "authorization": "Bearer should-not-persist",
                "nested": {"api_key": "nested-secret"},
                "text_preview": "user visible note",
            },
        )
    )

    replay_path = next((tmp_path / "browser_workbench" / "replay").glob("*.jsonl"))
    replay_text = replay_path.read_text("utf-8")
    events = replay.list_events("run-sensitive-metadata")

    assert "should-not-persist" not in replay_text
    assert "nested-secret" not in replay_text
    assert events[0]["metadata"]["selector_value"] == "#save"
    assert "authorization" not in events[0]["metadata"]
    assert events[0]["metadata"]["nested"]["api_key"]["redacted"] is True
    assert events[0]["metadata"]["data_classification"] == "sensitive"

    bundle = replay.export_bundle(
        "run-sensitive-metadata",
        approval=_export_approval("export sanitized replay metadata"),
    )
    manifest = json.loads(Path(bundle["manifest_path"]).read_text("utf-8"))
    manifest_text = json.dumps(manifest, ensure_ascii=False)
    manifest_metadata = manifest["events"][0]["metadata"]

    assert "should-not-persist" not in manifest_text
    assert "nested-secret" not in manifest_text
    assert manifest["second_sanitization"]["metadata"] is True
    assert manifest["export_approval"]["approved"] is True
    assert manifest_metadata["selector_value"] == "#save"
    assert manifest_metadata["nested"]["api_key"]["redacted"] is True
    assert manifest_metadata["data_classification"] == "sensitive"


def test_replay_store_run_id_cannot_escape_replay_root(tmp_path):
    replay = BrowserReplayStore(tmp_path)

    path = replay.append(
        BrowserReplayEvent(
            run_id="../escape",
            profile_id="research",
            action="open",
            runtime="electron_bridge",
            access="read",
            risk="low",
            decision="allow",
            ok=True,
            url="https://example.com",
        )
    )

    replay_root = tmp_path / "browser_workbench" / "replay"
    assert path.resolve().is_relative_to(replay_root.resolve())
    assert not (tmp_path / "browser_workbench" / "escape.jsonl").exists()
    assert replay.list_events("../escape")[0]["run_id"] == "../escape"


def test_replay_store_list_events_rejects_embedded_run_id_mismatch(tmp_path):
    replay = BrowserReplayStore(tmp_path)
    path = replay.append(
        BrowserReplayEvent(
            run_id="run-owner",
            profile_id="research",
            action="open",
            runtime="electron_bridge",
            access="read",
            risk="low",
            decision="allow",
            ok=True,
            url="https://example.com/owner",
        )
    )
    path.write_text(
        path.read_text("utf-8")
        + json.dumps(
            {
                "run_id": "run-foreign",
                "profile_id": "research",
                "action": "open",
                "runtime": "electron_bridge",
                "access": "read",
                "risk": "low",
                "decision": "allow",
                "ok": True,
                "url": "https://example.com/foreign",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    events = replay.list_events("run-owner")

    assert [event["run_id"] for event in events] == ["run-owner"]
    assert [event["url"] for event in events] == ["https://example.com/owner"]


def test_content_store_source_id_cannot_escape_sources_root(tmp_path):
    store = ContentWorkbenchStore(tmp_path)
    source = ContentSource(
        id="../escape",
        source_type="web_article",
        title="Traversal",
        url="https://example.com/traversal",
    )

    store.save_source(source)

    sources_root = tmp_path / "browser_workbench" / "content" / "sources"
    files = list(sources_root.glob("*.json"))
    assert len(files) == 1
    assert files[0].resolve().is_relative_to(sources_root.resolve())
    assert not (tmp_path / "browser_workbench" / "content" / "escape.json").exists()
    assert store.load_source("../escape").url == source.url


def test_content_store_rejects_artifact_id_path_mismatches(tmp_path):
    store = ContentWorkbenchStore(tmp_path)

    def write_mismatched_artifact(directory, kind, path_id, model):
        path = store._artifact_path(  # pylint: disable=protected-access
            directory,
            kind,
            path_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(model.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    store.save_source(
        ContentSource(
            id="source-valid",
            source_type="web_article",
            title="Valid source",
            url="https://example.com/valid",
        )
    )
    write_mismatched_artifact(
        store.sources_dir,
        "source",
        "source-path-owner",
        ContentSource(
            id="source-embedded-owner",
            source_type="web_article",
            title="Foreign source",
            url="https://example.com/foreign",
        ),
    )
    write_mismatched_artifact(
        store.dossiers_dir,
        "dossier",
        "dossier-path-owner",
        ContentDossier(id="dossier-embedded-owner", topic="Foreign dossier"),
    )
    write_mismatched_artifact(
        store.drafts_dir,
        "draft",
        "draft-path-owner",
        PublishingDraft(id="draft-embedded-owner", title="Foreign draft"),
    )
    write_mismatched_artifact(
        store.queue_dir,
        "queue-item",
        "queue-path-owner",
        PublishingQueueItem(
            id="queue-embedded-owner",
            draft_id="draft-embedded-owner",
        ),
    )
    write_mismatched_artifact(
        store.root_dir / "workflow_runs",
        "workflow-run",
        "workflow-path-owner",
        ContentWorkflowRun(
            id="workflow-embedded-owner",
            workflow="browser_research_pack",
            topic="Foreign workflow",
        ),
    )

    assert [item["id"] for item in store.list_sources()] == ["source-valid"]
    assert store.list_dossiers() == []
    assert store.list_drafts() == []
    assert store.list_queue() == []
    assert store.list_workflow_runs() == []
    with pytest.raises(KeyError):
        store.load_source("source-path-owner")
    with pytest.raises(KeyError):
        store.load_dossier("dossier-path-owner")
    with pytest.raises(KeyError):
        store.load_draft("draft-path-owner")
    with pytest.raises(KeyError):
        store.load_queue_item("queue-path-owner")


def test_content_store_sanitizes_artifact_metadata_at_write(tmp_path):
    store = ContentWorkbenchStore(tmp_path)
    source = store.save_source(
        ContentSource(
            source_type="web_article",
            title="Sensitive Source",
            url="https://example.com/source",
            metadata={
                "api_key": "source-secret",
                "nested": {"authorization": "Bearer source"},
            },
        )
    )
    dossier = store.build_dossier(
        topic="Sensitive Dossier",
        sources=[source],
        metadata={"token": "dossier-secret"},
    )
    series = store.create_series_plan(
        title="Sensitive Series",
        metadata={"access_token": "series-secret"},
    )
    draft = store.create_publishing_draft(
        title="Sensitive Draft",
        metadata={"authorization": "Bearer draft"},
    )
    queue_item = store.queue_publishing_draft(
        draft_id=draft.id,
        metadata={"cookie": "queue-secret"},
    )
    workflow = store.record_workflow_run(
        workflow="browser_research_pack",
        topic="Sensitive Workflow",
        metadata={"refresh_token": "workflow-secret"},
    )

    loaded_source = store.load_source(source.id)
    loaded_dossier = store.load_dossier(dossier.id)
    loaded_series = store._list_json(  # pylint: disable=protected-access
        store.series_dir,
        "series",
    )
    loaded_draft = store.load_draft(draft.id)
    loaded_queue = store.load_queue_item(queue_item.id)
    loaded_workflow = [
        item for item in store.list_workflow_runs() if item.get("id") == workflow.id
    ][0]

    assert "api_key" not in loaded_source.metadata
    assert loaded_source.metadata["nested"]["authorization"]["redacted"] is True
    assert loaded_source.metadata["data_classification"] == "sensitive"
    assert "api_key" not in loaded_dossier.sources[0].metadata
    assert "token" not in loaded_dossier.metadata
    assert "access_token" not in loaded_series[0].get("metadata", {})
    assert "authorization" not in loaded_draft.metadata
    assert "cookie" not in loaded_queue.metadata
    assert "refresh_token" not in loaded_workflow["metadata"]


def test_domain_skill_draft_domain_cannot_escape_registry_root(tmp_path):
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")
    draft = DomainSkillDraft(
        domain="../../escape",
        title="Traversal Skill",
        summary="Should stay inside registry",
    )

    path = registry.save_domain_skill_draft(draft)

    root = tmp_path / "skills"
    assert path.resolve().is_relative_to(root.resolve())
    assert not (tmp_path.parent / "escape").exists()
    assert registry.list_domain_skill_drafts()[0].draft.domain == "../../escape"


def test_replay_store_rejects_symlink_replay_file(tmp_path):
    replay = BrowserReplayStore(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("outside\n", "utf-8")
    path = replay._replay_path("run-symlink")  # noqa: SLF001
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises((OSError, ValueError)):
        replay.append(
            BrowserReplayEvent(
                run_id="run-symlink",
                profile_id="research",
                action="open",
                runtime="electron_bridge",
                access="read",
                risk="low",
                decision="allow",
                ok=True,
                url="https://example.com",
            )
        )

    assert outside.read_text("utf-8") == "outside\n"


def test_content_store_source_storage_rejects_symlink_file(tmp_path):
    store = ContentWorkbenchStore(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text('{"outside": true}', "utf-8")
    source = ContentSource(
        id="source-symlink",
        source_type="web_article",
        title="Symlink",
        url="https://example.com/symlink",
    )
    path = store._artifact_path(  # noqa: SLF001
        store.sources_dir,
        "source",
        source.id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises((OSError, ValueError)):
        store.save_source(source)

    assert outside.read_text("utf-8") == '{"outside": true}'


def test_domain_skill_draft_update_rejects_symlink_storage_file(tmp_path):
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")
    registry.save_domain_skill_draft(
        DomainSkillDraft(
            domain="example.com",
            title="Symlink Skill",
            summary="Original",
        )
    )
    record = registry.list_domain_skill_drafts()[0]
    path = Path(record.path)
    outside = tmp_path / "outside-skill.json"
    outside.write_text('{"outside": true}', "utf-8")
    path.unlink()
    try:
        path.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises((OSError, ValueError)):
        registry.update_domain_skill_draft(
            record.id,
            DomainSkillDraftPatch(summary="Updated"),
        )

    assert outside.read_text("utf-8") == '{"outside": true}'


def test_domain_skill_draft_rejects_path_identity_mismatch(tmp_path):
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")
    path = registry.save_domain_skill_draft(
        DomainSkillDraft(
            domain="example.com",
            title="Example Article Flow",
            summary="Original",
        )
    )
    path.write_text(
        DomainSkillDraft(
            domain="evil.example",
            title="Other Flow",
            summary="Mismatched payload",
        ).model_dump_json(indent=2),
        "utf-8",
    )

    assert registry.list_domain_skill_drafts() == []
    with pytest.raises(ValueError):
        registry.load_domain_skill_draft("example.com:example-article-flow")


def test_registry_sanitizes_domain_skill_metadata_at_write_and_update(tmp_path):
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")
    registry.save_domain_skill_draft(
        DomainSkillDraft(
            domain="example.com",
            title="Sensitive Skill",
            metadata={
                "api_key": "draft-secret",
                "nested": {"authorization": "Bearer secret"},
            },
        )
    )

    record = registry.list_domain_skill_drafts()[0]
    metadata = record.draft.metadata
    assert "api_key" not in metadata
    assert metadata["nested"]["authorization"]["redacted"] is True
    assert metadata["data_classification"] == "sensitive"

    updated = registry.update_domain_skill_draft(
        record.id,
        DomainSkillDraftPatch(
            metadata={
                "review_note": "approved",
                "token": "patch-secret",
            },
        ),
    )

    assert updated.draft.metadata["review_note"] == "approved"
    assert "token" not in updated.draft.metadata
    assert updated.draft.metadata["data_classification"] == "sensitive"


def test_replay_store_builds_domain_skill_draft(tmp_path):
    replay = BrowserReplayStore(tmp_path)
    replay.append(
        BrowserReplayEvent(
            run_id="run-skill",
            profile_id="research",
            action="open",
            runtime="electron_bridge",
            access="read",
            risk="low",
            decision="allow",
            ok=True,
            url="https://example.com/articles/123456",
        )
    )
    replay.append(
        BrowserReplayEvent(
            run_id="run-skill",
            profile_id="research",
            action="click",
            runtime="electron_bridge",
            access="write",
            risk="medium",
            decision="allow",
            ok=True,
            url="https://example.com/articles/123456",
            metadata={
                "selector_value": "button.save",
                "observation_summary": {
                    "scope": {
                        "frame_selector": "iframe.editor",
                        "shadow_host_selector": "editor-shell",
                    },
                    "field_scopes": [
                        {
                            "selector": "#title",
                            "frame_selector": "iframe.editor",
                            "shadow_host_selector": "editor-shell",
                        }
                    ],
                },
            },
        )
    )

    draft = replay.build_domain_skill_draft("run-skill")

    assert draft.domain == "example.com"
    assert draft.url_patterns == ["https://example.com/articles/{id}"]
    assert draft.selectors == ["button.save"]
    assert draft.metadata["run_id"] == "run-skill"
    assert draft.metadata["scopes"] == [
        {
            "frame_selector": "iframe.editor",
            "shadow_host_selector": "editor-shell",
        },
        {
            "selector": "#title",
            "frame_selector": "iframe.editor",
            "shadow_host_selector": "editor-shell",
        },
    ]
    assert "verify target element is visible" in draft.wait_conditions[-1]


@pytest.mark.asyncio
async def test_domain_skill_from_replay_merges_task_recovery_failure_modes(
    tmp_path,
    monkeypatch,
):
    replay = BrowserReplayStore(tmp_path)
    replay.append(
        BrowserReplayEvent(
            run_id="run-recovery-skill",
            profile_id="research",
            action="click",
            runtime="electron_bridge",
            access="read",
            risk="low",
            decision="allow",
            ok=False,
            url="https://example.com/app",
            error="Element not found.",
            metadata={"selector_value": "#missing"},
        )
    )
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")

    class BrokerWithReplay:
        pass

    broker = BrokerWithReplay()
    broker.replay = replay
    monkeypatch.setattr(browser_workbench_router, "_broker", broker)
    monkeypatch.setattr(browser_workbench_router, "_registry", registry)

    payload = await browser_workbench_router.build_domain_skill_draft_from_replay(
        "run-recovery-skill",
        browser_workbench_router.DomainSkillFromReplayRequest(
            title="Recovery Skill",
            save=True,
            failure_modes=[
                "element_not_found: browser_element_not_found",
                "Re-run selector probe.",
            ],
            metadata={
                "task_recovery": {
                    "kind": "element_not_found",
                    "error_code": "browser_element_not_found",
                },
                "scope_candidates": [
                    {
                        "selector": "#missing",
                        "frame_selector": "iframe.editor",
                    },
                    {
                        "selector": "#missing",
                        "shadow_host_selector": "editor-shell",
                    },
                ],
                "password": "must not persist",
            },
        ),
    )

    assert payload["ok"] is True
    assert Path(payload["path"]).is_file()
    assert payload["draft"]["failure_modes"] == [
        "Element not found.",
        "element_not_found: browser_element_not_found",
        "Re-run selector probe.",
    ]
    assert payload["draft"]["metadata"]["task_recovery"] == {
        "kind": "element_not_found",
        "error_code": "browser_element_not_found",
    }
    assert payload["draft"]["metadata"]["scope_candidates"] == [
        {
            "selector": "#missing",
            "frame_selector": "iframe.editor",
        },
        {
            "selector": "#missing",
            "shadow_host_selector": "editor-shell",
        },
    ]
    assert payload["draft"]["metadata"]["scopes"] == [
        {
            "selector": "#missing",
            "frame_selector": "iframe.editor",
        },
        {
            "selector": "#missing",
            "shadow_host_selector": "editor-shell",
        },
    ]
    assert "password" not in payload["draft"]["metadata"]


def test_registry_lists_and_updates_domain_skill_drafts(tmp_path):
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")
    draft = DomainSkillDraft(
        domain="example.com",
        title="Example Article Flow",
        summary="Extract article pages with stable selectors.",
        url_patterns=["https://example.com/articles/{id}"],
        selectors=["main article"],
        wait_conditions=["wait for article body"],
        failure_modes=["paywall"],
        metadata={
            "run_id": "run-skill",
            "recommended_adapter_id": "media.article.extract",
        },
    )

    saved_path = registry.save_domain_skill_draft(draft)
    records = registry.list_domain_skill_drafts()
    enabled = registry.update_domain_skill_draft(
        records[0].id,
        DomainSkillDraftPatch(
            enabled=True,
            selectors=["main article", ".byline"],
            metadata={"review_note": "人工验收通过"},
        ),
    )

    assert saved_path.is_file()
    assert records[0].id == "example.com:example-article-flow"
    assert records[0].source_run_id == "run-skill"
    assert records[0].recommended_adapter_id == "media.article.extract"
    assert enabled.enabled is True
    assert enabled.draft.selectors == ["main article", ".byline"]
    assert enabled.draft.metadata["review_note"] == "人工验收通过"
    assert enabled.draft.metadata["enabled"] is True
    assert registry.load_domain_skill_draft(enabled.id).draft.title == (
        "Example Article Flow"
    )


@pytest.mark.asyncio
async def test_domain_skill_selector_probe_runs_read_evaluate_action(
    tmp_path,
    monkeypatch,
):
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")
    registry.save_domain_skill_draft(
        DomainSkillDraft(
            domain="example.com",
            title="Example Article Flow",
            url_patterns=["https://example.com/articles/{id}"],
            selectors=["main article"],
        )
    )
    record = registry.list_domain_skill_drafts()[0]
    broker = FakeSelectorProbeBroker()
    monkeypatch.setattr(browser_workbench_router, "_registry", registry)
    monkeypatch.setattr(browser_workbench_router, "_broker", broker)

    result = await probe_domain_skill_selectors(
        record.id,
        DomainSkillSelectorProbeRequest(
            profile_id="research",
            page_id="tab-1",
            url="https://example.com/articles/123",
        ),
    )

    action = broker.actions[0]
    assert result.ok is True
    assert action.action == "evaluate"
    assert action.access == "read"
    assert action.profile_id == "research"
    assert action.page_id == "tab-1"
    assert "main article" in action.code
    assert "iframe_count" in action.code
    assert "accessible_iframe_count" in action.code
    assert "blocked_iframe_count" in action.code
    assert "scope_counts" in action.code
    assert "shadow_host_count" in action.code
    assert "open_shadow_root_count" in action.code
    assert action.metadata["domain_skill_id"] == record.id
    assert action.metadata["probe"] == "domain_skill_selectors"
    updated = registry.load_domain_skill_draft(record.id)
    probe = updated.draft.metadata["last_selector_probe"]
    assert probe["run_id"] == action.run_id
    assert probe["selector_count"] == 1
    assert probe["matched_selector_count"] == 1
    assert probe["total_matches"] == 6
    assert probe["document_match_count"] == 1
    assert probe["iframe_match_count"] == 2
    assert probe["shadow_match_count"] == 3
    assert probe["blocked_frame_count"] == 1
    assert probe["scope_match_counts"] == {
        "document": 1,
        "iframe": 2,
        "shadow": 3,
    }
    assert probe["url_pattern_matched"] is True
    assert probe["matched_url_patterns"] == ["https://example.com/articles/{id}"]
    assert probe["page_diagnostics"]["iframe_count"] == 2
    assert probe["page_diagnostics"]["accessible_iframe_count"] == 1
    assert probe["page_diagnostics"]["blocked_iframe_count"] == 1
    assert probe["page_diagnostics"]["shadow_host_count"] == 1
    assert probe["page_diagnostics"]["open_shadow_root_count"] == 1
    assert updated.draft.metadata["selector_probe_history"][0] == probe
    assert result.data["selector_probe_summary"]["total_matches"] == 6
    assert result.data["domain_skill_record"]["id"] == record.id


def test_replay_store_builds_disabled_browser_capability(tmp_path):
    replay = BrowserReplayStore(tmp_path)
    replay.append(
        BrowserReplayEvent(
            run_id="run-capability",
            profile_id="research",
            action="open",
            runtime="electron_bridge",
            access="read",
            risk="low",
            decision="allow",
            ok=True,
            url="https://example.com/reports",
        )
    )

    capability = replay.build_capability_from_replay(
        "run-capability",
        profile_id="research",
    )

    assert capability.enabled is False
    assert capability.source_run_id == "run-capability"
    assert capability.steps[0].kind == "browser_prompt"
    assert capability.steps[0].profile_id == "research"
    assert capability.steps[0].target_domains == ["example.com"]
    assert capability.delivery_defaults[-1].type == "wiki_draft"


def test_broker_blocks_scheduled_browser_write(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="wechat",
            name="WeChat",
            allowed_domains=["mp.weixin.qq.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action="click",
            profile_id="wechat",
            url="https://mp.weixin.qq.com/",
            confirmed=True,
            metadata={"scheduled": True},
        )
    )

    assert decision.decision == "block"
    assert decision.matched_rule_id == "browser_scheduled_write_blocked"


def test_broker_treats_download_wait_as_read_observation(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action="download_wait",
            profile_id="research",
            url="https://example.com/report.csv",
            metadata={"scheduled": True},
        )
    )

    assert decision.decision == "allow"
    assert decision.access == "read"


def test_broker_treats_structured_extractors_as_read_observation(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action="extract_table",
            profile_id="research",
            url="https://example.com/report",
        )
    )

    assert decision.decision == "allow"
    assert decision.access == "read"


def test_broker_blocks_manual_takeover_contexts(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="wechat",
            name="WeChat",
            allowed_domains=["mp.weixin.qq.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
    )

    decision = broker.evaluate(
        BrowserAction(
            action="click",
            profile_id="wechat",
            url="https://mp.weixin.qq.com/",
            confirmed=True,
            metadata={
                "requires_manual_takeover": True,
                "page_kind": "publish",
            },
        )
    )

    assert decision.decision == "block"
    assert decision.matched_rule_id == "browser_manual_takeover_required"
    assert "Publishing pages require manual takeover" in decision.reason


def test_wechat_write_detects_manual_takeover_from_snapshot_context():
    observation = BrowserObservation(
        ok=True,
        action="snapshot",
        runtime="electron_bridge",
        profile_id="wechat",
        data={
            "data": {
                "title": "微信公众平台登录",
                "page_context": {
                    "kind": "login",
                    "requires_manual_takeover": True,
                },
            }
        },
    )

    page_context = manual_takeover_context_from_observation(observation)

    assert page_context == {
        "kind": "login",
        "requires_manual_takeover": True,
    }


@pytest.mark.asyncio
async def test_wechat_editor_probe_is_read_only_and_reports_risk_boundaries(
    monkeypatch,
):
    class ProbeBroker:
        def __init__(self) -> None:
            self.actions: list[BrowserAction] = []

        async def execute(self, action: BrowserAction) -> BrowserObservation:
            self.actions.append(action)
            return BrowserObservation(
                ok=True,
                action=action.action,
                runtime="electron_bridge",
                profile_id=action.profile_id,
                page_id=action.page_id,
                url=action.url,
                data={
                    "data": {
                        "page_title": "微信公众平台图文编辑",
                        "url": "https://mp.weixin.qq.com/cgi-bin/appmsg",
                        "read_only": True,
                        "options": [
                            {"id": "cover", "label": "封面", "found": True},
                            {"id": "original", "label": "原创", "found": True},
                            {"id": "comment", "label": "留言", "found": True},
                        ],
                        "risk_buttons": [{"text": "发表"}],
                        "manual_takeover_hints": [],
                        "requires_manual_takeover": False,
                        "safety": {"no_write_actions": True},
                    }
                },
            )

    broker = ProbeBroker()
    monkeypatch.setattr(browser_workbench_router, "_broker", broker)

    result = await probe_wechat_editor_options(
        WechatEditorProbeRequest(
            profile_id="wechat-work",
            page_id="editor-tab",
            url="https://mp.weixin.qq.com/cgi-bin/appmsg",
            run_id="run-wechat-probe",
        )
    )

    assert result["ok"] is True
    assert result["run_id"] == "run-wechat-probe"
    assert result["safety"]["read_only"] is True
    assert result["safety"]["no_write_actions"] is True
    assert result["safety"]["publish_requires_confirmation"] is True
    assert result["probe"]["options"][0]["id"] == "cover"
    assert result["probe"]["risk_buttons"][0]["text"] == "发表"
    assert broker.actions[0].action == "evaluate"
    assert broker.actions[0].access == "read"
    assert broker.actions[0].metadata["probe"] == "wechat_editor_options"
    assert broker.actions[0].metadata["no_write_actions"] is True


@pytest.mark.asyncio
async def test_wechat_write_request_fills_rich_text_and_saves_after_confirmation(
    tmp_path,
    monkeypatch,
):
    content = ContentWorkbenchStore(tmp_path)
    broker = FakeWechatWriteBroker()
    draft = content.create_publishing_draft(
        title="AI 浏览器第一篇",
        summary="写入前摘要",
        body="正文第一段\n正文第二段",
        platform="wechat_official_account",
    )
    queue_item = content.queue_publishing_draft(draft_id=draft.id)
    monkeypatch.setattr(browser_workbench_router, "_content", content)
    monkeypatch.setattr(browser_workbench_router, "_broker", broker)

    result = await browser_workbench_router.request_wechat_draft_write(
        draft.id,
        browser_workbench_router.WechatDraftWriteRequest(
            profile_id="wechat-work",
            confirmed=True,
            run_id="run-wechat-save",
            queue_item_id=queue_item.id,
            open_url="https://mp.weixin.qq.com/cgi-bin/appmsg",
            title_selector="#title",
            body_selector="#editor",
            summary_selector="#summary",
            save_after_fill=True,
            save_selector="button.save-draft",
        ),
    )
    updated_draft = content.load_draft(draft.id)
    updated_queue = content.load_queue_item(queue_item.id)
    fill_action = broker.actions[2]

    assert [action.action for action in broker.actions] == [
        "open",
        "snapshot",
        "fill_form",
        "click",
    ]
    assert result["ok"] is True
    assert result["safety"]["publish_requires_confirmation"] is True
    assert result["safety"]["silent_publish_enabled"] is False
    assert result["safety"]["save_after_fill"] is True
    assert fill_action.confirmed is True
    assert fill_action.access == "write"
    assert fill_action.metadata["fields_json"] == {
        "#title": "AI 浏览器第一篇",
        "#editor": "正文第一段\n正文第二段",
        "#summary": "写入前摘要",
    }
    assert result["observations"][2]["data"]["data"]["details"][1]["mode"] == (
        "contenteditable_insert_text"
    )
    assert updated_draft.status == "saved_to_platform"
    assert updated_draft.metadata["requires_publish_confirmation"] is True
    assert updated_draft.metadata["save_after_fill"] is True
    assert updated_queue.status == "saved"
    assert updated_queue.metadata["wechat_write_run_id"] == "run-wechat-save"


@pytest.mark.asyncio
async def test_wechat_write_request_returns_structured_confirmation_context(
    tmp_path,
    monkeypatch,
):
    class PromptWechatWriteBroker(FakeWechatWriteBroker):
        async def execute(self, action: BrowserAction) -> BrowserObservation:
            if action.action == "fill_form" and not action.confirmed:
                self.actions.append(action)
                return BrowserObservation(
                    ok=False,
                    action=action.action,
                    runtime="electron_bridge",
                    profile_id=action.profile_id,
                    url="https://mp.weixin.qq.com/cgi-bin/appmsg",
                    data={
                        "requires_confirmation": True,
                        "error_code": "browser_write_confirmation_required",
                    },
                    decision=BrowserPolicyDecision(
                        decision="prompt",
                        reason="Browser write actions require user confirmation.",
                        risk="medium",
                        access="write",
                        requires_confirmation=True,
                        matched_rule_id="browser_write_confirmation_required",
                    ),
                )
            return await super().execute(action)

    content = ContentWorkbenchStore(tmp_path)
    broker = PromptWechatWriteBroker()
    draft = content.create_publishing_draft(
        title="需要确认的草稿",
        summary="摘要",
        body="正文",
        platform="wechat_official_account",
    )
    queue_item = content.queue_publishing_draft(draft_id=draft.id)
    monkeypatch.setattr(browser_workbench_router, "_content", content)
    monkeypatch.setattr(browser_workbench_router, "_broker", broker)

    result = await browser_workbench_router.request_wechat_draft_write(
        draft.id,
        browser_workbench_router.WechatDraftWriteRequest(
            profile_id="wechat-work",
            confirmed=False,
            run_id="run-wechat-confirm",
            queue_item_id=queue_item.id,
            open_url="https://mp.weixin.qq.com/cgi-bin/appmsg",
            title_selector="#title",
            body_selector="#editor",
            summary_selector="#summary",
        ),
    )
    updated_queue = content.load_queue_item(queue_item.id)
    fill_action = broker.actions[2]

    assert result["ok"] is False
    assert result["requires_confirmation"] is True
    assert result["confirmation"]["kind"] == "draft_fill"
    assert result["confirmation"]["action_label"] == "填写微信公众号草稿标题、正文和摘要"
    assert result["confirmation"]["field_selectors"] == [
        "#title",
        "#editor",
        "#summary",
    ]
    assert result["confirmation"]["input_summary"] == {
        "title_length": 7,
        "body_length": 2,
        "summary_length": 2,
        "field_count": 3,
    }
    assert result["confirmation"]["silent_publish_enabled"] is False
    assert fill_action.metadata["confirmation_context"]["safe_boundaries"][0] == (
        "本动作只允许写入或保存草稿"
    )
    assert updated_queue.status == "needs_confirmation"


@pytest.mark.asyncio
async def test_wechat_write_request_stops_before_fill_when_manual_takeover_required(
    tmp_path,
    monkeypatch,
):
    content = ContentWorkbenchStore(tmp_path)
    broker = FakeWechatWriteBroker(
        snapshot_context={
            "kind": "login",
            "requires_manual_takeover": True,
        }
    )
    draft = content.create_publishing_draft(
        title="需要登录的草稿",
        body="正文",
        platform="wechat_official_account",
    )
    queue_item = content.queue_publishing_draft(draft_id=draft.id)
    monkeypatch.setattr(browser_workbench_router, "_content", content)
    monkeypatch.setattr(browser_workbench_router, "_broker", broker)

    result = await browser_workbench_router.request_wechat_draft_write(
        draft.id,
        browser_workbench_router.WechatDraftWriteRequest(
            profile_id="wechat-work",
            confirmed=True,
            run_id="run-wechat-login",
            queue_item_id=queue_item.id,
            open_url="https://mp.weixin.qq.com/",
            title_selector="#title",
            body_selector="#editor",
            save_after_fill=True,
            save_selector="button.save-draft",
        ),
    )
    updated_draft = content.load_draft(draft.id)
    updated_queue = content.load_queue_item(queue_item.id)

    assert [action.action for action in broker.actions] == ["open", "snapshot"]
    assert result["ok"] is False
    assert result["requires_manual_takeover"] is True
    assert result["safety"]["manual_takeover_required"] is True
    assert updated_draft.status == "blocked"
    assert updated_draft.metadata["manual_takeover_required"] is True
    assert updated_queue.status == "blocked"
    assert updated_queue.metadata["page_context"]["kind"] == "login"


@pytest.mark.asyncio
async def test_browser_task_runtime_runs_read_task_in_background(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=FakeElectronBridge(),
    )
    runtime = BrowserTaskRuntime(broker=broker, app_store_distribution=lambda: False)

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-read",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/report",
            instruction="Read report in background",
        )
    )
    completed = await wait_for_task_status(runtime, run.run_id, {"completed", "failed"})

    assert completed is not None
    assert completed.status == "completed"
    assert completed.completed_action_count == 3
    assert broker.replay.list_events("task-read")[0]["run_id"] == "task-read"
    artifacts = runtime.artifacts("task-read")
    assert artifacts["event_count"] == 3
    assert artifacts["domain_skill_candidate"]["domain"] == "example.com"
    assert artifacts["domain_skill_candidate"]["metadata"]["run_id"] == "task-read"
    assert any(event.type == "task_completed" for event in runtime.list_events("task-read"))


@pytest.mark.asyncio
async def test_browser_task_write_lock_covers_all_target_domains(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com", "admin.example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=FakeElectronBridge(),
    )
    runtime = BrowserTaskRuntime(broker=broker, app_store_distribution=lambda: False)

    first = await runtime.create(
        BrowserTaskRequest(
            run_id="task-write-multi-domain",
            profile_id="research",
            target_domains=["example.com", "admin.example.com"],
            access="write",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/editor",
                    selector="#save",
                    access="write",
                )
            ],
        )
    )
    second = await runtime.create(
        BrowserTaskRequest(
            run_id="task-write-overlap",
            profile_id="research",
            target_domains=["admin.example.com"],
            access="write",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://admin.example.com/editor",
                    selector="#save",
                    access="write",
                )
            ],
        )
    )

    assert first.metadata["write_locks"] == [
        {"profile_id": "research", "domain": "example.com"},
        {"profile_id": "research", "domain": "admin.example.com"},
    ]
    assert second.status == "failed"
    assert second.error_code == "browser_task_write_lock_conflict"

    runtime.cancel(first.run_id)


@pytest.mark.asyncio
async def test_browser_task_runtime_records_shadow_recovery_for_manual_takeover(
    tmp_path,
):
    replay = BrowserReplayStore(tmp_path)
    broker = FakeFailingTaskBroker(
        replay,
        error="Shadow root is closed or unavailable.",
        data={"scope": {"error": "Shadow root is closed or unavailable."}},
    )
    runtime = BrowserTaskRuntime(broker=broker, app_store_distribution=lambda: False)

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-shadow-failed",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/app",
                    selector="#save",
                    access="read",
                    metadata={"shadow_host_selector": "editor-shell"},
                )
            ],
        )
    )
    stopped = await wait_for_task_status(
        runtime,
        run.run_id,
        {"manual_takeover_required", "failed"},
    )

    assert stopped is not None
    assert stopped.status == "manual_takeover_required"
    assert stopped.error_code == "browser_shadow_root_unavailable"
    recovery = stopped.metadata["last_recovery"]
    assert recovery["kind"] == "shadow_unavailable"
    assert recovery["manual_takeover_required"] is True
    assert recovery["scope"] == {"shadow_host_selector": "editor-shell"}
    artifacts = runtime.artifacts(run.run_id)
    assert artifacts["recovery"]["error_code"] == "browser_shadow_root_unavailable"
    assert artifacts["recoveries"][0]["kind"] == "shadow_unavailable"


@pytest.mark.asyncio
async def test_browser_task_runtime_records_recoverable_element_failure(tmp_path):
    replay = BrowserReplayStore(tmp_path)
    broker = FakeFailingTaskBroker(
        replay,
        error="Element not found.",
    )
    runtime = BrowserTaskRuntime(broker=broker, app_store_distribution=lambda: False)

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-element-missing",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/app",
                    selector="#missing",
                    access="read",
                )
            ],
        )
    )
    stopped = await wait_for_task_status(runtime, run.run_id, {"failed"})

    assert stopped is not None
    assert stopped.status == "failed"
    assert stopped.error_code == "browser_element_not_found"
    recovery = stopped.metadata["last_recovery"]
    assert recovery["kind"] == "element_not_found"
    assert recovery["recoverable"] is True
    assert recovery["manual_takeover_required"] is False
    assert recovery["selector"] == "#missing"


@pytest.mark.asyncio
async def test_browser_task_artifacts_include_domain_skill_preflight_results(
    tmp_path,
):
    replay = BrowserReplayStore(tmp_path)
    runtime = BrowserTaskRuntime(
        broker=FakePreflightBroker(replay),
        app_store_distribution=lambda: False,
    )

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-preflight",
            profile_id="wechat-work",
            target_domains=["mp.weixin.qq.com"],
            access="read",
            actions=[
                BrowserAction(
                    action="evaluate",
                    profile_id="wechat-work",
                    url="https://mp.weixin.qq.com/cgi-bin/appmsg",
                    access="read",
                    metadata={
                        "domain_skill_id": "mp.weixin.qq.com:wechat-editor",
                        "domain_skill_preflight": "selector_probe",
                    },
                )
            ],
        )
    )
    completed = await wait_for_task_status(runtime, run.run_id, {"completed"})

    assert completed is not None
    artifacts = runtime.artifacts(run.run_id)
    assert artifacts["preflights"] == [
        {
            "type": "selector_probe",
            "ok": True,
            "action": "evaluate",
            "domain_skill_id": "mp.weixin.qq.com:wechat-editor",
            "matched_selector_count": 1,
            "selector_count": 2,
            "selectors": [
                {"selector": "button.save-draft", "count": 0, "error": ""},
                {
                    "selector": "#title",
                    "count": 4,
                    "error": "",
                    "scope_counts": {
                        "document": 1,
                        "iframe": 2,
                        "shadow": 1,
                    },
                    "blocked_frame_count": 1,
                    "scopes": [
                        {
                            "kind": "document",
                            "scope": "document",
                            "count": 1,
                            "blocked": False,
                        },
                        {
                            "kind": "iframe",
                            "scope": "iframe[0]",
                            "count": 2,
                            "blocked": False,
                            "frame_selector": "iframe.editor",
                        },
                        {
                            "kind": "shadow",
                            "scope": "shadow[0]",
                            "count": 1,
                            "blocked": False,
                            "shadow_host_selector": "editor-shell",
                        },
                        {
                            "kind": "iframe",
                            "scope": "iframe[1]",
                            "count": 0,
                            "blocked": True,
                            "frame_selector": "iframe.cross-origin",
                            "error": "cross_origin_or_unavailable",
                        },
                    ],
                },
            ],
            "scope_candidates": [
                {
                    "selector": "#title",
                    "frame_selector": "iframe.editor",
                },
                {
                    "selector": "#title",
                    "shadow_host_selector": "editor-shell",
                },
            ],
        }
    ]


@pytest.mark.asyncio
async def test_browser_task_runtime_plans_lantaicli_adapter_from_instruction(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="github-work",
            name="GitHub Work",
            allowed_domains=["github.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=FakeElectronBridge(),
    )
    runtime = BrowserTaskRuntime(broker=broker, app_store_distribution=lambda: False)

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-planned-issues",
            profile_id="github-work",
            target_domains=["github.com"],
            access="read",
            url="https://github.com/org/repo",
            instruction="读取最近 issue 并整理可行动项",
            max_actions=5,
        )
    )
    completed = await wait_for_task_status(
        runtime,
        run.run_id,
        {"completed", "failed"},
    )

    assert completed is not None
    assert completed.status == "completed"
    assert completed.planned_action_count == 3
    assert completed.metadata["plan"]["strategy"] == "lantaicli_adapter"
    assert completed.metadata["plan"]["adapter_id"] == "github.issues.list"
    replay_events = broker.replay.list_events("task-planned-issues")
    assert replay_events[0]["url"].endswith("/issues?per_page=10")
    assert replay_events[0]["metadata"]["planner_adapter_id"] == "github.issues.list"
    assert replay_events[-1]["action"] == "screenshot"
    artifacts = runtime.artifacts("task-planned-issues")
    candidate = artifacts["domain_skill_candidate"]
    assert candidate["metadata"]["recommended_adapter_id"] == "github.issues.list"
    assert candidate["metadata"]["adapter_ids"] == ["github.issues.list"]


def test_browser_task_planner_respects_explicit_adapter_and_blocks_implicit_write():
    planner = BrowserTaskPlanner()

    explicit = planner.plan(
        BrowserTaskRequest(
            run_id="task-xhs",
            profile_id="social",
            target_domains=["xiaohongshu.com"],
            access="read",
            url="https://www.xiaohongshu.com/explore/1",
            instruction="整理这篇小红书笔记",
        )
    )
    blocked = planner.plan(
        BrowserTaskRequest(
            run_id="task-write-implicit",
            profile_id="wechat-work",
            target_domains=["mp.weixin.qq.com"],
            access="write",
            instruction="写入公众号草稿",
        )
    )

    assert explicit.metadata["adapter_id"] == "xiaohongshu.note.collect"
    assert [action.action for action in explicit.actions] == [
        "open",
        "snapshot",
        "screenshot",
    ]
    assert blocked.actions == []
    assert blocked.metadata["strategy"] == "blocked_without_explicit_actions"


def test_browser_task_planner_uses_enabled_domain_skill_recommended_adapter(
    tmp_path,
):
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")
    registry.save_domain_skill_draft(
        DomainSkillDraft(
            domain="example.com",
            title="Example article skill",
            url_patterns=["https://example.com/articles/{id}"],
            selectors=["main article"],
            wait_conditions=["wait for article body"],
            metadata={
                "recommended_adapter_id": "media.article.extract",
                "scopes": [
                    {
                        "selector": "main article",
                        "frame_selector": "iframe.article",
                        "shadow_host_selector": "article-shell",
                    }
                ],
            },
        )
    )
    record = registry.list_domain_skill_drafts()[0]
    registry.update_domain_skill_draft(
        record.id,
        DomainSkillDraftPatch(enabled=True),
    )
    planner = BrowserTaskPlanner(registry=registry)

    plan = planner.plan(
        BrowserTaskRequest(
            run_id="task-domain-skill",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/articles/123",
            instruction="整理这个页面",
        )
    )

    assert plan.metadata["strategy"] == "domain_skill_adapter"
    assert plan.metadata["adapter_id"] == "media.article.extract"
    assert plan.metadata["domain_skill"]["id"] == (
        "example.com:example-article-skill"
    )
    assert plan.metadata["domain_skill"]["selectors"] == ["main article"]
    assert plan.metadata["domain_skill"]["scopes"] == [
        {
            "selector": "main article",
            "frame_selector": "iframe.article",
            "shadow_host_selector": "article-shell",
        }
    ]
    assert plan.actions[0].metadata["domain_skill_id"] == (
        "example.com:example-article-skill"
    )
    assert plan.actions[-1].action == "screenshot"


def test_browser_task_planner_applies_domain_skill_scope_to_write_action(
    tmp_path,
):
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")
    registry.save_domain_skill_draft(
        DomainSkillDraft(
            domain="mp.weixin.qq.com",
            title="WeChat editor skill",
            url_patterns=["https://mp.weixin.qq.com/cgi-bin/appmsg"],
            selectors=["button.save-draft"],
            metadata={
                "recommended_adapter_id": "wechat.official_account.draft.save",
                "scopes": [
                    {
                        "selector": "button.save-draft",
                        "frame_selector": "iframe.editor",
                        "shadow_host_selector": "editor-shell",
                    }
                ],
            },
        )
    )
    record = registry.list_domain_skill_drafts()[0]
    registry.update_domain_skill_draft(record.id, DomainSkillDraftPatch(enabled=True))
    planner = BrowserTaskPlanner(registry=registry)

    plan = planner.plan(
        BrowserTaskRequest(
            run_id="task-domain-skill-scope",
            profile_id="wechat-work",
            target_domains=["mp.weixin.qq.com"],
            access="write",
            url="https://mp.weixin.qq.com/cgi-bin/appmsg",
            instruction="保存公众号草稿",
            metadata={
                "confirmed": True,
                "adapter_inputs": {
                    "selector": "button.save-draft",
                    "draft_id": "draft-1",
                },
            },
        )
    )

    write_action = next(action for action in plan.actions if action.action == "click")
    assert plan.metadata["strategy"] == "domain_skill_adapter"
    assert write_action.metadata["frame_selector"] == "iframe.editor"
    assert write_action.metadata["shadow_host_selector"] == "editor-shell"
    assert write_action.metadata["domain_skill_scopes"][0]["selector"] == (
        "button.save-draft"
    )


def test_browser_task_planner_adds_recovery_preflight_for_failed_skill(
    tmp_path,
):
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")
    registry.save_domain_skill_draft(
        DomainSkillDraft(
            domain="mp.weixin.qq.com",
            title="WeChat editor recovery skill",
            url_patterns=["https://mp.weixin.qq.com/cgi-bin/appmsg"],
            selectors=["button.save-draft"],
            failure_modes=[
                "element_not_found: browser_element_not_found",
                "Re-run selector probe.",
            ],
            metadata={
                "recommended_adapter_id": "wechat.official_account.draft.save",
            },
        )
    )
    record = registry.list_domain_skill_drafts()[0]
    registry.update_domain_skill_draft(record.id, DomainSkillDraftPatch(enabled=True))
    planner = BrowserTaskPlanner(registry=registry)

    plan = planner.plan(
        BrowserTaskRequest(
            run_id="task-domain-skill-recovery",
            profile_id="wechat-work",
            target_domains=["mp.weixin.qq.com"],
            access="write",
            url="https://mp.weixin.qq.com/cgi-bin/appmsg",
            instruction="保存公众号草稿",
            metadata={
                "confirmed": True,
                "adapter_inputs": {
                    "selector": "button.save-draft",
                    "draft_id": "draft-1",
                },
            },
        )
    )

    preflight = next(
        action
        for action in plan.actions
        if action.metadata.get("domain_skill_preflight") == "selector_probe"
    )
    write_index = next(
        index for index, action in enumerate(plan.actions) if action.action == "click"
    )
    preflight_index = plan.actions.index(preflight)
    assert preflight_index < write_index
    assert preflight.action == "evaluate"
    assert preflight.access == "read"
    assert "button.save-draft" in preflight.code
    assert plan.metadata["domain_skill"]["recovery_guard"] == {
        "failure_kinds": ["element_not_found"],
        "requires_selector_probe": True,
        "requires_scope_probe": False,
        "requires_download_check": False,
        "source": "domain_skill_failure_modes",
    }
    assert plan.actions[write_index].metadata["domain_skill_recovery_guard"][
        "requires_selector_probe"
    ] is True


def test_browser_task_planner_skips_domain_skill_after_failed_probe(
    tmp_path,
):
    registry = LantaiCliAdapterRegistry(domain_skill_root=tmp_path / "skills")
    registry.save_domain_skill_draft(
        DomainSkillDraft(
            domain="example.com",
            title="Broken article skill",
            url_patterns=["https://example.com/articles/{id}"],
            selectors=["main article"],
            metadata={
                "recommended_adapter_id": "media.article.extract",
                "last_selector_probe": {
                    "ok": True,
                    "selector_count": 1,
                    "matched_selector_count": 0,
                },
            },
        )
    )
    record = registry.list_domain_skill_drafts()[0]
    registry.update_domain_skill_draft(
        record.id,
        DomainSkillDraftPatch(enabled=True),
    )
    planner = BrowserTaskPlanner(registry=registry)

    plan = planner.plan(
        BrowserTaskRequest(
            run_id="task-domain-skill-failed-probe",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/articles/123",
            instruction="整理这个页面",
        )
    )

    assert plan.metadata["strategy"] == "lantaicli_adapter"
    assert plan.metadata["adapter_id"] == "media.article.extract"
    assert "domain_skill" not in plan.metadata
    assert all("domain_skill_id" not in action.metadata for action in plan.actions)


@pytest.mark.asyncio
async def test_browser_task_runtime_waits_for_write_confirmation_then_continues(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=FakeElectronBridge(),
    )
    runtime = BrowserTaskRuntime(broker=broker, app_store_distribution=lambda: False)

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-write",
            profile_id="research",
            target_domains=["example.com"],
            access="write",
            instruction="Click save after confirmation",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/form",
                    selector="#save",
                    access="write",
                    run_id="task-write",
                )
            ],
        )
    )
    waiting = await wait_for_task_status(
        runtime,
        run.run_id,
        {"waiting_for_user_confirmation", "failed"},
    )

    assert waiting is not None
    assert waiting.status == "waiting_for_user_confirmation"
    assert waiting.pending_confirmation is not None
    assert waiting.error_code == "browser_write_confirmation_required"

    await runtime.confirm(
        run.run_id,
        BrowserTaskConfirmation(
            approved=True,
            reason="allow once",
        ),
    )
    completed = await wait_for_task_status(runtime, run.run_id, {"completed", "failed"})

    assert completed is not None
    assert completed.status == "completed"
    events = broker.replay.list_events("task-write")
    assert events[-1]["decision"] == "allow"
    assert events[-1]["metadata"]["confirmation_result"] == "approved"


@pytest.mark.asyncio
async def test_browser_task_runtime_denies_confirmation_and_cancels(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=FakeElectronBridge(),
    )
    runtime = BrowserTaskRuntime(broker=broker, app_store_distribution=lambda: False)

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-deny",
            profile_id="research",
            target_domains=["example.com"],
            access="write",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/form",
                    selector="#save",
                    access="write",
                    run_id="task-deny",
                )
            ],
        )
    )
    await wait_for_task_status(runtime, run.run_id, {"waiting_for_user_confirmation"})
    cancelled = await runtime.confirm(
        run.run_id,
        BrowserTaskConfirmation(approved=False, reason="not now"),
    )

    assert cancelled.status == "cancelled"
    assert cancelled.error_code == "browser_task_confirmation_denied"
    assert broker.replay.list_events("task-deny")[-1]["decision"] == "block"


@pytest.mark.asyncio
async def test_browser_takeover_waits_for_the_active_task_safe_point():
    runtime = BrowserTaskRuntime(app_store_distribution=lambda: False)
    run = BrowserTaskRun(
        run_id="task-takeover",
        profile_id="default",
        status="running_background",
    )
    runtime._runs[run.run_id] = run
    release = asyncio.Event()

    async def active_browser_action():
        await release.wait()

    handle = asyncio.create_task(active_browser_action())
    runtime._handles[run.run_id] = handle

    pending = await runtime.request_takeover(run.run_id, timeout_seconds=0.01)

    assert pending.ready is False
    assert pending.run.status == "paused"
    assert pending.error_code == "browser_takeover_action_still_running"

    release.set()
    await handle
    ready = await runtime.request_takeover(run.run_id, timeout_seconds=0.01)

    assert ready.ready is True
    assert ready.run.status == "paused"
    assert ready.error_code == ""


@pytest.mark.asyncio
async def test_browser_takeover_preserves_manual_takeover_state():
    runtime = BrowserTaskRuntime(app_store_distribution=lambda: False)
    run = BrowserTaskRun(
        run_id="task-manual-takeover",
        profile_id="default",
        status="manual_takeover_required",
    )
    runtime._runs[run.run_id] = run

    receipt = await runtime.request_takeover(run.run_id)

    assert receipt.ready is True
    assert receipt.run.status == "manual_takeover_required"


@pytest.mark.asyncio
async def test_browser_task_cancel_during_inflight_action_stays_cancelled(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowBroker:
        replay = BrowserReplayStore(tmp_path)

        async def execute(self, action: BrowserAction) -> BrowserObservation:
            started.set()
            await release.wait()
            return BrowserObservation(
                ok=True,
                action=action.action,
                runtime="electron_bridge",
                profile_id=action.profile_id,
                page_id=action.page_id,
                url=action.url,
                message="late success",
            )

    runtime = BrowserTaskRuntime(
        broker=SlowBroker(),  # type: ignore[arg-type]
        app_store_distribution=lambda: False,
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-cancel-inflight",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            actions=[
                BrowserAction(
                    action="open",
                    profile_id="research",
                    url="https://example.com/report",
                    access="read",
                )
            ],
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    runtime.cancel(run.run_id)
    release.set()
    handle = runtime._handles[run.run_id]  # pylint: disable=protected-access
    await asyncio.wait_for(handle, timeout=1)

    assert runtime.get_run(run.run_id).status == "cancelled"
    assert all(
        event.type != "action_completed"
        for event in runtime.list_events(run.run_id)
        if event.ts >= run.updated_at
    )


@pytest.mark.asyncio
async def test_browser_task_duration_budget_interrupts_inflight_action(tmp_path):
    started = asyncio.Event()

    class SlowBroker:
        replay = BrowserReplayStore(tmp_path)

        def __init__(self) -> None:
            self.cancelled = False
            self.timeout_seconds: float | None = None

        async def execute(self, action: BrowserAction) -> BrowserObservation:
            self.timeout_seconds = float(
                action.metadata["browser_task_action_timeout_seconds"]
            )
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return BrowserObservation(
                ok=True,
                action=action.action,
                runtime="electron_bridge",
                profile_id=action.profile_id,
                page_id=action.page_id,
                url=action.url,
                message="late success",
            )

    broker = SlowBroker()
    runtime = BrowserTaskRuntime(
        broker=broker,  # type: ignore[arg-type]
        app_store_distribution=lambda: False,
    )
    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-duration-timeout",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            max_duration_seconds=1.0,
            actions=[
                BrowserAction(
                    action="open",
                    profile_id="research",
                    url="https://example.com/report",
                    access="read",
                )
            ],
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    failed = await wait_for_task_status(
        runtime,
        run.run_id,
        {"failed"},
        timeout_seconds=2.0,
    )

    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "browser_task_duration_budget_exceeded"
    assert broker.cancelled is True
    assert broker.timeout_seconds is not None
    assert 0 < broker.timeout_seconds <= 1.0
    assert all(
        event.type != "action_completed"
        for event in runtime.list_events(run.run_id)
    )


@pytest.mark.asyncio
async def test_browser_task_runtime_persists_waiting_task_and_resumes_after_restart(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    store = BrowserTaskStore(tmp_path)
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=FakeElectronBridge(),
    )
    runtime = BrowserTaskRuntime(
        broker=broker,
        app_store_distribution=lambda: False,
        store=store,
    )

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-persist",
            profile_id="research",
            target_domains=["example.com"],
            access="write",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/form",
                    selector="#save",
                    access="write",
                    run_id="task-persist",
                )
            ],
        )
    )
    await wait_for_task_status(
        runtime,
        run.run_id,
        {"waiting_for_user_confirmation"},
    )

    restored = BrowserTaskRuntime(
        broker=broker,
        app_store_distribution=lambda: False,
        store=store,
    )
    loaded = restored.get_run("task-persist")

    assert loaded is not None
    assert loaded.status == "waiting_for_user_confirmation"
    assert loaded.pending_confirmation is not None
    assert restored.list_events("task-persist")[-1].type == "action_waiting_for_confirmation"

    await restored.confirm(
        "task-persist",
        BrowserTaskConfirmation(approved=True, reason="resume after restart"),
    )
    completed = await wait_for_task_status(
        restored,
        "task-persist",
        {"completed", "failed"},
    )

    assert completed is not None
    assert completed.status == "completed"
    assert completed.completed_action_count == 1
    assert BrowserTaskStore(tmp_path).load_all()[0][0].status == "completed"


def test_browser_task_store_sanitizes_runtime_metadata(tmp_path):
    store = BrowserTaskStore(tmp_path)
    run = BrowserTaskRun(
        run_id="task-sensitive-store",
        profile_id="research",
        target_domains=["example.com"],
        access="write",
        status="waiting_for_user_confirmation",
        planned_action_count=1,
        pending_confirmation=BrowserAction(
            action="click",
            profile_id="research",
            url="https://example.com/form",
            selector="#save",
            access="write",
            run_id="task-sensitive-store",
            metadata={"api_key": "pending-secret"},
        ),
        metadata={"authorization": "Bearer run-secret"},
        result={"nested": {"password": "result-secret"}},
    )
    event = BrowserTaskEvent(
        run_id=run.run_id,
        type="action_waiting_for_confirmation",
        status=run.status,
        message="waiting",
        metadata={"token": "event-secret"},
    )
    action = BrowserAction(
        action="click",
        profile_id="research",
        url="https://example.com/form",
        selector="#save",
        access="write",
        run_id=run.run_id,
        text="draft-password-value",
        path="/Users/test/secrets/upload.txt",
        metadata={
            "cookie": "action-secret",
            "fields_json": '{"#password":"raw-field-secret"}',
        },
    )

    path = store.save(run, [event], [action], 0)
    raw = path.read_text("utf-8")
    loaded_run, loaded_events, loaded_actions, _next_index = store.load_all()[0]

    assert "run-secret" not in raw
    assert "pending-secret" not in raw
    assert "result-secret" not in raw
    assert "event-secret" not in raw
    assert "action-secret" not in raw
    assert "draft-password-value" not in raw
    assert "raw-field-secret" not in raw
    assert "/Users/test/secrets" not in raw
    assert loaded_run.metadata["data_classification"] == "sensitive"
    assert loaded_run.result["nested"]["password"]["redacted"] is True
    assert loaded_run.pending_confirmation is not None
    assert "api_key" not in loaded_run.pending_confirmation.metadata
    assert "token" not in loaded_events[0].metadata
    assert "cookie" not in loaded_actions[0].metadata
    assert loaded_actions[0].text == ""
    assert loaded_actions[0].path == ""
    assert loaded_actions[0].metadata["stored_path_name"] == "upload.txt"


def test_browser_task_store_rejects_canonical_run_id_path_mismatch(tmp_path):
    store = BrowserTaskStore(tmp_path)
    valid_run = BrowserTaskRun(run_id="task-valid")
    store.save(valid_run, [], [], 0)

    mismatched_path = store._canonical_path_for_run_id(  # pylint: disable=protected-access
        "task-path-owner",
    )
    mismatched_payload = store._payload_for_storage(  # pylint: disable=protected-access
        BrowserTaskRun(run_id="task-embedded-owner"),
        [],
        [],
        0,
    )
    mismatched_path.parent.mkdir(parents=True, exist_ok=True)
    mismatched_path.write_text(
        json.dumps(mismatched_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    runs = [run.run_id for run, _events, _actions, _next_index in store.load_all()]

    assert runs == ["task-valid"]


def test_browser_task_store_prune_ignores_canonical_run_id_path_mismatch(tmp_path):
    store = BrowserTaskStore(tmp_path)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    mismatched_path = store._canonical_path_for_run_id(  # pylint: disable=protected-access
        "task-path-owner",
    )
    mismatched_payload = store._payload_for_storage(  # pylint: disable=protected-access
        BrowserTaskRun(
            run_id="task-embedded-owner",
            status="completed",
            created_at=old_ts,
            updated_at=old_ts,
        ),
        [],
        [],
        0,
    )
    mismatched_path.parent.mkdir(parents=True, exist_ok=True)
    mismatched_path.write_text(
        json.dumps(mismatched_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    result = store.prune(max_runs=10, max_age_days=30)

    assert result["removed_run_ids"] == []
    assert mismatched_path.exists()


def test_browser_task_store_restores_inflight_task_as_paused(tmp_path):
    store = BrowserTaskStore(tmp_path)
    run = BrowserTaskRun(
        run_id="task-running",
        profile_id="research",
        target_domains=["example.com"],
        access="read",
        status="running_background",
        planned_action_count=1,
    )
    action = BrowserAction(
        action="snapshot",
        profile_id="research",
        url="https://example.com",
        access="read",
        run_id="task-running",
    )
    store.save(run, [], [action], 0)

    restored = BrowserTaskRuntime(
        broker=BrowserActionBroker(
            profiles=BrowserProfileService(tmp_path),
            replay=BrowserReplayStore(tmp_path),
        ),
        app_store_distribution=lambda: False,
        store=store,
    )
    loaded = restored.get_run("task-running")

    assert loaded is not None
    assert loaded.status == "paused"
    assert loaded.metadata["restored_after_restart"] is True
    assert loaded.metadata["restored_status"] == "running_background"
    assert restored.list_events("task-running")[-1].type == "task_restored_paused"


@pytest.mark.asyncio
async def test_browser_task_runtime_enforces_action_budget(tmp_path):
    broker = BrowserActionBroker(
        profiles=BrowserProfileService(tmp_path),
        replay=BrowserReplayStore(tmp_path),
        bridge=FakeElectronBridge(),
    )
    runtime = BrowserTaskRuntime(broker=broker, app_store_distribution=lambda: False)

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-budget",
            profile_id="default",
            access="read",
            url="https://example.com",
            max_actions=1,
        )
    )

    assert run.status == "failed"
    assert run.error_code == "browser_task_action_budget_exceeded"


@pytest.mark.asyncio
async def test_browser_task_runtime_enforces_concurrency_limit(tmp_path):
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=FakeElectronBridge(),
    )
    runtime = BrowserTaskRuntime(
        broker=broker,
        app_store_distribution=lambda: False,
        max_concurrent_runs=1,
    )

    first = await runtime.create(
        BrowserTaskRequest(
            run_id="task-active",
            profile_id="research",
            target_domains=["example.com"],
            access="write",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="research",
                    url="https://example.com/form",
                    selector="#save",
                    access="write",
                )
            ],
        )
    )
    await wait_for_task_status(
        runtime,
        first.run_id,
        {"waiting_for_user_confirmation"},
    )

    second = await runtime.create(
        BrowserTaskRequest(
            run_id="task-over-limit",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/report",
        )
    )

    assert second.status == "failed"
    assert second.error_code == "browser_task_concurrency_limit_reached"


def test_browser_task_runtime_cleans_old_terminal_history(tmp_path):
    store = BrowserTaskStore(tmp_path)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    old_run = BrowserTaskRun(
        run_id="task-old",
        profile_id="research",
        status="completed",
        created_at=old_ts,
        updated_at=old_ts,
    )
    active_run = BrowserTaskRun(
        run_id="task-active",
        profile_id="research",
        status="paused",
    )
    store.save(old_run, [], [], 0)
    store.save(active_run, [], [], 0)
    runtime = BrowserTaskRuntime(
        broker=BrowserActionBroker(
            profiles=BrowserProfileService(tmp_path),
            replay=BrowserReplayStore(tmp_path),
        ),
        app_store_distribution=lambda: False,
        store=store,
    )

    result = runtime.cleanup_history(max_runs=10, max_age_days=30)

    assert result["removed_run_ids"] == ["task-old"]
    assert runtime.get_run("task-old") is None
    assert runtime.get_run("task-active") is not None


@pytest.mark.asyncio
async def test_browser_task_runtime_blocks_app_store_write_task(tmp_path):
    broker = BrowserActionBroker(
        profiles=BrowserProfileService(tmp_path),
        replay=BrowserReplayStore(tmp_path),
    )
    runtime = BrowserTaskRuntime(broker=broker, app_store_distribution=lambda: True)

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-mas-write",
            profile_id="default",
            access="write",
            actions=[
                BrowserAction(
                    action="click",
                    profile_id="default",
                    selector="#save",
                    access="write",
                )
            ],
        )
    )

    assert run.status == "failed"
    assert run.error_code == "app_store_browser_task_blocked"


@pytest.mark.asyncio
async def test_browser_task_runtime_preflights_app_store_read_task_with_broker(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "lantai.app.capabilities.policy.is_app_store_distribution",
        lambda: True,
    )
    profiles = BrowserProfileService(tmp_path)
    profiles.upsert_profile(
        BrowserProfile(
            id="research",
            name="Research",
            allowed_domains=["example.com"],
            agent_control_enabled=True,
        )
    )
    broker = BrowserActionBroker(
        profiles=profiles,
        replay=BrowserReplayStore(tmp_path),
        bridge=FakeElectronBridge(),
    )
    runtime = BrowserTaskRuntime(broker=broker, app_store_distribution=lambda: True)

    run = await runtime.create(
        BrowserTaskRequest(
            run_id="task-mas-read",
            profile_id="research",
            target_domains=["example.com"],
            access="read",
            url="https://example.com/report",
            instruction="Read report in background",
        )
    )

    event_types = [event.type for event in runtime.list_events(run.run_id)]

    assert run.status == "failed"
    assert run.error_code == "app_store_browser_automation_blocked"
    assert run.completed_action_count == 0
    assert "task_started" not in event_types
    assert "action_started" not in event_types


@pytest.mark.asyncio
async def test_computer_action_broker_blocks_without_permission_or_target():
    missing_permission = ComputerActionBroker(
        app_store_distribution=lambda: False,
        permission_status=lambda: {"accessibility": False},
    )
    no_target = ComputerActionBroker(
        app_store_distribution=lambda: False,
        permission_status=lambda: {"accessibility": True},
    )

    permission_observation = await missing_permission.execute(
        ComputerAction(action="click_at", x=10, y=20, confirmed=True)
    )
    target_observation = await no_target.execute(
        ComputerAction(action="click_at", x=10, y=20, confirmed=True)
    )

    assert permission_observation.ok is False
    assert permission_observation.data["error_code"] == "computer_permission_missing"
    assert target_observation.ok is False
    assert target_observation.data["error_code"] == "computer_target_required"


@pytest.mark.asyncio
async def test_computer_action_broker_prompts_for_confirmed_write_and_blocks_mas():
    broker = ComputerActionBroker(
        app_store_distribution=lambda: False,
        permission_status=lambda: {"accessibility": True},
    )
    mas = ComputerActionBroker(app_store_distribution=lambda: True)

    prompt = await broker.execute(
        ComputerAction(
            action="type_text",
            app_allowlist=["澜台"],
            text="secret text",
        )
    )
    allowed = await broker.execute(
        ComputerAction(
            action="type_text",
            app_allowlist=["澜台"],
            text="secret text",
            confirmed=True,
        )
    )
    blocked = await mas.execute(ComputerAction(action="list_windows"))

    assert prompt.ok is False
    assert prompt.decision is not None
    assert prompt.decision.decision == "prompt"
    assert prompt.data["text_length"] == len("secret text")
    assert allowed.ok is False
    assert allowed.data["text_length"] == len("secret text")
    assert allowed.data["dispatch_status"] == "not_sent"
    assert allowed.data["effect_status"] == "failed"
    assert allowed.data["error_code"] == "computer_native_adapter_unavailable"
    assert blocked.ok is False
    assert blocked.data["error_code"] == "app_store_computer_automation_blocked"


@pytest.mark.asyncio
async def test_computer_action_broker_calls_native_adapter_after_policy() -> None:
    received: list[ComputerAction] = []

    async def execute_native(action: ComputerAction) -> dict[str, object]:
        received.append(action)
        return {
            "ok": True,
            "message": "Value read back.",
            "dispatch_status": "sent",
            "effect_status": "verified",
        }

    broker = ComputerActionBroker(
        app_store_distribution=lambda: False,
        permission_status=lambda: {"accessibility": True},
        native_executor=execute_native,
    )
    action = ComputerAction(
        action="type_text",
        app_bundle_id="com.apple.TextEdit",
        window_id="42",
        text="澜台",
        confirmed=True,
        metadata={"observation_id": "obs-1"},
    )

    result = await broker.execute(action)

    assert result.ok is True
    assert received == [action]
    assert result.data["effect_status"] == "verified"


def test_registry_includes_content_and_wechat_adapters(tmp_path):
    registry = LantaiCliAdapterRegistry(
        broker=BrowserActionBroker(
            profiles=BrowserProfileService(tmp_path),
            replay=BrowserReplayStore(tmp_path),
        )
    )
    ids = {adapter.id for adapter in registry.list_adapters()}

    assert "content.series.plan" in ids
    assert "xiaohongshu.note.collect" in ids
    assert "wechat.official_account.draft.fill" in ids


def test_registry_verifies_adapter_action_plan(tmp_path):
    registry = LantaiCliAdapterRegistry(
        broker=BrowserActionBroker(
            profiles=BrowserProfileService(tmp_path),
            replay=BrowserReplayStore(tmp_path),
        )
    )

    result = registry.verify_adapter(
        "wechat.official_account.draft.fill",
    )

    assert result["ok"] is True
    assert result["adapter_id"] == "wechat.official_account.draft.fill"
    assert result["planned_actions"][-1]["requires_confirmation"] is True
    assert any("requires explicit confirmation" in item for item in result["warnings"])


def test_registry_uses_structured_extractors_for_table_and_list(tmp_path):
    registry = LantaiCliAdapterRegistry(
        broker=BrowserActionBroker(
            profiles=BrowserProfileService(tmp_path),
            replay=BrowserReplayStore(tmp_path),
        )
    )

    table = registry.verify_adapter(
        "web.table.extract",
        AdapterRunRequest(
            profile_id="default",
            inputs={"url": "https://example.com/table", "limit": 3},
        ),
    )
    listing = registry.verify_adapter(
        "web.list.extract",
        AdapterRunRequest(
            profile_id="default",
            inputs={"url": "https://example.com/list", "limit": 20},
        ),
    )

    assert table["ok"] is True
    assert table["planned_actions"][-1]["action"] == "extract_table"
    assert listing["ok"] is True
    assert listing["planned_actions"][-1]["action"] == "extract_list"


def test_content_workbench_store_builds_dossier_and_queue(tmp_path):
    store = ContentWorkbenchStore(tmp_path)
    evidence = store.root_dir.parent / "evidence_bundles" / "shot.png"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_bytes(b"\x89PNG\r\n\x1a\n")
    source = ContentSource(
        source_type="web_article",
        title="Sample",
        url="https://example.com/a",
        domain="example.com",
        summary="A useful source.",
        key_points=["Fact one", "Fact two"],
        evidence_paths=[str(evidence)],
        metadata={"api_key": "source-secret"},
    )

    dossier = store.build_dossier(topic="AI 浏览器", sources=[source])
    plan = store.create_series_plan(
        title="AI 浏览器系列",
        article_count=3,
        start_date="2026-06-02",
        publish_time="08:30",
    )
    draft = store.create_publishing_draft(
        title="第一篇",
        body="正文",
        platform="wechat_official_account",
        source_dossier_id=dossier.id,
        metadata={"authorization": "Bearer draft-secret"},
    )
    queue_item = store.queue_publishing_draft(draft_id=draft.id)
    updated = store.update_publishing_draft(
        draft.id,
        status="write_requested",
        platform_url="https://mp.weixin.qq.com/",
        metadata={"wechat_write_run_id": "run-write"},
    )
    queue_detail = store.get_publishing_queue_detail(queue_item.id)
    updated_queue = store.update_publishing_queue_item(
        queue_item.id,
        status="ready_to_write",
        note="用户确认可进入后台草稿写入",
    )
    source_detail = store.get_source_detail(source.id)
    source_asset = store.get_source_evidence_asset(source.id, 0)
    source_markdown = store.export_source_markdown(source.id)
    dossier_markdown = store.export_dossier_markdown(dossier.id)
    draft_markdown = store.export_draft_markdown(draft.id)
    wiki_export = store.import_markdown_export_to_wiki(
        dossier_markdown,
        workspace_dir=tmp_path / "wiki-workspace",
        agent_id="test-agent",
    )

    assert dossier.facts == ["Fact one", "Fact two"]
    assert plan.article_count == 3
    assert plan.calendar[0]["scheduled_for"] == "2026-06-02T08:30:00"
    assert plan.calendar[1]["scheduled_for"] == "2026-06-03T08:30:00"
    assert plan.metadata["scheduled_browser_write_enabled"] is False
    assert draft.platform == "wechat_official_account"
    assert queue_item.requires_confirmation is True
    assert updated.status == "write_requested"
    assert updated.metadata["wechat_write_run_id"] == "run-write"
    assert queue_detail["draft"]["id"] == draft.id
    assert queue_detail["preview"]["body_length"] == len("正文")
    assert queue_detail["diff"]["status_changed"] is True
    assert updated_queue.status == "ready_to_write"
    assert updated_queue.requires_confirmation is False
    assert updated_queue.metadata["history"][-1]["note"] == "用户确认可进入后台草稿写入"
    assert source_detail["source"]["id"] == source.id
    assert source_detail["evidence"]["evidence_count"] == 1
    assert source_detail["evidence"]["assets"][0]["exists"] is True
    assert source_detail["evidence"]["assets"][0]["is_image"] is True
    assert source_detail["evidence"]["assets"][0]["preview_url"].endswith(
        f"/content/sources/{source.id}/evidence/0"
    )
    assert source_asset.is_file()
    assert store.evidence_assets_dir in source_asset.parents
    assert source_detail["related_dossiers"][0]["id"] == dossier.id
    assert store.list_sources()[0]["url"] == "https://example.com/a"
    assert store.list_dossiers()[0]["id"] == dossier.id
    assert store.list_drafts()[0]["id"] == draft.id
    assert store.list_queue()[0]["draft_id"] == draft.id
    assert store.list_queue()[0]["schedule"]["due_status"] == "unscheduled"
    assert "https://example.com/a" in source_markdown.read_text("utf-8")
    assert "shot.png" in source_markdown.read_text("utf-8")
    assert "source-secret" not in source_markdown.read_text("utf-8")
    assert "draft-secret" not in draft_markdown.read_text("utf-8")
    manifest = json.loads(artifact_manifest_path(tmp_path).read_text("utf-8"))
    artifacts = manifest["artifacts"]
    kinds = {
        item["metadata"]["content_artifact_kind"]
        for item in artifacts
        if item.get("producer") == "browser_workbench_content"
    }
    assert {
        "content_source",
        "content_dossier",
        "content_series_plan",
        "publishing_draft",
        "publishing_queue_item",
        "source_evidence_asset",
        "source_markdown_export",
        "dossier_markdown_export",
        "draft_markdown_export",
    }.issubset(kinds)
    assert all(
        item["retention"] == "user_saved"
        for item in artifacts
        if item.get("producer") == "browser_workbench_content"
    )
    assert "https://example.com/a" in dossier_markdown.read_text("utf-8")
    assert "## Draft" in draft_markdown.read_text("utf-8")
    assert wiki_export["ok"] is True
    assert wiki_export["source"]["path"].startswith(".wiki/raw/sources/")


def test_content_workbench_creates_research_pack_from_replay(tmp_path):
    store = ContentWorkbenchStore(tmp_path)
    screenshot = tmp_path / "xiaohongshu.png"
    screenshot.write_bytes(b"png")
    events = [
        BrowserReplayEvent(
            run_id="run-research",
            profile_id="xhs-profile",
            action="snapshot",
            runtime="electron_bridge",
            access="read",
            risk="low",
            decision="allow",
            ok=True,
            url="https://www.xiaohongshu.com/explore/abc",
            screenshot_path=str(screenshot),
            metadata={
                "adapter_id": "xiaohongshu.note.collect",
                "observation_summary": {
                    "title": "小红书笔记样例",
                    "text_length": 3200,
                    "links_count": 8,
                    "text_excerpt": "这是一条可用于选题分析的小红书笔记。",
                    "page_context": {
                        "kind": "normal",
                        "requires_manual_takeover": False,
                    },
                },
            },
        ).model_dump(mode="json"),
        BrowserReplayEvent(
            run_id="run-research",
            profile_id="xhs-profile",
            action="screenshot",
            runtime="electron_bridge",
            access="read",
            risk="low",
            decision="allow",
            ok=True,
            url="https://www.xiaohongshu.com/explore/abc",
            screenshot_path=str(screenshot),
        ).model_dump(mode="json"),
    ]

    result = store.create_replay_research_pack(
        run_id="run-research",
        events=events,
        topic="小红书选题观察",
        profile_id="xhs-profile",
    )

    assert result["ok"] is True
    assert len(result["sources"]) == 1
    source = result["sources"][0]
    assert source["source_type"] == "social_post"
    assert source["metadata"]["adapter_candidate"] == "xiaohongshu.note.collect"
    assert source["metadata"]["actions"] == ["snapshot", "screenshot"]
    assert "已保留截图证据" in source["key_points"]
    assert result["dossier"]["topic"] == "小红书选题观察"
    assert result["workflow_run"]["workflow"] == "browser_research_pack"
    assert result["workflow_run"]["metadata"]["source_replay_run_id"] == "run-research"
    assert Path(result["export_paths"]["dossier"]).is_file()


def test_content_industry_smoke_closes_research_to_draft_wiki_and_skill_loop(
    tmp_path,
):
    replay = BrowserReplayStore(tmp_path)
    store = ContentWorkbenchStore(tmp_path)
    evidence_dir = store.root_dir.parent / "evidence_bundles" / "content-smoke"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    fixtures = [
        {
            "url": "https://news.example.com/report/2026/browser-workbench",
            "title": "媒体报道：Agentic Browser Workbench",
            "excerpt": "媒体页梳理了浏览器自动化、资料证据和内容生产之间的关系。",
            "adapter_id": "media.article.extract",
            "selector": "article",
        },
        {
            "url": "https://www.xiaohongshu.com/explore/content-smoke",
            "title": "小红书笔记：自媒体选题观察",
            "excerpt": "笔记讨论创作者如何从评论、收藏和竞品笔记里发现系列选题。",
            "adapter_id": "xiaohongshu.note.collect",
            "selector": "[data-note-id]",
        },
        {
            "url": "https://www.douyin.com/video/7123456789012345678",
            "title": "抖音视频：内容运营流程",
            "excerpt": "视频页面提供了短视频脚本、评论反馈和二次创作角度。",
            "adapter_id": "douyin.video.research",
            "selector": "video",
        },
    ]
    events: list[dict[str, object]] = []
    for index, item in enumerate(fixtures, start=1):
        screenshot = evidence_dir / f"{index:02d}.png"
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\n")
        for action in ("open", "snapshot", "screenshot"):
            event = BrowserReplayEvent(
                run_id="run-content-smoke",
                profile_id="content-research",
                action=action,
                runtime="electron_bridge",
                access="read",
                risk="low",
                decision="allow",
                ok=True,
                url=str(item["url"]),
                screenshot_path=str(screenshot) if action != "open" else None,
                metadata={
                    "adapter_id": item["adapter_id"],
                    "selector_value": item["selector"],
                    "observation_summary": {
                        "title": item["title"],
                        "text_length": 2400 + index * 100,
                        "links_count": 5 + index,
                        "text_excerpt": item["excerpt"],
                        "page_context": {
                            "kind": "normal",
                            "requires_manual_takeover": False,
                        },
                    },
                },
            )
            replay.append(event)
            events.append(event.model_dump(mode="json"))

    research_pack = store.create_replay_research_pack(
        run_id="run-content-smoke",
        events=events,
        topic="内容团队浏览器自动化选题 smoke",
        brief="覆盖媒体页、小红书笔记、短视频页的只读采集闭环。",
        profile_id="content-research",
    )
    sources = [
        ContentSource.model_validate(source)
        for source in research_pack["sources"]
    ]
    source_types = {source.source_type for source in sources}
    adapter_candidates = {
        item["adapter_id"]
        for item in research_pack["workflow_run"]["metadata"]["adapter_candidates"]
    }

    first_detail = store.get_source_detail(sources[0].id)
    first_asset = store.get_source_evidence_asset(sources[0].id, 0)
    series = store.prepare_wechat_series(
        title="AI 浏览器内容运营系列",
        audience="记者、编辑、自媒体和营销人员",
        positioning="可信资料、可核查证据、可执行选题",
        article_count=3,
        topics=[
            "第一篇：为什么内容团队需要浏览器工作台",
            "第二篇：如何把网页、笔记和视频沉淀成资料包",
            "第三篇：如何安全写入公众号后台草稿",
        ],
        sources=sources,
        profile_id="wechat-work",
        start_date="2026-06-03",
        publish_time="08:00",
        metadata={"source_replay_run_id": "run-content-smoke"},
    )
    first_queue_detail = store.get_publishing_queue_detail(
        series["queue_items"][0]["id"]
    )
    dossier_wiki = store.import_markdown_export_to_wiki(
        research_pack["export_paths"]["dossier"],
        workspace_dir=tmp_path / "wiki-workspace",
        agent_id="content-smoke",
    )
    first_draft_export = Path(series["export_paths"]["drafts"][0])
    draft_wiki = store.import_markdown_export_to_wiki(
        first_draft_export,
        workspace_dir=tmp_path / "wiki-workspace",
        agent_id="content-smoke",
    )
    skill_draft = replay.build_domain_skill_draft(
        "run-content-smoke",
        title="内容行业跨站采集",
        summary="从媒体页、社媒笔记和视频页沉淀内容生产资料包。",
    )
    replay_bundle = replay.export_bundle(
        "run-content-smoke",
        approval=_export_approval("export content smoke replay evidence"),
    )

    assert research_pack["ok"] is True
    assert source_types == {"web_article", "social_post", "video_page"}
    assert adapter_candidates == {
        "media.article.extract",
        "xiaohongshu.note.collect",
        "douyin.video.research",
    }
    assert first_detail["evidence"]["evidence_count"] == 1
    assert first_detail["evidence"]["assets"][0]["preview_url"].endswith(
        f"/content/sources/{sources[0].id}/evidence/0"
    )
    assert first_asset.is_file()
    assert store.evidence_assets_dir in first_asset.parents
    assert Path(research_pack["export_paths"]["dossier"]).is_file()
    assert series["safety"]["silent_publish_enabled"] is False
    assert series["safety"]["scheduled_browser_write_enabled"] is False
    assert len(series["drafts"]) == 3
    assert first_queue_detail["schedule"]["auto_browser_write_allowed"] is False
    assert first_queue_detail["safety"]["platform_write_requires_confirmation"] is True
    assert "微信公众号后台" in first_draft_export.read_text("utf-8")
    assert dossier_wiki["ok"] is True
    assert draft_wiki["ok"] is True
    assert skill_draft.domain == "news.example.com"
    assert "https://www.douyin.com/video/{id}" in skill_draft.url_patterns
    assert skill_draft.metadata["action_sequence"].count("snapshot") == 3
    assert Path(replay_bundle["archive_path"]).is_file()


@pytest.mark.asyncio
async def test_research_pack_workflow_browses_urls_through_broker(tmp_path):
    replay = BrowserReplayStore(tmp_path)
    store = ContentWorkbenchStore(tmp_path)
    broker = FakeResearchBroker(replay)

    result = await run_url_research_pack_workflow(
        broker=broker,  # type: ignore[arg-type]
        content=store,
        request=UrlResearchPackRunRequest(
            topic="跨平台选题观察",
            urls=[
                "example.com/article",
                "https://www.bilibili.com/video/BV123",
            ],
            profile_id="research",
            screenshot_each=True,
            run_id="run-url-pack",
        ),
    )

    assert result["ok"] is True
    assert result["run_id"] == "run-url-pack"
    assert result["visited_urls"] == [
        "https://example.com/article",
        "https://www.bilibili.com/video/BV123",
    ]
    assert len(result["observations"]) == 6
    assert result["ok_observation_count"] == 6
    assert result["blocked_observation_count"] == 0
    assert len(result["sources"]) == 2
    assert result["sources"][1]["source_type"] == "video_page"
    assert result["workflow_run"]["workflow"] == "browser_research_pack"
    assert result["safety"]["broker_enforced"] is True
    assert Path(result["export_paths"]["dossier"]).is_file()


def test_content_workbench_prepares_wechat_series_package(tmp_path):
    store = ContentWorkbenchStore(tmp_path)
    source = ContentSource(
        source_type="web_article",
        title="行业观察",
        url="https://example.com/research",
        domain="example.com",
        summary="行业正在变化。",
        key_points=["趋势一", "用户痛点二", "风险三"],
    )

    result = store.prepare_wechat_series(
        title="AI 浏览器运营系列",
        audience="自媒体创作者",
        positioning="实用、克制、可执行",
        article_count=2,
        topics=["第一篇：为什么要做浏览器工作台", "第二篇：如何沉淀资料包"],
        sources=[source],
        profile_id="wechat-work",
        start_date="2026-06-03",
        publish_time="07:45",
    )

    assert result["safety"]["silent_publish_enabled"] is False
    assert result["safety"]["scheduled_browser_write_enabled"] is False
    assert result["series_plan"]["article_count"] == 2
    assert result["series_plan"]["calendar"][0]["scheduled_for"] == "2026-06-03T07:45:00"
    assert result["series_plan"]["calendar"][1]["scheduled_for"] == "2026-06-04T07:45:00"
    assert len(result["drafts"]) == 2
    assert len(result["queue_items"]) == 2
    assert result["workflow_run"]["workflow"] == "wechat_official_account_series"
    assert result["queue_items"][0]["requires_confirmation"] is True
    assert result["queue_items"][0]["status"] == "scheduled"
    assert result["queue_items"][0]["scheduled_for"] == "2026-06-03T07:45:00"
    assert result["queue_items"][0]["metadata"]["scheduled_browser_write_enabled"] is False
    assert "微信公众号后台" in store.load_draft(result["drafts"][0]["id"]).body
    queue_detail = store.get_publishing_queue_detail(result["queue_items"][0]["id"])
    assert queue_detail["schedule"]["scheduled_for"] == "2026-06-03T07:45:00"
    assert queue_detail["schedule"]["auto_browser_write_allowed"] is False
    assert store.list_workflow_runs()[0]["metadata"]["series_plan_id"] == result["series_plan"]["id"]
