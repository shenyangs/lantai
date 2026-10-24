"""An acknowledged public config update may not be erased by restore rollback."""
import json
import select
from pathlib import Path
import subprocess
import sys
import textwrap
import zipfile

import pytest

import os
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


@pytest.mark.parametrize('kind', ['global_config', 'agent_config'])
def test_restore_config_rollback_cannot_erase_an_acknowledged_concurrent_update(tmp_path, kind):
    working, roots, environment = _fixture(tmp_path)
    config = working / 'config.json'
    if kind == 'agent_config':
        with zipfile.ZipFile(Path(environment['LANTAI_BACKUP_DIR']) / 'fixture.zip', 'a') as archive:
            for aid, root in zip(('first', 'second'), roots):
                (root / 'agent.json').write_text(json.dumps({
                    'id': aid, 'name': aid.title(), 'workspace_dir': str(root), 'description': 'old description',
                }))
                archive.writestr('data/workspaces/' + aid + '/agent.json', json.dumps({
                    'id': aid, 'name': aid.title(), 'workspace_dir': '/synthetic-old-host/' + aid,
                    'description': 'restored description',
                }))
    observed = config if kind == 'global_config' else roots[0] / 'agent.json'
    def acknowledged_value():
        value = json.loads(observed.read_bytes())
        return value.get('last_api', {}).get('port') if kind == 'global_config' else value.get('description')
    expected = 54321 if kind == 'global_config' else 'acknowledged-user-update'
    publisher_script = textwrap.dedent('''
        import importlib, json, sys
        from pathlib import Path
        from lantai.backup.models import RestoreBackupRequest
        restore = importlib.import_module('lantai.backup._ops.restore')
        config = Path(sys.argv[1])
        rename, dump = Path.rename, json.dump
        reached = False
        def hold_after_config_promotion(path, destination):
            global reached
            result = rename(path, destination)
            if Path(destination) == config and str(path).endswith('.restore-config.tmp'):
                reached = True
                print('READY_RESTORE_CONFIG_PROMOTED', flush=True)
                assert sys.stdin.readline().strip() == 'RELEASE'
            return result
        def fail_actual_config_finalization(value, handle, *args, **kwargs):
            if reached and Path(handle.name) == config:
                handle.write('{')
                handle.flush()
                raise OSError('synthetic required config write failure')
            return dump(value, handle, *args, **kwargs)
        Path.rename, json.dump = hold_after_config_promotion, fail_actual_config_finalization
        try:
            restore._restore_sync('fixture', RestoreBackupRequest(
                agent_ids=['first', 'second'], include_global_config=False,
                include_secrets=False, include_skill_pool=False,
            ))
        except OSError:
            print('RESTORE_ROLLED_BACK', flush=True)
        else:
            raise AssertionError('fixture did not inject the actual final config write fault')
    ''')
    writer_script = textwrap.dedent('''
        import json, sys
        from lantai.config.utils import load_config, save_config
        from lantai.config.config import load_agent_config, save_agent_config
        from lantai.security.mcp_credentials import protect_agent_mcp_credentials
        kind = sys.argv[1]
        config = load_config() if kind == 'global_config' else load_agent_config('first')
        print('READY_CONFIG_WRITER', flush=True)
        assert sys.stdin.readline().strip() == 'SAVE'
        if kind == 'global_config':
            config.last_api.port = 54321
            save_config(config)
        else:
            config.description = 'acknowledged-user-update'
            save_agent_config('first', config)
        print('CONFIG_UPDATE_ACKNOWLEDGED', flush=True)
    ''')
    publisher = None
    writer = subprocess.Popen([sys.executable, '-c', writer_script, kind],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=environment)
    try:
        ready, _, _ = select.select([writer.stdout], [], [], 12)
        assert ready, 'config writer never completed its actual initial public load'
        if writer.stdout.readline().strip() != 'READY_CONFIG_WRITER':
            writer.wait(timeout=3)
            pytest.fail('fixture failed before the initial writer barrier: ' + writer.stderr.read())
        publisher = subprocess.Popen([sys.executable, '-c', publisher_script, str(config)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=environment)
        ready, _, _ = select.select([publisher.stdout], [], [], 12)
        assert ready, 'real restore never reached its completed config promotion'
        if publisher.stdout.readline().strip() != 'READY_RESTORE_CONFIG_PROMOTED':
            publisher.wait(timeout=3)
            pytest.fail('fixture failed before writer barrier: ' + publisher.stderr.read())
        assert [(root / 'MEMORY.md').read_text() for root in roots] == ['new-first', 'new-second']
        writer.stdin.write('SAVE\n')
        writer.stdin.flush()
        # Correct admission waits until rollback. An unfenced writer can
        # acknowledge before release, which the real rollback then erases.
        ready, _, _ = select.select([writer.stdout], [], [], 0.6)
        acknowledged_before_release = bool(ready)
        if acknowledged_before_release:
            assert writer.stdout.readline().strip() == 'CONFIG_UPDATE_ACKNOWLEDGED'
            assert acknowledged_value() == expected
        publisher.stdin.write('RELEASE\n')
        publisher.stdin.flush()
        restored_output, restored_error = publisher.communicate(timeout=12)
        assert publisher.returncode == 0, restored_error
        assert 'RESTORE_ROLLED_BACK' in restored_output
        writer_output, writer_error = writer.communicate(timeout=12)
        assert writer.returncode == 0, writer_error
        if not acknowledged_before_release:
            assert 'CONFIG_UPDATE_ACKNOWLEDGED' in writer_output, writer_error
        assert [(root / 'MEMORY.md').read_text() for root in roots] == ['old-first', 'old-second']
        assert acknowledged_value() == expected, (
            'restore rollback erased an acknowledged public config update; '
            f'writer acknowledged before release: {acknowledged_before_release}'
        )
    finally:
        for child in (publisher, writer):
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                for pipe in (child.stdin, child.stdout, child.stderr):
                    if pipe is not None:
                        pipe.close()


def test_preloaded_global_settings_save_after_successful_restore_retains_new_agent_profile(tmp_path):
    working, roots, environment = _fixture(tmp_path)
    archive_path = Path(environment['LANTAI_BACKUP_DIR']) / 'fixture.zip'
    with zipfile.ZipFile(archive_path, 'a') as archive:
        archive.writestr('data/workspaces/third/MEMORY.md', 'new-third')
        archive.writestr('data/workspaces/third/agent.json', json.dumps({
            'id': 'third', 'name': 'Third', 'workspace_dir': '/synthetic-old-host/third',
        }))
    writer_script = textwrap.dedent('''
        import sys
        from lantai.config.utils import load_config, save_config
        config = load_config()
        assert set(config.agents.profiles) == {'first', 'second'}
        print('READY_CONFIG_WRITER', flush=True)
        assert sys.stdin.readline().strip() == 'SAVE'
        config.last_api.port = 54321
        save_config(config)
        print('CONFIG_UPDATE_ACKNOWLEDGED', flush=True)
    ''')
    restore_script = textwrap.dedent('''
        import importlib
        from lantai.backup.models import RestoreBackupRequest
        restore = importlib.import_module('lantai.backup._ops.restore')
        restore._restore_sync('fixture', RestoreBackupRequest(
            agent_ids=['first', 'second', 'third'], include_global_config=False,
            include_secrets=False, include_skill_pool=False,
        ))
        print('RESTORE_COMPLETED', flush=True)
    ''')
    writer = subprocess.Popen([sys.executable, '-c', writer_script],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=environment)
    try:
        ready, _, _ = select.select([writer.stdout], [], [], 12)
        assert ready, 'preloaded settings process never reached its public load'
        if writer.stdout.readline().strip() != 'READY_CONFIG_WRITER':
            writer.wait(timeout=3)
            pytest.fail('fixture failed before the initial writer barrier: ' + writer.stderr.read())
        restored = subprocess.run([sys.executable, '-c', restore_script],
                                  capture_output=True, text=True, env=environment, timeout=15)
        assert restored.returncode == 0 and 'RESTORE_COMPLETED' in restored.stdout, restored.stderr
        assert [(root / 'MEMORY.md').read_text() for root in roots] == ['new-first', 'new-second']
        third = working / 'workspaces' / 'third'
        before_save = json.loads((working / 'config.json').read_bytes())
        assert before_save['agents']['profiles']['third']['workspace_dir'] == str(third)
        assert json.loads((third / 'agent.json').read_bytes())['workspace_dir'] == str(third)
        writer.stdin.write('SAVE\n')
        writer.stdin.flush()
        output, error = writer.communicate(timeout=12)
        assert writer.returncode == 0 and 'CONFIG_UPDATE_ACKNOWLEDGED' in output, error
        after_save = json.loads((working / 'config.json').read_bytes())
        assert after_save['last_api']['port'] == 54321
        assert after_save['agents']['profiles'] == before_save['agents']['profiles'], (
            'saving an unrelated setting from a preloaded Config erased the completed restore profiles'
        )
        assert (third / 'MEMORY.md').read_text() == 'new-third'
    finally:
        if writer.poll() is None:
            writer.kill()
        writer.wait(timeout=5)
        for pipe in (writer.stdin, writer.stdout, writer.stderr):
            pipe.close()


def test_preloaded_agent_settings_save_after_successful_restore_retains_new_fields(tmp_path):
    working, roots, environment = _fixture(tmp_path)
    with zipfile.ZipFile(Path(environment['LANTAI_BACKUP_DIR']) / 'fixture.zip', 'a') as archive:
        for aid, root in zip(('first', 'second'), roots):
            (root / 'agent.json').write_text(json.dumps({
                'id': aid, 'name': aid.title(), 'workspace_dir': str(root), 'description': 'old description',
            }))
            archive.writestr('data/workspaces/' + aid + '/agent.json', json.dumps({
                'id': aid, 'name': aid.title() + ' restored', 'workspace_dir': '/synthetic-old-host/' + aid,
                'description': 'restored description',
            }))
    writer_script = textwrap.dedent('''
        import sys
        from lantai.config.config import load_agent_config, save_agent_config
        from lantai.security.mcp_credentials import protect_agent_mcp_credentials
        config = load_agent_config('first')
        assert config.name == 'First'
        print('READY_CONFIG_WRITER', flush=True)
        assert sys.stdin.readline().strip() == 'SAVE'
        config.description = 'acknowledged-user-update'
        save_agent_config('first', config)
        print('CONFIG_UPDATE_ACKNOWLEDGED', flush=True)
    ''')
    restore_script = textwrap.dedent('''
        import importlib
        from lantai.backup.models import RestoreBackupRequest
        restore = importlib.import_module('lantai.backup._ops.restore')
        restore._restore_sync('fixture', RestoreBackupRequest(
            agent_ids=['first', 'second'], include_global_config=False,
            include_secrets=False, include_skill_pool=False,
        ))
        print('RESTORE_COMPLETED', flush=True)
    ''')
    writer = subprocess.Popen([sys.executable, '-c', writer_script],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=environment)
    try:
        ready, _, _ = select.select([writer.stdout], [], [], 12)
        assert ready, 'preloaded agent settings process never reached its public load'
        if writer.stdout.readline().strip() != 'READY_CONFIG_WRITER':
            writer.wait(timeout=3)
            pytest.fail('fixture failed before the initial writer barrier: ' + writer.stderr.read())
        restored = subprocess.run([sys.executable, '-c', restore_script],
                                  capture_output=True, text=True, env=environment, timeout=15)
        assert restored.returncode == 0 and 'RESTORE_COMPLETED' in restored.stdout, restored.stderr
        before_save = json.loads((roots[0] / 'agent.json').read_bytes())
        assert before_save['name'] == 'First restored'
        assert before_save['workspace_dir'] == str(roots[0])
        writer.stdin.write('SAVE\n')
        writer.stdin.flush()
        output, error = writer.communicate(timeout=12)
        assert writer.returncode == 0 and 'CONFIG_UPDATE_ACKNOWLEDGED' in output, error
        after_save = json.loads((roots[0] / 'agent.json').read_bytes())
        assert after_save['description'] == 'acknowledged-user-update'
        assert after_save['name'] == before_save['name'], (
            'saving one agent setting from a preloaded AgentProfileConfig erased completed restore fields'
        )
        assert after_save['workspace_dir'] == before_save['workspace_dir']
    finally:
        if writer.poll() is None:
            writer.kill()
        writer.wait(timeout=5)
        for pipe in (writer.stdin, writer.stdout, writer.stderr):
            pipe.close()
