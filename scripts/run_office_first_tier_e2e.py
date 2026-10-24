#!/usr/bin/env python3
"""Build real Office fixtures through Lantai's safe bridge and reopen/render them.

This is an evidence gate, not a demo generator. DOCX/XLSX/PPTX writes cross the
same transactional boundary used by the OfficeCLI MCP connector: isolated copy,
flush, OpenXML validation, baseline hash check, backup and atomic replacement.
The PDF fixture is opened by PDFKit and recognized by Lantai's bundled offline
Vision helper. With --capture, all four files are reopened in the real editor
renderers at the same 1440x900 viewport and must satisfy the shared shell checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"
DEFAULT_OFFICECLI = (
    ROOT / "scripts/pack/assets/officecli/darwin-arm64/officecli"
)
PDF_FIXTURE_BUILDER = CONSOLE / "scripts/create-office-shell-pdf-fixture.py"
PDF_VISION = (
    CONSOLE
    / "vendor/lantai-docx-editor/apps/pdf/native-bin/darwin-arm64/lantai-pdf-vision"
)
ELECTRON = CONSOLE / "node_modules/.bin/electron"
CAPTURE_SCRIPT = CONSOLE / "scripts/capture-office-shell.cjs"


class EvidenceError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_json(
    command: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    result = subprocess.run(
        command,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise EvidenceError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr or result.stdout}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EvidenceError(
            f"command did not return JSON: {' '.join(command)}"
        ) from exc
    if payload.get("success") is False or payload.get("ok") is False:
        raise EvidenceError(f"command reported failure: {payload}")
    return payload


def _marker_present(payload: dict[str, Any], marker: str) -> bool:
    return marker in json.dumps(payload, ensure_ascii=False)


def _close_officecli_residents(
    officecli: Path,
    paths: Iterable[Path],
    *,
    env: dict[str, str],
) -> None:
    errors: list[tuple[Path, Exception]] = []
    for path in paths:
        try:
            _run_json([str(officecli), "close", str(path), "--json"], env=env)
        except Exception as error:
            # A failed close must not prevent the other documents from closing.
            errors.append((path, error))
    if errors:
        details = "; ".join(f"{path}: {error}" for path, error in errors)
        raise EvidenceError(
            f"OfficeCLI resident close failed: {details}"
        ) from errors[0][1]


def _create_openxml_evidence(
    *,
    officecli: Path,
    output_dir: Path,
    case_id: str,
) -> tuple[dict[str, Path], dict[str, Any]]:
    # Resolve the backend working directory before importing the bridge: this
    # keeps rollback evidence inside this run's evidence directory.
    os.environ["LANTAI_WORKING_DIR"] = str(output_dir / "bridge-runtime")
    sys.path.insert(0, str(ROOT / "src"))
    from lantai.app.officecli_safe_bridge import (  # noqa: PLC0415
        cleanup_officecli_transaction,
        commit_officecli_transaction,
        prepare_officecli_mcp_call,
    )

    cli_env = os.environ.copy()
    cli_env["OFFICECLI_RESIDENT_FLUSH"] = "each"
    receipts: list[dict[str, Any]] = []

    def transact(argv: list[str]) -> dict[str, Any]:
        transaction = prepare_officecli_mcp_call({"command": argv})
        if transaction is None:
            raise EvidenceError(
                f"expected a mutating OfficeCLI command: {argv}"
            )
        resident_closed = False
        try:
            try:
                operation = _run_json(
                    [
                        str(officecli),
                        *transaction.arguments["command"],
                        "--json",
                    ],
                    env=cli_env,
                )
                # Flush the mutation before validating its on-disk result.
                _run_json(
                    [
                        str(officecli),
                        "close",
                        str(transaction.working_path),
                        "--json",
                    ],
                    env=cli_env,
                )
                validation = _run_json(
                    [
                        str(officecli),
                        "validate",
                        str(transaction.working_path),
                        "--json",
                    ],
                    env=cli_env,
                )
            finally:
                # Validation also auto-starts a resident. Close it on both
                # success and failure, before publishing or removing its file.
                _close_officecli_residents(
                    officecli, [transaction.working_path], env=cli_env
                )
                resident_closed = True
            receipt = commit_officecli_transaction(transaction)
            evidence = {
                "verb": transaction.verb,
                "operation": operation,
                "validation": validation,
                "receipt": asdict(receipt),
            }
            receipts.append(evidence)
            return evidence
        finally:
            if resident_closed:
                cleanup_officecli_transaction(transaction)

    files = {
        "docs": output_dir / "office-first-tier.docx",
        "sheets": output_dir / "office-first-tier.xlsx",
        "slides": output_dir / "office-first-tier.pptx",
    }
    for target in files.values():
        transact(["create", str(target)])

    doc_marker = f"Lantai Word first-tier {case_id} 314159"
    transact(
        [
            "batch",
            str(files["docs"]),
            "--commands",
            json.dumps(
                [
                    {
                        "command": "add",
                        "parent": "/body",
                        "type": "paragraph",
                        "props": {
                            "text": doc_marker,
                            "bold": "true",
                            "size": "22pt",
                            "color": "#1F3A5F",
                        },
                    },
                    {
                        "command": "add",
                        "parent": "/body",
                        "type": "paragraph",
                        "props": {
                            "text": "Transactional save, validation, reopen and rollback evidence.",
                            "size": "12pt",
                        },
                    },
                ],
                ensure_ascii=False,
            ),
        ]
    )

    sheet_marker = f"Lantai Excel first-tier {case_id} 161803"
    transact(
        [
            "batch",
            str(files["sheets"]),
            "--commands",
            json.dumps(
                [
                    {
                        "command": "add",
                        "parent": "/Sheet1/A1",
                        "type": "cell",
                        "props": {"value": sheet_marker, "bold": "true"},
                    },
                    {
                        "command": "add",
                        "parent": "/Sheet1/B1",
                        "type": "cell",
                        "props": {"value": "7", "type": "number"},
                    },
                    {
                        "command": "add",
                        "parent": "/Sheet1/C1",
                        "type": "cell",
                        "props": {"formula": "B1*6"},
                    },
                ],
                ensure_ascii=False,
            ),
        ]
    )

    slide_marker = f"Lantai PPT first-tier {case_id} 271828"
    transact(
        [
            "batch",
            str(files["slides"]),
            "--commands",
            json.dumps(
                [
                    {"command": "add", "parent": "/", "type": "slide"},
                    {
                        "command": "add",
                        "parent": "/slide[1]",
                        "type": "shape",
                        "props": {
                            "text": slide_marker,
                            "x": "2cm",
                            "y": "2cm",
                            "width": "20cm",
                            "height": "3cm",
                            "size": "30pt",
                            "bold": "true",
                            "color": "#1F3A5F",
                        },
                    },
                    {
                        "command": "add",
                        "parent": "/slide[1]",
                        "type": "shape",
                        "props": {
                            "text": "Native layout / render / QC evidence",
                            "x": "2cm",
                            "y": "6cm",
                            "width": "18cm",
                            "height": "2cm",
                            "size": "18pt",
                            "color": "#536F90",
                        },
                    },
                ],
                ensure_ascii=False,
            ),
        ]
    )

    # Fresh OfficeCLI processes reopen the committed files from disk. These
    # checks therefore cannot accidentally read the bridge's temporary copy.
    try:
        readback = {
            "docs": _run_json(
                [str(officecli), "get", str(files["docs"]), "/body", "--json"],
                env=cli_env,
            ),
            "sheets_a1": _run_json(
                [
                    str(officecli),
                    "get",
                    str(files["sheets"]),
                    "/Sheet1/A1",
                    "--json",
                ],
                env=cli_env,
            ),
            "sheets_c1": _run_json(
                [
                    str(officecli),
                    "get",
                    str(files["sheets"]),
                    "/Sheet1/C1",
                    "--json",
                ],
                env=cli_env,
            ),
            "slides": _run_json(
                [
                    str(officecli),
                    "get",
                    str(files["slides"]),
                    "/slide[1]",
                    "--json",
                ],
                env=cli_env,
            ),
        }
        if not _marker_present(readback["docs"], doc_marker):
            raise EvidenceError("DOCX marker was missing after reopen")
        if not _marker_present(readback["sheets_a1"], sheet_marker):
            raise EvidenceError("XLSX marker was missing after reopen")
        c1 = json.dumps(readback["sheets_c1"], ensure_ascii=False)
        if '"formula": "B1*6"' not in c1 or '"computedValue": "42"' not in c1:
            raise EvidenceError(
                "XLSX formula was not preserved and recalculated after reopen"
            )
        if not _marker_present(readback["slides"], slide_marker):
            raise EvidenceError("PPTX marker was missing after reopen")

        for target in files.values():
            _run_json(
                [str(officecli), "validate", str(target), "--json"],
                env=cli_env,
            )
            with zipfile.ZipFile(target) as archive:
                bad = archive.testzip()
                if bad is not None:
                    raise EvidenceError(
                        f"corrupt OpenXML member in {target.name}: {bad}"
                    )

        return files, {"receipts": receipts, "readback": readback}
    finally:
        _close_officecli_residents(officecli, files.values(), env=cli_env)


def _create_pdf_evidence(
    output_dir: Path, *, case_id: str
) -> tuple[Path, dict[str, Any]]:
    target = output_dir / "office-first-tier.pdf"
    subprocess.run(
        [sys.executable, str(PDF_FIXTURE_BUILDER), str(target), case_id],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    if not target.read_bytes().startswith(b"%PDF-"):
        raise EvidenceError("PDF fixture does not have a PDF header")
    if not PDF_VISION.is_file() or not os.access(PDF_VISION, os.X_OK):
        raise EvidenceError(
            f"bundled PDF Vision helper is unavailable: {PDF_VISION}"
        )
    ocr = _run_json(
        [str(PDF_VISION)],
        input_text=json.dumps(
            {"path": str(target), "pages": [0, 1], "languages": ["en-US"]}
        ),
        timeout=90,
    )
    pages = ocr.get("pages") or []
    recognized = " ".join(
        str(line.get("text") or "")
        for page in pages
        for line in (page.get("lines") or [])
    )
    if (
        len(pages) != 2
        or "healthy PDF" not in recognized
        or case_id not in recognized
    ):
        raise EvidenceError(
            "PDFKit/Vision did not reopen and recognize the two-page PDF"
        )
    return target, {"ocr": ocr, "recognized_text": recognized}


def _capture_all(files: dict[str, Path], output_dir: Path) -> dict[str, Any]:
    if not ELECTRON.is_file():
        raise EvidenceError(f"Electron executable is unavailable: {ELECTRON}")
    captures: dict[str, Any] = {}
    for editor, fixture in files.items():
        screenshot = output_dir / f"{editor}.png"
        result = subprocess.run(
            [
                str(ELECTRON),
                str(CAPTURE_SCRIPT),
                editor,
                str(screenshot),
                str(fixture),
            ],
            cwd=CONSOLE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
            check=False,
        )
        if result.returncode != 0 or not screenshot.is_file():
            raise EvidenceError(
                f"{editor} renderer capture failed ({result.returncode})\n"
                f"{result.stdout}\n{result.stderr}"
            )
        captures[editor] = {
            "screenshot": str(screenshot),
            "sha256": _sha256(screenshot),
            "stdout": result.stdout.strip(),
        }
    return captures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--officecli", type=Path, default=DEFAULT_OFFICECLI)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--case-id", default="case-01")
    args = parser.parse_args()

    officecli = args.officecli.expanduser().resolve(strict=True)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else CONSOLE
        / ".artifacts/office-first-tier-e2e"
        / time.strftime("%Y%m%d-%H%M%S")
    )
    if output_dir.exists() and any(output_dir.iterdir()):
        raise EvidenceError(
            f"refusing to overwrite non-empty evidence directory: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    openxml_files, openxml = _create_openxml_evidence(
        officecli=officecli,
        output_dir=output_dir,
        case_id=args.case_id,
    )
    pdf_file, pdf = _create_pdf_evidence(output_dir, case_id=args.case_id)
    files = {**openxml_files, "pdf": pdf_file}
    captures = _capture_all(files, output_dir) if args.capture else {}
    payload = {
        "ok": True,
        "case_id": args.case_id,
        "output_dir": str(output_dir),
        "officecli": {"path": str(officecli), "sha256": _sha256(officecli)},
        "files": {
            editor: {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for editor, path in files.items()
        },
        "openxml": openxml,
        "pdf": pdf,
        "captures": captures,
    }
    report = output_dir / "evidence.json"
    report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps({"ok": True, "evidence": str(report)}, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
