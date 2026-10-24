#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONSOLE_DIR="$ROOT_DIR/console"
CONSOLE_BUILD_DIR="$CONSOLE_DIR/build"
CONSOLE_STATIC_DIR="$ROOT_DIR/src/lantai/console"
RELEASE_ROOT_DIR="$ROOT_DIR/release"
RELEASE_DIR=""
DOWNLOADS_DIR="${HOME}/Downloads"
USER_DATA_DIR="${HOME}/Library/Application Support/lantai-console"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
ICON_SOURCE="$ROOT_DIR/scripts/pack/assets/icon.icns"
DEPENDENCY_MANIFEST="$ROOT_DIR/build/release-dependency-manifest.json"
RELEASE_BUILD_LOCK_FILE="$ROOT_DIR/build/.release-build.lock"
RELEASE_SOURCE_AUTHORITY_SCRIPT="$ROOT_DIR/scripts/check_release_source_authority.py"
RELEASE_SOURCE_AUTHORITY_REPORT="$ROOT_DIR/build/release-source-authority.json"
RELEASE_BUILD_IDENTITY_SCRIPT="$ROOT_DIR/scripts/write_release_build_identity.py"
RELEASE_PRODUCT_CONTRACT_SCRIPT="$ROOT_DIR/scripts/check_release_product_contract.py"
RELEASE_PROVENANCE_SCRIPT="$ROOT_DIR/scripts/check_release_artifact_provenance.py"
RELEASE_DEPLOYMENT_SCRIPT="$ROOT_DIR/scripts/release_deployment_transaction.py"
RELEASE_UPLOAD_SCRIPT="$ROOT_DIR/scripts/check_release_upload.py"
SUPPLY_CHAIN_SCRIPT="$ROOT_DIR/scripts/check_release_supply_chain.py"
RELEASE_READINESS_SCRIPT="$ROOT_DIR/scripts/check_release_evidence_readiness.py"
AGENT_RELIABILITY_GATE_SCRIPT="$ROOT_DIR/scripts/check_agent_reliability_release_gate.py"
AGENT_RELIABILITY_EVIDENCE="${LANTAI_AGENT_RELIABILITY_EVIDENCE:-$ROOT_DIR/build/agent-reliability-release-evidence.json}"
UPGRADE_ROLLBACK_SMOKE_SCRIPT="$ROOT_DIR/scripts/packaged_app_upgrade_rollback_smoke.py"
UPGRADE_ROLLBACK_GATE_SCRIPT="$ROOT_DIR/scripts/check_packaged_upgrade_rollback_evidence.py"
PREVIOUS_RELEASE_APP="${LANTAI_PREVIOUS_RELEASE_APP:-}"
PERSONA_RELEASE_GATE_SCRIPT="$ROOT_DIR/scripts/check_packaged_persona_release_evidence.py"
PERSONA_EVIDENCE_A="${LANTAI_PERSONA_EVIDENCE_A:-}"
PERSONA_EVIDENCE_B="${LANTAI_PERSONA_EVIDENCE_B:-}"
PERSONA_REVIEW_A="${LANTAI_PERSONA_REVIEW_A:-}"
PERSONA_REVIEW_B="${LANTAI_PERSONA_REVIEW_B:-}"
OFFICE_RELEASE_GATE_SCRIPT="$ROOT_DIR/scripts/check_office_release_evidence.py"
OFFICE_FIRST_TIER_EVIDENCE="${LANTAI_OFFICE_FIRST_TIER_EVIDENCE:-}"
OFFICE_PACKAGED_EVIDENCE="${LANTAI_OFFICE_PACKAGED_EVIDENCE:-}"
OFFICE_CORPUS_EVIDENCE="${LANTAI_OFFICE_CORPUS_EVIDENCE:-}"
OFFICE_EDIT_ROUNDTRIP_EVIDENCE="${LANTAI_OFFICE_EDIT_ROUNDTRIP_EVIDENCE:-}"
GENOFFICE_UPSTREAM_SCRIPT="$ROOT_DIR/scripts/check_genoffice_upstream.py"
GENOFFICE_UPSTREAM_REPORT="$ROOT_DIR/build/genoffice-upstream-check.json"
GENOFFICE_UPSTREAM_CHECK_INTERVAL_HOURS="${LANTAI_GENOFFICE_UPSTREAM_CHECK_INTERVAL_HOURS:-72}"
LAUNCHSERVICES_CLEANUP_SCRIPT="$ROOT_DIR/scripts/clean_lantai_launchservices.py"
RUNTIME_MIGRATION_REPORT_SCRIPT="$ROOT_DIR/scripts/write_runtime_migration_dry_run_report.py"
RUNTIME_MIGRATION_BACKUP_RESTORE_SCRIPT="$ROOT_DIR/scripts/run_runtime_migration_backup_restore_smoke.py"
PACKAGED_SKILL_RUNTIME_SMOKE_SCRIPT="$ROOT_DIR/scripts/packaged_skill_runtime_smoke.py"
PACKAGED_IMAGE_PARSER_MITIGATION_SCRIPT="$ROOT_DIR/scripts/check_packaged_image_parser_mitigation.py"
SOURCE_SHA=""
SOURCE_SHORT_SHA=""
SOURCE_DIRTY="0"
RELEASE_TAG=""
RELEASE_ARCH="${LANTAI_RELEASE_ARCH:-}"
ARTIFACT_MANIFEST=""
SUPPLY_CHAIN_MANIFEST=""
SBOM_PATH=""
NOTARY_RECORD_DIR=""
DOWNLOAD_APP=""
DOWNLOAD_DMG=""
DOWNLOAD_ZIP=""
LIFECYCLE_SMOKE_EVIDENCE=""
RAPID_LIFECYCLE_SMOKE_EVIDENCE=""
CUSTOMIZATION_UPGRADE_SMOKE_EVIDENCE=""
PACKAGED_IMAGE_PARSER_MITIGATION_EVIDENCE=""
UPGRADE_ROLLBACK_SMOKE_EVIDENCE=""
RUNTIME_MIGRATION_REPORT=""
PUBLISH_GITHUB_RELEASE=0
ARTIFACT_ONLY=0
PREPARE_RELEASE=0
PREPARED_RELEASE_DIR=""
RELEASE_CANDIDATE_ID="${LANTAI_RELEASE_CANDIDATE_ID:-}"
GITHUB_RELEASE_REPOSITORY="${LANTAI_GITHUB_RELEASE_REPOSITORY:-shenyangs/lantai3-releases}"
GITHUB_CLI="${LANTAI_GITHUB_CLI:-}"

SKIP_BUILD=0
NO_LAUNCH=0
ALLOW_UNVERIFIED_LOCAL_BUILD="${LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD:-0}"
ALLOW_DIRTY_RELEASE_BUILD="${LANTAI_ALLOW_DIRTY_RELEASE_BUILD:-0}"
DEFER_AGENT_RELIABILITY_SOAK="${LANTAI_DEFER_AGENT_RELIABILITY_SOAK:-0}"
FORCE_ADHOC_LOCAL_BUILD="${LANTAI_FORCE_ADHOC_LOCAL_BUILD:-0}"
MAC_CODESIGN_IDENTITY="${LANTAI_MAC_CODESIGN_IDENTITY:-${CSC_NAME:-}}"
MAC_SIGNING_MODE="unknown"
RELEASE_BUILD_LOCK_OWNED=0
DEPLOYMENT_ACTIVE=0
DEPLOYMENT_PROMOTED=0
DEPLOYMENT_PREVIOUS_RUNNING=0
DEPLOYMENT_STAGE=""

# Local debug packages are ad-hoc signed, so macOS would treat each rebuilt
# lantai-server as a new Keychain client. Keep local packages on the file-backed
# master key at working.secret/.master_key to avoid repeated Keychain prompts.
export LANTAI_DISABLE_KEYRING="${LANTAI_DISABLE_KEYRING:-1}"
export LANTAI_SKIP_KEYRING="${LANTAI_SKIP_KEYRING:-$LANTAI_DISABLE_KEYRING}"

release_build_lock() {
  if [[ "$RELEASE_BUILD_LOCK_OWNED" != "1" ]]; then
    return
  fi
  local lock_owner=""
  if [[ -f "$RELEASE_BUILD_LOCK_FILE" ]]; then
    lock_owner="$(<"$RELEASE_BUILD_LOCK_FILE")"
  fi
  if [[ "$lock_owner" == "$$" ]]; then
    rm -f "$RELEASE_BUILD_LOCK_FILE"
  fi
  RELEASE_BUILD_LOCK_OWNED=0
}

acquire_release_build_lock() {
  mkdir -p "$(dirname "$RELEASE_BUILD_LOCK_FILE")"
  if ! /usr/bin/shlock -f "$RELEASE_BUILD_LOCK_FILE" -p "$$"; then
    local lock_owner="unknown"
    if [[ -f "$RELEASE_BUILD_LOCK_FILE" ]]; then
      lock_owner="$(<"$RELEASE_BUILD_LOCK_FILE")"
    fi
    echo "Error: another Lantai release build is already running (PID $lock_owner)." >&2
    echo "Wait for it to finish before starting a new build." >&2
    exit 1
  fi
  RELEASE_BUILD_LOCK_OWNED=1
  trap release_build_lock EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

release_deployment_cleanup() {
  local result=$?
  trap - EXIT
  set +e
  if [[ "${DEPLOYMENT_ACTIVE:-0}" == "1" ]]; then
    local previous_running
    if previous_running="$("$VENV_PYTHON" "$RELEASE_DEPLOYMENT_SCRIPT" previous-running --root "$DOWNLOADS_DIR" --owner-pid "$$")"; then
      DEPLOYMENT_PREVIOUS_RUNNING="$previous_running"
    fi
    # The transaction proves and settles the precise instance before restoring
    # any artifact. A failed stop keeps every backup and the private journal.
    if "$VENV_PYTHON" "$RELEASE_DEPLOYMENT_SCRIPT" rollback --root "$DOWNLOADS_DIR" --owner-pid "$$"; then
      echo "==> Restored the previous App, DMG and ZIP after deployment failure" >&2
      if [[ "${DEPLOYMENT_PREVIOUS_RUNNING:-0}" == "1" ]]; then
        /usr/bin/open -n "$DOWNLOAD_APP" || true
      fi
    else
      echo "Error: deployment recovery needs inspection; journal and backups are preserved." >&2
      [[ "$result" -ne 0 ]] || result=1
    fi
  fi
  release_build_lock
  exit "$result"
}

usage() {
  cat <<'EOF'
Usage: ./build_and_deploy.sh [options]

Options:
  --skip-build   Reuse existing release artifacts and only fix metadata/deploy.
                 This is the only mode exempt from the x.y.z -> x.y.(z+1)
                 new-build version gate.
  --no-launch    Do not reopen the copied app after deployment.
  --artifact-only
                 Build and validate a local candidate under release/builds,
                 without replacing anything in Downloads. Requires
                 LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1 and cannot publish.
  --prepare-release
                 Complete the formal release gates from canonical clean main,
                 then retain verified release files under release/builds.
                 Does not deploy to Downloads or upload to GitHub. Cannot be
                 combined with local-build bypasses, artifact-only or publish.
  --publish-github-release
                 Publish this verified release to the public GitHub update
                 channel after the local Developer ID, notarization,
                 Gatekeeper, and evidence gates pass.
  -h, --help     Show this help text.

Environment:
  LANTAI_REQUIRE_BUNDLED_OFFICECLI=1  Fail unless officecli is bundled.
  LANTAI_OFFICECLI_BINARY=/path/to/officecli
                                      Fixed external OfficeCLI artifact path.
  LANTAI_ALLOW_EXTERNAL_OFFICECLI=1
                                      Permit the external artifact path only
                                      when SHA/version/arch/license metadata
                                      env vars are also provided.
  LANTAI_RUN_PACKAGED_APP_LIFECYCLE_SMOKE=1
                                      Launch the deployed .app through the
                                      complete packaged-app lifecycle smoke
                                      before the final interactive launch. The
                                      smoke includes backend recovery, full
                                      Electron relaunch, persisted chat
                                      history/control checks, and streamed UI.
  LANTAI_SKIP_NOTARIZATION=1          Skip notarytool submission for a
                                      same-machine smoke build. Requires
                                      LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1.
  LANTAI_FORCE_ADHOC_LOCAL_BUILD=1    Force an ad-hoc signed same-machine build
                                      when Apple's timestamp service is down.
                                      Requires LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1.
  LANTAI_RESUME_NOTARIZATION=1        Resume an interrupted notarization/deploy
                                      from existing signed release artifacts.
                                      Only skips the pre-notarization manifest
                                      check when that manifest was never
                                      written; all signing, notarization and
                                      final readiness gates still run.
  LANTAI_RESUME_APP_NOTARY_SUBMISSION_ID=<uuid>
                                      Reuse an Apple app-archive submission
                                      whose local --wait transport was
                                      interrupted. Requires
                                      LANTAI_RESUME_NOTARIZATION=1 and only
                                      continues after Apple reports Accepted.
  LANTAI_RESUME_DMG_NOTARY_SUBMISSION_ID=<uuid>
                                      Reuse an Apple DMG submission after an
                                      interrupted validation/deploy step.
                                      Requires LANTAI_RESUME_NOTARIZATION=1
                                      and an existing signed DMG artifact.
  LANTAI_GITHUB_RELEASE_REPOSITORY=owner/repository
                                      Public GitHub Release repository for
                                      desktop auto-updates. Defaults to
                                      shenyangs/lantai3-releases.
  LANTAI_GITHUB_CLI=/absolute/path/to/gh
                                      GitHub CLI used by --publish-github-release.
                                      Defaults to ~/.local/bin/gh when present.
  LANTAI_AGENT_RELIABILITY_EVIDENCE=/absolute/path/to/evidence.json
                                      Required P0 reliability soak evidence for
                                      the exact official .app being deployed.
  LANTAI_PREVIOUS_RELEASE_APP=/absolute/path/to/previous.app
                                      Previous public app used for the required
                                      upgrade -> rollback -> re-upgrade test.
  LANTAI_PERSONA_EVIDENCE_A=/absolute/path/to/provider-a.json
  LANTAI_PERSONA_EVIDENCE_B=/absolute/path/to/provider-b.json
  LANTAI_PERSONA_REVIEW_A=/absolute/path/to/review-of-a.json
  LANTAI_PERSONA_REVIEW_B=/absolute/path/to/review-of-b.json
                                      Two distinct real-provider persona runs
                                      plus crossed semantic reviews, all bound
                                      to the exact release app.
  LANTAI_OFFICE_FIRST_TIER_EVIDENCE=/absolute/path/to/evidence.json
  LANTAI_OFFICE_PACKAGED_EVIDENCE=/absolute/path/to/evidence.json
  LANTAI_OFFICE_CORPUS_EVIDENCE=/absolute/path/to/evidence.json
  LANTAI_OFFICE_EDIT_ROUNDTRIP_EVIDENCE=/absolute/path/to/evidence.json
                                      Real-file and packaged Office evidence
                                      bound to the exact release app.
  LANTAI_DEFER_AGENT_RELIABILITY_SOAK=1
                                      Record the four-hour soak as deferred for
                                      an explicitly local/internal build. This
                                      never permits public GitHub publishing.
  LANTAI_RELEASE_CANDIDATE_ID=<id>    Required with --artifact-only. Adds a
                                      filesystem-safe identity to the release
                                      directory so dirty candidates cannot
                                      overwrite evidence from the same Git SHA.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build)
      SKIP_BUILD=1
      shift
      ;;
    --no-launch)
      NO_LAUNCH=1
      shift
      ;;
    --artifact-only)
      ARTIFACT_ONLY=1
      NO_LAUNCH=1
      shift
      ;;
    --prepare-release)
      PREPARE_RELEASE=1
      NO_LAUNCH=1
      shift
      ;;
    --publish-github-release)
      PUBLISH_GITHUB_RELEASE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

