from datetime import date
import hashlib

import pytest

from lantai.app.daily_brief.activation import DailyBriefActivationService
from lantai.app.daily_brief.models import (
    BriefActivationRequest, BriefFeedbackCreateRequest, BriefDiscussionDraftCreateRequest,
)
from lantai.app.daily_brief.repo import DailyBriefRepository
from lantai.app.daily_brief.service import BriefConflict, ConsentRequired
from tests.unit.app.test_daily_brief_domain import (
    _provision_subscription, _ready_publish_request, _seed_topic,
)


async def _edition(tmp_path):
    service, _, _, subscription = await _provision_subscription(tmp_path)
    cycle = await service.claim_occurrence(subscription_id=subscription.id,
        occurrence_id="first", local_date=date(2026, 10, 3))
    return service, cycle, await service.publish(_ready_publish_request(cycle))


@pytest.mark.asyncio
async def test_failed_attempt_has_safe_actionable_workbench_status(tmp_path):
    service, _, _, subscription = await _provision_subscription(tmp_path)
    cycle = await service.claim_occurrence(subscription_id=subscription.id,
        occurrence_id="first", local_date=date(2026, 10, 3))
    await service.fail_cycle(cycle.id, "interrupted")
    retry = await service.retry_cycle(cycle.id, explicit_consent=True, action_id="again")
    await service.fail_cycle(retry.id, "RuntimeError: https://host/?api_key=secret-private")
    snapshot = await service.get_workspace_snapshot()
    view = next(item for item in snapshot.cycles if item.id == retry.id)
    assert view.status == "failed"
    assert view.retryable
    assert view.failure_code == "research_failed"
    assert "secret-private" not in view.model_dump_json()


@pytest.mark.asyncio
async def test_read_watermark_is_revision_specific_and_idempotent(tmp_path):
    service, _, edition = await _edition(tmp_path)
    assert (await service.get_home_snapshot()).card.has_update
    await service.mark_edition_read(edition.id, edition.revision)
    await service.mark_edition_read(edition.id, edition.revision)
    assert not (await service.get_home_snapshot()).card.has_update


@pytest.mark.asyncio
async def test_saved_item_can_be_listed_and_removed_without_deleting_feedback(tmp_path):
    service, _, edition = await _edition(tmp_path)
    for action in ["save-1", "save-2"]:
        await service.add_feedback(edition.id, edition.items[0].id,
            BriefFeedbackCreateRequest(kind="save", action_id=action))
    saved = await service.list_saved_items()
    assert len(saved) == 1
    await service.remove_saved_item(saved[0].id)
    await service.remove_saved_item(saved[0].id)
    assert not await service.list_saved_items()
    assert len((await DailyBriefRepository(tmp_path).load()).feedback) == 2


@pytest.mark.asyncio
async def test_activation_rejection_leaves_no_orphan_grant(tmp_path):
    await _seed_topic(tmp_path)
    activation = DailyBriefActivationService(tmp_path, agent_id="fixture", structured_task=lambda **_: None)
    with pytest.raises(ConsentRequired):
        await activation.activate(BriefActivationRequest(explicit_consent=True,
            use_memory_clues=False, topic_ids=["topic-1"], native_notifications=True,
            action_id="invalid-native"))
    assert not (await DailyBriefRepository(tmp_path).load()).grants


@pytest.mark.asyncio
async def test_activation_same_action_rejects_changed_payload(tmp_path):
    await _seed_topic(tmp_path)
    activation = DailyBriefActivationService(tmp_path, agent_id="fixture", structured_task=lambda **_: None)
    request = BriefActivationRequest(explicit_consent=True, use_memory_clues=False,
        topic_ids=["topic-1"], action_id="stable-action")
    first = await activation.activate(request)
    assert (await activation.activate(request)).grant.id == first.grant.id
    with pytest.raises(BriefConflict):
        await activation.activate(request.model_copy(update={"mode": "preview"}))


@pytest.mark.asyncio
async def test_legacy_store_upgrade_backs_up_exact_bytes(tmp_path):
    repo = DailyBriefRepository(tmp_path)
    repo.root.mkdir(parents=True)
    original = '{"version":1,"invitation_hidden":true}'
    repo.path.write_text(original, encoding="utf-8")
    await repo.update(lambda data: data)
    assert (await repo.load()).version == 2
    assert (repo.root / "daily_brief.v1.backup.json").read_text() == original
    checksum = (repo.root / "daily_brief.v1.backup.sha256").read_text().split()[0]
    assert checksum == hashlib.sha256(original.encode()).hexdigest()


