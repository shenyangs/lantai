import pytest

from lantai.research.citations import format_citations


def item(identifier, title, author=None, year=2024):
    return {'id': identifier, 'type': 'article-journal', 'title': title,
            'author': author or [{'family': 'Smith', 'given': 'Ada'}],
            'issued': {'date-parts': [[year]]}, 'container-title': 'Evidence Journal',
            'volume': '12', 'page': '1-9'}


def test_apa_updates_earlier_citations_when_later_item_disambiguates():
    result = format_citations([item('a', 'Alpha'), item('b', 'Beta')],
                              [[{'id': 'a'}], [{'id': 'b'}]], 'apa')
    assert '2024a' in result['citations'][0]
    assert '2024b' in result['citations'][1]
    assert len(result['bibliography']) == 2
    assert result['provenance']['engine_version'] == '2.4.63'


def test_numeric_order_follows_first_citation_and_reuses_number():
    result = format_citations([item('a', 'Alpha'), item('b', 'Beta')],
                              [[{'id': 'b'}], [{'id': 'a'}], [{'id': 'b'}]], 'numeric')
    assert result['citations'] == ['[1]', '[2]', '[1]']
    assert 'Beta' in result['bibliography'][0]
    assert 'Alpha' in result['bibliography'][1]


def test_chinese_corporate_author_and_locator_are_preserved():
    result = format_citations([item('中', '中文证据报告', [{'literal': '中国科学研究院'}])],
                              [[{'id': '中', 'locator': '27', 'label': 'page'}]], 'gb7714')
    assert '中国科学研究院' in result['bibliography'][0]
    assert '中文证据报告' in result['bibliography'][0]
    assert result['provenance']['style_sha256']


def test_unknown_ids_or_styles_fail_without_placeholder_references():
    with pytest.raises(ValueError):
        format_citations([item('a', 'Alpha')], [[{'id': 'missing'}]], 'apa')
    with pytest.raises(ValueError):
        format_citations([item('a', 'Alpha')], [[{'id': 'a'}]], '../apa')


@pytest.mark.parametrize('style', ['apa', 'numeric', 'gb7714'])
def test_citation_audit_maps_repeated_clusters_to_actual_bibliography(style):
    result = format_citations([item('a', 'Alpha'), item('b', 'Beta'), item('unused', 'Unused')],
                              [[{'id': 'b'}], [{'id': 'a'}, {'id': 'b'}]], style)
    audit = result['audit']
    assert audit['status'] == 'consistent'
    assert audit['clusters'] == [['b'], ['a', 'b']]
    assert set(audit['bibliography_ids']) == {'a', 'b'}
    assert len(result['bibliography']) == len(audit['bibliography_ids']) == 2
    assert audit['metadata_warnings'] == []


def test_missing_bibliographic_fields_remain_unknown_and_are_disclosed():
    result = format_citations([{'id': 'a', 'title': 'Anonymous observation', 'type': 'article-journal'}],
                              [[{'id': 'a'}]], 'apa')
    warning = result['audit']['metadata_warnings'][0]
    assert warning == {'id': 'a', 'missing_fields': ['author', 'issued', 'container-title']}


def test_inconsistent_formatter_output_cannot_be_exported():
    from lantai.research.citations import validate_citation_output
    correct = {'citations': ['[1]'], 'bibliography': ['Alpha'],
               'bibliography_parameters': {'entry_ids': [['a']]}}
    validate_citation_output(correct, [[{'id': 'a'}]])
    for corrupted in [dict(correct, citations=[]), dict(correct, bibliography=[]),
                      dict(correct, bibliography_parameters={'entry_ids': [['b']]}),
                      dict(correct, bibliography=['A', 'A'], bibliography_parameters={'entry_ids': [['a'], ['a']]})]:
        with pytest.raises(ValueError, match='Citation consistency'):
            validate_citation_output(corrupted, [[{'id': 'a'}]])


@pytest.mark.parametrize('issued', [{}, {'date-parts': []}, {'raw': '   '}])
def test_empty_date_containers_do_not_hide_missing_date(issued):
    value = item('a', 'Alpha')
    value['issued'] = issued
    result = format_citations([value], [[{'id': 'a'}]], 'numeric')
    assert result['audit']['metadata_warnings'] == [{'id': 'a', 'missing_fields': ['issued']}]
