"""Small clean-room command boundary shared by Lantai's Office Skills."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from lantai.ooxml_content_types import find_content_type_sequence_issue


PACKAGE_MEMBERS = {
    "pptx": {"[Content_Types].xml", "ppt/presentation.xml"},
    "docx": {"[Content_Types].xml", "word/document.xml"},
    "xlsx": {"[Content_Types].xml", "xl/workbook.xml"},
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preflight_office_artifact(path: str | Path, kind: str) -> dict[str, object]:
    """Inspect an artifact container without changing it."""

    source = Path(path).expanduser().resolve()
    normalized = kind.lower().lstrip(".")
    if normalized not in {*PACKAGE_MEMBERS, "pdf"}:
        raise ValueError(f"unsupported Office artifact kind: {kind}")
    if source.suffix.lower() != f".{normalized}":
        raise ValueError(f"expected a .{normalized} file: {source}")
    if not source.is_file():
        raise FileNotFoundError(source)

    issues: list[str] = []
    if normalized == "pdf":
        size = source.stat().st_size
        with source.open("rb") as stream:
            header = stream.read(5)
            stream.seek(max(0, size - 2048))
            trailer = stream.read()
        if header != b"%PDF-":
            issues.append("missing PDF signature")
        if b"%%EOF" not in trailer:
            issues.append("missing PDF trailer")
    else:
        try:
            with zipfile.ZipFile(source) as archive:
                names = set(archive.namelist())
                corrupt = archive.testzip()
        except zipfile.BadZipFile:
            issues.append("invalid ZIP container")
        else:
            issues.extend(
                f"missing package member: {member}"
                for member in sorted(PACKAGE_MEMBERS[normalized] - names)
            )
            if corrupt:
                issues.append(f"corrupt package member: {corrupt}")
            if normalized == "pptx":
                content_type_issue = find_content_type_sequence_issue(source)
                if content_type_issue:
                    issues.append(content_type_issue)

    return {
        "schema_version": "1.0",
        "kind": normalized,
        "path": str(source),
        "sha256": _sha256(source),
        "size_bytes": source.stat().st_size,
        "passed": not issues,
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect one Office artifact")
    parser.add_argument("--kind", choices=["pptx", "docx", "xlsx", "pdf"], required=True)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = preflight_office_artifact(args.path, args.kind)
    except (OSError, ValueError) as exc:
        result = {"schema_version": "1.0", "kind": args.kind, "passed": False, "issues": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
