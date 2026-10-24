from __future__ import annotations

import asyncio
import shutil
import subprocess
import io
import shlex
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lantai.app.routers import coding
from lantai.app.workspace_grants import (
    WorkspaceGrantRegistry,
    create_workspace_selection_receipt,
)


@pytest.mark.asyncio
async def test_background_task_streams_output_before_process_exit(
    tmp_path: Path,
) -> None:
    task_id = "streaming-task"
    script = (
        "import sys,time; "
        "print('first', flush=True); "
        "time.sleep(0.25); "
        "print('second', flush=True); "
        "print('problem', file=sys.stderr, flush=True)"
    )
    command = f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}"
    coding._TASKS[task_id] = coding.CodeTaskRun(
        task_id=task_id,
        workspace_dir=str(tmp_path),
        command=command,
        status="queued",
        started_at=coding._now_iso(),
    )

    runner = asyncio.create_task(
        coding._run_background_task(task_id, tmp_path, command, 5)
    )
    try:
        for _ in range(80):
            task = coding._TASKS[task_id]
            if "first" in task.stdout_tail:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("first output chunk was not exposed while task ran")

        assert not runner.done()
        assert coding._TASKS[task_id].status == "running"
        await runner

        completed = coding._TASKS[task_id]
        assert completed.status == "completed"
        assert completed.returncode == 0
        assert "first" in completed.stdout_tail
        assert "second" in completed.stdout_tail
        assert "problem" in completed.stderr_tail
        assert completed.output_seq >= 2
    finally:
        if not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        coding._TASKS.pop(task_id, None)
        coding._TASK_PROCESSES.pop(task_id, None)


def test_background_task_output_tail_is_bounded(tmp_path: Path) -> None:
    task_id = "bounded-task"
    coding._TASKS[task_id] = coding.CodeTaskRun(
        task_id=task_id,
        workspace_dir=str(tmp_path),
        command="noop",
        status="running",
        started_at=coding._now_iso(),
    )
    try:
        coding._append_task_output(
            task_id,
            "stdout_tail",
            "x" * (coding.MAX_TASK_TAIL_CHARS + 100),
        )
        assert len(coding._TASKS[task_id].stdout_tail) == coding.MAX_TASK_TAIL_CHARS
        assert coding._TASKS[task_id].output_seq == 1
    finally:
        coding._TASKS.pop(task_id, None)


def test_background_task_slots_are_bounded_and_reusable() -> None:
    reserved = [f"slot-{index}" for index in range(coding.MAX_CONCURRENT_CODING_TASKS)]
    overflow = "slot-overflow"
    original_reservations = set(coding._TASK_RESERVATIONS)
    coding._TASK_RESERVATIONS.clear()
    try:
        assert all(coding._reserve_task_slot(task_id) for task_id in reserved)
        assert coding._reserve_task_slot(overflow) is False

        coding._release_task_slot(reserved[0])
        assert coding._reserve_task_slot(overflow) is True
    finally:
        coding._TASK_RESERVATIONS.clear()
        coding._TASK_RESERVATIONS.update(original_reservations)


def _flatten_paths(nodes: list[coding.CodeFileNode]) -> set[str]:
    paths: set[str] = set()
    for node in nodes:
        paths.add(node.path)
        paths.update(_flatten_paths(node.children))
    return paths


def test_safe_join_rejects_workspace_escape(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as exc_info:
        coding._safe_join(tmp_path.resolve(), "../outside.txt")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_coding_root_rejects_external_raw_path_and_accepts_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_home = tmp_path / "managed" / "agent"
    project_root = tmp_path / "projects" / "external"
    agent_home.mkdir(parents=True)
    project_root.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    registry = WorkspaceGrantRegistry(
        tmp_path / "managed" / "workspace-grants.json",
        clock=lambda: now,
    )
    grant = registry.claim_selection(
        create_workspace_selection_receipt(
            project_root=project_root,
            agent_id="default",
            desktop_token="secret",
            issued_at=now,
            nonce="coding-selection-1",
        ),
        desktop_token="secret",
        agent_id="default",
    )

    async def fake_agent(_request):
        return SimpleNamespace(
            agent_id="default",
            workspace_dir=agent_home,
        )

    monkeypatch.setattr(coding, "get_agent_for_request", fake_agent)
    monkeypatch.setattr(coding, "default_workspace_grant_registry", registry)

    with pytest.raises(HTTPException) as exc_info:
        await coding._workspace_root(
            SimpleNamespace(headers={}),
            str(project_root),
        )
    assert exc_info.value.status_code == 403

    resolved = await coding._workspace_root(
        SimpleNamespace(
            headers={"X-Workspace-Grant-Id": grant.id},
        ),
        str(project_root),
    )
    assert resolved == project_root.resolve()


def test_build_tree_skips_generated_and_sensitive_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "deploy.pem").write_text("secret\n", encoding="utf-8")

    tree = coding._build_tree(tmp_path.resolve(), max_entries=100, max_depth=4)
    paths = _flatten_paths(tree.children)

    assert "src/app.py" in paths
    assert ".git" not in paths
    assert "node_modules" not in paths
    assert ".env" not in paths
    assert "deploy.pem" not in paths


