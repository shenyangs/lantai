"""Fixed-operation analysis child for source and frozen desktop runtimes.

The child accepts only an in-memory CSV/XLSX blob plus a validated operation
name. It never evaluates model-supplied code, SQL, or file paths. This is a
bounded process boundary, not a claim of OS-level sandboxing.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .service import AnalysisError, MAX_SOURCE_BYTES, _aggregate, _parse_table, _profile, _validate_plan

MAX_REQUEST_BYTES = 29 * 1024 * 1024
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
WORKER_TIMEOUT_SECONDS = 30.0
_WORKER_SLOTS = threading.BoundedSemaphore(2)


def _child_environment() -> dict[str, str]:
    """Keep only process-launch essentials; do not pass Provider credentials."""

    allowed = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT", "WINDIR")
    return {
        **{key: os.environ[key] for key in allowed if key in os.environ},
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANTAI_ANALYSIS_WORKER_CHILD": "1",
    }


def _command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--analysis-worker"]
    return [sys.executable, "-m", "lantai.analysis.worker"]


def run_worker(
    operation: str,
    *,
    blob: bytes,
    suffix: str,
    sheet_name: str | None,
    plan: dict[str, Any] | None = None,
    timeout_seconds: float = WORKER_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Launch one short-lived analysis child with a wall-clock deadline."""

    if operation not in {"ingest", "compute"}:
        raise AnalysisError("Unsupported analysis worker operation")
    if not isinstance(blob, bytes) or len(blob) > MAX_SOURCE_BYTES or suffix not in {".csv", ".xlsx"}:
        raise AnalysisError("Invalid analysis worker source")
    if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= WORKER_TIMEOUT_SECONDS:
        raise AnalysisError("Invalid analysis worker timeout")
    if operation == "compute" and not isinstance(plan, dict):
        raise AnalysisError("Analysis worker needs a structured plan")
    request = json.dumps({
        "operation": operation,
        "blob": base64.b64encode(blob).decode("ascii"),
        "suffix": suffix,
        "sheet_name": sheet_name,
        "plan": plan,
    }, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(request) > MAX_REQUEST_BYTES:
        raise AnalysisError("Analysis worker request exceeds the safety limit")
    deadline = time.monotonic() + timeout_seconds
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise AnalysisError("Analysis worker cancelled before launch")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AnalysisError("Analysis worker timed out while waiting for a worker slot")
        if _WORKER_SLOTS.acquire(timeout=min(remaining, 0.1)):
            break
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise AnalysisError("Analysis worker cancelled before launch")
        try:
            process = subprocess.Popen(
                _command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=_child_environment(),
                cwd=str(Path(__file__).resolve().parent),
            )
        except OSError as exc:
            raise AnalysisError("Analysis worker could not start") from exc
        first_input: bytes | None = request
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise AnalysisError("Analysis worker cancelled; the result was not saved")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AnalysisError("Analysis worker timed out; the result was not saved")
                try:
                    stdout, _stderr = process.communicate(
                        input=first_input, timeout=min(remaining, 0.1 if cancel_event is not None else remaining),
                    )
                    break
                except subprocess.TimeoutExpired:
                    first_input = None
            if cancel_event is not None and cancel_event.is_set():
                raise AnalysisError("Analysis worker cancelled; the result was not saved")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    finally:
        _WORKER_SLOTS.release()
    if process.returncode != 0 or len(stdout) > MAX_RESPONSE_BYTES:
        raise AnalysisError("Analysis worker failed; the result was not saved")
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisError("Analysis worker returned an invalid response") from exc
    if not isinstance(response, dict) or response.get("schema_version") != 1:
        raise AnalysisError("Analysis worker returned an invalid response")
    if response.get("ok") is False:
        raise AnalysisError(str(response.get("error", "Analysis worker rejected the request"))[:300])
    value = response.get("value")
    if response.get("ok") is not True or not isinstance(value, dict):
        raise AnalysisError("Analysis worker returned an invalid response")
    return value


def _execute(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != {"operation", "blob", "suffix", "sheet_name", "plan"}:
        raise AnalysisError("Invalid analysis worker request")
    operation = request["operation"]
    suffix = request["suffix"]
    if operation not in {"ingest", "compute"} or suffix not in {".csv", ".xlsx"}:
        raise AnalysisError("Unsupported analysis worker operation")
    try:
        blob = base64.b64decode(request["blob"], validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise AnalysisError("Invalid analysis worker source") from exc
    if len(blob) > MAX_SOURCE_BYTES:
        raise AnalysisError("Analysis worker source exceeds the safety limit")
    columns, rows, chosen_sheet = _parse_table(blob, suffix, request["sheet_name"])
    if operation == "ingest":
        return {"columns": columns, "row_count": len(rows), "profile": _profile(columns, rows), "sheet_name": chosen_sheet, "worker_pid": os.getpid()}
    normalized = _validate_plan(request["plan"], columns)
    output, warnings = _aggregate(rows, normalized)
    return {"columns": columns, "row_count": len(rows), "rows": output, "warnings": warnings, "plan": normalized, "worker_pid": os.getpid()}


def main() -> int:
    """Internal frozen/source process entry; stdin and stdout are the protocol."""

    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise AnalysisError("Analysis worker request exceeds the safety limit")
        result = _execute(json.loads(raw))
        response = {"schema_version": 1, "ok": True, "value": result}
    except AnalysisError as exc:
        response = {"schema_version": 1, "ok": False, "error": str(exc)[:300]}
    except Exception:
        response = {"schema_version": 1, "ok": False, "error": "Analysis worker failed to parse or calculate"}
    encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        encoded = b'{"schema_version":1,"ok":false,"error":"Analysis result exceeds the safety limit"}'
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
