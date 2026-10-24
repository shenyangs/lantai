import pytest

from lantai.research.library import LibraryService
from lantai.research.store import RevisionConflict


def add(library):
    return library.import_items([{'title': 'A study', 'DOI': '10.1234/study'}],
                                origin={'provider': 'fixture'})['items'][0]['work_id']


def test_organization_survives_restart_and_duplicate_import_without_changing_bibliography(tmp_path):
    library = LibraryService(tmp_path)
    work = library.work(add(library))
    updated = library.organize(work['work_id'], 0, tags=[' causal ', 'causal'], collections=['Review'])
    assert updated['organization'] == {'revision': 1, 'tags': ['causal'], 'collections': ['Review']}
    assert updated['selected_version_id'] == work['selected_version_id']
    assert add(library) == work['work_id']
    assert LibraryService(tmp_path).work(work['work_id'])['organization'] == updated['organization']
    with pytest.raises(RevisionConflict):
        library.organize(work['work_id'], 0, tags=['lost'], collections=[])
    cleared = library.organize(work['work_id'], 1, tags=[], collections=[])
    assert cleared['organization'] == {'revision': 2, 'tags': [], 'collections': []}
    assert len(library.store.list('library_organization')) == 2


@pytest.mark.parametrize('format', ['csl-json', 'bibtex', 'ris'])
def test_organization_export_roundtrip_and_reimport_never_overwrites_newer_edit(tmp_path, format):
    library = LibraryService(tmp_path)
    work = add(library)
    library.organize(work, 0, tags=['方法'], collections=['文献综述'])
    exported = library.export(format)
    library.organize(work, 1, tags=['changed'], collections=[])
    library.import_text(exported, format)
    assert library.work(work)['organization']['tags'] == ['changed']
    assert len(library.work(work)['versions']) == 1
    other = LibraryService(tmp_path / format)
    imported = other.import_text(exported, format)['items'][0]
    assert other.work(imported['work_id'])['organization']['collections'] == ['文献综述']


@pytest.mark.parametrize('tags', ['text', [42], [''], ['x' * 201], ['tag'] * 101])
def test_invalid_labels_leave_organization_unchanged(tmp_path, tags):
    library = LibraryService(tmp_path)
    work = add(library)
    with pytest.raises(ValueError, match='labels'):
        library.organize(work, 0, tags=tags, collections=[])
    assert library.work(work)['organization']['revision'] == 0


@pytest.mark.parametrize('custom', [{}, {'note': 'original metadata'}])
def test_roundtrip_preserves_original_custom_metadata_without_new_edition(tmp_path, custom):
    library = LibraryService(tmp_path)
    work = library.import_items([{'title': 'Custom metadata', 'custom': custom}],
                                origin={'provider': 'fixture'})['items'][0]['work_id']
    library.organize(work, 0, tags=['tag'], collections=[])
    library.import_text(library.export('csl-json'), 'csl-json')
    assert library.work(work)['csl']['custom'] == custom
    assert len(library.work(work)['versions']) == 1
