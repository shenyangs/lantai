"""Keep former Quick Search credential and accounting guarantees after retirement."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from lantai.app.web_search import WebSearchStore
from lantai.security.secret_store import encrypt

pytestmark = pytest.mark.usefixtures("isolated_web_search_secrets")


def test_web_credential_is_encrypted_and_preserved_on_blank_save(tmp_path):
    store = WebSearchStore(path=tmp_path / "web_search.json")
    store.save(api_key="sk-web-only", enabled=True)
    store.save(api_key="", enabled=False)
    assert store.load_api_key() == "sk-web-only"
    assert not store.status().enabled
    assert "sk-web-only" not in store.path.read_text()
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_legacy_plaintext_is_encrypted_only_in_new_store(tmp_path):
    legacy = tmp_path / "deepseek_quick_search.json"
    legacy.write_text(json.dumps({"api_key": "sk-legacy", "enabled": False}))
    store = WebSearchStore(path=tmp_path / "web_search.json")
    store.save(api_key="", enabled=False)
    assert "sk-legacy" not in store.path.read_text()
    assert json.loads(legacy.read_text())["api_key"] == "sk-legacy"


def test_new_counts_are_atomic_across_agent_store_instances(tmp_path):
    path = tmp_path / "web_search.json"
    store = WebSearchStore(path=path)
    store.save(api_key="sk-web-only", enabled=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: WebSearchStore(path=path).record_request("search"),
                range(20),
            )
        )
    assert store.status().search_count == 20
    assert store.status().fetch_count == 0
    assert store.status().last_used_at


def test_unreadable_legacy_key_fails_closed_and_can_be_replaced(tmp_path):
    legacy = tmp_path / "deepseek_quick_search.json"
    legacy.write_text(
        json.dumps(
            {"api_key": encrypt("sk-old")[:-5] + "bad", "enabled": True}
        )
    )
    store = WebSearchStore(path=tmp_path / "web_search.json")
    assert store.status().credential_error
    assert not store.status().enabled
    assert store.save(api_key="sk-new", enabled=True).enabled
