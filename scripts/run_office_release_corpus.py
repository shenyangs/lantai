#!/usr/bin/env python3
"""Build five versioned, independently verified Office cases for the 2.0 gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_office_first_tier_e2e.py"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--officecli",
        type=Path,
        help="OfficeCLI executable used for every corpus case.",
    )
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty corpus: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    for index in range(1, 6):
        case_id = f"case-{index:02d}"
        case_dir = output_dir / case_id
        command = [
            sys.executable,
            str(RUNNER),
            "--output-dir",
            str(case_dir),
            "--case-id",
            case_id,
        ]
        if args.officecli is not None:
            command.extend(["--officecli", str(args.officecli.expanduser().resolve(strict=True))])
        if args.capture:
            command.append("--capture")
        subprocess.run(command, cwd=ROOT, check=True)
        evidence_path = case_dir / "evidence.json"
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        cases.append(
            {
                "case_id": case_id,
                "evidence": str(evidence_path),
                "evidence_sha256": _sha256(evidence_path),
                "payload": payload,
            }
        )
    report = {
        "schema": "com.lantai.office-release-corpus.v1",
        "ok": True,
        "case_count": len(cases),
        "cases": cases,
    }
    report_path = output_dir / "evidence.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "evidence": str(report_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
