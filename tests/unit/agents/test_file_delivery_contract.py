# -*- coding: utf-8 -*-
"""Regression coverage for action-driven file delivery evidence."""

from pathlib import Path
import zipfile

from lantai.runtime.file_delivery import (
    has_matching_file_delivery_block,
    resolve_workspace_deliverable_path,
    select_archive_member_delivery_paths,
)


def test_ppt_delivery_requires_a_real_pptx_attachment() -> None:
    expected = frozenset({".pptx"})

    assert not has_matching_file_delivery_block(
        [
            {
                "type": "file",
                "filename": "build_ppt.js",
                "source": {
                    "type": "url",
                    "url": "file:///tmp/build_ppt.js",
                },
            },
        ],
        expected,
    )
    assert has_matching_file_delivery_block(
        [
            {
                "type": "file",
                "filename": "董事会汇报.pptx",
                "source": {
                    "type": "url",
                    "url": "file:///tmp/%E8%91%A3%E4%BA%8B%E4%BC%9A.pptx",
                },
            },
        ],
        expected,
    )


def test_model_declared_delivery_path_stays_inside_workspace(tmp_path: Path) -> None:
    page = tmp_path / "页面.html"
    page.write_text("<!doctype html>", encoding="utf-8")
    outside = tmp_path.parent / "outside.html"
    outside.write_text("outside", encoding="utf-8")
    source_file = tmp_path / "build.py"
    source_file.write_text("print('build')", encoding="utf-8")
    symlink = tmp_path / "linked.html"
    symlink.symlink_to(outside)

    assert resolve_workspace_deliverable_path(tmp_path, page.name) == str(
        page.resolve()
    )
    assert resolve_workspace_deliverable_path(tmp_path, "../outside.html") is None
    assert resolve_workspace_deliverable_path(tmp_path, source_file.name) is None
    assert resolve_workspace_deliverable_path(tmp_path, symlink.name) is None


def test_zip_delivery_selects_each_table_but_not_supporting_files(
    tmp_path: Path,
) -> None:
    dataset_dir = tmp_path / "ai_office_dataset"
    dataset_dir.mkdir()
    csv_paths = [dataset_dir / f"table_{index}.csv" for index in range(5)]
    for index, path in enumerate(csv_paths):
        path.write_text(f"month,value\n2026-0{index + 1},{index}\n", encoding="utf-8")
    readme = dataset_dir / "README.md"
    readme.write_text("# dataset", encoding="utf-8")
    archive_path = tmp_path / "dataset.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in [*csv_paths, readme]:
            archive.write(path, arcname=path.name)

    selected = select_archive_member_delivery_paths(tmp_path, archive_path)

    assert selected == tuple(str(path.resolve()) for path in csv_paths)


def test_zip_delivery_does_not_duplicate_a_single_table(tmp_path: Path) -> None:
    csv_path = tmp_path / "only.csv"
    csv_path.write_text("value\n1\n", encoding="utf-8")
    archive_path = tmp_path / "only.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(csv_path, arcname=csv_path.name)

    assert select_archive_member_delivery_paths(tmp_path, archive_path) == ()


def test_zip_delivery_supports_mixed_deliverable_types(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "campaign_bundle"
    bundle_dir.mkdir()
    deliverables = [
        bundle_dir / "brief.pdf",
        bundle_dir / "cover.png",
        bundle_dir / "voiceover.mp3",
        bundle_dir / "landing-page.html",
    ]
    for path in deliverables:
        path.write_bytes(b"deliverable")
    readme = bundle_dir / "README.txt"
    readme.write_text("package notes", encoding="utf-8")
    build_script = bundle_dir / "build.py"
    build_script.write_text("print('build')", encoding="utf-8")
    archive_path = tmp_path / "campaign_bundle.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in [*deliverables, readme, build_script]:
            archive.write(path, arcname=path.name)

    selected = select_archive_member_delivery_paths(tmp_path, archive_path)

    assert selected == tuple(str(path.resolve()) for path in deliverables)
