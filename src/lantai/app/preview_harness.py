# -*- coding: utf-8 -*-
"""Local preview inspection harness for vibe-coding workflows."""

from __future__ import annotations

import asyncio
import html
import re
import time
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

LOCAL_PREVIEW_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
MAX_BODY_BYTES = 500_000
MAX_ASSETS = 16
DEFAULT_TIMEOUT_SECONDS = 4.0
PLAYWRIGHT_TIMEOUT_MS = 6_000


PreviewFindingSeverity = Literal["critical", "major", "minor"]


class PreviewHarnessFinding(BaseModel):
    severity: PreviewFindingSeverity
    category: str
    message: str
    evidence: str = ""
    url: str | None = None


class PreviewNetworkError(BaseModel):
    url: str
    status: int | None = None
    error: str = ""


class PreviewHarnessReport(BaseModel):
    url: str
    fetch_url: str
    ok: bool
    summary: str
    status_code: int | None = None
    content_type: str = ""
    duration_ms: int = 0
    body_bytes: int = 0
    body_text_length: int = 0
    root_selector_present: bool = False
    white_screen_likely: bool = False
    browser_engine: str = "none"
    screenshot_path: str | None = None
    console_errors: list[str] = Field(default_factory=list)
    page_errors: list[str] = Field(default_factory=list)
    network_errors: list[PreviewNetworkError] = Field(default_factory=list)
    findings: list[PreviewHarnessFinding] = Field(default_factory=list)


def is_local_preview_url(value: str) -> bool:
    """Return whether a URL is safe for the local preview harness to fetch."""

    try:
        parsed = urlparse(value)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return host in LOCAL_PREVIEW_HOSTS


def normalize_preview_fetch_url(value: str) -> str:
    """Normalize dev-server wildcard hosts to a concrete loopback URL."""

    parsed = urlparse(value)
    if (parsed.hostname or "").lower() != "0.0.0.0":
        return value
    netloc = "127.0.0.1"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _short_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}..."


