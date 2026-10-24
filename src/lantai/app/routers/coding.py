# -*- coding: utf-8 -*-
"""Coding workbench APIs.

The coding workbench is intentionally bound to Lantai's existing workspace
model.  The frontend may pass the active topic/manual workspace directory; if
it does not, these APIs fall back to the selected agent workspace.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fnmatch
import hashlib
import io
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..agent_context import get_agent_for_request
from ..coding_task_runtime import CodeTaskRun, CodingTaskRuntime
from ..concurrency import ConcurrencyAdmissionError, get_runtime_concurrency_governor
from ..workspace_grants import (
    WorkspaceGrantError,
    default_workspace_grant_registry,
    workspace_grant_http_exception,
)


router = APIRouter(prefix="/coding", tags=["coding"])

MAX_FILE_READ_BYTES = 5 * 1024 * 1024
MAX_FILE_WRITE_BYTES = 5 * 1024 * 1024
MAX_TREE_ENTRIES = 5000
MAX_WATCH_ENTRIES = 5000
MAX_DIFF_CHARS = 1_000_000
MAX_IMPORT_ZIP_BYTES = 200 * 1024 * 1024
MAX_IMPORT_ZIP_ENTRIES = 20_000
DEFAULT_WATCH_INTERVAL_SECONDS = 1.5
APP_STORE_DISTRIBUTIONS = {"app_store", "app-store", "appstore", "mas", "testflight"}
RESET_HARD_CONFIRMATION = "reset hard"
CLEAN_CONFIRMATION = "clean untracked"
DISCARD_CONFIRMATION = "discard changes"
CLONE_CONFIRMATION = "clone project"
IMPORT_ZIP_CONFIRMATION = "import zip"
MAX_CODING_OPEN_TABS = 50
MAX_CODING_DRAFTS = 20
MAX_CODING_DRAFT_BYTES = 256 * 1024
MAX_CODING_STATE_BYTES = 750 * 1024
MAX_TASK_TAIL_CHARS = 24_000
MAX_TASK_SECONDS = 20 * 60
MAX_CONCURRENT_CODING_TASKS = 1
TASK_READ_CHUNK_BYTES = 8192

DANGEROUS_TASK_PATTERNS = (
    r"\bgit\s+reset\b",
    r"\bgit\s+clean\b",
    r"\brm\s+-[^\n;]*r[^\n;]*f\b",
    r"\bsudo\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r":\s*\(\s*\)\s*\{",
    r"\bcurl\b[^\n|;]*\|\s*(?:sh|bash|zsh)\b",
    r"\bwget\b[^\n|;]*\|\s*(?:sh|bash|zsh)\b",
)

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "release",
    "electron-dist",
    "coverage",
    ".lantai",
    ".wiki",
    "backup",
    "browser",
    "capabilities",
    "dialog",
    "embedding_cache",
    "file_store",
    "media",
    "memory",
    "sessions",
}

SKIP_FILES = {
    ".DS_Store",
    "Thumbs.db",
}

SENSITIVE_FILE_PATTERNS = {
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
}


class CodeFileNode(BaseModel):
    name: str
    path: str
    kind: str = Field(description="'file' or 'directory'")
    size: int = 0
    modified_time: str | None = None
    children: list["CodeFileNode"] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None


class CodeFileTreeResponse(BaseModel):
    root_path: str
    root_name: str
    path: str = ""
    children: list[CodeFileNode] = Field(default_factory=list)
    total_entries: int
    truncated: bool = False
    max_entries: int = MAX_TREE_ENTRIES
    depth: int = 1
    limit: int = 300
    cursor: str | None = None
    next_cursor: str | None = None
    has_more: bool = False


class CodeFileContentResponse(BaseModel):
    path: str
    content: str
    size: int
    modified_time: str
    sha256: str
    encoding: str
    binary: bool = False
    truncated: bool = False


class CodeFileSaveRequest(BaseModel):
    content: str
    expected_sha256: str | None = None


class CodeGitPathRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)


class CodeGitCommitRequest(BaseModel):
    message: str = Field(min_length=1, max_length=300)
    confirmed: bool = False


class CodeGitInitRequest(BaseModel):
    confirmed: bool = False
    initial_commit: bool = False
    message: str = Field(default="Initial commit", max_length=300)


class CodeGitDiscardRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)
    confirmed: bool = False
    confirmation_phrase: str = ""


class CodeGitCleanRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=100)
    confirmed: bool = False
    confirmation_phrase: str = ""


class CodeGitResetRequest(BaseModel):
    target: str = Field(default="HEAD", max_length=120)
    confirmed: bool = False
    confirmation_phrase: str = ""


class CodeGitRevertRequest(BaseModel):
    commit: str = Field(min_length=7, max_length=40)
    confirmed: bool = False


class CodeGitCheckoutRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CodeGitCreateBranchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    checkout: bool = True


class CodeProjectCloneRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    target_dir: str = Field(min_length=1, max_length=120)
    confirmed: bool = False
    confirmation_phrase: str = ""


class CodeProjectCreateEmptyRequest(BaseModel):
    target_dir: str = Field(min_length=1, max_length=120)


class CodeProjectActivateRequest(BaseModel):
    workspace_dir: str = Field(min_length=1, max_length=4096)
    chat_id: str | None = Field(default=None, max_length=120)


class CodeGitStatusEntry(BaseModel):
    status: str
    path: str
    original_path: str | None = None
    index_status: str = ""
    worktree_status: str = ""
    staged: bool = False
    unstaged: bool = False
    untracked: bool = False


class CodeGitStatusResponse(BaseModel):
    is_repo: bool
    branch: str | None = None
    entries: list[CodeGitStatusEntry] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class CodeGitDiffResponse(BaseModel):
    is_repo: bool
    path: str | None = None
    unstaged: str = ""
    staged: str = ""
    truncated: bool = False
    error: str | None = None


class CodeGitLogEntry(BaseModel):
    hash: str
    short_hash: str
    author: str
    date: str
    subject: str


class CodeGitLogResponse(BaseModel):
    is_repo: bool
    entries: list[CodeGitLogEntry] = Field(default_factory=list)
    error: str | None = None


class CodeGitBranchEntry(BaseModel):
    name: str
    current: bool = False
    upstream: str | None = None


class CodeGitBranchesResponse(BaseModel):
    is_repo: bool
    branches: list[CodeGitBranchEntry] = Field(default_factory=list)
    current: str | None = None
    error: str | None = None


class CodeGitCommitDiffResponse(BaseModel):
    is_repo: bool
    commit: str | None = None
    diff: str = ""
    stat: str = ""
    truncated: bool = False
    error: str | None = None


class CodeGitMutationResponse(BaseModel):
    is_repo: bool
    ok: bool
    paths: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


class CodeProjectSummary(BaseModel):
    workspace_dir: str
    root_name: str
    exists: bool
    is_git: bool
    status: str = "ok"
    chat_id: str | None = None
    updated_at: str | None = None
    workspace_grant_id: str | None = None


class CodeProjectListResponse(BaseModel):
    projects: list[CodeProjectSummary] = Field(default_factory=list)


class CodeCodingState(BaseModel):
    chat_id: str
    session_id: str | None = None
    workspace_dir: str | None = None
    active_file: str | None = None
    open_tabs: list[str] = Field(default_factory=list)
    dirty_drafts: dict[str, str] = Field(default_factory=dict)
    pending_diffs: list[str] = Field(default_factory=list)
    git_summary: dict[str, int] = Field(default_factory=dict)
    last_run: str | None = None
    next_recovery_step: str | None = None
    updated_at: str | None = None


class CodeTaskStartRequest(BaseModel):
    command: str = Field(min_length=1, max_length=2000)
    confirmed: bool = False
    timeout_seconds: int = Field(default=300, ge=1, le=MAX_TASK_SECONDS)


class CodeTaskListResponse(BaseModel):
    tasks: list[CodeTaskRun] = Field(default_factory=list)


class CodeCodingCapabilitiesResponse(BaseModel):
    distribution: str
    file_tree: bool = True
    file_read: bool = True
    file_write: bool = True
    file_watch: bool = True
    git_read: bool
    git_write: bool
    git_dangerous_write: bool
    project_import: bool
    ast_tools: bool
    lsp_tools: bool
    external_process_tools: bool
    background_tasks: bool
    reasons: dict[str, str] = Field(default_factory=dict)


class CodeWorkbenchHealth(BaseModel):
    status: str
    message: str


class CodeWorkbenchStateResponse(BaseModel):
    workspace_dir: str
    workspace_name: str
    health: CodeWorkbenchHealth
    capabilities: CodeCodingCapabilitiesResponse
    file_tree: CodeFileTreeResponse | None = None
    git_status: CodeGitStatusResponse | None = None
    recent_projects: list[CodeProjectSummary] = Field(default_factory=list)
    tasks: list[CodeTaskRun] = Field(default_factory=list)
    coding_state: CodeCodingState | None = None
    watch_interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS


_TASKS: dict[str, CodeTaskRun] = {}
_TASK_PROCESSES: dict[str, asyncio.subprocess.Process] = {}
_TASK_RUNTIMES: dict[str, CodingTaskRuntime] = {}
_TASK_RESERVATIONS: set[str] = set()
_HYDRATED_TASK_WORKSPACES: set[str] = set()
_TASK_HYDRATION_LOCK = asyncio.Lock()


class _TreeState:
    def __init__(self, max_entries: int) -> None:
        self.max_entries = max_entries
        self.total_entries = 0
        self.truncated = False

    def take(self) -> bool:
        if self.total_entries >= self.max_entries:
            self.truncated = True
            return False
        self.total_entries += 1
        return True


async def _workspace_root(
    request: Request,
    workspace_dir: str | None = None,
) -> Path:
    workspace = await get_agent_for_request(request)
    agent_root = Path(workspace.workspace_dir).expanduser().resolve(
        strict=False,
    )
    grant_id = str(
        request.headers.get("X-Workspace-Grant-Id")
        or request.headers.get("x-workspace-grant-id")
        or "",
    ).strip()
    if grant_id:
        try:
            binding = default_workspace_grant_registry.resolve(
                grant_id,
                agent_id=str(getattr(workspace, "agent_id", "default")),
                operation="write",
            )
        except WorkspaceGrantError as error:
            raise workspace_grant_http_exception(error) from error
        if workspace_dir and workspace_dir.strip():
            try:
                requested = Path(workspace_dir).expanduser().resolve(
                    strict=False,
                )
            except OSError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unable to resolve workspace: {exc}",
                ) from exc
            if requested != binding.project_root:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "workspace_grant_path_mismatch",
                        "message": (
                            "The requested path does not match the active "
                            "workspace authorization."
                        ),
                    },
                )
        return binding.project_root

    if workspace_dir and workspace_dir.strip():
        root = Path(workspace_dir).expanduser()
    else:
        root = agent_root

    try:
        resolved = root.resolve()
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unable to resolve workspace: {exc}",
        ) from exc

    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Workspace does not exist: {resolved}",
        )
    if resolved != agent_root and agent_root not in resolved.parents:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "workspace_grant_required",
                "message": (
                    "An external Coding workspace must be selected and "
                    "authorized first."
                ),
            },
        )
    return resolved


async def _chat_manager_for_request(request: Request) -> Any:
    workspace = await get_agent_for_request(request)
    return workspace.chat_manager


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative_posix(path: Path, root: Path) -> str:
    if path == root:
        return ""
    return path.relative_to(root).as_posix()


def _safe_join(root: Path, relative_path: str) -> Path:
    raw = (relative_path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Missing file path")
    if Path(raw).is_absolute():
        raise HTTPException(status_code=400, detail="Absolute paths are not allowed")

    try:
        target = (root / raw).resolve(strict=False)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid path: {exc}") from exc

    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Path escapes workspace") from exc

    return target


def _is_sensitive_file(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_FILE_PATTERNS)


def _should_skip(path: Path) -> bool:
    name = path.name
    if path.is_symlink():
        return True
    if path.is_dir():
        return name in SKIP_DIRS
    return name in SKIP_FILES or _is_sensitive_file(name)


def _ensure_visible_file_target(root: Path, target: Path) -> None:
    if target.is_symlink():
        raise HTTPException(status_code=403, detail="Symlink files are not readable")
    if _should_skip(target) or _is_under_skipped_dir(target, root):
        raise HTTPException(status_code=403, detail="File is hidden from Coding")


def _is_visible_relative_path(root: Path, relative_path: str) -> bool:
    try:
        target = _safe_join(root, relative_path)
        _ensure_visible_file_target(root, target)
    except HTTPException:
        return False
    return True


def _visible_state_paths(root: Path, paths: list[str]) -> list[str]:
    visible: list[str] = []
    seen: set[str] = set()
    for raw_path in paths[:MAX_CODING_OPEN_TABS]:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        try:
            target = _safe_join(root, raw_path)
            _ensure_visible_file_target(root, target)
        except HTTPException:
            continue
        rel = _relative_posix(target, root)
        if rel and rel not in seen:
            visible.append(rel)
            seen.add(rel)
    return visible


def _sanitize_coding_state(root: Path, state: CodeCodingState) -> CodeCodingState:
    open_tabs = _visible_state_paths(root, state.open_tabs)
    active_file = None
    if state.active_file:
        active = _visible_state_paths(root, [state.active_file])
        active_file = active[0] if active else None
    if active_file and active_file not in open_tabs:
        open_tabs = [active_file, *open_tabs]
    open_tabs = open_tabs[:MAX_CODING_OPEN_TABS]

    dirty_drafts: dict[str, str] = {}
    total_bytes = 0
    for path, content in state.dirty_drafts.items():
        if len(dirty_drafts) >= MAX_CODING_DRAFTS:
            break
        if path not in open_tabs:
            continue
        if not isinstance(content, str):
            continue
        encoded = content.encode("utf-8", errors="ignore")
        if len(encoded) > MAX_CODING_DRAFT_BYTES:
            continue
        if total_bytes + len(encoded) > MAX_CODING_STATE_BYTES:
            break
        dirty_drafts[path] = content
        total_bytes += len(encoded)

    pending_diffs = _visible_state_paths(root, state.pending_diffs)
    git_summary = {
        str(key): int(value)
        for key, value in (state.git_summary or {}).items()
        if isinstance(value, int | float)
    }
    return CodeCodingState(
        chat_id=state.chat_id,
        session_id=state.session_id,
        workspace_dir=str(root),
        active_file=active_file,
        open_tabs=open_tabs,
        dirty_drafts=dirty_drafts,
        pending_diffs=pending_diffs,
        git_summary=git_summary,
        last_run=(state.last_run or "")[:400] or None,
        next_recovery_step=(state.next_recovery_step or "")[:1000] or None,
        updated_at=_now_iso(),
    )


def _state_from_chat(chat: Any) -> CodeCodingState:
    meta = dict(getattr(chat, "meta", None) or {})
    raw_state = meta.get("coding_state")
    if isinstance(raw_state, dict):
        try:
            state = CodeCodingState.model_validate(raw_state)
        except Exception:
            state = CodeCodingState(chat_id=chat.id)
    else:
        state = CodeCodingState(chat_id=chat.id)
    workspace_dir = (
        state.workspace_dir
        or meta.get("coding_workspace_dir")
        or meta.get("manual_workspace_dir")
        or meta.get("workspace_dir")
    )
    return state.model_copy(
        update={
            "chat_id": chat.id,
            "session_id": getattr(chat, "session_id", None),
            "workspace_dir": workspace_dir if isinstance(workspace_dir, str) else None,
        }
    )


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _visible_children(path: Path) -> list[Path]:
    try:
        entries = sorted(
            path.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
    except OSError:
        return []
    return [item for item in entries if not _should_skip(item)]


def _directory_has_visible_children(path: Path, root: Path) -> bool:
    try:
        for child in path.iterdir():
            if _should_skip(child) or _is_under_skipped_dir(child, root):
                continue
            return True
    except OSError:
        return False
    return False


def _build_tree_node(
    path: Path,
    root: Path,
    *,
    depth: int,
    max_depth: int,
    state: _TreeState,
) -> CodeFileNode | None:
    if _should_skip(path) or not state.take():
        return None

    try:
        stat = path.stat()
    except OSError:
        return None

    if path.is_dir():
        children: list[CodeFileNode] = []
        has_more = False
        if depth < max_depth:
            for child in _visible_children(path):
                node = _build_tree_node(
                    child,
                    root,
                    depth=depth + 1,
                    max_depth=max_depth,
                    state=state,
                )
                if node is not None:
                    children.append(node)
                if state.truncated:
                    break
        else:
            has_more = _directory_has_visible_children(path, root)
        return CodeFileNode(
            name=path.name,
            path=_relative_posix(path, root),
            kind="directory",
            size=0,
            modified_time=_mtime_iso(path),
            children=children,
            has_more=has_more,
        )

    return CodeFileNode(
        name=path.name,
        path=_relative_posix(path, root),
        kind="file",
        size=stat.st_size,
        modified_time=_mtime_iso(path),
    )


def _parse_tree_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid tree cursor")
    if value < 0:
        raise HTTPException(status_code=400, detail="Invalid tree cursor")
    return value


def _build_tree(
    root: Path,
    *,
    max_entries: int,
    max_depth: int,
) -> CodeFileTreeResponse:
    state = _TreeState(max_entries=max_entries)
    children: list[CodeFileNode] = []
    try:
        entries = sorted(
            root.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unable to list workspace: {exc}",
        ) from exc

    for child in entries:
        node = _build_tree_node(
            child,
            root,
            depth=1,
            max_depth=max_depth,
            state=state,
        )
        if node is not None:
            children.append(node)
        if state.truncated:
            break

    return CodeFileTreeResponse(
        root_path=str(root),
        root_name=root.name or str(root),
        children=children,
        total_entries=state.total_entries,
        truncated=state.truncated,
        max_entries=max_entries,
        depth=max_depth,
        limit=max_entries,
    )


def _build_tree_slice(
    root: Path,
    *,
    relative_path: str = "",
    max_entries: int,
    max_depth: int,
    cursor: str | None,
    limit: int,
) -> CodeFileTreeResponse:
    rel = (relative_path or "").strip().strip("/")
    target = root if not rel else _safe_join(root, rel)
    if target != root:
        if target.is_symlink() or _should_skip(target) or _is_under_skipped_dir(target, root):
            raise HTTPException(status_code=403, detail="Directory is hidden from Coding")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {rel or '.'}")

    offset = _parse_tree_cursor(cursor)
    entries = _visible_children(target)
    page = entries[offset : offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < len(entries)
    state = _TreeState(max_entries=max_entries)
    children: list[CodeFileNode] = []
    for child in page:
        if _is_under_skipped_dir(child, root):
            continue
        node = _build_tree_node(
            child,
            root,
            depth=1,
            max_depth=max_depth,
            state=state,
        )
        if node is not None:
            children.append(node)
        if state.truncated:
            break

    next_cursor = str(next_offset) if has_more and not state.truncated else None
    return CodeFileTreeResponse(
        root_path=str(root),
        root_name=root.name or str(root),
        path=_relative_posix(target.resolve(strict=False), root),
        children=children,
        total_entries=state.total_entries,
        truncated=state.truncated,
        max_entries=max_entries,
        depth=max_depth,
        limit=limit,
        cursor=str(offset) if cursor else None,
        next_cursor=next_cursor,
        has_more=bool(next_cursor),
    )


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:4096]
    return b"\0" in sample


def _read_code_file(root: Path, relative_path: str) -> CodeFileContentResponse:
    target = _safe_join(root, relative_path)
    _ensure_visible_file_target(root, target)
    if not target.exists() or not target.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"File not found: {relative_path}",
        )

    stat = target.stat()
    if stat.st_size > MAX_FILE_READ_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File is too large for inline preview "
                f"({stat.st_size} bytes > {MAX_FILE_READ_BYTES} bytes)"
            ),
        )

    data = target.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    binary = _looks_binary(data)
    if binary:
        content = ""
        encoding = "binary"
    else:
        encoding = "utf-8"
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            encoding = "utf-8-replace"
            content = data.decode("utf-8", errors="replace")

    return CodeFileContentResponse(
        path=_relative_posix(target.resolve(strict=False), root),
        content=content,
        size=stat.st_size,
        modified_time=_mtime_iso(target),
        sha256=digest,
        encoding=encoding,
        binary=binary,
    )


def _downloadable_code_file(root: Path, relative_path: str) -> Path:
    target = _safe_join(root, relative_path)
    _ensure_visible_file_target(root, target)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {relative_path}")
    return target


def _snapshot(root: Path, max_entries: int = MAX_WATCH_ENTRIES) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    count = 0
    for path in root.rglob("*"):
        if count >= max_entries:
            break
        if _should_skip(path):
            if path.is_dir():
                # rglob cannot be pruned after yielding a directory, so skipped
                # nested entries are filtered by _is_under_skipped_dir below.
                continue
            continue
        if _is_under_skipped_dir(path, root):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = _relative_posix(path, root)
        snapshot[rel] = f"{stat.st_mtime_ns}:{stat.st_size}:{int(path.is_dir())}"
        count += 1
    return snapshot


def _is_under_skipped_dir(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    return any(part in SKIP_DIRS for part in rel.parts[:-1])


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    paths = set(before) | set(after)
    return sorted(path for path in paths if before.get(path) != after.get(path))[:200]


def _sse(event: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


def _run_git(root: Path, args: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_git_no_repo(
    cwd: Path,
    args: list[str],
    *,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _git_repo_error(root: Path) -> str | None:
    try:
        result = _run_git(root, ["rev-parse", "--is-inside-work-tree"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if result.returncode != 0 or result.stdout.strip() != "true":
        return result.stderr.strip() or "Not a git repository"
    return None


def _parse_git_status(output: str) -> tuple[str | None, list[CodeGitStatusEntry]]:
    branch: str | None = None
    entries: list[CodeGitStatusEntry] = []
    for line in output.splitlines():
        if not line:
            continue
        if line.startswith("## "):
            branch = line[3:].strip()
            continue
        status = line[:2].strip() or "?"
        index_status = line[0] if len(line) > 0 else ""
        worktree_status = line[1] if len(line) > 1 else ""
        path_text = line[3:].strip()
        original_path = None
        if " -> " in path_text:
            original_path, path_text = path_text.split(" -> ", 1)
        untracked = index_status == "?" and worktree_status == "?"
        entries.append(
            CodeGitStatusEntry(
                status=status,
                path=path_text,
                original_path=original_path,
                index_status="" if index_status == " " else index_status,
                worktree_status="" if worktree_status == " " else worktree_status,
                staged=index_status not in {"", " ", "?"},
                unstaged=worktree_status not in {"", " ", "?"},
                untracked=untracked,
            )
        )
    return branch, entries


def _status_summary(entries: list[CodeGitStatusEntry]) -> dict[str, int]:
    summary = {"modified": 0, "added": 0, "deleted": 0, "renamed": 0, "untracked": 0}
    for entry in entries:
        status = entry.status
        if "?" in status:
            summary["untracked"] += 1
        elif "R" in status:
            summary["renamed"] += 1
        elif "D" in status:
            summary["deleted"] += 1
        elif "A" in status:
            summary["added"] += 1
        else:
            summary["modified"] += 1
    return summary


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_DIFF_CHARS:
        return text, False
    return text[:MAX_DIFF_CHARS], True


def _visible_git_paths(root: Path, paths: list[str]) -> list[str]:
    visible_paths: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        target = _safe_join(root, raw_path)
        _ensure_visible_file_target(root, target)
        rel = _relative_posix(target, root)
        if not rel:
            raise HTTPException(status_code=400, detail="Git path must target a file")
        if rel not in seen:
            visible_paths.append(rel)
            seen.add(rel)
    if not visible_paths:
        raise HTTPException(status_code=400, detail="No Git paths provided")
    return visible_paths


def _all_visible_files_for_git(root: Path, *, max_paths: int = 20_000) -> list[str]:
    paths: list[str] = []
    for path in root.rglob("*"):
        if len(paths) >= max_paths:
            break
        if path.is_dir() or _should_skip(path) or _is_under_skipped_dir(path, root):
            continue
        try:
            _ensure_visible_file_target(root, path)
        except HTTPException:
            continue
        paths.append(_relative_posix(path, root))
    return paths


def _require_confirmation(
    *,
    confirmed: bool,
    phrase: str | None = None,
    expected_phrase: str | None = None,
    action: str,
) -> None:
    if not confirmed:
        raise HTTPException(status_code=400, detail=f"{action} requires confirmation")
    if expected_phrase is not None and phrase.strip().lower() != expected_phrase:
        raise HTTPException(
            status_code=400,
            detail=f'Type "{expected_phrase}" to confirm {action}',
        )


def _git_mutation_response(
    result: subprocess.CompletedProcess[str],
    *,
    paths: list[str] | None = None,
) -> CodeGitMutationResponse:
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    return CodeGitMutationResponse(
        is_repo=True,
        ok=result.returncode == 0,
        paths=paths or [],
        stdout=stdout,
        stderr=stderr,
        error=None if result.returncode == 0 else stderr or stdout or "Git command failed",
    )


def _git_stage_paths(root: Path, paths: list[str]) -> CodeGitMutationResponse:
    visible_paths = _visible_git_paths(root, paths)
    result = _run_git(root, ["add", "--", *visible_paths], timeout=10.0)
    return _git_mutation_response(result, paths=visible_paths)


def _git_unstage_paths(root: Path, paths: list[str]) -> CodeGitMutationResponse:
    visible_paths = _visible_git_paths(root, paths)
    result = _run_git(root, ["restore", "--staged", "--", *visible_paths], timeout=10.0)
    return _git_mutation_response(result, paths=visible_paths)


def _git_has_staged_changes(root: Path) -> bool:
    result = _run_git(root, ["diff", "--cached", "--quiet"], timeout=5.0)
    return result.returncode == 1


def _git_commit(root: Path, request: CodeGitCommitRequest) -> CodeGitMutationResponse:
    message = " ".join(request.message.split()).strip()
    if not request.confirmed:
        raise HTTPException(status_code=400, detail="Commit requires confirmation")
    if not message:
        raise HTTPException(status_code=400, detail="Commit message is required")
    if not _git_has_staged_changes(root):
        return CodeGitMutationResponse(
            is_repo=True,
            ok=False,
            error="No staged changes to commit",
        )
    result = _run_git(root, ["commit", "-m", message], timeout=30.0)
    return _git_mutation_response(result)


def _git_init(root: Path, request: CodeGitInitRequest) -> CodeGitMutationResponse:
    _require_confirmation(confirmed=request.confirmed, action="git init")
    if _git_repo_error(root) is None:
        return CodeGitMutationResponse(
            is_repo=True,
            ok=True,
            stdout="Workspace is already a Git repository",
        )

    init_result = _run_git_no_repo(root, ["init"], timeout=30.0)
    if init_result.returncode != 0:
        return CodeGitMutationResponse(
            is_repo=False,
            ok=False,
            stdout=init_result.stdout.strip(),
            stderr=init_result.stderr.strip(),
            error=init_result.stderr.strip() or "git init failed",
        )

    if not request.initial_commit:
        return _git_mutation_response(init_result)

    files = _all_visible_files_for_git(root)
    if files:
        add_result = _run_git(root, ["add", "--", *files], timeout=60.0)
        if add_result.returncode != 0:
            return _git_mutation_response(add_result, paths=files)
    if not _git_has_staged_changes(root):
        return CodeGitMutationResponse(
            is_repo=True,
            ok=True,
            stdout=(init_result.stdout.strip() or "Initialized empty Git repository"),
            error="Repository initialized; no visible files to commit",
        )
    commit_request = CodeGitCommitRequest(
        message=request.message or "Initial commit",
        confirmed=True,
    )
    committed = _git_commit(root, commit_request)
    committed.stdout = "\n".join(
        part for part in [init_result.stdout.strip(), committed.stdout] if part
    )
    return committed


def _git_discard(root: Path, request: CodeGitDiscardRequest) -> CodeGitMutationResponse:
    _require_confirmation(
        confirmed=request.confirmed,
        phrase=request.confirmation_phrase,
        expected_phrase=DISCARD_CONFIRMATION,
        action="discard changes",
    )
    visible_paths = _visible_git_paths(root, request.paths)
    _branch, entries = _parse_git_status(
        _run_git(
            root,
            ["status", "--short", "--branch", "--untracked-files=normal"],
            timeout=5.0,
        ).stdout,
    )
    untracked = {entry.path for entry in entries if entry.untracked}
    tracked_paths = [path for path in visible_paths if path not in untracked]
    untracked_paths = [path for path in visible_paths if path in untracked]

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    ok = True
    if tracked_paths:
        restore_result = _run_git(
            root,
            ["restore", "--staged", "--worktree", "--", *tracked_paths],
            timeout=20.0,
        )
        ok = ok and restore_result.returncode == 0
        stdout_parts.append(restore_result.stdout.strip())
        stderr_parts.append(restore_result.stderr.strip())
    if untracked_paths:
        clean_result = _run_git(
            root,
            ["clean", "-f", "--", *untracked_paths],
            timeout=20.0,
        )
        ok = ok and clean_result.returncode == 0
        stdout_parts.append(clean_result.stdout.strip())
        stderr_parts.append(clean_result.stderr.strip())

    stdout = "\n".join(part for part in stdout_parts if part)
    stderr = "\n".join(part for part in stderr_parts if part)
    return CodeGitMutationResponse(
        is_repo=True,
        ok=ok,
        paths=visible_paths,
        stdout=stdout,
        stderr=stderr,
        error=None if ok else stderr or stdout or "Discard failed",
    )


def _git_clean(root: Path, request: CodeGitCleanRequest) -> CodeGitMutationResponse:
    _require_confirmation(
        confirmed=request.confirmed,
        phrase=request.confirmation_phrase,
        expected_phrase=CLEAN_CONFIRMATION,
        action="git clean",
    )
    visible_paths = _visible_git_paths(root, request.paths) if request.paths else []
    args = ["clean", "-fd"]
    if visible_paths:
        args.extend(["--", *visible_paths])
    result = _run_git(root, args, timeout=30.0)
    return _git_mutation_response(result, paths=visible_paths)


def _git_reset_hard(root: Path, request: CodeGitResetRequest) -> CodeGitMutationResponse:
    _require_confirmation(
        confirmed=request.confirmed,
        phrase=request.confirmation_phrase,
        expected_phrase=RESET_HARD_CONFIRMATION,
        action="git reset --hard",
    )
    target = request.target.strip() or "HEAD"
    if not re.fullmatch(r"[A-Za-z0-9_./@{}~^:-]+", target):
        raise HTTPException(status_code=400, detail="Invalid reset target")
    result = _run_git(root, ["reset", "--hard", target], timeout=30.0)
    return _git_mutation_response(result)


def _git_revert(root: Path, request: CodeGitRevertRequest) -> CodeGitMutationResponse:
    _require_confirmation(confirmed=request.confirmed, action="git revert")
    commit = request.commit.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
        raise HTTPException(status_code=400, detail="Invalid commit hash")
    result = _run_git(root, ["revert", "--no-edit", commit], timeout=60.0)
    return _git_mutation_response(result)


def _validate_branch_name(name: str) -> str:
    branch = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", branch):
        raise HTTPException(status_code=400, detail="Invalid branch name")
    if (
        ".." in branch
        or "//" in branch
        or branch.endswith("/")
        or branch.endswith(".lock")
        or "@{" in branch
        or branch.startswith("-")
    ):
        raise HTTPException(status_code=400, detail="Invalid branch name")
    return branch


def _git_branches(root: Path) -> CodeGitBranchesResponse:
    result = _run_git(
        root,
        ["branch", "--format=%(refname:short)|%(HEAD)|%(upstream:short)"],
        timeout=10.0,
    )
    if result.returncode != 0:
        return CodeGitBranchesResponse(
            is_repo=True,
            error=result.stderr.strip() or "Unable to list branches",
        )

    branches: list[CodeGitBranchEntry] = []
    current: str | None = None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 2)
        name = parts[0].strip()
        if not name:
            continue
        is_current = len(parts) > 1 and parts[1].strip() == "*"
        upstream = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        if is_current:
            current = name
        branches.append(
            CodeGitBranchEntry(name=name, current=is_current, upstream=upstream)
        )
    return CodeGitBranchesResponse(is_repo=True, branches=branches, current=current)


def _git_checkout(root: Path, request: CodeGitCheckoutRequest) -> CodeGitMutationResponse:
    branch = _validate_branch_name(request.name)
    result = _run_git(root, ["checkout", branch], timeout=30.0)
    return _git_mutation_response(result)


def _git_create_branch(
    root: Path,
    request: CodeGitCreateBranchRequest,
) -> CodeGitMutationResponse:
    branch = _validate_branch_name(request.name)
    args = ["checkout", "-b", branch] if request.checkout else ["branch", branch]
    result = _run_git(root, args, timeout=30.0)
    return _git_mutation_response(result)


def _git_commit_diff(root: Path, commit: str) -> CodeGitCommitDiffResponse:
    ref = commit.strip()
    if not re.fullmatch(r"[A-Za-z0-9_./@{}~^:-]{1,120}", ref):
        raise HTTPException(status_code=400, detail="Invalid commit reference")
    stat_result = _run_git(
        root,
        ["show", "--stat", "--format=short", "--max-count=1", ref],
        timeout=15.0,
    )
    diff_result = _run_git(
        root,
        ["show", "--patch", "--find-renames", "--format=", "--max-count=1", ref],
        timeout=20.0,
    )
    if stat_result.returncode != 0 or diff_result.returncode != 0:
        return CodeGitCommitDiffResponse(
            is_repo=True,
            commit=ref,
            error=(
                stat_result.stderr.strip()
                or diff_result.stderr.strip()
                or "Unable to read commit diff"
            ),
        )
    stat, stat_truncated = _truncate(stat_result.stdout)
    diff, diff_truncated = _truncate(diff_result.stdout)
    return CodeGitCommitDiffResponse(
        is_repo=True,
        commit=ref,
        stat=stat,
        diff=diff,
        truncated=stat_truncated or diff_truncated,
    )


def _project_summary(
    path: Path,
    *,
    chat_id: str | None = None,
    updated_at: str | None = None,
) -> CodeProjectSummary:
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        return CodeProjectSummary(
            workspace_dir=str(path),
            root_name=path.name or str(path),
            exists=False,
            is_git=False,
            status="invalid",
            chat_id=chat_id,
            updated_at=updated_at,
        )
    exists = resolved.exists() and resolved.is_dir()
    is_git = False
    status = "ok" if exists else "missing"
    if exists and shutil.which("git") is not None:
        is_git = _git_repo_error(resolved) is None
    return CodeProjectSummary(
        workspace_dir=str(resolved),
        root_name=resolved.name or str(resolved),
        exists=exists,
        is_git=is_git,
        status=status,
        chat_id=chat_id,
        updated_at=updated_at,
    )


def _guard_task_command(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        raise HTTPException(status_code=400, detail="Command is required")
    for pattern in DANGEROUS_TASK_PATTERNS:
        if re.search(pattern, stripped, flags=re.IGNORECASE):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Command is blocked by Coding Tool Guard. Use the explicit "
                    "Git guarded actions for destructive operations."
                ),
            )
    return stripped


def _tail_text(text: str) -> str:
    if len(text) <= MAX_TASK_TAIL_CHARS:
        return text
    return text[-MAX_TASK_TAIL_CHARS:]


def _reserve_task_slot(task_id: str) -> bool:
    """Reserve one bounded execution slot without becoming a run-state source."""

    active_ids = set(_TASK_RESERVATIONS)
    active_ids.update(
        running_id
        for running_id, process in _TASK_PROCESSES.items()
        if process.returncode is None
    )
    if task_id in active_ids:
        return True
    if len(active_ids) >= MAX_CONCURRENT_CODING_TASKS:
        return False
    _TASK_RESERVATIONS.add(task_id)
    return True


def _release_task_slot(task_id: str) -> None:
    _TASK_RESERVATIONS.discard(task_id)


async def _hydrate_coding_tasks(root: Path, *, agent_id: str) -> None:
    """Load durable task projections and reconcile a previous backend exit."""

    workspace_key = str(root)
    if workspace_key in _HYDRATED_TASK_WORKSPACES:
        return
    async with _TASK_HYDRATION_LOCK:
        if workspace_key in _HYDRATED_TASK_WORKSPACES:
            return
        runtime = CodingTaskRuntime(root, agent_id=agent_id)
        tasks = await runtime.recover_interrupted()
        for task in tasks:
            _TASKS[task.task_id] = task
            _TASK_RUNTIMES[task.task_id] = runtime
        _HYDRATED_TASK_WORKSPACES.add(workspace_key)


async def _runtime_for_task(
    task: CodeTaskRun,
    root: Path,
    *,
    agent_id: str = "default",
) -> CodingTaskRuntime:
    runtime = _TASK_RUNTIMES.get(task.task_id)
    if runtime is not None:
        return runtime
    runtime = CodingTaskRuntime(root, agent_id=agent_id)
    if await runtime.store.load_contract(task.task_id) is None:
        task = await runtime.create(task)
        _TASKS[task.task_id] = task
    _TASK_RUNTIMES[task.task_id] = runtime
    return runtime


def _append_task_output(task_id: str, field: str, text: str) -> None:
    if not text:
        return
    task = _TASKS.get(task_id)
    if task is None:
        return
    current = str(getattr(task, field, "") or "")
    _TASKS[task_id] = task.model_copy(
        update={
            field: _tail_text(current + text),
            "output_seq": task.output_seq + 1,
            "updated_at": _now_iso(),
        }
    )
    runtime = _TASK_RUNTIMES.get(task_id)
    if runtime is not None:
        runtime.save(_TASKS[task_id])


async def _stream_task_pipe(
    task_id: str,
    stream: asyncio.StreamReader | None,
    field: str,
) -> None:
    """Continuously expose bounded subprocess output while it is running."""
    if stream is None:
        return
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while chunk := await stream.read(TASK_READ_CHUNK_BYTES):
        _append_task_output(task_id, field, decoder.decode(chunk))
    _append_task_output(task_id, field, decoder.decode(b"", final=True))


async def _terminate_task_process(process: asyncio.subprocess.Process) -> None:
    """Stop the whole command process tree without blocking the event loop."""
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=3)
        return

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=3)


def _task_disabled_reason() -> str | None:
    capabilities = _build_coding_capabilities()
    if capabilities.background_tasks:
        return None
    return (
        capabilities.reasons.get("background_tasks")
        or capabilities.reasons.get("external_process_tools")
        or "Background command tasks are disabled."
    )


async def _run_background_task_admitted(
    task_id: str,
    root: Path,
    command: str,
    timeout_seconds: int,
) -> None:
    process: asyncio.subprocess.Process | None = None
    reader_tasks: list[asyncio.Task[None]] = []
    runtime: CodingTaskRuntime | None = None
    try:
        task = _TASKS[task_id]
        runtime = await _runtime_for_task(task, root)
        _TASKS[task_id] = await runtime.mark_running(_TASKS[task_id])
        spawn_options: dict[str, Any] = {}
        if sys.platform == "win32":
            spawn_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            spawn_options["start_new_session"] = True
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **spawn_options,
        )
        _TASK_PROCESSES[task_id] = process
        reader_tasks = [
            asyncio.create_task(
                _stream_task_pipe(task_id, process.stdout, "stdout_tail")
            ),
            asyncio.create_task(
                _stream_task_pipe(task_id, process.stderr, "stderr_tail")
            ),
        ]
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            await _terminate_task_process(process)
        await asyncio.gather(*reader_tasks, return_exceptions=True)
        task = _TASKS[task_id]
        stopped = task.status == "stopping"
        if timed_out:
            status = "stalled"
            error = f"Command timed out after {timeout_seconds}s"
        elif stopped:
            status = "cancelled"
            error = "Stopped by user"
        elif process.returncode == 0:
            status = "completed"
            error = None
        else:
            status = "failed"
            error = "Command exited with a non-zero status"
        _TASKS[task_id] = await runtime.settle(
            task,
            status=status,
            returncode=process.returncode,
            error=error,
            reason_code=("terminal_task_timeout" if timed_out else None),
        )
    except Exception as exc:
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                await _terminate_task_process(process)
        for reader_task in reader_tasks:
            if not reader_task.done():
                reader_task.cancel()
        if reader_tasks:
            await asyncio.gather(*reader_tasks, return_exceptions=True)
        task = _TASKS[task_id]
        if runtime is None:
            runtime = await _runtime_for_task(task, root)
        try:
            _TASKS[task_id] = await runtime.settle(
                task,
                status="failed",
                returncode=process.returncode if process is not None else None,
                error=str(exc),
                reason_code="terminal_task_execution_failed",
            )
        except Exception:
            _TASKS[task_id] = task.model_copy(
                update={
                    "status": "failed",
                    "ended_at": _now_iso(),
                    "updated_at": _now_iso(),
                    "error": str(exc),
                },
            )
            runtime.save(_TASKS[task_id])
    finally:
        if process is not None:
            _TASK_PROCESSES.pop(task_id, None)
        _release_task_slot(task_id)


async def _run_background_task(
    task_id: str,
    root: Path,
    command: str,
    timeout_seconds: int,
) -> None:
    """Run a coding command under the shared desktop admission governor."""

    try:
        async with get_runtime_concurrency_governor().slot(
            category="coding",
            owner_key=f"coding:{root.resolve()}",
        ):
            await _run_background_task_admitted(
                task_id,
                root,
                command,
                timeout_seconds,
            )
    except ConcurrencyAdmissionError as exc:
        task = _TASKS.get(task_id)
        if task is not None:
            runtime = await _runtime_for_task(task, root)
            _TASKS[task_id] = await runtime.settle(
                task,
                status="failed",
                returncode=None,
                error=str(exc),
                reason_code=exc.code,
            )
        _release_task_slot(task_id)


def _safe_import_dir(root: Path, target_dir: str) -> Path:
    name = target_dir.strip().strip("/\\")
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid target directory")
    if Path(name).is_absolute() or "/" in name or "\\" in name:
        raise HTTPException(
            status_code=400,
            detail="Target directory must be a single folder name",
        )
    if name.startswith(".") or _is_sensitive_file(name) or name in SKIP_DIRS:
        raise HTTPException(status_code=400, detail="Target directory is hidden")
    target = _safe_join(root, name)
    if target.exists():
        if not target.is_dir():
            raise HTTPException(status_code=409, detail="Target path is not a directory")
        if any(target.iterdir()):
            raise HTTPException(status_code=409, detail="Target directory is not empty")
    return target


def _clone_project(root: Path, request: CodeProjectCloneRequest) -> CodeGitMutationResponse:
    _require_confirmation(
        confirmed=request.confirmed,
        phrase=request.confirmation_phrase,
        expected_phrase=CLONE_CONFIRMATION,
        action="clone project",
    )
    url = request.url.strip()
    if not (
        url.startswith("https://")
        or url.startswith("http://")
        or url.startswith("ssh://")
        or re.match(r"^[\w.-]+@[\w.-]+:", url)
    ):
        raise HTTPException(status_code=400, detail="Unsupported Git clone URL")
    target = _safe_import_dir(root, request.target_dir)
    result = _run_git_no_repo(root, ["clone", url, str(target)], timeout=300.0)
    return CodeGitMutationResponse(
        is_repo=_git_repo_error(target) is None if target.exists() else False,
        ok=result.returncode == 0,
        paths=[target.name],
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
        error=None if result.returncode == 0 else result.stderr.strip() or "git clone failed",
    )


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _is_import_skipped_path(path: Path, root: Path) -> bool:
    if _should_skip(path) or _is_under_skipped_dir(path, root):
        return True
    return any(part in SKIP_DIRS for part in path.relative_to(root).parts)


def _extract_zip_project(root: Path, target_dir: str, data: bytes) -> CodeGitMutationResponse:
    if len(data) > MAX_IMPORT_ZIP_BYTES:
        raise HTTPException(status_code=413, detail="Zip file is too large")
    target = _safe_import_dir(root, target_dir)
    target.mkdir(parents=True, exist_ok=True)
    extracted = 0
    skipped = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_IMPORT_ZIP_ENTRIES:
            raise HTTPException(status_code=413, detail="Zip has too many entries")
        for info in infos:
            raw_name = info.filename
            if not raw_name or raw_name.endswith("/"):
                continue
            if _zip_member_is_symlink(info):
                skipped += 1
                continue
            relative = Path(raw_name)
            if relative.is_absolute() or ".." in relative.parts:
                raise HTTPException(status_code=400, detail="Zip path escapes target")
            destination = (target / relative).resolve(strict=False)
            try:
                destination.relative_to(target)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Zip path escapes target") from exc
            if _is_import_skipped_path(destination, target):
                skipped += 1
                continue
            if info.file_size > MAX_FILE_WRITE_BYTES:
                skipped += 1
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(info))
            extracted += 1
    return CodeGitMutationResponse(
        is_repo=_git_repo_error(target) is None,
        ok=True,
        paths=[target.name],
        stdout=f"Imported {extracted} files into {target.name}; skipped {skipped}",
    )


def _coding_distribution() -> str:
    raw = (
        os.environ.get("LANTAI_DISTRIBUTION_CHANNEL")
        or os.environ.get("LANTAI_RELEASE_CHANNEL")
        or os.environ.get("LANTAI_DISTRIBUTION")
        or ""
    )
    normalized = raw.strip().lower()
    app_store_flag = os.environ.get("LANTAI_APP_STORE", "").strip().lower()
    if normalized in APP_STORE_DISTRIBUTIONS or app_store_flag in {
        "1",
        "true",
        "yes",
    }:
        return "app_store"
    if any("lantai_app store" == part.lower() for part in Path(__file__).parts):
        return "app_store"
    return "developer_id"


def _build_coding_capabilities() -> CodeCodingCapabilitiesResponse:
    distribution = _coding_distribution()
    app_store = distribution == "app_store"
    reasons: dict[str, str] = {}
    git_available = shutil.which("git") is not None
    if app_store:
        reasons["git_read"] = (
            "Git subprocess integration is disabled for App Store builds until "
            "sandbox-compatible entitlements and review copy are settled."
        )
        reasons["git_write"] = (
            "Git write actions are disabled for App Store builds until the "
            "sandbox-compatible confirmation flow is reviewed."
        )
        reasons["git_dangerous_write"] = (
            "Destructive Git actions are disabled for App Store builds."
        )
        reasons["project_import"] = (
            "Project clone/import is disabled for App Store builds."
        )
        reasons["external_process_tools"] = (
            "AST/LSP helper subprocesses are disabled for App Store builds."
        )
        reasons["background_tasks"] = (
            "Background command tasks are disabled for App Store builds until "
            "sandbox-compatible execution is reviewed."
        )
    elif not git_available:
        reasons["git_read"] = "git binary was not found."
        reasons["git_write"] = "git binary was not found."
        reasons["git_dangerous_write"] = "git binary was not found."
        reasons["project_import"] = "git binary was not found."

    try:
        from ...agents.tools.coding import detect_coding_tool_capabilities

        tool_caps = detect_coding_tool_capabilities(distribution=distribution)
        ast_available = tool_caps["ast"].available
        lsp_available = tool_caps["lsp"].available
        if not ast_available and tool_caps["ast"].reason:
            reasons["ast_tools"] = tool_caps["ast"].reason
        if not lsp_available and tool_caps["lsp"].reason:
            reasons["lsp_tools"] = tool_caps["lsp"].reason
    except Exception:
        ast_available = False
        lsp_available = False
        reasons["ast_tools"] = "Unable to probe AST helper dependencies."
        reasons["lsp_tools"] = "Unable to probe LSP helper dependencies."

    return CodeCodingCapabilitiesResponse(
        distribution=distribution,
        git_read=git_available and not app_store,
        git_write=git_available and not app_store,
        git_dangerous_write=git_available and not app_store,
        project_import=git_available and not app_store,
        ast_tools=ast_available and not app_store,
        lsp_tools=lsp_available and not app_store,
        external_process_tools=not app_store,
        background_tasks=not app_store,
        reasons=reasons,
    )


def _git_disabled_reason() -> str | None:
    capabilities = _build_coding_capabilities()
    if capabilities.git_read:
        return None
    return capabilities.reasons.get("git_read") or "Git integration is disabled."


def _git_write_disabled_reason() -> str | None:
    capabilities = _build_coding_capabilities()
    if capabilities.git_write:
        return None
    return capabilities.reasons.get("git_write") or "Git write actions are disabled."


def _git_dangerous_disabled_reason() -> str | None:
    capabilities = _build_coding_capabilities()
    if capabilities.git_dangerous_write:
        return None
    return (
        capabilities.reasons.get("git_dangerous_write")
        or "Dangerous Git actions are disabled."
    )


def _project_import_disabled_reason() -> str | None:
    capabilities = _build_coding_capabilities()
    if capabilities.project_import:
        return None
    return capabilities.reasons.get("project_import") or "Project import is disabled."


@router.get("/capabilities", response_model=CodeCodingCapabilitiesResponse)
async def coding_capabilities() -> CodeCodingCapabilitiesResponse:
    return _build_coding_capabilities()


@router.get("/workbench-state", response_model=CodeWorkbenchStateResponse)
async def get_coding_workbench_state(
    request: Request,
    workspace_dir: str | None = Query(default=None),
    chat_id: str | None = Query(default=None),
    tree_path: str = Query(default=""),
    tree_depth: int = Query(default=1, ge=1, le=4),
    tree_limit: int = Query(default=300, ge=20, le=1000),
) -> CodeWorkbenchStateResponse:
    root = await _workspace_root(request, workspace_dir)
    capabilities = _build_coding_capabilities()
    file_tree = await asyncio.to_thread(
        _build_tree_slice,
        root,
        relative_path=tree_path,
        max_entries=tree_limit,
        max_depth=tree_depth,
        cursor=None,
        limit=tree_limit,
    )

    disabled = _git_disabled_reason()
    git_status_response: CodeGitStatusResponse | None = None
    if disabled:
        git_status_response = CodeGitStatusResponse(is_repo=False, error=disabled)
    else:
        repo_error = await asyncio.to_thread(_git_repo_error, root)
        if repo_error:
            git_status_response = CodeGitStatusResponse(is_repo=False, error=repo_error)
        else:
            result = await asyncio.to_thread(
                _run_git,
                root,
                ["status", "--short", "--branch", "--untracked-files=normal"],
            )
            if result.returncode != 0:
                git_status_response = CodeGitStatusResponse(
                    is_repo=False,
                    error=result.stderr.strip() or "Unable to read git status",
                )
            else:
                branch, entries = _parse_git_status(result.stdout)
                entries = [
                    entry
                    for entry in entries
                    if _is_visible_relative_path(root, entry.path)
                    and (
                        entry.original_path is None
                        or _is_visible_relative_path(root, entry.original_path)
                    )
                ]
                git_status_response = CodeGitStatusResponse(
                    is_repo=True,
                    branch=branch,
                    entries=entries,
                    summary=_status_summary(entries),
                )

    projects = await list_recent_projects(
        request,
        workspace_dir=str(root),
        limit=12,
    )
    tasks = await list_coding_tasks(request, workspace_dir=str(root))
    coding_state: CodeCodingState | None = None
    if chat_id:
        try:
            coding_state = await get_coding_state(request, chat_id=chat_id)
        except HTTPException:
            coding_state = None
    health_status = "ready"
    health_message = "Coding 工作区可用"
    if capabilities.distribution == "app_store":
        health_status = "limited"
        health_message = "App Store 版已启用沙盒降级能力"
    elif git_status_response and not git_status_response.is_repo:
        health_status = "limited"
        health_message = git_status_response.error or "当前工作区不是 Git 仓库"

    return CodeWorkbenchStateResponse(
        workspace_dir=str(root),
        workspace_name=root.name or str(root),
        health=CodeWorkbenchHealth(status=health_status, message=health_message),
        capabilities=capabilities,
        file_tree=file_tree,
        git_status=git_status_response,
        recent_projects=projects.projects,
        tasks=tasks.tasks,
        coding_state=coding_state,
    )


@router.get("/state", response_model=CodeCodingState)
async def get_coding_state(
    request: Request,
    chat_id: str = Query(..., min_length=1),
) -> CodeCodingState:
    chat_manager = await _chat_manager_for_request(request)
    chat = await chat_manager.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Coding chat not found")
    return _state_from_chat(chat)


@router.put("/state", response_model=CodeCodingState)
async def put_coding_state(
    request: Request,
    payload: CodeCodingState = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeCodingState:
    chat_manager = await _chat_manager_for_request(request)
    chat = await chat_manager.get_chat(payload.chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Coding chat not found")
    root = await _workspace_root(request, payload.workspace_dir or workspace_dir)
    state = _sanitize_coding_state(root, payload)
    dumped = state.model_dump()
    if len(json.dumps(dumped, ensure_ascii=False).encode("utf-8")) > MAX_CODING_STATE_BYTES:
        raise HTTPException(status_code=413, detail="Coding state is too large")
    grant_id = str(
        request.headers.get("X-Workspace-Grant-Id")
        or request.headers.get("x-workspace-grant-id")
        or "",
    ).strip()
    meta_updates: dict[str, object] = {
        "coding_mode": True,
        "coding_state": dumped,
    }
    remove_keys: list[str] = []
    if grant_id:
        meta_updates["workspace_grant_id"] = grant_id
        remove_keys.extend(["coding_workspace_dir", "manual_workspace_dir"])
    else:
        meta_updates["coding_workspace_dir"] = state.workspace_dir
        meta_updates["manual_workspace_dir"] = state.workspace_dir
    updated = await chat_manager.merge_chat_meta(
        state.chat_id,
        meta_updates,
        remove_keys=remove_keys,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Coding chat not found")
    return _state_from_chat(updated)


@router.get("/projects/recent", response_model=CodeProjectListResponse)
async def list_recent_projects(
    request: Request,
    workspace_dir: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> CodeProjectListResponse:
    projects: list[CodeProjectSummary] = []
    seen: set[str] = set()

    try:
        root = await _workspace_root(request, workspace_dir)
        summary = await asyncio.to_thread(_project_summary, root)
        active_grant_id = str(
            request.headers.get("X-Workspace-Grant-Id")
            or request.headers.get("x-workspace-grant-id")
            or "",
        ).strip()
        if active_grant_id:
            summary.workspace_grant_id = active_grant_id
        projects.append(summary)
        seen.add(summary.workspace_dir)
    except HTTPException:
        pass

    workspace = await get_agent_for_request(request)
    agent_id = str(getattr(workspace, "agent_id", "default"))
    for grant in default_workspace_grant_registry.list(agent_id=agent_id):
        try:
            binding = default_workspace_grant_registry.resolve(
                grant.id,
                agent_id=agent_id,
                operation="write",
            )
        except WorkspaceGrantError:
            continue
        summary = await asyncio.to_thread(
            _project_summary,
            binding.project_root,
        )
        summary.workspace_grant_id = grant.id
        if summary.workspace_dir in seen:
            for existing in projects:
                if existing.workspace_dir == summary.workspace_dir:
                    existing.workspace_grant_id = grant.id
            continue
        projects.append(summary)
        seen.add(summary.workspace_dir)
        if len(projects) >= limit:
            return CodeProjectListResponse(projects=projects[:limit])

    chat_manager = await _chat_manager_for_request(request)
    chats = await chat_manager.list_chats()
    chats = sorted(
        chats,
        key=lambda chat: getattr(chat, "updated_at", None) or getattr(chat, "created_at", None),
        reverse=True,
    )
    for chat in chats:
        meta = dict(chat.meta or {})
        if not (
            meta.get("coding_mode") is True
            or isinstance(meta.get("coding_state"), dict)
            or str(chat.session_id).startswith("coding:")
        ):
            continue
        raw_path = (
            meta.get("coding_workspace_dir")
            or meta.get("manual_workspace_dir")
            or meta.get("workspace_dir")
        )
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        summary = await asyncio.to_thread(
            _project_summary,
            Path(raw_path),
            chat_id=chat.id,
            updated_at=getattr(chat, "updated_at", None).isoformat()
            if getattr(chat, "updated_at", None)
            else None,
        )
        if summary.workspace_dir in seen:
            continue
        projects.append(summary)
        seen.add(summary.workspace_dir)
        if len(projects) >= limit:
            break
    return CodeProjectListResponse(projects=projects[:limit])


@router.post("/projects/activate", response_model=CodeProjectSummary)
async def activate_project(
    request: Request,
    payload: CodeProjectActivateRequest = Body(...),
) -> CodeProjectSummary:
    root = await _workspace_root(request, payload.workspace_dir)
    summary = await asyncio.to_thread(_project_summary, root, chat_id=payload.chat_id)
    if payload.chat_id:
        chat_manager = await _chat_manager_for_request(request)
        chat = await chat_manager.get_chat(payload.chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Coding chat not found")
        state = _state_from_chat(chat).model_copy(update={"workspace_dir": str(root)})
        state = _sanitize_coding_state(root, state)
        grant_id = str(
            request.headers.get("X-Workspace-Grant-Id")
            or request.headers.get("x-workspace-grant-id")
            or "",
        ).strip()
        updates: dict[str, object] = {
            "coding_mode": True,
            "coding_state": state.model_dump(),
        }
        remove_keys: list[str] = []
        if grant_id:
            updates["workspace_grant_id"] = grant_id
            remove_keys.extend(["coding_workspace_dir", "manual_workspace_dir"])
            summary.workspace_grant_id = grant_id
        else:
            updates["coding_workspace_dir"] = str(root)
            updates["manual_workspace_dir"] = str(root)
        await chat_manager.merge_chat_meta(
            payload.chat_id,
            updates,
            remove_keys=remove_keys,
        )
    return summary


@router.post("/projects/create-empty", response_model=CodeProjectSummary)
async def create_empty_project(
    request: Request,
    payload: CodeProjectCreateEmptyRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeProjectSummary:
    root = await _workspace_root(request, workspace_dir)
    target = _safe_import_dir(root, payload.target_dir)
    target.mkdir(parents=True, exist_ok=True)
    return await asyncio.to_thread(_project_summary, target)


@router.get("/files/tree", response_model=CodeFileTreeResponse)
async def list_code_files(
    request: Request,
    workspace_dir: str | None = Query(default=None),
    path: str = Query(default=""),
    depth: int = Query(default=1, ge=1, le=8),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=300, ge=20, le=1000),
    max_entries: int = Query(default=MAX_TREE_ENTRIES, ge=100, le=20_000),
    max_depth: int | None = Query(default=None, ge=1, le=20),
) -> CodeFileTreeResponse:
    root = await _workspace_root(request, workspace_dir)
    return await asyncio.to_thread(
        _build_tree_slice,
        root,
        relative_path=path,
        max_entries=max_entries,
        max_depth=max_depth or depth,
        cursor=cursor,
        limit=limit,
    )


@router.get("/files/content", response_model=CodeFileContentResponse)
async def read_code_file(
    request: Request,
    path: str = Query(..., min_length=1),
    workspace_dir: str | None = Query(default=None),
) -> CodeFileContentResponse:
    root = await _workspace_root(request, workspace_dir)
    return await asyncio.to_thread(_read_code_file, root, path)


@router.get("/files/download")
async def download_code_file(
    request: Request,
    path: str = Query(..., min_length=1),
    workspace_dir: str | None = Query(default=None),
) -> FileResponse:
    root = await _workspace_root(request, workspace_dir)
    target = await asyncio.to_thread(_downloadable_code_file, root, path)
    return FileResponse(
        target,
        filename=target.name,
        content_disposition_type="attachment",
    )


@router.put("/files/content", response_model=CodeFileContentResponse)
async def save_code_file(
    request: Request,
    payload: CodeFileSaveRequest = Body(...),
    path: str = Query(..., min_length=1),
    workspace_dir: str | None = Query(default=None),
) -> CodeFileContentResponse:
    root = await _workspace_root(request, workspace_dir)
    return await asyncio.to_thread(_write_code_file, root, path, payload)


@router.get("/files/watch")
async def watch_code_files(
    request: Request,
    workspace_dir: str | None = Query(default=None),
    interval: float = Query(
        default=DEFAULT_WATCH_INTERVAL_SECONDS,
        ge=0.5,
        le=10.0,
    ),
) -> StreamingResponse:
    root = await _workspace_root(request, workspace_dir)

    async def event_generator() -> AsyncIterator[str]:
        before = await asyncio.to_thread(_snapshot, root)
        yield _sse("ready", {"root_path": str(root), "count": len(before)})
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(interval)
            after = await asyncio.to_thread(_snapshot, root)
            changed = _changed_paths(before, after)
            if changed:
                yield _sse(
                    "change",
                    {
                        "root_path": str(root),
                        "changed_paths": changed,
                        "count": len(after),
                    },
                )
                before = after

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/git/status", response_model=CodeGitStatusResponse)
async def git_status(
    request: Request,
    workspace_dir: str | None = Query(default=None),
) -> CodeGitStatusResponse:
    disabled = _git_disabled_reason()
    if disabled:
        return CodeGitStatusResponse(is_repo=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitStatusResponse(is_repo=False, error=error)
    result = await asyncio.to_thread(
        _run_git,
        root,
        ["status", "--short", "--branch", "--untracked-files=normal"],
    )
    if result.returncode != 0:
        return CodeGitStatusResponse(
            is_repo=False,
            error=result.stderr.strip() or "Unable to read git status",
        )
    branch, entries = _parse_git_status(result.stdout)
    entries = [
        entry
        for entry in entries
        if _is_visible_relative_path(root, entry.path)
        and (
            entry.original_path is None
            or _is_visible_relative_path(root, entry.original_path)
        )
    ]
    return CodeGitStatusResponse(
        is_repo=True,
        branch=branch,
        entries=entries,
        summary=_status_summary(entries),
    )


@router.get("/git/diff", response_model=CodeGitDiffResponse)
async def git_diff(
    request: Request,
    workspace_dir: str | None = Query(default=None),
    path: str | None = Query(default=None),
) -> CodeGitDiffResponse:
    disabled = _git_disabled_reason()
    if disabled:
        return CodeGitDiffResponse(is_repo=False, path=path, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitDiffResponse(is_repo=False, path=path, error=error)

    args_suffix: list[str] = []
    if path and path.strip():
        target = _safe_join(root, path)
        _ensure_visible_file_target(root, target)
        args_suffix = ["--", _relative_posix(target, root)]

    unstaged_result = await asyncio.to_thread(
        _run_git,
        root,
        ["diff", *args_suffix],
    )
    staged_result = await asyncio.to_thread(
        _run_git,
        root,
        ["diff", "--cached", *args_suffix],
    )
    if unstaged_result.returncode != 0 or staged_result.returncode != 0:
        return CodeGitDiffResponse(
            is_repo=True,
            path=path,
            error=(
                unstaged_result.stderr.strip()
                or staged_result.stderr.strip()
                or "Unable to read git diff"
            ),
        )

    unstaged, unstaged_truncated = _truncate(unstaged_result.stdout)
    staged, staged_truncated = _truncate(staged_result.stdout)
    return CodeGitDiffResponse(
        is_repo=True,
        path=path,
        unstaged=unstaged,
        staged=staged,
        truncated=unstaged_truncated or staged_truncated,
    )


@router.get("/git/branches", response_model=CodeGitBranchesResponse)
async def git_branches(
    request: Request,
    workspace_dir: str | None = Query(default=None),
) -> CodeGitBranchesResponse:
    disabled = _git_disabled_reason()
    if disabled:
        return CodeGitBranchesResponse(is_repo=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitBranchesResponse(is_repo=False, error=error)
    return await asyncio.to_thread(_git_branches, root)


@router.post("/git/checkout", response_model=CodeGitMutationResponse)
async def git_checkout(
    request: Request,
    payload: CodeGitCheckoutRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _git_write_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=error)
    return await asyncio.to_thread(_git_checkout, root, payload)


@router.post("/git/create-branch", response_model=CodeGitMutationResponse)
async def git_create_branch(
    request: Request,
    payload: CodeGitCreateBranchRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _git_write_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=error)
    return await asyncio.to_thread(_git_create_branch, root, payload)


@router.get("/git/commit-diff", response_model=CodeGitCommitDiffResponse)
async def git_commit_diff(
    request: Request,
    commit: str = Query(..., min_length=1, max_length=120),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitCommitDiffResponse:
    disabled = _git_disabled_reason()
    if disabled:
        return CodeGitCommitDiffResponse(is_repo=False, commit=commit, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitCommitDiffResponse(is_repo=False, commit=commit, error=error)
    return await asyncio.to_thread(_git_commit_diff, root, commit)


@router.post("/git/stage", response_model=CodeGitMutationResponse)
async def git_stage(
    request: Request,
    payload: CodeGitPathRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _git_write_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=error)
    return await asyncio.to_thread(_git_stage_paths, root, payload.paths)


@router.post("/git/unstage", response_model=CodeGitMutationResponse)
async def git_unstage(
    request: Request,
    payload: CodeGitPathRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _git_write_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=error)
    return await asyncio.to_thread(_git_unstage_paths, root, payload.paths)


@router.post("/git/commit", response_model=CodeGitMutationResponse)
async def git_commit(
    request: Request,
    payload: CodeGitCommitRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _git_write_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=error)
    return await asyncio.to_thread(_git_commit, root, payload)


@router.post("/git/init", response_model=CodeGitMutationResponse)
async def git_init(
    request: Request,
    payload: CodeGitInitRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _git_write_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    return await asyncio.to_thread(_git_init, root, payload)


@router.post("/git/discard", response_model=CodeGitMutationResponse)
async def git_discard(
    request: Request,
    payload: CodeGitDiscardRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _git_dangerous_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=error)
    return await asyncio.to_thread(_git_discard, root, payload)


@router.post("/git/clean", response_model=CodeGitMutationResponse)
async def git_clean(
    request: Request,
    payload: CodeGitCleanRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _git_dangerous_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=error)
    return await asyncio.to_thread(_git_clean, root, payload)


@router.post("/git/reset-hard", response_model=CodeGitMutationResponse)
async def git_reset_hard(
    request: Request,
    payload: CodeGitResetRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _git_dangerous_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=error)
    return await asyncio.to_thread(_git_reset_hard, root, payload)


@router.post("/git/revert", response_model=CodeGitMutationResponse)
async def git_revert(
    request: Request,
    payload: CodeGitRevertRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _git_dangerous_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=error)
    return await asyncio.to_thread(_git_revert, root, payload)


@router.post("/projects/clone", response_model=CodeGitMutationResponse)
async def clone_project(
    request: Request,
    payload: CodeProjectCloneRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeGitMutationResponse:
    disabled = _project_import_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    return await asyncio.to_thread(_clone_project, root, payload)


@router.post("/projects/import-zip", response_model=CodeGitMutationResponse)
async def import_zip_project(
    request: Request,
    target_dir: str = Query(..., min_length=1),
    confirmed: bool = Query(default=False),
    confirmation_phrase: str = Query(default=""),
    workspace_dir: str | None = Query(default=None),
    file: UploadFile = File(...),
) -> CodeGitMutationResponse:
    disabled = _project_import_disabled_reason()
    if disabled:
        return CodeGitMutationResponse(is_repo=False, ok=False, error=disabled)
    _require_confirmation(
        confirmed=confirmed,
        phrase=confirmation_phrase,
        expected_phrase=IMPORT_ZIP_CONFIRMATION,
        action="import zip",
    )
    root = await _workspace_root(request, workspace_dir)
    data = await file.read()
    return await asyncio.to_thread(_extract_zip_project, root, target_dir, data)


@router.post("/tasks", response_model=CodeTaskRun)
async def start_coding_task(
    request: Request,
    payload: CodeTaskStartRequest = Body(...),
    workspace_dir: str | None = Query(default=None),
) -> CodeTaskRun:
    disabled = _task_disabled_reason()
    if disabled:
        raise HTTPException(status_code=403, detail=disabled)
    _require_confirmation(confirmed=payload.confirmed, action="background task")
    root = await _workspace_root(request, workspace_dir)
    agent = await get_agent_for_request(request)
    agent_id = str(getattr(agent, "agent_id", "default"))
    await _hydrate_coding_tasks(root, agent_id=agent_id)
    command = _guard_task_command(payload.command)
    task_id = str(uuid.uuid4())
    if not _reserve_task_slot(task_id):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "coding_task_capacity_reached",
                "message": (
                    "Too many background coding tasks are running. "
                    "Wait for one to finish or stop an existing task."
                ),
                "limit": MAX_CONCURRENT_CODING_TASKS,
            },
        )
    task = CodeTaskRun(
        task_id=task_id,
        workspace_dir=str(root),
        command=command,
        status="queued",
        started_at=_now_iso(),
        timeout_seconds=payload.timeout_seconds,
    )
    runtime = CodingTaskRuntime(root, agent_id=agent_id)
    try:
        task = await runtime.create(task)
        _TASKS[task_id] = task
        _TASK_RUNTIMES[task_id] = runtime
        asyncio.create_task(
            _run_background_task(task_id, root, command, payload.timeout_seconds)
        )
    except Exception:
        _release_task_slot(task_id)
        raise
    return task


@router.get("/tasks", response_model=CodeTaskListResponse)
async def list_coding_tasks(
    request: Request,
    workspace_dir: str | None = Query(default=None),
) -> CodeTaskListResponse:
    root = await _workspace_root(request, workspace_dir)
    agent = await get_agent_for_request(request)
    await _hydrate_coding_tasks(
        root,
        agent_id=str(getattr(agent, "agent_id", "default")),
    )
    tasks = [
        task
        for task in _TASKS.values()
        if Path(task.workspace_dir).expanduser().resolve(strict=False) == root
    ]
    tasks.sort(key=lambda task: task.started_at, reverse=True)
    return CodeTaskListResponse(tasks=tasks)


@router.get("/tasks/{task_id}", response_model=CodeTaskRun)
async def get_coding_task(
    request: Request,
    task_id: str,
    workspace_dir: str | None = Query(default=None),
) -> CodeTaskRun:
    root = await _workspace_root(request, workspace_dir)
    agent = await get_agent_for_request(request)
    await _hydrate_coding_tasks(
        root,
        agent_id=str(getattr(agent, "agent_id", "default")),
    )
    task = _TASKS.get(task_id)
    if task is None or Path(task.workspace_dir).resolve(strict=False) != root:
        raise HTTPException(status_code=404, detail="Coding task not found")
    return task


@router.post("/tasks/{task_id}/stop", response_model=CodeTaskRun)
async def stop_coding_task(
    request: Request,
    task_id: str,
    workspace_dir: str | None = Query(default=None),
) -> CodeTaskRun:
    root = await _workspace_root(request, workspace_dir)
    agent = await get_agent_for_request(request)
    await _hydrate_coding_tasks(
        root,
        agent_id=str(getattr(agent, "agent_id", "default")),
    )
    task = _TASKS.get(task_id)
    if task is None or Path(task.workspace_dir).resolve(strict=False) != root:
        raise HTTPException(status_code=404, detail="Coding task not found")
    process = _TASK_PROCESSES.get(task_id)
    if process is None:
        return task
    runtime = await _runtime_for_task(
        task,
        root,
        agent_id=str(getattr(agent, "agent_id", "default")),
    )
    _TASKS[task_id] = await runtime.request_stop(task)
    await _terminate_task_process(process)
    return _TASKS[task_id]


def _write_code_file(
    root: Path,
    relative_path: str,
    request: CodeFileSaveRequest,
) -> CodeFileContentResponse:
    target = _safe_join(root, relative_path)
    _ensure_visible_file_target(root, target)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {relative_path}")

    current = _read_code_file(root, relative_path)
    if current.binary:
        raise HTTPException(status_code=415, detail="Binary files are not editable")
    if request.expected_sha256 and request.expected_sha256 != current.sha256:
        preview = current.content[:4000]
        raise HTTPException(
            status_code=409,
            detail={
                "error": "File changed on disk; resolve conflict before saving",
                "code": "coding_save_conflict",
                "path": current.path,
                "current_sha256": current.sha256,
                "expected_sha256": request.expected_sha256,
                "modified_time": current.modified_time,
                "disk_preview": preview,
                "disk_preview_truncated": len(current.content) > len(preview),
            },
        )

    data = request.content.encode("utf-8")
    if len(data) > MAX_FILE_WRITE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File is too large to save from Coding "
                f"({len(data)} bytes > {MAX_FILE_WRITE_BYTES} bytes)"
            ),
        )

    tmp = target.with_name(f".{target.name}.lantai-tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return _read_code_file(root, relative_path)


@router.get("/git/log", response_model=CodeGitLogResponse)
async def git_log(
    request: Request,
    workspace_dir: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
) -> CodeGitLogResponse:
    disabled = _git_disabled_reason()
    if disabled:
        return CodeGitLogResponse(is_repo=False, error=disabled)
    root = await _workspace_root(request, workspace_dir)
    error = await asyncio.to_thread(_git_repo_error, root)
    if error:
        return CodeGitLogResponse(is_repo=False, error=error)
    result = await asyncio.to_thread(
        _run_git,
        root,
        [
            "log",
            f"--max-count={limit}",
            "--date=iso-strict",
            "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s",
        ],
    )
    if result.returncode != 0:
        return CodeGitLogResponse(
            is_repo=True,
            error=result.stderr.strip() or "Unable to read git log",
        )

    entries: list[CodeGitLogEntry] = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f", 4)
        if len(parts) != 5:
            continue
        entries.append(
            CodeGitLogEntry(
                hash=parts[0],
                short_hash=parts[1],
                author=parts[2],
                date=parts[3],
                subject=parts[4],
            )
        )
    return CodeGitLogResponse(is_repo=True, entries=entries)
