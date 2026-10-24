from __future__ import annotations

from pathlib import Path

import pytest

from lantai.app.mcp.connectors import (
    ChromeDevToolsConnectorAdapter,
    ConnectorAdapter,
    connector_adapter_for_key,
)
from lantai.app.mcp.stateful_client import StdIOStatefulClient
from lantai.config.context import WorkspaceContext, set_current_workspace_context


@pytest.fixture(autouse=True)
def _reset_workspace_context():
    set_current_workspace_context(None)
    yield
    set_current_workspace_context(None)


def _set_workspace(
    root: Path,
    *,
    access_mode: str = "read_write",
    grant_id: str | None = "host-workspace-grant",
) -> None:
    set_current_workspace_context(
        WorkspaceContext(
            agent_home=root / ".agent",
            state_root=root / ".state",
            project_root=root,
            workspace_grant_id=grant_id,
            access_mode=access_mode,
            trust_state="trusted",
        ),
    )


def _prepare(name: str, arguments: dict):
    return ChromeDevToolsConnectorAdapter().prepare(
        name,
        arguments,
        input_schema={"type": "object"},
        read_timeout_seconds=20,
    )


def test_only_reserved_key_selects_the_chrome_adapter() -> None:
    assert isinstance(
        connector_adapter_for_key("chrome-devtools"),
        ChromeDevToolsConnectorAdapter,
    )
    assert type(connector_adapter_for_key("custom")) is ConnectorAdapter
    assert type(connector_adapter_for_key("guanlan")) is not ConnectorAdapter
    assert type(connector_adapter_for_key("officecli")) is not ConnectorAdapter
    assert type(connector_adapter_for_key("openchatcut")) is not ConnectorAdapter


def test_path_free_tool_remains_callable_without_workspace_context() -> None:
    prepared = _prepare("list_pages", {})
    assert prepared.arguments == {}


def test_relative_output_path_is_rewritten_inside_active_workspace(
    tmp_path: Path,
) -> None:
    _set_workspace(tmp_path)

    prepared = _prepare("take_screenshot", {"filePath": "artifacts/page.png"})

    assert prepared.arguments["filePath"] == str(
        (tmp_path / "artifacts" / "page.png").resolve(),
    )


