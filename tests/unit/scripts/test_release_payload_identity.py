"""Actual upload payload checks with finite OS doubles; no signing or mounts."""
import importlib.util
import json
from pathlib import Path
import plistlib
import sys

import pytest

from scripts.release_build_identity import identity_digest

spec = importlib.util.spec_from_file_location(
    'payload_shell_baseline', Path(__file__).with_name('test_release_chain_boundaries.py'))
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


def identity(*, candidate=False, sha=baseline.EXPECTED_SHA, version=baseline.VERSION,
             build_time='2026-10-04T00:00:00Z'):
    return {'schemaVersion': 1, 'gitSha': sha, 'version': version,
            'sourceBranch': 'DETACHED' if candidate else 'main',
            'canonicalMain': not candidate, 'artifactOnly': candidate,
            'candidateId': 'synthetic-candidate' if candidate else None,
            'buildTime': build_time}


def app(path, payload_identity):
    executable = path / 'Contents/MacOS/Synthetic'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'Synthetic executable; never executed')
    (path / 'Contents/Info.plist').write_bytes(plistlib.dumps({
        'CFBundleExecutable': 'Synthetic', 'CFBundleIdentifier': 'com.lantai.synthetic',
        'CFBundleShortVersionString': payload_identity['version'],
        'CFBundleVersion': payload_identity['version']}))
    dist = path / 'Contents/Resources/console/dist'
    baseline.write_json(dist / 'release-provenance.json', payload_identity)
    (dist / 'index.html').write_text('让浏览器帮我 浏览器会话')
    return path


@pytest.mark.parametrize('fault', ['none', 'candidate', 'relabelled-candidate', 'changed-identity'])
def test_downloads_transaction_requires_the_actual_main_payload_before_begin(tmp_path, fault):
    official = identity()
    embedded = identity(candidate=True) if 'candidate' in fault else official
    target = app(tmp_path / 'release/Synthetic.app', embedded)
    manifest_identity = official if fault == 'relabelled-candidate' else embedded
    if fault == 'changed-identity':
        manifest_identity = identity(build_time='2026-10-03T00:00:00Z')
    manifest = tmp_path / 'manifest.json'
    baseline.write_json(manifest, {
        'buildIdentity': manifest_identity,
        'buildIdentitySha256': identity_digest(manifest_identity)})
    binaries = tmp_path / 'finite-bin'
    binaries.mkdir()
    lipo = binaries / 'lipo'
    lipo.write_text("#!/bin/sh\nprintf 'arm64\\n'\n")
    lipo.chmod(0o700)
    calls = tmp_path / 'calls'
    transaction = tmp_path / 'finite-transaction.py'
    transaction.write_text(
        "import os, pathlib, sys\n"
        "with pathlib.Path(os.environ['CALLS']).open('a') as f: f.write(sys.argv[1]+'\\n')\n"
        "if sys.argv[1]=='begin': print(os.environ['STAGE'])\n")
    script = ''.join(baseline.source_function(name) for name in [
        'validate_deployment_payload_identity', 'begin_deployment', 'finish_deployment']) + '''
release_deployment_cleanup() { :; }
revalidate_deployment_source_authority() { :; } # finite source authority double
find_release_app() { printf '%s\n' "$SOURCE_APP"; }
begin_deployment
printf 'process-action\n' >> "$CALLS"
finish_deployment
'''
    result = baseline.run_shell(tmp_path, script, {
        'SOURCE_APP': target, 'DOWNLOAD_APP': target, 'ARTIFACT_MANIFEST': manifest,
        'SOURCE_SHA': baseline.EXPECTED_SHA, 'VERSION': baseline.VERSION,
        'RELEASE_ARCH': 'arm64', 'ROOT_DIR': baseline.W, 'VENV_PYTHON': sys.executable,
        'DOWNLOADS_DIR': tmp_path / 'synthetic-downloads', 'DOWNLOAD_DMG': tmp_path / 'synthetic.dmg',
        'DOWNLOAD_ZIP': tmp_path / 'synthetic.zip', 'RELEASE_DIR': tmp_path / 'release',
        'RELEASE_DEPLOYMENT_SCRIPT': transaction, 'CALLS': calls, 'STAGE': tmp_path / 'stage',
        'PATH': str(binaries) + ':/usr/bin:/bin:/usr/sbin:/sbin'})
    observed = calls.read_text().splitlines() if calls.exists() else []
    if fault == 'none':
        assert result.returncode == 0, result.stderr
        assert observed == ['begin', 'process-action', 'finish']
    else:
        assert result.returncode != 0, 'Candidate or mismatched payload was admitted to Downloads'
        assert observed == [], 'Rejection must precede transaction allocation and user process actions'