validate_prepare_release_request() {
  if [[ "$PREPARE_RELEASE" -ne 1 ]]; then
    return
  fi
  if [[ "$ARTIFACT_ONLY" -eq 1 || "$PUBLISH_GITHUB_RELEASE" -eq 1 ]]; then
    echo "Error: --prepare-release cannot be combined with --artifact-only or --publish-github-release." >&2
    exit 1
  fi
  if [[ -n "$RELEASE_CANDIDATE_ID" ]]; then
    echo "Error: --prepare-release cannot use LANTAI_RELEASE_CANDIDATE_ID." >&2
    exit 1
  fi
  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" != "0" || "$ALLOW_DIRTY_RELEASE_BUILD" != "0" \
    || "$FORCE_ADHOC_LOCAL_BUILD" != "0" || "$DEFER_AGENT_RELIABILITY_SOAK" != "0" \
    || "${LANTAI_SKIP_NOTARIZATION:-0}" != "0" || "${LANTAI_ALLOW_UNSIGNED_RELEASE_TAG:-0}" != "0" \
    || "${LANTAI_ALLOW_SAME_VERSION_REBUILD:-0}" != "0" ]]; then
    echo "Error: --prepare-release requires verification defaults; local-build and deferred-soak bypasses must be unset or 0." >&2
    exit 1
  fi
}

validate_artifact_only_request() {
  if [[ "$ARTIFACT_ONLY" -ne 1 ]]; then
    if [[ -n "$RELEASE_CANDIDATE_ID" ]]; then
      echo "Error: LANTAI_RELEASE_CANDIDATE_ID is only valid with --artifact-only." >&2
      exit 1
    fi
    return
  fi
  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" != "1" ]]; then
    echo "Error: --artifact-only requires LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1." >&2
    exit 1
  fi
  if [[ "$PUBLISH_GITHUB_RELEASE" -eq 1 ]]; then
    echo "Error: --artifact-only cannot publish a GitHub release." >&2
    exit 1
  fi
  if [[ -z "$RELEASE_CANDIDATE_ID" ]]; then
    echo "Error: --artifact-only requires LANTAI_RELEASE_CANDIDATE_ID." >&2
    exit 1
  fi
  if [[ ! "$RELEASE_CANDIDATE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    echo "Error: LANTAI_RELEASE_CANDIDATE_ID must be 1-64 filesystem-safe characters." >&2
    exit 1
  fi
}

validate_prepare_release_request
validate_artifact_only_request

acquire_release_build_lock

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd npm
require_cmd uv
require_cmd git
require_cmd ditto
require_cmd codesign
require_cmd find
require_cmd spctl
require_cmd xcrun
require_cmd hdiutil
require_cmd diskutil

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Python virtualenv not found: $VENV_PYTHON" >&2
  exit 1
fi

validate_release_dependency_locks() {
  echo "==> Validating complete release dependencies (locks, environment, imports)"
  # This is a hard release gate. A valid lockfile alone is not enough: the
  # active build environment must match it, critical runtime packages must be
  # direct dependencies, and the real backend entry path must import cleanly.
  "$VENV_PYTHON" "$ROOT_DIR/scripts/check_release_dependency_locks.py" \
    --write-manifest "$DEPENDENCY_MANIFEST"
}

validate_genoffice_upstream() {
  echo "==> Checking vendored GenOffice against upstream main (every ${GENOFFICE_UPSTREAM_CHECK_INTERVAL_HOURS}h)"
  local args=(
    --source-file "$ROOT_DIR/console/vendor/lantai-docx-editor/SOURCE.md"
    --write-report "$GENOFFICE_UPSTREAM_REPORT"
    --check-interval-hours "$GENOFFICE_UPSTREAM_CHECK_INTERVAL_HOURS"
    --allow-stale
  )
  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" ]]; then
    # A local-only smoke build may continue while offline.  Upstream changes
    # are reviewed on the scheduled three-day cadence, never silently synced.
    args+=(--allow-network-failure)
  fi
  "$VENV_PYTHON" "$GENOFFICE_UPSTREAM_SCRIPT" "${args[@]}"
}

find_developer_id_identity() {
  security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(Developer ID Application: [^"]*\)"/\1/p' \
    | head -n 1
}

normalize_codesign_identity() {
  local identity="$1"
  identity="${identity#Developer ID Application: }"
  identity="${identity#Apple Distribution: }"
  identity="${identity#Apple Development: }"
  printf '%s\n' "$identity"
}

configure_distribution_signing() {
  export CSC_IDENTITY_AUTO_DISCOVERY="false"

  if [[ "$FORCE_ADHOC_LOCAL_BUILD" =~ ^(1|true|yes|on)$ ]]; then
    if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" != "1" ]]; then
      cat >&2 <<'EOF'
Error: LANTAI_FORCE_ADHOC_LOCAL_BUILD=1 is restricted to same-machine smoke builds.
Set LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1 to acknowledge that the resulting
app/DMG/ZIP must not be distributed to another Mac.
EOF
      exit 1
    fi
    unset CSC_LINK CSC_NAME
    MAC_CODESIGN_IDENTITY="-"
    MAC_SIGNING_MODE="unsigned"
    echo "Warning: forcing an ad-hoc signed local-only build because LANTAI_FORCE_ADHOC_LOCAL_BUILD=1" >&2
    return
  fi

  if [[ -n "${CSC_LINK:-}" ]]; then
    MAC_SIGNING_MODE="developer-id"
    echo "==> Using signing certificate from CSC_LINK"
    return
  fi

  if [[ -z "$MAC_CODESIGN_IDENTITY" ]]; then
    MAC_CODESIGN_IDENTITY="$(find_developer_id_identity || true)"
  fi

  if [[ -n "$MAC_CODESIGN_IDENTITY" ]]; then
    export CSC_NAME="$(normalize_codesign_identity "$MAC_CODESIGN_IDENTITY")"
    MAC_SIGNING_MODE="developer-id"
    echo "==> Using Developer ID identity: $MAC_CODESIGN_IDENTITY"
    return
  fi

  MAC_SIGNING_MODE="unsigned"
  cat >&2 <<'EOF'
No Developer ID Application certificate was found for the direct macOS build.

This script is used for local DMG/ZIP distribution. Letting electron-builder
auto-pick Apple Distribution or Apple Development here can create a package that
opens on the build machine but fails on a fresh Mac when the bundled backend is
spawned.

Next steps:
  1. Install a `Developer ID Application` certificate, or
  2. Provide `CSC_LINK` / `CSC_KEY_PASSWORD`, or
  3. Set `LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1` only for a local smoke build
     that will not be shared to another Mac.
EOF

  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" != "1" ]]; then
    exit 1
  fi

  echo "Warning: continuing with an unsigned local-only build because LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1" >&2
}

codesign_leaf_authority() {
  local target="$1"
  codesign -dvv "$target" 2>&1 | sed -n 's/^Authority=//p' | head -n 1
}

validate_direct_distribution_signature() {
  local target="$1"
  local label="$2"
  local authority

  authority="$(codesign_leaf_authority "$target" || true)"
  if [[ -z "$authority" ]]; then
    if [[ "$MAC_SIGNING_MODE" == "developer-id" ]]; then
      echo "Error: $label is unsigned, but a Developer ID build was requested." >&2
      exit 1
    fi
    return
  fi

  case "$authority" in
    "Apple Distribution:"*|"Apple Development:"*)
      cat >&2 <<EOF
Error: $label was signed with an App Store/development identity:
  $authority

Direct local distribution must not use Apple Distribution or Apple Development.
Those identities can produce a package that passes on the build machine but
fails to start correctly on a fresh Mac.
EOF
      exit 1
      ;;
  esac

  if [[ "$MAC_SIGNING_MODE" == "developer-id" && "$authority" != "Developer ID Application:"* ]]; then
    echo "Error: $label was expected to use Developer ID Application, but got: $authority" >&2
    exit 1
  fi
}

has_notarization_credentials() {
  if [[ -n "${LANTAI_NOTARY_KEYCHAIN_PROFILE:-${APPLE_KEYCHAIN_PROFILE:-}}" ]]; then
    return 0
  fi

  if [[ -n "${APPLE_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" && -n "${APPLE_TEAM_ID:-}" ]]; then
    return 0
  fi

  if [[ -n "${APPLE_API_KEY:-}" && -n "${APPLE_API_KEY_ID:-}" && -n "${APPLE_API_ISSUER:-}" ]]; then
    return 0
  fi

  return 1
}

skip_notarization_requested() {
  [[ "${LANTAI_SKIP_NOTARIZATION:-0}" =~ ^(1|true|yes|on)$ ]]
}

require_notarization_credentials() {
  if [[ "$MAC_SIGNING_MODE" != "developer-id" ]]; then
    return
  fi

  if skip_notarization_requested; then
    if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" != "1" ]]; then
      cat >&2 <<'EOF'
Error: LANTAI_SKIP_NOTARIZATION=1 is only allowed for local smoke builds.
Set LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1 as an explicit acknowledgement that
the resulting app/DMG/ZIP must not be treated as redistributable.
EOF
      exit 1
    fi
    echo "Warning: skipping notarization credentials because LANTAI_SKIP_NOTARIZATION=1" >&2
    return
  fi

  if has_notarization_credentials; then
    echo "==> Notarization credentials detected"
    return
  fi

  cat >&2 <<'EOF'
Missing notarization credentials for a Developer ID build.

Provide one of the following:
  1. `LANTAI_NOTARY_KEYCHAIN_PROFILE` or `APPLE_KEYCHAIN_PROFILE`
  2. `APPLE_ID` + `APPLE_APP_SPECIFIC_PASSWORD` + `APPLE_TEAM_ID`
  3. `APPLE_API_KEY` + `APPLE_API_KEY_ID` + `APPLE_API_ISSUER`

If you only need a same-machine smoke build, set:
  LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1
EOF

  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" != "1" ]]; then
    exit 1
  fi

  echo "Warning: continuing without notarization credentials because LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1" >&2
}

build_notarytool_auth_args() {
  NOTARYTOOL_AUTH_ARGS=()

  if [[ -n "${LANTAI_NOTARY_KEYCHAIN_PROFILE:-${APPLE_KEYCHAIN_PROFILE:-}}" ]]; then
    if [[ -n "${LANTAI_NOTARY_KEYCHAIN:-${APPLE_KEYCHAIN:-}}" ]]; then
      NOTARYTOOL_AUTH_ARGS+=(
        --keychain "${LANTAI_NOTARY_KEYCHAIN:-${APPLE_KEYCHAIN}}"
      )
    fi
    NOTARYTOOL_AUTH_ARGS+=(
      --keychain-profile "${LANTAI_NOTARY_KEYCHAIN_PROFILE:-${APPLE_KEYCHAIN_PROFILE}}"
    )
    return
  fi

  if [[ -n "${APPLE_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" && -n "${APPLE_TEAM_ID:-}" ]]; then
    NOTARYTOOL_AUTH_ARGS+=(
      --apple-id "$APPLE_ID"
      --password "$APPLE_APP_SPECIFIC_PASSWORD"
      --team-id "$APPLE_TEAM_ID"
    )
    return
  fi

  if [[ -n "${APPLE_API_KEY:-}" && -n "${APPLE_API_KEY_ID:-}" && -n "${APPLE_API_ISSUER:-}" ]]; then
    NOTARYTOOL_AUTH_ARGS+=(
      --key "$APPLE_API_KEY"
      --key-id "$APPLE_API_KEY_ID"
      --issuer "$APPLE_API_ISSUER"
    )
  fi
}

APP_INFO="$(
  "$VENV_PYTHON" - "$ROOT_DIR" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
package_json = root / "console/package.json"
data = json.loads(package_json.read_text(encoding="utf-8"))
product_name = data.get("build", {}).get("productName", "澜台")
version = data.get("version")
if not version:
    raise SystemExit("console/package.json is missing version")
print(product_name)
print(version)
PY
)"

PRODUCT_NAME="$(printf '%s\n' "$APP_INFO" | sed -n '1p')"
VERSION="$(printf '%s\n' "$APP_INFO" | sed -n '2p')"

normalize_release_arch() {
  local arch="$1"
  if [[ -z "$arch" ]]; then
    arch="$(uname -m)"
  fi

  case "$arch" in
    arm64)
      printf '%s\n' "arm64"
      ;;
    x64|x86_64|amd64)
      printf '%s\n' "x64"
      ;;
    universal|universal2)
      printf '%s\n' "universal"
      ;;
    *)
      echo "Unsupported release architecture: $arch" >&2
      exit 1
      ;;
  esac
}