@pytest.mark.asyncio
async def test_migration_write_failure_keeps_verified_backup_and_can_resume(tmp_path, monkeypatch):
    repo = DailyBriefRepository(tmp_path)
    repo.root.mkdir(parents=True)
    original = b'{"version":1,"invitation_hidden":true}\n'
    repo.path.write_bytes(original)
    write_model = DailyBriefRepository._write_model
    def interrupted(*_):
        raise OSError("fixture interrupted before store replacement")
    monkeypatch.setattr(DailyBriefRepository, "_write_model", interrupted)
    with pytest.raises(OSError):
        await repo.update(lambda data: data)
    assert repo.path.read_bytes() == original
    backup = repo.root / "daily_brief.v1.backup.json"
    assert backup.read_bytes() == original
    checksum = (repo.root / "daily_brief.v1.backup.sha256").read_bytes()
    monkeypatch.setattr(DailyBriefRepository, "_write_model", staticmethod(write_model))
    await repo.update(lambda data: data)
    assert (await repo.load()).version == 2
    assert backup.read_bytes() == original
    assert (repo.root / "daily_brief.v1.backup.sha256").read_bytes() == checksum


@pytest.mark.asyncio
async def test_conflicting_legacy_backup_blocks_migration_without_overwriting_data(tmp_path):
    repo = DailyBriefRepository(tmp_path)
    repo.root.mkdir(parents=True)
    original = b'{"version":1,"invitation_hidden":true}'
    repo.path.write_bytes(original)
    backup = repo.root / "daily_brief.v1.backup.json"
    backup.write_bytes(b"unrelated backup")
    with pytest.raises(ValueError, match="backup"):
        await repo.update(lambda data: data)
    assert repo.path.read_bytes() == original
    assert backup.read_bytes() == b"unrelated backup"


@pytest.mark.asyncio
async def test_snapshot_bounds_history_and_attempt_payload_but_keeps_audit_store(tmp_path):
    service, cycle, edition = await _edition(tmp_path)
    repo = DailyBriefRepository(tmp_path)
    await repo.update(lambda data: data.model_copy(update={
        "cycles": [cycle.model_copy(update={"id": f"cycle-{i}"}) for i in range(300)],
        "editions": [edition.model_copy(update={"id": f"edition-{i}"}) for i in range(300)],
    }))
    snapshot = await service.get_workspace_snapshot()
    assert len(snapshot.history) <= 50
    assert snapshot.history_total == 300
    assert len(snapshot.cycles) <= 50
    assert snapshot.storage.edition_count == snapshot.storage.cycle_count == 300
    assert snapshot.storage.store_bytes == repo.path.stat().st_size
    assert len((await repo.load()).editions) == 300


@pytest.mark.asyncio
async def test_discussion_draft_is_readable_with_exact_context_and_revocation_fence(tmp_path):
    from lantai.app.daily_brief.service import ScopeFenceFailed
    service, _, edition = await _edition(tmp_path)
    draft = await service.create_discussion_draft(edition.id,
        BriefDiscussionDraftCreateRequest(edition_revision=edition.revision,
            item_id=edition.items[0].id, text="继续分析", action_id="discuss"))
    loaded = await service.get_discussion_draft(draft.id)
    assert loaded.context_ref.edition_revision == edition.revision
    assert edition.items[0].title in loaded.composer_text
    assert edition.evidence[0].canonical_url in loaded.composer_text
    await service.revoke_grant(edition.grant_id, expected_revision=edition.grant_revision, action_id="revoke")
    with pytest.raises(ScopeFenceFailed):
        await service.get_discussion_draft(draft.id)


@pytest.mark.asyncio
async def test_notification_read_updates_exact_watermark_and_rejects_revocation(tmp_path):
    from lantai.app.daily_brief.service import ScopeFenceFailed
    service, _, edition = await _edition(tmp_path)
    repo = DailyBriefRepository(tmp_path)
    receipt = (await repo.load()).delivery_receipts[0]
    first = await service.mark_delivery_read(receipt.id)
    assert (await service.mark_delivery_read(receipt.id)).read_at == first.read_at
    assert not (await service.get_home_snapshot()).card.has_update
    await service.revoke_grant(edition.grant_id, expected_revision=edition.grant_revision, action_id="revoke")
    with pytest.raises(ScopeFenceFailed):
        await service.mark_delivery_read(receipt.id)


@pytest.mark.asyncio
async def test_history_pages_keep_order_and_do_not_remove_audit_records(tmp_path):
    service, _, edition = await _edition(tmp_path)
    repo = DailyBriefRepository(tmp_path)
    await repo.update(lambda data: data.model_copy(update={"editions": [
        edition.model_copy(update={"id": f"edition-{i}"}) for i in range(120)]}))
    first = await service.list_editions(limit=50, offset=0)
    second = await service.list_editions(limit=50, offset=50)
    assert len(first) == len(second) == 50
    assert not {entry.id for entry in first} & {entry.id for entry in second}
    assert len((await repo.load()).editions) == 120
