"""SBOM identities retain each locked installation without ambiguous references."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location('sbom_identity_gate', ROOT / 'scripts/check_release_supply_chain.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nested_same_version_installations_have_unique_stable_references(gate, tmp_path):
    path = tmp_path / 'console/package-lock.json'
    path.parent.mkdir()
    packages = {
        'node_modules/shared': {'version': '1.0', 'license': 'MIT'},
        'node_modules/parent/node_modules/shared': {'version': '1.0', 'license': 'ISC', 'dev': True},
    }
    path.write_text(json.dumps({'packages': packages}))
    components, _ = gate._npm_components(tmp_path)
    assert len({item['bom-ref'] for item in components}) == 2
    assert len({item['purl'] for item in components}) == 1
    by_path = {next(p['value'] for p in c['properties'] if p['name'] == 'lantai:lock:path'): c for c in components}
    assert set(by_path) == set(packages)
    for location, component in by_path.items():
        assert component['licenses'][0]['license']['id'] == packages[location]['license']
        assert component['scope'] == ('excluded' if packages[location].get('dev') else 'required')
    path.write_text(json.dumps({'packages': dict(reversed(list(packages.items())))}))
    assert gate._npm_components(tmp_path)[0] == components


def test_sbom_rejects_duplicate_component_references(gate):
    with pytest.raises(SystemExit, match='duplicate.*bom-ref'):
        gate._cyclonedx_bom(package={'name': 'app'}, source_sha='f' * 40, version='1', arch='arm64',
                            components=[{'bom-ref': 'same'}, {'bom-ref': 'same'}])


@pytest.mark.parametrize('components', [[{'bom-ref': 'application:app@1'}], [{}], [{'bom-ref': ''}]])
def test_sbom_rejects_missing_or_application_colliding_reference(gate, components):
    with pytest.raises(SystemExit, match='bom-ref'):
        gate._cyclonedx_bom(package={'name': 'app'}, source_sha='f' * 40, version='1', arch='arm64', components=components)


def test_manifest_validation_rejects_hash_correct_duplicate_sbom(gate, tmp_path):
    sbom = tmp_path / 'sbom.json'
    sbom.write_text(json.dumps({'components': [{'bom-ref': 'same'}, {'bom-ref': 'same'}]}))
    manifest = {'schema': gate.SCHEMA, 'source': {'sha': 'f' * 40},
                'release': {'version': '1', 'arch': 'arm64'},
                'sbom': {'path': str(sbom), 'sha256': gate._sha256_file(sbom)}}
    with pytest.raises(SystemExit, match='duplicate.*bom-ref'):
        gate.validate_manifest_payload(manifest, source_sha='f' * 40, version='1', arch='arm64', base_dir=tmp_path)


@pytest.mark.parametrize('payload', [[], {'metadata': []}, {'metadata': ['bad']}, {'components': {}}, {'components': [None]}, {'metadata': {'component': ''}}])
def test_malformed_sbom_identity_structure_fails_explicitly(gate, payload):
    with pytest.raises(SystemExit, match='SBOM'):
        gate._validate_sbom_references(payload)
