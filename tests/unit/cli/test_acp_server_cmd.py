from click.testing import CliRunner

from lantai.cli import acp_server_cmd
from lantai.cli.main import cli


def test_acp_command_is_registered_with_server_options() -> None:
    result = CliRunner().invoke(cli, ["acp", "--help"])

    assert result.exit_code == 0
    assert "--base-url" in result.output
    assert "--agent" in result.output
    assert "--task-timeout" in result.output
    assert "ACP-compatible IDEs" in result.output
    assert "pair" in result.output


def test_acp_pair_subcommand_saves_session_without_starting_stdio(
    monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeBridge:
        def __init__(self, *, base_url: str) -> None:
            calls.append(("init", base_url))

        async def pair(self, code: str) -> int:
            calls.append(("pair", code))
            return 1_900_000_000

        async def aclose(self) -> None:
            calls.append(("close", True))

    monkeypatch.setattr(acp_server_cmd, "LantaiAcpBridge", FakeBridge)

    result = CliRunner().invoke(
        cli,
        [
            "--host",
            "127.0.0.1",
            "--port",
            "8123",
            "acp",
            "pair",
            "ABCD-EFGH",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        ("init", "http://127.0.0.1:8123"),
        ("pair", "ABCD-EFGH"),
        ("close", True),
    ]
    assert "pairing saved" in result.output
