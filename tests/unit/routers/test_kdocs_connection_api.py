from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lantai.app import agent_context
from lantai.app.routers.skills import router


def _write_fake_kdocs_cli(cli_dir: Path, state_path: Path) -> Path:
    cli_dir.mkdir()
    executable = cli_dir / "kdocs-cli"
    executable.write_text(
        f"""#!/bin/sh
if [ "$1" = "version" ]; then
  printf '2.5.7\\n'
  exit 0
fi
if [ "$1" = "auth" ] && [ "$2" = "set-token" ]; then
  touch '{state_path}'
  exit 0
fi
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then
  if [ -f '{state_path}' ]; then
    printf '{{"authenticated":true,"token":"must-not-leak"}}\\n'
  else
    printf '{{"authenticated":false}}\\n'
  fi
  exit 0
fi
if [ "$1" = "auth" ] && [ "$2" = "logout" ]; then
  rm -f '{state_path}'
  exit 0
fi
if [ "$1" = "drive" ] && [ "$2" = "list-my-files" ]; then
  printf '{{"code":0,"msg":"ok","data":{{"items":[]}}}}\n'
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_user_can_bind_kdocs_token_without_it_appearing_in_response(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "kdocs"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: kdocs\ndescription: KDocs integration.\n---\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "authenticated"
    cli_dir = tmp_path / "cli"
    _write_fake_kdocs_cli(cli_dir, state_path)
    monkeypatch.setenv("KDOCS_CLI_DIR", str(cli_dir))

    async def fake_agent_for_request(_request):
        return SimpleNamespace(workspace_dir=str(workspace), agent_id="default")

    monkeypatch.setattr(agent_context, "get_agent_for_request", fake_agent_for_request)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    token = "private-kdocs-token-value"

    response = client.post(
        "/skills/integrations/kdocs/connect",
        json={"token": token},
    )

    assert response.status_code == 200
    assert response.json() == {
        "installed": True,
        "authenticated": True,
        "cli_version": "2.5.7",
        "token_url": "https://www.kdocs.cn/latest",
    }
    assert token not in response.text
    assert "must-not-leak" not in response.text


def test_kdocs_status_uses_packaged_skill_before_workspace_import(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cli_dir = tmp_path / "cli"
    state_path = tmp_path / "authenticated"
    _write_fake_kdocs_cli(cli_dir, state_path)
    monkeypatch.setenv("KDOCS_CLI_DIR", str(cli_dir))

    async def fake_agent_for_request(_request):
        return SimpleNamespace(workspace_dir=str(workspace), agent_id="default")

    monkeypatch.setattr(agent_context, "get_agent_for_request", fake_agent_for_request)
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get("/skills/integrations/kdocs/status")

    assert response.status_code == 200
    assert response.json()["installed"] is True
