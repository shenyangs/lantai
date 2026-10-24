from pathlib import Path

import pytest

from lantai.research.evidence import create_packet, audit_packet, load_record


def source(root, content='样本中观察到改善，但不能推断因果。'):
    p = root / 'wiki/raw/sources/study.txt'
    p.parent.mkdir(parents=True)
    p.write_text(content)
    return p, p.relative_to(root).as_posix()


def claims(packet):
    s = packet['sources'][0]
    return [{'claim_id': 'c1', 'text': '样本中观察到改善。', 'evidence': [{
        'source_id': s['source_id'], 'start': 0, 'end': 8,
        'quote': s['text'][:8], 'relation': 'supports',
        'reviewer': 'human:test', 'rationale': '限定为样本内观察，未外推因果。',
    }]}]


def test_roundtrip_and_verifiable_export(tmp_path):
    _, path = source(tmp_path)
    packet = create_packet(tmp_path, [path])
    assert load_record(tmp_path, packet['packet_id']) == packet
    report = audit_packet(tmp_path, packet['packet_id'], claims(packet))
    assert report['status'] == 'ready_for_review'
    assert report['semantic_validation'] == 'reviewer_supplied_not_independently_verified'
    assert '#/workspace?source=' in report['markdown']
    assert load_record(tmp_path, report['audit_id']) == report


def test_changed_file_invalidates_even_identical_quote(tmp_path):
    p, path = source(tmp_path)
    packet = create_packet(tmp_path, [path])
    p.write_text(p.read_text() + '\n新增限制条件。')
    result = audit_packet(tmp_path, packet['packet_id'], claims(packet))
    assert result['status'] == 'blocked'
    assert 'source_changed' in result['issues']


def test_quote_forgery_unknown_source_and_pending_review_fail_closed(tmp_path):
    _, path = source(tmp_path)
    packet = create_packet(tmp_path, [path])
    for change, code in [({'quote': '捏造引文'}, 'quote_mismatch'),
                         ({'source_id': 'unknown'}, 'unknown_source'),
                         ({'relation': 'pending'}, 'review_pending'),
                         ({'rationale': ''}, 'review_pending'),
                         ({'relation': 'contradicts'}, 'conflicting_evidence')]:
        cs = claims(packet)
        cs[0]['evidence'][0].update(change)
        result = audit_packet(tmp_path, packet['packet_id'], cs)
        assert result['status'] == 'blocked'
        assert code in result['issues']


def test_empty_uncited_and_open_issues_block(tmp_path):
    _, path = source(tmp_path)
    packet = create_packet(tmp_path, [path])
    assert audit_packet(tmp_path, packet['packet_id'], [])['status'] == 'blocked'
    assert audit_packet(tmp_path, packet['packet_id'], [{'claim_id': 'x', 'text': '猜测', 'evidence': []}])['status'] == 'blocked'
    assert audit_packet(tmp_path, packet['packet_id'], claims(packet), ['尚缺对照组'])['status'] == 'blocked'


def test_path_escape_internal_files_and_symlinks_rejected(tmp_path):
    _, path = source(tmp_path)
    (tmp_path / 'SOUL.md').write_text('private')
    link = tmp_path / 'wiki/raw/sources/link.txt'
    link.symlink_to(tmp_path / 'SOUL.md')
    for bad in ['SOUL.md', '../outside.txt', '/etc/hosts', link.relative_to(tmp_path).as_posix()]:
        with pytest.raises(ValueError):
            create_packet(tmp_path, [bad])
    with pytest.raises(ValueError):
        load_record(tmp_path, '../SOUL')


def test_store_symlink_and_packet_tamper_rejected(tmp_path):
    _, path = source(tmp_path)
    packet = create_packet(tmp_path, [path])
    record = tmp_path / '.lantai/research' / (packet['packet_id'] + '.json')
    record.write_text(record.read_text().replace('样本中观察到改善', '原文被篡改成新内容'))
    with pytest.raises(ValueError, match='integrity'):
        load_record(tmp_path, packet['packet_id'])


def test_source_limit_not_silent_truncation(tmp_path):
    _, path = source(tmp_path)
    with pytest.raises(ValueError):
        create_packet(tmp_path, [path] * 21)


def test_citations_do_not_apply_snapshot_offsets_to_transformed_preview(tmp_path):
    _, path = source(tmp_path, '---\ntitle: test\n---\n正文。')
    packet = create_packet(tmp_path, [path])
    report = audit_packet(tmp_path, packet['packet_id'], claims(packet))
    assert '&start=' not in report['markdown']
    assert packet['packet_id'] in report['markdown']
    assert '0:8' in report['markdown']


def test_open_issues_must_be_an_array(tmp_path):
    _, path = source(tmp_path)
    packet = create_packet(tmp_path, [path])
    with pytest.raises(ValueError):
        audit_packet(tmp_path, packet['packet_id'], claims(packet), 'not an array')
