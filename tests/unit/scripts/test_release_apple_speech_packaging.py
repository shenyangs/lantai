# -*- coding: utf-8 -*-
"""Release guards for the removed Apple Speech helper."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_release_does_not_build_or_validate_speech_helper() -> None:
    script = (ROOT / "build_and_deploy.sh").read_text(encoding="utf-8")
    package = (ROOT / "console/package.json").read_text(encoding="utf-8")

    assert "build_apple_speech_helper" not in script
    assert "validate_packaged_apple_speech_helper" not in script
    assert "build:apple-speech-helper" not in package
    assert "apple-speech" not in package


def test_python_package_does_not_offer_transcription_extra() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "whisper = [" not in project
    assert "lantai[local,whisper]" not in project
