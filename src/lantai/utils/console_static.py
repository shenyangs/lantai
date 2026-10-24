# -*- coding: utf-8 -*-
"""Resolve the web console static assets directory (shared by app and CLI)."""
from __future__ import annotations

import os
from pathlib import Path

from ..constant import EnvVarLoader

# Primary env key (``COPAW_CONSOLE_STATIC_DIR`` is accepted as a legacy
# fallback via :class:`~lantai.constant.EnvVarLoader`).
CONSOLE_STATIC_ENV = "LANTAI_CONSOLE_STATIC_DIR"


def resolve_console_static_dir() -> str:
    """Return the directory expected to contain ``index.html`` for the console.

    Resolution order is: env override, source-checkout ``console/dist``,
    packaged ``lantai/console``, then cwd fallbacks.  A source checkout must
    prefer its latest frontend build over stale embedded release assets.
    """
    static_dir = EnvVarLoader.get_str(CONSOLE_STATIC_ENV)
    if static_dir:
        return static_dir

    pkg_dir = Path(__file__).resolve().parent.parent
    repo_dir = pkg_dir.parent.parent
    candidate = repo_dir / "console" / "dist"
    if candidate.is_dir() and (candidate / "index.html").is_file():
        return str(candidate)

    candidate = pkg_dir / "console"
    if candidate.is_dir() and (candidate / "index.html").is_file():
        return str(candidate)

    cwd = Path(os.getcwd())
    for subdir in ("console/dist", "console_dist"):
        candidate = cwd / subdir
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return str(candidate)

    return str(cwd / "console" / "dist")


def find_lantai_source_repo_root() -> Path | None:
    """Return the git checkout root if this Python
    is running from 澜台 source.

    Looks upward from :mod:`lantai` for ``console/package.json``,
    ``console/package-lock.json``, and ``src/lantai/``
    (bundled static target).
    Returns ``None`` for a normal pip/wheel install.
    """
    try:
        import lantai  # noqa: PLC0415 — avoid import cycle at module load
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    cur = Path(lantai.__file__).resolve().parent
    for _ in range(20):
        con = cur / "console"
        if (
            (con / "package.json").is_file()
            and (con / "package-lock.json").is_file()
            and (cur / "src" / "lantai").is_dir()
        ):
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None
