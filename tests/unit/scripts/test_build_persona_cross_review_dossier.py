from __future__ import annotations

from scripts import build_persona_cross_review_dossier as dossier


def test_dossier_timestamp_helper_supports_project_python():
    assert dossier._iso(0).endswith("+00:00")

