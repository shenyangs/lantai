# -*- coding: utf-8 -*-
"""Persistent local effect service used by process-crash integration tests.

The service deliberately runs outside the Runtime worker process.  Each
mutation is fsynced before the response so a killed worker cannot erase proof
that the external side effect already happened.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from lantai.utils.authoritative_store import fsync_directory


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mutations": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _handler(state_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path != "/mutate":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length) or b"{}")
            state = _read_json(state_path)
            mutations = list(state.get("mutations") or [])
            receipt = {
                "effect_id": str(payload.get("effect_id") or ""),
                "operation_id": str(payload.get("operation_id") or ""),
                "attempt_id": str(payload.get("attempt_id") or ""),
                "tool_call_id": str(payload.get("tool_call_id") or ""),
                "idempotency_key": str(payload.get("idempotency_key") or ""),
                "nonce": str(payload.get("nonce") or ""),
                "ordinal": len(mutations) + 1,
            }
            mutations.append(receipt)
            state = {"mutations": mutations}
            _write_json(state_path, state)
            hold_response_barrier = str(
                payload.get("hold_response_barrier") or "",
            )
            if hold_response_barrier:
                barrier_metadata = payload.get("barrier_metadata")
                _write_json(
                    Path(hold_response_barrier),
                    {
                        "service_pid": os.getpid(),
                        "target_worker_pid": int(payload["worker_pid"]),
                        "nonce": str(payload.get("nonce") or ""),
                        "scenario": "during_external_response",
                        **(
                            dict(barrier_metadata)
                            if isinstance(barrier_metadata, dict)
                            else {}
                        ),
                    },
                )
                threading.Event().wait()
            self._send_json(
                {
                    "accepted": True,
                    "count": len(mutations),
                    "receipt": receipt,
                },
            )

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            if parsed.path == "/snapshot":
                self._send_json(_read_json(state_path))
                return
            if parsed.path == "/probe":
                operation_id = str(
                    (parse_qs(parsed.query).get("operation_id") or [""])[0],
                )
                mutation = next(
                    (
                        item
                        for item in _read_json(state_path).get("mutations")
                        or []
                        if str(item.get("operation_id") or "") == operation_id
                    ),
                    None,
                )
                if mutation is None:
                    self.send_error(404)
                    return
                self._send_json(
                    {
                        "committed": True,
                        "operation_id": operation_id,
                        "receipt": mutation,
                    },
                )
                return
            self.send_error(404)

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send_json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(args.state_file))
    port = int(server.server_address[1])
    _write_json(
        args.ready_file,
        {"pid": os.getpid(), "url": f"http://127.0.0.1:{port}"},
    )
    server.serve_forever(poll_interval=0.05)


if __name__ == "__main__":
    main()