def test_build_tree_slice_paginates_visible_entries(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"file_{index}.txt").write_text(
            f"{index}\n",
            encoding="utf-8",
        )

    first_page = coding._build_tree_slice(
        tmp_path.resolve(),
        relative_path="",
        max_entries=100,
        max_depth=1,
        cursor=None,
        limit=2,
    )
    second_page = coding._build_tree_slice(
        tmp_path.resolve(),
        relative_path="",
        max_entries=100,
        max_depth=1,
        cursor=first_page.next_cursor,
        limit=2,
    )

    assert first_page.has_more
    assert first_page.next_cursor == "2"
    assert [node.name for node in first_page.children] == [
        "file_0.txt",
        "file_1.txt",
    ]
    assert second_page.has_more
    assert second_page.next_cursor == "4"
    assert [node.name for node in second_page.children] == [
        "file_2.txt",
        "file_3.txt",
    ]


def test_read_code_file_rejects_large_inline_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coding, "MAX_FILE_READ_BYTES", 3)
    (tmp_path / "large.txt").write_text("large", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        coding._read_code_file(tmp_path.resolve(), "large.txt")

    assert exc_info.value.status_code == 413


def test_read_code_file_rejects_hidden_direct_paths(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    with pytest.raises(HTTPException) as env_exc:
        coding._read_code_file(tmp_path.resolve(), ".env")
    with pytest.raises(HTTPException) as git_exc:
        coding._read_code_file(tmp_path.resolve(), ".git/config")

    assert env_exc.value.status_code == 403
    assert git_exc.value.status_code == 403


def test_downloadable_code_file_keeps_exact_bytes_inside_visible_workspace(
    tmp_path: Path,
) -> None:
    target = tmp_path / "draft.bin"
    target.write_bytes(b"draft\x00bytes")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    resolved = coding._downloadable_code_file(tmp_path.resolve(), "draft.bin")

    assert resolved == target.resolve()
    assert resolved.read_bytes() == b"draft\x00bytes"
    with pytest.raises(HTTPException) as hidden_exc:
        coding._downloadable_code_file(tmp_path.resolve(), ".env")
    with pytest.raises(HTTPException) as escape_exc:
        coding._downloadable_code_file(tmp_path.resolve(), "../outside.bin")
    assert hidden_exc.value.status_code == 403
    assert escape_exc.value.status_code == 400


@pytest.mark.asyncio
async def test_download_code_file_rechecks_the_authorized_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "deck.pptx"
    target.write_bytes(b"pptx-bytes")
    resolved_roots: list[str | None] = []

    async def fake_workspace_root(_request, workspace_dir):
        resolved_roots.append(workspace_dir)
        return tmp_path.resolve()

    monkeypatch.setattr(coding, "_workspace_root", fake_workspace_root)

    response = await coding.download_code_file(
        SimpleNamespace(headers={}),
        path="deck.pptx",
        workspace_dir=str(tmp_path),
    )

    assert resolved_roots == [str(tmp_path)]
    assert Path(response.path) == target.resolve()
    assert response.filename == "deck.pptx"


def test_write_code_file_updates_existing_text_file(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("before\n", encoding="utf-8")
    current = coding._read_code_file(tmp_path.resolve(), "notes.md")

    saved = coding._write_code_file(
        tmp_path.resolve(),
        "notes.md",
        coding.CodeFileSaveRequest(
            content="after\n",
            expected_sha256=current.sha256,
        ),
    )

    assert saved.content == "after\n"
    assert target.read_text(encoding="utf-8") == "after\n"


def test_write_code_file_rejects_stale_expected_hash(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("newer\n", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        coding._write_code_file(
            tmp_path.resolve(),
            "notes.md",
            coding.CodeFileSaveRequest(
                content="overwrite\n",
                expected_sha256="not-the-current-hash",
            ),
        )

    assert exc_info.value.status_code == 409
    assert isinstance(exc_info.value.detail, dict)
    assert exc_info.value.detail["code"] == "coding_save_conflict"
    assert exc_info.value.detail["expected_sha256"] == "not-the-current-hash"
    assert exc_info.value.detail["disk_preview"] == "newer\n"
    assert exc_info.value.detail["current_sha256"]
    assert target.read_text(encoding="utf-8") == "newer\n"


def test_git_status_helper_reports_non_repo(tmp_path: Path) -> None:
    error = coding._git_repo_error(tmp_path.resolve())

    assert error


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_git_repo(root: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git binary is not available")
    _run_git(root, ["init"])
    _run_git(root, ["config", "user.email", "coding-test@example.com"])
    _run_git(root, ["config", "user.name", "Coding Test"])
    (root / "README.md").write_text("initial\n", encoding="utf-8")
    _run_git(root, ["add", "README.md"])
    _run_git(root, ["commit", "-m", "Initial commit"])


def test_parse_git_status_preserves_staged_and_unstaged_state() -> None:
    _branch, entries = coding._parse_git_status(
        "## main\n"
        " M changed.txt\n"
        "M  staged.txt\n"
        "MM both.txt\n"
        "?? new.txt\n",
    )

    by_path = {entry.path: entry for entry in entries}
    assert by_path["changed.txt"].unstaged
    assert not by_path["changed.txt"].staged
    assert by_path["staged.txt"].staged
    assert not by_path["staged.txt"].unstaged
    assert by_path["both.txt"].staged
    assert by_path["both.txt"].unstaged
    assert by_path["new.txt"].untracked


def test_git_stage_and_unstage_paths_are_index_only(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "notes.md"
    target.write_text("hello\n", encoding="utf-8")

    staged = coding._git_stage_paths(tmp_path.resolve(), ["notes.md"])
    assert staged.ok
    _branch, entries = coding._parse_git_status(
        coding._run_git(
            tmp_path.resolve(),
            ["status", "--short", "--branch", "--untracked-files=normal"],
        ).stdout,
    )
    assert next(entry for entry in entries if entry.path == "notes.md").staged

    unstaged = coding._git_unstage_paths(tmp_path.resolve(), ["notes.md"])
    assert unstaged.ok
    _branch, entries = coding._parse_git_status(
        coding._run_git(
            tmp_path.resolve(),
            ["status", "--short", "--branch", "--untracked-files=normal"],
        ).stdout,
    )
    assert next(entry for entry in entries if entry.path == "notes.md").untracked
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_git_commit_requires_confirmation_and_staged_changes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "notes.md").write_text("hello\n", encoding="utf-8")
    assert coding._git_stage_paths(tmp_path.resolve(), ["notes.md"]).ok

    with pytest.raises(HTTPException) as exc_info:
        coding._git_commit(
            tmp_path.resolve(),
            coding.CodeGitCommitRequest(message="Add notes", confirmed=False),
        )

    assert exc_info.value.status_code == 400

    committed = coding._git_commit(
        tmp_path.resolve(),
        coding.CodeGitCommitRequest(message="Add notes", confirmed=True),
    )
    assert committed.ok
    log = coding._run_git(tmp_path.resolve(), ["log", "--oneline", "-1"]).stdout
    assert "Add notes" in log


def test_git_stage_rejects_sensitive_paths(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        coding._git_stage_paths(tmp_path.resolve(), [".env"])

    assert exc_info.value.status_code == 403


def test_git_init_with_initial_commit_requires_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git binary is not available")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Coding Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "coding-test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Coding Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "coding-test@example.com")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        coding._git_init(
            tmp_path.resolve(),
            coding.CodeGitInitRequest(confirmed=False, initial_commit=True),
        )

    assert exc_info.value.status_code == 400
    initialized = coding._git_init(
        tmp_path.resolve(),
        coding.CodeGitInitRequest(confirmed=True, initial_commit=True),
    )
    assert initialized.ok
    log = coding._run_git(tmp_path.resolve(), ["log", "--oneline", "-1"]).stdout
    assert "Initial commit" in log


def test_git_discard_requires_phrase_and_restores_changes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "README.md"
    target.write_text("changed\n", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        coding._git_discard(
            tmp_path.resolve(),
            coding.CodeGitDiscardRequest(
                paths=["README.md"],
                confirmed=True,
                confirmation_phrase="wrong",
            ),
        )

    assert exc_info.value.status_code == 400
    discarded = coding._git_discard(
        tmp_path.resolve(),
        coding.CodeGitDiscardRequest(
            paths=["README.md"],
            confirmed=True,
            confirmation_phrase=coding.DISCARD_CONFIRMATION,
        ),
    )
    assert discarded.ok
    assert target.read_text(encoding="utf-8") == "initial\n"


def test_git_clean_requires_phrase_and_removes_untracked(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "scratch.txt"
    target.write_text("temporary\n", encoding="utf-8")

    cleaned = coding._git_clean(
        tmp_path.resolve(),
        coding.CodeGitCleanRequest(
            paths=["scratch.txt"],
            confirmed=True,
            confirmation_phrase=coding.CLEAN_CONFIRMATION,
        ),
    )

    assert cleaned.ok
    assert not target.exists()


def test_git_reset_hard_requires_phrase_and_restores_head(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "README.md"
    target.write_text("changed\n", encoding="utf-8")

    reset = coding._git_reset_hard(
        tmp_path.resolve(),
        coding.CodeGitResetRequest(
            target="HEAD",
            confirmed=True,
            confirmation_phrase=coding.RESET_HARD_CONFIRMATION,
        ),
    )

    assert reset.ok
    assert target.read_text(encoding="utf-8") == "initial\n"


def test_git_branch_helpers_list_and_checkout_branch(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)

    created = coding._git_create_branch(
        tmp_path.resolve(),
        coding.CodeGitCreateBranchRequest(name="feature/coding-state"),
    )
    assert created.ok

    branches = coding._git_branches(tmp_path.resolve())
    assert branches.is_repo
    assert branches.current == "feature/coding-state"
    assert any(branch.name == "feature/coding-state" for branch in branches.branches)

    checked_out = coding._git_checkout(
        tmp_path.resolve(),
        coding.CodeGitCheckoutRequest(name="master"),
    )
    if not checked_out.ok:
        checked_out = coding._git_checkout(
            tmp_path.resolve(),
            coding.CodeGitCheckoutRequest(name="main"),
        )
    assert checked_out.ok


def test_git_branch_name_rejects_unsafe_refs() -> None:
    for name in ["../escape", "-bad", "bad..name", "topic.lock", "bad//@{x}"]:
        with pytest.raises(HTTPException):
            coding._validate_branch_name(name)


def test_git_commit_diff_returns_patch_for_history(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("initial\nchanged\n", encoding="utf-8")
    assert coding._git_stage_paths(tmp_path.resolve(), ["README.md"]).ok
    assert coding._git_commit(
        tmp_path.resolve(),
        coding.CodeGitCommitRequest(message="Change readme", confirmed=True),
    ).ok

    commit = coding._run_git(tmp_path.resolve(), ["rev-parse", "HEAD"]).stdout.strip()
    diff = coding._git_commit_diff(tmp_path.resolve(), commit)

    assert diff.is_repo
    assert "Change readme" in diff.stat
    assert "+changed" in diff.diff


def test_sanitize_coding_state_keeps_visible_dirty_drafts(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    state = coding._sanitize_coding_state(
        tmp_path.resolve(),
        coding.CodeCodingState(
            chat_id="chat-1",
            workspace_dir=str(tmp_path),
            active_file="src/app.py",
            open_tabs=["src/app.py", ".env"],
            dirty_drafts={
                "src/app.py": "print('changed')\n",
                ".env": "TOKEN=leaked\n",
            },
            pending_diffs=["src/app.py", ".env"],
            git_summary={"modified": 1},
        ),
    )

    assert state.workspace_dir == str(tmp_path.resolve())
    assert state.open_tabs == ["src/app.py"]
    assert state.dirty_drafts == {"src/app.py": "print('changed')\n"}
    assert state.pending_diffs == ["src/app.py"]


def test_project_summary_reports_git_status(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)

    summary = coding._project_summary(tmp_path.resolve(), chat_id="chat-1")

    assert summary.exists
    assert summary.is_git
    assert summary.chat_id == "chat-1"


def test_task_guard_blocks_destructive_git_commands() -> None:
    with pytest.raises(HTTPException) as exc_info:
        coding._guard_task_command("git reset --hard HEAD")

    assert exc_info.value.status_code == 400


def test_import_zip_skips_hidden_and_rejects_zip_slip(tmp_path: Path) -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("src/app.py", "print('ok')\n")
        archive.writestr(".env", "TOKEN=secret\n")

    imported = coding._extract_zip_project(
        tmp_path.resolve(),
        "imported",
        data.getvalue(),
    )

    assert imported.ok
    assert (tmp_path / "imported" / "src" / "app.py").exists()
    assert not (tmp_path / "imported" / ".env").exists()

    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../escape.txt", "nope")
    with pytest.raises(HTTPException) as exc_info:
        coding._extract_zip_project(tmp_path.resolve(), "bad", bad.getvalue())
    assert exc_info.value.status_code == 400


def test_coding_capabilities_disable_external_processes_for_app_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANTAI_APP_STORE", "1")

    capabilities = coding._build_coding_capabilities()

    assert capabilities.distribution == "app_store"
    assert not capabilities.git_read
    assert not capabilities.git_write
    assert not capabilities.git_dangerous_write
    assert not capabilities.project_import
    assert not capabilities.external_process_tools
    assert not capabilities.background_tasks
    assert "git_read" in capabilities.reasons
