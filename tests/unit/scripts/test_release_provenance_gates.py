# -*- coding: utf-8 -*-
"""Release artifact provenance and stale-output guard tests."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _release_roots() -> list[Path]:
    return [ROOT]


def _script_names(_root: Path) -> list[str]:
    return ["build_and_deploy.sh"]


def _script_text(root: Path, script_name: str) -> str:
    return (root / script_name).read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    marker = f"{name}() {{"
    start = text.find(marker)
    assert start != -1, f"missing function {name}"
    next_function = text.find("\n}\n\n", start)
    assert next_function != -1, f"could not find end of {name}"
    return text[start : next_function + 3]


def test_direct_release_script_does_not_mutate_tracked_version_files():
    for root in _release_roots():
        text = _script_text(root, "build_and_deploy.sh")

        assert "bump_build_version" not in text
        assert "LANTAI_SKIP_VERSION_BUMP" not in text
        assert "package_path.write_text" not in text
        assert "lock_path.write_text" not in text
        assert "version_path.write_text" not in text


def test_release_scripts_require_source_and_manifest_provenance_gates():
    for root in _release_roots():
        for script_name in _script_names(root):
            text = _script_text(root, script_name)

            assert "SOURCE_SHA" in text
            assert "RELEASE_ARCH" in text
            assert "validate_release_source_state" in text
            assert "validate_release_tag_metadata" in text
            assert "LANTAI_ALLOW_DIRTY_RELEASE_BUILD" in text
            assert "ARTIFACT_MANIFEST" in text
            assert "write_artifact_manifest" in text
            assert "check_release_artifact_provenance.py" in text
            assert '--config.directories.output="$RELEASE_DIR"' in text

        assert (
            root / "scripts/check_release_artifact_provenance.py"
        ).is_file()


def test_direct_release_checks_genoffice_upstream_before_building():
    text = _script_text(ROOT, "build_and_deploy.sh")
    gate = _function_body(text, "validate_genoffice_upstream")

    assert "check_genoffice_upstream.py" in text
    assert "console/vendor/lantai-docx-editor/SOURCE.md" in gate
    assert "genoffice-upstream-check.json" in text
    assert "--allow-network-failure" in gate
    assert "--allow-stale" in gate
    assert "--check-interval-hours" in gate
    assert "GENOFFICE_UPSTREAM_CHECK_INTERVAL_HOURS" in text
    assert text.index("validate_genoffice_upstream\n") < text.index(
        "build_console\n",
    )


# pylint: disable-next=too-many-statements
def test_release_scripts_run_stage_specific_evidence_readiness_gates():
    direct_text = _script_text(ROOT, "build_and_deploy.sh")
    direct_gate = _function_body(
        direct_text,
        "run_release_evidence_readiness_gate",
    )
    assert "check_release_evidence_readiness.py" in direct_text
    assert "write_runtime_migration_dry_run_report.py" in direct_text
    assert "run_runtime_migration_backup_restore_smoke.py" in direct_text
    assert "--legacy-writer-mode read_only" in _function_body(
        direct_text,
        "write_runtime_migration_dry_run_report",
    )
    assert "official-release-readiness-stage.json" in direct_gate
    assert '--official-app "$release_app"' in direct_gate
    assert '--official-dmg "$release_dmg"' in direct_gate
    assert '--official-zip "$release_zip"' in direct_gate
    assert (
        '--runtime-migration-report "$RUNTIME_MIGRATION_REPORT"' in direct_gate
    )
    assert (
        "--runtime-migration-backup-restore-evidence "
        '"$RUNTIME_MIGRATION_BACKUP_RESTORE_EVIDENCE"'
    ) in direct_gate
    assert "--require-check official.download_app.bundle_id" in direct_gate
    assert "--require-check official.download_app.signature" in direct_gate
    assert "--require-check official.download_bundles.present" in direct_gate
    assert (
        "--require-check close01.runtime_migration_backup_restore.evidence"
        in direct_gate
    )
    assert (
        "--require-check official.download_app.gatekeeper_notarized"
        in direct_gate
    )
    assert (
        "--require-check official.packaged_lifecycle_smoke.evidence"
        in direct_gate
    )
    assert (
        "--require-check official.legacy_writer_observation.evidence"
        in direct_gate
    )
    assert "run_release_evidence_readiness_gate\n" in _function_body(
        direct_text,
        "main",
    )
    assert direct_text.index(
        "run_packaged_app_lifecycle_smoke\n",
    ) < direct_text.index("run_release_evidence_readiness_gate\n")
    assert direct_text.index(
        "write_runtime_migration_dry_run_report\n",
    ) < direct_text.index("run_release_evidence_readiness_gate\n")
    assert direct_text.index(
        "write_runtime_migration_backup_restore_evidence\n",
    ) < direct_text.index("run_release_evidence_readiness_gate\n")
    assert direct_text.index(
        "run_release_evidence_readiness_gate\n",
    ) < direct_text.index("deploy_release_artifacts\n")
    assert direct_text.index(
        "run_release_evidence_readiness_gate\n",
    ) < direct_text.index("deploy_app\n")
    reliability_gate = _function_body(
        direct_text,
        "run_agent_reliability_release_gate",
    )
    assert "check_agent_reliability_release_gate.py" in direct_text
    assert '--evidence "$AGENT_RELIABILITY_EVIDENCE"' in reliability_gate
    assert '--official-app "$release_app"' in reliability_gate
    assert '"$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1"' in reliability_gate
    assert "skip_notarization_requested" in reliability_gate
    assert '"skipped": True' in reliability_gate
    assert '"local_only": True' in reliability_gate
    assert "run_agent_reliability_release_gate\n" in _function_body(
        direct_text,
        "main",
    )
    assert direct_text.index(
        "run_release_evidence_readiness_gate\n",
    ) < direct_text.index("run_agent_reliability_release_gate\n")
    assert direct_text.index(
        "run_agent_reliability_release_gate\n",
    ) < direct_text.index("deploy_release_artifacts\n")
    persona_office_gate = _function_body(
        direct_text,
        "run_persona_and_office_release_gates",
    )
    assert "check_packaged_persona_release_evidence.py" in direct_text
    assert "check_office_release_evidence.py" in direct_text
    assert "LANTAI_OFFICE_CORPUS_EVIDENCE" in direct_text
    assert '--corpus "$OFFICE_CORPUS_EVIDENCE"' in direct_text
    assert "LANTAI_OFFICE_EDIT_ROUNDTRIP_EVIDENCE" in direct_text
    assert '--roundtrip "$OFFICE_EDIT_ROUNDTRIP_EVIDENCE"' in direct_text
    assert '--app "$release_app"' in persona_office_gate
    assert '--evidence "$PERSONA_EVIDENCE_A"' in persona_office_gate
    assert '--evidence "$PERSONA_EVIDENCE_B"' in persona_office_gate
    assert "LANTAI_PERSONA_REVIEW_A" in direct_text
    assert "LANTAI_PERSONA_REVIEW_B" in direct_text
    assert '--review "$PERSONA_REVIEW_A"' in persona_office_gate
    assert '--review "$PERSONA_REVIEW_B"' in persona_office_gate
    assert '--first-tier "$OFFICE_FIRST_TIER_EVIDENCE"' in persona_office_gate
    assert '--packaged "$OFFICE_PACKAGED_EVIDENCE"' in persona_office_gate
    assert '"$PUBLISH_GITHUB_RELEASE" != "1"' in persona_office_gate
    assert "run_persona_and_office_release_gates\n" in _function_body(
        direct_text,
        "main",
    )
    assert direct_text.index(
        "run_persona_and_office_release_gates\n",
    ) < direct_text.index("deploy_release_artifacts\n")


def test_release_output_paths_are_per_source_version_and_arch():
    for root in _release_roots():
        for script_name in _script_names(root):
            text = _script_text(root, script_name)

            assert 'RELEASE_ROOT_DIR="$ROOT_DIR/release"' in text
            assert (
                "builds/${VERSION}-${SOURCE_SHORT_SHA}-${RELEASE_ARCH}" in text
            )
            assert "arm64-internal" not in text
            assert "-${VERSION}-arm64" not in text
            assert "mas-dev-arm64" not in text


def test_release_artifact_selection_fails_on_ambiguous_outputs():
    for root in _release_roots():
        for script_name in _script_names(root):
            text = _script_text(root, script_name)
            assert "fail_if_multiple_paths" in text

            for function_name in (
                "find_release_file",
                "find_release_app",
                "find_packaged_app",
            ):
                if f"{function_name}() {{" not in text:
                    continue
                body = _function_body(text, function_name)
                assert "| sort" not in body
                assert "| head" not in body
                assert "head -n 1" not in body


def test_direct_release_script_can_run_packaged_app_lifecycle_smoke():
    text = _script_text(ROOT, "build_and_deploy.sh")

    assert "LANTAI_RUN_PACKAGED_APP_LIFECYCLE_SMOKE" in text
    assert "run_packaged_app_lifecycle_smoke" in text
    assert "scripts/packaged_app_lifecycle_smoke.py" in text
    assert "--distribution official" in text
    assert "LIFECYCLE_SMOKE_EVIDENCE" in text
    assert "official-packaged-lifecycle-smoke.json" in text
    assert 'DOWNLOAD_APP="$DOWNLOADS_DIR/${PRODUCT_NAME}.app"' in text
    assert "deploy_app\n" in _function_body(text, "main")
    assert "run_packaged_app_lifecycle_smoke" in _function_body(text, "main")
    assert text.index("run_packaged_app_lifecycle_smoke\n") < text.index(
        "deploy_app\n",
    )
    smoke_body = _function_body(text, "run_packaged_app_lifecycle_smoke")
    assert 'release_app="$(find_release_app)"' in smoke_body
    assert '--app "$release_app"' in smoke_body
    assert '--evidence-output "$LIFECYCLE_SMOKE_EVIDENCE"' in smoke_body
    assert "--restart-cycle" in smoke_body
    assert "--app-relaunch-cycle" in smoke_body
    assert "LANTAI_PACKAGED_APP_LIFECYCLE_RESTART" not in smoke_body


def test_direct_release_re_signs_every_embedded_macho_before_notarization():
    """Apple validates embedded executable leaves more strictly than --deep."""

    text = _script_text(ROOT, "build_and_deploy.sh")
    signing = _function_body(
        text,
        "sign_packaged_macho_leaves_for_distribution",
    )
    main = _function_body(text, "main")

    assert "file -b \"$path\" | grep -q 'Mach-O'" in signing
    assert 'find "$release_app" -type f -print0' in signing
    assert "--timestamp --options runtime" in signing
    assert '--preserve-metadata="$preserve_metadata"' in signing
    assert "-name '*.framework'" in signing
    assert "developer-id.app.plist" in signing
    assert (
        'codesign --verify --deep --strict --verbose=2 "$release_app"'
        in signing
    )
    assert main.index(
        "sign_packaged_macho_leaves_for_distribution\n",
    ) < main.index("run_packaged_skill_runtime_smoke\n")


def test_release_retries_post_package_timestamp_failures():
    """Do not discard a build after a transient TSA failure."""

    text = _script_text(ROOT, "build_and_deploy.sh")
    retry = _function_body(text, "codesign_with_timestamp_retry")
    signing = _function_body(
        text,
        "sign_packaged_macho_leaves_for_distribution",
    )

    assert "LANTAI_CODESIGN_TIMESTAMP_MAX_ATTEMPTS" in retry
    assert "a timestamp was expected but was not found" in retry
    assert 'return "$status"' in retry
    assert 'codesign_with_timestamp_retry "embedded Mach-O $path"' in signing
    assert 'codesign_with_timestamp_retry "nested app $bundle"' in signing
    assert (
        'codesign_with_timestamp_retry "nested framework $bundle"' in signing
    )
    assert 'codesign_with_timestamp_retry "root app $release_app"' in signing
    assert "app-builder-lib/templates/entitlements.mac.plist" in signing
    assert '--entitlements "$bundle_entitlements" "$bundle"' in signing


def test_direct_release_assembles_unsigned_then_signs_and_repackages():
    """Require per-leaf TSA retries, not an opaque builder retry."""

    text = _script_text(ROOT, "build_and_deploy.sh")
    electron = _function_body(text, "build_electron")
    signing = _function_body(
        text,
        "sign_packaged_macho_leaves_for_distribution",
    )
    notarize = _function_body(text, "notarize_release_bundle_and_artifacts")
    main = _function_body(text, "main")

    assert "--config.mac.identity=null" in electron
    assert "codesign_with_timestamp_retry" in signing
    assert main.index("build_electron\n") < main.index(
        "sign_packaged_macho_leaves_for_distribution\n",
    )
    assert "if skip_notarization_requested; then" in notarize
    assert (
        'rebuild_release_artifacts_from_stapled_app "$release_app"' in notarize
    )


def test_stapled_app_is_immutable_during_packaging_and_testing():
    """Keep the notarized app immutable during packaging and tests."""

    text = _script_text(ROOT, "build_and_deploy.sh")
    packaging = _function_body(text, "package_stapled_app_as_dmg")
    main = _function_body(text, "main")

    assert (
        'staging_dir="$(mktemp -d "$RELEASE_DIR/dmg-prepackaged.XXXXXX")"'
        in packaging
    )
    assert (
        'output_dir="$(mktemp -d "$RELEASE_DIR/dmg-output.XXXXXX")"'
        in packaging
    )
    assert 'ditto "$release_app" "$staged_app"' in packaging
    assert '--prepackaged "$staged_app"' in packaging
    assert "CSC_IDENTITY_AUTO_DISCOVERY=false" in packaging
    assert "--config.mac.identity=null" in packaging
    assert '"--config.directories.output=$output_dir"' in packaging
    assert 'ditto "$built_dmg" "$dmg_path"' in packaging

    notarize_index = main.index("notarize_release_bundle_and_artifacts\n")
    for smoke in (
        "run_packaged_app_lifecycle_smoke\n",
        "run_packaged_app_rapid_lifecycle_smoke\n",
        "run_packaged_customization_upgrade_smoke\n",
        "run_packaged_image_parser_mitigation\n",
        "run_packaged_upgrade_rollback_smoke\n",
    ):
        assert main.index(smoke) < notarize_index


def test_direct_release_fails_closed_on_packaged_image_parser_mitigation():
    text = _script_text(ROOT, "build_and_deploy.sh")

    assert "check_packaged_image_parser_mitigation.py" in text
    assert "PACKAGED_IMAGE_PARSER_MITIGATION_EVIDENCE" in text
    assert "official-packaged-image-parser-mitigation.json" in text
    assert "run_packaged_image_parser_mitigation" in _function_body(
        text,
        "main",
    )
    mitigation_body = _function_body(
        text,
        "run_packaged_image_parser_mitigation",
    )
    assert 'release_app="$(find_release_app)"' in mitigation_body
    assert '--app "$release_app"' in mitigation_body
    assert (
        '--output "$PACKAGED_IMAGE_PARSER_MITIGATION_EVIDENCE"'
        in mitigation_body
    )
    assert text.index("run_packaged_image_parser_mitigation\n") < text.index(
        "run_release_evidence_readiness_gate\n",
    )
    assert text.index("run_packaged_image_parser_mitigation\n") < text.index(
        "deploy_app\n",
    )


def test_direct_release_script_can_skip_notarization_for_local_smoke():
    if ROOT.name == "lantai_App Store":
        return
    text = _script_text(ROOT, "build_and_deploy.sh")

    assert "LANTAI_SKIP_NOTARIZATION" in text
    assert "skip_notarization_requested" in text
    assert "not be treated as redistributable" in text
    assert "LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1" in text
    assert "skipping notarization credentials" in _function_body(
        text,
        "require_notarization_credentials",
    )
    notarize_body = _function_body(
        text,
        "notarize_release_bundle_and_artifacts",
    )
    assert (
        "Skipping notarization because LANTAI_SKIP_NOTARIZATION=1"
        in notarize_body
    )
    # A local smoke package must be rebuilt from the final signed app; that
    # helper also applies and verifies the Developer ID signature on its DMG.
    # Only the Apple notary submission/stapling step may be skipped.
    assert (
        'rebuild_release_artifacts_from_stapled_app "$release_app"'
        in notarize_body
    )
    rebuild_body = _function_body(
        text,
        "rebuild_release_artifacts_from_stapled_app",
    )
    assert 'sign_dmg_for_distribution "$dmg_path"' in rebuild_body
    assert (
        "local smoke build with LANTAI_SKIP_NOTARIZATION=1"
        in _function_body(
            text,
            "assess_gatekeeper",
        )
    )


def test_direct_release_script_has_fail_closed_artifact_only_mode():
    """A local candidate may be built without replacing the user's live app."""

    if ROOT.name == "lantai_App Store":
        return
    text = _script_text(ROOT, "build_and_deploy.sh")
    guard = _function_body(text, "validate_artifact_only_request")
    main = _function_body(text, "main")

    assert "--artifact-only" in text
    assert '"$ALLOW_UNVERIFIED_LOCAL_BUILD" != "1"' in guard
    assert '"$PUBLISH_GITHUB_RELEASE" -eq 1' in guard
    assert '[[ -z "$RELEASE_CANDIDATE_ID" ]]' in guard
    assert "filesystem-safe characters" in guard
    assert "only valid with --artifact-only" in guard
    assert "ARTIFACT_ONLY=1" in text
    assert "candidate-${RELEASE_CANDIDATE_ID}" in text
    assert 'if [[ "$ARTIFACT_ONLY" -eq 1 ]]' in main
    assert "deploy_release_artifacts" in main
    assert "deploy_app" in main
    assert "assess_gatekeeper" in main
    assert "publish_github_release" in main
    assert "launch_app" in main
    assert main.index('if [[ "$ARTIFACT_ONLY" -eq 1 ]]') < main.index(
        "deploy_release_artifacts",
    )


