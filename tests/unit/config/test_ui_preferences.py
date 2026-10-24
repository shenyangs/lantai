"""Behavior locks for backend preferences shared with the renderer."""

from __future__ import annotations

import json

from lantai.config.ui_preferences import (
    persona_mode,
    ququ_enabled,
    ququ_turn_selected,
    set_ququ_enabled,
)


def test_ququ_enabled_defaults_on_when_settings_are_absent(tmp_path) -> None:
    assert ququ_enabled(settings_file=tmp_path / "missing.json") is True


def test_ququ_enabled_reads_the_appearance_toggle(tmp_path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "appearance": {
                    "display_settings": {
                        "showInnerVoice": False,
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    assert ququ_enabled(settings_file=settings_file) is False


def test_ququ_enabled_falls_back_for_malformed_settings(tmp_path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{broken", encoding="utf-8")

    assert ququ_enabled(settings_file=settings_file, default=False) is False


def test_ququ_writer_and_reader_share_one_canonical_setting(tmp_path) -> None:
    settings_file = tmp_path / "settings.json"

    set_ququ_enabled(False, settings_file=settings_file)

    assert ququ_enabled(settings_file=settings_file) is False
    payload = json.loads(settings_file.read_text("utf-8"))
    assert payload["appearance"]["display_settings"]["showInnerVoice"] is False


def test_ququ_turn_selection_uses_one_of_two_equal_buckets(monkeypatch) -> None:
    draws = iter([0, 1])

    def fake_randbelow(upper: int) -> int:
        assert upper == 2
        return next(draws)

    monkeypatch.setattr(
        "lantai.config.ui_preferences.secrets.randbelow",
        fake_randbelow,
    )

    assert [ququ_turn_selected() for _ in range(2)] == [True, False]


def test_persona_mode_defaults_to_full_and_reads_the_same_settings_source(
    tmp_path,
) -> None:
    settings_file = tmp_path / "settings.json"
    assert persona_mode(settings_file=settings_file) == "full"
    settings_file.write_text(
        json.dumps(
            {
                "appearance": {
                    "display_settings": {
                        "personaMode": "quiet",
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    assert persona_mode(settings_file=settings_file) == "quiet"
