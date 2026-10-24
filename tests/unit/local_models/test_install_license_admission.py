"""Four finite actual-public-pipeline license response failure locks."""
from pathlib import Path

import pytest

from tests.unit.local_models.test_source_install_regressions import (
    DownloadTaskStatus,
    backend,
    finite_staged_health_command,
    forbid_real_network_and_execution,
    good_archive,
    install,
    prepare,
    run_worker,
)


@pytest.mark.parametrize("failure", ["license_empty", "license_truncated", "license_wrong", "license_nonzero"])
def test_invalid_license_response_cannot_publish_or_change_previous_install(
    tmp_path, monkeypatch, finite_staged_health_command, failure,
):
    instance = backend(tmp_path / "bin")
    _, _, previous = install(monkeypatch, instance, good_archive(instance, marker=b"old-license-verified"))
    assert previous.status == DownloadTaskStatus.COMPLETED
    assert instance.check_llamacpp_installation() == (True, "")
    old_files = {str(path.relative_to(instance.target_dir)): path.read_bytes()
                 for path in instance.target_dir.rglob("*") if path.is_file()}
    outside = tmp_path / "unrelated-license-fixture"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"must-retain")
    data = good_archive(instance, marker=b"candidate-license-failure")
    spec, _, _ = prepare(monkeypatch, instance, data)
    stage = Path(spec.task.payload["staging_dir"])
    finite_staged_health_command.mode = failure
    worker = run_worker(monkeypatch, spec, data)
    final, _ = spec.task.finalize(worker)
    assert final.status == DownloadTaskStatus.FAILED, f"{failure} license response published as complete"
    assert final.error, "license failure requires an observable diagnostic"
    assert any(call["stage"] == str(stage) and call["command"][1] == "--license"
               and call["mode"] == failure for call in finite_staged_health_command.calls), "actual finite license command boundary was not exercised"
    assert {str(path.relative_to(instance.target_dir)): path.read_bytes()
            for path in instance.target_dir.rglob("*") if path.is_file()} == old_files, "license failure changed old verified bytes or receipt"
    assert instance.check_llamacpp_installation() == (True, "")
    spec.task.run_cleanup()
    assert not stage.exists(), "license failure left its owned candidate stage"
    assert (outside / "sentinel").read_bytes() == b"must-retain"