resolve_release_metadata() {
  SOURCE_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  SOURCE_SHORT_SHA="$(git -C "$ROOT_DIR" rev-parse --short=12 HEAD)"
  RELEASE_ARCH="$(normalize_release_arch "$RELEASE_ARCH")"
  RELEASE_DIR="$RELEASE_ROOT_DIR/builds/${VERSION}-${SOURCE_SHORT_SHA}-${RELEASE_ARCH}"
  if [[ -n "$RELEASE_CANDIDATE_ID" ]]; then
    RELEASE_DIR="${RELEASE_DIR}-candidate-${RELEASE_CANDIDATE_ID}"
  fi
  ARTIFACT_MANIFEST="$RELEASE_DIR/lantai-artifact-manifest.json"
  SUPPLY_CHAIN_MANIFEST="$RELEASE_DIR/lantai-supply-chain-manifest.json"
  SBOM_PATH="$RELEASE_DIR/lantai-sbom.cdx.json"
  NOTARY_RECORD_DIR="$RELEASE_DIR/notary-records"
  DOWNLOAD_APP="$DOWNLOADS_DIR/${PRODUCT_NAME}.app"
  DOWNLOAD_DMG="$DOWNLOADS_DIR/${PRODUCT_NAME}-${VERSION}-${RELEASE_ARCH}-internal.dmg"
  DOWNLOAD_ZIP="$DOWNLOADS_DIR/${PRODUCT_NAME}-${VERSION}-${RELEASE_ARCH}-internal.zip"
  LIFECYCLE_SMOKE_EVIDENCE="$RELEASE_DIR/release-evidence/official-packaged-lifecycle-smoke.json"
  RAPID_LIFECYCLE_SMOKE_EVIDENCE="$RELEASE_DIR/release-evidence/official-packaged-rapid-lifecycle-smoke.json"
  CUSTOMIZATION_UPGRADE_SMOKE_EVIDENCE="$RELEASE_DIR/release-evidence/official-packaged-customization-upgrade-smoke.json"
  PACKAGED_IMAGE_PARSER_MITIGATION_EVIDENCE="$RELEASE_DIR/release-evidence/official-packaged-image-parser-mitigation.json"
  PACKAGED_RUNTIME_COMPLETENESS_EVIDENCE="$RELEASE_DIR/release-evidence/official-packaged-runtime-completeness-smoke.json"
  UPGRADE_ROLLBACK_SMOKE_EVIDENCE="$RELEASE_DIR/release-evidence/official-packaged-upgrade-rollback-smoke.json"
  RUNTIME_MIGRATION_REPORT="$RELEASE_DIR/release-evidence/runtime-migration-dry-run-report.json"
  RUNTIME_MIGRATION_BACKUP_RESTORE_EVIDENCE="$RELEASE_DIR/release-evidence/runtime-migration-backup-restore-smoke.json"
}

validate_release_source_authority() {
  local args=(--root "$ROOT_DIR" --output "$RELEASE_SOURCE_AUTHORITY_REPORT")
  if [[ "$ARTIFACT_ONLY" -eq 1 ]]; then
    args+=(--artifact-only)
  fi
  echo "==> Validating canonical release source authority"
  "$VENV_PYTHON" "$RELEASE_SOURCE_AUTHORITY_SCRIPT" "${args[@]}"
}

validate_release_version_increment() {
  if [[ "$SKIP_BUILD" -eq 1 ]]; then
    echo "==> Skipping release version increment gate for --skip-build"
    return
  fi

  echo "==> Validating release version increment"
  local args=(
    --target-version "$VERSION"
    --deployed-app "$DOWNLOAD_APP"
    --package-lock "$CONSOLE_DIR/package-lock.json"
  )
  if [[ "${LANTAI_ALLOW_SAME_VERSION_REBUILD:-0}" =~ ^(1|true|yes|on)$ ]]; then
    if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" != "1" ]]; then
      echo "Error: same-version rebuilds require LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1." >&2
      exit 1
    fi
    echo "Warning: explicitly allowing same-version preview replacement." >&2
    args+=(--allow-same-version-rebuild)
  fi
  "$VENV_PYTHON" "$ROOT_DIR/scripts/check_release_version_increment.py" "${args[@]}"
}

validate_source_version_coherence() {
  echo "==> Validating release source version coherence"
  "$VENV_PYTHON" "$ROOT_DIR/scripts/check_release_version_coherence.py" \
    --project-root "$ROOT_DIR" \
    --expected-version "$VERSION"
}

refresh_release_python_project_metadata() {
  if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv is required to refresh release Python metadata." >&2
    exit 1
  fi
  echo "==> Refreshing local Lantai distribution metadata"
  (
    cd "$ROOT_DIR"
    UV_PYTHON_DOWNLOADS=never uv sync --locked --extra dev --reinstall-package lantai
  )
  local installed_version
  installed_version="$(
    "$VENV_PYTHON" -c 'import importlib.metadata; print(importlib.metadata.version("lantai"))'
  )"
  if [[ "$installed_version" != "$VERSION" ]]; then
    echo "Error: refreshed Lantai metadata is $installed_version, expected $VERSION." >&2
    exit 1
  fi
}

validate_foundation_experience_gate() {
  echo "==> Enforcing foundation experience preservation ratchet"
  # Source tests may need an empty profile and checkout-local Node modules.
  # Keep those overrides out of packaged App and upgrade/lifecycle validation.
  local validation_env=()
  if [[ -n "${LANTAI_SOURCE_VALIDATION_WORKING_DIR:-}" || -n "${LANTAI_SOURCE_VALIDATION_SECRET_DIR:-}" ]]; then
    if [[ -z "${LANTAI_SOURCE_VALIDATION_WORKING_DIR:-}" || -z "${LANTAI_SOURCE_VALIDATION_SECRET_DIR:-}" ]]; then
      echo "Error: source validation working and secret directories must be set together." >&2
      return 1
    fi
    validation_env+=("LANTAI_WORKING_DIR=$LANTAI_SOURCE_VALIDATION_WORKING_DIR")
    validation_env+=("LANTAI_SECRET_DIR=$LANTAI_SOURCE_VALIDATION_SECRET_DIR")
  fi
  if [[ -n "${LANTAI_SOURCE_VALIDATION_PPTXGENJS_MODULE:-}" ]]; then
    validation_env+=("LANTAI_PPTXGENJS_MODULE=$LANTAI_SOURCE_VALIDATION_PPTXGENJS_MODULE")
  fi
  env "${validation_env[@]}" "$VENV_PYTHON" "$ROOT_DIR/scripts/check_foundation_experience_gate.py" --run-verifications
}

validate_release_source_state() {
  echo "==> Validating release source state"

  "$VENV_PYTHON" "$ROOT_DIR/scripts/worktree_guard.py" \
    --root "$ROOT_DIR" check --require-no-active-lease || {
      echo "Error: release build refused while another task owns the mutation lease." >&2
      exit 1
    }

  if ! git -C "$ROOT_DIR" diff --quiet --ignore-submodules --; then
    SOURCE_DIRTY="1"
  fi
  if ! git -C "$ROOT_DIR" diff --cached --quiet --ignore-submodules --; then
    SOURCE_DIRTY="1"
  fi
  if [[ -n "$(git -C "$ROOT_DIR" ls-files --others --exclude-standard)" ]]; then
    SOURCE_DIRTY="1"
  fi

  if [[ "$SOURCE_DIRTY" == "1" && "$ALLOW_DIRTY_RELEASE_BUILD" != "1" ]]; then
    cat >&2 <<'EOF'
Error: release build requires a clean git worktree.

Commit or remove local changes before building a distributable package. For a
local smoke build that will not be shared, set LANTAI_ALLOW_DIRTY_RELEASE_BUILD=1
and keep LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1 explicit in the environment.
EOF
    exit 1
  fi

  if [[ "$SOURCE_DIRTY" == "1" ]]; then
    echo "Warning: source tree is dirty; provenance manifest will record dirty=true." >&2
  fi
}

validate_release_tag_metadata() {
  local expected_tag="${LANTAI_RELEASE_TAG:-v$VERSION}"
  local tag_sha

  if git -C "$ROOT_DIR" rev-parse -q --verify "refs/tags/$expected_tag" >/dev/null; then
    tag_sha="$(git -C "$ROOT_DIR" rev-list -n 1 "$expected_tag")"
    if [[ "$tag_sha" != "$SOURCE_SHA" ]]; then
      echo "Error: release tag $expected_tag points at $tag_sha, not $SOURCE_SHA" >&2
      exit 1
    fi
    if [[ "${LANTAI_ALLOW_UNSIGNED_RELEASE_TAG:-0}" != "1" ]]; then
      git -C "$ROOT_DIR" tag -v "$expected_tag" >/dev/null 2>&1 || {
        echo "Error: release tag $expected_tag is not GPG-verifiable." >&2
        echo "Set LANTAI_ALLOW_UNSIGNED_RELEASE_TAG=1 only for a local smoke build." >&2
        exit 1
      }
    fi
    RELEASE_TAG="$expected_tag"
    return
  fi

  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" != "1" ]]; then
    cat >&2 <<EOF
Error: release tag $expected_tag was not found.

Create a signed tag for the package version or set LANTAI_RELEASE_TAG to the
intended tag. For local smoke only, set LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1.
EOF
    exit 1
  fi

  echo "Warning: continuing without release tag $expected_tag because LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1" >&2
}

prepare_release_output_dir() {
  if [[ "$SKIP_BUILD" -eq 1 ]]; then
    if [[ ! -d "$RELEASE_DIR" ]]; then
      echo "Release output dir not found for --skip-build: $RELEASE_DIR" >&2
      exit 1
    fi
    return
  fi

  rm -rf "$RELEASE_DIR"
  mkdir -p "$RELEASE_DIR"
  rm -rf "$NOTARY_RECORD_DIR"
}

fail_if_multiple_paths() {
  local label="$1"
  local count="$2"
  if [[ "$count" -gt 1 ]]; then
    echo "Ambiguous $label under $RELEASE_DIR; refusing to guess from stale artifacts." >&2
    exit 1
  fi
}

resolve_release_metadata
validate_release_source_authority
validate_release_version_increment
validate_source_version_coherence
validate_foundation_experience_gate
validate_release_source_state
validate_release_tag_metadata
validate_genoffice_upstream
refresh_release_python_project_metadata
validate_release_dependency_locks
configure_distribution_signing
require_notarization_credentials
prepare_release_output_dir

build_backend() {
  echo "==> Building Python backend"
  (
    cd "$ROOT_DIR"
    export LANTAI_PYINSTALLER_DISTRIBUTION="official"
    export LANTAI_PYINSTALLER_TARGET_ARCH="$RELEASE_ARCH"
    export LANTAI_PYINSTALLER_CODESIGN_IDENTITY="${MAC_CODESIGN_IDENTITY:--}"
    "$VENV_PYTHON" electron/build-python.py
  )
}

build_mac_use_helper() {
  echo "==> Building Mac Use helper"
  (
    cd "$CONSOLE_DIR"
    npm run build:mac-use-helper
  )
}

build_console() {
  echo "==> Building web console"
  (
    cd "$CONSOLE_DIR"
    npm ci
    electron_config_cache="${LANTAI_ELECTRON_CACHE_DIR:-$ROOT_DIR/build/electron-cache}" \
      npm run ensure:electron-runtime
    npm run build
  )
}

write_embedded_release_build_identity() {
  local args=(
    --root "$ROOT_DIR"
    --output "$CONSOLE_DIR/dist/release-provenance.json"
    --version "$VERSION"
    --source-sha "$SOURCE_SHA"
  )
  if [[ "$ARTIFACT_ONLY" == "1" ]]; then
    args+=(--artifact-only --candidate-id "$RELEASE_CANDIDATE_ID")
  elif [[ "$(git -C "$ROOT_DIR" symbolic-ref --short -q HEAD || true)" == "main" ]] && \
     [[ "$(git -C "$ROOT_DIR" rev-parse refs/heads/main)" == "$SOURCE_SHA" ]]; then
    args+=(--canonical-main)
  fi
  echo "==> Embedding release build identity"
  "$VENV_PYTHON" "$RELEASE_BUILD_IDENTITY_SCRIPT" "${args[@]}"
}

validate_console_product_contract() {
  echo "==> Validating built console product contract"
  "$VENV_PYTHON" "$RELEASE_PRODUCT_CONTRACT_SCRIPT" \
    --console-dist "$CONSOLE_DIR/dist" \
    --expected-sha "$SOURCE_SHA" \
    --expected-version "$VERSION"
}

validate_packaged_product_contract() {
  local release_app report_path
  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found while validating the product contract." >&2
    exit 1
  fi
  report_path="$RELEASE_DIR/release-evidence/release-product-contract.json"
  echo "==> Validating packaged App product contract and embedded Git SHA"
  "$VENV_PYTHON" "$RELEASE_PRODUCT_CONTRACT_SCRIPT" \
    --app "$release_app" \
    --expected-sha "$SOURCE_SHA" \
    --expected-version "$VERSION" \
    --output "$report_path"
}

validate_packaged_mac_use_helper() {
  local release_app helper_app helper_executable helper_version
  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found while validating Mac Use helper." >&2
    exit 1
  fi

  helper_app="$release_app/Contents/Resources/mac-use/Lantai Mac Use Helper.app"
  helper_executable="$helper_app/Contents/MacOS/lantai-mac-use-helper"
  if [[ ! -x "$helper_executable" ]]; then
    echo "Packaged Mac Use helper is missing or not executable: $helper_executable" >&2
    exit 1
  fi

  echo "==> Validating packaged Mac Use helper"
  xcrun lipo "$helper_executable" -verify_arch arm64
  xcrun lipo "$helper_executable" -verify_arch x86_64
  helper_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$helper_app/Contents/Info.plist")"
  if [[ "$helper_version" != "$VERSION" ]]; then
    echo "Packaged Mac Use helper version mismatch: $helper_version (expected: $VERSION)." >&2
    exit 1
  fi
  codesign --verify --deep --strict --verbose=2 "$helper_app"
}

run_packaged_mac_use_helper_smoke() {
  local release_app app_executable smoke_dir response_path diagnostics_path stdout_path evidence_path user_data_dir app_pid app_status
  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found while running Mac Use helper smoke." >&2
    exit 1
  fi
  app_executable="$release_app/Contents/MacOS/$PRODUCT_NAME"
  if [[ ! -x "$app_executable" ]]; then
    echo "Packaged app executable is missing for Mac Use smoke: $app_executable" >&2
    exit 1
  fi
  smoke_dir="$(mktemp -d "${TMPDIR:-/tmp}/lantai-mac-use-package-smoke.XXXXXX")"
  response_path="$smoke_dir/response.json"
  diagnostics_path="$smoke_dir/diagnostics.log"
  stdout_path="$smoke_dir/stdout.log"
  user_data_dir="$smoke_dir/user-data"
  evidence_path="$RELEASE_DIR/release-evidence/mac-use-helper-smoke.json"
  mkdir -p "$(dirname "$evidence_path")"
  : > "$diagnostics_path"
  : > "$stdout_path"
  mkdir -p "$user_data_dir"

  echo "==> Running packaged Mac Use bridge and helper smoke"
  env -u ELECTRON_RUN_AS_NODE \
    LANTAI_MAC_USE_PACKAGED_SMOKE_FILE="$response_path" \
    LANTAI_DESKTOP_USER_DATA_DIR="$user_data_dir" \
    LANTAI_DESKTOP_HEADLESS_SMOKE=1 \
    "$app_executable" >"$stdout_path" 2>"$diagnostics_path" &
  app_pid=$!
  for _ in {1..120}; do
    if ! kill -0 "$app_pid" 2>/dev/null; then
      break
    fi
    sleep 0.25
  done
  if kill -0 "$app_pid" 2>/dev/null; then
    kill -TERM "$app_pid" 2>/dev/null || true
    wait "$app_pid" 2>/dev/null || true
    cat "$diagnostics_path" >&2
    rm -rf "$smoke_dir"
    echo "Packaged Mac Use smoke timed out." >&2
    exit 1
  fi
  set +e
  wait "$app_pid"
  app_status=$?
  set -e
  if [[ "$app_status" -ne 0 || ! -s "$response_path" ]]; then
    cat "$diagnostics_path" >&2
    rm -rf "$smoke_dir"
    echo "Packaged Mac Use smoke failed with exit code $app_status." >&2
    exit 1
  fi
  "$VENV_PYTHON" - "$response_path" "$evidence_path" "$release_app" <<'PY'
import json
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])
release_app = pathlib.Path(sys.argv[3]).resolve()
payload = json.loads(source.read_text(encoding="utf-8"))
assert payload.get("ok") is True, payload
assert payload.get("schema") == "com.lantai.mac-use-packaged-smoke.v1", payload
assert payload.get("protocol_version") == 1, payload
for name in ("status", "windows"):
    result = payload.get(name) or {}
    assert result.get("ok") is True, payload
    assert result.get("dispatch_status") == "not_sent", payload
    assert result.get("effect_status") == "not_applicable", payload
