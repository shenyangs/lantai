# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ...constant import WORKING_DIR
from ...security.storage_keys import (
    append_text_no_follow,
    contained_existing_file,
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
)
from ...runtime.sanitizer import sanitize_runtime_payload
from ..capabilities.models import (
    Capability,
    CapabilityParameter,
    CapabilityStep,
    DeliveryTarget,
)
from .models import BrowserReplayEvent, DomainSkillDraft
from .models import BrowserExportApproval


class BrowserReplayStore:
    """JSONL evidence log for browser workbench actions."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        self.base_dir = Path(root_dir or WORKING_DIR)
        self.root_dir = self.base_dir / "browser_workbench" / "replay"
        self._allowed_evidence_roots = tuple(
            root.resolve()
            for root in {
                self.root_dir.parent / "evidence",
                self.base_dir.parent / "browser-workbench" / "evidence",
            }
        )

    def append(self, event: BrowserReplayEvent) -> Path:
        from .agent.safety import redact_untrusted_text
        self.root_dir.mkdir(parents=True, exist_ok=True)
        path = self._replay_path(event.run_id)
        sanitized_event = event.model_copy(
            update={
                "reason": redact_untrusted_text(event.reason, limit=1000),
                "error": redact_untrusted_text(event.error, limit=1000)
                if event.error
                else None,
                "url": redact_untrusted_text(event.url, limit=4096),
                "metadata": self._sanitize_metadata(event.metadata),
            },
        )
        append_text_no_follow(
            path,
            json.dumps(sanitized_event.model_dump(mode="json"), ensure_ascii=False)
            + "\n",
        )
        return path

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        path = self._existing_replay_path(run_id)
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in read_text_no_follow(path).splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                if str(payload.get("run_id") or "") != str(run_id):
                    continue
                out.append(payload)
        return out

    def export_bundle(
        self,
        run_id: str,
        *,
        approval: BrowserExportApproval | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        approval_payload = self._export_approval_payload(
            approval,
            export_kind="browser_replay_bundle",
        )
        safe_run_id = self._safe_run_id(run_id)
        events = self.list_events(run_id)
        bundle_dir = self.root_dir.parent / "evidence_bundles" / safe_run_id
        evidence_dir = bundle_dir / "evidence"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)

        copied: list[dict[str, str]] = []
        sanitized_events: list[dict[str, Any]] = []
        for index, event in enumerate(events, start=1):
            sanitized = self._sanitize_event(event)
            screenshot_path = event.get("screenshot_path")
            if screenshot_path:
                source = Path(str(screenshot_path))
                sanitized["screenshot_path"] = source.name
                safe_source = self._resolve_allowed_evidence_path(source)
                if safe_source and safe_source.is_file():
                    target = evidence_dir / f"{index:03d}-{safe_source.name}"
                    shutil.copy2(safe_source, target)
                    copied.append(
                        {
                            "event_id": str(event.get("id") or ""),
                            "path": str(target),
                            "file": target.name,
                        }
                    )
                    sanitized["evidence_file"] = target.name
                elif source.is_file():
                    sanitized["evidence_untrusted"] = source.name
                else:
                    sanitized["evidence_missing"] = source.name
            sanitized_events.append(sanitized)

        manifest = {
            "run_id": run_id,
            "event_count": len(events),
            "copied_evidence_count": len(copied),
            "export_approval": approval_payload,
            "second_sanitization": {
                "events": True,
                "metadata": True,
                "evidence_paths": "basename_only",
            },
            "events": sanitized_events,
            "evidence": copied,
        }
        manifest_path = bundle_dir / "manifest.json"
        report_path = bundle_dir / "report.md"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            "utf-8",
        )
        report_path.write_text(self._render_report(run_id, sanitized_events, copied), "utf-8")
        archive_path = shutil.make_archive(str(bundle_dir), "zip", root_dir=bundle_dir)
        return {
            "ok": True,
            "run_id": run_id,
            "event_count": len(events),
            "copied_evidence_count": len(copied),
            "bundle_dir": str(bundle_dir),
            "manifest_path": str(manifest_path),
            "report_path": str(report_path),
            "archive_path": archive_path,
            "export_approval": approval_payload,
        }

    def build_domain_skill_draft(
        self,
        run_id: str,
        *,
        title: str = "",
        summary: str = "",
    ) -> DomainSkillDraft:
        events = self.list_events(run_id)
        domains = Counter(
            domain
            for event in events
            for domain in [self._domain_from_url(str(event.get("url") or ""))]
            if domain
        )
        domain = domains.most_common(1)[0][0] if domains else "unknown"
        url_patterns = self._unique(
            pattern
            for event in events
            for pattern in [self._url_pattern(str(event.get("url") or ""))]
            if pattern
        )
        selectors = self._unique(
            selector
            for event in events
            for selector in self._selectors_from_event(event)
        )
        failures = self._unique(
            str(event.get("error") or event.get("reason") or "")[:240]
            for event in events
            if event.get("error") or event.get("ok") is False
        )
        action_sequence = [
            str(event.get("action") or "")
            for event in events
            if event.get("action")
        ]
        adapter_ids = self._unique(
            adapter_id
            for event in events
            for adapter_id in [
                self._adapter_id_from_event(event),
            ]
            if adapter_id
        )
        ok_count = sum(1 for event in events if event.get("ok") is True)
        draft = DomainSkillDraft(
            domain=domain,
            title=title.strip() or f"{domain} browser workflow",
            summary=summary.strip()
            or f"Generated from browser replay `{run_id}` with {len(events)} events.",
            url_patterns=url_patterns[:20],
            selectors=selectors[:40],
            wait_conditions=self._derive_wait_conditions(action_sequence),
            failure_modes=failures[:20],
            metadata={
                "generated_from": "browser_replay",
                "run_id": run_id,
                "event_count": len(events),
                "ok_count": ok_count,
                "action_sequence": action_sequence[:80],
                "adapter_ids": adapter_ids,
                "recommended_adapter_id": adapter_ids[0] if adapter_ids else "",
                "scopes": self._scopes_from_events(events)[:40],
                "access_levels": self._unique(
                    str(event.get("access") or "")
                    for event in events
                    if event.get("access")
                ),
                "evidence_event_ids": [
                    str(event.get("id") or "")
                    for event in events
                    if event.get("screenshot_path")
                ][:40],
            },
        )
        return draft

    def build_capability_from_replay(
        self,
        run_id: str,
        *,
        name: str = "",
        description: str = "",
        profile_id: str = "default",
        access: str = "read",
        instruction_template: str = "",
    ) -> Capability:
        skill = self.build_domain_skill_draft(run_id, title=name, summary=description)
        domain = "" if skill.domain == "unknown" else skill.domain
        safe_access = access if access in {"read", "write", "sensitive"} else "read"
        title = name.strip() or f"Browser workflow: {skill.domain}"
        instruction = instruction_template.strip() or (
            "Use the authorized Browser Workbench profile to repeat the "
            f"`{skill.domain}` workflow from replay `{run_id}`. "
            "Respect profile/domain policy, keep replay evidence, and stop for "
            "login, captcha, payment, publishing, deletion, authorization, or any "
            "unexpected write-risk page."
        )
        return Capability(
            name=title,
            description=description.strip() or skill.summary,
            enabled=False,
            tags=[
                item
                for item in ["browser-workbench", "browser_prompt", domain]
                if item
            ],
            source_run_id=run_id,
            parameters=[
                CapabilityParameter(
                    name="task_goal",
                    label="Task goal",
                    type="text",
                    required=True,
                    description="What the browser workflow should accomplish.",
                ),
                CapabilityParameter(
                    name="target_url",
                    label="Target URL",
                    type="text",
                    required=False,
                    description="Optional page to open before running the workflow.",
                ),
            ],
            steps=[
                CapabilityStep(
                    index=0,
                    title="Run Browser Workbench flow",
                    kind="browser_prompt",
                    instruction_template=instruction,
                    required_parameters=["task_goal"],
                    profile_id=profile_id,
                    target_domains=[domain] if domain else [],
                    access=safe_access,  # type: ignore[arg-type]
                    requires_manual_confirmation=safe_access != "read",
                    replay_policy="full",
                    risk_level="medium" if safe_access != "read" else "low",
                    metadata={
                        "generated_from": "browser_replay",
                        "source_replay_run_id": run_id,
                        "url_patterns": skill.url_patterns,
                        "selectors": skill.selectors,
                        "wait_conditions": skill.wait_conditions,
                        "failure_modes": skill.failure_modes,
                    },
                )
            ],
            delivery_defaults=[
                DeliveryTarget(type="chat"),
                DeliveryTarget(
                    type="wiki_draft",
                    title=title,
                ),
            ],
        )

    def _replay_path(self, run_id: str) -> Path:
        return contained_storage_path(
            self.root_dir,
            safe_storage_key("browser-replay", run_id, suffix=".jsonl"),
        )

    def _legacy_replay_path(self, run_id: str) -> Path | None:
        return contained_existing_file(self.root_dir, f"{run_id}.jsonl")

    def _existing_replay_path(self, run_id: str) -> Path:
        path = self._replay_path(run_id)
        if path.is_file():
            return path
        return self._legacy_replay_path(run_id) or path

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        safe = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "-"
            for ch in str(run_id).strip()
        ).strip("-")
        return safe or "run"

    @staticmethod
    def _export_approval_payload(
        approval: BrowserExportApproval | dict[str, Any] | None,
        *,
        export_kind: str,
    ) -> dict[str, Any]:
        if approval is None:
            raise PermissionError("export approval required")
        parsed = (
            approval
            if isinstance(approval, BrowserExportApproval)
            else BrowserExportApproval.model_validate(approval)
        )
        if not parsed.approved:
            raise PermissionError("export approval required")
        if not parsed.reason.strip():
            raise ValueError("export approval reason required")
        payload = parsed.model_dump(mode="json")
        payload["export_kind"] = export_kind
        return BrowserReplayStore._sanitize_metadata(payload)

    def _resolve_allowed_evidence_path(self, raw_path: Path) -> Path | None:
        try:
            source = raw_path.expanduser().resolve(strict=True)
        except OSError:
            return None
        for root in self._allowed_evidence_roots:
            try:
                source.relative_to(root)
            except ValueError:
                continue
            return source
        return None

    @staticmethod
    def _domain_from_url(value: str) -> str:
        try:
            return (urlparse(value).hostname or "").lower()
        except Exception:
            return ""

    @staticmethod
    def _url_pattern(value: str) -> str:
        try:
            parsed = urlparse(value)
        except Exception:
            return ""
        if not parsed.hostname:
            return ""
        scheme = parsed.scheme or "https"
        segments = [
            BrowserReplayStore._normalize_path_segment(segment)
            for segment in parsed.path.split("/")
            if segment
        ][:5]
        path = "/".join(segments)
        return f"{scheme}://{parsed.hostname.lower()}/{path}".rstrip("/")

    @staticmethod
    def _normalize_path_segment(segment: str) -> str:
        lowered = segment.lower()
        hexish = sum(1 for ch in lowered if ch in "0123456789abcdef")
        digit_count = sum(1 for ch in lowered if ch.isdigit())
        if segment.isdigit() or digit_count >= 4:
            return "{id}"
        if len(segment) >= 24 and hexish / max(len(segment), 1) > 0.75:
            return "{id}"
        return segment[:80]

    @staticmethod
    def _selectors_from_event(event: dict[str, Any]) -> list[str]:
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            return []
        out: list[str] = []
        for key in ("selector_value", "ref_value"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                out.append(value.strip()[:240])
        return out

    @classmethod
    def _scopes_from_events(cls, events: list[dict[str, Any]]) -> list[dict[str, str]]:
        seen: set[tuple[str, str, str]] = set()
        out: list[dict[str, str]] = []
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, dict):
                continue
            summary = metadata.get("observation_summary")
            if not isinstance(summary, dict):
                continue
            candidates: list[dict[str, Any]] = []
            scope = summary.get("scope")
            if isinstance(scope, dict):
                candidates.append({"selector": "", **scope})
            field_scopes = summary.get("field_scopes")
            if isinstance(field_scopes, list):
                candidates.extend(
                    item for item in field_scopes if isinstance(item, dict)
                )
            for item in candidates:
                selector = str(item.get("selector") or "").strip()[:240]
                frame_selector = str(item.get("frame_selector") or "").strip()[:240]
                shadow_host_selector = str(
                    item.get("shadow_host_selector") or ""
                ).strip()[:240]
                if not (frame_selector or shadow_host_selector):
                    continue
                key = (selector, frame_selector, shadow_host_selector)
                if key in seen:
                    continue
                seen.add(key)
                scope_item = {
                    "selector": selector,
                    "frame_selector": frame_selector,
                    "shadow_host_selector": shadow_host_selector,
                }
                out.append({key: value for key, value in scope_item.items() if value})
        return out

    @staticmethod
    def _adapter_id_from_event(event: dict[str, Any]) -> str:
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            return ""
        for key in ("planner_adapter_id", "adapter_id", "lantaicli_adapter_id"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value[:160]
        return ""

    @staticmethod
    def _derive_wait_conditions(action_sequence: list[str]) -> list[str]:
        conditions: list[str] = []
        if any(action in {"open", "navigate", "new_tab"} for action in action_sequence):
            conditions.append("wait for navigation and document readiness")
        if any(action in {"snapshot", "snapshot_dom", "snapshot_ax"} for action in action_sequence):
            conditions.append("capture DOM/AX snapshot before extracting data")
        if any(action in {"click", "click_at", "fill_form", "type"} for action in action_sequence):
            conditions.append("verify target element is visible before write action")
        if any(action in {"download_wait", "upload_file"} for action in action_sequence):
            conditions.append("wait for file transfer completion and explicit result")
        return conditions

    @staticmethod
    def _unique(values: Any) -> list[str]:
        out: list[str] = []
        for value in values:
            item = str(value or "").strip()
            if item and item not in out:
                out.append(item)
        return out

    @staticmethod
    def _sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
        from .agent.safety import redact_untrusted_text
        allowed = {
            "id",
            "ts",
            "run_id",
            "profile_id",
            "action",
            "runtime",
            "access",
            "risk",
            "decision",
            "reason",
            "ok",
            "url",
            "page_id",
            "error",
        }
        sanitized = {key: event.get(key) for key in allowed if key in event}
        for key in ("reason", "error", "url"):
            if sanitized.get(key):
                sanitized[key] = redact_untrusted_text(
                    sanitized[key],
                    limit=4096 if key == "url" else 1000,
                )
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            sanitized["metadata"] = BrowserReplayStore._sanitize_metadata(metadata)
        return sanitized

    @staticmethod
    def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        sanitized = sanitize_runtime_payload(metadata)
        if (
            metadata.get("data_classification") == "sensitive"
            and sanitized.get("data_classification") == "internal"
        ):
            sanitized["data_classification"] = "sensitive"
        return sanitized

    @staticmethod
    def _render_report(
        run_id: str,
        events: list[dict[str, Any]],
        copied: list[dict[str, str]],
    ) -> str:
        lines = [
            f"# Browser Replay Evidence: {run_id}",
            "",
            f"- Events: {len(events)}",
            f"- Evidence files: {len(copied)}",
            "",
            "## Timeline",
            "",
            "| # | Time | Action | Decision | Risk | OK | URL |",
            "|---:|---|---|---|---|---|---|",
        ]
        for index, event in enumerate(events, start=1):
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(index),
                        BrowserReplayStore._md_cell(event.get("ts", "")),
                        BrowserReplayStore._md_cell(event.get("action", "")),
                        BrowserReplayStore._md_cell(event.get("decision", "")),
                        BrowserReplayStore._md_cell(event.get("risk", "")),
                        BrowserReplayStore._md_cell(event.get("ok", "")),
                        BrowserReplayStore._md_cell(event.get("url", "")),
                    ]
                )
                + " |"
            )
        if copied:
            lines += ["", "## Screenshot Evidence", ""]
            for item in copied:
                lines.append(f"- `{item['file']}`")
        failures = [event for event in events if event.get("error")]
        if failures:
            lines += ["", "## Errors", ""]
            for event in failures:
                lines.append(
                    f"- `{event.get('action', '')}`: {event.get('error')}"
                )
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _md_cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")[:180]
