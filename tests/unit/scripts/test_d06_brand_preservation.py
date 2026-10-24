from __future__ import annotations

import hashlib
import importlib.util
import zipfile
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts/presentation_flagship/validate_d06_brand_preservation.py"
)
SPEC = importlib.util.spec_from_file_location("d06_brand_preservation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
LOGO = b"unchanged-original-logo-test-bytes"
FRAME = (11106450, 123825, 400050, 400050)


def _rels(kind: str, target: str, *, external: bool = False) -> str:
    mode = ' TargetMode="External"' if external else ""
    return (
        f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" '
        f'Type="{R}/{kind}" Target="{target}"{mode}/></Relationships>'
    )


def _deck(tmp_path: Path, defect: str = "") -> Path:
    slide_show = ' showMasterSp="0"' if defect == "slide-hides-master" else ""
    layout_show = ' showMasterSp="false"' if defect == "layout-hides-master" else ""
    hidden = ' hidden="1"' if defect == "hidden-picture" else ""
    crop = '<a:srcRect l="1000"/>' if defect == "cropped-picture" else ""
    rotation = ' rot="60000"' if defect == "rotated-picture" else ""
    x, y, cx, cy = FRAME
    if defect == "moved-picture":
        x += 1
    picture = (
        f'<p:pic><p:nvPicPr><p:cNvPr id="2" name="logo"{hidden}/>'
        '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr>'
        '<p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="rId1"/>'
        f'{crop}<a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr><a:xfrm{rotation}><a:off x="{x}" y="{y}"/>'
        f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm></p:spPr></p:pic>'
    )
    if defect == "media-only":
        picture = ""
    image = b"changed-logo" if defect == "changed-image" else LOGO
    parts = {
        "ppt/presentation.xml": (
            f'<p:presentation xmlns:p="{P}" xmlns:r="{R}">'
            '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
            '</p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": _rels("slide", "slides/slide1.xml"),
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{P}"{slide_show}><p:cSld><p:spTree/></p:cSld></p:sld>',
        "ppt/slides/_rels/slide1.xml.rels": _rels("slideLayout", "../slideLayouts/slideLayout1.xml"),
        "ppt/slideLayouts/slideLayout1.xml": f'<p:sldLayout xmlns:p="{P}"{layout_show}><p:cSld><p:spTree/></p:cSld></p:sldLayout>',
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _rels("slideMaster", "../slideMasters/slideMaster1.xml"),
        "ppt/slideMasters/slideMaster1.xml": (
            f'<p:sldMaster xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
            f'<p:cSld><p:spTree>{picture}</p:spTree></p:cSld></p:sldMaster>'
        ),
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": _rels(
            "image", "../media/logo.png", external=defect == "external-image",
        ),
        "ppt/media/logo.png": image,
    }
    if defect == "broken-master-link":
        parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = _rels("slideMaster", "../slideMasters/missing.xml")
    if defect == "broken-image-link":
        parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = _rels("image", "../media/missing.png")
    if defect == "missing-image":
        del parts["ppt/media/logo.png"]
    path = tmp_path / f"{defect or 'valid'}.pptx"
    with zipfile.ZipFile(path, "w") as package:
        for name, payload in parts.items():
            package.writestr(name, payload)
    return path


def _inspect(path: Path) -> dict:
    return MODULE.inspect_logo_inheritance(
        path, logo_sha256=hashlib.sha256(LOGO).hexdigest(), frame_emu=FRAME,
        expected_slide_count=1,
    )


def test_native_master_logo_requires_active_slide_layout_master_image_chain(tmp_path: Path) -> None:
    result = _inspect(_deck(tmp_path))
    assert result["passed"] is True
    assert result["slides"][0]["logo_part"] == "ppt/media/logo.png"
    assert result["slides"][0]["master_part"] == "ppt/slideMasters/slideMaster1.xml"
    assert result["visual_review"] == "REQUIRED_SEPARATE"


@pytest.mark.parametrize("defect", [
    "media-only", "slide-hides-master", "layout-hides-master", "hidden-picture",
    "cropped-picture", "rotated-picture", "moved-picture", "changed-image",
    "external-image", "broken-master-link", "broken-image-link", "missing-image",
])
def test_logo_media_presence_does_not_accept_invisible_or_changed_logo(tmp_path: Path, defect: str) -> None:
    result = _inspect(_deck(tmp_path, defect))
    assert result["passed"] is False
    assert result["findings"]


def test_logo_gate_does_not_accept_wrong_slide_count(tmp_path: Path) -> None:
    result = MODULE.inspect_logo_inheritance(
        _deck(tmp_path), logo_sha256=hashlib.sha256(LOGO).hexdigest(),
        frame_emu=FRAME, expected_slide_count=10,
    )
    assert result["passed"] is False
    assert "expected_slide_count_mismatch" in result["findings"]
