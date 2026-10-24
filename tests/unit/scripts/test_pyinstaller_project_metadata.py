# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import sys
import sysconfig
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[3]
VERSION = "9.8.7"


def _write_metadata(
    root: Path,
    version: str = VERSION,
    *,
    name: str = "lantai",
    metadata_version: str | None = None,
) -> Path:
    directory = root / f"lantai-{version}.dist-info"
    directory.mkdir(parents=True)
    (directory / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\n"
        f"Version: {metadata_version or version}\n",
        encoding="utf-8",
    )
    return directory


def _spec_metadata_data(version: str) -> list[tuple[str, str]]:
    """Execute the spec's metadata collection without freezing binaries."""
    tree = ast.parse((ROOT / "lantai-server.spec").read_text(encoding="utf-8"))
    namespace: dict[str, Any] = {"datas": [], "release_version": version}
    statements = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module
        in {
            "PyInstaller.utils.hooks",
            "scripts.pyinstaller_project_metadata",
        }
    ]
    collections = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Attribute)
        and isinstance(statement.value.func.value, ast.Name)
        and statement.value.func.value.id == "datas"
        and statement.value.func.attr == "extend"
        and any(
            isinstance(argument, ast.Call)
            and isinstance(argument.func, ast.Name)
            and argument.func.id
            in {"copy_metadata", "project_distribution_data"}
            for argument in statement.value.args
        )
    ]
    assert len(collections) == 1
    module = ast.Module(body=statements + collections, type_ignores=[])
    exec(compile(module, "lantai-server.spec:metadata", "exec"), namespace)
    return namespace["datas"]


def test_spec_copies_installed_dist_info_when_source_egg_info_shadows_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site_packages = tmp_path / "site-packages"
    distribution = _write_metadata(site_packages)
    source = tmp_path / "src"
    egg_info = source / "lantai.egg-info"
    egg_info.mkdir(parents=True)
    (egg_info / "PKG-INFO").write_text(
        f"Metadata-Version: 2.4\nName: lantai\nVersion: {VERSION}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "path",
        [str(source), str(site_packages), *sys.path],
    )
    original_get_path = sysconfig.get_path
    monkeypatch.setattr(
        sysconfig,
        "get_path",
        lambda name, *args, **kwargs: (
            str(site_packages)
            if name == "purelib"
            else original_get_path(name, *args, **kwargs)
        ),
    )

    assert _spec_metadata_data(VERSION) == [
        (str(distribution), distribution.name),
    ]


def test_project_metadata_uses_the_installed_distribution(
    tmp_path: Path,
) -> None:
    from scripts.pyinstaller_project_metadata import project_distribution_data

    directory = _write_metadata(tmp_path)

    assert project_distribution_data(VERSION, site_packages=tmp_path) == [
        (str(directory), directory.name),
    ]


@pytest.mark.parametrize("egg_info_only", [False, True])
def test_project_metadata_rejects_missing_dist_info(
    tmp_path: Path,
    egg_info_only: bool,
) -> None:
    from scripts.pyinstaller_project_metadata import project_distribution_data

    if egg_info_only:
        egg = tmp_path / "lantai.egg-info"
        egg.mkdir()
        (egg / "PKG-INFO").write_text(
            f"Name: lantai\nVersion: {VERSION}\n",
            encoding="utf-8",
        )

    with pytest.raises(RuntimeError, match="exactly one"):
        project_distribution_data(VERSION, site_packages=tmp_path)


def test_project_metadata_rejects_duplicate_versions(tmp_path: Path) -> None:
    from scripts.pyinstaller_project_metadata import project_distribution_data

    _write_metadata(tmp_path)
    _write_metadata(tmp_path, "9.8.6")

    with pytest.raises(RuntimeError, match="exactly one"):
        project_distribution_data(VERSION, site_packages=tmp_path)


@pytest.mark.parametrize(
    ("directory_version", "name", "metadata_version"),
    [
        ("9.8.6", "lantai", "9.8.6"),
        (VERSION, "lantai", "9.8.6"),
        (VERSION, "another-package", VERSION),
    ],
)
def test_project_metadata_rejects_version_or_name_mismatch(
    tmp_path: Path,
    directory_version: str,
    name: str,
    metadata_version: str,
) -> None:
    from scripts.pyinstaller_project_metadata import project_distribution_data

    _write_metadata(
        tmp_path,
        directory_version,
        name=name,
        metadata_version=metadata_version,
    )

    with pytest.raises(RuntimeError, match="does not match"):
        project_distribution_data(VERSION, site_packages=tmp_path)


def test_project_metadata_rejects_duplicate_version_headers(
    tmp_path: Path,
) -> None:
    from scripts.pyinstaller_project_metadata import project_distribution_data

    directory = _write_metadata(tmp_path)
    metadata = directory / "METADATA"
    metadata.write_text(
        metadata.read_text(encoding="utf-8") + "Version: 9.8.6\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="does not match"):
        project_distribution_data(VERSION, site_packages=tmp_path)


def test_project_metadata_rejects_missing_metadata_file(
    tmp_path: Path,
) -> None:
    from scripts.pyinstaller_project_metadata import project_distribution_data

    directory = _write_metadata(tmp_path)
    (directory / "METADATA").unlink()

    with pytest.raises(RuntimeError, match="Could not read"):
        project_distribution_data(VERSION, site_packages=tmp_path)