def test_read_only_workspace_cannot_receive_tool_file_output(tmp_path: Path) -> None:
    _set_workspace(tmp_path, access_mode="read_only")

    with pytest.raises(PermissionError, match="read-only workspace"):
        _prepare("performance_stop_trace", {"filePath": "trace.json"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("filePath", "\x00bad"),
        ("filePath", "file:///tmp/secret"),
        ("filePath", "https://example.com/output"),
        ("alternatePath", "result.json"),
    ],
)
def test_invalid_or_unknown_path_fields_are_rejected(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    _set_workspace(tmp_path)

    with pytest.raises(PermissionError):
        _prepare("take_snapshot", {field: value})


def test_nested_path_alias_is_rejected_even_for_dynamic_webmcp_tools(
    tmp_path: Path,
) -> None:
    _set_workspace(tmp_path)

    with pytest.raises(PermissionError, match="not trusted"):
        _prepare(
            "execute_webmcp_tool",
            {"name": "example", "arguments": {"output_file": "secret.txt"}},
        )


def test_file_url_navigation_is_rejected_before_upstream_call(tmp_path: Path) -> None:
    _set_workspace(tmp_path)

    with pytest.raises(PermissionError, match="file URLs"):
        _prepare("new_page", {"url": "file:///etc/passwd"})


def test_input_file_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.heapsnapshot"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "linked.heapsnapshot"
    link.symlink_to(outside)
    _set_workspace(tmp_path)

    with pytest.raises(PermissionError, match="escapes the active workspace"):
        _prepare("get_heapsnapshot_summary", {"filePath": str(link)})


def test_output_parent_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-output"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "linked-output"
    link.symlink_to(outside, target_is_directory=True)
    _set_workspace(tmp_path)

    with pytest.raises(PermissionError, match="escapes the active workspace"):
        _prepare("take_screenshot", {"filePath": str(link / "page.png")})


def test_upload_requires_host_context_grant_not_a_tool_argument(
    tmp_path: Path,
) -> None:
    upload = tmp_path / "upload.txt"
    upload.write_text("upload", encoding="utf-8")
    _set_workspace(tmp_path, grant_id=None)

    with pytest.raises(PermissionError, match="cannot be supplied"):
        _prepare(
            "upload_file",
            {
                "filePaths": [str(upload)],
                "workspace_grant_id": "model-invented",
            },
        )


def test_upload_rewrites_every_granted_file_and_rejects_non_strings(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    _set_workspace(tmp_path)

    prepared = _prepare(
        "upload_file",
        {"filePaths": ["first.txt", str(second)]},
    )
    assert prepared.arguments["filePaths"] == [str(first), str(second)]

    with pytest.raises(PermissionError, match="must be a string"):
        _prepare("upload_file", {"filePaths": [str(first), 42]})


def test_extension_install_requires_an_existing_workspace_directory(
    tmp_path: Path,
) -> None:
    extension = tmp_path / "extension"
    extension.mkdir()
    _set_workspace(tmp_path)

    prepared = _prepare("install_extension", {"path": "extension"})
    assert prepared.arguments["path"] == str(extension)

    with pytest.raises(PermissionError, match="does not exist"):
        _prepare("install_extension", {"path": "missing"})


@pytest.mark.asyncio
async def test_guard_rejection_makes_zero_upstream_calls(tmp_path: Path) -> None:
    class Protocol:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, arguments: dict):
            self.calls.append((name, arguments))
            return {"ok": True}

    protocol = Protocol()
    client = StdIOStatefulClient("Chrome DevTools", "unused")
    client.session = protocol
    client.is_connected = True
    client.allowed_tools = frozenset({"upload_file"})
    client._lantai_client_key = "chrome-devtools"
    _set_workspace(tmp_path, grant_id=None)
    upload = tmp_path / "upload.txt"
    upload.write_text("upload", encoding="utf-8")

    with pytest.raises(PermissionError, match="host-issued workspace grant"):
        await client.call_tool("upload_file", {"filePaths": [str(upload)]})

    assert protocol.calls == []


@pytest.mark.asyncio
async def test_every_argument_guard_fails_before_upstream_dispatch(
    tmp_path: Path,
) -> None:
    class Protocol:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, arguments: dict):
            self.calls.append((name, arguments))
            return {"ok": True}

    upload = tmp_path / "upload.txt"
    upload.write_text("upload", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.heapsnapshot"
    outside.write_text("outside", encoding="utf-8")
    linked = tmp_path / "linked.heapsnapshot"
    linked.symlink_to(outside)
    _set_workspace(tmp_path)
    protocol = Protocol()
    client = StdIOStatefulClient("Chrome DevTools", "unused")
    client.session = protocol
    client.is_connected = True
    client._lantai_client_key = "chrome-devtools"
    cases = (
        ("take_snapshot", {"alternatePath": "result.txt"}),
        ("take_snapshot", {"filePath": "bad\x00path"}),
        ("take_screenshot", {"filePath": "../escape.png"}),
        ("new_page", {"url": "file:///etc/passwd"}),
        ("get_heapsnapshot_summary", {"filePath": str(linked)}),
        (
            "execute_webmcp_tool",
            {"name": "dynamic", "arguments": {"output_file": "escape.txt"}},
        ),
        (
            "upload_file",
            {
                "filePaths": [str(upload)],
                "workspace_grant_id": "model-invented",
            },
        ),
        ("upload_file", {"filePaths": [str(upload), 42]}),
    )
    client.allowed_tools = frozenset(name for name, _args in cases)

    for name, arguments in cases:
        with pytest.raises(PermissionError):
            await client.call_tool(name, arguments)

    assert protocol.calls == []
