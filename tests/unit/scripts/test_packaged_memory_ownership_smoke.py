from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "packaged_memory_ownership_smoke.py"
)


def _load_module():
    scripts_dir = str(SCRIPT.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(
        "packaged_memory_ownership_smoke",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pending_fact_never_enters_projection_and_forget_writes_tombstone(
    monkeypatch,
) -> None:
    module = _load_module()
    calls: list[tuple[str, str]] = []
    state = {
        "api_base_url": "http://127.0.0.1:43123",
        "desktop_token": "test-token",
    }

    def fake_request(method, url, **_kwargs):
        calls.append((method, url))
        if "memory-facts?status=pending" in url:
            return 200, "", {
                "facts": [
                    {
                        "id": "pending-1",
                        "content": module.PENDING_SENTINEL,
                        "status": "pending",
                    }
                ]
            }
        if method == "POST" and url.endswith("/user-memory"):
            return 200, "", {
                "entry": {
                    "id": "entry-1",
                    "confirmed": True,
                    "canonical_fact_id": "fact-1",
                },
                "ledger_fact_id": "fact-1",
            }
        if "memory-facts?status=confirmed" in url:
            deleted = any(method == "DELETE" for method, _url in calls)
            return 200, "", {"facts": [] if deleted else [{"id": "fact-1"}]}
        if "memory-facts?status=forgotten" in url:
            return 200, "", {"facts": [{"id": "fact-1"}]}
        if method == "DELETE":
            return 200, "", {"deleted": True}
        if "/user-memory?include_archived=true" in url:
            created = any(method == "POST" for method, _url in calls)
            deleted = any(method == "DELETE" for method, _url in calls)
            return 200, "", {
                "entries": (
                    []
                    if not created or deleted
                    else [
                        {
                            "id": "entry-1",
                            "confirmed": True,
                            "canonical_fact_id": "fact-1",
                        }
                    ]
                )
            }
        raise AssertionError((method, url))

    monkeypatch.setattr(module, "_json_request", fake_request)
    summary = module._exercise_initial_memory_ownership(
        state,
        pending_fact_id="pending-1",
        timeout=1,
    )

    assert summary["pending_projection_absent"] is True
    assert summary["after_delete"] == {
        "confirmed_fact_absent": True,
        "canonical_forget_tombstone_present": True,
        "structured_projection_absent": True,
    }


def test_packaged_memory_smoke_cli_is_update_free(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()
    received = {}
    monkeypatch.setattr(
        module,
        "_run",
        lambda args: (
            received.update(vars(args))
            or {"app": {"short_version": "2.0.0"}}
        ),
    )

    assert module.main(["--app", str(tmp_path), "--timeout", "12"]) == 0
    assert received["timeout"] == 12
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"LANTAI_DISABLE_GITHUB_UPDATER": "1"' in source
    assert "packaged_auto_update" not in source


def test_model_context_includes_only_confirmed_then_removes_forgotten(
    monkeypatch,
) -> None:
    module = _load_module()
    state = {
        "api_base_url": "http://127.0.0.1:43123",
        "desktop_token": "test-token",
    }
    deleted = False

    class FakeServer:
        request_payloads = []

        @property
        def request_count(self):
            return len(self.request_payloads)

    server = FakeServer()

    def fake_request(method, url, **_kwargs):
        nonlocal deleted
        if method == "POST" and url.endswith("/user-memory"):
            return 200, "", {
                "entry": {"id": "entry-model", "confirmed": True},
                "ledger_fact_id": "fact-model",
            }
        if method == "DELETE":
            deleted = True
            return 200, "", {"deleted": True}
        if "memory-facts?status=confirmed" in url:
            return 200, "", {"facts": [] if deleted else [{"id": "fact-model"}]}
        if "memory-facts?status=forgotten" in url:
            return 200, "", {"facts": [{"id": "fact-model"}] if deleted else []}
        if "/user-memory?include_archived=true" in url:
            return 200, "", {
                "entries": [] if deleted else [{"id": "entry-model"}]
            }
        raise AssertionError((method, url))

    def fake_probe(_state, *, timeout):
        assert timeout == 1
        content = (
            "ordinary prompt"
            if deleted
            else f"ordinary prompt {module.MODEL_CONTEXT_SENTINEL}"
        )
        server.request_payloads.append(
            {"messages": [{"role": "system", "content": content}]}
        )

    monkeypatch.setattr(module, "_json_request", fake_request)
    monkeypatch.setattr(module, "_send_memory_probe_turn", fake_probe)
    monkeypatch.setattr(module, "_wait_for_provider_quiet", lambda _server: None)

    summary = module._exercise_packaged_model_memory_context(
        state,
        server=server,
        timeout=1,
    )

    assert summary["confirmed_present_before_forget"] is True
    assert summary["confirmed_absent_after_forget"] is True
    assert summary["pending_absent_before_forget"] is True
    assert summary["pending_absent_after_forget"] is True
