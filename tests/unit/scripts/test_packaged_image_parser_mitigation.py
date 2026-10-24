from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    script = (
        Path(__file__).parents[3]
        / "scripts"
        / "check_packaged_image_parser_mitigation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "check_packaged_image_parser_mitigation",
        script,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_presentation_root_uses_pyinstaller_bundle_resources(tmp_path: Path) -> None:
    module = _load_module()
    app = tmp_path / "澜台.app"

    assert module._packaged_presentation_root(app) == (
        app
        / "Contents"
        / "Resources"
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "Resources"
        / "lantai"
        / "presentation"
    )


def test_v3_compiler_installs_security_before_loading_pptxgenjs() -> None:
    root = Path(__file__).parents[3]
    compiler = (root / "src/lantai/presentation/pptxgenjs_v3.mjs").read_text(
        encoding="utf-8"
    )
    security = (root / "src/lantai/presentation/pptxgenjs_security.cjs").read_text(
        encoding="utf-8"
    )

    assert compiler.index("installImageParserMitigation(modulePath)") < compiler.index(
        "const PptxGenJS = require(modulePath)"
    )
    for image_type in ("icns", "jxl", "jxl-stream", "heif"):
        assert f'"{image_type}"' in security