def _fetch_url(url: str, timeout_seconds: float) -> dict[str, Any]:
    started = time.perf_counter()
    request = Request(
        url,
        headers={
            "User-Agent": "lantai-preview-harness/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
            body = response.read(MAX_BODY_BYTES + 1)
            return {
                "status_code": int(response.status),
                "content_type": response.headers.get("content-type", ""),
                "body": body[:MAX_BODY_BYTES],
                "truncated": len(body) > MAX_BODY_BYTES,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "error": "",
            }
    except HTTPError as exc:
        body = exc.read(MAX_BODY_BYTES)
        return {
            "status_code": int(exc.code),
            "content_type": exc.headers.get("content-type", "") if exc.headers else "",
            "body": body,
            "truncated": False,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "error": str(exc),
        }
    except (OSError, URLError) as exc:
        return {
            "status_code": None,
            "content_type": "",
            "body": b"",
            "truncated": False,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "error": str(exc),
        }


ASSET_RE = re.compile(
    r"""(?:src|href)\s*=\s*["'](?P<url>[^"']+)["']""",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")


def _same_origin(a: str, b: str) -> bool:
    parsed_a = urlparse(a)
    parsed_b = urlparse(b)
    return (
        parsed_a.scheme,
        parsed_a.hostname,
        parsed_a.port,
    ) == (
        parsed_b.scheme,
        parsed_b.hostname,
        parsed_b.port,
    )


def _extract_asset_urls(base_url: str, text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in ASSET_RE.finditer(text):
        raw = html.unescape(match.group("url")).strip()
        if not raw or raw.startswith(("#", "data:", "mailto:", "tel:", "blob:")):
            continue
        absolute = urljoin(base_url, raw)
        if not _same_origin(base_url, absolute):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
        if len(urls) >= MAX_ASSETS:
            break
    return urls


def _visible_text_length(markup: str) -> int:
    without_scripts = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        " ",
        markup,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = TAG_RE.sub(" ", without_scripts)
    text = html.unescape(text)
    return len(" ".join(text.split()))


def _has_root_selector(markup: str) -> bool:
    return bool(
        re.search(
            r"""id\s*=\s*["'](?:root|app|__next|__nuxt|svelte)["']""",
            markup,
            flags=re.IGNORECASE,
        ),
    )


async def _inspect_with_playwright(
    url: str,
    *,
    workspace_dir: Path | None,
) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception:
        return {"browser_engine": "unavailable"}

    console_errors: list[str] = []
    page_errors: list[str] = []
    network_errors: list[dict[str, Any]] = []
    screenshot_path: str | None = None
    text_length: int | None = None

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1365, "height": 900})

            def on_console(msg: Any) -> None:
                if msg.type in {"error", "warning"}:
                    console_errors.append(_short_text(msg.text, 360))

            def on_page_error(exc: Any) -> None:
                page_errors.append(_short_text(exc, 360))

            def on_request_failed(request: Any) -> None:
                failure = request.failure or ""
                error_text = (
                    failure.get("errorText")
                    if isinstance(failure, dict)
                    else str(failure)
                )
                network_errors.append(
                    {
                        "url": request.url,
                        "status": None,
                        "error": _short_text(error_text or "request failed"),
                    },
                )

            page.on("console", on_console)
            page.on("pageerror", on_page_error)
            page.on("requestfailed", on_request_failed)
            await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
            try:
                text_length = len(await page.locator("body").inner_text(timeout=1000))
            except Exception:
                text_length = None

            if workspace_dir is not None:
                screenshots_dir = workspace_dir / ".lantai" / "preview-harness"
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                screenshot_path = str(
                    screenshots_dir
                    / f"preview-{int(time.time() * 1000)}.png",
                )
                await page.screenshot(path=screenshot_path, full_page=True)
            await browser.close()
    except Exception as exc:
        page_errors.append(_short_text(exc, 360))
        return {
            "browser_engine": "playwright",
            "console_errors": console_errors,
            "page_errors": page_errors,
            "network_errors": network_errors,
            "screenshot_path": screenshot_path,
            "body_text_length": text_length,
        }

    return {
        "browser_engine": "playwright",
        "console_errors": console_errors,
        "page_errors": page_errors,
        "network_errors": network_errors,
        "screenshot_path": screenshot_path,
        "body_text_length": text_length,
    }


async def inspect_preview_url(
    url: str,
    *,
    workspace_dir: str | Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    enable_browser: bool = True,
) -> PreviewHarnessReport:
    """Inspect a local frontend preview URL with bounded network/browser checks."""

    findings: list[PreviewHarnessFinding] = []
    if not is_local_preview_url(url):
        finding = PreviewHarnessFinding(
            severity="critical",
            category="security",
            message="预览诊断只允许访问本机 localhost / 127.0.0.1 / ::1 URL。",
            evidence=url,
            url=url,
        )
        return PreviewHarnessReport(
            url=url,
            fetch_url=url,
            ok=False,
            summary=finding.message,
            findings=[finding],
        )

    fetch_url = normalize_preview_fetch_url(url)
    fetched = await asyncio.to_thread(_fetch_url, fetch_url, timeout_seconds)
    status_code = fetched["status_code"]
    content_type = str(fetched["content_type"] or "")
    body = fetched["body"] or b""
    markup = body.decode("utf-8", errors="replace")
    body_text_length = _visible_text_length(markup) if body else 0
    root_selector_present = _has_root_selector(markup)
    white_screen_likely = bool(
        status_code is not None
        and status_code < 400
        and "html" in content_type.lower()
        and body_text_length < 20
        and not root_selector_present
    )

    network_errors: list[PreviewNetworkError] = []
    if fetched["error"] and (status_code is None or status_code >= 400):
        network_errors.append(
            PreviewNetworkError(
                url=fetch_url,
                status=status_code,
                error=_short_text(fetched["error"]),
            ),
        )

    if status_code is None:
        findings.append(
            PreviewHarnessFinding(
                severity="critical",
                category="availability",
                message="本地预览不可达。",
                evidence=_short_text(fetched["error"]),
                url=fetch_url,
            ),
        )
    elif status_code >= 400:
        findings.append(
            PreviewHarnessFinding(
                severity="critical",
                category="availability",
                message=f"本地预览返回 HTTP {status_code}。",
                evidence=_short_text(fetched["error"]),
                url=fetch_url,
            ),
        )

    if status_code is not None and status_code < 400 and "html" in content_type.lower():
        asset_urls = _extract_asset_urls(fetch_url, markup)
        for asset_url in asset_urls:
            asset = await asyncio.to_thread(_fetch_url, asset_url, min(timeout_seconds, 2.0))
            asset_status = asset["status_code"]
            if asset_status is None or asset_status >= 400:
                network_errors.append(
                    PreviewNetworkError(
                        url=asset_url,
                        status=asset_status,
                        error=_short_text(asset["error"] or f"HTTP {asset_status}"),
                    ),
                )
        if network_errors:
            findings.append(
                PreviewHarnessFinding(
                    severity="major",
                    category="network",
                    message=f"预览页面有 {len(network_errors)} 个资源请求失败。",
                    evidence=network_errors[0].error or str(network_errors[0].status),
                    url=network_errors[0].url,
                ),
            )
        if white_screen_likely:
            findings.append(
                PreviewHarnessFinding(
                    severity="major",
                    category="render",
                    message="页面正文几乎为空，且未发现常见前端挂载节点，可能白屏。",
                    evidence=f"body_text_length={body_text_length}",
                    url=fetch_url,
                ),
            )

    browser_engine = "none"
    screenshot_path: str | None = None
    console_errors: list[str] = []
    page_errors: list[str] = []
    if enable_browser and status_code is not None and status_code < 400:
        browser_result = await _inspect_with_playwright(
            fetch_url,
            workspace_dir=Path(workspace_dir).expanduser() if workspace_dir else None,
        )
        browser_engine = str(browser_result.get("browser_engine") or "none")
        console_errors = list(browser_result.get("console_errors") or [])
        page_errors = list(browser_result.get("page_errors") or [])
        screenshot_path = browser_result.get("screenshot_path") or None
        browser_text_length = browser_result.get("body_text_length")
        if isinstance(browser_text_length, int):
            body_text_length = browser_text_length
        for raw in browser_result.get("network_errors") or []:
            if isinstance(raw, dict):
                network_errors.append(PreviewNetworkError.model_validate(raw))

    if console_errors:
        findings.append(
            PreviewHarnessFinding(
                severity="major",
                category="console",
                message=f"浏览器控制台有 {len(console_errors)} 条错误或警告。",
                evidence=console_errors[0],
                url=fetch_url,
            ),
        )
    if page_errors:
        findings.append(
            PreviewHarnessFinding(
                severity="critical",
                category="pageerror",
                message="页面运行时抛出了未捕获异常。",
                evidence=page_errors[0],
                url=fetch_url,
            ),
        )

    ok = not any(finding.severity in {"critical", "major"} for finding in findings)
    summary = (
        "预览诊断通过。"
        if ok
        else _short_text(findings[0].message or "预览诊断发现问题。", 120)
    )
    return PreviewHarnessReport(
        url=url,
        fetch_url=fetch_url,
        ok=ok,
        summary=summary,
        status_code=status_code,
        content_type=content_type,
        duration_ms=int(fetched["duration_ms"] or 0),
        body_bytes=len(body),
        body_text_length=body_text_length,
        root_selector_present=root_selector_present,
        white_screen_likely=white_screen_likely,
        browser_engine=browser_engine,
        screenshot_path=screenshot_path,
        console_errors=console_errors[:8],
        page_errors=page_errors[:8],
        network_errors=network_errors[:12],
        findings=findings[:12],
    )