assert isinstance(payload["windows"].get("count"), int), payload
assert payload["windows"]["count"] >= 0, payload
helper_path = pathlib.Path(payload.get("helper_path") or "").resolve()
helper_path.relative_to(release_app)
output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  rm -rf "$smoke_dir"
}

sync_console_static() {
  echo "==> Syncing web console into Python package"
  (
    cd "$ROOT_DIR"
    "$VENV_PYTHON" scripts/check_console_static_fresh.py --sync
  )
}

prepare_console_assets() {
  if [[ -f "$ICON_SOURCE" ]]; then
    echo "==> Preparing Electron icon"
    mkdir -p "$CONSOLE_BUILD_DIR"
    ditto "$ICON_SOURCE" "$CONSOLE_BUILD_DIR/icon.icns"
  else
    echo "Warning: Electron icon source not found: $ICON_SOURCE" >&2
  fi
}

build_electron() {
  echo "==> Building Electron release"
  (
    cd "$CONSOLE_DIR"
    local attempt max_attempts output_file retry_delay status
    local builder_args=(
      --config.directories.output="$RELEASE_DIR"
      --config.mac.identity=null
    )
    local prepared_electron_dist="$CONSOLE_DIR/node_modules/electron/dist"
    if [[ -d "$prepared_electron_dist/Electron.app" ]]; then
      # `build_console` has already restored and checksum-verified this exact
      # Electron runtime. Point electron-builder at it explicitly so a release
      # build stays reproducible when GitHub is unavailable or network access
      # is intentionally restricted.
      builder_args+=(--config.electronDist="$prepared_electron_dist")
    fi
    # Assemble first, then sign every Mach-O leaf with our own retry-aware
    # signer. electron-builder signs thousands of files as one opaque step;
    # one transient Apple TSA response otherwise discards the whole 10-15
    # minute pass. The explicit post-package signer preserves entitlements,
    # signs depth-first, retries only the failing file, then seals the root.
    max_attempts="${LANTAI_CODESIGN_TIMESTAMP_MAX_ATTEMPTS:-5}"
    if [[ ! "$max_attempts" =~ ^[1-9][0-9]*$ ]]; then
      echo "Error: LANTAI_CODESIGN_TIMESTAMP_MAX_ATTEMPTS must be a positive integer." >&2
      exit 1
    fi
    for ((attempt = 1; attempt <= max_attempts; attempt++)); do
      echo "==> Packaging and signing Electron app (attempt $attempt/$max_attempts)"
      output_file="$(mktemp "${TMPDIR:-/tmp}/lantai-electron-builder.XXXXXX")"
      set +e
      npm exec --offline -- electron-builder "${builder_args[@]}" 2>&1 | tee "$output_file"
      status=${PIPESTATUS[0]}
      set -e
      if [[ "$status" -eq 0 ]]; then
        rm -f "$output_file"
        return 0
      fi
      if [[ "$attempt" -lt "$max_attempts" ]] \
        && grep -Eqi 'timestamp service is not available|timestamp.*(timed out|unavailable)|a timestamp was expected but was not found|network connection was lost|NSURLErrorDomain' "$output_file"; then
        rm -f "$output_file"
        retry_delay=$((attempt * 10))
        echo "Warning: transient Apple timestamp failure while signing the app; retrying in ${retry_delay}s." >&2
        sleep "$retry_delay"
        continue
      fi
      rm -f "$output_file"
      return "$status"
    done
  )
}

codesign_with_timestamp_retry() {
  local label="$1"
  shift
  local attempt max_attempts output_file retry_delay status

  max_attempts="${LANTAI_CODESIGN_TIMESTAMP_MAX_ATTEMPTS:-5}"
  if [[ ! "$max_attempts" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: LANTAI_CODESIGN_TIMESTAMP_MAX_ATTEMPTS must be a positive integer." >&2
    return 1
  fi

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    output_file="$(mktemp "${TMPDIR:-/tmp}/lantai-codesign-timestamp.XXXXXX")"
    set +e
    "$@" >"$output_file" 2>&1
    status=$?
    set -e
    cat "$output_file"
    if [[ "$status" -eq 0 ]]; then
      rm -f "$output_file"
      return 0
    fi
    if [[ "$attempt" -lt "$max_attempts" ]] \
      && grep -Eqi 'timestamp service is not available|timestamp.*(timed out|unavailable)|a timestamp was expected but was not found|network connection was lost|NSURLErrorDomain' "$output_file"; then
      rm -f "$output_file"
      retry_delay=$((attempt * 10))
      echo "Warning: transient Apple timestamp failure while signing ${label}; retrying in ${retry_delay}s (attempt $attempt/$max_attempts)." >&2
      sleep "$retry_delay"
      continue
    fi
    rm -f "$output_file"
    return "$status"
  done
}

sign_packaged_macho_leaves_for_distribution() {
  local release_app identity preserve_metadata helper_entitlements bundle_entitlements path bundle signed_leaves=0 signed_bundles=0

  # electron-builder signs the app bundle, but some embedded native runtimes
  # (notably Electron helper libraries and our Office engines) can retain their
  # upstream signatures. Local `codesign --deep` accepts that state, whereas
  # Apple's notarization service correctly rejects those executable leaves.
  # Sign inner executable files first, then their containing bundles, and only
  # then the root app so every notarized executable has the same Developer ID,
  # secure timestamp, and hardened-runtime option.
  if [[ "$MAC_SIGNING_MODE" != "developer-id" ]]; then
    return
  fi

  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found for distribution signing under $RELEASE_DIR" >&2
    exit 1
  fi

  identity="$MAC_CODESIGN_IDENTITY"
  if [[ -z "$identity" ]]; then
    identity="$(find_developer_id_identity || true)"
  fi
  if [[ -z "$identity" && -n "${CSC_NAME:-}" ]]; then
    identity="Developer ID Application: ${CSC_NAME}"
  fi
  if [[ -z "$identity" ]]; then
    echo "Error: missing Developer ID Application identity for packaged app signing." >&2
    exit 1
  fi

  preserve_metadata="identifier,entitlements,requirements,flags,runtime"
  helper_entitlements="$CONSOLE_DIR/node_modules/app-builder-lib/templates/entitlements.mac.plist"
  if [[ ! -f "$helper_entitlements" ]]; then
    echo "Error: missing Electron Helper entitlements: $helper_entitlements" >&2
    exit 1
  fi
  echo "==> Signing embedded Mach-O executables for notarization"
  while IFS= read -r -d '' path; do
    if file -b "$path" | grep -q 'Mach-O'; then
      codesign_with_timestamp_retry "embedded Mach-O $path" \
        codesign --force --sign "$identity" --timestamp --options runtime \
          --preserve-metadata="$preserve_metadata" "$path"
      signed_leaves=$((signed_leaves + 1))
    fi
  done < <(find "$release_app" -type f -print0)

  while IFS= read -r -d '' bundle; do
    if [[ "$bundle" == *.app ]]; then
      # Electron's renderer/GPU/plugin helpers need JIT and unsigned
      # executable-memory entitlements. The unsigned assembly stage cannot
      # preserve them, so restore the same minimal Helper policy that
      # electron-builder applies during its normal signing path.
      bundle_entitlements="$helper_entitlements"
      if [[ "$bundle" == "$release_app/Contents/Frameworks/"*" Helper"*.app ]]; then
        bundle_entitlements="$CONSOLE_DIR/electron/entitlements/developer-id.electron-helper.plist"
      fi
      codesign_with_timestamp_retry "nested app $bundle" \
        codesign --force --sign "$identity" --timestamp --options runtime \
          --entitlements "$bundle_entitlements" "$bundle"
    else
      codesign_with_timestamp_retry "nested framework $bundle" \
        codesign --force --sign "$identity" --timestamp --options runtime \
          --preserve-metadata="$preserve_metadata" "$bundle"
    fi
    signed_bundles=$((signed_bundles + 1))
  done < <(find "$release_app/Contents" -depth -type d \( -name '*.app' -o -name '*.framework' \) -print0)

  codesign_with_timestamp_retry "root app $release_app" \
    codesign --force --sign "$identity" --timestamp --options runtime \
      --entitlements "$CONSOLE_DIR/electron/entitlements/developer-id.app.plist" \
      "$release_app"
  codesign --verify --deep --strict --verbose=2 "$release_app"
  echo "==> Distribution signing complete: $signed_leaves Mach-O leaves, $signed_bundles nested bundles"
}

run_packaged_skill_runtime_smoke() {
  local release_app
  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found for Skill runtime smoke under $RELEASE_DIR" >&2
    exit 1
  fi
  echo "==> Running packaged Skill runtime smoke"
  "$VENV_PYTHON" "$PACKAGED_SKILL_RUNTIME_SMOKE_SCRIPT" \
    --app "$release_app" \
    --timeout 120 \
    --output "$PACKAGED_RUNTIME_COMPLETENESS_EVIDENCE"
}

find_release_file() {
  local pattern="$1"
  local found="" count=0 candidate

  while IFS= read -r candidate; do
    count=$((count + 1))
    found="$candidate"
  done < <(find "$RELEASE_DIR" -maxdepth 1 -type f -name "$pattern" ! -name '*.blockmap')

  fail_if_multiple_paths "release artifact matching $pattern" "$count"
  if [[ "$count" -eq 1 ]]; then
    printf '%s\n' "$found"
  fi
}

find_release_app() {
  local app_path="" count=0 candidate
  for _ in {1..40}; do
    count=0
    app_path=""
    while IFS= read -r candidate; do
      count=$((count + 1))
      app_path="$candidate"
    done < <(find "$RELEASE_DIR" -maxdepth 2 -type d -name "${PRODUCT_NAME}.app" -path '*/mac-*/*')

    fail_if_multiple_paths "release app bundle" "$count"
    if [[ "$count" -eq 1 && -d "$app_path/Contents/MacOS" ]]; then
      printf '%s\n' "$app_path"
      return
    fi
    sleep 0.25
  done
}

validate_packaged_version_coherence() {
  local release_app
  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found for version coherence gate under $RELEASE_DIR" >&2
    exit 1
  fi
  echo "==> Validating packaged app and backend version coherence"
  "$VENV_PYTHON" "$ROOT_DIR/scripts/check_release_version_coherence.py" \
    --project-root "$ROOT_DIR" \
    --expected-version "$VERSION" \
    --packaged-app "$release_app"
}

validate_release_bundle_signature() {
  local release_app release_backend_app release_server release_officecli officecli_smoke_dir officecli_smoke_copy officecli_smoke_status
  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found under $RELEASE_DIR/mac-*" >&2
    exit 1
  fi

  release_backend_app="$release_app/Contents/Resources/lantai-server/Lantai Backend.app"
  release_server="$release_backend_app/Contents/MacOS/lantai-server"
  if [[ ! -x "$release_server" ]]; then
    echo "Bundled backend not found: $release_server" >&2
    exit 1
  fi

  echo "==> Validating macOS signing channel"
  validate_direct_distribution_signature "$release_app" "App bundle"
  validate_direct_distribution_signature "$release_backend_app" "Calendar backend helper app"
  validate_direct_distribution_signature "$release_server" "Bundled backend"

  release_officecli="$release_app/Contents/Resources/lantai-server/bin/officecli"
  if [[ -x "$release_officecli" ]]; then
    validate_direct_distribution_signature "$release_officecli" "Bundled OfficeCLI"
    # OfficeCLI may rewrite its own executable during startup even with its
    # updater disabled. Running the signed bundle resource in place invalidates
    # the outer app signature, so smoke only a disposable copy.
    officecli_smoke_dir="$(mktemp -d "${TMPDIR:-/tmp}/lantai-officecli-release-smoke.XXXXXX")"
    officecli_smoke_copy="$officecli_smoke_dir/officecli"
    cp -p "$release_officecli" "$officecli_smoke_copy"
    set +e
    OFFICECLI_SKIP_UPDATE=1 "$officecli_smoke_copy" --version >/dev/null
    officecli_smoke_status=$?
    set -e
    rm -f "$officecli_smoke_copy"
    rmdir "$officecli_smoke_dir"
    if [[ "$officecli_smoke_status" -ne 0 ]]; then
      echo "Bundled OfficeCLI version smoke failed with code $officecli_smoke_status" >&2
      exit "$officecli_smoke_status"
    fi
  elif [[ "${LANTAI_REQUIRE_BUNDLED_OFFICECLI:-0}" =~ ^(1|true|yes|on)$ ]]; then
    echo "Bundled OfficeCLI not found: $release_officecli" >&2
    exit 1
  fi
}

notarize_file_with_notarytool() {
  local file_path="$1"
  local label="$2"
  local output_file="$3"
  local output notary_s3_arg attempt max_attempts retry_delay

  build_notarytool_auth_args
  if [[ "${#NOTARYTOOL_AUTH_ARGS[@]}" -eq 0 ]]; then
    echo "Error: missing notarytool authentication arguments for $label" >&2
    exit 1
  fi

  # Apple's S3 transfer acceleration endpoint is fragile behind some VPN/TUN
  # routes. Keep direct uploads as the default; opt into acceleration only when
  # the network is known to support it.
  notary_s3_arg="--no-s3-acceleration"
  if [[ "${LANTAI_NOTARY_S3_ACCELERATION:-0}" =~ ^(1|true|yes|on)$ ]]; then
    notary_s3_arg="--s3-acceleration"
  fi

  max_attempts="${LANTAI_NOTARY_MAX_ATTEMPTS:-2}"
  if [[ ! "$max_attempts" =~ ^[12]$ ]]; then
    echo "Error: LANTAI_NOTARY_MAX_ATTEMPTS must be 1 or 2." >&2
    exit 1
  fi

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    echo "==> Notarizing $label (attempt $attempt/$max_attempts)"
    if output="$(
      xcrun notarytool submit "$file_path" \
      "${NOTARYTOOL_AUTH_ARGS[@]}" \
      "$notary_s3_arg" \
      --wait \
      --output-format json 2>&1
    )"; then
      printf '%s\n' "$output" >"$output_file"
      # A successful transport is not Apple's acceptance. Invalid, pending,
      # malformed and missing-identity responses are durable failures and are
      # never retried as an upload timeout.
      "${VENV_PYTHON:-/usr/bin/python3}" - "$output_file" <<'PY'
import json
import re
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
identity = payload.get("id") or payload.get("submissionId")
if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identity):
    raise SystemExit("notarytool response has no valid submission identity")
if payload.get("status") != "Accepted":
    raise SystemExit(f"Apple notarization was not accepted: {payload.get('status')!r}")
PY
      return
    fi

    if [[ "$output" == *"deadlineExceeded" || "$output" == *"connectTimeout" || "$output" == *"networkConnectionLost" || "$output" == *"network connection was lost" || "$output" == *"NSURLErrorDomain Code=-1001" || "$output" == *"NSURLErrorDomain Code=-1005" || "$output" == *"timed out" ]] \
      && (( attempt < max_attempts )); then
      retry_delay=$((attempt * 10))
      echo "Warning: transient notary upload failure for $label; retrying in ${retry_delay}s." >&2
      sleep "$retry_delay"
      continue
    fi

    echo "Error: notarytool submit failed for $label" >&2
    printf '%s\n' "$output" >&2
    exit 1
  done
}

