# -*- coding: utf-8 -*-
"""Unknown-version, duplicate, and fork governance for projections."""

from lantai.runtime.coherence import govern_projection_replay


def test_unknown_event_type_is_preserved_but_not_applied() -> None:
    report = govern_projection_replay(
        [
            {"schema_version": 1, "id": "e1", "seq": 1, "type": "known"},
            {"schema_version": 1, "id": "e2", "seq": 2, "type": "future"},
        ],
        projection_id="surface-v1",
        supported_event_types={"known"},
    )

    assert report.applied_event_ids == ("e1",)
    assert report.decisions[1].disposition == "ignored_unknown_type"
    assert report.decisions[1].event_sha256
    assert report.forked is False
    assert report.verify_integrity()


def test_future_schema_is_quarantined_without_guessing() -> None:
    report = govern_projection_replay(
        [{"schema_version": 2, "id": "e1", "seq": 1, "type": "known"}],
        projection_id="surface-v1",
        supported_event_types={"known"},
        supported_event_schema=1,
    )

    assert report.applied_event_ids == ()
    assert report.decisions[0].disposition == "quarantined_future_schema"


def test_exact_duplicate_is_idempotent_but_conflicting_identity_forks() -> (
    None
):
    original = {"schema_version": 1, "id": "e1", "seq": 1, "type": "known"}
    report = govern_projection_replay(
        [
            original,
            dict(original),
            {
                "schema_version": 1,
                "id": "e1",
                "seq": 2,
                "type": "known",
                "payload": {"changed": True},
            },
        ],
        projection_id="surface-v1",
        supported_event_types={"known"},
    )

    assert [row.disposition for row in report.decisions] == [
        "applied",
        "ignored_duplicate",
        "forked_conflict",
    ]
    assert report.forked is True
