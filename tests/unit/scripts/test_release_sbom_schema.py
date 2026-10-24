"""Release SBOMs must conform even when their file digest is correct."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location('sbom_schema_gate', ROOT / 'scripts/check_release_supply_chain.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.mark.parametrize('value', ['NOASSERTION', 'UNLICENSED', 'Proprietary', 'Invented-License-1.0'])
def test_unknown_license_is_not_an_spdx_identifier(gate, value):
    assert all('id' not in entry.get('license', {}) for entry in gate._license_entry(value))

@pytest.mark.parametrize('entrypoint', ['supply', 'artifact'])
@pytest.mark.parametrize('mutation', ['format', 'hash', 'license', 'spec', 'version', 'manifest_spec'])
def test_hash_correct_invalid_sbom_is_rejected(gate, tmp_path, mutation, entrypoint):
    bom = gate._cyclonedx_bom(package={'name': 'probe', 'license': 'MIT'}, source_sha='a'*40,
                             version='1', arch='arm64', components=[{'type':'library', 'bom-ref':'probe',
                             'name':'probe', 'version':'1', 'hashes':[{'alg':'SHA-256', 'content':'a'*64}]}])
    if mutation == 'format': bom['bomFormat'] = 'NotCycloneDX'
    if mutation == 'hash': bom['components'][0]['hashes'][0]['content'] = 'invalid'
    if mutation == 'license': bom['metadata']['component']['licenses'] = [{'license': {'id': 'NOASSERTION'}}]
    if mutation == 'spec': bom['specVersion'] = '999'
    if mutation == 'version': del bom['version']
    path = tmp_path / 'bom.json'
    path.write_text(json.dumps(bom))
    manifest = {'schema':gate.SCHEMA, 'source':{'sha':'a'*40}, 'release':{'version':'1','arch':'arm64'},
                'sbom':{'path':str(path), 'sha256':gate._sha256_file(path), 'format':'CycloneDX',
                        'specVersion':'999' if mutation == 'manifest_spec' else '1.5'},
                'binarySources':[{'name':'synthetic', 'version':'1', 'arch':'arm64','sha256':'b'*64,
                                  'source':{'type':'git'}, 'license':'MIT'}]}
    with pytest.raises(SystemExit, match='SBOM'):
        if entrypoint == 'supply':
            gate.validate_manifest_payload(manifest, source_sha='a'*40,version='1',arch='arm64',base_dir=tmp_path)
        else:
            spec = importlib.util.spec_from_file_location('artifact_schema_gate', ROOT / 'scripts/check_release_artifact_provenance.py')
            artifact = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(artifact)
            artifact._validate_supply_chain_payload(manifest, source_sha='a'*40,version='1',arch='arm64',release_dir=tmp_path)

def test_generation_rejects_invalid_component(gate):
    with pytest.raises(SystemExit, match='SBOM'):
        gate._cyclonedx_bom(package={'name':'app'}, source_sha='a'*40, version='1', arch='arm64',
                            components=[{'bom-ref':'broken', 'type':'unknown', 'name':'bad'}])
