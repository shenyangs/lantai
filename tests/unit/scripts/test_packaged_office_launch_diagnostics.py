import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "packaged_office_launch_diagnostics",
    ROOT / "scripts" / "packaged_office_edit_roundtrip.py",
)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def test_structural_evidence_does_not_copy_sensitive_text(
    monkeypatch, tmp_path
):
    root = tmp_path / "profile"
    (root / "user-data").mkdir(parents=True)
    secret = "SECRET_CONTENT_NOT_FOR_EVIDENCE"
    (root / "user-data" / "backend.log").write_text(
        secret + " Backend health check timed out"
    )
    log = tmp_path / "launch.log"
    log.write_text(secret + " ERR_CONNECTION_REFUSED")
    monkeypatch.setattr(
        m,
        "_all_targets",
        lambda _: [
            {
                "url": "file:///private/pdf/index.html?token=" + secret,
                "title": secret,
                "type": "page",
            }
        ],
    )
    m._capture_launch_failure(
        root,
        log,
        123,
        "body-content",
        SimpleNamespace(poll=lambda: None),
        AssertionError(secret),
    )
    target = log.with_suffix(".launch-failure.json")
    evidence = json.loads(target.read_text())
    assert secret not in target.read_text()
    assert evidence["stage"] == "body-content"
    assert evidence["targets"] == [
        {"scheme": "file", "editor": "pdf", "type": "page"}
    ]
    assert evidence["logs"]["backend"]["signals"]["backend_health_timeout"]
    assert evidence["logs"]["electron"]["signals"]["connection_refused"]
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("capture_fails", [False, True])
def test_launch_failure_captures_before_cleanup_and_keeps_primary(
    monkeypatch, tmp_path, capture_fails
):
    root = tmp_path / "profile"
    root.mkdir()
    fixture = tmp_path / "sample.pdf"
    fixture.write_bytes(b"fixture")
    log = tmp_path / "launch.log"
    primary = AssertionError("Office renderer missing")
    events = []
    monkeypatch.setattr(m.tempfile, "mkdtemp", lambda **_: str(root))
    monkeypatch.setattr(m, "_free_port", lambda: 123)
    monkeypatch.setattr(m, "_office_environment", lambda _: {})
    monkeypatch.setattr(m, "_app_executable", lambda _: Path("/fake/app"))
    monkeypatch.setattr(
        m.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(pid=123, poll=lambda: None),
    )

    def settle():
        events.append("settle")
        return {"ok": True, "live": [], "unknown": []}

    monkeypatch.setattr(
        m, "OwnedProcessCohort", lambda _: SimpleNamespace(settle=settle)
    )

    def fail(*_):
        raise primary

    monkeypatch.setattr(m, "_find_office_cdp", fail)

    def capture(*args):
        assert root.exists()
        assert args[3] == "office-api"
        events.append("capture")
        if capture_fails:
            raise OSError("diagnostic write unavailable")

    monkeypatch.setattr(m, "_capture_launch_failure", capture, raising=False)
    with pytest.raises(AssertionError) as result:
        m._launch_once(tmp_path / "App", fixture, log, 1)
    assert result.value is primary
    assert events == ["capture", "settle"]
    assert not root.exists()
    assert json.loads(log.with_suffix(".settlement.json").read_text())["ok"]
