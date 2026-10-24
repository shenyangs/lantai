"""Real legacy ZIP restore, child SIGKILL, and a fresh ordinary memory reader."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import textwrap
import zipfile

import pytest


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


def test_killed_group_restore_cannot_expose_one_new_and_one_old_root_to_public_reader(tmp_path):
    working, roots, environment = _fixture(tmp_path)
    script = textwrap.dedent('''
        import importlib, sys, time
        from pathlib import Path
        from lantai.backup.models import RestoreBackupRequest
        restore = importlib.import_module('lantai.backup._ops.restore')
        first = Path(sys.argv[1])
        commit = restore.commit_tmp
        def pause_after_actual_commit(destination):
            commit(destination)
            if destination == first:
                print('READY_GROUP_CRASH', flush=True)
                while True:
                    time.sleep(0.05)
        restore.commit_tmp = pause_after_actual_commit
        restore._restore_sync('fixture', RestoreBackupRequest(
            agent_ids=['first', 'second'], include_global_config=False,
            include_secrets=False, include_skill_pool=False,
        ))
    ''')
    publisher = subprocess.Popen([sys.executable, '-c', script, str(roots[0])],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=environment)
    try:
        ready, _, _ = select.select([publisher.stdout], [], [], 12)
        assert ready, 'synthetic restore did not reach its real first-root commit'
        line = publisher.stdout.readline().strip()
        if line != 'READY_GROUP_CRASH':
            publisher.wait(timeout=3)
            pytest.fail('restore fixture failed before crash boundary: ' + publisher.stderr.read())
        journal = working.parent / ('.' + working.name + '.restore-transaction.json')
        assert json.loads(journal.read_bytes())['phase'] == 'prepared'
        assert (roots[0] / 'MEMORY.md').read_text() == 'new-first'
        assert (roots[1] / 'MEMORY.md').read_text() == 'old-second'
        publisher.kill()
        publisher.wait(timeout=5)
        reader_script = textwrap.dedent('''
            import json, sys
            from pathlib import Path
            from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
            roots = [Path(value) for value in sys.argv[1:]]
            try:
                for root in roots:
                    CanonicalMemoryLedger(root).read_snapshot()
            except RuntimeError as error:
                # Explicit fail-closed blocking is safe until startup or
                # the dedicated restore recovery owner settles the group.
                print(json.dumps({'blocked': True, 'error_type': type(error).__name__,
                                  'error': str(error)}))
            else:
                print(json.dumps({'blocked': False, 'values': [
                    (root / 'MEMORY.md').read_text() for root in roots
                ]}))
        ''')
        reader = subprocess.run([sys.executable, '-c', reader_script, *map(str, roots)],
                                capture_output=True, text=True, env=environment, timeout=12)
        assert reader.returncode == 0, reader.stderr
        result = json.loads(reader.stdout)
        if result['blocked']:
            assert any(word in result['error'].lower() for word in ('restore', 'transaction'))
        assert result['blocked'] or result['values'] in [
            ['old-first', 'old-second'], ['new-first', 'new-second'],
        ], 'ordinary public memory read bypassed an unsettled group restore: ' + repr(result)
    finally:
        if publisher.poll() is None:
            publisher.kill()
        publisher.wait(timeout=5)
        publisher.stdout.close()
        publisher.stderr.close()
