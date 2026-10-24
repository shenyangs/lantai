"""Real ZIP restore finalization faults, using only disposable synthetic data."""
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import zipfile

import pytest

import os

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


@pytest.mark.parametrize('boundary', ['workspace_rewrite', 'config_save', 'key_then_config_save'])
def test_required_restore_finalization_cannot_silently_commit_incomplete_state(tmp_path, boundary):
    working, roots, environment = _fixture(tmp_path)
    config_before = (working / 'config.json').read_bytes()
    archive_path = Path(environment['LANTAI_BACKUP_DIR']) / 'fixture.zip'
    with zipfile.ZipFile(archive_path, 'a') as archive:
        for aid in ('first', 'second'):
            archive.writestr('data/workspaces/' + aid + '/agent.json', json.dumps({
                'id': aid, 'workspace_dir': '/synthetic-old-host/' + aid,
            }))
        if boundary == 'key_then_config_save':
            # Deterministic disposable test keys; no user store or credentials.
            archive.writestr('data/secrets/.master_key', '22' * 32)
    if boundary == 'key_then_config_save':
        secret = Path(environment['LANTAI_SECRET_DIR'])
        secret.mkdir()
        (secret / '.master_key').write_text('11' * 32)
        # Rewrite the legal legacy scope instead of adding a duplicate meta.
        with zipfile.ZipFile(archive_path) as source:
            members = [(info.filename, source.read(info)) for info in source.infolist()]
        with zipfile.ZipFile(archive_path, 'w') as archive:
            for name, data in members:
                if name == 'meta.json':
                    meta = json.loads(data)
                    meta['scope']['include_secrets'] = True
                    data = json.dumps(meta).encode()
                archive.writestr(name, data)
    script = textwrap.dedent('''
        import importlib, json, os, sys
        from pathlib import Path
        from lantai.backup.models import RestoreBackupRequest
        from lantai.security import secret_store
        restore = importlib.import_module('lantai.backup._ops.restore')
        working, boundary = Path(sys.argv[1]), sys.argv[2]
        roots = [working / 'workspaces' / aid for aid in ('first', 'second')]
        config = working / 'config.json'
        original_replace, original_dump = os.replace, json.dump
        injected = False
        original_key = secret_store._get_master_key() if boundary == 'key_then_config_save' else None
        old_cipher = secret_store.encrypt('disposable test value') if original_key is not None else None
        def fault_replace(source, destination):
            global injected
            if boundary == 'workspace_rewrite' and Path(destination) == roots[0] / 'agent.json':
                injected = True
                raise OSError('synthetic required workspace finalization I/O fault')
            return original_replace(source, destination)
        def fault_dump(value, handle, *args, **kwargs):
            global injected
            if boundary != 'workspace_rewrite' and Path(handle.name) == config:
                if original_key is not None:
                    assert secret_store._get_master_key() != original_key
                injected = True
                handle.write('{')
                handle.flush()
                raise OSError('synthetic required config finalization I/O fault')
            return original_dump(value, handle, *args, **kwargs)
        os.replace, json.dump = fault_replace, fault_dump
        raised = False
        try:
            restore._restore_sync('fixture', RestoreBackupRequest(
                agent_ids=['first', 'second'], include_global_config=False,
                include_secrets=boundary == 'key_then_config_save', include_skill_pool=False,
            ))
        except OSError:
            raised = True
        result = {'injected': injected, 'raised': raised,
                  'values': [(root / 'MEMORY.md').read_text() for root in roots],
                  'config_bytes': config.read_text()}
        if original_key is not None:
            result['old_key_restored'] = secret_store._get_master_key() == original_key
            result['old_cipher_decrypts'] = secret_store.decrypt(old_cipher) == 'disposable test value'
        if not raised:
            result['workspace_paths_correct'] = all(
                json.loads((root / 'agent.json').read_bytes())['workspace_dir'] == str(root)
                for root in roots
            )
        print(json.dumps(result))
    ''')
    restored = subprocess.run([sys.executable, '-c', script, str(working), boundary],
                              capture_output=True, text=True, env=environment, timeout=12)
    assert restored.returncode == 0, restored.stderr
    result = json.loads(restored.stdout)
    assert result['injected'], 'fixture never reached the real required finalization write'
    if result['raised']:
        assert result['values'] == ['old-first', 'old-second']
        assert result['config_bytes'].encode() == config_before
        if boundary == 'key_then_config_save':
            assert result['old_key_restored'] and result['old_cipher_decrypts']
    else:
        assert result['workspace_paths_correct'], (
            'restore reported completion despite a failed required cross-host workspace rewrite'
        )

