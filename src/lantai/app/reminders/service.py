# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any
import secrets
from uuid import uuid4

from .models import (
    ReminderCreateRequest,
    ReminderDeliveryClaim,
    ReminderItem,
    ReminderSettings,
    ReminderStatus,
    ReminderTriggerResult,
    ReminderUpdateRequest,
    TodayBrief,
    WatchTopic,
    WatchTopicBriefUseRequest,
    WatchTopicCreateRequest,
    WatchTopicUpdateRequest,
    utc_now,
)
from .repo import ReminderRepository
from .runtime_runs import record_reminder_runtime_run_started


DEFAULT_REMINDER_DEDUPE_WINDOW = timedelta(hours=24)


class WatchTopicRevisionConflict(RuntimeError):
    pass


class WatchTopicConfirmationRequired(ValueError):
    pass


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _runtime_metadata(item: ReminderItem) -> dict[str, Any]:
    metadata = item.metadata.get("runtime")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _parse_runtime_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return _as_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _is_due_for_trigger(item: ReminderItem, now: datetime) -> bool:
    if item.status in {"dismissed", "done"}:
        return False
    if item.status == "snoozed":
        return item.snooze_until is not None and _as_utc(item.snooze_until) <= now
    return item.due_at is not None and _as_utc(item.due_at) <= now


