# -*- coding: utf-8 -*-
"""Evidence-based file-delivery result contracts and helpers."""

from __future__ import annotations

import os
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse


FILE_DELIVERY_MEDIA_TYPES = frozenset({"file", "image", "audio", "video"})
FILE_DELIVERY_TOOL_NAME = "send_file_to_user"


GeneratedFileSnapshot = dict[str, tuple[int, int]]

_DELIVERABLE_EXTENSIONS = frozenset(
    {
        ".csv",
        ".doc",
        ".docx",
        ".gif",
        ".htm",
        ".html",
        ".jpeg",
        ".jpg",
        ".json",
        ".key",
        ".m4a",
        ".md",
        ".mov",
        ".mp3",
        ".mp4",
        ".numbers",
        ".odp",
        ".ods",
        ".odt",
        ".pages",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".rtf",
        ".svg",
        ".txt",
        ".wav",
        ".webp",
        ".xls",
        ".xlsx",
        ".zip",
    },
)
_DELIVERABLE_EXTENSION_PRIORITY = {
    extension: index
    for index, extension in enumerate(
        (
            ".pptx",
            ".docx",
            ".xlsx",
            ".pdf",
            ".zip",
            ".ppt",
            ".doc",
            ".xls",
            ".key",
            ".pages",
            ".numbers",
            ".odt",
            ".ods",
            ".odp",
            ".rtf",
            ".html",
            ".htm",
            ".md",
            ".csv",
            ".txt",
            ".json",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".svg",
            ".gif",
            ".mp3",
            ".wav",
            ".m4a",
            ".mp4",
            ".mov",
        ),
    )
}
_SNAPSHOT_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    },
)
_SNAPSHOT_SKIPPED_FILENAMES = frozenset(
    {
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "yarn.lock",
    },
)

_ARCHIVE_MEMBER_EXTENSIONS = _DELIVERABLE_EXTENSIONS - {".zip"}
_ARCHIVE_SUPPORT_FILENAMES = frozenset(
    {"readme.md", "readme.txt"}
)
_MAX_ARCHIVE_MEMBER_ATTACHMENTS = 20


def snapshot_generated_files(workspace_dir: str | None) -> GeneratedFileSnapshot:
    """Capture user-facing artifact files without reading their contents."""

    root = Path(str(workspace_dir or "")).expanduser()
    if not str(workspace_dir or "").strip() or not root.is_dir():
        return {}
    snapshot: GeneratedFileSnapshot = {}
    try:
        for current_root, directory_names, file_names in os.walk(
            root,
            followlinks=False,
        ):
            directory_names[:] = [
                name
                for name in directory_names
                if name not in _SNAPSHOT_SKIPPED_DIRECTORIES
                and not name.startswith(".")
            ]
            for name in file_names:
                if (
                    name.startswith(".")
                    or name in _SNAPSHOT_SKIPPED_FILENAMES
                ):
                    continue
                candidate = Path(current_root) / name
                if (
                    candidate.suffix.lower() not in _DELIVERABLE_EXTENSIONS
                    or candidate.is_symlink()
                ):
                    continue
                try:
                    stat_result = candidate.stat()
                except OSError:
                    continue
                snapshot[str(candidate)] = (
                    int(stat_result.st_size),
                    int(stat_result.st_mtime_ns),
                )
    except OSError:
        return snapshot
    return snapshot


def resolve_workspace_deliverable_path(
    workspace_dir: str | None,
    raw_path: object,
    *,
    require_file: bool = True,
) -> str | None:
    """Resolve one deliverable path without allowing workspace escape."""

    raw_workspace = str(workspace_dir or "").strip()
    raw_candidate = str(raw_path or "").strip()
    if not raw_workspace or not raw_candidate:
        return None
    root = Path(raw_workspace).expanduser().resolve(strict=False)
    candidate = Path(raw_candidate).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=require_file)
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    if resolved.suffix.lower() not in _DELIVERABLE_EXTENSIONS:
        return None
    if require_file and not resolved.is_file():
        return None
    return str(resolved)


def select_generated_delivery_paths(
    workspace_dir: str | None,
    baseline: GeneratedFileSnapshot,
    expected_extensions: Iterable[str] = (),
) -> tuple[str, ...]:
    """Select files created or changed during a file-generating turn."""

    current = snapshot_generated_files(workspace_dir)
    expected = {
        str(extension).lower()
        if str(extension).startswith(".")
        else f".{str(extension).lower()}"
        for extension in expected_extensions
        if str(extension).strip()
    }
    changed = [
        path
        for path, fingerprint in current.items()
        if baseline.get(path) != fingerprint
        and (not expected or Path(path).suffix.lower() in expected)
    ]
    changed.sort(
        key=lambda path: (
            _DELIVERABLE_EXTENSION_PRIORITY.get(
                Path(path).suffix.lower(),
                len(_DELIVERABLE_EXTENSION_PRIORITY),
            ),
            -current[path][1],
            len(Path(path).parts),
            path,
        ),
    )
    if not expected:
        return tuple(changed[:1])

    selected: list[str] = []
    selected_extensions: set[str] = set()
    for path in changed:
        extension = Path(path).suffix.lower()
        if extension in selected_extensions:
            continue
        selected.append(path)
        selected_extensions.add(extension)
        if len(selected) >= min(4, len(expected)):
            break
    return tuple(selected)


