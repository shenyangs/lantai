"""Safe, bounded cleanup for disposable Lantai local data."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .constant import WORKING_DIR

MEBIBYTE = 1024 * 1024
DEFAULT_POLICY = {
    "logs_cache_days": 14,
    "logs_cache_max_bytes": 200 * MEBIBYTE,
    "runtime_days": 30,
    "runtime_max_bytes": 500 * MEBIBYTE,
    "generated_images_days": 30,
}
_MAX_DAYS = 3650
_MAX_BYTES = 100 * 1024 * MEBIBYTE
_GENERATED_IMAGE_DAY_CHOICES = {0, 3, 7, 15, 30}


def _settings_path(root: Path) -> Path:
    return root / "settings.json"


def _read_settings(root: Path) -> dict[str, Any]:
    try:
        value = json.loads(_settings_path(root).read_text("utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _normalize(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_POLICY)
    for key, default in DEFAULT_POLICY.items():
        candidate = raw.get(key, default)
        if not isinstance(candidate, int) or isinstance(candidate, bool):
            continue
        if key == "generated_images_days":
            if candidate in _GENERATED_IMAGE_DAY_CHOICES:
                result[key] = candidate
            continue
        maximum = _MAX_DAYS if key.endswith("_days") else _MAX_BYTES
        result[key] = max(0, min(candidate, maximum))
    return result


def get_storage_retention_policy(root: Path = WORKING_DIR) -> dict[str, int]:
    return _normalize(_read_settings(root).get("storage_retention"))


def update_storage_retention_policy(
    patch: dict[str, Any], root: Path = WORKING_DIR
) -> dict[str, int]:
    current = get_storage_retention_policy(root)
    updated = _normalize({**current, **patch})
    data = _read_settings(root)
    data["storage_retention"] = updated
    path = _settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)
    return updated


def _regular_files(root: Path):
    if not root.is_dir():
        return
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories if not (current_path / name).is_symlink()
        ]
        for name in files:
            path = current_path / name
            if not path.is_symlink() and path.is_file():
                yield path


def _category(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    normalized_parts = tuple(part.lower() for part in relative.parts)
    parts = set(normalized_parts)
    if normalized_parts[:2] == ("media", "generated_images"):
        return "generated_images"
    if any(
        normalized_parts[index : index + 2] == (".lantai", "generated_images")
        for index in range(max(0, len(normalized_parts) - 1))
    ):
        return "generated_images"
    if any(
        part in {"logs", "log", "cache", "caches"} for part in parts
    ) or path.name.startswith("lantai.log"):
        return "logs_cache"
    # Runtime ledgers rotate immutable segments. Never delete the active
    # events.jsonl file, which may be receiving a write at this moment.
    if (
        path.name.startswith("events.")
        and path.name.endswith(".jsonl")
        and ".lantai" in parts
    ):
        return "runtime"
    return None


def _cleanup_category(
    files: list[Path], *, days: int, max_bytes: int, dry_run: bool
) -> dict[str, int]:
    rows: list[tuple[Path, int, float]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append((path, stat.st_size, stat.st_mtime))
    before = sum(size for _, size, _ in rows)
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        if days
        else None
    )
    selected: set[Path] = {
        path for path, _, mtime in rows if cutoff is not None and mtime < cutoff
    }
    remaining = sum(size for path, size, _ in rows if path not in selected)
    if max_bytes:
        for path, size, _ in sorted(rows, key=lambda row: row[2]):
            if remaining <= max_bytes:
                break
            if path not in selected:
                selected.add(path)
                remaining -= size
    reclaimed = sum(size for path, size, _ in rows if path in selected)
    removed = 0
    if not dry_run:
        for path in selected:
            try:
                path.unlink()
                # Rolled runtime ledgers have a same-name sidecar index. It is
                # disposable only when its immutable ledger segment is gone.
                if path.name.startswith("events.") and path.name.endswith(".jsonl"):
                    try:
                        path.with_name(f"{path.name}.index.json").unlink()
                    except FileNotFoundError:
                        pass
                removed += 1
            except FileNotFoundError:
                pass
            except OSError:
                continue
    return {
        "bytes_before": before,
        "bytes_reclaimable": reclaimed,
        "files_reclaimable": len(selected),
        "files_removed": removed,
    }


def clean_storage_retention(
    *, dry_run: bool, root: Path = WORKING_DIR
) -> dict[str, Any]:
    policy = get_storage_retention_policy(root)
    buckets: dict[str, list[Path]] = {
        "logs_cache": [],
        "runtime": [],
        "generated_images": [],
    }
    for path in _regular_files(root):
        key = _category(path, root)
        if key:
            buckets[key].append(path)
    return {
        "policy": policy,
        "dry_run": dry_run,
        "logs_cache": _cleanup_category(
            buckets["logs_cache"],
            days=policy["logs_cache_days"],
            max_bytes=policy["logs_cache_max_bytes"],
            dry_run=dry_run,
        ),
        "runtime": _cleanup_category(
            buckets["runtime"],
            days=policy["runtime_days"],
            max_bytes=policy["runtime_max_bytes"],
            dry_run=dry_run,
        ),
        "generated_images": _cleanup_category(
            buckets["generated_images"],
            days=policy["generated_images_days"],
            max_bytes=0,
            dry_run=dry_run,
        ),
    }
