"""Behavior locks for logical Skill resources and packaged script execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import lantai.agents.tools.skill_resources as skill_resources_module
from lantai.agents.tools.skill_resources import (
    copy_skill_resource,
    list_skill_resources,
    run_skill_script,
)
from lantai.config.context import WorkspaceContext, set_current_workspace_context

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _install_skill(agent_home: Path, *, enabled: bool = True) -> Path:
    skill_dir = agent_home / "skills" / "demo"
    scripts_dir = skill_dir / "scripts"
    references_dir = skill_dir / "references"
    scripts_dir.mkdir(parents=True)
    references_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n",
        encoding="utf-8",
    )
    (scripts_dir / "write_result.py").write_text(
        """from pathlib import Path
import sys

Path(sys.argv[1]).write_text("skill-script-ok", encoding="utf-8")
print("completed")
""",
        encoding="utf-8",
    )
    (references_dir / "guide.md").write_text("guide", encoding="utf-8")
    assets_dir = skill_dir / "assets"
    assets_dir.mkdir()
    (assets_dir / "template.bin").write_bytes(b"skill-template")
    (agent_home / "skill.json").write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "version": 1,
                "skills": {
                    "demo": {
                        "enabled": enabled,
                        "channels": ["all"],
                        "source": "builtin",
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def skill_context(tmp_path: Path):
    agent_home = tmp_path / "agent"
    project_root = agent_home / "topic-workspaces" / "chat-1"
    project_root.mkdir(parents=True)
    _install_skill(agent_home)
    set_current_workspace_context(
        WorkspaceContext(
            agent_home=agent_home,
            state_root=project_root,
            project_root=project_root,
        ),
    )
    try:
        yield agent_home, project_root
    finally:
        set_current_workspace_context(None)


@pytest.mark.asyncio
async def test_lists_logical_resources_without_exposing_install_path(
    skill_context,
) -> None:
    agent_home, _project_root = skill_context

    response = await list_skill_resources("demo")

    text = response.content[0]["text"]
    assert response.is_error is False
    assert "scripts/write_result.py" in text
    assert "references/guide.md" in text
    assert str(agent_home) not in text
    assert response.metadata["skill_name"] == "demo"
    assert response.metadata["prefix"] == ""
    assert response.metadata["resource_count"] == 4
    assert response.metadata["skill_source"] == "builtin"
    assert response.metadata["skill_resolution_priority"] == 100
    assert len(response.metadata["skill_content_sha256"]) == 64


@pytest.mark.asyncio
async def test_copies_binary_skill_resource_into_topic_workspace(
    skill_context,
) -> None:
    agent_home, project_root = skill_context

    response = await copy_skill_resource(
        "demo",
        "assets/template.bin",
        "inputs/template.bin",
    )

    assert response.is_error is False
    assert response.metadata["skill_name"] == "demo"
    assert response.metadata["resource_path"] == "assets/template.bin"
    assert response.metadata["destination_path"] == "inputs/template.bin"
    assert response.metadata["resource_type"] == "file"
    assert response.metadata["skill_source"] == "builtin"
    assert response.metadata["skill_resolution_priority"] == 100
    assert len(response.metadata["skill_content_sha256"]) == 64
    assert (project_root / "inputs" / "template.bin").read_bytes() == (
        b"skill-template"
    )
    assert str(agent_home) not in response.content[0]["text"]


@pytest.mark.asyncio
async def test_runs_enabled_python_skill_script_in_topic_workspace(
    skill_context,
) -> None:
    _agent_home, project_root = skill_context

    response = await run_skill_script(
        "demo",
        "scripts/write_result.py",
        ["result.txt"],
        timeout=10,
    )

    assert response.is_error is False
    assert response.metadata["exit_code"] == 0
    assert response.metadata["skill_name"] == "demo"
    assert response.metadata["script_path"] == "scripts/write_result.py"
    assert "completed" in response.content[0]["text"]
    assert (project_root / "result.txt").read_text(encoding="utf-8") == (
        "skill-script-ok"
    )


@pytest.mark.asyncio
async def test_skill_script_inherits_trusted_lantai_working_dir(
    skill_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_home, project_root = skill_context
    desktop_working_dir = project_root / "desktop-working"
    monkeypatch.setattr(
        skill_resources_module,
        "WORKING_DIR",
        desktop_working_dir,
    )
    script = agent_home / "skills" / "demo" / "scripts" / "working_dir.py"
    script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path('observed-working-dir.txt').write_text(\n"
        "    os.environ['LANTAI_WORKING_DIR'], encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )

    response = await run_skill_script(
        "demo",
        "scripts/working_dir.py",
        [],
        timeout=10,
    )

    assert response.is_error is False
    assert (project_root / "observed-working-dir.txt").read_text(
        encoding="utf-8",
    ) == str(desktop_working_dir)


@pytest.mark.asyncio
async def test_repairs_legacy_string_arguments_without_using_a_shell(
    skill_context,
) -> None:
    """Weak providers may flatten argv; runtime should recover safely."""
    _agent_home, project_root = skill_context

    response = await run_skill_script(
        "demo",
        "scripts/write_result.py",
        '"result with spaces.txt"',
        timeout=10,
    )

    assert response.is_error is False
    assert response.metadata["arguments_repaired"] == "string_to_array"
    assert (project_root / "result with spaces.txt").read_text(
        encoding="utf-8",
    ) == "skill-script-ok"


@pytest.mark.asyncio
async def test_rejects_disabled_or_escaping_skill_scripts(tmp_path: Path) -> None:
    agent_home = tmp_path / "agent"
    project_root = agent_home / "topic-workspaces" / "chat-1"
    project_root.mkdir(parents=True)
    _install_skill(agent_home, enabled=False)
    set_current_workspace_context(
        WorkspaceContext(
            agent_home=agent_home,
            state_root=project_root,
            project_root=project_root,
        ),
    )
    try:
        disabled = await run_skill_script(
            "demo",
            "scripts/write_result.py",
            ["result.txt"],
        )
        traversal = await run_skill_script(
            "demo",
            "../outside.py",
            [],
        )
    finally:
        set_current_workspace_context(None)

    assert disabled.is_error is True
    assert "not enabled" in disabled.content[0]["text"]
    assert traversal.is_error is True
    assert "scripts/" in traversal.content[0]["text"]


def test_frozen_backend_entry_can_execute_python_skill_script(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = tmp_path / "script.py"
    script.write_text(
        "from pathlib import Path\nPath('bridge.txt').write_text('ok')\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "LANTAI_RUN_SKILL_SCRIPT_PATH": str(script),
    }

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "electron" / "entry.py")],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (workspace / "bridge.txt").read_text(encoding="utf-8") == "ok"


def test_frozen_skill_bridge_does_not_bootstrap_unrelated_persisted_secrets(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "envs.json").write_text(
        json.dumps({"UNRELATED_SECRET": "ENC:not-a-valid-token"}),
        encoding="utf-8",
    )
    script = tmp_path / "script.py"
    script.write_text("import lantai\nprint('skill-ok')\n", encoding="utf-8")
    env = {
        **os.environ,
        "LANTAI_RUN_SKILL_SCRIPT_PATH": str(script),
        "LANTAI_WORKING_DIR": str(tmp_path / "working"),
        "LANTAI_SECRET_DIR": str(secret_dir),
    }

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "electron" / "entry.py")],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "skill-ok"
    assert "Failed to decrypt stored secret" not in result.stderr


def test_frozen_backend_skill_bridge_never_writes_bytecode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script_dir = tmp_path / "skill" / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "helper.py").write_text("VALUE = 'ok'\n", encoding="utf-8")
    script = script_dir / "run.py"
    script.write_text(
        "from helper import VALUE\n"
        "from pathlib import Path\n"
        "Path('bridge-import.txt').write_text(VALUE)\n",
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "PYTHONHOME"}
    }
    env["LANTAI_RUN_SKILL_SCRIPT_PATH"] = str(script)

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "electron" / "entry.py")],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (workspace / "bridge-import.txt").read_text(encoding="utf-8") == "ok"
    assert not (script_dir / "__pycache__").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group evidence")
async def test_cancelled_skill_script_reaps_child_process(skill_context):
    import asyncio
    import signal
    from contextlib import suppress

    agent_home, project = skill_context
    script = agent_home / "skills/demo/scripts/blocked.py"
    script.write_text(
        "import os,time,subprocess,sys\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        "Path('child.pid').write_text(str(child.pid))\n"
        "Path('running.pid').write_text(str(os.getpid()))\ntime.sleep(30)\n",
    )
    task = asyncio.create_task(run_skill_script("demo", "scripts/blocked.py", []))
    pid = None
    try:
        async def ready():
            while not (project / "running.pid").exists():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(ready(), 3)
        pid = int((project / "running.pid").read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        child_pid = int((project / "child.pid").read_text())
        async def reaped():
            for remaining_pid in (pid, child_pid):
                while True:
                    try:
                        os.kill(remaining_pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)
        await asyncio.wait_for(reaped(), 2)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)


async def test_skill_script_stops_on_output_limit(skill_context, monkeypatch):
    from lantai.agents.tools import skill_resources as module
    agent_home, _project = skill_context
    monkeypatch.setattr(module, "_MAX_OUTPUT_CHARS", 1024)
    script = agent_home / "skills/demo/scripts/noisy.py"
    script.write_text("import sys,time\nsys.stdout.write('x'*100000)\nsys.stdout.flush()\ntime.sleep(5)\n")
    response = await run_skill_script("demo", "scripts/noisy.py", [], timeout=1)
    assert response.is_error
    assert "output" in response.content[0]["text"].lower()
    assert "limit" in response.content[0]["text"].lower()


@pytest.mark.parametrize("tool_name,kwargs", [
    ("read_skill", {"skill_name": "demo"}),
    ("list_skill_resources", {"skill_name": "demo"}),
    ("copy_skill_resource", {"skill_name": "demo", "resource_path": "assets/template.bin", "destination_path": "copied.bin"}),
    ("run_skill_script", {"skill_name": "demo", "script_path": "scripts/write_result.py", "arguments": ["channel-result.txt"]}),
])
async def test_registered_skill_tools_honor_request_channel(skill_context, tool_name, kwargs):
    import asyncio
    from types import SimpleNamespace
    from lantai.agents.tool_registry import build_lantai_tool_registry

    agent_home, _project = skill_context
    manifest_path = agent_home / "skill.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["skills"]["demo"]["channels"] = ["feishu"]
    manifest_path.write_text(json.dumps(manifest))
    config = SimpleNamespace(tools=SimpleNamespace(builtin_tools={}))
    registries = [
        build_lantai_tool_registry(
            agent_config=config,
            request_context={"channel": channel, "distribution": "developer_id"},
        )
        for channel in ("feishu", "console")
    ]
    calls = [registry.get_tool(tool_name) for registry in registries]
    assert all(item is not None for item in calls)
    assert "channel" not in calls[0][0].input_schema["properties"]
    allowed, denied = await asyncio.gather(*(item[1](**kwargs) for item in calls))
    assert not allowed.is_error, allowed.content
    assert denied.is_error