verify_existing_notary_submission() {
  local submission_id="$1"
  local label="$2"
  local output_file="$3"
  local output status

  build_notarytool_auth_args
  if [[ "${#NOTARYTOOL_AUTH_ARGS[@]}" -eq 0 ]]; then
    echo "Error: missing notarytool authentication arguments for $label" >&2
    exit 1
  fi

  echo "==> Checking existing Apple notarization submission for $label: $submission_id"
  if ! output="$(
    xcrun notarytool info "$submission_id" \
      "${NOTARYTOOL_AUTH_ARGS[@]}" \
      --output-format json 2>&1
  )"; then
    echo "Error: unable to read existing Apple notarization submission $submission_id" >&2
    printf '%s\n' "$output" >&2
    exit 1
  fi
  printf '%s\n' "$output" >"$output_file"

  status="$(printf '%s' "$output" | "$VENV_PYTHON" -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError as error:
    raise SystemExit(f"notarytool info did not return JSON: {error}")
status = payload.get("status")
if not isinstance(status, str) or not status:
    raise SystemExit("notarytool info response has no status")
print(status)
')"

  case "$status" in
    Accepted)
      return
      ;;
    In\ Progress|In\ Progress*)
      echo "Apple has not finished notarizing $label (status: $status). Retry after it reaches Accepted; no artifacts were deployed." >&2
      exit 1
      ;;
    *)
      echo "Apple notarization for $label is not accepted (status: $status). Refusing to staple or deploy." >&2
      exit 1
      ;;
  esac
}

staple_and_validate() {
  local target="$1"
  local label="$2"
  local attempt output_file

  echo "==> Stapling $label"
  xcrun stapler staple -v "$target"
  for attempt in 1 2 3; do
    echo "==> Validating staple for $label (attempt $attempt/3)"
    output_file="$(mktemp "${TMPDIR:-/tmp}/lantai-stapler-validate.XXXXXX")"
    if xcrun stapler validate "$target" >"$output_file" 2>&1; then
      cat "$output_file"
      rm -f "$output_file"
      return
    fi
    cat "$output_file" >&2
    if [[ "$attempt" -lt 3 ]] \
      && grep -Eq 'NSURLErrorDomain|network connection was lost|timed out|CloudKit.s response is inconsistent' "$output_file"; then
      rm -f "$output_file"
      echo "Warning: transient stapler validation failure for $label; retrying in 5s." >&2
      sleep 5
      continue
    fi
    rm -f "$output_file"
    return 1
  done
}

