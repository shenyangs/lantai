# -*- coding: utf-8 -*-
"""Repository-wide pytest collection behavior locks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = ROOT / "pyproject.toml"


def test_pytest_uses_importlib_mode_for_duplicate_test_module_names():
    """The full unit gate must collect same-named tests from separate packages."""

    text = PYPROJECT.read_text(encoding="utf-8")
    pytest_section = text.split("[tool.pytest.ini_options]", maxsplit=1)[1].split(
        "\n[", maxsplit=1
    )[0]

    assert 'addopts = ["--import-mode=importlib"]' in pytest_section