class ReminderService:
    def __init__(self, repo: ReminderRepository):
        self._repo = repo

    @property
    def workspace_dir(self):
        return self._repo.workspace_dir

    async def _invalidate_brief_topic_scope(
        self,
        topic_id: str,
        *,
        current_revision: int | None,
    ) -> None:
        brief_store = self.workspace_dir / "daily_brief" / "daily_brief.json"
        if not brief_store.exists():
            return
        # Lazy import keeps the reminder domain independent when briefs are unused.
        from ..daily_brief.service import DailyBriefService

        await DailyBriefService(self.workspace_dir).invalidate_topic_scope(
            topic_id,
            current_revision=current_revision,
        )

    async def list_reminders(
        self,
        status: ReminderStatus | None = None,
    ) -> list[ReminderItem]:
        data = await self._repo.load()
        items = []
        for item in data.reminders:
            runtime = _runtime_metadata(item)
            claim = data.delivery_claims.get(str(runtime.get("last_invocation_id") or ""))
            if claim is not None:
                item = item.model_copy(update={"metadata": {**item.metadata, "runtime": {
                    **runtime, "delivery_status": claim.status, "delivery_error": claim.error,
                }}})
            items.append(item)
        if status is not None:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.updated_at, reverse=True)

    async def create_reminder(
        self,
        request: ReminderCreateRequest,
    ) -> ReminderItem:
        now = utc_now()
        item = ReminderItem(
            title=request.title,
            summary=request.summary,
            reason=request.reason,
            source=request.source,
            priority=request.priority,
            due_at=request.due_at,
            metadata=request.metadata,
            created_at=now,
            updated_at=now,
        )

        def updater(data):
            return data.model_copy(update={"reminders": [item, *data.reminders]})

        await self._repo.update(updater)
        return item

    async def update_reminder(
        self,
        reminder_id: str,
        request: ReminderUpdateRequest,
    ) -> ReminderItem | None:
        update_data = request.model_dump(exclude_unset=True)
        update_data["updated_at"] = utc_now()
        updated: ReminderItem | None = None

        def updater(data):
            nonlocal updated
            reminders: list[ReminderItem] = []
            for item in data.reminders:
                if item.id == reminder_id:
                    updated = item.model_copy(update=update_data)
                    reminders.append(updated)
                else:
                    reminders.append(item)
            return data.model_copy(update={"reminders": reminders})

        await self._repo.update(updater)
        return updated

    async def set_status(
        self,
        reminder_id: str,
        status: ReminderStatus,
        **extra: Any,
    ) -> ReminderItem | None:
        return await self.update_reminder(
            reminder_id,
            ReminderUpdateRequest(status=status, **extra),
        )

    async def snooze_reminder(
        self,
        reminder_id: str,
        until: datetime,
    ) -> ReminderItem | None:
        return await self.set_status(
            reminder_id,
            "snoozed",
            snooze_until=until,
        )

    async def claim_due_reminders(
        self,
        *,
        agent_id: str,
        now: datetime | None = None,
        dedupe_window: timedelta = DEFAULT_REMINDER_DEDUPE_WINDOW,
        limit: int = 50,
        delivery_owner: dict[str, Any] | None = None,
        native_only: bool = False,
    ) -> list[ReminderTriggerResult]:
        trigger_at = _as_utc(now or utc_now())
        claimed: list[ReminderItem] = []
        results: list[ReminderTriggerResult] = []

        def updater(data):
            next_reminders: list[ReminderItem] = []
            claims = dict(data.delivery_claims)
            remaining_claims = max(0, limit)
            native_available = (
                data.settings.native_notification_policy == "explicit_only"
                and data.native_adapter_agent_id == agent_id
                and data.native_adapter_ready_until is not None
                and _as_utc(data.native_adapter_ready_until) > utc_now()
            )
            for item in data.reminders:
                if native_only and not native_available:
                    next_reminders.append(item)
                    continue
                if not _is_due_for_trigger(item, trigger_at):
                    next_reminders.append(item)
                    continue

                runtime_meta = _runtime_metadata(item)
                previous_claim = claims.get(str(runtime_meta.get("last_invocation_id") or ""))
                rescheduled = previous_claim is not None and (
                    previous_claim.reminder.due_at != item.due_at
                    or previous_claim.reminder.snooze_until != item.snooze_until
                )
                if previous_claim and (not previous_claim.settled or (
                        previous_claim.status == "unknown" and not rescheduled)):
                    results.append(ReminderTriggerResult(
                        reminder_id=item.id, status="deduped", run_id=previous_claim.run_id,
                        reason="delivery_confirmation_unknown" if previous_claim.status == "unknown" else "delivery_owner_active",
                        triggered_at=trigger_at,
                    ))
                    next_reminders.append(item)
                    continue
                last_invocation_at = _parse_runtime_datetime(
                    runtime_meta.get("last_invocation_at"),
                )
                never_submitted = bool(native_only and previous_claim
                    and previous_claim.settled and previous_claim.native_ready
                    and previous_claim.status in {"failed", "cancelled"}
                    and (not previous_claim.caller_id or (
                        previous_claim.receipt is not None
                        and previous_claim.receipt.get("reason") in {"preference", "desktop_not_submitted"}
                    )))
                dedupe_until = (
                    last_invocation_at + dedupe_window
                    if last_invocation_at is not None and not rescheduled and not never_submitted
                    else None
                )
                if dedupe_until is not None and trigger_at < dedupe_until:
                    results.append(
                        ReminderTriggerResult(
                            reminder_id=item.id,
                            status="deduped",
                            run_id=str(runtime_meta.get("last_invocation_id") or "")
                            or None,
                            reason="dedupe_window_active",
                            triggered_at=trigger_at,
                            dedupe_until=dedupe_until,
                        ),
                    )
                    next_reminders.append(item)
                    continue

                if remaining_claims <= 0:
                    next_reminders.append(item)
                    continue

                run_id = f"reminder:{item.id}:{uuid4().hex}"
                next_runtime_meta = {
                    **runtime_meta,
                    "last_invocation_id": run_id,
                    "last_invocation_at": trigger_at.isoformat(),
                    "dedupe_window_seconds": int(dedupe_window.total_seconds()),
                }
                next_metadata = {
                    **item.metadata,
                    "runtime": next_runtime_meta,
                }
                next_item = item.model_copy(
                    update={
                        "metadata": next_metadata,
                        "updated_at": trigger_at,
                    },
                )
                claim = None
                if delivery_owner is not None:
                    claim = ReminderDeliveryClaim(
                        run_id=run_id, attempt_id=f"{run_id}:reminder-attempt-1",
                        agent_id=agent_id, reminder=next_item,
                        receipt_token=secrets.token_urlsafe(32), created_at=trigger_at,
                        **delivery_owner,
                    )
                    claims[run_id] = claim
                claimed.append(next_item)
                results.append(
                    ReminderTriggerResult(
                        reminder_id=item.id,
                        status="claimed",
                        run_id=run_id,
                        attempt_id=claim.attempt_id if claim else None,
                        agent_id=agent_id,
                        receipt_token=claim.receipt_token if claim else None,
                        reason="due",
                        triggered_at=trigger_at,
                        dedupe_until=trigger_at + dedupe_window,
                    ),
                )
                remaining_claims -= 1
                next_reminders.append(next_item)
            return data.model_copy(update={"reminders": next_reminders, "delivery_claims": claims})

        await self._repo.update(updater)

        for item in claimed:
            run_id = _runtime_metadata(item).get("last_invocation_id")
            if isinstance(run_id, str) and run_id:
                await record_reminder_runtime_run_started(
                    workspace_dir=self._repo.workspace_dir,
                    agent_id=agent_id,
                    reminder=item,
                    run_id=run_id,
                )

        return results

    async def set_settings(self, settings: ReminderSettings) -> ReminderSettings:
        await self._repo.update(lambda data: data.model_copy(update={"settings": settings}))
        return settings

    async def list_watch_topics(self) -> list[WatchTopic]:
        data = await self._repo.load()
        return sorted(data.watch_topics, key=lambda item: item.updated_at, reverse=True)

    async def create_watch_topic(
        self,
        request: WatchTopicCreateRequest,
    ) -> WatchTopic:
        now = utc_now()
        topic = WatchTopic(
            title=request.title,
            description=request.description,
            origin=request.origin,
            enabled=request.enabled,
            frequency=request.frequency,
            metadata=request.metadata,
            created_at=now,
            updated_at=now,
        )

        def updater(data):
            return data.model_copy(update={"watch_topics": [topic, *data.watch_topics]})

        await self._repo.update(updater)
        return topic

    async def update_watch_topic(
        self,
        topic_id: str,
        request: WatchTopicUpdateRequest,
    ) -> WatchTopic | None:
        update_data = request.model_dump(exclude_unset=True)
        expected_revision = update_data.pop("expected_revision", None)
        scope_changed = any(key != "last_checked_at" for key in update_data)
        update_data["updated_at"] = utc_now()
        updated: WatchTopic | None = None

        def updater(data):
            nonlocal updated
            topics: list[WatchTopic] = []
            for item in data.watch_topics:
                if item.id == topic_id:
                    if (
                        expected_revision is not None
                        and item.revision != expected_revision
                    ):
                        raise WatchTopicRevisionConflict(
                            f"watch topic {topic_id} revision changed"
                        )
                    updated = item.model_copy(
                        update={
                            **update_data,
                            "revision": item.revision + 1
                            if scope_changed
                            else item.revision,
                        }
                    )
                    topics.append(updated)
                else:
                    topics.append(item)
            return data.model_copy(update={"watch_topics": topics})

        await self._repo.update(updater)
        if updated is not None and scope_changed:
            await self._invalidate_brief_topic_scope(
                updated.id,
                current_revision=updated.revision,
            )
        return updated

    async def configure_watch_topic_for_brief(
        self,
        topic_id: str,
        request: WatchTopicBriefUseRequest,
    ) -> WatchTopic | None:
        if request.status == "confirmed" and not request.explicit_confirmation:
            raise WatchTopicConfirmationRequired(
                "explicit confirmation is required before a topic can be researched"
            )

        updated: WatchTopic | None = None
        now = utc_now()

        def updater(data):
            nonlocal updated
            topics: list[WatchTopic] = []
            for item in data.watch_topics:
                if item.id != topic_id:
                    topics.append(item)
                    continue
                if item.revision != request.expected_revision:
                    raise WatchTopicRevisionConflict(
                        f"watch topic {topic_id} revision changed"
                    )
                updated = item.model_copy(
                    update={
                        "revision": item.revision + 1,
                        "brief_status": request.status,
                        "brief_purpose": request.purpose.strip(),
                        "brief_scope_refs": list(dict.fromkeys(request.scope_refs)),
                        "brief_evidence_refs": list(
                            dict.fromkeys(request.evidence_refs)
                        ),
                        "brief_exclusions": list(dict.fromkeys(request.exclusions)),
                        "brief_valid_until": request.valid_until,
                        "updated_at": now,
                    }
                )
                topics.append(updated)
            return data.model_copy(update={"watch_topics": topics})

        await self._repo.update(updater)
        if updated is not None:
            await self._invalidate_brief_topic_scope(
                updated.id,
                current_revision=updated.revision,
            )
        return updated

    async def invalidate_watch_topics_by_evidence_ref(
        self,
        evidence_ref: str,
    ) -> int:
        """Pause brief topics whose authority depended on a retired fact.

        The evidence reference remains on the topic as an audit trail, while
        its revision changes so every derived cycle, edition, draft and
        pending delivery is invalidated through the existing topic fence.
        """

        clean_ref = str(evidence_ref or "").strip()
        if not clean_ref:
            return 0
        now = utc_now()
        invalidated: list[WatchTopic] = []

        def updater(data):
            topics: list[WatchTopic] = []
            for item in data.watch_topics:
                if (
                    clean_ref not in item.brief_evidence_refs
                    or item.brief_status in {"paused", "archived"}
                ):
                    topics.append(item)
                    continue
                metadata = {
                    **item.metadata,
                    "brief_dependency_invalidated": clean_ref,
                    "brief_dependency_invalidated_at": now.isoformat(),
                }
                updated = item.model_copy(
                    update={
                        "brief_status": "paused",
                        "revision": item.revision + 1,
                        "metadata": metadata,
                        "updated_at": now,
                    }
                )
                invalidated.append(updated)
                topics.append(updated)
            return data.model_copy(update={"watch_topics": topics})

        await self._repo.update(updater)
        for topic in invalidated:
            await self._invalidate_brief_topic_scope(
                topic.id,
                current_revision=topic.revision,
            )
        return len(invalidated)

    async def delete_watch_topic(self, topic_id: str) -> bool:
        deleted = False

        def updater(data):
            nonlocal deleted
            topics = [item for item in data.watch_topics if item.id != topic_id]
            deleted = len(topics) != len(data.watch_topics)
            return data.model_copy(update={"watch_topics": topics})

        await self._repo.update(updater)
        if deleted:
            await self._invalidate_brief_topic_scope(
                topic_id,
                current_revision=None,
            )
        return deleted

    async def get_settings(self) -> ReminderSettings:
        data = await self._repo.load()
        return data.settings

    @property
    def repository(self) -> ReminderRepository:
        return self._repo

    async def today_brief(self) -> TodayBrief:
        data = await self._repo.load()
        now = utc_now()
        local_now = now.astimezone()
        end_of_today = datetime.combine(
            local_now.date(),
            time.max,
            tzinfo=local_now.tzinfo,
        )
        active_items = [
            item
            for item in data.reminders
            if item.status not in {"dismissed", "done"}
            and not (
                item.status == "snoozed"
                and item.snooze_until is not None
                and item.snooze_until > now
            )
        ]
        due_items = [
            item
            for item in active_items
            if item.due_at is not None and item.due_at <= end_of_today
        ]
        high_items = [item for item in active_items if item.priority == "high"]
        brief_items_by_id = {item.id: item for item in [*high_items, *due_items]}
        brief_items = sorted(
            brief_items_by_id.values(),
            key=lambda item: (
                0 if item.priority == "high" else 1,
                item.due_at or item.updated_at,
            ),
        )
        return TodayBrief(
            date=local_now.date().isoformat(),
            items=brief_items[:12],
            new_count=len([item for item in active_items if item.status == "new"]),
            due_count=len(due_items),
            high_value_count=len(high_items),
            quiet_mode=data.settings.quiet_mode,
        )
