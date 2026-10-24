# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ...constant import WORKING_DIR
from ...runtime.sanitizer import sanitize_runtime_payload
from ...security.storage_keys import (
    contained_existing_file,
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
    write_text_atomic_no_follow,
)
from ..capabilities.artifacts import artifact_from_path, register_artifact
from .models import (
    ContentDossier,
    ContentSeriesPlan,
    ContentSource,
    ContentWorkflowRun,
    PublishingDraft,
    PublishingQueueItem,
    utc_now_iso,
)


class ContentWorkbenchStore:
    """Local content-industry artifacts produced from browser evidence."""

    _IMAGE_EVIDENCE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

    def __init__(self, root_dir: str | Path | None = None) -> None:
        self.base_dir = Path(root_dir or WORKING_DIR).expanduser()
        self.root_dir = self.base_dir / "browser_workbench" / "content"
        self.sources_dir = self.root_dir / "sources"
        self.dossiers_dir = self.root_dir / "dossiers"
        self.series_dir = self.root_dir / "series"
        self.drafts_dir = self.root_dir / "drafts"
        self.queue_dir = self.root_dir / "publishing_queue"
        self.exports_dir = self.root_dir / "exports"
        self.evidence_assets_dir = self.root_dir / "evidence_assets"

    def _artifact_path(self, directory: Path, kind: str, artifact_id: str) -> Path:
        return contained_storage_path(
            directory,
            safe_storage_key(f"content-{kind}", artifact_id, suffix=".json"),
        )

    def _existing_artifact_path(
        self,
        directory: Path,
        kind: str,
        artifact_id: str,
    ) -> Path:
        path = self._artifact_path(directory, kind, artifact_id)
        if path.is_file():
            return path
        return contained_existing_file(directory, f"{artifact_id}.json") or path

    def _export_markdown_path(
        self,
        directory: Path,
        kind: str,
        artifact_id: str,
        label: str,
    ) -> Path:
        filename = (
            f"{self._slug(label)}-"
            f"{safe_storage_key(f'content-export-{kind}', artifact_id, suffix='.md')}"
        )
        return contained_storage_path(directory, filename)

    def _source_evidence_assets_dir(self, source_id: str) -> Path:
        return contained_storage_path(
            self.evidence_assets_dir,
            safe_storage_key("content-source-evidence-assets", source_id),
        )

    def save_source(self, source: ContentSource) -> ContentSource:
        source = self._sanitize_model_for_storage(source)
        self._write_model(
            self._artifact_path(self.sources_dir, "source", source.id),
            source,
        )
        return source

    def load_source(self, source_id: str) -> ContentSource:
        payload = self._read_artifact_payload(self.sources_dir, "source", source_id)
        return ContentSource.model_validate(payload)

    def get_source_detail(self, source_id: str) -> dict[str, Any]:
        source = self.load_source(source_id)
        related_dossiers: list[dict[str, Any]] = []
        for payload in self.list_dossiers():
            dossier_sources = payload.get("sources")
            if not isinstance(dossier_sources, list):
                continue
            if any(
                isinstance(item, dict)
                and (
                    item.get("id") == source.id
                    or (source.url and item.get("url") == source.url)
                )
                for item in dossier_sources
            ):
                related_dossiers.append(
                    {
                        "id": payload.get("id"),
                        "topic": payload.get("topic"),
                        "created_at": payload.get("created_at"),
                    }
                )

        related_workflows: list[dict[str, Any]] = []
        for payload in self.list_workflow_runs():
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            source_ids = metadata.get("source_ids")
            source_domains = metadata.get("source_domains")
            if (
                isinstance(source_ids, list)
                and source.id in {str(item) for item in source_ids}
            ) or (
                isinstance(source_domains, list)
                and source.domain
                and source.domain in {str(item) for item in source_domains}
            ):
                related_workflows.append(
                    {
                        "id": payload.get("id"),
                        "workflow": payload.get("workflow"),
                        "topic": payload.get("topic"),
                        "status": payload.get("status"),
                        "created_at": payload.get("created_at"),
                    }
                )

        return {
            "ok": True,
            "source": source.model_dump(mode="json"),
            "evidence": self._source_evidence_summary(source),
            "related_dossiers": related_dossiers[:12],
            "related_workflows": related_workflows[:12],
            "safety": {
                "contains_secret_material": False,
                "source_is_local_artifact": True,
                "browser_action_required": False,
            },
        }

    def get_source_evidence_asset(self, source_id: str, index: int) -> Path:
        source = self.load_source(source_id)
        evidence_paths = self._clean_evidence_paths(source)
        if index < 0 or index >= len(evidence_paths):
            raise IndexError(index)

        raw_path = evidence_paths[index]
        source_path = self._resolve_evidence_path(raw_path)
        if source_path is None or not source_path.is_file():
            raise FileNotFoundError(raw_path)
        if source_path.suffix.lower() not in self._IMAGE_EVIDENCE_EXTENSIONS:
            raise ValueError("Evidence is not a previewable image")
        if not self._is_safe_evidence_path(source_path):
            raise PermissionError("Evidence path is outside Browser Workbench storage")

        target_dir = self._source_evidence_assets_dir(source.id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (
            f"{index:03d}-{self._slug(source_path.stem)}{source_path.suffix.lower()}"
        )
        if target.is_symlink():
            raise ValueError("Evidence asset target is a symlink")
        if not target.exists() or target.stat().st_size != source_path.stat().st_size:
            shutil.copy2(source_path, target)
        self._register_content_artifact(
            target,
            kind="source_evidence_asset",
            artifact_id=f"{source.id}:{index}",
            title=f"Evidence asset {index + 1}: {source.title or source.id}",
            metadata={
                "source_id": source.id,
                "source_url": source.url,
                "evidence_index": index,
            },
        )
        return target

    def create_replay_research_pack(
        self,
        *,
        run_id: str,
        events: list[dict[str, Any]],
        topic: str = "",
        brief: str = "",
        profile_id: str = "default",
        source_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Turn browser replay events into reusable content research artifacts."""

        sources = self.sources_from_replay_events(
            run_id=run_id,
            events=events,
            topic=topic,
            source_type=source_type,
            metadata=metadata,
        )
        if not sources:
            return {
                "ok": False,
                "run_id": run_id,
                "sources": [],
                "dossier": None,
                "workflow_run": None,
                "export_paths": {},
                "message": "No replay events with web URLs were found.",
            }

        safe_topic = topic.strip() or f"Browser replay {run_id}"
        dossier = self.build_dossier(
            topic=safe_topic,
            sources=sources,
            brief=brief
            or (
                "由 Browser Workbench replay 自动沉淀的资料包。"
                "可继续进入选题、写稿、公众号系列准备和 Wiki 归档。"
            ),
            metadata={
                "workflow": "browser_research_pack",
                "source_replay_run_id": run_id,
                "profile_id": profile_id,
                **(metadata or {}),
            },
        )
        dossier_export = str(self.export_dossier_markdown(dossier.id))
        workflow_run = self.record_workflow_run(
            workflow="browser_research_pack",
            topic=safe_topic,
            profile_id=profile_id,
            source_count=len(sources),
            dossier_id=dossier.id,
            metadata={
                "source_replay_run_id": run_id,
                "dossier_export": dossier_export,
                "source_ids": [source.id for source in sources],
                "source_domains": self._unique([source.domain for source in sources]),
                "adapter_candidates": self._adapter_candidates_for_sources(sources),
                **(metadata or {}),
            },
        )
        return {
            "ok": True,
            "run_id": run_id,
            "sources": [source.model_dump(mode="json") for source in sources],
            "dossier": dossier.model_dump(mode="json"),
            "workflow_run": workflow_run.model_dump(mode="json"),
            "export_paths": {"dossier": dossier_export},
            "message": f"Created {len(sources)} content sources from replay.",
        }

    def sources_from_replay_events(
        self,
        *,
        run_id: str,
        events: list[dict[str, Any]],
        topic: str = "",
        source_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[ContentSource]:
        by_url: dict[str, ContentSource] = {}
        for event in events:
            if event.get("ok") is False:
                continue
            url = str(event.get("url") or "").strip()
            if not self._is_web_url(url):
                continue
            event_metadata = event.get("metadata")
            if not isinstance(event_metadata, dict):
                event_metadata = {}
            summary = event_metadata.get("observation_summary")
            if not isinstance(summary, dict):
                summary = {}

            source = by_url.get(url)
            if source is None:
                domain = self._domain_from_url(url)
                source = ContentSource(
                    source_type=self._safe_source_type(
                        source_type or self._classify_source_type(url, event_metadata)
                    ),
                    title=str(summary.get("title") or event.get("reason") or url)[:180],
                    url=url,
                    domain=domain,
                    summary=self._summary_from_observation(summary, event),
                    key_points=[],
                    evidence_paths=[],
                    metadata={
                        "generated_from": "browser_replay",
                        "source_replay_run_id": run_id,
                        "topic": topic,
                        "profile_id": str(event.get("profile_id") or ""),
                        "actions": [],
                        "page_context": summary.get("page_context") or {},
                        "adapter_candidate": self._adapter_candidate_for_url(url),
                        **(metadata or {}),
                    },
                )
                by_url[url] = source

            key_points = self._key_points_from_observation(summary, event)
            evidence_path = str(event.get("screenshot_path") or "").strip()
            actions = list(source.metadata.get("actions") or [])
            if event.get("action"):
                actions.append(str(event.get("action")))
            source = source.model_copy(
                update={
                    "key_points": self._unique(
                        [*source.key_points, *key_points],
                        limit=20,
                    ),
                    "evidence_paths": self._unique(
                        [*source.evidence_paths, evidence_path],
                        limit=20,
                    ),
                    "metadata": {
                        **source.metadata,
                        "actions": self._unique(actions, limit=40),
                        "event_count": int(source.metadata.get("event_count") or 0) + 1,
                    },
                }
            )
            by_url[url] = source

        sources = list(by_url.values())
        for source in sources:
            self.save_source(source)
        return sources

    def build_dossier(
        self,
        *,
        topic: str,
        sources: list[ContentSource],
        brief: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ContentDossier:
        sanitized_sources = [self.save_source(source) for source in sources]
        dossier = ContentDossier(
            topic=topic,
            brief=brief,
            sources=sanitized_sources,
            facts=[
                point
                for source in sanitized_sources
                for point in source.key_points[:6]
                if point.strip()
            ][:30],
            angles=self._derive_angles(topic, sanitized_sources),
            outline=self._derive_outline(topic, sanitized_sources),
            metadata=metadata or {},
        )
        dossier = self._sanitize_model_for_storage(dossier)
        self._write_model(
            self._artifact_path(self.dossiers_dir, "dossier", dossier.id),
            dossier,
        )
        return dossier

    def create_series_plan(
        self,
        *,
        title: str,
        audience: str = "",
        positioning: str = "",
        cadence: str = "daily",
        article_count: int = 7,
        topics: list[str] | None = None,
        start_date: str | None = None,
        publish_time: str = "09:00",
        metadata: dict[str, Any] | None = None,
    ) -> ContentSeriesPlan:
        count = max(1, min(int(article_count or 1), 60))
        normalized_topics = [item.strip() for item in topics or [] if item.strip()]
        if not normalized_topics:
            normalized_topics = [f"{title} #{index + 1}" for index in range(count)]
        normalized_publish_time = self._normalize_publish_time(publish_time)
        parsed_start_date = self._parse_start_date(start_date)
        calendar = [
            {
                "index": index + 1,
                "topic": normalized_topics[index % len(normalized_topics)],
                "cadence": cadence,
                "scheduled_for": self._scheduled_for_index(
                    index,
                    cadence=cadence,
                    start_date=parsed_start_date,
                    publish_time=normalized_publish_time,
                ),
                "status": "planned",
            }
            for index in range(count)
        ]
        plan = ContentSeriesPlan(
            title=title,
            audience=audience,
            positioning=positioning,
            cadence=cadence,
            article_count=count,
            topics=normalized_topics,
            calendar=calendar,
            metadata={
                "start_date": parsed_start_date.isoformat()
                if parsed_start_date
                else "",
                "publish_time": normalized_publish_time,
                "scheduled_browser_write_enabled": False,
                **(metadata or {}),
            },
        )
        plan = self._sanitize_model_for_storage(plan)
        self._write_model(
            self._artifact_path(self.series_dir, "series", plan.id),
            plan,
        )
        return plan

    def create_publishing_draft(
        self,
        *,
        title: str,
        body: str = "",
        summary: str = "",
        platform: str = "local_markdown",
        source_dossier_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PublishingDraft:
        draft = PublishingDraft(
            title=title,
            body=body,
            summary=summary,
            platform=platform,  # type: ignore[arg-type]
            source_dossier_id=source_dossier_id,
            metadata=metadata or {},
        )
        draft = self._sanitize_model_for_storage(draft)
        self._write_model(
            self._artifact_path(self.drafts_dir, "draft", draft.id),
            draft,
        )
        return draft

    def queue_publishing_draft(
        self,
        *,
        draft_id: str,
        scheduled_for: str | None = None,
        platform: str = "wechat_official_account",
        metadata: dict[str, Any] | None = None,
    ) -> PublishingQueueItem:
        item = PublishingQueueItem(
            draft_id=draft_id,
            platform=platform,  # type: ignore[arg-type]
            scheduled_for=scheduled_for,
            status="scheduled" if scheduled_for else "needs_confirmation",
            requires_confirmation=True,
            metadata={
                **(metadata or {}),
                "queued_draft_snapshot": self._draft_snapshot_for_queue(draft_id),
                "scheduled_browser_write_enabled": False,
                "requires_manual_write_confirmation": True,
                "history": [
                    {
                        "ts": utc_now_iso(),
                        "status": "scheduled" if scheduled_for else "needs_confirmation",
                        "note": (
                            "queued on editorial calendar; browser write still requires explicit confirmation"
                            if scheduled_for
                            else "queued for explicit user confirmation"
                        ),
                        "source": "content_workbench",
                    }
                ],
            },
        )
        item = self._sanitize_model_for_storage(item)
        self._write_model(
            self._artifact_path(self.queue_dir, "queue-item", item.id),
            item,
        )
        return item

    def prepare_wechat_series(
        self,
        *,
        title: str,
        audience: str = "",
        positioning: str = "",
        cadence: str = "daily",
        article_count: int = 7,
        topics: list[str] | None = None,
        sources: list[ContentSource] | None = None,
        profile_id: str = "default",
        start_date: str | None = None,
        publish_time: str = "09:00",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare a safe local WeChat series package without publishing."""

        safe_sources = sources or []
        dossier = self.build_dossier(
            topic=title,
            sources=safe_sources,
            brief=(
                f"公众号系列准备包。目标读者：{audience or '未指定'}。"
                f"账号定位：{positioning or '未指定'}。"
            ),
            metadata={
                "workflow": "wechat_official_account_series",
                "profile_id": profile_id,
                **(metadata or {}),
            },
        )
        series = self.create_series_plan(
            title=title,
            audience=audience,
            positioning=positioning,
            cadence=cadence,
            article_count=article_count,
            topics=topics,
            start_date=start_date,
            publish_time=publish_time,
            metadata={
                "workflow": "wechat_official_account_series",
                "source_dossier_id": dossier.id,
                "no_silent_publish": True,
                **(metadata or {}),
            },
        )
        drafts: list[PublishingDraft] = []
        queue_items: list[PublishingQueueItem] = []
        draft_exports: list[str] = []
        for item in series.calendar:
            index = int(item.get("index") or len(drafts) + 1)
            topic = str(item.get("topic") or f"{title} #{index}")
            draft = self.create_publishing_draft(
                title=topic,
                summary=self._compose_series_summary(topic, dossier, audience),
                body=self._compose_series_draft(topic, dossier, series, index=index),
                platform="wechat_official_account",
                source_dossier_id=dossier.id,
                metadata={
                    "workflow": "wechat_official_account_series",
                    "series_plan_id": series.id,
                    "series_index": index,
                    "requires_platform_confirmation": True,
                    "no_silent_publish": True,
                },
            )
            queue_item = self.queue_publishing_draft(
                draft_id=draft.id,
                scheduled_for=str(item.get("scheduled_for") or ""),
                platform="wechat_official_account",
                metadata={
                    "workflow": "wechat_official_account_series",
                    "series_plan_id": series.id,
                    "series_index": index,
                    "requires_user_confirmation_before_platform_write": True,
                    "no_silent_publish": True,
                },
            )
            drafts.append(draft)
            queue_items.append(queue_item)
            draft_exports.append(str(self.export_draft_markdown(draft.id)))

        dossier_export = str(self.export_dossier_markdown(dossier.id))
        workflow_run = self.record_workflow_run(
            workflow="wechat_official_account_series",
            topic=title,
            profile_id=profile_id,
            source_count=len(safe_sources),
            dossier_id=dossier.id,
            metadata={
                "series_plan_id": series.id,
                "draft_ids": [draft.id for draft in drafts],
                "queue_item_ids": [item.id for item in queue_items],
                "dossier_export": dossier_export,
                "draft_exports": draft_exports,
                "requires_confirmation_before_platform_write": True,
                "publishing_blocked_until_user_confirms": True,
            },
        )
        return {
            "workflow_run": workflow_run.model_dump(mode="json"),
            "series_plan": series.model_dump(mode="json"),
            "dossier": dossier.model_dump(mode="json"),
            "drafts": [draft.model_dump(mode="json") for draft in drafts],
            "queue_items": [item.model_dump(mode="json") for item in queue_items],
            "export_paths": {
                "dossier": dossier_export,
                "drafts": draft_exports,
            },
            "safety": {
                "platform_write_requires_confirmation": True,
                "publish_requires_confirmation": True,
                "silent_publish_enabled": False,
                "scheduled_browser_write_enabled": False,
            },
        }

    def load_dossier(self, dossier_id: str) -> ContentDossier:
        payload = self._read_artifact_payload(
            self.dossiers_dir,
            "dossier",
            dossier_id,
        )
        return ContentDossier.model_validate(payload)

    def load_draft(self, draft_id: str) -> PublishingDraft:
        payload = self._read_artifact_payload(self.drafts_dir, "draft", draft_id)
        return PublishingDraft.model_validate(payload)

    def load_queue_item(self, queue_item_id: str) -> PublishingQueueItem:
        payload = self._read_artifact_payload(
            self.queue_dir,
            "queue-item",
            queue_item_id,
        )
        return PublishingQueueItem.model_validate(payload)

    def update_publishing_draft(
        self,
        draft_id: str,
        *,
        status: str | None = None,
        platform_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PublishingDraft:
        draft = self.load_draft(draft_id)
        patch: dict[str, Any] = {
            "updated_at": utc_now_iso(),
            "metadata": {
                **draft.metadata,
                **(metadata or {}),
            },
        }
        if status:
            patch["status"] = status
        if platform_url is not None:
            patch["platform_url"] = platform_url
        updated = draft.model_copy(update=patch)
        updated = self._sanitize_model_for_storage(updated)
        self._write_model(
            self._artifact_path(self.drafts_dir, "draft", updated.id),
            updated,
        )
        return updated

    def update_publishing_queue_item(
        self,
        queue_item_id: str,
        *,
        status: str | None = None,
        requires_confirmation: bool | None = None,
        note: str = "",
        source: str = "browser_workbench",
        metadata: dict[str, Any] | None = None,
    ) -> PublishingQueueItem:
        item = self.load_queue_item(queue_item_id)
        next_status = status or item.status
        if next_status not in self._allowed_queue_statuses():
            raise ValueError(f"Unsupported publishing queue status: {next_status}")
        history = item.metadata.get("history")
        if not isinstance(history, list):
            history = []
        history = [
            *history,
            {
                "ts": utc_now_iso(),
                "status": next_status,
                "note": note,
                "source": source,
            },
        ]
        next_requires_confirmation = (
            self._queue_status_requires_confirmation(next_status)
            if requires_confirmation is None
            else requires_confirmation
        )
        patch = {
            "status": next_status,
            "requires_confirmation": next_requires_confirmation,
            "updated_at": utc_now_iso(),
            "metadata": {
                **item.metadata,
                **(metadata or {}),
                "history": history,
            },
        }
        updated = item.model_copy(update=patch)
        updated = self._sanitize_model_for_storage(updated)
        self._write_model(
            self._artifact_path(self.queue_dir, "queue-item", updated.id),
            updated,
        )
        return updated

    def get_publishing_queue_detail(self, queue_item_id: str) -> dict[str, Any]:
        item = self.load_queue_item(queue_item_id)
        draft: PublishingDraft | None = None
        dossier: ContentDossier | None = None
        try:
            draft = self.load_draft(item.draft_id)
        except KeyError:
            draft = None
        if draft and draft.source_dossier_id:
            try:
                dossier = self.load_dossier(draft.source_dossier_id)
            except KeyError:
                dossier = None

        snapshot = item.metadata.get("queued_draft_snapshot")
        if not isinstance(snapshot, dict) and draft:
            snapshot = self._draft_snapshot(draft)
        diff = self._draft_diff(draft, snapshot if isinstance(snapshot, dict) else {})
        return {
            "ok": True,
            "item": item.model_dump(mode="json"),
            "draft": draft.model_dump(mode="json") if draft else None,
            "dossier": dossier.model_dump(mode="json") if dossier else None,
            "preview": self._draft_preview(draft),
            "diff": diff,
            "history": item.metadata.get("history") if isinstance(item.metadata.get("history"), list) else [],
            "schedule": self._queue_schedule_info(item),
            "safety": {
                "queue_confirmation_required": item.requires_confirmation,
                "platform_write_requires_confirmation": True,
                "publish_requires_confirmation": True,
                "silent_publish_enabled": False,
                "scheduled_browser_write_enabled": False,
            },
        }

    def export_dossier_markdown(self, dossier_id: str) -> Path:
        dossier = self.load_dossier(dossier_id)
        target = self._export_markdown_path(
            self.exports_dir / "dossiers",
            "dossier",
            dossier.id,
            dossier.topic,
        )
        lines = [
            f"# {dossier.topic}",
            "",
            f"- Dossier ID: `{dossier.id}`",
            f"- Created: {dossier.created_at}",
        ]
        if dossier.brief:
            lines += ["", "## Brief", "", dossier.brief]
        if dossier.facts:
            lines += ["", "## Key Facts", ""]
            lines += [f"- {item}" for item in dossier.facts]
        if dossier.angles:
            lines += ["", "## Angles", ""]
            lines += [f"- {item}" for item in dossier.angles]
        if dossier.outline:
            lines += ["", "## Outline", ""]
            lines += [f"{index + 1}. {item}" for index, item in enumerate(dossier.outline)]
        if dossier.sources:
            lines += ["", "## Sources", ""]
            for source in dossier.sources:
                lines += [
                    f"### {source.title or source.url or source.id}",
                    "",
                    f"- Type: {source.source_type}",
                    f"- URL: {source.url or '-'}",
                    f"- Domain: {source.domain or '-'}",
                    f"- Author: {source.author or '-'}",
                    f"- Captured: {source.captured_at}",
                ]
                if source.summary:
                    lines += ["", source.summary]
                if source.key_points:
                    lines += ["", "Key points:"]
                    lines += [f"- {item}" for item in source.key_points]
                if source.evidence_paths:
                    lines += ["", "Evidence:"]
                    lines += [f"- `{Path(item).name if Path(item).is_absolute() else item}`" for item in source.evidence_paths]
                lines.append("")
        self._write_text(target, "\n".join(lines).strip() + "\n")
        self._register_content_artifact(
            target,
            kind="dossier_markdown_export",
            artifact_id=dossier.id,
            title=f"Dossier export: {dossier.topic}",
            metadata={
                "dossier_id": dossier.id,
                "topic": dossier.topic,
                "source_count": len(dossier.sources),
            },
        )
        return target

    def export_source_markdown(self, source_id: str) -> Path:
        source = self.load_source(source_id)
        target = self._export_markdown_path(
            self.exports_dir / "sources",
            "source",
            source.id,
            source.title or source.url or source.id,
        )
        lines = [
            f"# {source.title or source.url or source.id}",
            "",
            f"- Source ID: `{source.id}`",
            f"- Type: {source.source_type}",
            f"- URL: {source.url or '-'}",
            f"- Domain: {source.domain or '-'}",
            f"- Author: {source.author or '-'}",
            f"- Published: {source.published_at or '-'}",
            f"- Captured: {source.captured_at}",
        ]
        if source.summary:
            lines += ["", "## Summary", "", source.summary]
        if source.key_points:
            lines += ["", "## Key Points", ""]
            lines += [f"- {item}" for item in source.key_points]
        if source.evidence_paths:
            lines += ["", "## Evidence", ""]
            lines += [
                f"- `{Path(item).name if Path(item).is_absolute() else item}`"
                for item in source.evidence_paths
            ]
        if source.metadata:
            export_metadata = sanitize_runtime_payload(source.metadata)
            lines += [
                "",
                "## Metadata",
                "",
                "```json",
                json.dumps(export_metadata, ensure_ascii=False, indent=2),
                "```",
            ]
        self._write_text(target, "\n".join(lines).strip() + "\n")
        self._register_content_artifact(
            target,
            kind="source_markdown_export",
            artifact_id=source.id,
            title=f"Source export: {source.title or source.url or source.id}",
            metadata={
                "source_id": source.id,
                "source_url": source.url,
                "domain": source.domain,
            },
        )
        return target

    def export_draft_markdown(self, draft_id: str) -> Path:
        draft = self.load_draft(draft_id)
        target = self._export_markdown_path(
            self.exports_dir / "drafts",
            "draft",
            draft.id,
            draft.title,
        )
        lines = [
            f"# {draft.title}",
            "",
            f"- Draft ID: `{draft.id}`",
            f"- Platform: {draft.platform}",
            f"- Status: {draft.status}",
            f"- Created: {draft.created_at}",
        ]
        if draft.source_dossier_id:
            lines.append(f"- Source Dossier: `{draft.source_dossier_id}`")
        if draft.summary:
            lines += ["", "## Summary", "", draft.summary]
        if draft.body:
            lines += ["", "## Draft", "", draft.body]
        if draft.metadata:
            export_metadata = sanitize_runtime_payload(draft.metadata)
            lines += ["", "## Metadata", "", "```json", json.dumps(export_metadata, ensure_ascii=False, indent=2), "```"]
        self._write_text(target, "\n".join(lines).strip() + "\n")
        self._register_content_artifact(
            target,
            kind="draft_markdown_export",
            artifact_id=draft.id,
            title=f"Draft export: {draft.title}",
            metadata={
                "draft_id": draft.id,
                "platform": draft.platform,
                "source_dossier_id": draft.source_dossier_id,
            },
        )
        return target

    def import_markdown_export_to_wiki(
        self,
        export_path: str | Path,
        *,
        workspace_dir: str | Path,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        from ..wiki import core as wiki_core

        source = Path(export_path)
        if not source.is_file():
            raise FileNotFoundError(str(source))
        source_info, page_name, skipped = wiki_core.import_single_wiki_source(
            Path(workspace_dir),
            f"browser-workbench/{source.name}",
            source.read_bytes(),
            agent_id=agent_id,
        )
        return {
            "ok": True,
            "source": source_info.model_dump(mode="json"),
            "page": page_name,
            "skipped": skipped,
        }

    def record_workflow_run(
        self,
        *,
        workflow: str,
        topic: str,
        profile_id: str = "default",
        source_count: int = 0,
        dossier_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContentWorkflowRun:
        run = ContentWorkflowRun(
            workflow=workflow,  # type: ignore[arg-type]
            topic=topic,
            profile_id=profile_id,
            source_count=source_count,
            dossier_id=dossier_id,
            status="ready",
            updated_at=utc_now_iso(),
            metadata=metadata or {},
        )
        run = self._sanitize_model_for_storage(run)
        self._write_model(
            self._artifact_path(
                self.root_dir / "workflow_runs",
                "workflow-run",
                run.id,
            ),
            run,
        )
        return run

    def list_queue(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for payload in self._list_json(self.queue_dir, "queue-item"):
            try:
                item = PublishingQueueItem.model_validate(payload)
            except ValueError:
                continue
            enriched = item.model_dump(mode="json")
            enriched["schedule"] = self._queue_schedule_info(item)
            items.append(enriched)
        return sorted(
            items,
            key=lambda item: (
                str(item.get("scheduled_for") or "9999-12-31T23:59:59"),
                str(item.get("created_at") or ""),
            ),
        )

    def list_sources(self) -> list[dict[str, Any]]:
        return self._list_json(self.sources_dir, "source")

    def list_dossiers(self) -> list[dict[str, Any]]:
        return self._list_json(self.dossiers_dir, "dossier")

    def list_drafts(self) -> list[dict[str, Any]]:
        return self._list_json(self.drafts_dir, "draft")

    def list_workflow_runs(self) -> list[dict[str, Any]]:
        return self._list_json(self.root_dir / "workflow_runs", "workflow-run")

    def _source_evidence_summary(self, source: ContentSource) -> dict[str, Any]:
        evidence_paths = self._clean_evidence_paths(source)
        metadata = source.metadata if isinstance(source.metadata, dict) else {}
        actions = metadata.get("actions")
        if not isinstance(actions, list):
            actions = []
        page_context = metadata.get("page_context")
        if not isinstance(page_context, dict):
            page_context = {}
        return {
            "evidence_paths": evidence_paths,
            "evidence_count": len(evidence_paths),
            "assets": [
                self._source_evidence_asset_summary(source.id, index, item)
                for index, item in enumerate(evidence_paths)
            ],
            "actions": [str(item) for item in actions[:40]],
            "page_context": page_context,
            "adapter_candidate": str(metadata.get("adapter_candidate") or ""),
            "source_replay_run_id": str(metadata.get("source_replay_run_id") or ""),
        }

    @staticmethod
    def _clean_evidence_paths(source: ContentSource) -> list[str]:
        return [str(item).strip() for item in source.evidence_paths if str(item).strip()]

    def _source_evidence_asset_summary(
        self,
        source_id: str,
        index: int,
        raw_path: str,
    ) -> dict[str, Any]:
        resolved = self._resolve_evidence_path(raw_path)
        file_name = Path(raw_path).name or f"evidence-{index}"
        exists = bool(resolved and resolved.is_file())
        suffix = (resolved.suffix if resolved else Path(raw_path).suffix).lower()
        is_image = suffix in self._IMAGE_EVIDENCE_EXTENSIONS
        is_safe = bool(resolved and self._is_safe_evidence_path(resolved))
        reason = "preview_ready"
        preview_url = ""
        if not exists:
            reason = "missing"
        elif not is_image:
            reason = "not_image"
        elif not is_safe:
            reason = "outside_browser_workbench_storage"
        else:
            preview_url = (
                f"/browser-workbench/content/sources/{source_id}/evidence/{index}"
            )
        return {
            "index": index,
            "path": raw_path,
            "file_name": file_name,
            "exists": exists,
            "is_image": is_image,
            "preview_url": preview_url,
            "reason": reason,
        }

    def _resolve_evidence_path(self, raw_path: str) -> Path | None:
        if not raw_path.strip():
            return None
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root_dir.parent / candidate
        try:
            return candidate.resolve()
        except OSError:
            return None

    def _is_safe_evidence_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            browser_root = self.root_dir.parent.resolve()
        except OSError:
            return False
        return resolved == browser_root or browser_root in resolved.parents

    def _draft_snapshot_for_queue(self, draft_id: str) -> dict[str, Any]:
        try:
            return self._draft_snapshot(self.load_draft(draft_id))
        except KeyError:
            return {"draft_id": draft_id, "missing": True}

    @staticmethod
    def _draft_snapshot(draft: PublishingDraft) -> dict[str, Any]:
        body = draft.body or ""
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return {
            "draft_id": draft.id,
            "title": draft.title,
            "summary": draft.summary,
            "body_sha256": digest,
            "body_length": len(body),
            "body_excerpt": body[:1200],
            "status": draft.status,
            "updated_at": draft.updated_at,
        }

    @staticmethod
    def _draft_preview(draft: PublishingDraft | None) -> dict[str, Any]:
        if draft is None:
            return {
                "missing": True,
                "title": "",
                "summary": "",
                "body_excerpt": "",
                "body_length": 0,
                "body_line_count": 0,
            }
        body = draft.body or ""
        return {
            "missing": False,
            "title": draft.title,
            "summary": draft.summary,
            "body_excerpt": body[:1800],
            "body_length": len(body),
            "body_line_count": len(body.splitlines()),
            "platform": draft.platform,
            "status": draft.status,
        }

    @classmethod
    def _draft_diff(
        cls,
        draft: PublishingDraft | None,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        if draft is None:
            return {
                "draft_missing": True,
                "changed_fields": ["draft"],
                "body_length_delta": 0,
            }
        current = cls._draft_snapshot(draft)
        changed_fields = [
            field
            for field in ("title", "summary", "body_sha256", "status")
            if current.get(field) != snapshot.get(field)
        ]
        return {
            "draft_missing": False,
            "changed_fields": changed_fields,
            "title_changed": "title" in changed_fields,
            "summary_changed": "summary" in changed_fields,
            "body_changed": "body_sha256" in changed_fields,
            "status_changed": "status" in changed_fields,
            "body_length_delta": int(current.get("body_length") or 0)
            - int(snapshot.get("body_length") or 0),
            "queued_body_length": int(snapshot.get("body_length") or 0),
            "current_body_length": int(current.get("body_length") or 0),
        }

    @staticmethod
    def _queue_status_requires_confirmation(status: str) -> bool:
        return status in {"queued", "scheduled", "needs_confirmation"}

    @staticmethod
    def _allowed_queue_statuses() -> set[str]:
        return {
            "queued",
            "scheduled",
            "needs_confirmation",
            "ready_to_write",
            "write_requested",
            "saved",
            "blocked",
            "archived",
        }

    @classmethod
    def _queue_schedule_info(
        cls,
        item: PublishingQueueItem,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        scheduled_for = str(item.scheduled_for or "").strip()
        base = {
            "scheduled_for": scheduled_for,
            "due_status": "unscheduled",
            "is_due": False,
            "minutes_until": None,
            "label": "无排期",
            "auto_browser_write_allowed": False,
            "requires_manual_write_confirmation": True,
        }
        if not scheduled_for:
            return base
        scheduled_at = cls._parse_scheduled_datetime(scheduled_for)
        if scheduled_at is None:
            return {
                **base,
                "due_status": "invalid",
                "label": "排期格式异常",
            }
        current = now.replace(tzinfo=None) if now else datetime.now().replace(tzinfo=None)
        delta_minutes = int((scheduled_at - current).total_seconds() // 60)
        if delta_minutes <= -24 * 60:
            due_status = "overdue"
            label = "已过期"
        elif delta_minutes <= 0:
            due_status = "due"
            label = "到期"
        elif scheduled_at.date() == current.date():
            due_status = "today"
            label = "今日待发"
        else:
            due_status = "upcoming"
            label = "未到期"
        return {
            **base,
            "due_status": due_status,
            "is_due": delta_minutes <= 0,
            "minutes_until": delta_minutes,
            "label": label,
        }

    @staticmethod
    def _parse_start_date(value: str | None) -> date | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if "T" in text:
                return datetime.fromisoformat(
                    text.replace("Z", "+00:00")
                ).date()
            return date.fromisoformat(text[:10])
        except ValueError:
            return None

    @staticmethod
    def _normalize_publish_time(value: str) -> str:
        text = str(value or "").strip() or "09:00"
        try:
            hour_text, minute_text, *_ = [*text.split(":"), "0"]
            hour = max(0, min(23, int(hour_text)))
            minute = max(0, min(59, int(minute_text)))
        except ValueError:
            return "09:00"
        return f"{hour:02d}:{minute:02d}"

    @classmethod
    def _scheduled_for_index(
        cls,
        index: int,
        *,
        cadence: str,
        start_date: date | None,
        publish_time: str,
    ) -> str:
        if start_date is None:
            return ""
        normalized_cadence = str(cadence or "daily").strip().lower()
        if normalized_cadence in {"weekly", "每周"}:
            scheduled_date = start_date + timedelta(days=index * 7)
        elif normalized_cadence in {"weekdays", "workdays", "工作日"}:
            scheduled_date = start_date
            remaining = index
            while remaining > 0:
                scheduled_date += timedelta(days=1)
                if scheduled_date.weekday() < 5:
                    remaining -= 1
        else:
            scheduled_date = start_date + timedelta(days=index)
        return f"{scheduled_date.isoformat()}T{publish_time}:00"

    @staticmethod
    def _parse_scheduled_datetime(value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if len(text) == 10:
                return datetime.fromisoformat(f"{text}T00:00:00")
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            return None

    @staticmethod
    def _derive_angles(topic: str, sources: list[ContentSource]) -> list[str]:
        angles = [f"{topic} 的事实脉络", f"{topic} 的用户痛点"]
        for source in sources[:6]:
            if source.summary:
                angles.append(source.summary[:120])
        return angles[:12]

    @staticmethod
    def _derive_outline(topic: str, sources: list[ContentSource]) -> list[str]:
        return [
            f"开场：为什么现在讨论 {topic}",
            "事实与背景",
            "关键案例和信源",
            "争议、风险与反方观点",
            "行动建议或内容结论",
            f"结尾：{len(sources)} 个信源的下一步追踪",
        ]

    @staticmethod
    def _compose_series_summary(
        topic: str,
        dossier: ContentDossier,
        audience: str,
    ) -> str:
        target = f"面向{audience}" if audience else "面向目标读者"
        first_fact = dossier.facts[0] if dossier.facts else "先建立问题意识和事实框架"
        return f"{target}，围绕“{topic}”展开：{first_fact}"

    @staticmethod
    def _compose_series_draft(
        topic: str,
        dossier: ContentDossier,
        series: ContentSeriesPlan,
        *,
        index: int,
    ) -> str:
        facts = dossier.facts or ["待补充核心事实", "待补充案例", "待补充反方观点"]
        outline = dossier.outline or ContentWorkbenchStore._derive_outline(
            topic,
            dossier.sources,
        )
        lines = [
            f"# {topic}",
            "",
            "## 开头",
            f"这是《{series.title}》系列的第 {index} 篇。今天先把问题讲清楚：{topic}。",
            "",
            "## 资料要点",
        ]
        lines += [f"- {item}" for item in facts[:8]]
        lines += ["", "## 文章提纲"]
        lines += [f"{item_index + 1}. {item}" for item_index, item in enumerate(outline)]
        lines += [
            "",
            "## 正文草稿",
            f"围绕“{topic}”，这篇文章建议先用一个具体场景进入，再解释背景、事实、争议和行动建议。",
            "",
            "第一部分：指出读者当下最容易忽略的问题，并用已收集信源中的事实做铺垫。",
            "",
            "第二部分：展开关键事实和案例，注意每个重要判断都保留来源链接和截图证据。",
            "",
            "第三部分：列出争议点、反方观点或风险边界，避免把资料整理写成单向宣传。",
            "",
            "第四部分：给出面向读者的下一步建议，并预告系列下一篇。",
            "",
            "## 发布提醒",
            "本草稿只进入本地待确认队列。写入微信公众号后台、保存平台草稿、定时发布、群发或发布都必须再次人工确认。",
        ]
        return "\n".join(lines)

    @staticmethod
    def _is_web_url(value: str) -> bool:
        try:
            parsed = urlparse(value)
        except Exception:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)

    @staticmethod
    def _domain_from_url(value: str) -> str:
        try:
            return (urlparse(value).hostname or "").lower()
        except Exception:
            return ""

    @classmethod
    def _classify_source_type(cls, url: str, metadata: dict[str, Any]) -> str:
        domain = cls._domain_from_url(url)
        adapter_id = str(metadata.get("adapter_id") or "")
        if any(
            marker in domain
            for marker in ("douyin.com", "bilibili.com", "youtube.com", "tiktok.com")
        ) or "video" in adapter_id:
            return "video_page"
        if any(
            marker in domain
            for marker in (
                "xiaohongshu.com",
                "weibo.com",
                "x.com",
                "twitter.com",
                "reddit.com",
                "zhihu.com",
            )
        ) or "social" in adapter_id:
            return "social_post"
        if any(marker in domain for marker in ("google.", "bing.com", "baidu.com")):
            return "search_result"
        return "web_article"

    @staticmethod
    def _safe_source_type(value: str) -> str:
        allowed = {
            "web_article",
            "social_post",
            "video_page",
            "comment_thread",
            "search_result",
            "manual_note",
        }
        return value if value in allowed else "web_article"

    @staticmethod
    def _summary_from_observation(
        summary: dict[str, Any],
        event: dict[str, Any],
    ) -> str:
        excerpt = str(summary.get("text_excerpt") or "").strip()
        if excerpt:
            return excerpt[:500]
        page_context = summary.get("page_context")
        if isinstance(page_context, dict) and page_context.get("kind"):
            return f"页面类型：{page_context.get('kind')}"
        return str(event.get("reason") or "Browser replay source").strip()[:500]

    @staticmethod
    def _key_points_from_observation(
        summary: dict[str, Any],
        event: dict[str, Any],
    ) -> list[str]:
        points: list[str] = []
        title = str(summary.get("title") or "").strip()
        if title:
            points.append(f"页面标题：{title}")
        text_length = summary.get("text_length")
        if isinstance(text_length, int) and text_length > 0:
            points.append(f"可见文本约 {text_length} 字符")
        links_count = summary.get("links_count")
        if isinstance(links_count, int):
            points.append(f"可见链接 {links_count} 个")
        controls_count = summary.get("controls_count")
        if isinstance(controls_count, int):
            points.append(f"可见控件 {controls_count} 个")
        page_context = summary.get("page_context")
        if isinstance(page_context, dict):
            kind = str(page_context.get("kind") or "").strip()
            if kind and kind != "normal":
                points.append(f"页面风险类型：{kind}")
            if page_context.get("requires_manual_takeover"):
                points.append("需要人工接管后继续")
        if event.get("screenshot_path"):
            points.append("已保留截图证据")
        return points

    @classmethod
    def _adapter_candidate_for_url(cls, url: str) -> str:
        domain = cls._domain_from_url(url)
        if "xiaohongshu.com" in domain:
            return "xiaohongshu.note.collect"
        if "douyin.com" in domain:
            return "douyin.video.research"
        if "bilibili.com" in domain:
            return "bilibili.video.research"
        if "weibo.com" in domain:
            return "weibo.topic.scan"
        if any(marker in domain for marker in ("youtube.com", "tiktok.com")):
            return "video.page.summarize"
        return "media.article.extract"

    @classmethod
    def _adapter_candidates_for_sources(
        cls,
        sources: list[ContentSource],
    ) -> list[dict[str, str]]:
        return [
            {
                "source_id": source.id,
                "domain": source.domain,
                "adapter_id": str(
                    source.metadata.get("adapter_candidate")
                    or cls._adapter_candidate_for_url(source.url)
                ),
            }
            for source in sources
        ]

    @staticmethod
    def _unique(values: list[str], *, limit: int = 100) -> list[str]:
        out: list[str] = []
        for value in values:
            item = str(value or "").strip()
            if item and item not in out:
                out.append(item)
            if len(out) >= limit:
                break
        return out

    def _write_model(self, path: Path, model: Any) -> None:
        payload = model.model_dump(mode="json") if hasattr(model, "model_dump") else model
        payload = self._sanitize_artifact_payload(payload)
        write_text_atomic_no_follow(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        self._register_content_artifact(
            path,
            kind=self._model_artifact_kind(model),
            artifact_id=str(getattr(model, "id", path.stem)),
            title=self._model_artifact_title(model),
            metadata={
                "model": model.__class__.__name__,
                "content_artifact_kind": self._model_artifact_kind(model),
            },
        )

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        write_text_atomic_no_follow(path, text)

    def _register_content_artifact(
        self,
        path: Path,
        *,
        kind: str,
        artifact_id: str,
        title: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not path.is_file():
            return
        artifact = artifact_from_path(
            path,
            title=title,
            producer="browser_workbench_content",
            classification="user_content",
            retention="user_saved",
            metadata={
                "browser_workbench_content": True,
                "content_artifact_kind": kind,
                "content_artifact_id": artifact_id,
                **(metadata or {}),
            },
        )
        stable_id = safe_storage_key(
            "browser-workbench-content-artifact",
            f"{kind}:{artifact_id}:{path}",
        )
        register_artifact(
            self.base_dir,
            artifact.model_copy(update={"id": stable_id}),
        )

    @classmethod
    def _sanitize_model_for_storage(cls, model: Any) -> Any:
        if not hasattr(model, "model_dump") or not hasattr(
            model.__class__,
            "model_validate",
        ):
            return model
        payload = cls._sanitize_artifact_payload(model.model_dump(mode="json"))
        return model.__class__.model_validate(payload)

    @classmethod
    def _sanitize_artifact_payload(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._sanitize_artifact_payload(item) for item in value]
        if not isinstance(value, dict):
            return value
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "metadata" and isinstance(item, dict):
                sanitized[key] = sanitize_runtime_payload(item) if item else {}
            else:
                sanitized[key] = cls._sanitize_artifact_payload(item)
        return sanitized

    @staticmethod
    def _model_artifact_kind(model: Any) -> str:
        name = model.__class__.__name__
        mapping = {
            "ContentSource": "content_source",
            "ContentDossier": "content_dossier",
            "ContentSeriesPlan": "content_series_plan",
            "PublishingDraft": "publishing_draft",
            "PublishingQueueItem": "publishing_queue_item",
            "ContentWorkflowRun": "content_workflow_run",
        }
        return mapping.get(name, name)

    @staticmethod
    def _model_artifact_title(model: Any) -> str:
        for attr in ("title", "topic", "url", "workflow", "id"):
            value = str(getattr(model, attr, "") or "").strip()
            if value:
                return value
        return model.__class__.__name__

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(read_text_no_follow(path))
        except FileNotFoundError as exc:
            raise KeyError(path.stem) from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid browser workbench content artifact: {path}")
        return payload

    def _read_artifact_payload(
        self,
        directory: Path,
        kind: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        path = self._existing_artifact_path(directory, kind, artifact_id)
        payload = self._read_json(path)
        if str(payload.get("id") or "") != str(artifact_id):
            raise KeyError(artifact_id)
        return payload

    def _list_json(self, root: Path, kind: str) -> list[dict[str, Any]]:
        if not root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.json")):
            try:
                payload = json.loads(read_text_no_follow(path))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                if not self._matches_artifact_path(root, kind, path, payload):
                    continue
                out.append(payload)
        return out

    def _matches_artifact_path(
        self,
        directory: Path,
        kind: str,
        path: Path,
        payload: dict[str, Any],
    ) -> bool:
        artifact_id = str(payload.get("id") or "")
        if not artifact_id:
            return False
        try:
            canonical = self._artifact_path(directory, kind, artifact_id)
            legacy = contained_storage_path(directory, f"{artifact_id}.json")
        except ValueError:
            return False
        resolved = path.resolve(strict=False)
        return resolved in {
            canonical.resolve(strict=False),
            legacy.resolve(strict=False),
        }

    @staticmethod
    def _slug(value: str) -> str:
        slug = "".join(
            ch.lower() if ch.isalnum() else "-"
            for ch in value.strip()
        )
        slug = "-".join(part for part in slug.split("-") if part)
        return slug[:80] or "artifact"
