"""Authored wire fixtures; never an independent semantic quality judgment."""

from __future__ import annotations

import json


def synthetic_review_response(kwargs, findings):
    """Add protocol records without changing any existing finding/assertion.

    These literals exercise parsing and associations only. They are not
    Provider replies, source truth, benchmark evidence, or a finding waiver.
    """

    context = json.loads(kwargs["user_content"][1]["text"])
    grounding = context.get("grounding", [context])
    source_pages = [
        page for source in grounding if source.get("source_brief") is not None
        for page in source.get("slide_ids", [source.get("slide_id")])
    ]
    payload = {"issues": findings}
    if not source_pages:
        return payload

    def check(page, kind, objects, verdict, indices):
        return {
            "slide_id": page, "object_ids": objects, "check_type": kind,
            "verdict": verdict, "source_refs": ["source_brief"],
            "source_evidence": "Authored synthetic parser fixture: original input exists.",
            "candidate_evidence": "Authored synthetic parser fixture: candidate input exists.",
            "explanation": "Exercise protocol linkage, not semantic approval.",
            "issue_indices": indices,
        }

    checks = [check(
        finding["slide_id"], finding["issue_type"], finding.get("object_ids", []),
        "verified_defect", [index],
    ) for index, finding in enumerate(findings)
        if finding.get("issue_type") in {"content_accuracy", "source_coverage"}]
    for kind in ("content_accuracy", "source_coverage"):
        if not any(item["check_type"] == kind for item in checks):
            checks.append(check(source_pages[0], kind, [], "verified_satisfied", []))
    payload["verification"] = checks
    return payload