sign_dmg_for_distribution() {
  local dmg_path="$1"
  local identity="$MAC_CODESIGN_IDENTITY"
  local attempt max_attempts output_file retry_delay

  if [[ "$MAC_SIGNING_MODE" != "developer-id" ]]; then
    return
  fi

  if [[ -z "$identity" ]]; then
    identity="$(find_developer_id_identity || true)"
  fi

  if [[ -z "$identity" && -n "${CSC_NAME:-}" ]]; then
    identity="Developer ID Application: ${CSC_NAME}"
  fi

  if [[ -z "$identity" ]]; then
    echo "Error: missing Developer ID Application identity for DMG signing." >&2
    exit 1
  fi

  max_attempts="${LANTAI_CODESIGN_TIMESTAMP_MAX_ATTEMPTS:-3}"
  if [[ ! "$max_attempts" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: LANTAI_CODESIGN_TIMESTAMP_MAX_ATTEMPTS must be a positive integer." >&2
    exit 1
  fi

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    echo "==> Signing DMG (attempt $attempt/$max_attempts)"
    output_file="$(mktemp "${TMPDIR:-/tmp}/lantai-codesign-dmg.XXXXXX")"
    if codesign --force --sign "$identity" --timestamp "$dmg_path" >"$output_file" 2>&1; then
      cat "$output_file"
      rm -f "$output_file"
      codesign --verify --verbose=2 "$dmg_path"
      return
    fi

    cat "$output_file" >&2
    if [[ "$attempt" -lt "$max_attempts" ]] \
      && grep -Eqi 'timestamp service is not available|timestamp.*(timed out|unavailable)|a timestamp was expected but was not found|network connection was lost|NSURLErrorDomain' "$output_file"; then
      rm -f "$output_file"
      retry_delay=$((attempt * 10))
      echo "Warning: transient Apple timestamp failure while signing DMG; retrying in ${retry_delay}s." >&2
      sleep "$retry_delay"
      continue
    fi

    rm -f "$output_file"
    return 1
  done
}

validate_release_dmg_installer_layout() {
  local dmg_path="$1"
  local mount_dir background_dir background_tiff volume_name expected_volume_name ds_store_text

  mount_dir="$(mktemp -d "$RELEASE_DIR/dmg-layout-verify.XXXXXX")"
  hdiutil attach -nobrowse -readonly -mountpoint "$mount_dir" "$dmg_path" >/dev/null
  if [[ ! -L "$mount_dir/Applications" ]] \
    || [[ "$(readlink "$mount_dir/Applications")" != "/Applications" ]] \
    || [[ ! -f "$mount_dir/.DS_Store" ]]; then
    hdiutil detach "$mount_dir" -quiet || true
    rmdir "$mount_dir" || true
    echo "Rebuilt DMG lost the required Finder install layout." >&2
    exit 1
  fi
  background_dir="$mount_dir/.background"
  background_tiff="$mount_dir/.background.tiff"
  if { [[ ! -d "$background_dir" ]] || ! find "$background_dir" -type f -print -quit | grep -q .; } \
    && [[ ! -s "$background_tiff" ]]; then
    hdiutil detach "$mount_dir" -quiet || true
    rmdir "$mount_dir" || true
    echo "Rebuilt DMG lost its installer background." >&2
    exit 1
  fi
  volume_name="$(diskutil info "$mount_dir" | sed -n 's/^[[:space:]]*Volume Name:[[:space:]]*//p' | head -n 1)"
  expected_volume_name="${PRODUCT_NAME} ${VERSION}-${RELEASE_ARCH}"
  if [[ "$volume_name" != "$expected_volume_name" ]]; then
    hdiutil detach "$mount_dir" -quiet || true
    rmdir "$mount_dir" || true
    echo "Rebuilt DMG has an unexpected Finder volume name: $volume_name (expected: $expected_volume_name)." >&2
    exit 1
  fi
  ds_store_text="$(strings -a "$mount_dir/.DS_Store")"
  if ! grep -Fq "backgroundImageAlias" <<<"$ds_store_text" \
    || ! grep -Fq "${VERSION}-${RELEASE_ARCH}:.background.tiff" <<<"$ds_store_text"; then
    hdiutil detach "$mount_dir" -quiet || true
    rmdir "$mount_dir" || true
    echo "Rebuilt DMG Finder metadata does not bind its background to the final volume." >&2
    exit 1
  fi
  hdiutil detach "$mount_dir" -quiet
  rmdir "$mount_dir"
}

package_stapled_app_as_dmg() {
  local release_app="$1"
  local dmg_path="$RELEASE_DIR/Lantai-${VERSION}-${RELEASE_ARCH}.dmg"
  local staging_dir output_dir staged_app built_dmg

  # electron-builder may normalize or re-sign a --prepackaged app. Never let
  # that implementation detail touch the stapled release bundle: doing so
  # invalidates its ticket after the app has already passed Apple notary.
  staging_dir="$(mktemp -d "$RELEASE_DIR/dmg-prepackaged.XXXXXX")"
  output_dir="$(mktemp -d "$RELEASE_DIR/dmg-output.XXXXXX")"
  staged_app="$staging_dir/${PRODUCT_NAME}.app"
  built_dmg="$output_dir/Lantai-${VERSION}-${RELEASE_ARCH}.dmg"

  echo "==> Packaging an isolated copy of the stapled app into a final Finder-configured DMG"
  ditto "$release_app" "$staged_app"
  rm -f "$dmg_path" "$dmg_path.blockmap"
  (
    cd "$CONSOLE_DIR"
    CSC_IDENTITY_AUTO_DISCOVERY=false npm exec --offline -- electron-builder \
      --prepackaged "$staged_app" \
      --mac dmg \
      "--${RELEASE_ARCH}" \
      --config.mac.identity=null \
      "--config.directories.output=$output_dir"
  )
  if [[ ! -f "$built_dmg" ]]; then
    echo "Electron builder did not produce the staged DMG: $built_dmg" >&2
    exit 1
  fi
  ditto "$built_dmg" "$dmg_path"
  rm -rf "$staging_dir" "$output_dir"
}

rebuild_release_artifacts_from_stapled_app() {
  local release_app="$1"
  local zip_path dmg_path

  zip_path="$RELEASE_DIR/Lantai-${VERSION}-${RELEASE_ARCH}.zip"
  dmg_path="$RELEASE_DIR/Lantai-${VERSION}-${RELEASE_ARCH}.dmg"
  echo "==> Rebuilding ZIP from stapled app"
  rm -f "$zip_path" "$zip_path.blockmap"
  ditto -c -k --sequesterRsrc --keepParent "$release_app" "$zip_path"

  package_stapled_app_as_dmg "$release_app"
  sign_dmg_for_distribution "$dmg_path"
  validate_release_dmg_installer_layout "$dmg_path"
}

adhoc_sign_local_release_bundle_and_artifacts() {
  local release_app

  if [[ "$MAC_SIGNING_MODE" != "unsigned" ]]; then
    return
  fi

  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found under $RELEASE_DIR/mac-*" >&2
    exit 1
  fi

  echo "==> Applying ad-hoc signature for same-machine smoke"
  codesign --force --deep --sign - "$release_app"
  codesign --verify --deep --strict "$release_app"
  rebuild_release_artifacts_from_stapled_app "$release_app"
}

notarize_release_bundle_and_artifacts() {
  local release_app notary_tmp_dir app_zip_tmp app_notary_output dmg_path dmg_notary_output app_submission_id dmg_submission_id
  notary_tmp_dir=""
  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found under $RELEASE_DIR/mac-*" >&2
    exit 1
  fi
  dmg_path="$RELEASE_DIR/Lantai-${VERSION}-${RELEASE_ARCH}.dmg"

  if [[ "$MAC_SIGNING_MODE" != "developer-id" ]]; then
    echo "==> Skipping notarization because signing mode is $MAC_SIGNING_MODE"
    return
  fi
  if skip_notarization_requested; then
    echo "==> Skipping notarization because LANTAI_SKIP_NOTARIZATION=1"
    # A local-only build skips Apple's submission and stapling, but its
    # installer must still contain the final Developer ID-signed app, rather
    # than electron-builder's deliberately unsigned assembly-stage copy.
    rebuild_release_artifacts_from_stapled_app "$release_app"
    return
  fi

  mkdir -p "$NOTARY_RECORD_DIR"
  app_notary_output="$NOTARY_RECORD_DIR/notary-app.json"
  dmg_notary_output="$NOTARY_RECORD_DIR/notary-dmg.json"
  app_submission_id="${LANTAI_RESUME_APP_NOTARY_SUBMISSION_ID:-}"
  dmg_submission_id="${LANTAI_RESUME_DMG_NOTARY_SUBMISSION_ID:-}"

  if [[ -n "$dmg_submission_id" ]] \
    && [[ ! "${LANTAI_RESUME_NOTARIZATION:-0}" =~ ^(1|true|yes|on)$ ]]; then
    echo "LANTAI_RESUME_DMG_NOTARY_SUBMISSION_ID requires LANTAI_RESUME_NOTARIZATION=1." >&2
    exit 1
  fi

  if [[ -n "$app_submission_id" ]]; then
    if [[ ! "${LANTAI_RESUME_NOTARIZATION:-0}" =~ ^(1|true|yes|on)$ ]]; then
      echo "LANTAI_RESUME_APP_NOTARY_SUBMISSION_ID requires LANTAI_RESUME_NOTARIZATION=1." >&2
      exit 1
    fi
    verify_existing_notary_submission "$app_submission_id" "app bundle archive" "$app_notary_output"
  else
    notary_tmp_dir="$(mktemp -d "$RELEASE_DIR/notary.XXXXXX")"
    app_zip_tmp="$notary_tmp_dir/Lantai-notary-app.zip"
    echo "==> Preparing app archive for notarization"
    ditto -c -k --sequesterRsrc --keepParent "$release_app" "$app_zip_tmp"
    notarize_file_with_notarytool "$app_zip_tmp" "app bundle archive" "$app_notary_output"
  fi
  staple_and_validate "$release_app" "app bundle"

  if [[ -n "$dmg_submission_id" ]]; then
    if [[ ! -f "$dmg_path" ]]; then
      echo "Existing DMG not found for resumed notarization: $dmg_path" >&2
      exit 1
    fi
    verify_existing_notary_submission "$dmg_submission_id" "DMG" "$dmg_notary_output"
  else
    rebuild_release_artifacts_from_stapled_app "$release_app"
    notarize_file_with_notarytool "$dmg_path" "DMG" "$dmg_notary_output"
  fi
  staple_and_validate "$dmg_path" "DMG"

  if [[ -n "$notary_tmp_dir" ]]; then
    rm -rf "$notary_tmp_dir"
  fi
}

write_artifact_manifest() {
  local release_app zip_path dmg_path extra_args identity_class
  identity_class="$MAC_SIGNING_MODE"
  if [[ "$MAC_SIGNING_MODE" == "developer-id" ]]; then
    identity_class="Developer ID Application"
  fi
  release_app="$(find_release_app)"
  zip_path="$(find_release_file "Lantai-${VERSION}-${RELEASE_ARCH}.zip")"
  dmg_path="$(find_release_file "Lantai-${VERSION}-${RELEASE_ARCH}.dmg")"
  extra_args=()
  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" ]]; then
    extra_args+=(--allow-unreleasable-binaries)
  fi

  echo "==> Writing release artifact manifest"
  "$VENV_PYTHON" "$RELEASE_PROVENANCE_SCRIPT" write-manifest \
    --root "$ROOT_DIR" \
    --release-dir "$RELEASE_DIR" \
    --output "$ARTIFACT_MANIFEST" \
    --source-sha "$SOURCE_SHA" \
    --source-dirty "$SOURCE_DIRTY" \
    --release-tag "$RELEASE_TAG" \
    --version "$VERSION" \
    --arch "$RELEASE_ARCH" \
    --product-name "$PRODUCT_NAME" \
    --signing-mode "$MAC_SIGNING_MODE" \
    --codesign-identity-class "$identity_class" \
    --dependency-manifest "$DEPENDENCY_MANIFEST" \
    --supply-chain-manifest "$SUPPLY_CHAIN_MANIFEST" \
    --packaged-runtime-evidence "$PACKAGED_RUNTIME_COMPLETENESS_EVIDENCE" \
    --notary-json-dir "$NOTARY_RECORD_DIR" \
    --app "$release_app" \
    --zip "$zip_path" \
    --dmg "$dmg_path" \
    "${extra_args[@]}"
}

write_supply_chain_manifest() {
  local release_app extra_args
  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found under $RELEASE_DIR/mac-*" >&2
    exit 1
  fi

  extra_args=()
  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" ]]; then
    extra_args+=(--allow-unreleasable-binaries)
  fi

  echo "==> Writing release SBOM and supply-chain manifest"
  "$VENV_PYTHON" "$SUPPLY_CHAIN_SCRIPT" write-manifest \
    --root "$ROOT_DIR" \
    --release-dir "$RELEASE_DIR" \
    --output "$SUPPLY_CHAIN_MANIFEST" \
    --sbom-output "$SBOM_PATH" \
    --source-sha "$SOURCE_SHA" \
    --version "$VERSION" \
    --arch "$RELEASE_ARCH" \
    --app "$release_app" \
    --dependency-manifest "$DEPENDENCY_MANIFEST" \
    "${extra_args[@]}"
}

validate_skip_build_manifest() {
  if [[ "$SKIP_BUILD" -eq 0 ]]; then
    return
  fi

  echo "==> Validating --skip-build artifact manifest"
  local extra_args
  extra_args=()
  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" ]]; then
    extra_args+=(--allow-unreleasable-binaries)
  fi
  if [[ ! -f "$ARTIFACT_MANIFEST" ]] && [[ "${LANTAI_RESUME_NOTARIZATION:-0}" =~ ^(1|true|yes|on)$ ]]; then
    echo "==> Resuming from signed artifacts after interrupted notarization; manifest will be written after notarization"
    return
  fi
  if [[ ! -f "$ARTIFACT_MANIFEST" ]]; then
    echo "Missing artifact manifest for --skip-build: $ARTIFACT_MANIFEST" >&2
    exit 1
  fi

  "$VENV_PYTHON" "$RELEASE_PROVENANCE_SCRIPT" validate-skip-manifest \
    --manifest "$ARTIFACT_MANIFEST" \
    --release-dir "$RELEASE_DIR" \
    --source-sha "$SOURCE_SHA" \
    --version "$VERSION" \
    --arch "$RELEASE_ARCH" \
    "${extra_args[@]}"
}

deploy_app() {
  local release_app stage isolated_stage=0 copy_result
  release_app="$(find_release_app)"
  if [[ -z "$release_app" ]]; then
    echo "Packaged app not found under $RELEASE_DIR/mac-*" >&2
    exit 1
  fi

  stage="${DEPLOYMENT_STAGE:-}"
  if [[ -z "$stage" ]]; then
    # A direct function call can inspect a copy failure safely, but cannot
    # replace anything without the complete deployment transaction.
    stage="$(mktemp -d "$(dirname "$DOWNLOAD_APP")/.lantai-app-copy.XXXXXX")"
    isolated_stage=1
  fi
  echo "==> Staging $DOWNLOAD_APP"
  if ditto "$release_app" "$stage/app"; then
    :
  else
    copy_result=$?
    [[ "$isolated_stage" -eq 0 ]] || rm -rf "$stage"
    return "$copy_result"
  fi
  if [[ "$isolated_stage" -eq 1 ]]; then
    rm -rf "$stage"
    echo "Error: App replacement requires the complete deployment transaction." >&2
    return 1
  fi
  codesign --verify --deep --strict "$stage/app"
  "$VENV_PYTHON" "$RELEASE_PRODUCT_CONTRACT_SCRIPT" \
    --app "$stage/app" --expected-sha "$SOURCE_SHA" --expected-version "$VERSION" \
    --output "$RELEASE_DIR/release-evidence/staged-downloads-product-contract.json"
  revalidate_deployment_source_authority
  echo "==> Replacing the verified App, DMG and ZIP together"
  DEPLOYMENT_PROMOTED=1
  "$VENV_PYTHON" "$RELEASE_DEPLOYMENT_SCRIPT" promote \
    --root "$DOWNLOADS_DIR" --owner-pid "$$" \
    --app "$release_app" \
    --dmg "$(find_release_file "Lantai-${VERSION}-${RELEASE_ARCH}.dmg")" \
    --zip "$(find_release_file "Lantai-${VERSION}-${RELEASE_ARCH}.zip")"
  DEPLOYMENT_PREVIOUS_RUNNING="$("$VENV_PYTHON" "$RELEASE_DEPLOYMENT_SCRIPT" previous-running --root "$DOWNLOADS_DIR" --owner-pid "$$")"
  xattr -dr com.apple.quarantine "$DOWNLOAD_APP" 2>/dev/null || true
  touch "$DOWNLOAD_APP"
  xattr -d com.apple.quarantine "$DOWNLOAD_DMG" "$DOWNLOAD_ZIP" 2>/dev/null || true

  echo "==> Verifying signature"
  codesign --verify --deep --strict "$DOWNLOAD_APP"

  echo "==> Verifying deployed App identity and product contract"
  "$VENV_PYTHON" "$RELEASE_PRODUCT_CONTRACT_SCRIPT" \
    --app "$DOWNLOAD_APP" \
    --expected-sha "$SOURCE_SHA" \
    --expected-version "$VERSION" \
    --output "$RELEASE_DIR/release-evidence/downloads-product-contract.json"
}

assess_gatekeeper() {
  echo "==> Assessing Gatekeeper"
  if spctl --assess --type execute --verbose=4 "$DOWNLOAD_APP"; then
    return
  fi

  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" && "$MAC_SIGNING_MODE" == "unsigned" ]]; then
    echo "Warning: Gatekeeper rejected $DOWNLOAD_APP" >&2
    echo "         Continuing only because LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1." >&2
    echo "         Do not ship this DMG or ZIP to another Mac." >&2
    return
  fi
  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" ]] && skip_notarization_requested; then
    echo "Warning: Gatekeeper rejected $DOWNLOAD_APP" >&2
    echo "         Continuing because this is a local smoke build with LANTAI_SKIP_NOTARIZATION=1." >&2
    echo "         Do not ship this DMG or ZIP to another Mac." >&2
    return
  fi

  cat >&2 <<EOF
Error: Gatekeeper rejected $DOWNLOAD_APP

This build is not safe to distribute to another Mac. The current machine may
still launch it after quarantine is cleared, but a fresh Mac can block the app
or its bundled backend and leave users with a white screen and restart loops.

To produce a redistributable local package:
  1. Sign with Developer ID Application
  2. Notarize and staple the app or DMG
  3. Rebuild and re-run this script

If you only need a same-machine smoke build, set:
  LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1
EOF
  exit 1
}

resolve_github_cli() {
  local candidate="$GITHUB_CLI"

  if [[ -z "$candidate" && -x "$HOME/.local/bin/gh" ]]; then
    candidate="$HOME/.local/bin/gh"
  fi
  if [[ -z "$candidate" ]]; then
    candidate="$(command -v gh || true)"
  elif [[ "$candidate" != */* ]]; then
    candidate="$(command -v "$candidate" || true)"
  fi

  if [[ -z "$candidate" || ! -x "$candidate" ]]; then
    echo "Error: GitHub CLI was not found for public release publishing." >&2
    echo "Install gh or set LANTAI_GITHUB_CLI=/absolute/path/to/gh." >&2
    exit 1
  fi
  if ! "$candidate" --version >/dev/null 2>&1; then
    echo "Error: GitHub CLI is not runnable: $candidate" >&2
    exit 1
  fi
  if ! "$candidate" auth status --hostname github.com; then
    cat >&2 <<EOF
Error: GitHub CLI is not authenticated for github.com.

Run:
  $candidate auth login --hostname github.com --git-protocol https --web
EOF
    exit 1
  fi

  GITHUB_CLI="$candidate"
}

assert_accepted_notary_record() {
  local label="$1"
  local record_path="$2"

  if [[ ! -f "$record_path" ]]; then
    echo "Error: missing notarization record for $label: $record_path" >&2
    exit 1
  fi
  "$VENV_PYTHON" - "$label" "$record_path" <<'PY'
import json
import sys
from pathlib import Path

label, path = sys.argv[1:]
try:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Could not read notarization record for {label}: {exc}")

if payload.get("status") != "Accepted":
    raise SystemExit(
        f"Notarization record for {label} is not Accepted: {payload.get('status')!r}"
    )
import re
identity = payload.get("id") or payload.get("submissionId")
if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
    raise SystemExit(f"Notarization record for {label} lacks a safe submission identity")
PY
}

assert_public_release_prerequisites() {
  local app_notary_record="$NOTARY_RECORD_DIR/notary-app.json"
  local dmg_notary_record="$NOTARY_RECORD_DIR/notary-dmg.json"

  if [[ "$SOURCE_DIRTY" != "0" ]]; then
    echo "Error: refusing to publish a dirty source tree to the public release channel." >&2
    exit 1
  fi
  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" || "$ALLOW_DIRTY_RELEASE_BUILD" == "1" \
    || "$FORCE_ADHOC_LOCAL_BUILD" =~ ^(1|true|yes|on)$ || "${LANTAI_ALLOW_UNSIGNED_RELEASE_TAG:-0}" == "1" ]] \
    || skip_notarization_requested; then
    echo "Error: public publishing is unavailable for local-only or verification-bypassed builds." >&2
    exit 1
  fi
  if [[ "$MAC_SIGNING_MODE" != "developer-id" ]]; then
    echo "Error: public publishing requires Developer ID Application signing." >&2
    exit 1
  fi
  if [[ "$RELEASE_TAG" != "v$VERSION" ]]; then
    echo "Error: public publishing requires the signed source tag v$VERSION." >&2
    exit 1
  fi
  if [[ ! -f "$ARTIFACT_MANIFEST" || ! -f "$SUPPLY_CHAIN_MANIFEST" || ! -f "$SBOM_PATH" ]]; then
    echo "Error: release provenance, supply-chain, or SBOM is missing." >&2
    exit 1
  fi

  assert_accepted_notary_record "app bundle" "$app_notary_record"
  assert_accepted_notary_record "DMG" "$dmg_notary_record"

  "$VENV_PYTHON" - "$ARTIFACT_MANIFEST" "$VERSION" "$RELEASE_TAG" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
version = sys.argv[2]
tag = sys.argv[3]
try:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Could not read artifact manifest: {exc}")

source = payload.get("source", {})
release = payload.get("release", {})
signing = payload.get("signing", {})
notarization = payload.get("notarization", {})
errors = []
if source.get("dirty") is not False:
    errors.append("artifact manifest records dirty source")
if source.get("tag") != tag:
    errors.append("artifact manifest release tag does not match")
if release.get("version") != version:
    errors.append("artifact manifest version does not match")
if signing.get("mode") != "developer-id":
    errors.append("artifact manifest is not a Developer ID release")
if len(notarization.get("requestIds", [])) < 2:
    errors.append("artifact manifest is missing notarization request evidence")
if errors:
    raise SystemExit("Error: " + "; ".join(errors))
PY
}

audit_public_release_version_lineage() {
  local phase="$1"

  "$VENV_PYTHON" "$ROOT_DIR/scripts/audit_release_version_lineage.py" \
    --source-package "$CONSOLE_DIR/package.json" \
    --installed-app "$DOWNLOAD_APP" \
    --internal-dir "$DOWNLOADS_DIR" \
    --release-repo "$GITHUB_RELEASE_REPOSITORY" \
    --phase "$phase" \
    --strict
}

verify_public_notary_binding() {
  "$VENV_PYTHON" - "$ARTIFACT_MANIFEST" "$NOTARY_RECORD_DIR" <<'PY'
import hashlib
import json
import sys
from pathlib import Path
manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
records = manifest.get("notarization", {}).get("requestIds", [])
for label in ("notary-app", "notary-dmg"):
    original = json.loads((Path(sys.argv[2]) / (label + ".json")).read_text(encoding="utf-8"))
    identity = original.get("id") or original.get("submissionId")
    matches = [record for record in records if isinstance(record, dict) and record.get("label") == label]
    if (len(matches) != 1 or original.get("status") != "Accepted" or
        matches[0].get("status") != "Accepted" or not isinstance(identity, str) or
        matches[0].get("requestIdSHA256") != hashlib.sha256(identity.encode("utf-8")).hexdigest()):
        raise SystemExit("Public notarization identity/status differs from the actual submission: " + label)
PY
}

verify_public_upload_signatures() {
  (
  local upload_dir="$1" expanded="$2" app mount mount_quoted
  mkdir "$expanded"
  ditto -x -k "$upload_dir/Lantai-${VERSION}-${RELEASE_ARCH}.zip" "$expanded"
  app="$expanded/${PRODUCT_NAME}.app"
  [[ -d "$app" ]] || { echo 'Public ZIP does not contain the expected App.' >&2; return 1; }
  codesign --verify --deep --strict "$app"
  validate_direct_distribution_signature "$app" app
  xcrun stapler validate "$app"
  spctl --assess --type execute --verbose=4 "$app"
  "$VENV_PYTHON" "$RELEASE_PRODUCT_CONTRACT_SCRIPT" \
    --app "$app" --expected-sha "$SOURCE_SHA" --expected-version "$VERSION" \
    --output "$expanded/product-contract.json"
  "$VENV_PYTHON" - "$app" "$VERSION" "$RELEASE_ARCH" "$ROOT_DIR" "$upload_dir/lantai-artifact-manifest.json" "$SOURCE_SHA" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from scripts.release_build_identity import validate_public_app
manifest = json.loads(Path(sys.argv[5]).read_text(encoding='utf-8'))
if len(list(Path(sys.argv[1]).parent.glob('*.app'))) != 1:
    raise ValueError('Public ZIP must contain exactly one App')
validate_public_app(Path(sys.argv[1]), manifest, sys.argv[6], sys.argv[2], sys.argv[3])
PY
  codesign --verify --strict "$upload_dir/Lantai-${VERSION}-${RELEASE_ARCH}.dmg"
  validate_direct_distribution_signature "$upload_dir/Lantai-${VERSION}-${RELEASE_ARCH}.dmg" dmg
  xcrun stapler validate "$upload_dir/Lantai-${VERSION}-${RELEASE_ARCH}.dmg"
  spctl --assess --type open --context context:primary-signature --verbose=4 "$upload_dir/Lantai-${VERSION}-${RELEASE_ARCH}.dmg"
  mount="$expanded/dmg-mount"
  mkdir "$mount"
  # The EXIT trap also runs when a payload check fails. A detach failure is
  # an acceptance failure; no mounted filesystem is silently left behind.
  # Capture the path now: bash unwinds function-local variables before running
  # EXIT after an errexit failure inside this subshell.
  printf -v mount_quoted '%q' "$mount"
  trap "status=\$?; hdiutil detach ${mount_quoted} || status=1; exit \"\$status\"" EXIT
  hdiutil attach -readonly -nobrowse -noautoopen -mountpoint "$mount" -plist \
    "$upload_dir/Lantai-${VERSION}-${RELEASE_ARCH}.dmg" > "$expanded/dmg-attach.plist"
  "$VENV_PYTHON" - "$expanded/dmg-attach.plist" "$mount" "$PRODUCT_NAME" <<'PY'
import plistlib, sys
from pathlib import Path
record = plistlib.loads(Path(sys.argv[1]).read_bytes())
mounted = [Path(e['mount-point']) for e in record.get('system-entities', []) if e.get('mount-point')]
root = Path(sys.argv[2])
if mounted != [root] or root.is_symlink():
    raise ValueError('Public DMG did not mount at its private read-only location')
apps = list(root.glob('*.app'))
if apps != [root / (sys.argv[3] + '.app')] or apps[0].is_symlink():
    raise ValueError('Public DMG must contain exactly the expected real App')
PY
  app="$mount/${PRODUCT_NAME}.app"
  codesign --verify --deep --strict "$app"
  validate_direct_distribution_signature "$app" app
  xcrun stapler validate "$app"
  spctl --assess --type execute --verbose=4 "$app"
  "$VENV_PYTHON" "$RELEASE_PRODUCT_CONTRACT_SCRIPT" \
    --app "$app" --expected-sha "$SOURCE_SHA" --expected-version "$VERSION" \
    --output "$expanded/dmg-product-contract.json"
  "$VENV_PYTHON" - "$app" "$SOURCE_SHA" "$VERSION" "$RELEASE_ARCH" "$ROOT_DIR" "$upload_dir/lantai-artifact-manifest.json" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[5])
from scripts.release_build_identity import validate_public_app
validate_public_app(Path(sys.argv[1]), json.loads(Path(sys.argv[6]).read_text(encoding='utf-8')),
                    sys.argv[2], sys.argv[3], sys.argv[4])
PY
  )
}

prepare_formal_release() {
  local prepared_root prepared_dir
  if [[ "$PREPARE_RELEASE" -ne 1 ]]; then
    return
  fi

  echo "==> Preparing verified formal release files without deployment or upload"
  revalidate_deployment_source_authority
  assert_public_release_prerequisites
  verify_public_notary_binding
  mkdir -p "$RELEASE_DIR/release-evidence"
  prepared_root="$(mktemp -d "$RELEASE_DIR/release-evidence/prepared-release.XXXXXX")"
  prepared_dir="$prepared_root/verified"
  "$VENV_PYTHON" "$RELEASE_UPLOAD_SCRIPT" stage \
    --root "$RELEASE_DIR" --output "$prepared_dir" --source-sha "$SOURCE_SHA" \
    --version "$VERSION" --arch "$RELEASE_ARCH" --public --flat-root
  verify_public_upload_signatures "$prepared_dir" "$prepared_root/expanded"
  "$VENV_PYTHON" "$RELEASE_UPLOAD_SCRIPT" validate \
    --root "$prepared_dir" --source-sha "$SOURCE_SHA" --version "$VERSION" --arch "$RELEASE_ARCH" --public
  revalidate_deployment_source_authority
  PREPARED_RELEASE_DIR="$prepared_dir"
}

publish_github_release() {
  local upload_root upload_dir release_url visibility
  local -a assets

  if [[ "$PUBLISH_GITHUB_RELEASE" -ne 1 ]]; then
    return
  fi

  echo "==> Publishing verified release to GitHub: $GITHUB_RELEASE_REPOSITORY"
  revalidate_deployment_source_authority
  assert_public_release_prerequisites
  verify_public_notary_binding
  resolve_github_cli

  visibility="$("$GITHUB_CLI" repo view "$GITHUB_RELEASE_REPOSITORY" --json visibility --jq .visibility)"
  if [[ "$visibility" != "PUBLIC" ]]; then
    echo "Error: release repository must be public, got: $visibility" >&2
    exit 1
  fi
  if "$GITHUB_CLI" release view "$RELEASE_TAG" --repo "$GITHUB_RELEASE_REPOSITORY" >/dev/null 2>&1; then
    echo "Error: public GitHub Release $RELEASE_TAG already exists; releases are immutable." >&2
    exit 1
  fi

  audit_public_release_version_lineage prepublish

  if [[ ! -f "$ARTIFACT_MANIFEST" || ! -f "$SUPPLY_CHAIN_MANIFEST" ]]; then
    echo "Error: release artifact and supply-chain manifests are required for publication." >&2
    return 1
  fi
  mkdir -p "$RELEASE_DIR/release-evidence"
  upload_root="$(mktemp -d "$RELEASE_DIR/release-evidence/public-upload.XXXXXX")"
  upload_dir="$upload_root/verified"
  "$VENV_PYTHON" "$RELEASE_UPLOAD_SCRIPT" stage \
    --root "$RELEASE_DIR" --output "$upload_dir" --source-sha "$SOURCE_SHA" \
    --version "$VERSION" --arch "$RELEASE_ARCH" --public --flat-root
  verify_public_upload_signatures "$upload_dir" "$upload_root/expanded"
  "$VENV_PYTHON" "$RELEASE_UPLOAD_SCRIPT" validate \
    --root "$upload_dir" --source-sha "$SOURCE_SHA" --version "$VERSION" --arch "$RELEASE_ARCH" --public
  assets=(
    "$upload_dir/Lantai-${VERSION}-${RELEASE_ARCH}.zip"
    "$upload_dir/Lantai-${VERSION}-${RELEASE_ARCH}.dmg"
    "$upload_dir/lantai-artifact-manifest.json"
    "$upload_dir/lantai-supply-chain-manifest.json"
    "$upload_dir/lantai-sbom.cdx.json"
  )
  revalidate_deployment_source_authority

  "$GITHUB_CLI" release create "$RELEASE_TAG" \
    --repo "$GITHUB_RELEASE_REPOSITORY" \
    --title "$PRODUCT_NAME $VERSION" \
    --generate-notes \
    "${assets[@]}"

  audit_public_release_version_lineage postpublish
  release_url="https://github.com/$GITHUB_RELEASE_REPOSITORY/releases/tag/$RELEASE_TAG"
  echo "Public release: $release_url"
}

run_packaged_app_lifecycle_smoke() {
  if [[ "${LANTAI_RUN_PACKAGED_APP_LIFECYCLE_SMOKE:-0}" != "1" ]]; then
    return
  fi
  local release_app
  release_app="$(find_release_app)"
  if [[ -z "$release_app" || ! -d "$release_app" ]]; then
    echo "Release app not found for lifecycle smoke under $RELEASE_DIR" >&2
    exit 1
  fi

  echo "==> Running packaged app lifecycle smoke before Downloads deployment"
  local smoke_args=(
    "$VENV_PYTHON"
    "$ROOT_DIR/scripts/packaged_app_lifecycle_smoke.py"
    --app "$release_app"
    --distribution official
    --evidence-output "$LIFECYCLE_SMOKE_EVIDENCE"
  )
  smoke_args+=(--headless)
  smoke_args+=(--restart-cycle)
  smoke_args+=(--app-relaunch-cycle)
  "${smoke_args[@]}"
}

run_packaged_app_rapid_lifecycle_smoke() {
  if [[ "${LANTAI_RUN_PACKAGED_APP_LIFECYCLE_SMOKE:-0}" != "1" ]]; then
    return
  fi
  local release_app rapid_lifecycle_cycles
  release_app="$(find_release_app)"
  if [[ -z "$release_app" || ! -d "$release_app" ]]; then
    echo "Release app not found for rapid lifecycle smoke under $RELEASE_DIR" >&2
    exit 1
  fi

  rapid_lifecycle_cycles="${LANTAI_PACKAGED_APP_RAPID_LIFECYCLE_CYCLES:-100}"
  echo "==> Running ${rapid_lifecycle_cycles}-cycle packaged app process-ownership smoke"
  "$VENV_PYTHON" "$ROOT_DIR/scripts/packaged_app_rapid_lifecycle_smoke.py" \
    --app "$release_app" \
    --distribution official \
    --cycles "$rapid_lifecycle_cycles" \
    --evidence-output "$RAPID_LIFECYCLE_SMOKE_EVIDENCE"
}

run_packaged_customization_upgrade_smoke() {
  if [[ "${LANTAI_RUN_PACKAGED_APP_LIFECYCLE_SMOKE:-0}" != "1" ]]; then
    return
  fi
  local release_app
  release_app="$(find_release_app)"
  if [[ -z "$release_app" || ! -d "$release_app" ]]; then
    echo "Release app not found for customization upgrade smoke under $RELEASE_DIR" >&2
    exit 1
  fi

  echo "==> Verifying packaged upgrade of protected 1.x personalization state"
  "$VENV_PYTHON" "$ROOT_DIR/scripts/packaged_customization_upgrade_smoke.py" \
    --app "$release_app" \
    --fixture "$ROOT_DIR/tests/fixtures/customization_v1" \
    --evidence-output "$CUSTOMIZATION_UPGRADE_SMOKE_EVIDENCE"
}

run_packaged_image_parser_mitigation() {
  local release_app
  release_app="$(find_release_app)"
  if [[ -z "$release_app" || ! -d "$release_app" ]]; then
    echo "Release app not found for packaged image parser mitigation under $RELEASE_DIR" >&2
    exit 1
  fi

  echo "==> Proving packaged presentation runtime rejects vulnerable image parsers"
  "$VENV_PYTHON" "$PACKAGED_IMAGE_PARSER_MITIGATION_SCRIPT" \
    --app "$release_app" \
    --output "$PACKAGED_IMAGE_PARSER_MITIGATION_EVIDENCE"
}

run_packaged_upgrade_rollback_smoke() {
  local release_app report_path
  release_app="$(find_release_app)"
  report_path="$RELEASE_DIR/release-evidence/official-packaged-upgrade-rollback-gate.json"
  if [[ -z "$release_app" || ! -d "$release_app" ]]; then
    echo "Release app not found for upgrade/rollback smoke under $RELEASE_DIR" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$UPGRADE_ROLLBACK_SMOKE_EVIDENCE")"

  if [[ -z "$PREVIOUS_RELEASE_APP" || ! -d "$PREVIOUS_RELEASE_APP" ]]; then
    if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" && "$PUBLISH_GITHUB_RELEASE" != "1" ]]; then
      echo "==> Deferring previous-version upgrade/rollback smoke for local/internal build"
      "$VENV_PYTHON" - "$report_path" "$release_app" <<'PY'
import json
import sys
from pathlib import Path

report, current_app = sys.argv[1:]
Path(report).write_text(
    json.dumps(
        {
            "schema": "com.lantai.packaged-app-upgrade-rollback-smoke.v1",
            "passed": False,
            "skipped": True,
            "local_only": True,
            "reason": "LANTAI_PREVIOUS_RELEASE_APP was not provided",
            "current_app": current_app,
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)
PY
      return
    fi
    echo "Error: LANTAI_PREVIOUS_RELEASE_APP must name the previous public .app." >&2
    exit 1
  fi

  echo "==> Verifying previous -> current -> previous -> current data safety"
  "$VENV_PYTHON" "$UPGRADE_ROLLBACK_SMOKE_SCRIPT" \
    --previous-app "$PREVIOUS_RELEASE_APP" \
    --current-app "$release_app" \
    --customization-fixture "$ROOT_DIR/tests/fixtures/customization_v1" \
    --evidence-output "$UPGRADE_ROLLBACK_SMOKE_EVIDENCE"
  "$VENV_PYTHON" "$UPGRADE_ROLLBACK_GATE_SCRIPT" \
    --evidence "$UPGRADE_ROLLBACK_SMOKE_EVIDENCE" \
    --previous-app "$PREVIOUS_RELEASE_APP" \
    --current-app "$release_app" \
    --format json > "$report_path"
  echo "Upgrade/rollback gate report: $report_path"
}

run_persona_and_office_release_gates() {
  local release_app persona_report office_report
  release_app="$(find_release_app)"
  persona_report="$RELEASE_DIR/release-evidence/persona-release-gate.json"
  office_report="$RELEASE_DIR/release-evidence/office-release-gate.json"
  if [[ -z "$release_app" || ! -d "$release_app" ]]; then
    echo "Release app not found for Persona/Office gates under $RELEASE_DIR" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$persona_report")"

  if [[ -z "$PERSONA_EVIDENCE_A" || -z "$PERSONA_EVIDENCE_B" \
      || -z "$PERSONA_REVIEW_A" || -z "$PERSONA_REVIEW_B" \
      || -z "$OFFICE_FIRST_TIER_EVIDENCE" || -z "$OFFICE_PACKAGED_EVIDENCE" \
      || -z "$OFFICE_CORPUS_EVIDENCE" || -z "$OFFICE_EDIT_ROUNDTRIP_EVIDENCE" ]]; then
    if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" && "$PUBLISH_GITHUB_RELEASE" != "1" ]]; then
      echo "==> Deferring exact-app Persona/Office release gates for local/internal build"
      "$VENV_PYTHON" - "$persona_report" "$office_report" "$release_app" <<'PY'
import json
import sys
from pathlib import Path

persona, office, app = sys.argv[1:]
for path, gate in ((persona, "persona"), (office, "office")):
    Path(path).write_text(
        json.dumps(
            {
                "passed": False,
                "skipped": True,
                "local_only": True,
                "gate": gate,
                "reason": "exact release-app evidence paths were not provided",
                "app": app,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
PY
      return
    fi
    echo "Error: public release requires Persona and Office evidence paths." >&2
    exit 1
  fi

  echo "==> Enforcing real-provider Persona release gate"
  "$VENV_PYTHON" "$PERSONA_RELEASE_GATE_SCRIPT" \
    --app "$release_app" \
    --evidence "$PERSONA_EVIDENCE_A" \
    --evidence "$PERSONA_EVIDENCE_B" \
    --review "$PERSONA_REVIEW_A" \
    --review "$PERSONA_REVIEW_B" > "$persona_report"
  echo "==> Enforcing real-file packaged Office release gate"
  "$VENV_PYTHON" "$OFFICE_RELEASE_GATE_SCRIPT" \
    --app "$release_app" \
    --first-tier "$OFFICE_FIRST_TIER_EVIDENCE" \
    --packaged "$OFFICE_PACKAGED_EVIDENCE" \
    --corpus "$OFFICE_CORPUS_EVIDENCE" \
    --roundtrip "$OFFICE_EDIT_ROUNDTRIP_EVIDENCE" > "$office_report"
}

write_runtime_migration_dry_run_report() {
  echo "==> Writing Runtime migration dry-run report"
  "$VENV_PYTHON" "$RUNTIME_MIGRATION_REPORT_SCRIPT" \
    --workspace "$ROOT_DIR" \
    --release-edition official \
    --legacy-writer-mode read_only \
    --output "$RUNTIME_MIGRATION_REPORT"
}

write_runtime_migration_backup_restore_evidence() {
  echo "==> Writing Runtime migration backup/restore smoke evidence"
  "$VENV_PYTHON" "$RUNTIME_MIGRATION_BACKUP_RESTORE_SCRIPT" \
    --release-edition official \
    --output "$RUNTIME_MIGRATION_BACKUP_RESTORE_EVIDENCE"
}

run_release_evidence_readiness_gate() {
  local report_path release_app release_dmg release_zip
  report_path="$RELEASE_DIR/release-evidence/official-release-readiness-stage.json"
  mkdir -p "$(dirname "$report_path")"

  release_app="$(find_release_app)"
  release_dmg="$(find_release_file "Lantai-${VERSION}-${RELEASE_ARCH}.dmg")"
  release_zip="$(find_release_file "Lantai-${VERSION}-${RELEASE_ARCH}.zip")"
  if [[ -z "$release_app" || -z "$release_dmg" || -z "$release_zip" ]]; then
    echo "Release artifacts are incomplete before the readiness stage gate" >&2
    exit 1
  fi

  echo "==> Running official release evidence readiness stage gate before Downloads deployment"
  local readiness_args=(
    "$VENV_PYTHON"
    "$RELEASE_READINESS_SCRIPT"
    --root "$ROOT_DIR"
    --downloads-dir "$DOWNLOADS_DIR"
    --version "$VERSION"
    --arch "$RELEASE_ARCH"
    --product-name "$PRODUCT_NAME"
    --source-sha "$SOURCE_SHA"
    --official-app "$release_app"
    --official-dmg "$release_dmg"
    --official-zip "$release_zip"
    --runtime-migration-report "$RUNTIME_MIGRATION_REPORT"
    --runtime-migration-backup-restore-evidence "$RUNTIME_MIGRATION_BACKUP_RESTORE_EVIDENCE"
    --packaged-runtime-evidence "$PACKAGED_RUNTIME_COMPLETENESS_EVIDENCE"
    --format json
    --require-check official.download_app.bundle_id
    --require-check official.download_bundles.present
    --require-check close01.runtime_migration_backup_restore.evidence
    --require-check official.packaged_runtime_completeness.evidence
  )
  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" && "$MAC_SIGNING_MODE" == "unsigned" ]]; then
    echo "Warning: local-only readiness gate records but does not require a Developer ID signature." >&2
  else
    readiness_args+=(--require-check official.download_app.signature)
  fi
  if [[ "$MAC_SIGNING_MODE" == "developer-id" ]] && ! skip_notarization_requested; then
    readiness_args+=(
      --require-check official.notary_credentials.available
      --require-check official.download_app.gatekeeper_notarized
    )
  fi
  if [[ "${LANTAI_RUN_PACKAGED_APP_LIFECYCLE_SMOKE:-0}" == "1" ]]; then
    readiness_args+=(
      --official-lifecycle-evidence "$LIFECYCLE_SMOKE_EVIDENCE"
      --official-rapid-lifecycle-evidence "$RAPID_LIFECYCLE_SMOKE_EVIDENCE"
      --official-rapid-lifecycle-minimum-cycles "${LANTAI_PACKAGED_APP_RAPID_LIFECYCLE_CYCLES:-100}"
      --official-customization-upgrade-evidence "$CUSTOMIZATION_UPGRADE_SMOKE_EVIDENCE"
      --require-check official.packaged_lifecycle_smoke.evidence
      --require-check official.packaged_rapid_lifecycle_smoke.evidence
      --require-check official.packaged_customization_upgrade_smoke.evidence
      --require-check official.legacy_writer_observation.evidence
    )
  fi
  "${readiness_args[@]}" > "$report_path"
  echo "Release readiness stage report: $report_path"
}

run_agent_reliability_release_gate() {
  local release_app report_path
  release_app="$(find_release_app)"
  report_path="$RELEASE_DIR/release-evidence/agent-reliability-release-gate.json"
  if [[ -z "$release_app" || ! -d "$release_app" ]]; then
    echo "Release app not found for Agent reliability gate" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$report_path")"

  # The four-hour reliability soak is a redistributable-release gate. Keep it
  # fail-closed for public publishing, but let an explicitly acknowledged
  # local/internal package reach Downloads for hands-on verification. Record
  # the omission as a skip rather than manufacturing passing evidence.
  local defer_reason=""
  if [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" ]] && skip_notarization_requested; then
    defer_reason="unnotarized local preview; official reliability soak still required"
  elif [[ "$ALLOW_UNVERIFIED_LOCAL_BUILD" == "1" \
      && "$DEFER_AGENT_RELIABILITY_SOAK" == "1" \
      && "$PUBLISH_GITHUB_RELEASE" != "1" ]]; then
    defer_reason="notarized internal test build; public release reliability soak still required"
  fi
  if [[ -n "$defer_reason" ]]; then
    echo "==> Recording deferred P0 Agent reliability soak for local/internal build"
    "$VENV_PYTHON" - "$report_path" "$release_app" "$AGENT_RELIABILITY_EVIDENCE" "$defer_reason" <<'PY'
import json
import sys
from pathlib import Path

report_path, app_path, evidence_path, reason = sys.argv[1:]
payload = {
    "schema": "com.lantai.agent-reliability-release-evidence.v2",
    "passed": False,
    "skipped": True,
    "local_only": True,
    "reason": reason,
    "evidence": evidence_path,
    "official_app": app_path,
}
Path(report_path).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
    echo "Agent reliability gate deferred for local/internal build: $report_path"
    return
  fi

  echo "==> Enforcing P0 Agent reliability soak before deployment"
  "$VENV_PYTHON" "$AGENT_RELIABILITY_GATE_SCRIPT" \
    --evidence "$AGENT_RELIABILITY_EVIDENCE" \
    --official-app "$release_app" \
    --format json > "$report_path"
  echo "Agent reliability gate report: $report_path"
}

revalidate_deployment_source_authority() {
  if [[ "$(git -C "$ROOT_DIR" rev-parse HEAD)" != "$SOURCE_SHA" ]]; then
    echo "Error: source HEAD changed after packaging." >&2
    return 1
  fi
  validate_release_source_authority
  "$VENV_PYTHON" "$ROOT_DIR/scripts/worktree_guard.py" \
    --root "$ROOT_DIR" check --require-clean --require-no-active-lease
}

validate_deployment_payload_identity() {
  local app="${1:-}"
  [[ -n "$app" ]] || app="$(find_release_app)"
  "$VENV_PYTHON" - "$app" "$ARTIFACT_MANIFEST" "$SOURCE_SHA" "$VERSION" "$RELEASE_ARCH" "$ROOT_DIR" <<'PY'
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[6])
from scripts.release_build_identity import validate_public_app

manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
validate_public_app(Path(sys.argv[1]), manifest, sys.argv[3], sys.argv[4], sys.argv[5])
PY
}

begin_deployment() {
  # The initial build lock cleanup remains active until deployment starts.
  # Install rollback cleanup before allocating or changing deployment state.
  trap release_deployment_cleanup EXIT
  revalidate_deployment_source_authority
  # Even a signed candidate with the same SHA must stay in its isolated output.
  # Check the embedded origin before allocating state or stopping the old App.
  validate_deployment_payload_identity
  DEPLOYMENT_STAGE="$("$VENV_PYTHON" "$RELEASE_DEPLOYMENT_SCRIPT" begin \
    --root "$DOWNLOADS_DIR" --owner-pid "$$" \
    --source-sha "$SOURCE_SHA" --version "$VERSION" \
    --app "$(basename "$DOWNLOAD_APP")" \
    --dmg "$(basename "$DOWNLOAD_DMG")" --zip "$(basename "$DOWNLOAD_ZIP")")"
  DEPLOYMENT_ACTIVE=1
}

finish_deployment() {
  revalidate_deployment_source_authority
  validate_deployment_payload_identity "$DOWNLOAD_APP"
  "$VENV_PYTHON" "$RELEASE_DEPLOYMENT_SCRIPT" finish \
    --root "$DOWNLOADS_DIR" --owner-pid "$$" \
    --receipt "$RELEASE_DIR/release-evidence/downloads-deployment.json"
  DEPLOYMENT_ACTIVE=0
}

clean_deployed_launchservices() {
  local lsregister="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
  if [[ -x "$lsregister" ]]; then
    # Cleanup starts after acceptance, when no previous release is a backup.
    "$VENV_PYTHON" "$LAUNCHSERVICES_CLEANUP_SCRIPT" \
      --current-app "$DOWNLOAD_APP" \
      --report "$RELEASE_DIR/lantai-launchservices-cleanup.json" \
      --remove-stale-app-copies --max-passes 12 --settle-seconds 0.25 --strict
    /usr/bin/killall sharedfilelistd >/dev/null 2>&1 || true
    /usr/bin/killall Finder >/dev/null 2>&1 || true
    sleep 1
    "$VENV_PYTHON" "$LAUNCHSERVICES_CLEANUP_SCRIPT" \
      --current-app "$DOWNLOAD_APP" \
      --report "$RELEASE_DIR/lantai-launchservices-cleanup-after-refresh.json" \
      --max-passes 12 --settle-seconds 0.25 --strict
  fi
}

deploy_release_artifacts() {
  local zip_path dmg_path stage isolated_stage=0 copy_result
  zip_path="$(find_release_file "Lantai-${VERSION}-${RELEASE_ARCH}.zip")"
  dmg_path="$(find_release_file "Lantai-${VERSION}-${RELEASE_ARCH}.dmg")"

  if [[ -z "$zip_path" || -z "$dmg_path" ]]; then
    echo "Release artifacts not found under $RELEASE_DIR" >&2
    exit 1
  fi

  stage="${DEPLOYMENT_STAGE:-}"
  if [[ -z "$stage" ]]; then
    stage="$(mktemp -d "$(dirname "$DOWNLOAD_DMG")/.lantai-artifact-copy.XXXXXX")"
    isolated_stage=1
  fi
  echo "==> Staging the complete DMG and ZIP pair"
  if ditto "$dmg_path" "$stage/dmg" && ditto "$zip_path" "$stage/zip"; then
    :
  else
    copy_result=$?
    [[ "$isolated_stage" -eq 0 ]] || rm -rf "$stage"
    return "$copy_result"
  fi
  if [[ "$isolated_stage" -eq 1 ]]; then
    rm -rf "$stage"
    echo "Error: artifact replacement requires the complete deployment transaction." >&2
    return 1
  fi
}

launch_app() {
  if [[ "$NO_LAUNCH" -eq 1 ]]; then
    echo "==> Skipping launch"
    return
  fi
  local executable="$DOWNLOAD_APP/Contents/MacOS/$PRODUCT_NAME"
  if [[ ! -x "$executable" ]]; then
    echo "App executable not found: $executable" >&2
    exit 1
  fi

  echo "==> Launching $DOWNLOAD_APP"
  "$VENV_PYTHON" "$RELEASE_DEPLOYMENT_SCRIPT" launch-intent --root "$DOWNLOADS_DIR" --owner-pid "$$"
  /usr/bin/open -n "$DOWNLOAD_APP"
  "$VENV_PYTHON" "$RELEASE_DEPLOYMENT_SCRIPT" register-launch --root "$DOWNLOADS_DIR" --owner-pid "$$"
  echo "==> Verified and privately registered the new App instance"
}

main() {
  echo "Project root: $ROOT_DIR"
  echo "Product: $PRODUCT_NAME"
  echo "Version: $VERSION"

  if [[ "$SKIP_BUILD" -eq 0 ]]; then
    build_console
    write_embedded_release_build_identity
    validate_console_product_contract
    sync_console_static
    build_backend
    # PyInstaller owns console/electron-dist and may replace it while building
    # the frozen backend. Rebuild native helpers afterwards so Electron's
    # extraResources inputs cannot silently disappear between stages.
    build_mac_use_helper
    prepare_console_assets
    build_electron
    sign_packaged_macho_leaves_for_distribution
    validate_packaged_mac_use_helper
    run_packaged_mac_use_helper_smoke
    adhoc_sign_local_release_bundle_and_artifacts
    # Capture runtime-completeness hashes only after the final local signing
    # step so readiness never validates a pre-signing executable.
    run_packaged_skill_runtime_smoke
  else
    echo "==> Skipping build steps"
  fi

  validate_packaged_product_contract
  validate_packaged_version_coherence
  validate_skip_build_manifest
  validate_release_bundle_signature
  # All executable package smokes run before notarization. Some desktop
  # runtimes create first-launch state while probing their own bundle; keeping
  # those probes before stapling guarantees the post-notary bundle is never
  # mutated by a test run.
  run_packaged_app_lifecycle_smoke
  run_packaged_app_rapid_lifecycle_smoke
  run_packaged_customization_upgrade_smoke
  run_packaged_image_parser_mitigation
  run_packaged_upgrade_rollback_smoke
  notarize_release_bundle_and_artifacts
  write_supply_chain_manifest
  write_artifact_manifest
  run_persona_and_office_release_gates
  write_runtime_migration_dry_run_report
  write_runtime_migration_backup_restore_evidence
  run_release_evidence_readiness_gate
  run_agent_reliability_release_gate
  if [[ "$ARTIFACT_ONLY" -eq 1 ]]; then
    echo "==> Preserving artifact-only candidate; Downloads remains unchanged"
  elif [[ "$PREPARE_RELEASE" -eq 1 ]]; then
    prepare_formal_release
  else
    begin_deployment
    deploy_release_artifacts
    deploy_app
    assess_gatekeeper
    launch_app
    finish_deployment
    clean_deployed_launchservices
    publish_github_release
  fi

  echo
  echo "Done."
  echo "Release dir: $RELEASE_DIR"
  if [[ "$ARTIFACT_ONLY" -eq 1 ]]; then
    echo "Downloads deployment: skipped (--artifact-only)"
  elif [[ "$PREPARE_RELEASE" -eq 1 ]]; then
    echo "Verified release files: $PREPARED_RELEASE_DIR"
    echo "Downloads deployment: skipped (--prepare-release)"
    echo "GitHub publication: skipped (--prepare-release)"
  else
    echo "Deployed app: $DOWNLOAD_APP"
    echo "Deployed dmg: $DOWNLOAD_DMG"
    echo "Deployed zip: $DOWNLOAD_ZIP"
  fi
}

main
