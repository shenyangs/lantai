import asyncio

import pytest

from lantai.research.projects import ResearchService
from lantai.research.review import review_report
from lantai.research.exports import export_report, export_file

IDENTITY = {'provider': 'fixture', 'model': 'fixture', 'method': 'full_report_review_v2',
            'cross_reference_contract': 1}
TEXT = '参见表一。\n\n表一：样本\n\n| 组别 | 人数 |\n| --- | --- |\n| A | 80 |\n'


def setup(tmp_path):
    service = ResearchService(tmp_path)
    p = service.create_project('表格引用', [], idempotency_key='p')
    report = service.save_report(p['project_id'], 1, TEXT, [], idempotency_key='r')
    return service, p, report


def result(assessment='consistent', target_id='table-1'):
    return {'claims': [{'text': '参见表一。', 'assessment': 'nonfactual', 'importance': 'normal',
                        'disposition': 'accepted', 'reason': '导航文字', 'evidence_indices': []}],
            'issues': [], 'references': [{'text': '参见表一。', 'target_id': target_id,
                                        'assessment': assessment, 'reason': '引用检查'}]}


def run(service, p, report, reviewer, **kwargs):
    return asyncio.run(review_report(service, p['project_id'], 1, report['id'], reviewer,
                                    reviewer_identity=IDENTITY, **kwargs))


def test_model_receives_global_targets_and_export_keeps_version_bound_scope(tmp_path):
    service, p, report = setup(tmp_path)
    async def reviewer(payload):
        target = payload['reference_targets'][0]
        assert target['id'] == 'table-1' and target['kind'] == 'table'
        assert '| A | 80 |' in target['text'] and '表一：样本' in target['context']
        return result()
    review = run(service, p, report, reviewer)
    assert review['status'] == 'reviewed'
    assert review['reference_review']['scope'] == 'table_text_and_figure_captions'
    assert review['reference_review']['references'][0]['start'] == 0
    artifact = export_report(service, p['project_id'], 1, report['id'], 'markdown', 'numeric', 'export')
    audit = artifact['citation_audit']
    assert audit['table_figure_reference_check'] == 'text_references_reviewed'
    assert audit['reference_review']['report_hash'] == report['text_hash']
    assert '图像像素内容未核验' in export_file(service, artifact['id']).read_text()
    revised = service.save_report(p['project_id'], 1, TEXT + '\n新段落。', [],
                                  expected_report_id=report['id'], idempotency_key='edit')
    new = export_report(service, p['project_id'], 1, revised['id'], 'markdown', 'numeric', 'new')
    assert new['citation_audit']['table_figure_reference_check'] == 'not_semantically_verified'


@pytest.mark.parametrize('assessment,target', [('mismatch', 'table-1'), ('missing', None), ('uncertain', 'table-1')])
def test_unresolved_reference_blocks_even_when_empirical_claims_pass(tmp_path, assessment, target):
    service, p, report = setup(tmp_path)
    async def reviewer(_):
        return result(assessment, target)
    review = run(service, p, report, reviewer)
    assert review['status'] == 'needs_revision'
    assert review['reference_review']['status'] == 'needs_revision'
    assert any('reference' in issue for issue in review['issues'])


@pytest.mark.parametrize('change', ['missing_schema', 'unknown_target', 'false_anchor', 'missing_reason', 'consistent_without_target'])
def test_malformed_reference_review_cannot_be_committed(tmp_path, change):
    service, p, report = setup(tmp_path)
    async def reviewer(_):
        value = result()
        if change == 'missing_schema':
            del value['references']
        elif change == 'unknown_target':
            value['references'][0]['target_id'] = 'invented'
        elif change == 'false_anchor':
            value['references'][0]['text'] = '不存在的正文'
        elif change == 'missing_reason':
            value['references'][0]['reason'] = ''
        else:
            value['references'][0]['target_id'] = None
        return value
    with pytest.raises(ValueError):
        run(service, p, report, reviewer)
    assert service.project(p['project_id'])['review_status'] == 'not_reviewed'


def test_inventory_uses_markup_not_number_keywords_and_excludes_fenced_examples():
    from lantai.research.crossrefs import reference_targets
    text = '```md\n' + TEXT + '```\n\n不是编号的标题\n\n| x | y |\n| --- | --- |\n| 1 | 2 |\n'
    targets = reference_targets(text)
    assert len(targets) == 1 and targets[0]['id'] == 'table-1'
    assert text[targets[0]['start']:targets[0]['end']] == targets[0]['text']
    assert '不是编号的标题' in targets[0]['context']


def test_targets_outside_current_chunk_and_cached_resume_are_preserved(tmp_path):
    service, p, report = setup(tmp_path)
    calls = []
    async def reviewer(payload):
        calls.append(payload)
        assert payload['reference_targets'][0]['id'] == 'table-1'
        if payload['range'][0] == 0:
            return result()
        return {'claims': [], 'issues': [], 'references': []}
    review = run(service, p, report, reviewer, chunk_chars=10)
    assert len(calls) > 1 and review['status'] == 'reviewed'
    first = len(calls)
    assert run(service, p, report, reviewer, chunk_chars=10)['id'] == review['id']
    assert len(calls) == first


def test_legacy_review_is_not_promoted_to_reference_review(tmp_path):
    service, p, report = setup(tmp_path)
    async def legacy(_):
        value = result()
        del value['references']
        return value
    old = asyncio.run(review_report(service, p['project_id'], 1, report['id'], legacy,
        reviewer_identity={k: v for k, v in IDENTITY.items() if k != 'cross_reference_contract'}))
    assert old['status'] == 'reviewed' and 'reference_review' not in old
    with pytest.raises(ValueError, match='structured'):
        run(service, p, report, legacy)
    assert service.project(p['project_id'])['review_id'] == old['id']


def test_figure_inventory_has_caption_but_never_pixels_or_empirical_authority():
    from lantai.research.crossrefs import reference_targets
    identifier = 'figure-' + 'a' * 32
    targets = reference_targets(f'![图一：流程示意](research-figure:{identifier})')
    assert len(targets) == 1
    assert targets[0]['kind'] == 'figure_caption' and targets[0]['text'] == '图一：流程示意'
    assert targets[0]['figure_id'] == identifier
    assert 'data' not in targets[0]
