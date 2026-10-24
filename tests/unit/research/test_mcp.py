from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.research import mcp


def _installation(tmp_path, monkeypatch, *, commit=None, dirty=''):
    checkout = tmp_path / 'paper-search'
    (checkout / 'paper_search_mcp').mkdir(parents=True)
    (checkout / 'paper_search_mcp/server.py').write_text('# pinned server')
    interpreter = tmp_path / 'python'
    interpreter.write_text('# isolated interpreter')
    interpreter.chmod(0o700)
    calls = []

    def git(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=(commit or mcp.PAPER_SEARCH_COMMIT) + '\n'
            if 'rev-parse' in command else dirty, stderr='')

    monkeypatch.setattr(mcp.subprocess, 'run', git)
    return checkout, interpreter, calls


def test_admitted_mcp_uses_isolated_interpreter_and_two_read_only_tools(tmp_path, monkeypatch):
    checkout, interpreter, calls = _installation(tmp_path, monkeypatch)
    config = mcp.build_paper_search_config(checkout, interpreter)
    assert not config.enabled  # User may explicitly enable it in the existing MCP control plane.
    assert config.transport == 'stdio'
    assert config.command == str(interpreter)
    assert config.cwd == str(checkout)
    assert config.args == ['-m', 'paper_search_mcp.server', '--transport', 'stdio']
    assert config.allowed_tools == ['search_crossref', 'get_crossref_paper_by_doi']
    assert config.env == {'PAPER_SEARCH_MCP_SEARCH_CACHE_ENABLED': '0'}
    assert all(not kwargs.get('shell') and kwargs['timeout'] == 10 for _, kwargs in calls)


@pytest.mark.parametrize('commit,dirty', [('0' * 40, ''), (None, ' M paper_search_mcp/server.py'),
    (None, '?? paper_search_mcp/local_override.py')])
def test_changed_or_unpinned_mcp_checkout_is_not_admitted(tmp_path, monkeypatch, commit, dirty):
    checkout, interpreter, _ = _installation(tmp_path, monkeypatch, commit=commit, dirty=dirty)
    with pytest.raises(ValueError, match='pinned|modified'):
        mcp.build_paper_search_config(checkout, interpreter, enabled=True)


def test_missing_installation_cannot_create_active_connector(tmp_path):
    with pytest.raises(ValueError, match='installation'):
        mcp.build_paper_search_config(tmp_path / 'missing', Path('/missing/python'), enabled=True)