@pytest.mark.parametrize('fault', [
    'none', 'zip-candidate', 'dmg-candidate', 'dmg-sha', 'dmg-version',
    'dmg-build-time', 'dmg-extra-app', 'detach-failed'])
def test_actual_upload_checks_both_signed_payload_identities_and_always_detaches(tmp_path, fault):
    official = identity()
    zip_identity = identity(candidate=True) if fault == 'zip-candidate' else official
    dmg_identity = dict(official)
    if fault == 'dmg-candidate':
        dmg_identity = identity(candidate=True)
    elif fault == 'dmg-sha':
        dmg_identity = identity(sha='b' * 40)
    elif fault == 'dmg-version':
        dmg_identity = identity(version='9.9.9')
    elif fault == 'dmg-build-time':
        dmg_identity = identity(build_time='2026-10-03T00:00:00Z')
    zip_app = app(tmp_path / 'zip-payload/Synthetic.app', zip_identity)
    dmg_app = app(tmp_path / 'dmg-payload/Synthetic.app', dmg_identity)
    if fault == 'dmg-extra-app':
        app(dmg_app.parent / 'Foreign.app', official)
    upload = tmp_path / 'upload'
    fixture = baseline.release_fixture(upload)
    manifest = json.loads(Path(fixture['ARTIFACT_MANIFEST']).read_text())
    manifest.update(buildIdentity=official, buildIdentitySha256=identity_digest(official))
    baseline.write_json(Path(fixture['ARTIFACT_MANIFEST']), manifest)
    binaries = tmp_path / 'finite-bin'
    binaries.mkdir()
    lipo = binaries / 'lipo'
    lipo.write_text("#!/bin/sh\nprintf 'arm64\\n'\n")
    lipo.chmod(0o700)
    # A space and quote exercise trap path capture as well as failure cleanup.
    expanded = tmp_path / "expanded with ' quote"
    script = baseline.source_function('verify_public_upload_signatures') + '''
ditto() { [[ "$1 $2" == '-x -k' ]]; cp -R "$ZIP_APP" "$4/Synthetic.app"; }
codesign() { return 0; }
validate_direct_distribution_signature() { return 0; }
xcrun() { return 0; }
spctl() { return 0; }
hdiutil() {
  printf '%s\\n' "$*" >> "$MOUNT_CALLS"
  if [[ "$1" == attach ]]; then
    [[ "$*" == *'-readonly'* && "$*" == *'-nobrowse'* && "$*" == *'-noautoopen'* ]]
    local index=1 mounted=""
    while (( index <= $# )); do
      if [[ "${!index}" == -mountpoint ]]; then ((index+=1)); mounted="${!index}"; break; fi
      ((index+=1))
    done
    [[ -d "$mounted" && ! -L "$mounted" ]]
    cp -R "$DMG_ROOT"/*.app "$mounted/"
    "$VENV_PYTHON" - "$mounted" <<'PY'
import plistlib, sys
sys.stdout.buffer.write(plistlib.dumps({'system-entities': [{'mount-point': sys.argv[1]}]}))
PY
  elif [[ "$1" == detach ]]; then
    [[ "$2" == "$EXPANDED/dmg-mount" ]]
    [[ "$DETACH_FAILED" == 0 ]]
  else
    return 92
  fi
}
verify_public_upload_signatures "$UPLOAD" "$EXPANDED"
'''
    calls = tmp_path / 'mount-calls'
    result = baseline.run_shell(tmp_path, script, {
        'ZIP_APP': zip_app, 'DMG_ROOT': dmg_app.parent, 'UPLOAD': upload,
        'EXPANDED': expanded, 'VERSION': baseline.VERSION, 'RELEASE_ARCH': 'arm64',
        'PRODUCT_NAME': 'Synthetic', 'VENV_PYTHON': sys.executable,
        'RELEASE_PRODUCT_CONTRACT_SCRIPT': baseline.W / 'scripts/check_release_product_contract.py',
        'SOURCE_SHA': baseline.EXPECTED_SHA, 'ROOT_DIR': baseline.W, 'MOUNT_CALLS': calls,
        'DETACH_FAILED': '1' if fault == 'detach-failed' else '0',
        'PATH': str(binaries) + ':/usr/bin:/bin:/usr/sbin:/sbin'})
    observed = calls.read_text().splitlines() if calls.exists() else []
    mounted = any(call.startswith('attach ') for call in observed)
    detached = any(call.startswith('detach ') for call in observed)
    if fault == 'none':
        assert result.returncode == 0, result.stderr
        assert mounted and detached
    else:
        assert result.returncode != 0, 'Mismatched payload or failed cleanup reached acceptance'
        if fault == 'zip-candidate':
            assert not mounted
        else:
            assert mounted, result.stderr
            assert detached, 'Failed mounted acceptance must still detach'
