# -*- coding: utf-8 -*-
"""Desktop workspace access boundaries.

Desktop startup may inspect app-managed data eagerly. External workspaces are
only touched in response to an explicit user action so macOS privacy checks,
offline volumes, or stalled file providers cannot block process readiness.
"""

import os
from pathlib import Path

from ..constant import WORKING_DIR

WORKSPACE_ACCESS_REQUIRED_CODE = "workspace_access_required"


def should_defer_desktop_workspace_io(
    workspace_dir: str | Path,
    *,
    working_dir: str | Path = WORKING_DIR,
) -> bool:
    """Return whether automatic desktop I/O must be deferred for this path."""

    if os.environ.get("LANTAI_DESKTOP_APP", "").strip() != "1":
        return False

    # macOS commonly aliases /var to /private/var. Compare canonical paths so
    # an app-managed workspace staged through one spelling is not rejected as
    # external through the other. Canonicalising both sides also preserves the
    # guard when a child workspace symlink resolves outside the managed root.
    workspace = os.path.realpath(
        os.path.abspath(os.path.expanduser(os.fspath(workspace_dir))),
    )
    managed_root = os.path.realpath(
        os.path.abspath(os.path.expanduser(os.fspath(working_dir))),
    )
    try:
        return os.path.commonpath((workspace, managed_root)) != managed_root
    except ValueError:
        return True


def workspace_access_required_detail(
    agent_id: str,
    workspace_dir: str | Path,
) -> dict[str, str]:
    """Return a stable API detail for recoverable desktop workspace access."""

    return {
        "code": WORKSPACE_ACCESS_REQUIRED_CODE,
        "agent_id": agent_id,
        "workspace_dir": os.fspath(workspace_dir),
        "message": (
            "This agent uses a workspace outside app-managed storage. "
            "Restore it from the desktop app before using this route."
        ),
    }
