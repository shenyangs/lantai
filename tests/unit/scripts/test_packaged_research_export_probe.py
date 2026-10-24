from scripts.packaged_research_export_probe import run_probe


def test_research_export_probe_checks_all_real_formats_and_draft_status():
    report = run_probe()
    assert report['status'] == 'ok'
    assert report['model_calls'] == 0
    assert set(report['exports']) == {'markdown', 'docx', 'pdf'}
    assert all(item['hash_verified'] and item['draft_label'] for item in report['exports'].values())
    assert report['exports']['docx']['embedded_font']
    assert report['exports']['pdf']['embedded_font']