def test_direct_release_script_retries_transient_notary_upload_timeouts():
    text = _script_text(ROOT, "build_and_deploy.sh")
    notary_body = _function_body(text, "notarize_file_with_notarytool")

    assert "LANTAI_NOTARY_MAX_ATTEMPTS" in notary_body
    assert "deadlineExceeded" in notary_body
    assert "connectTimeout" in notary_body
    assert "attempt < max_attempts" in notary_body
    assert "sleep" in notary_body


def test_direct_release_script_can_force_gated_adhoc_local_build():
    if ROOT.name == "lantai_App Store":
        return
    text = _script_text(ROOT, "build_and_deploy.sh")

    assert "LANTAI_FORCE_ADHOC_LOCAL_BUILD" in text
    signing_body = _function_body(text, "configure_distribution_signing")
    assert "LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1" in signing_body
    assert 'MAC_CODESIGN_IDENTITY="-"' in signing_body
    assert 'MAC_SIGNING_MODE="unsigned"' in signing_body
    assert "unset CSC_LINK CSC_NAME" in signing_body

    electron_body = _function_body(text, "build_electron")
    assert "--config.mac.identity=null" in electron_body
    adhoc_body = _function_body(
        text,
        "adhoc_sign_local_release_bundle_and_artifacts",
    )
    assert "codesign --force --deep --sign -" in adhoc_body
    assert "rebuild_release_artifacts_from_stapled_app" in adhoc_body
    assert "adhoc_sign_local_release_bundle_and_artifacts\n" in _function_body(
        text,
        "main",
    )


