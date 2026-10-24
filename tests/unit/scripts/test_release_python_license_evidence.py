"""SBOM license claims must come from the matching packaged distribution."""
import hashlib
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def gate():
    path = Path(__file__).resolve().parents[3] / 'scripts/check_release_supply_chain.py'
    spec = importlib.util.spec_from_file_location('python_license_gate', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path, header='License-Expression: MIT', version='1.0'):
    (tmp_path / 'uv.lock').write_text('[[package]]\nname = "demo-lib"\nversion = "1.0"\n')
    app = tmp_path / 'Demo.app'
    meta = app / 'Contents/Resources/backend/demo_lib.dist-info/METADATA'
    meta.parent.mkdir(parents=True)
    meta.write_text(f'Metadata-Version: 2.4\nName: Demo_Lib\nVersion: {version}\n{header}\n\n')
    return app, meta


@pytest.mark.parametrize('header,expected', [
    ('License-Expression: MIT', [{'expression': 'MIT'}]),
    ('License-Expression: MIT OR Apache-2.0\nLicense: obsolete', [{'expression': 'MIT OR Apache-2.0'}]),
    ('License: Proprietary', [{'license': {'name': 'Proprietary'}}]),
    ('Classifier: License :: OSI Approved :: BSD License', [{'license': {'name': 'License :: OSI Approved :: BSD License'}}]),
])
def test_exact_packaged_license_and_hashed_source(gate, tmp_path, header, expected):
    app, meta = fixture(tmp_path, header)
    component, = gate._python_components(tmp_path, app=app)
    assert component['licenses'] == expected
    evidence = {p['name']: p['value'] for p in component['properties']}
    assert evidence['lantai:python:metadata:0:path'] == meta.relative_to(app).as_posix()
    assert evidence['lantai:python:metadata:0:sha256'] == hashlib.sha256(meta.read_bytes()).hexdigest()
    gate._cyclonedx_bom(package={'name': 'app'}, source_sha='a'*40, version='1', arch='arm64', components=[component])


def test_other_version_cannot_supply_license(gate, tmp_path):
    app, _ = fixture(tmp_path, version='2.0')
    component, = gate._python_components(tmp_path, app=app)
    assert 'licenses' not in component
    assert not any(p['name'].startswith('lantai:python:metadata:') for p in component['properties'])


def test_duplicate_packaged_copies_preserve_both_sources(gate, tmp_path):
    app, meta = fixture(tmp_path)
    other = app / 'Contents/Frameworks/demo_lib.dist-info/METADATA'
    other.parent.mkdir(parents=True)
    other.write_bytes(meta.read_bytes())
    component, = gate._python_components(tmp_path, app=app)
    assert component['licenses'] == [{'expression': 'MIT'}]
    assert sum(p['name'].endswith(':sha256') for p in component['properties']) == 2


def test_conflicting_packaged_license_is_rejected(gate, tmp_path):
    app, meta = fixture(tmp_path)
    other = app / 'Contents/Frameworks/demo_lib.dist-info/METADATA'
    other.parent.mkdir(parents=True)
    other.write_text(meta.read_text().replace('MIT', 'Apache-2.0'))
    with pytest.raises(SystemExit, match='Conflicting packaged license'):
        gate._python_components(tmp_path, app=app)


def test_packaged_metadata_cannot_escape_app(gate, tmp_path):
    app, meta = fixture(tmp_path)
    outside = tmp_path / 'outside-METADATA'
    outside.write_bytes(meta.read_bytes())
    meta.unlink()
    meta.symlink_to(outside)
    with pytest.raises(SystemExit, match='outside app'):
        gate._python_components(tmp_path, app=app)


@pytest.mark.parametrize('license_text', [
    'Copyright © 2026 José，版权所有',
    'BSD license\n Copyright © 2026 José，版权所有',
])
def test_legacy_license_preserves_utf8_and_folded_headers(gate, tmp_path, license_text):
    app, _ = fixture(tmp_path, header='License: ' + license_text)
    component, = gate._python_components(tmp_path, app=app)
    claimed = component['licenses'][0]['license']['name']
    assert '© 2026 José，版权所有' in claimed
    gate._cyclonedx_bom(package={'name': 'app'}, source_sha='a'*40, version='1', arch='arm64', components=[component])
