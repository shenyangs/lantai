"""Evidence and deliverable projections attached to the existing Runtime Run.

This module deliberately does not persist a second task or artifact state. It
projects research-specific facts from the authoritative Runtime task and
artifact-effect journals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Literal

from .artifact_effects import (
    RuntimeArtifactEffectJournal,
    RuntimeArtifactEffectRecord,
    resolve_artifact_effect_path,
)


EvidenceReadState = Literal["original", "snippet_only", "unread"]
MaterialScope = Literal["local_only", "approved_external", "local_sources", "public_search", "offline"]
EvidenceSupportStatus = Literal["unreviewed", "supports", "partial_support", "contradicts", "unrelated", "insufficient", "uncertain", "overclaim", "nonfactual"]
DeliveryValidationStatus = Literal[
    "validated",
    "superseded_task_revision",
    "artifact_not_committed",
    "artifact_missing_or_changed",
    "content_validation_pending",
    "visual_validation_pending",
]


@dataclass(frozen=True, slots=True)
class EvidenceNode:
    evidence_id: str
    source_uri: str
    source_hash: str
    claim: str
    summary: str
    read_state: EvidenceReadState = "original"
    contains_untrusted_instruction: bool = False
    support_status: EvidenceSupportStatus = "unreviewed"
    controlled_review_id: str | None = None
    report_hash: str | None = None

    @property
    def citation_status(self) -> Literal["verified_original", "needs_verification"]:
        """Original source identity only; reading never establishes semantic support."""
        return (
            "verified_original"
            if self.read_state == "original"
            else "needs_verification"
        )


@dataclass(frozen=True, slots=True)
class EvidenceConflict:
    topic: str
    evidence_ids: tuple[str, ...]


@dataclass(slots=True)
class EvidenceGraph:
    task_revision: int
    nodes: dict[str, EvidenceNode] = field(default_factory=dict)
    conflicts: list[EvidenceConflict] = field(default_factory=list)
    open_issues: list[str] = field(default_factory=list)

    def add(self, node: EvidenceNode) -> None:
        current = self.nodes.get(node.evidence_id)
        if current is not None and current != node:
            raise ValueError("evidence id was reused with different content")
        self.nodes[node.evidence_id] = node

    def record_conflict(self, topic: str, evidence_ids: tuple[str, ...]) -> None:
        if len(evidence_ids) < 2 or any(item not in self.nodes for item in evidence_ids):
            raise ValueError("conflict must reference at least two known evidence nodes")
        conflict = EvidenceConflict(topic=topic, evidence_ids=evidence_ids)
        if conflict not in self.conflicts:
            self.conflicts.append(conflict)

    def actionable_instructions(self) -> tuple[str, ...]:
        # Source text is evidence. Only the user's task contract can instruct Runtime.
        return ()

    @property
    def graph_hash(self) -> str:
        payload = {
            "task_revision": self.task_revision,
            "nodes": [
                {
                    "id": item.evidence_id,
                    "uri": item.source_uri,
                    "hash": item.source_hash,
                    "claim": item.claim,
                    "summary": item.summary,
                    "read_state": item.read_state,
                    "untrusted": item.contains_untrusted_instruction,
                    "support_status": item.support_status,
                    "controlled_review_id": item.controlled_review_id,
                    "report_hash": item.report_hash,
                }
                for item in sorted(self.nodes.values(), key=lambda node: node.evidence_id)
            ],
            "conflicts": [
                {"topic": item.topic, "evidence_ids": item.evidence_ids}
                for item in self.conflicts
            ],
            "open_issues": self.open_issues,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def project_research_evidence_graph(service, project_id: str) -> EvidenceGraph:
    """Project immutable research records; accept no caller supplied review facts."""
    bundle = service.evidence(project_id)
    project, report, review = bundle['project'], bundle['report'], bundle['review']
    graph = EvidenceGraph(task_revision=project['revision'])
    if report is None:
        graph.open_issues.append('No current report')
        return graph
    current_review = (review is not None and review['report_id'] == report['id']
        and review['report_hash'] == report['text_hash']
        and review['source_versions'] == project['source_versions']
        and not project['stale_sources'] and not project['stale_bibliography'])
    if not current_review:
        graph.open_issues.append('Current source and report versions require controlled semantic review')
    else:
        graph.open_issues.extend(review['issues'])
    evidence_index = 0
    span_indices = {}
    for claim in report['claims']:
        for span in claim.get('evidence', []):
            source = service.source_manifest(span['source_version_id'])
            span_key = (span['source_version_id'], span['start'], span['end'])
            review_index = span_indices.setdefault(span_key, len(span_indices))
            reviewed_claims = (review['claims'] + review.get('critical_review', {}).get('claims', [])) if current_review else []
            assessments = [c for c in reviewed_claims
                if review_index in c.get('evidence_indices', [])
                and c['start'] < claim['end'] and c['end'] > claim['start']] if current_review else []
            statuses = {c['assessment'] for c in assessments}
            # Conflicting assessments remain explicit; they cannot collapse to support.
            status = next((s for s in ('contradicts', 'overclaim', 'unrelated', 'insufficient', 'uncertain', 'partial_support', 'supports', 'nonfactual')
                if s in statuses), 'unreviewed')
            graph.add(EvidenceNode(evidence_id=f"{report['id']}:{evidence_index}",
                source_uri=source['path'], source_hash=source['raw_hash'],
                claim=report['text'][claim['start']:claim['end']], summary=span['quote'],
                read_state='original', support_status=status,
                controlled_review_id=review['id'] if current_review else None,
                report_hash=report['text_hash']))
            evidence_index += 1
    return graph


@dataclass(frozen=True, slots=True)
class DeliveryArtifact:
    artifact_id: str
    artifact_effect_id: str
    content_hash: str | None
    status: DeliveryValidationStatus
    delivery_key: str
    task_revision: int
    relative_path: str


@dataclass(frozen=True, slots=True)
class ResearchDeliveryContract:
    run_id: str
    attempt_id: str
    task_revision: int
    material_scope: MaterialScope
    evidence_graph_hash: str
    deliverable_kind: str
    visual_verification_required: bool = False
    configured_provider_ids: tuple[str, ...] = ()
    local_provider_ids: tuple[str, ...] = ("local",)

    def may_send_to_provider(self, provider_id: str) -> bool:
        if self.material_scope in {"local_only", "offline"}:
            return provider_id in self.local_provider_ids
        return provider_id in self.configured_provider_ids or provider_id in self.local_provider_ids


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class ResearchDeliveryCoordinator:
    """Read-only research projection over Runtime's artifact truth source."""

    contract: ResearchDeliveryContract
    artifact_journal: RuntimeArtifactEffectJournal
    workspace_dir: Path | str

    def project_artifact(
        self,
        *,
        artifact_effect_id: str,
        produced_for_task_revision: int,
        content_validated: bool,
        visual_validated: bool,
    ) -> DeliveryArtifact:
        record = self.artifact_journal.latest(
            self.contract.run_id,
            self.contract.attempt_id,
            artifact_effect_id,
        )
        if record is None:
            raise ValueError("artifact effect is not present in Runtime journal")
        return self._project(
            record,
            produced_for_task_revision=produced_for_task_revision,
            content_validated=content_validated,
            visual_validated=visual_validated,
        )

    def _project(
        self,
        record: RuntimeArtifactEffectRecord,
        *,
        produced_for_task_revision: int,
        content_validated: bool,
        visual_validated: bool,
    ) -> DeliveryArtifact:
        status: DeliveryValidationStatus
        if produced_for_task_revision != self.contract.task_revision:
            status = "superseded_task_revision"
        elif not (record.artifact_role == "primary" or
                  (record.artifact_role == "delivery" and record.research_export_id is not None)) or record.status not in {
            "committed",
            "delivered",
        }:
            status = "artifact_not_committed"
        else:
            resolved = resolve_artifact_effect_path(record, self.workspace_dir)
            if (
                not resolved
                or not record.sha256
                or _file_sha256(Path(resolved)) != record.sha256
            ):
                status = "artifact_missing_or_changed"
            elif not content_validated:
                status = "content_validation_pending"
            elif self.contract.visual_verification_required and not visual_validated:
                status = "visual_validation_pending"
            else:
                status = "validated"
        return DeliveryArtifact(
            artifact_id=record.artifact_id,
            artifact_effect_id=record.artifact_effect_id,
            content_hash=record.sha256,
            status=status,
            delivery_key=record.delivery_idempotency_key,
            task_revision=produced_for_task_revision,
            relative_path=record.relative_path,
        )


__all__ = [
    "DeliveryArtifact",
    "EvidenceConflict",
    "EvidenceGraph",
    "EvidenceNode",
    "ResearchDeliveryContract",
    "ResearchDeliveryCoordinator",
    "project_research_evidence_graph",
]
