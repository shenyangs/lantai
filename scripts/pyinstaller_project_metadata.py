# -*- coding: utf-8 -*-
"""Select the installed project metadata without editable-source shadowing."""

from __future__ import annotations

import re
import sysconfig
from email.parser import Parser
from pathlib import Path


def project_distribution_data(
    expected_version: str,
    *,
    site_packages: Path | None = None,
) -> list[tuple[str, str]]:
    """Return the complete installed dist-info directory for PyInstaller datas.

    The spec adds src to sys.path, where an editable lantai.egg-info can shadow
    the installed wheel. Resolve from this interpreter's purelib explicitly,
    and reject missing, duplicate or stale metadata before freezing anything.
    """
    if (
        re.fullmatch(
            r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)",
            expected_version,
        )
        is None
    ):
        raise RuntimeError(
            f"Invalid Lantai release version: {expected_version!r}",
        )
    root = (
        site_packages
        if site_packages is not None
        else Path(sysconfig.get_path("purelib"))
    )
    distributions = sorted(root.glob("lantai-*.dist-info"))
    if len(distributions) != 1 or not distributions[0].is_dir():
        names = ", ".join(path.name for path in distributions) or "none"
        raise RuntimeError(
            "Expected exactly one installed Lantai dist-info directory "
            f"in {root}; found {names}. Run: uv sync --locked --extra dev "
            "--reinstall-package lantai",
        )

    directory = distributions[0]
    metadata_path = directory / "METADATA"
    try:
        metadata = Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            f"Could not read installed Lantai metadata {metadata_path}: {exc}",
        ) from exc
    if (
        directory.name != f"lantai-{expected_version}.dist-info"
        or metadata.get_all("Name") != ["lantai"]
        or metadata.get_all("Version") != [expected_version]
    ):
        raise RuntimeError(
            f"Installed Lantai metadata {metadata_path} does not match release "
            f"{expected_version}; reinstall the project with uv sync --locked "
            "--extra dev --reinstall-package lantai.",
        )
    return [(str(directory), directory.name)]
