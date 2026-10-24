# -*- coding: utf-8 -*-
"""Collision-safe, isolated LibreOffice conversions for Office workflows."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ..utils import owned_subprocess as subprocess
from ..utils.operation_control import check_operation, publication_boundary, remaining_timeout


class OfficeConversionUnavailable(RuntimeError):
    """Raised when no supported local Office converter is installed."""


@dataclass(frozen=True)
class OfficeConversionResult:
    """A verified conversion copied into its final collision-safe location."""

    output_path: Path
    sha256: str
    converter: str


def find_soffice() -> str:
    """Find the configured/system LibreOffice binary without invoking it."""

    candidates = (
        os.environ.get("LANTAI_SOFFICE_PATH"),
        shutil.which("soffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise OfficeConversionUnavailable("LibreOffice soffice is unavailable")


def _normalized_extension(value: str) -> str:
    extension = str(value or "").strip().lower().lstrip(".")
    if not extension or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789"
        for char in extension
    ):
        raise ValueError("target extension must be alphanumeric")
    return extension


def _collision_safe_name(source: Path, target_extension: str) -> str:
    """Keep source format in names to prevent conversion collisions."""

    source_extension = source.suffix.lower().lstrip(".") or "source"
    return f"{source.stem}.{source_extension}.{target_extension}"


def _bundled_child_environment(soffice: str, profile: Path) -> dict[str, str] | None:
    """Pin the bundled macOS font resolver without changing the parent.

    System/custom installations keep their existing environment. Bundled
    resources must resolve inside the selected headless runtime, not through
    a symlink to a different system or user configuration.
    """
    executable = Path(soffice).resolve()
    runtime_root = next((
        parent for parent in executable.parents
        if parent.name == "libreoffice-headless"
    ), None)
    if (
        runtime_root is None or executable.parent.name != "MacOS"
        or executable.parent.parent.name != "Contents"
    ):
        return None
    configuration = (
        executable.parent.parent / "Resources" / "fontconfig" / "fonts.conf"
    ).resolve()
    if not configuration.is_relative_to(runtime_root) or not configuration.is_file():
        return None
    cache = profile / "font-cache"
    cache.mkdir()
    environment = os.environ.copy()
    environment.update({
        "FONTCONFIG_FILE": str(configuration),
        "FONTCONFIG_PATH": str(configuration.parent),
        "XDG_CACHE_HOME": str(cache),
    })
    return environment


def convert_office_document(
    source_path: str | Path,
    target_extension: str,
    output_dir: str | Path,
    *,
    output_name: str | None = None,
    timeout: float = 120,
    pdf_page_range: int | None = None,
) -> OfficeConversionResult:
    """Convert in a disposable profile and atomically publish the result.

    LibreOffice always writes ``<source.stem>.<target>``.  The wrapper moves
    that private intermediate to a name that also contains the source format,
    preventing ``report.docx`` and ``report.pptx`` from both becoming
    ``report.pdf`` in a shared output directory.
    """

    check_operation()
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    extension = _normalized_extension(target_extension)
    conversion_format = extension
    if pdf_page_range is not None:
        if (
            extension != "pdf" or source.suffix.lower() != ".pptx"
            or type(pdf_page_range) is not int or pdf_page_range <= 0
        ):
            raise ValueError("pdf_page_range must be a positive PPTX slide number")
        conversion_format = "pdf:impress_pdf_Export:" + json.dumps({
            "PageRange": {"type": "string", "value": str(pdf_page_range)},
            "ExportHiddenSlides": {"type": "boolean", "value": "true"},
        }, separators=(",", ":"))
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    final_name = output_name or _collision_safe_name(source, extension)
    if (
        Path(final_name).name != final_name
        or Path(final_name).suffix.lower() != f".{extension}"
    ):
        raise ValueError(
            "output_name must be a basename with the target extension"
        )
    final_path = destination / final_name

    with tempfile.TemporaryDirectory(
        prefix="lantai-office-convert-"
    ) as temporary:
        temporary_path = Path(temporary)
        profile = temporary_path / "profile"
        converted = temporary_path / "converted"
        profile.mkdir()
        converted.mkdir()
        profile_uri = f"file://{quote(str(profile))}"
        soffice = find_soffice()
        child_environment = _bundled_child_environment(soffice, profile)
        completed = subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={profile_uri}",
                "--headless",
                "--convert-to",
                conversion_format,
                "--outdir",
                str(converted),
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=remaining_timeout(float(timeout)),
            check=False,
            **({"env": child_environment} if child_environment is not None else {}),
        )
        generated = converted / f"{source.stem}.{extension}"
        if completed.returncode != 0 or not generated.is_file():
            detail = (
                completed.stderr
                or completed.stdout
                or "unknown conversion error"
            ).strip()
            raise RuntimeError(f"LibreOffice conversion failed: {detail}")
        if generated.stat().st_size <= 0:
            raise RuntimeError("LibreOffice conversion produced an empty file")
        final_fd, final_temporary = tempfile.mkstemp(prefix=f".{final_name}.", suffix=".tmp", dir=destination)
        os.close(final_fd)
        temporary_final = Path(final_temporary)
        try:
            check_operation()
            shutil.copy2(generated, temporary_final)
            with publication_boundary():
                temporary_final.replace(final_path)
        finally:
            temporary_final.unlink(missing_ok=True)

    digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
    return OfficeConversionResult(
        output_path=final_path,
        sha256=f"sha256:{digest}",
        converter="libreoffice",
    )


def recalculate_xlsx(
    path: str | Path, *, timeout: int = 120
) -> OfficeConversionResult:
    """Recalculate an XLSX through LibreOffice and atomically replace it."""

    source = Path(path).expanduser().resolve()
    if source.suffix.lower() != ".xlsx":
        raise ValueError("recalculate_xlsx only accepts .xlsx files")
    with tempfile.TemporaryDirectory(
        prefix="lantai-xlsx-recalc-"
    ) as temporary:
        working = Path(temporary) / f"recalc-{source.name}"
        shutil.copy2(source, working)
        result = convert_office_document(
            working,
            "xlsx",
            Path(temporary) / "output",
            output_name="recalculated.xlsx",
            timeout=timeout,
        )
        replacement = source.with_name(
            f".{source.name}.{os.getpid()}.recalc.tmp"
        )
        shutil.copy2(result.output_path, replacement)
        with publication_boundary():
            replacement.replace(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return OfficeConversionResult(
        output_path=source,
        sha256=f"sha256:{digest}",
        converter=result.converter,
    )
