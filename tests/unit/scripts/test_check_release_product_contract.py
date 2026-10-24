from __future__ import annotations

import json
from pathlib import Path

from scripts.check_release_product_contract import validate_dist


def _write_identity(dist: Path, sha: str = "a" * 40, version: str = "9.9.9") -> None:
    (dist / "release-provenance.json").write_text(
        json.dumps({"gitSha": sha, "version": version}),
        encoding="utf-8",
    )


def test_product_contract_accepts_chat_browser_use(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("让浏览器帮我 浏览器会话", encoding="utf-8")
    _write_identity(dist)

    report = validate_dist(dist, expected_sha="a" * 40, expected_version="9.9.9")
    assert report["ok"] is True


def test_product_contract_rejects_old_panel_and_wrong_identity(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("让浏览器帮我 浏览器会话", encoding="utf-8")
    (assets / "browserWorkbench-old.js").write_text("old", encoding="utf-8")
    _write_identity(dist, sha="b" * 40)

    report = validate_dist(dist, expected_sha="a" * 40, expected_version="9.9.9")
    assert report["ok"] is False
    assert any("legacy browser workbench" in item for item in report["violations"])
    assert any("Git SHA" in item for item in report["violations"])
