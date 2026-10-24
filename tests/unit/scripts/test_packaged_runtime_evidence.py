from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    path = ROOT / "scripts" / "packaged_runtime_evidence.py"
    spec = importlib.util.spec_from_file_location("packaged_runtime_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_readiness_module():
    path = ROOT / "scripts" / "check_release_evidence_readiness.py"
    spec = importlib.util.spec_from_file_location(
        "check_release_evidence_readiness_for_runtime",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_artifact_module():
    path = ROOT / "scripts" / "check_release_artifact_provenance.py"
    spec = importlib.util.spec_from_file_location(
        "check_release_artifact_provenance_for_runtime",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(module, app: Path) -> dict[str, object]:
    artifact_paths = {
        "app_executable": "Contents/MacOS/澜台",
        "app_asar": "Contents/Resources/app.asar",
        "backend_executable": (
            "Contents/Resources/lantai-server/Lantai Backend.app/"
            "Contents/MacOS/lantai-server"
        ),
    }
    artifacts = {}
    for artifact_id, relative in artifact_paths.items():
        target = app / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(artifact_id.encode())
        artifacts[artifact_id] = {
            "path": relative,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    return {
        "schema": module.SCHEMA,
        "status": "ok",
        "source": {"git_sha": "a" * 40},
        "app": {"version": "1.2.3"},
        "artifacts": artifacts,
        "runtime_import_probe": {
            "module_count": 43,
            "failed_count": 0,
            "cli_help_count": 25,
        },
        "skill_import_probe": {
            "script_count": 111,
            "import_count": 508,
            "failed_count": 0,
        },
        "node_import_probe": {"script_count": 3, "failed_count": 0},
        "shell_syntax_count": 1,
        "skill_resource_parity": {
            "source_count": 10,
            "packaged_count": 10,
            "source_manifest_sha256": "b" * 64,
            "packaged_manifest_sha256": "f" * 64,
            "bundle_unchanged_after_probes": True,
        },
        "stable_presentations": {
            locale: {"slide_count": 1, "sha256": "c" * 64}
            for locale in module.REQUIRED_STABLE_PRESENTATIONS
        },
        "pdf_renderers": {
            locale: {
                "page_count": 1,
                "width": 100,
                "height": 100,
                "sha256": "d" * 64,
            }
            for locale in module.REQUIRED_PDF_RENDERERS
        },
        "sanitized_environment": {
            "clean_path": True,
            "dont_write_bytecode": True,
            "isolated_probe_cwd": True,
            "removed": list(module.REQUIRED_REMOVED_ENV),
        },
    }


def test_packaged_runtime_evidence_binds_source_and_artifact_hashes(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app = tmp_path / "澜台.app"
    payload = _payload(module, app)

    assert (
        module.validate_payload(
            payload,
            expected_source_sha="a" * 40,
            expected_version="1.2.3",
            app=app,
        )
        == []
    )

    (app / "Contents/Resources/app.asar").write_bytes(b"changed")
    assert "artifact_hash_mismatch:app_asar" in module.validate_payload(
        payload,
        expected_source_sha="a" * 40,
        expected_version="1.2.3",
        app=app,
    )


def test_packaged_runtime_evidence_requires_bilingual_ppt_and_pdfium(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app = tmp_path / "澜台.app"
    payload = _payload(module, app)
    payload["stable_presentations"].pop("pptx-en")
    payload["pdf_renderers"].pop("pdf-en")

    issues = module.validate_payload(
        payload,
        expected_source_sha="a" * 40,
        expected_version="1.2.3",
        app=app,
    )

    assert "stable_presentation_missing:pptx-en" in issues
    assert "pdf_renderer_missing:pdf-en" in issues


def test_readiness_gate_requires_bound_packaged_runtime_evidence(
    tmp_path: Path,
) -> None:
    evidence_module = _load_module()
    readiness = _load_readiness_module()
    app = tmp_path / "澜台.app"
    payload = _payload(evidence_module, app)
    evidence = tmp_path / "official-packaged-runtime-completeness-smoke.json"
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    check = readiness._check_packaged_runtime_completeness_evidence(
        evidence,
        expected_source_sha="a" * 40,
        expected_version="1.2.3",
        app=app,
    )

    assert check["id"] == "official.packaged_runtime_completeness.evidence"
    assert check["status"] == "passed"


def test_release_builder_persists_and_manifests_runtime_evidence() -> None:
    build = (ROOT / "build_and_deploy.sh").read_text(encoding="utf-8")
    artifact = (ROOT / "scripts" / "check_release_artifact_provenance.py").read_text(
        encoding="utf-8",
    )

    assert "official-packaged-runtime-completeness-smoke.json" in build
    assert '--output "$PACKAGED_RUNTIME_COMPLETENESS_EVIDENCE"' in build
    assert (
        '--packaged-runtime-evidence "$PACKAGED_RUNTIME_COMPLETENESS_EVIDENCE"' in build
    )
    assert "packagedRuntimeCompletenessEvidence" in artifact
    main = build[build.index("main() {") :]
    assert main.index("adhoc_sign_local_release_bundle_and_artifacts") < main.index(
        "run_packaged_skill_runtime_smoke"
    )


def test_artifact_manifest_embeds_validated_runtime_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evidence_module = _load_module()
    artifact_module = _load_artifact_module()
    root = tmp_path / "root"
    release = root / "release"
    app = release / "澜台.app"
    payload = _payload(evidence_module, app)
    identity_path = app / "Contents/Resources/console/dist/release-provenance.json"
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(json.dumps({"schemaVersion": 1, "gitSha": "a" * 40, "version": "1.2.3",
                                        "sourceBranch": "main", "canonicalMain": True,
                                        "artifactOnly": False, "candidateId": None}), encoding="utf-8")
    evidence = release / "release-evidence" / "runtime.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    (root / "console").mkdir(parents=True)
    (root / "console/package.json").write_text(
        json.dumps(
            {
                "name": "lantai-console",
                "version": "1.2.3",
                "build": {"productName": "澜台"},
            }
        ),
        encoding="utf-8",
    )
    output = release / "manifest.json"
    monkeypatch.setattr(
        artifact_module, "_validate_app", lambda *_args: {"path": str(app)}
    )
    monkeypatch.setattr(artifact_module, "_load_dependency_manifest", lambda *_args: {})
    monkeypatch.setattr(
        artifact_module, "_load_supply_chain_manifest", lambda *_args, **_kwargs: {}
    )

    artifact_module.write_manifest(
        Namespace(
            root=root,
            release_dir=release,
            output=output,
            source_sha="a" * 40,
            source_dirty="0",
            release_tag="",
            version="1.2.3",
            arch="arm64",
            product_name="澜台",
            signing_mode="developer-id",
            codesign_identity_class="developer-id",
            dependency_manifest=None,
            supply_chain_manifest=None,
            packaged_runtime_evidence=evidence,
            allow_unreleasable_binaries=False,
            notary_json_dir=None,
            app=app,
            zip=None,
            dmg=None,
        )
    )

    manifest = json.loads(output.read_text(encoding="utf-8"))
    embedded = manifest["packagedRuntimeCompletenessEvidence"]
    assert embedded["report"] == payload
    assert (
        embedded["file"]["sha256"] == hashlib.sha256(evidence.read_bytes()).hexdigest()
    )
