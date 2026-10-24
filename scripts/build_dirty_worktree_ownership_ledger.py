#!/usr/bin/env python3
"""Build a complete, machine-readable inventory of an uncommitted worktree."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


KNOWN_CLOSURES = {
    "console/electron/companionDepthRendererSmoke.cjs": "dynamic_research_advisory_and_companion_depth_gate",
    "console/src/api/types/utilityIntelligence.ts": "dynamic_research_advisory",
    "console/src/pages/Chat/components/QuquResponseCard.test.tsx": "ququ_creation_boundary",
    "console/src/pages/Chat/components/QuquResponseCard.tsx": "dynamic_research_advisory_and_single_request_boundary",
    "console/src/pages/Chat/components/StructuredContent.tsx": "dynamic_research_advisory",
    "console/src/pages/Chat/components/utilityArtifactClassification.ts": "dynamic_research_advisory_and_semantic_artifacts",
    "console/src/pages/Chat/index.module.less": "dynamic_research_advisory",
    "console/src/pages/Chat/runtimeEventStream.test.ts": "p0_incident_library_and_session_isolation",
    "console/src/pages/Chat/runtimeEventSubscriptionRegistry.test.ts": "p0_incident_library_and_session_isolation",
    "docs/remediation/lantai-2.0-internal-candidate-audit-2026-08-14.md": "internal_candidate_release_truth",
    "docs/remediation/lantai-2.0-remediation-ledger-2026-08-13.md": "remediation_master_ledger",
    "docs/remediation/lantai-2.0-candidate-completion-audit-2026-08-22-2.0.2-r13.md": "r13_candidate_completion_audit",
    "docs/remediation/chat-runtime-incidents-audit-2026-08-21-zh.md": "chat_p0_incident_audit",
    "docs/remediation/persona-cross-review-complete-record-2026-08-22-2.0.2-r13.md": "r13_persona_blind_review_record",
    "docs/remediation/evidence/memory-ownership-regression-2026-08-14.json": "memory_ownership_release_gate",
    "docs/remediation/persona-cross-review-complete-record-2026-08-14.md": "persona_blind_review_record",
    "docs/remediation/unauthorized-deletion-ledger-2026-08-14.md": "unauthorized_deletion_audit",
    "scripts/build_dirty_worktree_ownership_ledger.py": "dirty_worktree_ownership_audit",
    "scripts/packaged_companion_depth_smoke.py": "dynamic_research_advisory_and_companion_depth_gate",
    "scripts/run_packaged_agent_sustained_soak.py": "r13_short_workload_completion_gate",
    "scripts/check_lantai_2_memory_ownership_regression.sh": "memory_ownership_release_gate",
    "scripts/packaged_memory_ownership_smoke.py": "packaged_memory_ownership_release_gate",
    "scripts/refresh_lantai_2_internal_candidate_audit.py": "internal_candidate_evidence_refresh",
    "scripts/packaged_auto_update_install_recovery_smoke.py": "deferred_auto_update_harness",
    "tests/fixtures/runtime/incidents/p0_incident_library_v1.json": "p0_incident_library_and_session_isolation",
    "tests/fixtures/memory/memory_ledger_v1.json": "memory_ownership_release_gate",
    "tests/unit/agents/memory/test_memory_ledger_golden.py": "memory_ownership_release_gate",
    "tests/unit/runtime/test_p0_incident_library.py": "p0_incident_library_and_session_isolation",
    "tests/unit/scripts/test_build_dirty_worktree_ownership_ledger.py": "dirty_worktree_ownership_audit",
    "tests/unit/scripts/test_packaged_companion_depth_smoke.py": "dynamic_research_advisory_and_companion_depth_gate",
    "tests/unit/scripts/test_run_packaged_agent_sustained_soak.py": "r13_short_workload_completion_gate",
    "tests/unit/scripts/test_memory_ownership_regression_gate.py": "memory_ownership_release_gate",
    "tests/unit/scripts/test_packaged_memory_ownership_smoke.py": "packaged_memory_ownership_release_gate",
    "tests/unit/scripts/test_refresh_lantai_2_internal_candidate_audit.py": "internal_candidate_evidence_refresh",
    "docs/remediation/evidence/dirty-worktree-ownership-2026-08-14.json": "dirty_worktree_ownership_audit",
    "src/lantai/app/utility_intelligence.py": "dynamic_research_advisory_and_semantic_artifacts",
    "tests/unit/app/test_utility_intelligence.py": "dynamic_research_advisory_and_semantic_artifacts",
}
SELF_EVIDENCE_PATH = (
    "docs/remediation/evidence/dirty-worktree-ownership-2026-08-14.json"
)


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=root)


def parse_porcelain_v1_z(payload: bytes) -> list[dict[str, str | None]]:
    """Parse ``git status --porcelain=v1 -z`` including rename pairs."""

    fields = payload.decode("utf-8", errors="surrogateescape").split("\0")
    entries: list[dict[str, str | None]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            raise ValueError(f"Malformed porcelain record: {record!r}")
        status = record[:2]
        path = record[3:]
        original_path: str | None = None
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                raise ValueError(f"Missing rename source for: {record!r}")
            original_path = fields[index]
            index += 1
        entries.append(
            {
                "status": status,
                "path": path,
                "original_path": original_path,
            }
        )
    return entries


def classify_path(path: str) -> str:
    normalized = path.lower()
    if path.startswith("console/electron/") or "packaged_" in normalized:
        return "desktop_lifecycle_and_release"
    if (
        path.startswith("console/src/pages/Chat/")
        or path.startswith("console/src/vendor/lantaiChat")
        or path.startswith("src/lantai/runtime/")
    ):
        return "chat_runtime_integrity"
    if any(token in normalized for token in ("customization", "persona", "ququ", "memory")):
        return "companion_and_personalization"
    if any(token in normalized for token in ("office", "docx", "xlsx", "pptx")):
        return "office_deferred_this_cycle"
    if "browser" in normalized:
        return "browser_workbench"
    if path.startswith("docs/"):
        return "documentation_and_governance"
    if path.startswith("scripts/") or path.startswith("release/"):
        return "verification_and_release_evidence"
    if path.startswith("tests/"):
        return "verification"
    if path.startswith("console/"):
        return "console_product_surface"
    if path.startswith("src/"):
        return "backend_product_core"
    return "build_and_repository_infrastructure"


def _sha256(path: Path) -> str | None:
    if path.is_symlink():
        return hashlib.sha256(path.readlink().as_posix().encode()).hexdigest()
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_evidence_path(root: Path, output: Path) -> str | None:
    """Return the repository-relative output path when it belongs to ``root``."""

    try:
        return output.expanduser().resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def closure_for_path(path: str, *, self_evidence_path: str | None) -> str | None:
    if self_evidence_path is not None and path == self_evidence_path:
        return "dirty_worktree_ownership_audit"
    return KNOWN_CLOSURES.get(path)


def build_ledger(
    root: Path,
    *,
    self_evidence_path: str | None = SELF_EVIDENCE_PATH,
) -> dict[str, Any]:
    raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    parsed = parse_porcelain_v1_z(raw)
    entries: list[dict[str, Any]] = []
    for item in parsed:
        path = str(item["path"])
        closure = closure_for_path(path, self_evidence_path=self_evidence_path)
        entries.append(
            {
                **item,
                "category": classify_path(path),
                "working_sha256": (
                    None if path == self_evidence_path else _sha256(root / path)
                ),
                "hash_omitted_reason": (
                    "self_generated_output" if path == self_evidence_path else None
                ),
                "provenance_state": (
                    "known_current_goal_closure"
                    if closure
                    else "explicit_unresolved_conflict"
                ),
                "closure": closure,
                "release_disposition": (
                    "include_after_clean_commit_and_rebuild"
                    if closure
                    else "requires_owner_or_disposition_before_public_release"
                ),
            }
        )

    status_counts = Counter(str(item["status"]) for item in entries)
    category_counts = Counter(str(item["category"]) for item in entries)
    provenance_counts = Counter(str(item["provenance_state"]) for item in entries)
    source_sha = _git(root, "rev-parse", "HEAD").decode().strip()
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository_root": str(root),
        "source_head_sha": source_sha,
        "release_clean": not entries,
        "public_release_allowed_by_this_gate": not entries,
        "policy": {
            "known_current_goal_closure": "Known in-scope work, still requires a clean commit boundary and rebuild.",
            "explicit_unresolved_conflict": "Ownership cannot be reconstructed safely from an uncommitted diff; do not discard, commit, or release without an owner decision.",
        },
        "summary": {
            "entry_count": len(entries),
            "status_counts": dict(sorted(status_counts.items())),
            "category_counts": dict(sorted(category_counts.items())),
            "provenance_counts": dict(sorted(provenance_counts.items())),
        },
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    output = args.output.expanduser()
    if not output.is_absolute():
        output = root / output
    payload = build_ledger(
        root,
        self_evidence_path=relative_evidence_path(root, output),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
