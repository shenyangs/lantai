"""Real public adoption with faults at durable decision and rollback boundaries."""
import json
from pathlib import Path

import pytest

from lantai.agents.memory import memory_pack
from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.locking import memory_transaction_journal
from lantai.utils import authoritative_store as authority
from lantai.utils import path_transaction as transaction


def _seed(tmp_path):
    target, source = tmp_path / 'target', tmp_path / 'source'
    for root, value in ((target, 'old'), (source, 'new')):
        root.mkdir()
        (root / 'MEMORY.md').write_text(value + '-root', encoding='utf-8')
        directory = root / 'user-understanding'
        directory.mkdir()
        # Valid empty projection and a regular portable category file. Both
        # are copied by the real pack exporter and replaced by adoption.
        (directory / 'projection.json').write_text('{"version":1,"atoms":[]}', encoding='utf-8')
        (directory / 'fixture.txt').write_text(value + '-understanding', encoding='utf-8')
    pack = tmp_path / 'new-memory.zip'
    memory_pack.export_memory_pack(source, pack, agent_id='fixture')
    return target, pack


def _values(target):
    return (
        (target / 'MEMORY.md').read_text(encoding='utf-8'),
        (target / 'user-understanding' / 'fixture.txt').read_text(encoding='utf-8'),
    )


def _adopt(target, pack):
    return memory_pack.adopt_memory_pack(
        pack, target, agent_id='fixture', dry_run=False,
        confirmation=memory_pack.MEMORY_PACK_ADOPT_CONFIRMATION,
    )


def test_published_commit_receipt_cannot_drive_cleanup_of_partly_rolled_back_roots(tmp_path, monkeypatch):
    target, pack = _seed(tmp_path)
    journal = memory_transaction_journal(target)
    original_fsync, original_remove = authority.fsync_directory, transaction._remove
    failures = {'commit_fsync': False, 'rollback_remove': False}

    def fail_committed_journal_fsync(path):
        if Path(path) == target and journal.exists():
            state = json.loads(journal.read_bytes())
            if state['phase'] == 'committed' and not failures['commit_fsync']:
                failures['commit_fsync'] = True
                # atomic_write_bytes has actually replaced the journal; this
                # simulates its real final directory fsync raising EIO.
                raise OSError('fixture committed journal directory fsync failure')
        return original_fsync(path)

    def fail_one_rollback_remove(path):
        if failures['commit_fsync'] and Path(path) == target / 'MEMORY.md' and not failures['rollback_remove']:
            failures['rollback_remove'] = True
            raise OSError('fixture early-root rollback removal failure')
        return original_remove(path)

    with monkeypatch.context() as faults:
        faults.setattr(authority, 'fsync_directory', fail_committed_journal_fsync)
        faults.setattr(transaction, '_remove', fail_one_rollback_remove)
        try:
            report = _adopt(target, pack)
        except OSError:
            pass
        else:
            assert report.adopted is True
    assert failures['commit_fsync'], 'fixture did not reach the actual commit publication'
    # A fresh ordinary public read performs the implemented crash recovery.
    # Once that read is allowed, every category must reflect one decision.
    CanonicalMemoryLedger(target).read_snapshot()
    assert _values(target) in {
        ('old-root', 'old-understanding'), ('new-root', 'new-understanding'),
    }, 'commit recovery erased rollback copies and exposed a mixed old/new memory state'


