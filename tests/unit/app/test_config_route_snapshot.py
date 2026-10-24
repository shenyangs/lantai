"""Public settings routes must persist edits through the real Workspace.config property."""
import json
import subprocess
import sys
import textwrap

import pytest

import os
import zipfile
from pathlib import Path

def _fixture(tmp_path):
    working = tmp_path / 'working'
    working.mkdir()
    roots = [working / 'workspaces' / aid for aid in ('first', 'second')]
    for aid, root in zip(('first', 'second'), roots):
        root.mkdir(parents=True)
        (root / 'MEMORY.md').write_text('old-' + aid, encoding='utf-8')
    config = {'agents': {'active_agent': 'first', 'agent_order': ['first', 'second'], 'profiles': {
        aid: {'id': aid, 'workspace_dir': str(root)}
        for aid, root in zip(('first', 'second'), roots)
    }}}
    (working / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    backups = tmp_path / 'backups'
    backups.mkdir()
    with zipfile.ZipFile(backups / 'fixture.zip', 'w') as archive:
        archive.writestr('meta.json', json.dumps({
            'id': 'fixture', 'name': 'synthetic legacy restore', 'version': '1',
            'scope': {'include_agents': True, 'include_global_config': False,
                      'include_secrets': False, 'include_skill_pool': False},
        }))
        for aid in ('first', 'second'):
            archive.writestr('data/workspaces/' + aid + '/MEMORY.md', 'new-' + aid)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', LANTAI_SKIP_KEYRING='1',
                       LANTAI_WORKING_DIR=str(working), LANTAI_SECRET_DIR=str(tmp_path / 'secret'),
                       LANTAI_BACKUP_DIR=str(backups))
    return working, roots, environment


@pytest.mark.parametrize('kind', ['heartbeat', 'channels', 'single_channel', 'plan', 'mcp_update', 'mcp_toggle', 'mcp_delete'])
def test_actual_config_routes_acknowledge_only_persisted_workspace_edits(tmp_path, kind):
    working, roots, environment = _fixture(tmp_path)
    (roots[0] / 'agent.json').write_text(json.dumps({
        'id': 'first', 'name': 'First', 'workspace_dir': str(roots[0]),
        'heartbeat': {'enabled': False, 'every': '30m'},
        'channels': {'console': {'enabled': True}},
        'plan': {'enabled': False},
        'mcp': {'clients': {'synthetic': {'name': 'Synthetic', 'enabled': True, 'command': 'synthetic-unused'}}},
    }))
    script = textwrap.dedent('''
        import asyncio, json, sys
        from pathlib import Path
        from unittest.mock import MagicMock
        for dependency in ('aibot', 'lark_oapi'):
            sys.modules.setdefault(dependency, MagicMock())
        from lantai.app.workspace.workspace import Workspace
        from lantai.app import agent_context
        from lantai.app.routers import config as router
        from lantai.app.routers import plan as plan_router
        from lantai.app.routers import mcp as mcp_router
        from lantai.app.routers.schemas_config import HeartbeatBody
        from lantai.config.config import ChannelConfig, ConsoleConfig
        root, kind = Path(sys.argv[1]), sys.argv[2]
        workspace = Workspace('first', str(root))
        async def actual_workspace(_request):
            return workspace
        # Resolve the request to a real, unstarted Workspace. No Provider
        # or background service is needed to exercise its config property.
        agent_context.get_agent_for_request = actual_workspace
        plan_router._get_workspace = actual_workspace
        router.schedule_agent_reload = lambda *_args: None
        mcp_router.schedule_agent_reload = lambda *_args: None
        async def run():
            if kind == 'heartbeat':
                acknowledged = await router.put_heartbeat(None, HeartbeatBody(
                    enabled=True, every='47m', target='last',
                ))
                await asyncio.sleep(0)
                expected = {'enabled': True, 'every': '47m'}
            elif kind == 'channels':
                acknowledged = await router.put_channels(None, ChannelConfig(console=ConsoleConfig(enabled=False)))
                expected = {'enabled': False}
            elif kind == 'single_channel':
                acknowledged = await router.put_channel(
                    request=None, channel_name='console', single_channel_config={'enabled': False},
                )
                expected = {'enabled': False}
            elif kind == 'mcp_update':
                acknowledged = await mcp_router.update_mcp_client(None, 'synthetic',
                    mcp_router.MCPClientUpdateRequest(enabled=False))
                expected = {'enabled': False, 'revision': 1}
            elif kind == 'mcp_toggle':
                acknowledged = await mcp_router.toggle_mcp_client(None, 'synthetic')
                expected = {'enabled': False, 'revision': 1}
            elif kind == 'mcp_delete':
                acknowledged = await mcp_router.delete_mcp_client(None, 'synthetic')
                expected = {'present': False}
            else:
                from lantai.plan.schemas import PlanConfigResponse
                acknowledged = await plan_router.put_plan_config(None, PlanConfigResponse(enabled=True))
                expected = {'enabled': True}
            raw = json.loads((root / 'agent.json').read_bytes())
            stored = raw['heartbeat'] if kind == 'heartbeat' else (
                raw['plan'] if kind == 'plan' else raw['channels']['console']
            )
            if kind.startswith('mcp_'):
                clients = raw['mcp']['clients']
                stored = {'present': 'synthetic' in clients} if kind == 'mcp_delete' else clients['synthetic']
            print(json.dumps({'stored': stored, 'expected': expected, 'kind': kind}))
        asyncio.run(run())
    ''')
    completed = subprocess.run([sys.executable, '-c', script, str(roots[0]), kind],
                               capture_output=True, text=True, env=environment, timeout=15)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert all(result['stored'].get(key) == value for key, value in result['expected'].items()), (
        'actual settings route acknowledged success but the real Workspace.config reread discarded its edit: '
        + repr(result)
    )
