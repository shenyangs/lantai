from pathlib import Path

import pytest

from lantai.utils.path_transaction import path_transaction


def test_dangling_symlink_in_staging_rejects_the_whole_transaction(tmp_path):
    live = tmp_path / "workspace"
    live.mkdir()
    (live / "value").write_text("old")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "value").write_text("new")
    (stage / "dangling").symlink_to(tmp_path / "absent")
    journal = tmp_path / "transaction.json"
    with pytest.raises(ValueError, match="symlink"):
        with path_transaction(journal, [(live, stage)]):
            pass
    assert (live / "value").read_text() == "old"
    assert not journal.exists()