def test_partial_rollback_can_be_retried_after_all_transient_faults_clear(tmp_path, monkeypatch):
    target, pack = _seed(tmp_path)
    journal = memory_transaction_journal(target)
    original_snapshot, original_remove = CanonicalMemoryLedger.read_snapshot, transaction._remove
    failures = {'validation': False, 'rollback_remove': False}

    def fail_real_final_validation(self):
        if self.working_dir == target and journal.exists() and not failures['validation']:
            if _values(target) == ('new-root', 'new-understanding'):
                failures['validation'] = True
                raise OSError('fixture final snapshot validation failure')
        return original_snapshot(self)

    def fail_one_rollback_remove(path):
        if failures['validation'] and Path(path) == target / 'MEMORY.md' and not failures['rollback_remove']:
            failures['rollback_remove'] = True
            raise OSError('fixture early-root rollback removal failure')
        return original_remove(path)

    with monkeypatch.context() as faults:
        faults.setattr(CanonicalMemoryLedger, 'read_snapshot', fail_real_final_validation)
        faults.setattr(transaction, '_remove', fail_one_rollback_remove)
        with pytest.raises(OSError):
            _adopt(target, pack)
    assert all(failures.values()), 'fixture did not reach the real rollback boundary'
    assert journal.exists(), 'unsettled rollback must retain its durable owner'
    # Recovery must recognize categories already restored by the first pass;
    # loss of their staging paths in finally does not mean old data is gone.
    CanonicalMemoryLedger(target).read_snapshot()
    assert _values(target) == ('old-root', 'old-understanding')
    assert not journal.exists()


"""Memory adoption and public negative decisions use one stable process lock."""
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time
import pytest
from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.memory_pack import export_memory_pack

ADOPTER = r'''
from pathlib import Path
import sys, time
from lantai.agents.memory import memory_pack
root, pack, release = map(Path, sys.argv[1:4])
export = memory_pack.export_memory_pack
def held_export(working_dir, destination, *, agent_id):
    if Path(working_dir).resolve() == root.resolve():
        print('READY_ADOPTION', flush=True)
        deadline = time.monotonic() + 15
        while not release.exists():
            if time.monotonic() > deadline:
                raise RuntimeError('test release barrier timed out')
            time.sleep(0.01)
    return export(working_dir, destination, agent_id=agent_id)
memory_pack.export_memory_pack = held_export
report = memory_pack.adopt_memory_pack(pack, root, agent_id='fixture', dry_run=False,
    confirmation=memory_pack.MEMORY_PACK_ADOPT_CONFIRMATION)
assert report.adopted is True
print('COMPLETED_ADOPTION', flush=True)
'''
NEGATIVE_WRITER = r'''
from pathlib import Path
import sys
from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
root, decision, fact_id = sys.argv[1:4]
ledger = CanonicalMemoryLedger(Path(root))
print('READY_NEGATIVE', flush=True)
result = ledger.forget(fact_id) if decision == 'forget' else ledger.delete_source_event('source')
assert result is not None
if decision == 'delete_source':
    assert result['deleted'] is True
else:
    assert result is True
    assert ledger.get_fact(fact_id).status == 'forgotten'
print('COMPLETED_NEGATIVE', flush=True)
'''


def _line(process, timeout=8):
    deadline = time.monotonic() + timeout
    value = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, 'child did not acknowledge its test barrier'
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        assert readable, 'child did not acknowledge its test barrier'
        char = os.read(process.stdout.fileno(), 1)
        assert char, 'child exited before acknowledging its test barrier'
        if char == b'\n':
            return value.decode()
        value.extend(char)


