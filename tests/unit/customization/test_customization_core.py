# -*- coding: utf-8 -*-
"""Behavior locks for the customization platform core."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lantai.customization.models import (
    ChangeOperation,
    ChangePlan,
    CustomizationProfile,
    ProfileScope,
)
from lantai.customization.registry import CustomizationRegistry
from lantai.customization.resolver import resolve_effective_state
from lantai.customization.store import CustomizationStore, RevisionConflictError
from lantai.customization.import_export import export_template, import_template
from lantai.customization.impact import preview_plan
from lantai.customization.service import CustomizationService


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def test_bundled_registry_builds_a_factory_profile() -> None:
    registry = CustomizationRegistry.load_bundled()

    assert registry.get("appearance.palette").factory_value == "mist"
    assert registry.get("conversation.show_thinking").factory_value is True
    assert registry.get("core.runtime_facts").merge_strategy == "immutable"
    assert registry.factory_values()["navigation.sidebar.chat.visible"] is True
    assert "navigation.sidebar.tasks.visible" not in registry.factory_values()
    assert registry.factory_values()["navigation.sidebar.tools.visible"] is True
    assert len(registry.items) >= 20


def test_resolver_applies_scope_precedence_and_permission_intersection() -> None:
    registry = CustomizationRegistry.load_bundled()
    global_profile = CustomizationProfile(
        id="profile_global",
        name="Global",
        scope=ProfileScope(kind="global"),
        overrides={
            "appearance.palette": "warm",
            "permissions.tools.allowed": ["read_file", "write_file"],
        },
    )
    project_profile = CustomizationProfile(
        id="profile_project",
        name="Project",
        scope=ProfileScope(kind="project", ref="project-a"),
        overrides={
            "appearance.palette": "violet",
            "permissions.tools.allowed": ["read_file"],
        },
    )

    effective = resolve_effective_state(
        registry,
        [global_profile, project_profile],
    )

    assert effective.values["appearance.palette"] == "violet"
    assert effective.values["permissions.tools.allowed"] == ["read_file"]
    assert effective.sources["appearance.palette"].profile_id == "profile_project"


def test_resolver_never_allows_an_immutable_override() -> None:
    registry = CustomizationRegistry.load_bundled()
    profile = CustomizationProfile(
        id="profile_bad",
        name="Bad",
        overrides={"core.runtime_facts": False},
    )

    effective = resolve_effective_state(registry, [profile])

    assert effective.values["core.runtime_facts"] is True
    assert "core.runtime_facts" in effective.ignored


def test_store_has_five_slots_and_factory_does_not_consume_one(tmp_path) -> None:
    store = CustomizationStore(tmp_path)

    slots = store.list_slots()

    assert [slot.index for slot in slots] == [1, 2, 3, 4, 5]
    assert all(slot.profile_id is None for slot in slots)
    assert store.get_state().mode == "factory"
    assert store.get_state().active_profile_id is None


def test_v1_upgrade_fixture_preserves_customization_identity_and_effective_state(
    tmp_path,
) -> None:
    source = FIXTURES / "customization_v1"
    root = tmp_path / "customization"
    shutil.copytree(source, root)
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*.json")
    }

    service = CustomizationService(root)
    state = service.store.get_state()
    profile = service.store.get_profile("profile_v1_upgrade_fixture")
    effective = service.store.get_effective_state(service.registry)
    slots = service.store.list_slots()
    revisions = service.store.list_revisions(profile.id)

    assert state.mode == "normal"
    assert state.active_profile_id == profile.id
    assert state.last_known_good == {profile.id: 2}
    assert state.pending_boot_validation == {profile.id: 3}
    assert profile.revision == 3
    assert slots[0].profile_id == profile.id
    assert [slot.profile_id for slot in slots[1:]] == [None, None, None, None]
    assert revisions[0].revision == 3
    assert effective.values["appearance.palette"] == "warm"
    assert effective.values["appearance.composer_size"] == "spacious"
    assert effective.values["conversation.show_thinking"] is True
    assert effective.values["persona.presence"] == "quiet"
    after = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*.json")
    }
    assert after == before


def test_apply_plan_creates_revision_audit_and_lkg(tmp_path) -> None:
    store = CustomizationStore(tmp_path)
    profile = store.create_profile(name="夜航", icon="moon")
    plan = ChangePlan(
        target_profile_id=profile.id,
        base_revision=profile.revision,
        intent="换成暖色",
        operations=[
            ChangeOperation(op="set", path="appearance.palette", value="warm")
        ],
    )

    result = store.apply_plan(
        plan,
        CustomizationRegistry.load_bundled(),
        actor="user",
        health_check=lambda _state: {"ok": True, "checks": ["schema"]},
    )

    updated = store.get_profile(profile.id)
    assert result.status == "committed"
    assert updated.revision == 1
    assert updated.overrides["appearance.palette"] == "warm"
    assert store.get_state().last_known_good[profile.id] == 0
    assert store.get_state().pending_boot_validation[profile.id] == 1
    store.accept_boot_health()
    assert store.get_state().last_known_good[profile.id] == 1
    assert profile.id not in store.get_state().pending_boot_validation
    assert store.list_revisions(profile.id)[0].intent == "换成暖色"
    journal = [json.loads(line) for line in store.journal_path.read_text().splitlines()]
    transaction_event = next(
        event for event in reversed(journal) if event.get("event") == "transaction"
    )
    assert transaction_event["status"] == "committed"


def test_apply_plan_rejects_stale_revision(tmp_path) -> None:
    store = CustomizationStore(tmp_path)
    profile = store.create_profile(name="冲突")
    registry = CustomizationRegistry.load_bundled()
    first = ChangePlan(
        target_profile_id=profile.id,
        base_revision=0,
        operations=[
            ChangeOperation(op="set", path="appearance.palette", value="warm")
        ],
    )
    store.apply_plan(first, registry)

    with pytest.raises(RevisionConflictError):
        store.apply_plan(first, registry)


def test_failed_health_check_rolls_back_profile(tmp_path) -> None:
    store = CustomizationStore(tmp_path)
    profile = store.create_profile(name="回滚")
    plan = ChangePlan(
        target_profile_id=profile.id,
        base_revision=0,
        operations=[
            ChangeOperation(op="set", path="appearance.palette", value="violet")
        ],
    )

    result = store.apply_plan(
        plan,
        CustomizationRegistry.load_bundled(),
        health_check=lambda _state: {"ok": False, "checks": ["shell"]},
    )

    assert result.status == "rolled_back"
    restored = store.get_profile(profile.id)
    assert restored.revision == 0
    assert "appearance.palette" not in restored.overrides


def test_undo_restores_revision_zero_without_rewriting_history(tmp_path) -> None:
    store = CustomizationStore(tmp_path)
    profile = store.create_profile(name="可撤销")
    registry = CustomizationRegistry.load_bundled()
    store.apply_plan(
        ChangePlan(
            target_profile_id=profile.id,
            base_revision=0,
            intent="换色",
            operations=[
                ChangeOperation(op="set", path="appearance.palette", value="warm")
            ],
        ),
        registry,
    )

    result = store.undo_last(profile.id, registry)

    restored = store.get_profile(profile.id)
    assert result.status == "committed"
    assert restored.revision == 2
    assert restored.overrides == {}
    assert [record.revision for record in store.list_revisions(profile.id)] == [2, 1]


def test_interrupted_transaction_is_restored_from_snapshot(tmp_path) -> None:
    store = CustomizationStore(tmp_path)
    profile = store.create_profile(name="断电恢复")
    changed = profile.model_copy(deep=True)
    changed.revision = 1
    changed.overrides["appearance.palette"] = "violet"
    transaction_id = "tx_interrupted"
    from lantai.customization.store import _atomic_write

    _atomic_write(store.snapshots_dir / f"{transaction_id}.json", profile)
    _atomic_write(store._profile_path(profile.id), changed)
    _atomic_write(
        store.transactions_dir / f"{transaction_id}.json",
        {
            "id": transaction_id,
            "profile_id": profile.id,
            "status": "prepared",
        },
    )

    recovered = CustomizationStore(tmp_path)

    assert recovered.get_profile(profile.id).revision == 0
    transaction = recovered._read_json(
        recovered.transactions_dir / f"{transaction_id}.json", {}
    )
    assert transaction["status"] == "rolled_back"
    assert transaction["recovered_after_interruption"] is True


def test_corrupt_state_is_quarantined_and_factory_boots(tmp_path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text("{broken", encoding="utf-8")

    store = CustomizationStore(tmp_path)

    assert store.get_state().mode == "factory"
    assert list((tmp_path / "quarantine" / "corrupt").glob("state.json.*.json"))


def test_template_export_redacts_local_references_and_import_is_inactive(tmp_path) -> None:
    registry = CustomizationRegistry.load_bundled()
    store = CustomizationStore(tmp_path)
    profile = store.create_profile(
        name="可分享",
        overrides={
            "appearance.palette": "warm",
            "permissions.filesystem.allowed_roots": ["/Users/private"],
        },
    )

    template = export_template(profile, registry)
    imported, report = import_template(template, store=store, registry=registry)

    assert template["profile"]["overrides"] == {"appearance.palette": "warm"}
    assert template["requirements"][0]["item_id"] == (
        "permissions.filesystem.allowed_roots"
    )
    assert imported.overrides == {"appearance.palette": "warm"}
    assert report["activated"] is False
    assert store.get_state().active_profile_id is None


def test_template_import_rejects_secrets_paths_and_external_urls(tmp_path) -> None:
    registry = CustomizationRegistry.load_bundled()
    store = CustomizationStore(tmp_path)
    payload = {
        "format": "lantai-profile",
        "format_version": 1,
        "profile": {
            "name": "恶意模板",
            "overrides": {
                "delivery.naming_pattern": "sk-supersecret123456789",
                "persona.user_address": "/Users/alice/private",
                "appearance.design_tokens": {"--lt-accent": "https://bad.test"},
                "appearance.palette": "warm",
            },
        },
    }

    profile, report = import_template(payload, store=store, registry=registry)

    assert profile.overrides == {"appearance.palette": "warm"}
    assert report["ignored"]["delivery.naming_pattern"] == "suspected_secret"
    assert report["ignored"]["persona.user_address"] == "machine_path"
    assert report["ignored"]["appearance.design_tokens"] == "external_url"


def test_unsafe_design_token_is_rejected() -> None:
    registry = CustomizationRegistry.load_bundled()
    item = registry.get("appearance.design_tokens")

    with pytest.raises(ValueError):
        registry.validate_value(item, {"--lt-bg": "url(https://bad.test/x)"})


def test_lkg_boot_recovery_runs_once_and_creates_audited_revision(
    tmp_path, monkeypatch
) -> None:
    service = CustomizationService(tmp_path)
    profile = service.store.create_profile(name="启动恢复")
    service.store.activate_profile(profile.id)
    first = ChangePlan(
        target_profile_id=profile.id,
        base_revision=0,
        operations=[
            ChangeOperation(op="set", path="appearance.palette", value="warm")
        ],
    )
    service.store.apply_plan(first, service.registry)
    service.store.accept_boot_health()
    current = service.store.get_profile(profile.id)
    service.store.apply_plan(
        ChangePlan(
            target_profile_id=profile.id,
            base_revision=current.revision,
            operations=[
                ChangeOperation(op="set", path="appearance.palette", value="violet")
            ],
        ),
        service.registry,
    )
    monkeypatch.setenv("LANTAI_CUSTOMIZATION_RECOVER_LKG", "1")
    monkeypatch.setenv("LANTAI_CUSTOMIZATION_BOOT_ID", "boot_test")

    CustomizationService(tmp_path)
    recovered = CustomizationService(tmp_path)

    assert recovered.store.get_profile(profile.id).overrides["appearance.palette"] == "warm"
    assert recovered.store.get_profile(profile.id).revision == 3
    assert recovered.store.list_revisions(profile.id)[0].source == "revision_restore"


def test_deleting_inactive_profile_removes_only_its_customization_history(tmp_path) -> None:
    store = CustomizationStore(tmp_path)
    registry = CustomizationRegistry.load_bundled()
    profile = store.create_profile(name="待删除")
    other = store.create_profile(name="保留")
    plan = ChangePlan(
        target_profile_id=profile.id,
        base_revision=0,
        operations=[
            ChangeOperation(op="set", path="appearance.palette", value="warm")
        ],
    )
    store.save_plan(plan, preview_plan(profile, plan, registry))
    receipt = store.confirm_plan(plan.id, actor="user")
    result = store.apply_saved_plan(plan.id, registry, actor="user")
    store.set_slot(1, profile_id=profile.id)

    store.delete_profile(profile.id)

    assert all(slot.profile_id != profile.id for slot in store.list_slots())
    assert not (store.revisions_dir / profile.id).exists()
    assert not (store.snapshots_dir / f"{result.id}.json").exists()
    assert not (store.transactions_dir / f"{result.id}.json").exists()
    assert not (store.plans_dir / f"{plan.id}.json").exists()
    assert not (store.confirmations_dir / f"{receipt['token']}.json").exists()
    assert store.get_profile(other.id).name == "保留"
