from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from urllib import error


SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _module():
    _load(
        "packaged_app_lifecycle_smoke",
        SCRIPTS_DIR / "packaged_app_lifecycle_smoke.py",
    )
    _load(
        "packaged_app_rapid_lifecycle_smoke",
        SCRIPTS_DIR / "packaged_app_rapid_lifecycle_smoke.py",
    )
    return _load(
        "run_packaged_agent_sustained_soak",
        SCRIPTS_DIR / "run_packaged_agent_sustained_soak.py",
    )


def test_scheduled_offsets_include_start_and_exact_end() -> None:
    module = _module()

    assert module._scheduled_offsets(14_400, 600) == [
        float(value) for value in range(0, 14_401, 600)
    ]


def test_scheduled_offsets_include_partial_final_interval() -> None:
    module = _module()

    assert module._scheduled_offsets(5, 2) == [0.0, 2, 4, 5]


def test_record_artifact_is_hash_bound(tmp_path: Path) -> None:
    module = _module()

    proof = module._write_record_artifact(
        tmp_path, "record.json", {"status": "verified"}
    )

    assert Path(proof["path"]).is_file()
    assert len(proof["sha256"]) == 64


def test_ui_convergence_uses_all_view_for_terminal_runs(monkeypatch) -> None:
    module = _module()
    requested_urls: list[str] = []

    monkeypatch.setattr(
        module,
        "_find_runtime_run_by_id",
        lambda *_args, **_kwargs: {"authority_status": "cancelled"},
    )

    def fake_json_request(_method, url, **_kwargs):
        requested_urls.append(url)
        return 200, "{}", {
            "items": [
                {"id": "run-terminal", "authority_status": "cancelled"}
            ]
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)

    result = module._wait_for_ui_convergence(
        {"api_base_url": "http://127.0.0.1:1", "desktop_token": "test"},
        run_id="run-terminal",
        timeout=1,
    )

    assert result["authority_state"] == "cancelled"
    assert "view=all" in requested_urls[0]


def test_light_transport_recovery_retries_transient_503(monkeypatch) -> None:
    module = _module()
    calls = 0

    def exercise(_state, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error.HTTPError("http://127.0.0.1", 503, "restart", {}, io.BytesIO())
        return "console:recovered", "chat-recovered"

    recovered = {"sequence": 2, "backend_pid": 222}
    monkeypatch.setattr(module, "_exercise_console_model_success_path", exercise)
    monkeypatch.setattr(module, "_wait_for_state", lambda *_args, **_kwargs: recovered)
    monkeypatch.setattr(module, "_assert_desktop_readiness", lambda *_args, **_kwargs: 222)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    state, session_id, chat_id, incidents = module._exercise_light_with_transport_recovery(
        {"sequence": 1, "backend_pid": 111},
        state_path=Path("state.json"),
        process=object(),
        timeout=1,
    )

    assert state is recovered
    assert (session_id, chat_id) == ("console:recovered", "chat-recovered")
    assert incidents[0]["observed_failure"] == "http_503"
    assert incidents[0]["previous_backend_pid"] == 111