def _start(script, *arguments):
    return subprocess.Popen([sys.executable, '-c', script, *map(str, arguments)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy(), bufsize=0)


@pytest.mark.parametrize('decision', ['forget', 'delete_source'])
def test_another_process_waits_for_memory_adoption_then_keeps_its_negative_decision(tmp_path, decision):
    target = tmp_path / 'target'
    target.mkdir()
    local = CanonicalMemoryLedger(target)
    event = local.record_event('synthetic source evidence', event_id='source', session_id='fixture-session')
    fact = local.capture_fact('synthetic source evidence', memory_type='style', event=event, explicit_user_request=True)
    assert fact is not None and fact.status == 'confirmed'
    source = tmp_path / 'source'
    shutil.copytree(target, source)
    pack = tmp_path / 'old-memory.zip'
    export_memory_pack(source, pack, agent_id='fixture')
    release = tmp_path / 'release-adoption'
    adopter = _start(ADOPTER, target, pack, release)
    writer = None
    try:
        assert _line(adopter) == 'READY_ADOPTION'
        writer = _start(NEGATIVE_WRITER, target, decision, fact.id)
        assert _line(writer) == 'READY_NEGATIVE'
        readable, _, _ = select.select([writer.stdout], [], [], 0.2)
        assert not readable and writer.poll() is None, 'negative writer bypassed active adoption process lock'
        release.touch()
        adopter_out, adopter_err = adopter.communicate(timeout=10)
        writer_out, writer_err = writer.communicate(timeout=10)
        assert adopter.returncode == 0, adopter_err.decode()
        assert writer.returncode == 0, writer_err.decode()
        assert b'COMPLETED_ADOPTION' in adopter_out
        assert b'COMPLETED_NEGATIVE' in writer_out
    finally:
        release.touch(exist_ok=True)
        for process in (adopter, writer):
            if process is None:
                continue
            if process.poll() is None:
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=2)
    current = CanonicalMemoryLedger(target)
    assert current.confirmed_facts() == []
    if decision == 'forget':
        assert current.get_fact(fact.id).status == 'forgotten'
    else:
        assert current.list_events() == []
        assert current.record_event('synthetic source evidence', event_id='source') is None


"""Directory adoption recovery may not trust damaged decision records."""
import json
from pathlib import Path
import select
import subprocess
import sys
import textwrap

import pytest

from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.locking import memory_transaction_journal


