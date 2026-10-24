from scripts.build_dirty_worktree_ownership_ledger import (
    KNOWN_CLOSURES,
    SELF_EVIDENCE_PATH,
    classify_path,
    closure_for_path,
    parse_porcelain_v1_z,
    relative_evidence_path,
)


def test_parse_porcelain_v1_z_preserves_spaces_and_rename_sources() -> None:
    parsed = parse_porcelain_v1_z(
        b" M docs/file with spaces.md\0R  console/new.ts\0console/old.ts\0?? new.txt\0"
    )

    assert parsed == [
        {
            "status": " M",
            "path": "docs/file with spaces.md",
            "original_path": None,
        },
        {
            "status": "R ",
            "path": "console/new.ts",
            "original_path": "console/old.ts",
        },
        {"status": "??", "path": "new.txt", "original_path": None},
    ]


def test_every_known_closure_has_a_nonempty_name_and_stable_category() -> None:
    assert all(path and closure for path, closure in KNOWN_CLOSURES.items())
    assert SELF_EVIDENCE_PATH in KNOWN_CLOSURES
    assert classify_path("console/src/pages/Chat/runtimeEventStream.ts") == (
        "chat_runtime_integrity"
    )
    assert classify_path("src/lantai/customization/service.py") == (
        "companion_and_personalization"
    )
    assert classify_path("scripts/packaged_app_lifecycle_smoke.py") == (
        "desktop_lifecycle_and_release"
    )


def test_relative_evidence_path_tracks_the_actual_output_file(tmp_path) -> None:
    root = tmp_path / "repo"
    output = root / "docs" / "remediation" / "evidence" / "today.json"

    assert relative_evidence_path(root, output) == (
        "docs/remediation/evidence/today.json"
    )
    assert relative_evidence_path(root, tmp_path / "outside.json") is None


def test_actual_output_file_is_owned_by_the_audit_closure() -> None:
    current_output = "docs/remediation/evidence/dirty-worktree-ownership-today.json"

    assert closure_for_path(
        current_output,
        self_evidence_path=current_output,
    ) == "dirty_worktree_ownership_audit"
    assert closure_for_path(
        "unknown.txt",
        self_evidence_path=current_output,
    ) is None
