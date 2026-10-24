# -*- coding: utf-8 -*-

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lantai.app.preview_harness import (
    inspect_preview_url,
    is_local_preview_url,
    normalize_preview_fetch_url,
)


class _PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/":
            body = (
                "<!doctype html><html><body><div id='root'></div>"
                "<script src='/ok.js'></script>"
                "<script src='/missing.js'></script>"
                "</body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/ok.js":
            body = b"console.log('ok')"
            self.send_response(200)
            self.send_header("content-type", "application/javascript")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        return


@pytest.fixture
def preview_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PreviewHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


def test_preview_harness_url_safety():
    assert is_local_preview_url("http://localhost:5173/")
    assert is_local_preview_url("http://127.0.0.1:5173/")
    assert not is_local_preview_url("https://example.com")
    assert (
        normalize_preview_fetch_url("http://0.0.0.0:5173/")
        == "http://127.0.0.1:5173/"
    )


@pytest.mark.asyncio
async def test_preview_harness_finds_failed_same_origin_assets(preview_server):
    report = await inspect_preview_url(preview_server, enable_browser=False)

    assert report.status_code == 200
    assert report.root_selector_present is True
    assert report.ok is False
    assert report.findings[0].category == "network"
    assert any(error.url.endswith("/missing.js") for error in report.network_errors)


@pytest.mark.asyncio
async def test_preview_harness_blocks_remote_urls():
    report = await inspect_preview_url("https://example.com", enable_browser=False)

    assert report.ok is False
    assert report.findings[0].category == "security"
    assert report.network_errors == []
