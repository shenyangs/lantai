# -*- coding: utf-8 -*-
"""Behavior locks for the generic ``lantai mcp`` control seam."""

from unittest.mock import Mock, patch

from click.testing import CliRunner

from lantai.cli.main import cli


def _response(payload, status_code=200):
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    response.status_code = status_code
    return response


def test_mcp_list_uses_console_endpoint_and_json_output() -> None:
    http = Mock()
    http.__enter__ = Mock(return_value=http)
    http.__exit__ = Mock(return_value=False)
    http.request.return_value = _response([{"key": "docs", "enabled": True}])

    with patch("lantai.cli.mcp_cmd.client", return_value=http):
        result = CliRunner().invoke(cli, ["mcp", "list"])

    assert result.exit_code == 0
    assert '"key": "docs"' in result.output
    http.request.assert_called_once_with("GET", "/mcp", json=None)


def test_mcp_add_accepts_inline_json() -> None:
    http = Mock()
    http.__enter__ = Mock(return_value=http)
    http.__exit__ = Mock(return_value=False)
    http.request.return_value = _response({"key": "docs"}, 201)

    with patch("lantai.cli.mcp_cmd.client", return_value=http):
        result = CliRunner().invoke(
            cli,
            [
                "mcp",
                "add",
                "docs",
                "--config",
                '{"name":"Docs","transport":"stdio","command":"docs-mcp"}',
            ],
        )

    assert result.exit_code == 0
    _, kwargs = http.request.call_args
    assert kwargs["json"]["client_key"] == "docs"
    assert kwargs["json"]["client"]["command"] == "docs-mcp"


def test_mcp_remove_requires_confirmation() -> None:
    result = CliRunner().invoke(cli, ["mcp", "remove", "docs"], input="n\n")

    assert result.exit_code != 0
    assert "Aborted" in result.output
