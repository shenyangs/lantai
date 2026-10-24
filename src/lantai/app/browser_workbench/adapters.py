# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...runtime.sanitizer import sanitize_runtime_payload
from ...security.storage_keys import (
    contained_existing_file,
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
    write_text_atomic_no_follow,
)
from .broker import BrowserActionBroker
from .models import (
    AdapterRunRequest,
    BrowserAction,
    BrowserObservation,
    DomainSkillDraft,
    DomainSkillDraftPatch,
    DomainSkillDraftRecord,
    LantaiCliAdapterManifest,
    utc_now_iso,
)
from .safe_scripts import safe_evaluate_metadata


class LantaiCliAdapterRegistry:
    """Small deterministic site-adapter registry for browser workbench v1."""

    def __init__(
        self,
        *,
        broker: BrowserActionBroker | None = None,
        domain_skill_root: str | Path | None = None,
    ) -> None:
        self.broker = broker or BrowserActionBroker()
        self._manifests = self._builtin_manifests()
        self.domain_skill_root = (
            Path(domain_skill_root)
            if domain_skill_root is not None
            else self.broker.profiles.root_dir / "domain_skills"
        )

    def list_adapters(self) -> list[LantaiCliAdapterManifest]:
        return sorted(self._manifests.values(), key=lambda item: item.id)

    def get_adapter(self, adapter_id: str) -> LantaiCliAdapterManifest | None:
        return self._manifests.get(adapter_id)

    def plan_actions(
        self,
        adapter_id: str,
        request: AdapterRunRequest,
    ) -> list[BrowserAction]:
        manifest = self.get_adapter(adapter_id)
        if manifest is None:
            raise KeyError(f"LantaiCLI adapter not found: {adapter_id}")
        return self._actions_for_adapter(manifest, request)

    def verify_adapter(
        self,
        adapter_id: str,
        request: AdapterRunRequest | None = None,
    ) -> dict[str, Any]:
        manifest = self.get_adapter(adapter_id)
        if manifest is None:
            return {
                "ok": False,
                "adapter_id": adapter_id,
                "errors": [f"LantaiCLI adapter not found: {adapter_id}"],
                "warnings": [],
                "planned_actions": [],
            }
        run_request = request or AdapterRunRequest(profile_id="default", inputs={})
        errors: list[str] = []
        warnings: list[str] = []
        if not manifest.inputs:
            warnings.append("Adapter declares no inputs.")
        if not manifest.outputs:
            errors.append("Adapter declares no outputs.")
        if manifest.required_profile and not run_request.profile_id:
            errors.append("Adapter requires a browser profile.")
        try:
            actions = self._actions_for_adapter(manifest, run_request)
        except Exception as exc:
            return {
                "ok": False,
                "adapter_id": adapter_id,
                "errors": [f"Adapter action planning failed: {exc}"],
                "warnings": warnings,
                "planned_actions": [],
            }
        if not actions:
            errors.append("Adapter produced no planned browser actions.")
        planned_actions = []
        for action in actions:
            access = action.access or manifest.access
            planned_actions.append(
                {
                    "action": action.action,
                    "access": access,
                    "url": action.url,
                    "selector": bool(action.selector),
                    "requires_confirmation": access in {"write", "sensitive"}
                    and not action.confirmed,
                }
            )
            if access in {"write", "sensitive"} and not action.confirmed:
                warnings.append(
                    f"Action `{action.action}` requires explicit confirmation."
                )
            if action.action in {"click", "fill_form", "type", "select"} and not (
                action.selector or action.metadata.get("fields_json") or action.metadata.get("fields")
            ):
                warnings.append(
                    f"Action `{action.action}` has no stable selector and may require manual takeover."
                )
        return {
            "ok": not errors,
            "adapter_id": adapter_id,
            "manifest": manifest.model_dump(mode="json"),
            "errors": errors,
            "warnings": self._unique(warnings),
            "planned_actions": planned_actions,
        }

    async def run_adapter(
        self,
        adapter_id: str,
        request: AdapterRunRequest,
    ) -> BrowserObservation:
        manifest = self.get_adapter(adapter_id)
        if manifest is None:
            return BrowserObservation(
                ok=False,
                action="adapter_run",
                runtime="playwright_fallback",
                profile_id=request.profile_id,
                error=f"LantaiCLI adapter not found: {adapter_id}",
                data={"adapter_id": adapter_id},
            )
        actions = self._actions_for_adapter(manifest, request)
        last: BrowserObservation | None = None
        for action in actions:
            last = await self.broker.execute(action)
            if not last.ok:
                return last
        if last is None:
            return BrowserObservation(
                ok=False,
                action="adapter_run",
                runtime="playwright_fallback",
                profile_id=request.profile_id,
                error=f"LantaiCLI adapter produced no actions: {adapter_id}",
                data={"adapter_id": adapter_id},
            )
        last.data = {
            **last.data,
            "adapter_id": adapter_id,
            "adapter_outputs": manifest.outputs,
        }
        return last

    def save_domain_skill_draft(self, draft: DomainSkillDraft) -> Path:
        draft = self._sanitize_domain_skill_draft(draft)
        domain = draft.domain.strip().lower() or "unknown"
        target_dir = contained_storage_path(
            self.domain_skill_root,
            safe_storage_key("domain-skill-domain", domain),
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        slug = self._domain_skill_slug(draft.title)
        path = contained_storage_path(
            target_dir,
            f"{slug}-{self._domain_skill_path_hash(domain, slug)}.json",
        )
        write_text_atomic_no_follow(
            path,
            draft.model_dump_json(indent=2),
        )
        return path

    def list_domain_skill_drafts(
        self,
        *,
        domain: str | None = None,
    ) -> list[DomainSkillDraftRecord]:
        root = self.domain_skill_root
        if not root.exists():
            return []
        records: list[DomainSkillDraftRecord] = []
        domain_filter = (domain or "").strip().lower()
        paths = sorted(root.glob("*/*.json"))
        for path in paths:
            try:
                record = self._domain_skill_record_from_path(path)
            except (OSError, ValueError):
                continue
            if domain_filter and record.draft.domain != domain_filter:
                continue
            records.append(record)
        return sorted(
            records,
            key=lambda item: item.updated_at or item.draft.created_at,
            reverse=True,
        )

    def load_domain_skill_draft(self, draft_id: str) -> DomainSkillDraftRecord:
        return self._domain_skill_record_from_path(
            self._domain_skill_path_from_id(draft_id)
        )

    def update_domain_skill_draft(
        self,
        draft_id: str,
        patch: DomainSkillDraftPatch,
    ) -> DomainSkillDraftRecord:
        record = self.load_domain_skill_draft(draft_id)
        draft = record.draft.model_copy(deep=True)
        update = patch.model_dump(exclude_none=True)
        enabled = update.pop("enabled", None)
        metadata_update = update.pop("metadata", None)
        for key, value in update.items():
            setattr(draft, key, value)
        metadata = {**draft.metadata}
        if metadata_update:
            metadata.update(metadata_update)
        now = utc_now_iso()
        metadata["updated_at"] = now
        if enabled is not None:
            metadata["enabled"] = bool(enabled)
            if enabled:
                metadata["enabled_at"] = now
                metadata.pop("disabled_at", None)
            else:
                metadata["disabled_at"] = now
        draft.metadata = metadata
        draft = self._sanitize_domain_skill_draft(draft)
        path = self._domain_skill_path_from_id(draft_id)
        write_text_atomic_no_follow(path, draft.model_dump_json(indent=2))
        return self._domain_skill_record_from_path(path)

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        out: list[str] = []
        for value in values:
            item = str(value or "").strip()
            if item and item not in out:
                out.append(item)
        return out

    def _domain_skill_record_from_path(self, path: Path) -> DomainSkillDraftRecord:
        root = self.domain_skill_root.resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("Domain Skill draft path is outside registry root.")
        draft = self._sanitize_domain_skill_draft(
            DomainSkillDraft.model_validate_json(read_text_no_follow(path)),
        )
        if not self._domain_skill_path_matches(path, draft):
            raise ValueError("Domain Skill draft path does not match draft identity.")
        updated_at = datetime.fromtimestamp(
            path.stat().st_mtime,
            timezone.utc,
        ).isoformat()
        metadata = draft.metadata
        domain = draft.domain.strip().lower() or "unknown"
        slug = self._domain_skill_slug(draft.title)
        return DomainSkillDraftRecord(
            id=f"{domain}:{slug}",
            path=str(path),
            draft=draft,
            enabled=bool(metadata.get("enabled")),
            updated_at=str(metadata.get("updated_at") or updated_at),
            source_run_id=str(metadata.get("run_id") or ""),
            recommended_adapter_id=str(metadata.get("recommended_adapter_id") or ""),
        )

    def _domain_skill_path_from_id(self, draft_id: str) -> Path:
        if ":" not in draft_id:
            raise KeyError(f"Domain Skill draft not found: {draft_id}")
        domain, slug = draft_id.split(":", 1)
        domain = domain.strip().lower() or "unknown"
        slug = self._domain_skill_slug(slug)
        if not domain or not slug:
            raise KeyError(f"Domain Skill draft not found: {draft_id}")
        domain_key = safe_storage_key("domain-skill-domain", domain)
        path = contained_existing_file(
            self.domain_skill_root,
            domain_key,
            f"{slug}-{self._domain_skill_path_hash(domain, slug)}.json",
        )
        if path is not None:
            return path

        legacy_domain = "".join(
            ch for ch in domain if ch.isalnum() or ch in {".", "-"}
        )
        legacy = contained_existing_file(
            self.domain_skill_root,
            legacy_domain,
            f"{slug}.json",
        )
        if legacy is not None:
            return legacy
        raise KeyError(f"Domain Skill draft not found: {draft_id}")

    def _domain_skill_path_matches(
        self,
        path: Path,
        draft: DomainSkillDraft,
    ) -> bool:
        domain = draft.domain.strip().lower() or "unknown"
        slug = self._domain_skill_slug(draft.title)
        resolved = path.resolve()
        return any(
            resolved == candidate.resolve()
            for candidate in self._domain_skill_paths(domain, slug)
        )

    def _domain_skill_paths(self, domain: str, slug: str) -> list[Path]:
        domain_key = safe_storage_key("domain-skill-domain", domain)
        canonical = contained_storage_path(
            self.domain_skill_root,
            domain_key,
            f"{slug}-{self._domain_skill_path_hash(domain, slug)}.json",
        )
        legacy_domain = "".join(
            ch for ch in domain if ch.isalnum() or ch in {".", "-"}
        )
        legacy = contained_storage_path(
            self.domain_skill_root,
            legacy_domain,
            f"{slug}.json",
        )
        return [canonical, legacy]

    @staticmethod
    def _sanitize_domain_skill_draft(
        draft: DomainSkillDraft,
    ) -> DomainSkillDraft:
        if not draft.metadata:
            return draft
        return draft.model_copy(
            update={"metadata": sanitize_runtime_payload(dict(draft.metadata))},
        )

    @staticmethod
    def _domain_skill_slug(value: str) -> str:
        return (
            "".join(
                ch if ch.isalnum() or ch in {"-", "_"} else "-"
                for ch in value.strip().lower()
            ).strip("-")
            or "skill"
        )

    @staticmethod
    def _domain_skill_path_hash(domain: str, slug: str) -> str:
        return safe_storage_key(
            "domain-skill-draft",
            f"{domain}\0{slug}",
            hash_chars=16,
        ).rsplit("-", 1)[-1]

    @staticmethod
    def _builtin_manifests() -> dict[str, LantaiCliAdapterManifest]:
        manifests = [
            LantaiCliAdapterManifest(
                id="github.issues.list",
                domain="github.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["repo_url", "limit"],
                outputs=["issues", "summary"],
                description="Read GitHub repository issues through the logged-in browser.",
            ),
            LantaiCliAdapterManifest(
                id="github.prs.list",
                domain="github.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["repo_url", "limit"],
                outputs=["pull_requests", "summary"],
                description="Read GitHub repository pull requests through the logged-in browser.",
            ),
            LantaiCliAdapterManifest(
                id="web.page.summarize",
                domain="*",
                strategy="LOCAL_BROWSER",
                access="read",
                required_profile=False,
                inputs=["url"],
                outputs=["snapshot", "summary"],
                description="Open a page and capture a structured snapshot for summarization.",
            ),
            LantaiCliAdapterManifest(
                id="web.table.extract",
                domain="*",
                strategy="LOCAL_BROWSER",
                access="read",
                required_profile=False,
                inputs=["url"],
                outputs=["tables", "summary"],
                description="Open a page and extract visible table/list context.",
            ),
            LantaiCliAdapterManifest(
                id="web.list.extract",
                domain="*",
                strategy="LOCAL_BROWSER",
                access="read",
                required_profile=False,
                inputs=["url"],
                outputs=["items", "summary"],
                description="Extract visible list items and repeated cards from a page.",
            ),
            LantaiCliAdapterManifest(
                id="media.article.extract",
                domain="*",
                strategy="LOCAL_BROWSER",
                access="read",
                required_profile=False,
                inputs=["url"],
                outputs=["article", "source"],
                description="Capture article text, byline context, and evidence from a media page.",
            ),
            LantaiCliAdapterManifest(
                id="social.post.collect",
                domain="*",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url", "limit"],
                outputs=["posts", "evidence"],
                description="Collect visible social posts with login-state browser evidence.",
            ),
            LantaiCliAdapterManifest(
                id="video.page.summarize",
                domain="*",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url"],
                outputs=["video_context", "comments", "summary"],
                description="Summarize visible video page metadata, description, and comments.",
            ),
            LantaiCliAdapterManifest(
                id="xiaohongshu.note.collect",
                domain="xiaohongshu.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url", "limit"],
                outputs=["notes", "evidence"],
                description="Collect visible Xiaohongshu note context under user-authorized login state.",
            ),
            LantaiCliAdapterManifest(
                id="douyin.video.research",
                domain="douyin.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url"],
                outputs=["video_context", "comment_insights"],
                description="Research a Douyin video page without bypassing platform controls.",
            ),
            LantaiCliAdapterManifest(
                id="bilibili.video.research",
                domain="bilibili.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url"],
                outputs=["video_context", "comment_insights"],
                description="Research a Bilibili video page with replay evidence.",
            ),
            LantaiCliAdapterManifest(
                id="weibo.topic.scan",
                domain="weibo.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url", "limit"],
                outputs=["topic_posts", "summary"],
                description="Scan visible Weibo topic posts with profile authorization.",
            ),
            LantaiCliAdapterManifest(
                id="media.topic.scan",
                domain="*",
                strategy="LOCAL_BROWSER",
                access="read",
                required_profile=False,
                inputs=["query", "urls"],
                outputs=["sources", "clusters"],
                description="Scan media sources for a topic and prepare a source tray.",
            ),
            LantaiCliAdapterManifest(
                id="campaign.competitor.watch",
                domain="*",
                strategy="LOCAL_BROWSER",
                access="read",
                required_profile=False,
                inputs=["urls"],
                outputs=["changes", "insights"],
                description="Collect competitor campaign pages and summarize observable changes.",
            ),
            LantaiCliAdapterManifest(
                id="content.dossier.build",
                domain="*",
                strategy="LOCAL_BROWSER",
                access="read",
                required_profile=False,
                inputs=["topic", "sources"],
                outputs=["dossier"],
                description="Build a content dossier from collected browser evidence.",
            ),
            LantaiCliAdapterManifest(
                id="content.draft.write",
                domain="*",
                strategy="LOCAL_BROWSER",
                access="read",
                required_profile=False,
                inputs=["topic", "dossier"],
                outputs=["draft"],
                description="Prepare a local article draft from a content dossier.",
            ),
            LantaiCliAdapterManifest(
                id="content.series.plan",
                domain="*",
                strategy="LOCAL_BROWSER",
                access="read",
                required_profile=False,
                inputs=["theme", "audience", "count", "cadence"],
                outputs=["series_plan"],
                description="Plan a multi-day content series and publishing queue.",
            ),
            LantaiCliAdapterManifest(
                id="wechat.official_account.login_check",
                domain="mp.weixin.qq.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url"],
                outputs=["login_state"],
                description="Check WeChat Official Account backend login state.",
            ),
            LantaiCliAdapterManifest(
                id="wechat.official_account.draft.create",
                domain="mp.weixin.qq.com",
                strategy="LOGIN_UI",
                access="write",
                required_profile=True,
                inputs=["title"],
                outputs=["draft_state"],
                description="Create a WeChat Official Account draft after user confirmation.",
            ),
            LantaiCliAdapterManifest(
                id="wechat.official_account.draft.fill",
                domain="mp.weixin.qq.com",
                strategy="LOGIN_UI",
                access="write",
                required_profile=True,
                inputs=["title", "body", "summary"],
                outputs=["draft_state"],
                description="Fill a WeChat Official Account draft after user confirmation.",
            ),
            LantaiCliAdapterManifest(
                id="wechat.official_account.draft.save",
                domain="mp.weixin.qq.com",
                strategy="LOGIN_UI",
                access="write",
                required_profile=True,
                inputs=["draft_id"],
                outputs=["draft_state"],
                description="Save a WeChat Official Account draft after user confirmation.",
            ),
            LantaiCliAdapterManifest(
                id="wechat.official_account.series.queue",
                domain="mp.weixin.qq.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["series_plan"],
                outputs=["queue"],
                description="Create a local confirmation queue for WeChat series drafts.",
            ),
            LantaiCliAdapterManifest(
                id="wechat.official_account.cover.inspect",
                domain="mp.weixin.qq.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url"],
                outputs=["cover_options", "evidence"],
                description="Inspect WeChat cover image controls without selecting or uploading.",
            ),
            LantaiCliAdapterManifest(
                id="wechat.official_account.image_library.inspect",
                domain="mp.weixin.qq.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url"],
                outputs=["image_library_state", "evidence"],
                description="Inspect WeChat image library dialog without choosing a cover.",
            ),
            LantaiCliAdapterManifest(
                id="wechat.official_account.original.inspect",
                domain="mp.weixin.qq.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url"],
                outputs=["original_declaration_state", "evidence"],
                description="Inspect WeChat originality declaration controls without submitting.",
            ),
            LantaiCliAdapterManifest(
                id="wechat.official_account.comments.inspect",
                domain="mp.weixin.qq.com",
                strategy="LOGIN_UI",
                access="read",
                required_profile=True,
                inputs=["url"],
                outputs=["comment_settings_state", "evidence"],
                description="Inspect WeChat comment controls without changing settings.",
            ),
        ]
        return {item.id: item for item in manifests}

    @staticmethod
    def _actions_for_adapter(
        manifest: LantaiCliAdapterManifest,
        request: AdapterRunRequest,
    ) -> list[BrowserAction]:
        inputs: dict[str, Any] = request.inputs
        if manifest.id in {"github.issues.list", "github.prs.list"}:
            repo_url = str(inputs.get("repo_url") or "").rstrip("/")
            limit = int(inputs.get("limit") or 10)
            url = repo_url
            suffix = "issues" if manifest.id == "github.issues.list" else "pulls"
            if repo_url and not repo_url.endswith(f"/{suffix}"):
                url = f"{repo_url}/{suffix}?per_page={limit}"
            return [
                BrowserAction(
                    action="open",
                    profile_id=request.profile_id,
                    url=url,
                    access="read",
                    confirmed=request.confirmed,
                    run_id=request.run_id,
                    reason=f"Open page for LantaiCLI adapter {manifest.id}",
                ),
                BrowserAction(
                    action="snapshot",
                    profile_id=request.profile_id,
                    access="read",
                    confirmed=request.confirmed,
                    run_id=request.run_id,
                    reason=f"Run LantaiCLI adapter {manifest.id}",
                ),
            ]
        url = str(inputs.get("url") or "")
        if manifest.id == "web.table.extract":
            return [
                BrowserAction(
                    action="open",
                    profile_id=request.profile_id,
                    url=url,
                    access="read",
                    confirmed=request.confirmed,
                    run_id=request.run_id,
                    reason=f"Open page for LantaiCLI adapter {manifest.id}",
                ),
                BrowserAction(
                    action="extract_table",
                    profile_id=request.profile_id,
                    access="read",
                    confirmed=request.confirmed,
                    run_id=request.run_id,
                    reason=f"Run LantaiCLI adapter {manifest.id}",
                    metadata={"limit": inputs.get("limit") or 5},
                ),
            ]
        if manifest.id == "web.list.extract":
            return [
                BrowserAction(
                    action="open",
                    profile_id=request.profile_id,
                    url=url,
                    access="read",
                    confirmed=request.confirmed,
                    run_id=request.run_id,
                    reason=f"Open page for LantaiCLI adapter {manifest.id}",
                ),
                BrowserAction(
                    action="extract_list",
                    profile_id=request.profile_id,
                    access="read",
                    confirmed=request.confirmed,
                    run_id=request.run_id,
                    reason=f"Run LantaiCLI adapter {manifest.id}",
                    metadata={"limit": inputs.get("limit") or 80},
                ),
            ]
        if manifest.id.startswith("wechat.official_account."):
            target = url or "https://mp.weixin.qq.com/"
            actions = [
                BrowserAction(
                    action="open",
                    profile_id=request.profile_id,
                    url=target,
                    access="read",
                    confirmed=request.confirmed,
                    run_id=request.run_id,
                    reason=f"Open WeChat backend for {manifest.id}",
                ),
                BrowserAction(
                    action="snapshot",
                    profile_id=request.profile_id,
                    access="read",
                    confirmed=request.confirmed,
                    run_id=request.run_id,
                    reason=f"Check current WeChat backend state for {manifest.id}",
                ),
            ]
            if manifest.id.endswith(".inspect"):
                selectors = LantaiCliAdapterRegistry._wechat_inspect_selectors(
                    manifest.id,
                )
                actions.append(
                    BrowserAction(
                        action="evaluate",
                        profile_id=request.profile_id,
                        access="read",
                        confirmed=request.confirmed,
                        run_id=request.run_id,
                        reason=f"Inspect WeChat editor controls for {manifest.id}",
                        code=LantaiCliAdapterRegistry._wechat_inspect_code(manifest.id),
                        metadata={
                            "adapter_id": manifest.id,
                            **safe_evaluate_metadata(
                                "wechat.inspect_controls",
                                {"selectors": selectors},
                            ),
                        },
                    )
                )
                return actions
            if manifest.access == "write":
                actions.append(
                    BrowserAction(
                        action="click",
                        profile_id=request.profile_id,
                        selector=str(inputs.get("selector") or ""),
                        access="write",
                        confirmed=request.confirmed,
                        run_id=request.run_id,
                        reason=(
                            f"Prepare confirmed WeChat draft operation for {manifest.id}"
                        ),
                        metadata={"adapter_id": manifest.id},
                    )
                )
            return actions
        if manifest.id in {
            "media.article.extract",
            "social.post.collect",
            "video.page.summarize",
            "xiaohongshu.note.collect",
            "douyin.video.research",
            "bilibili.video.research",
            "weibo.topic.scan",
        }:
            return [
                BrowserAction(
                    action="open",
                    profile_id=request.profile_id,
                    url=url,
                    access="read",
                    confirmed=request.confirmed,
                    run_id=request.run_id,
                    reason=f"Open page for LantaiCLI adapter {manifest.id}",
                    metadata={"adapter_id": manifest.id, "domain": manifest.domain},
                ),
                BrowserAction(
                    action="snapshot",
                    profile_id=request.profile_id,
                    access="read",
                    confirmed=request.confirmed,
                    run_id=request.run_id,
                    reason=f"Capture source evidence for {manifest.id}",
                    metadata={"adapter_id": manifest.id, "domain": manifest.domain},
                ),
            ]
        return [
            BrowserAction(
                action="open",
                profile_id=request.profile_id,
                url=url,
                access="read",
                confirmed=request.confirmed,
                run_id=request.run_id,
                reason=f"Open page for LantaiCLI adapter {manifest.id}",
            ),
            BrowserAction(
                action="snapshot",
                profile_id=request.profile_id,
                access="read",
                confirmed=request.confirmed,
                run_id=request.run_id,
                reason=f"Run LantaiCLI adapter {manifest.id}",
            ),
        ]

    @staticmethod
    def _wechat_inspect_selectors(adapter_id: str) -> str:
        selector_map = {
            "wechat.official_account.cover.inspect": (
                ".js_cover_btn_area, #js_cover_null, .js_selectCoverFromContent, "
                ".js_imagedialog, .js_imageScan, .js_aiImage"
            ),
            "wechat.official_account.image_library.inspect": (
                ".weui-desktop-dialog_img-picker, .js_imagedialog, "
                ".single_upload_btn_container, .weui-desktop-menu__link"
            ),
            "wechat.official_account.original.inspect": (
                "#js_original, .js_original_apply, .js_edit_ori, "
                ".weui-desktop-dialog"
            ),
            "wechat.official_account.comments.inspect": (
                "#js_comment_and_fansmsg_area, .js_interaction, "
                ".interaction-dialog, .comment-options-container"
            ),
        }
        return selector_map.get(adapter_id, "body")

    @staticmethod
    def _wechat_inspect_code(adapter_id: str) -> str:
        selectors = LantaiCliAdapterRegistry._wechat_inspect_selectors(adapter_id)
        return (
            "() => {"
            "const visible = (el) => { const s = getComputedStyle(el);"
            "const r = el.getBoundingClientRect();"
            "return s.display !== 'none' && s.visibility !== 'hidden' &&"
            "r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0; };"
            f"const selectors = `{selectors}`;"
            "return Array.from(document.querySelectorAll(selectors)).filter(visible)"
            ".slice(0, 40).map((el) => { const r = el.getBoundingClientRect();"
            "return { tag: el.tagName.toLowerCase(), id: el.id || '',"
            "className: String(el.className || ''),"
            "text: (el.innerText || el.getAttribute('aria-label') || '')"
            ".replace(/\\s+/g, ' ').trim().slice(0, 180),"
            "rect: { x: r.x, y: r.y, width: r.width, height: r.height } }; });"
            "}"
        )
