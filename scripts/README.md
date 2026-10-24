# Scripts

Release builds run `packaged_skill_runtime_smoke.py` against the generated
`.app` before signing and notarization. The gate proves that bundled Skills,
their private script resources, the frozen Python bridge, and the packaged
presentation runtime are all present and executable; source-only success is
not sufficient for release.

The smoke is exhaustive rather than sample-only: it compares every Skill
resource hash, imports every absolute dependency referenced by every Python
Skill inside the frozen backend, discovers lazy CLI/channel/dynamic imports
from source registries, runs every lazy CLI command's `--help`, resolves every
Node Skill dependency with the packaged Electron runtime, checks every Shell
Skill's syntax, and then exercises both stable PPT generation and the locked
compatibility renderer.

Run from **repo root**.

> Since 2026-07-27, only the official distribution is maintained. Current
> development, CI, build, signing, and release workflows must not depend on
> the archived MAS/TestFlight evidence path.

## Prepare a formal desktop release without deployment

The unique Release Coordinator can reserve the build resources and run this in
the canonical, clean `main` worktree with no mutation lease:

```bash
./build_and_deploy.sh --prepare-release
```

This runs the existing source, package, signing, notarization, persona, Office,
readiness and reliability gates. It then stages exactly the ZIP, DMG, artifact
manifest, supply-chain manifest and SBOM in a private directory under
`release/builds/.../release-evidence/prepared-release.*/verified`. Both packaged
Apps receive the existing signature, stapling, Gatekeeper, product-contract and
build-identity checks; the final files pass the existing public metadata and
byte-hash validator. Canonical-main, HEAD, clean-worktree and lease checks run
again before staging and after validation.

Downloads and the running installation remain untouched. This mode does not
query or upload a GitHub Release and does not grant publication approval.
Publishing later still requires the full publishing flow and its fresh checks.
Existing `--skip-build` reuse is permitted only with all of its existing exact
source/version/arch and final evidence checks; it does not waive a failed gate.

`--prepare-release` rejects `--artifact-only`, `--publish-github-release` and
`LANTAI_RELEASE_CANDIDATE_ID`. Local-unverified, dirty-source, ad-hoc-signing,
unsigned-tag, skipped-notarization, same-version-rebuild and deferred-soak
switches must be unset or `0`. Feature branches, detached worktrees and CI
candidates continue to use `--artifact-only`; their candidate identity cannot
be promoted by this option. The default deployment and public publishing paths
retain their existing behavior.

## Build wheel (with latest console)

```bash
bash scripts/wheel_build.sh
```

- Builds the console frontend (`console/`), copies `console/dist` to `src/lantai/console`, verifies the embedded assets are byte-for-byte fresh, then builds the wheel. Output: `dist/*.whl`.

## Release Dependency Completeness Gate

Every desktop release must be reproducible from the checked-in dependency
declarations. `build_and_deploy.sh` runs the following hard gate before any
package is built:

```bash
.venv/bin/python scripts/check_release_dependency_locks.py \
  --write-manifest build/release-dependency-manifest.json
```

The gate fails the build unless all of these are true:

1. `uv.lock` and `console/package-lock.json` exist and match their project files.
2. `uv lock --check` succeeds without changing the lock.
3. `uv sync --locked --extra dev --check` proves the active build environment
   is synchronized with the lock, so stale packages in an old virtualenv cannot
   hide a missing declaration.
4. Critical runtime packages are declared as direct dependencies rather than
   being inherited accidentally through another package.
5. A fresh Python process can import the real backend entry path plus bundled
   Guanlan and serialization dependencies.

When application code introduces a new runtime import, add its distribution to
`pyproject.toml`, regenerate `uv.lock`, synchronize the release environment, and
rerun this gate. Do not bypass the gate to make a release build pass.

## GenOffice Upstream Freshness Gate

The desktop build checks the reviewed commit recorded in
`console/vendor/lantai-docx-editor/SOURCE.md` with `genspark-ai/genoffice`'s
current `main` branch at most once every 72 hours (configurable with
`LANTAI_GENOFFICE_UPSTREAM_CHECK_INTERVAL_HOURS`):

```bash
.venv/bin/python scripts/check_genoffice_upstream.py \
  --write-report build/genoffice-upstream-check.json
```

An upstream change is recorded and surfaced for review, but does not block a
build or download code. Engine updates remain an explicit reviewed source merge;
this gate never downloads executable code into an installed user app. A manual
strict check can omit `--allow-stale` when deciding whether to take an update.

## Console Static Freshness

```bash
# Fail if src/lantai/console is stale compared with console/dist
python scripts/check_console_static_fresh.py

# Replace src/lantai/console with console/dist, then verify
python scripts/check_console_static_fresh.py --sync
```

