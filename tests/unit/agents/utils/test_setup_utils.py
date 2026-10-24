# -*- coding: utf-8 -*-
"""Tests for agent workspace markdown setup utilities."""

from lantai.agents.utils.setup_utils import copy_md_files


def test_copy_md_files_copies_product_guide_as_on_demand_reference(tmp_path):
    """Product guide should exist in the workspace without being prompt-loaded."""
    copied = copy_md_files("zh", workspace_dir=tmp_path)

    assert "PRODUCT_GUIDE.md" in copied
    assert (tmp_path / "PRODUCT_GUIDE.md").is_file()