def select_archive_member_delivery_paths(
    workspace_dir: str | None,
    archive_path: object,
) -> tuple[str, ...]:
    """Find separately downloadable deliverables represented by one ZIP.

    The archive remains the batch download.  When it contains two or more
    user-facing files that also exist in the turn workspace, return those real
    files in archive order so the runtime can expose one named attachment per
    member. Conventional package support files are omitted, while documents,
    tables, images, audio, video, and other deliverables are all supported.
    ZIP entries are only used as an index; this helper never extracts content.
    """

    resolved_archive = resolve_workspace_deliverable_path(
        workspace_dir,
        archive_path,
    )
    if not resolved_archive or Path(resolved_archive).suffix.lower() != ".zip":
        return ()

    root = Path(str(workspace_dir or "")).expanduser().resolve(strict=False)
    current_paths = [
        Path(path).resolve(strict=False)
        for path in snapshot_generated_files(str(root))
        if Path(path).suffix.lower() in _ARCHIVE_MEMBER_EXTENSIONS
        and Path(path).name.casefold() not in _ARCHIVE_SUPPORT_FILENAMES
    ]
    relative_candidates: list[tuple[tuple[str, ...], Path]] = []
    for candidate in current_paths:
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        relative_candidates.append((relative.parts, candidate))

    selected: list[str] = []
    selected_set: set[str] = set()
    try:
        with zipfile.ZipFile(resolved_archive) as archive:
            members = archive.infolist()
            for member in members:
                if member.is_dir():
                    continue
                member_path = PurePosixPath(member.filename)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or member_path.suffix.lower() not in _ARCHIVE_MEMBER_EXTENSIONS
                    or member_path.name.casefold() in _ARCHIVE_SUPPORT_FILENAMES
                ):
                    continue
                member_parts = tuple(part for part in member_path.parts if part != ".")
                matches = [
                    candidate
                    for relative_parts, candidate in relative_candidates
                    if len(relative_parts) >= len(member_parts)
                    and relative_parts[-len(member_parts) :] == member_parts
                ]
                if len(matches) != 1:
                    continue
                resolved = str(matches[0])
                if resolved in selected_set:
                    continue
                selected.append(resolved)
                selected_set.add(resolved)
                if len(selected) >= _MAX_ARCHIVE_MEMBER_ATTACHMENTS:
                    break
    except (OSError, zipfile.BadZipFile):
        return ()

    return tuple(selected) if len(selected) >= 2 else ()


def has_file_delivery_block(content: Iterable[Any]) -> bool:
    """Return whether tool output contains a real deliverable media block."""

    return any(
        isinstance(item, dict)
        and str(item.get("type") or "") in FILE_DELIVERY_MEDIA_TYPES
        for item in content
    )


def file_path_matches_extensions(
    file_path: str | None,
    expected_extensions: Iterable[str],
) -> bool:
    """Return whether a path or file URL has one of the expected suffixes."""

    expected = {
        extension.lower()
        if str(extension).startswith(".")
        else f".{str(extension).lower()}"
        for extension in expected_extensions
        if str(extension).strip()
    }
    if not expected:
        return True
    raw = str(file_path or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    candidate = unquote(parsed.path) if parsed.scheme else raw
    return Path(candidate).suffix.lower() in expected


def expected_file_type_label(expected_extensions: Iterable[str]) -> str:
    """Return a compact reader-facing label such as ``PPTX``."""

    labels = [
        str(extension).strip().lstrip(".").upper()
        for extension in sorted(set(expected_extensions))
        if str(extension).strip()
    ]
    return " / ".join(labels) if labels else "文件"


def has_matching_file_delivery_block(
    content: Iterable[Any],
    expected_extensions: Iterable[str],
) -> bool:
    """Require a real media block whose file type matches the request."""

    expected = frozenset(expected_extensions)
    for item in content:
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or "")
        if block_type not in FILE_DELIVERY_MEDIA_TYPES:
            continue
        if not expected:
            return True
        candidates = [item.get("filename")]
        source = item.get("source")
        if isinstance(source, dict):
            candidates.extend((source.get("url"), source.get("path")))
        else:
            candidates.append(source)
        if any(
            file_path_matches_extensions(candidate, expected)
            for candidate in candidates
        ):
            return True
    return False
