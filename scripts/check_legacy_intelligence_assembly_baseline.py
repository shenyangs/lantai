#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail when protected Legacy intelligence assembly drifts without review."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = (
    ROOT
    / "docs"
    / "remediation"
    / "evidence"
    / "legacy-intelligence-assembly-r1-source-baseline-2026-08-30.json"
)
DEFAULT_TRANSITIONS = (
    ROOT
    / "docs"
    / "remediation"
    / "evidence"
    / "legacy-intelligence-assembly-reviewed-transitions-2026-08-31.json"
)
REQUIRED_CATEGORIES = {"prompt", "persona", "memory", "skills", "attachments"}


def _git_object_id(root: Path, path: str) -> str:
    return _git_object_id_at(root, "HEAD", path)


def _git_object_id_at(root: Path, commit: str, path: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", f"{commit}:{path}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_commit_id(root: Path, commit: str = "HEAD") -> str:
    result = subprocess.run(
        ["git", "rev-parse", commit],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_blob_id(path: Path) -> str:
    content = path.read_bytes()
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()  # noqa: S324 - Git object ID


def _read_object_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(path): str(object_id) for path, object_id in value.items()}


def _evidence_commit_matches(value: Any, commit: str) -> bool:
    evidence_commit = str(value or "").strip()
    return bool(evidence_commit) and commit.startswith(evidence_commit)


def check_baseline(
    root: Path,
    baseline_path: Path,
    transitions_path: Path = DEFAULT_TRANSITIONS,
) -> dict[str, Any]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    transition_manifest = json.loads(
        transitions_path.read_text(encoding="utf-8")
    )
    categories = set(baseline.get("protected_categories") or [])
    missing_categories = sorted(REQUIRED_CATEGORIES - categories)
    source_objects = _read_object_map(baseline.get("source_objects"))
    behavior_locks = baseline.get("behavior_locks")
    missing_behavior_locks = sorted(
        category
        for category in REQUIRED_CATEGORIES
        if not isinstance(behavior_locks, dict)
        or not behavior_locks.get(category)
    )

    baseline_integrity_errors: list[str] = []
    immutable_baseline = transition_manifest.get("immutable_baseline")
    if not isinstance(immutable_baseline, dict):
        immutable_baseline = {}
        baseline_integrity_errors.append("immutable baseline metadata is missing")
    expected_baseline_path = str(immutable_baseline.get("path") or "")
    try:
        actual_baseline_relative = baseline_path.resolve().relative_to(
            root.resolve()
        ).as_posix()
    except ValueError:
        actual_baseline_relative = str(baseline_path.resolve())
    if expected_baseline_path != actual_baseline_relative:
        baseline_integrity_errors.append(
            "immutable baseline path does not match reviewed manifest"
        )
    if str(immutable_baseline.get("sha256") or "") != _sha256(baseline_path):
        baseline_integrity_errors.append(
            "immutable baseline sha256 does not match reviewed manifest"
        )
    if str(immutable_baseline.get("git_blob") or "") != _git_blob_id(
        baseline_path
    ):
        baseline_integrity_errors.append(
            "immutable baseline Git blob does not match reviewed manifest"
        )

    transition_errors: list[str] = []
    reviewed_transition_ids: list[str] = []
    raw_transitions = transition_manifest.get("transitions")
    transitions = raw_transitions if isinstance(raw_transitions, list) else []
    if not transitions:
        transition_errors.append("reviewed transition chain is empty")

    previous_commit = ""
    previous_objects: dict[str, str] = {}
    terminal_commit = ""
    terminal_objects: dict[str, str] = {}
    for index, raw_transition in enumerate(transitions):
        if not isinstance(raw_transition, dict):
            transition_errors.append(f"transition {index} is not an object")
            continue
        transition_id = str(raw_transition.get("id") or f"#{index}")
        from_commit = str(raw_transition.get("from_commit") or "")
        to_commit = str(raw_transition.get("to_commit") or "")
        before_objects = _read_object_map(
            raw_transition.get("source_objects_before")
        )
        after_objects = _read_object_map(
            raw_transition.get("source_objects_after")
        )
        review = raw_transition.get("review")
        if not isinstance(review, dict) or review.get("status") != "approved":
            transition_errors.append(
                f"{transition_id}: transition is not explicitly approved"
            )
        else:
            reviewed_transition_ids.append(transition_id)
        if not from_commit or not to_commit:
            transition_errors.append(
                f"{transition_id}: from_commit/to_commit is missing"
            )
            continue
        if index == 0:
            expected_from = str(
                baseline.get("observation_baseline_commit") or ""
            )
            if from_commit != expected_from:
                transition_errors.append(
                    f"{transition_id}: first transition does not start at the observation baseline commit"
                )
            expected_r4 = str(
                (baseline.get("r4_execution_retirement") or {}).get(
                    "commit"
                )
            )
            if to_commit != expected_r4:
                transition_errors.append(
                    f"{transition_id}: first transition does not end at the reviewed R4 recapture"
                )
            if after_objects != source_objects:
                transition_errors.append(
                    f"{transition_id}: immutable baseline objects do not match the reviewed R4 recapture"
                )
        else:
            if from_commit != previous_commit:
                transition_errors.append(
                    f"{transition_id}: commit chain is not contiguous"
                )
            if before_objects != previous_objects:
                transition_errors.append(
                    f"{transition_id}: protected object chain is not contiguous"
                )
        if set(before_objects) != set(source_objects) or set(after_objects) != set(
            source_objects
        ):
            transition_errors.append(
                f"{transition_id}: protected source set differs from the immutable baseline"
            )
        changed_paths = sorted(
            path
            for path in set(before_objects) | set(after_objects)
            if before_objects.get(path) != after_objects.get(path)
        )
        if sorted(str(path) for path in raw_transition.get("changed_paths") or []) != changed_paths:
            transition_errors.append(
                f"{transition_id}: changed_paths does not match recorded object changes"
            )
        if not _git_is_ancestor(root, from_commit, to_commit):
            transition_errors.append(
                f"{transition_id}: to_commit does not descend from from_commit"
            )
        for commit, label, objects in (
            (from_commit, "before", before_objects),
            (to_commit, "after", after_objects),
        ):
            for path, expected in sorted(objects.items()):
                try:
                    actual = _git_object_id_at(root, commit, path)
                except subprocess.CalledProcessError:
                    actual = "<missing>"
                if actual != expected:
                    transition_errors.append(
                        f"{transition_id}: {label} object mismatch for {path}"
                    )

        evidence = raw_transition.get("evidence")
        if not isinstance(evidence, dict):
            transition_errors.append(
                f"{transition_id}: verification evidence is missing"
            )
        else:
            evidence_path = root / str(evidence.get("path") or "")
            if not evidence_path.is_file():
                transition_errors.append(
                    f"{transition_id}: verification evidence file is missing"
                )
            else:
                evidence_payload = json.loads(
                    evidence_path.read_text(encoding="utf-8")
                )
                if _sha256(evidence_path) != str(evidence.get("sha256") or ""):
                    transition_errors.append(
                        f"{transition_id}: verification evidence digest mismatch"
                    )
                for field in ("schema", "phase", "result"):
                    if evidence_payload.get(field) != evidence.get(field):
                        transition_errors.append(
                            f"{transition_id}: verification evidence {field} mismatch"
                        )
                commit_field = str(evidence.get("commit_field") or "")
                if not _evidence_commit_matches(
                    evidence_payload.get(commit_field), to_commit
                ):
                    transition_errors.append(
                        f"{transition_id}: verification evidence commit mismatch"
                    )
        previous_commit = to_commit
        previous_objects = after_objects
        terminal_commit = to_commit
        terminal_objects = after_objects

    head_commit = _git_commit_id(root)
    if terminal_commit and not _git_is_ancestor(root, terminal_commit, head_commit):
        transition_errors.append(
            "HEAD does not descend from the terminal reviewed transition"
        )
    mismatches = []
    for path, expected in sorted(terminal_objects.items()):
        try:
            actual = _git_object_id(root, path)
        except subprocess.CalledProcessError:
            actual = "<missing>"
        if actual != expected:
            mismatches.append(
                {"path": path, "expected": expected, "actual": actual}
            )
    return {
        "schema": "com.lantai.legacy-intelligence-assembly-r1-check.v2",
        "baseline": str(baseline_path),
        "transitions": str(transitions_path),
        "checked_source_count": len(source_objects),
        "missing_categories": missing_categories,
        "missing_behavior_locks": missing_behavior_locks,
        "baseline_integrity_errors": baseline_integrity_errors,
        "transition_errors": transition_errors,
        "reviewed_transition_ids": reviewed_transition_ids,
        "terminal_commit": terminal_commit,
        "head_commit": head_commit,
        "mismatches": mismatches,
        "passed": not (
            missing_categories
            or missing_behavior_locks
            or baseline_integrity_errors
            or transition_errors
            or mismatches
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--transitions", type=Path, default=DEFAULT_TRANSITIONS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = check_baseline(
            ROOT,
            args.baseline.expanduser(),
            args.transitions.expanduser(),
        )
    except Exception as exc:  # noqa: BLE001 - command-line diagnostics
        print(f"Legacy intelligence baseline check failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