Use this after frontend-only builds when the Python source package or wheel will
serve embedded console assets. Desktop/Electron packaging serves `console/dist`
directly, but wheel/source installs serve `src/lantai/console`.

## Build Docker image

```bash
bash scripts/docker_build.sh [IMAGE_TAG] [EXTRA_ARGS...]
```

- Default tag: `lantai:latest`. Uses `deploy/Dockerfile` (multi-stage: builds console then Python app).
- Example: `bash scripts/docker_build.sh ghcr.io/shenyangs/lantai:latest --no-cache`.

## Run Test

```bash
# Run all tests
python scripts/run_tests.py

# Run all unit tests
python scripts/run_tests.py -u

# Run unit tests for a specific module
python scripts/run_tests.py -u providers

# Run integration tests
python scripts/run_tests.py -i

# Run all tests and generate a coverage report
python scripts/run_tests.py -a -c

# Run tests in parallel (requires pytest-xdist)
python scripts/run_tests.py -p

# Show help
python scripts/run_tests.py -h
```

## Runtime Evaluator Preflight

```bash
# Run golden fixtures for preview/runtime failure diagnosis drift
python scripts/runtime_evaluator_preflight.py

# Machine-readable output for CI or release logs
python scripts/runtime_evaluator_preflight.py --json
```

## Codex Harness Quality Control Plane

```bash
# Validate the checked-in, read-only QA mission contract (CI-safe, no keys).
python scripts/codex_harness_quality.py validate --json

# Inspect local Codex/self-hosted execution readiness.
python scripts/codex_harness_quality.py preflight --profile source-runtime --json

# Run only after configuring separate application and restricted executor keys.
python scripts/codex_harness_quality.py run \
  --profile source-runtime \
  --acknowledge-beta-data-policy \
  --json
```

The runner connects the official Agents API to a local `codex exec-server`,
executes Lantai's existing gates as an evidence-producing long task, refuses a
dirty worktree, strips `OPENAI_API_KEY` from the executor environment, and
forces a read-only/no-approval sandbox with allowlisted test entrypoints. It
deletes the remote session by default. The mission is diagnosis-only: it cannot
replace behavior locks, fault/soak evidence, or the normal worktree lease and
review flow. See `docs/codex-harness-quality-control-plane-zh.md`.

## Frozen Backend Console Smoke

```bash
# Start the packaged desktop backend and exercise /api/console/chat end to end
python scripts/frozen_backend_console_smoke.py --distribution official

# Add Electron-style desktop auth/readiness proof and one backend restart cycle
python scripts/frozen_backend_console_smoke.py --distribution official --desktop-auth --restart-cycle
```

This smoke uses the frozen
`console/electron-dist/lantai-server/Lantai Backend.app/Contents/MacOS/lantai-server`
binary, then checks `/health`, optional Electron desktop readiness proof,
console chat SSE, chat listing, and persisted runtime events. With
`--restart-cycle`, it restarts the packaged backend with the same data
directory and verifies the failed turn history is still readable. It is meant
to catch P0 regressions where a desktop build can open but a basic message turn
is silent, recorded as completed after failure, or lost across backend restart.

## Packaged App Lifecycle Smoke

```bash
# Launch a freshly built .app and verify Electron-managed backend readiness
python scripts/packaged_app_lifecycle_smoke.py --app release/builds/.../mac-arm64/澜台.app

# Also kill the managed backend once and verify Electron restarts it
python scripts/packaged_app_lifecycle_smoke.py --app release/builds/.../mac-arm64/澜台.app --restart-cycle
```

This smoke launches the real packaged Electron app with an isolated temporary
user data directory. When the app sees `LANTAI_DESKTOP_LIFECYCLE_SMOKE_FILE`,
it writes a temporary readiness probe containing the backend URL, desktop
token, nonce, and pid. The script uses that probe to verify
`/api/desktop/readiness`, create a visible no-model failed console turn, and,
with `--restart-cycle`, kill the managed backend and confirm the restarted
backend can still read the same failed turn history.

## Source Chat Runtime Renderer Smoke

```bash
# Build console/dist first, then exercise the source backend plus Electron renderer.
npm --prefix console run build
uv run python scripts/source_chat_runtime_renderer_smoke.py

# Focused gate for base streaming plus an actual two-turn renderer chat.
uv run python scripts/source_chat_runtime_renderer_smoke.py --basic-only
```