def test_direct_release_publishes_without_update_metadata():
    text = _script_text(ROOT, "build_and_deploy.sh")
    publish = _function_body(text, "publish_github_release")
    prerequisites = _function_body(text, "assert_public_release_prerequisites")

    assert "--publish-github-release" in text
    assert "LANTAI_GITHUB_RELEASE_REPOSITORY" in text
    assert "shenyangs/lantai3-releases" in text
    assert "SOURCE_DIRTY" in prerequisites
    assert "Developer ID" in prerequisites
    assert "v$VERSION" in prerequisites
    assert "assert_accepted_notary_record" in prerequisites
    assert "audit_public_release_version_lineage prepublish" in publish
    assert "audit_public_release_version_lineage postpublish" in publish
    assert (
        'repo view "$GITHUB_RELEASE_REPOSITORY" '
        "--json visibility --jq .visibility" in publish
    )
    assert 'release create "$RELEASE_TAG"' in publish
    assert "LATEST_MAC_YML" not in publish
    assert '"$ARTIFACT_MANIFEST"' in publish
    assert '"$SUPPLY_CHAIN_MANIFEST"' in publish
    assert text.index("assess_gatekeeper\n") < text.index(
        "publish_github_release\n",
    )


def test_skip_build_and_release_artifacts_are_manifest_validated():
    direct_text = _script_text(ROOT, "build_and_deploy.sh")
    helper_text = (
        ROOT / "scripts/check_release_artifact_provenance.py"
    ).read_text(encoding="utf-8")

    assert "validate_skip_build_manifest" in direct_text
    assert "write_artifact_manifest" in direct_text
    assert "source-sha" in direct_text
    assert "latest-mac.yml" not in direct_text
    assert "updater" not in helper_text.lower()


def test_removed_microphone_capability_is_absent_from_signing_paths():
    import json
    import plistlib

    mac = json.loads((ROOT / "console/package.json").read_text())["build"]["mac"]
    for key in ("entitlements", "entitlementsInherit"):
        policy = ROOT / "console" / mac.get(key, "electron/entitlements/developer-id.app.plist")
        entitlements = plistlib.loads(policy.read_bytes())
        assert "com.apple.security.device.audio-input" not in entitlements
        assert mac.get(key), f"builder must explicitly use the {key} policy"
    signing = _function_body(_script_text(ROOT, "build_and_deploy.sh"), "sign_packaged_macho_leaves_for_distribution")
    assert "developer-id.app.plist" in signing
    assert "developer-id.electron-helper.plist" in signing
    assert "check_packaged_microphone_permissions.py" not in signing
