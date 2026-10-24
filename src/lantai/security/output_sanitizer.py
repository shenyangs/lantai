"""Public-safe projection of subprocess output.

Raw child-process output is retained by the runtime logs. Tool results shown to
the model and user must not disclose private installation paths, temporary
directories, ANSI terminal controls, or an implementation traceback.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable


_ANSI_RE = re.compile(
    r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)
_MAC_HOME_RE = re.compile(r"/Users/[^/\s\"']+")
_LINUX_HOME_RE = re.compile(r"/home/[^/\s\"']+")
_MAC_TEMP_RE = re.compile(r"/var/folders/[^\s\"']+")
_PYTHON_FRAME_RE = re.compile(r'^\s*File "[^"]+", line \d+(?:, in .*)?$')
_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:authorization|token|access_token|api[_-]?key|"
    r"secret|signature)"
    r"\s*(?:=|:)\s*(?:bearer\s+)?)"
    r"(?P<value>[^\s&;,\"']+)"
)


def strip_terminal_controls(text: str) -> str:
    """Remove ANSI escapes and non-printing terminal carriage returns."""

    return _ANSI_RE.sub("", str(text or "")).replace("\r", "")


def sanitize_public_output(
    text: str,
    *,
    private_roots: Iterable[str | Path] = (),
) -> str:
    """Redact known and conventional private paths from user-visible output."""

    projected = strip_terminal_controls(text)
    roots = sorted(
        {
            str(Path(root).expanduser().resolve())
            for root in private_roots
            if str(root or "").strip()
        },
        key=len,
        reverse=True,
    )
    for index, root in enumerate(roots, start=1):
        projected = projected.replace(root, f"<private-root-{index}>")
    projected = _MAC_TEMP_RE.sub("<temporary-path>", projected)
    projected = _MAC_HOME_RE.sub("~", projected)
    projected = _LINUX_HOME_RE.sub("~", projected)
    projected = _SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}<redacted>", projected
    )
    return projected


def summarize_subprocess_failure(
    text: str,
    *,
    private_roots: Iterable[str | Path] = (),
) -> str:
    """Collapse Python tracebacks to the actionable exception details."""

    projected = sanitize_public_output(
        text, private_roots=private_roots
    ).strip()
    if "Traceback (most recent call last):" not in projected:
        return projected

    lines = projected.splitlines()
    summary: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        if stripped == "Traceback (most recent call last):":
            collecting = True
            continue
        if not collecting:
            continue
        if _PYTHON_FRAME_RE.match(line) or stripped.startswith(
            ("raise ", "return ")
        ):
            continue
        if line.startswith((" ", "\t")) and not summary:
            continue
        if stripped:
            summary.append(line)

    if not summary:
        return "The Skill script failed inside its Python runtime."
    return "\n".join(summary[-12:]).strip()