@pytest.mark.parametrize('damage', ['committed', 'missing_first'])
def test_adoption_journal_damage_cannot_expose_mixed_category_generation(tmp_path, damage):
    target, pack = _seed(tmp_path)
    script = textwrap.dedent('''
        import sys, time
        from pathlib import Path
        from lantai.agents.memory import memory_pack
        root, pack = Path(sys.argv[1]), Path(sys.argv[2])
        rename = Path.rename
        def pause_after_actual_rename(path, destination):
            result = rename(path, destination)
            if Path(destination) == root / 'MEMORY.md':
                print('READY_ADOPTION_JOURNAL_GUARD', flush=True)
                while True:
                    time.sleep(0.05)
            return result
        Path.rename = pause_after_actual_rename
        memory_pack.adopt_memory_pack(pack, root, agent_id='fixture', dry_run=False,
            confirmation=memory_pack.MEMORY_PACK_ADOPT_CONFIRMATION)
    ''')
    publisher = subprocess.Popen([sys.executable, '-c', script, str(target), str(pack)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([publisher.stdout], [], [], 12)
        assert ready, 'real adoption did not reach its first completed category promotion'
        line = publisher.stdout.readline().strip()
        if line != 'READY_ADOPTION_JOURNAL_GUARD':
            publisher.wait(timeout=3)
            pytest.fail('adoption failed before damage boundary: ' + publisher.stderr.read())
        publisher.kill()
        publisher.wait(timeout=5)
        journal_path = memory_transaction_journal(target)
        journal = json.loads(journal_path.read_bytes())
        assert journal['phase'] == 'prepared'
        assert _values(target) == ('new-root', 'old-understanding')
        if damage == 'committed':
            journal['phase'] = 'committed'
        else:
            journal['paths'] = [record for record in journal['paths']
                                if record['destination'] != str(target / 'MEMORY.md')]
        journal_path.write_text(json.dumps(journal))
        try:
            CanonicalMemoryLedger(target).read_snapshot()
        except RuntimeError as error:
            assert 'transaction' in str(error).lower(), str(error)
        else:
            assert _values(target) in {
                ('old-root', 'old-understanding'), ('new-root', 'new-understanding'),
            }, 'ordinary read accepted mixed memory categories from damaged journal'
    finally:
        if publisher.poll() is None:
            publisher.kill()
        publisher.wait(timeout=5)
        publisher.stdout.close()
        publisher.stderr.close()


"""Real adoption publication must retain its exact recovery owner after SIGKILL."""
import hashlib
import json
import select
import subprocess
import sys
import textwrap

import pytest

from lantai.agents.memory.locking import memory_transaction_journal


@pytest.mark.parametrize('boundary,damage', [
    ('first', None), ('committed', None), ('settled', None),
    ('first', 'missing'), ('first', 'truncated'), ('first', 'older_valid'),
])
def test_memory_owner_publication_recovers_only_its_identified_candidate(tmp_path, boundary, damage):
    target, pack = _seed(tmp_path)
    journal = memory_transaction_journal(target)
    owner = journal.with_name(journal.name + '.owner.json')
    script = textwrap.dedent('''
        import json, sys, time
        from pathlib import Path
        from lantai.agents.memory import memory_pack
        from lantai.utils import authoritative_store as authority
        root, pack, owner, boundary = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
        original = authority.atomic_write_bytes
        def pause_after_primary(path, value):
            result = original(path, value)
            if path == owner:
                state = json.loads(value)
                reached = (
                    boundary == 'first' and state['phase'] == 'active'
                    or boundary == 'committed' and state['phase'] == 'active'
                        and state['transaction']['phase'] == 'committed'
                    or boundary == 'settled' and state['phase'] == 'settled'
                )
                if reached:
                    print('READY_MEMORY_OWNER_PUBLICATION', flush=True)
                    while True:
                        time.sleep(0.05)
            return result
        authority.atomic_write_bytes = pause_after_primary
        memory_pack.adopt_memory_pack(pack, root, agent_id='fixture', dry_run=False,
            confirmation=memory_pack.MEMORY_PACK_ADOPT_CONFIRMATION)
    ''')
    publisher = subprocess.Popen(
        [sys.executable, '-c', script, str(target), str(pack), str(owner), boundary],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        ready, _, _ = select.select([publisher.stdout], [], [], 12)
        assert ready, 'real adoption never reached its owner primary publication'
        line = publisher.stdout.readline().strip()
        if line != 'READY_MEMORY_OWNER_PUBLICATION':
            publisher.wait(timeout=3)
            pytest.fail('fixture failed before owner publication: ' + publisher.stderr.read())
        publisher.kill()
        publisher.wait(timeout=5)
        candidate = owner.with_name(owner.name + '.last-good')
        before = owner.read_bytes()
        pending = owner.with_name(owner.name + '.pending.json')
        intent = json.loads(pending.read_bytes())
        assert intent['operation'] == 'write'
        assert intent['sha256'] == hashlib.sha256(candidate.read_bytes()).hexdigest()
        assert candidate.read_bytes() == before
        if damage == 'missing':
            candidate.unlink()
        elif damage == 'truncated':
            candidate.write_bytes(b'{')
        elif damage == 'older_valid':
            candidate.write_bytes(b'{"version":1,"phase":"settled"}')
        reader_script = textwrap.dedent('''
            import json, sys
            from pathlib import Path
            from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
            try:
                CanonicalMemoryLedger(Path(sys.argv[1])).read_snapshot()
            except RuntimeError as error:
                print(json.dumps({'blocked': True, 'type': type(error).__name__}))
            else:
                print(json.dumps({'blocked': False}))
        ''')
        reader = subprocess.run([sys.executable, '-c', reader_script, str(target)],
                                capture_output=True, text=True, timeout=12)
        assert reader.returncode == 0, reader.stderr
        assert json.loads(reader.stdout)['blocked'] is (damage is not None)
        if damage is not None:
            assert owner.read_bytes() == before
            assert pending.exists(), 'unidentified candidate lost its publication barrier'
            assert _values(target) == ('old-root', 'old-understanding')
        else:
            expected = ('old-root', 'old-understanding') if boundary == 'first' else (
                'new-root', 'new-understanding')
            assert _values(target) == expected
            assert not pending.exists()
            assert not journal.exists()
    finally:
        if publisher.poll() is None:
            publisher.kill()
        publisher.wait(timeout=5)
        publisher.stdout.close()
        publisher.stderr.close()