This is the pre-package gate for foundational Chat Runtime behavior. It uses an
isolated temporary workspace and a local OpenAI-compatible fake provider, then
verifies actual Electron DOM states for Thinking before partial/final text,
single-card assistant chrome, two consecutive sends, FIFO rapid queueing,
stop/resend, and chat-route isolation. It never reads the user's configured
provider credentials or modifies the normal desktop workspace.

## Configured Provider Stream Smoke

```bash
# Explicit opt-in: stages a temporary copy of one configured agent and encrypted
# provider store, then checks real SSE ordering without printing credentials or output.
uv run python scripts/source_configured_provider_stream_smoke.py \
  --source-working-dir "$HOME/Library/Application Support/lantai-console/working" \
  --source-secret-dir "$HOME/Library/Application Support/lantai-console/working.secret" \
  --agent-id default \
  --timeout 90
```

This complements, but does not replace, the fake-provider renderer smoke. It
copies only the selected agent profile, `.master_key`, and encrypted provider
files into a fresh temporary directory, forces the old persisted-runtime
migration path there, then asserts that the configured model emits `reasoning`
SSE before answer text and a completed response. It deletes that temporary
directory on exit and prints only timing/type summary data: no key material and
no model response body. Run it only with the account owner's explicit approval;
a pass proves source-backend provider behavior, not a packaged desktop release.
The paths above are the current Downloads desktop app's Electron `userData`
layout on macOS. Confirm the active app process, provider-file modification time,
and model selectability before each run; `~/.lantai` may belong to a source run
or an older installation even when it still contains valid-looking data.

## Packaged Configured Provider Two-Turn Smoke

```bash
# Explicit opt-in: launch an isolated local .app candidate and exercise two
# consecutive turns in the same rendered Electron chat with the selected model.
uv run python scripts/packaged_configured_provider_stream_smoke.py \
  --app /tmp/lantai-candidate/mac-arm64/澜台.app \
  --distribution official \
  --source-working-dir "$HOME/.lantai" \
  --source-secret-dir "$HOME/.lantai.secret" \
  --agent-id default \
  --timeout 150 \
  --evidence-output /tmp/lantai-configured-provider-evidence.json

# S6 candidate: add one real generated-file turn and require the committed
# EffectRecord, delivered projection, and exactly one openable/downloadable card.
uv run python scripts/packaged_configured_provider_stream_smoke.py \
  --app /tmp/lantai-candidate/mac-arm64/澜台.app \
  --distribution official \
  --source-working-dir "$HOME/.lantai" \
  --source-secret-dir "$HOME/.lantai.secret" \
  --agent-id default \
  --timeout 150 \
  --require-artifact-delivery \
  --evidence-output /tmp/lantai-s6-artifact-evidence.json
```

This gate is deliberately stronger than the source-provider smoke. It creates
one isolated backend chat, submits two short sentinel prompts through the
actual Electron DOM, and requires Thinking to be visible before the answer in
each turn. At the end it requires exactly two user bubbles, two assistant
bubbles, two response avatars, two action bars, and no empty assistant bubble.
With `--require-artifact-delivery`, the gate adds a third real-provider turn,
requires one exact file card with open/download actions, verifies exact on-disk
content, and matches the committed primary EffectRecord to its delivered effect
by artifact identity and hash. The JSON evidence retains only timings, counts,
statuses, booleans, configured provider/model identifiers, and artifact hashes;
it never retains prompts, responses, credentials, or copied profile paths. It
is an internal candidate gate only. A pass does not create a release, authorize
signing/notarization, or justify replacing any Downloads build.

## Vibe Coding Preflight

```bash
# Run focused backend + frontend checks for vibe-coding reliability
python scripts/vibe_coding_preflight.py

# Include the frontend production build
python scripts/vibe_coding_preflight.py --full

# Write a machine-readable diagnosis report to a custom path
python scripts/vibe_coding_preflight.py --report .lantai/preflight/vibe-coding-report.json

# Smoke-test the failure diagnosis path without breaking real code
python scripts/vibe_coding_preflight.py --self-test-failure-report --report /tmp/vibe-failure-report.json

# Read a concise diagnosis from the latest JSON report
python scripts/read_vibe_preflight_report.py
python scripts/read_vibe_preflight_report.py --report /tmp/vibe-failure-report.json

# Run only one side when iterating locally
python scripts/vibe_coding_preflight.py --backend-only
python scripts/vibe_coding_preflight.py --frontend-only

# Show the planned commands without running them
python scripts/vibe_coding_preflight.py --list
```

The default report path is `.lantai/preflight/vibe-coding-report.json`. CI prints a short diagnosis and uploads the report as an artifact so failures can be triaged by category (`golden_evaluator`, `backend_runtime`, `frontend_runtime`, `frontend_build`) without digging through the full log first.
