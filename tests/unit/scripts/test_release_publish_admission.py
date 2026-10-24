"""Exercise the real publishing function with finite authority/OS/gh doubles.

No repository access, signature acceptance or upload is performed by these
tests. Real final-byte metadata admission executes on synthetic files.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

spec = importlib.util.spec_from_file_location('publish_shell_baseline', Path(__file__).with_name('test_release_chain_boundaries.py'))
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


@pytest.mark.parametrize('identity', [None, '', '../unsafe', 'unsafe\nidentity', '\u4e0d\u5408\u6cd5'])
def test_accepted_notary_requires_a_safe_submission_identity(tmp_path, identity):
    record = tmp_path / 'notary.json'
    baseline.write_json(record, {'status': 'Accepted', 'id': identity})
    result = baseline.run_shell(tmp_path, baseline.source_function('assert_accepted_notary_record') +
        'assert_accepted_notary_record app "$RECORD"\n', {'RECORD': record, 'VENV_PYTHON': sys.executable})
    assert result.returncode != 0, 'Accepted status alone is not a settled identified submission'


@pytest.mark.parametrize('fault', ['none', 'modified-dmg', 'authority-rejected', 'authority-changed', 'signature', 'notary-binding'])
def test_canonical_publisher_admits_exact_snapshot_and_rechecks_authority(tmp_path, fault):
    root = tmp_path / 'release'
    env = baseline.release_fixture(root)
    manifest = json.loads(Path(env['ARTIFACT_MANIFEST']).read_text())
    manifest['signing']['identityClass'] = 'Developer ID Application'
    for item in manifest['notarization']['requestIds']:
        role = item['label'].split('-')[-1]
        item.update(status='Accepted', requestIdSHA256=hashlib.sha256(('synthetic-' + role).encode()).hexdigest())
    if fault == 'notary-binding':
        manifest['notarization']['requestIds'][0]['requestIdSHA256'] = 'f' * 64
    baseline.write_json(Path(env['ARTIFACT_MANIFEST']), manifest)
    if fault == 'modified-dmg':
        next(root.glob('*.dmg')).write_bytes(b'modified after evidence')
    (root / 'unchecked.blockmap').write_bytes(b'must not upload')
    gh = tmp_path / 'finite-gh'
    gh.write_text('''#!/bin/bash
if [[ "$1 $2" == "repo view" ]]; then echo PUBLIC; exit 0; fi
if [[ "$1 $2" == "release view" ]]; then exit 1; fi
if [[ "$1 $2" == "release create" ]]; then printf '%s\\n' "$@" > "$CREATED"; exit 0; fi
exit 9
''')
    gh.chmod(0o700)
    names = ['skip_notarization_requested', 'assert_accepted_notary_record', 'assert_public_release_prerequisites',
             'verify_public_notary_binding', 'publish_github_release']
    source = baseline.SCRIPT.read_text()
    functions = ''.join(baseline.source_function(name) for name in names if name + '() {\n' in source)
    script = functions + '''
find_release_file() { printf '%s/%s\\n' "$RELEASE_DIR" "$1"; }
resolve_github_cli() { return 0; }
audit_public_release_version_lineage() { return 0; }
verify_public_upload_signatures() { [[ "$FAULT" != signature ]]; } # finite OS double
revalidate_deployment_source_authority() {
  printf check >> "$CHECKS"
  [[ "$FAULT" != authority-rejected ]] || return 71
  [[ "$FAULT" != authority-changed || $(wc -c < "$CHECKS") -lt 10 ]] || return 72
}
publish_github_release
'''
    env.update(FAULT=fault, CHECKS=tmp_path / 'authority-checks', CREATED=tmp_path / 'created',
               PUBLISH_GITHUB_RELEASE='1', RELEASE_DIR=root, RELEASE_ARCH='arm64',
               PRODUCT_NAME='Synthetic', GITHUB_RELEASE_REPOSITORY='synthetic/never-accessed', GITHUB_CLI=gh,
               RELEASE_UPLOAD_SCRIPT=baseline.W / 'scripts/check_release_upload.py')
    result = baseline.run_shell(tmp_path, script, env)
    if fault != 'none':
        assert result.returncode != 0, f'publisher admitted {fault}'
        assert not Path(env['CREATED']).exists(), 'invalid bundle reached the upload double'
        return
    assert result.returncode == 0, result.stderr
    assert Path(env['CHECKS']).read_text() == 'checkcheck', 'unique-main/HEAD/clean/no-lease must be fresh at both boundaries'
    lines = Path(env['CREATED']).read_text().splitlines()
    paths = [Path(item) for item in lines if Path(item).is_file()]
    assert len(paths) == 5
    assert {p.name for p in paths} == {p.name for p in root.iterdir() if p.suffix in ('.dmg', '.zip') or p.name.startswith('lantai-')}
    assert len({p.parent for p in paths}) == 1 and paths[0].parent != root
    assert all(p.read_bytes() == (root / p.name).read_bytes() for p in paths)
