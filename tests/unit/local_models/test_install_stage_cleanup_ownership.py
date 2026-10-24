"""One finite public-pipeline cleanup identity regression, entirely offline."""
from pathlib import Path

from tests.unit.local_models.test_source_install_regressions import (
    DownloadTaskStatus,
    backend,
    finite_staged_health_command,
    forbid_real_network_and_execution,
    good_archive,
    prepare,
    run_worker,
)


def test_cleanup_cannot_remove_foreign_real_directory_at_owned_stage_name(tmp_path, monkeypatch):
    instance = backend(tmp_path / "bin")
    data = good_archive(instance, marker=b"owned-stage")
    spec, _, _ = prepare(monkeypatch, instance, data)
    worker = run_worker(monkeypatch, spec, data)
    assert worker.status == DownloadTaskStatus.COMPLETED
    stage = Path(spec.task.payload["staging_dir"])
    original_identity = (stage.stat().st_dev, stage.stat().st_ino)
    original_files = {str(path.relative_to(stage)): path.read_bytes()
                      for path in stage.rglob("*") if path.is_file()}
    parked = tmp_path / "parked-owned-stage"
    stage.rename(parked)
    stage.mkdir()
    replacement_identity = (stage.stat().st_dev, stage.stat().st_ino)
    assert replacement_identity != original_identity
    sentinel = stage / "foreign-owned-sentinel"
    sentinel.write_bytes(b"must-retain-foreign-stage")
    failure = None
    try:
        spec.task.run_cleanup()
    except Exception as error:
        failure = error
    assert sentinel.exists(), "cleanup deleted a replacement directory it never owned"
    assert sentinel.read_bytes() == b"must-retain-foreign-stage"
    assert (stage.stat().st_dev, stage.stat().st_ino) == replacement_identity
    assert {str(path.relative_to(parked)): path.read_bytes()
            for path in parked.rglob("*") if path.is_file()} == original_files
    assert failure is not None, "unconfirmed stage ownership needs an observable cleanup block"
