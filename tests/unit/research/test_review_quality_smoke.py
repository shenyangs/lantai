"""Exercise the CLI evaluator without a Provider; model quality is not simulated."""
import asyncio
import importlib.util
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('research_quality_smoke', ROOT / 'scripts/research_review_quality_smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.parametrize('required,declared,checkpoints,assessments,passed', [
    (True, 0, [], [], False),  # Observed: primary marked the main answer normal.
    (True, 1, [], ['supports'], False),
    (True, 1, ['checkpoint'], [], False),
    (True, 1, ['checkpoint'], ['supports'], True),
    (True, 1, ['checkpoint'], ['insufficient'], False),
    (False, 0, [], [], True),  # Unannotated/background-only cases are allowed.
])
def test_required_recheck_cannot_pass_vacuously(tmp_path, monkeypatch, required,
        declared, checkpoints, assessments, passed):
    from lantai.config import config
    from lantai import providers
    from lantai.research import review

    slot = SimpleNamespace(provider_id='test', model='no-network')
    monkeypatch.setattr(config, 'load_agent_config', lambda _: SimpleNamespace(active_model=slot))
    monkeypatch.setattr(providers, 'ProviderManager', lambda: SimpleNamespace(
        is_chat_model_slot_selectable=lambda _: True))

    async def controlled_result(service, project, revision, report_id, *args, **kwargs):
        report = service.report(report_id)
        return {'claims': [{'assessment': 'supports', 'disposition': 'accepted'}],
            'status': 'reviewed', 'coverage': [[0, len(report['text'])]],
            'critical_review': {'required_claims': declared, 'checkpoint_ids': checkpoints,
                'claims': [{'assessment': a} for a in assessments]}}

    monkeypatch.setattr(review, 'configured_review', controlled_result)
    corpus = {'minimum_accuracy': 0.95, 'label_source': 'test', 'cases': [{
        'id': 'bounded', 'source': 'An association does not establish causality.',
        'report': 'An association does not establish causality.',
        'expected_assessment': 'supports', 'require_critical_recheck': required}]}
    result = asyncio.run(smoke.evaluate(SimpleNamespace(trials=1, concurrency=1, timeout=5),
        tmp_path, corpus))
    assert result['passed'] is passed
    assert result['accuracy'] == 1.0  # Missing review must not rewrite relation labels.
    assert result['trials'][0]['inference']['calls'] == 0
    assert result['required_critical_reviews'] == int(required)
    assert bool(result['missing_critical_reviews']) is (required and not (declared and checkpoints and assessments))


def test_v2_only_adds_required_recheck_annotations_to_frozen_v1():
    old = ROOT / 'tests/fixtures/research/review-quality-v1.json'
    baseline = json.loads(old.read_text())
    annotated = json.loads((old.parent / 'review-quality-v2.json').read_text())
    assert annotated.pop('baseline_corpus_sha256') == hashlib.sha256(old.read_bytes()).hexdigest()
    assert annotated.pop('critical_annotation_source')
    for case in annotated['cases']:
        assert case.pop('require_critical_recheck') is (case['expected_assessment'] != 'nonfactual')
    assert annotated == baseline


@pytest.mark.parametrize('value', ['false', 0, None])
def test_invalid_annotation_fails_before_loading_provider(value, tmp_path):
    with pytest.raises(ValueError, match='boolean annotation'):
        asyncio.run(smoke.evaluate(None, tmp_path, {'cases': [{'require_critical_recheck': value}]}))
