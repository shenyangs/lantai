# Community source snapshot

## Reliability update — 2026-11-06

This update incorporates the verified source changes from revision `246fb791da0b45350ce7cf3d990ade8df3643d6d` into the complete public snapshot. It adds 22 application and regression-test files or changes, while retaining this repository's independent Git history, MIT license, third-party notices and existing anonymization. Private audit records, runtime data and credentials are excluded.

The fixes address nine reproduced failure categories:

- Backend readiness requests that could hang indefinitely while receiving response headers or bodies, or accept a proof after the process had exited.
- Configuration saves and startup migrations that could truncate the last valid configuration on a write failure; writes now publish atomically.
- Cron pause/resume state lost after restart, including preservation of concurrency limits for active jobs.
- OpenAI Responses requests rejected because chat-completion token options were not normalized.
- File attachment context omitted from Responses requests.
- Retry drafts incorrectly registered as running before the frontend resubmitted them.
- Late session/history refreshes overwriting a newer conversation run's state.
- Prepared submission text lost before sending, including isolation from new typing during asynchronous preparation.
- Browser confirmation, denial and resume races leaving inconsistent task state or conflicting effect records.

Regression coverage includes interrupted writes and retry, process and HTTP lifecycle checks, stale-response races, simultaneous confirmations, denial, request cancellation and resume failure. PPT output quality, persona semantics and the unresolved acceptance work described in KNOWN_ISSUES.md are outside this update.

### Validation

Before export, the source candidate passed 4,797 backend tests, 321 targeted frontend tests and 537 Electron tests (including two explicitly enabled process tests). The full foundation verification also passed: 21 contracts, 21 incidents, 3,775 Python tests, 819 Vitest tests and 42 Node preflight checks. These groups overlap and should not be added together as a count of unique tests. TypeScript checks passed.

Against the updated **public source copy**, using existing development dependencies and isolated synthetic runtime data:

- Targeted backend and adjacent regressions: **524 passed**.
- Chat submission, refresh, runtime bridge and session API regressions: **321 passed** across five files.
- Electron backend lifecycle regressions: **18 passed**, no skips.
- Foundation experience gate against the previous public revision: **21 contracts and 21 incidents passed**.
- Builtin Skill license gate and `git diff --check`: passed.
- All changed production source files match the verified candidate byte for byte; prior public anonymization is retained in the affected test file.
- Gitleaks v8.30.1 with fully redacted reports: no findings in the 22 changed code/test files; the full 5,004-file public snapshot retains only the seven previously reviewed findings in unchanged files.

No new installer was built or published. This update does not establish fresh dependency installation, cross-platform packaging, real Provider acceptance, signing or notarization results. Existing binary releases do not contain this source-only update.

## Initial snapshot — 2026-10-24

This repository starts a new Git history from integrated source revision `8e0ca88fdb7d2f34ed2ade8cd40f3584d0972fc4`. The original private repository and working copies are retained privately. Uncommitted work from other branches is not represented by this snapshot.

Included: application source, bundled component licenses/assets, development and packaging scripts, contracts, regression tests, dependency lockfiles and selected architecture documentation. Excluded: private Git history, coordination records, internal acceptance artifacts, user runtime data, private service/deployment material and local evaluation scripts tied to personal documents. Machine-specific sample paths were anonymized. Root community documentation is new.

## Checks performed for this export

- Python syntax parsed for 1,969 Python files; no parse errors.
- 22 npm package/lock manifests parsed successfully.
- Foundation experience gate passed: 21 protected contracts and 21 registered incidents (bootstrap baseline).
- Targeted Python tests: `test_redaction.py` and `test_sanitized_export.py`, **18 passed** using existing development dependencies, against this exported source.
- PDF visibility lifecycle regression: **1 passed**, using existing development Node dependencies against exported source. The first invocation without dependencies failed with missing `typescript`; this was an environment prerequisite, not a passing run.
- Gitleaks v8.30.1 directory scan with redacted reporting, plus targeted review for private paths, source references and credential-like strings. Seven source findings were reviewed: synthetic test credentials (four), model file SHA-256 values (two), and an Ed25519 type annotation (one). None of those seven is an operational credential. Generated bytecode was excluded from publication.
- Builtin Skill license gate passed, including the replacement Office skills.
- Bundled third-party notices retained and indexed in NOTICE. This is not an exhaustive legal or vulnerability audit.

## Not performed

No fresh full dependency installation, full regression suite, cross-platform build, live model/provider run, signing, notarization or new binary release was performed for this source publication. Historical product acceptance failures remain unresolved; this source publication does not supersede them. Architecture documents and retained release scripts may reference private evidence or historical documents that are intentionally absent.

Active maintenance is paused. See KNOWN_ISSUES.md before using or redistributing a build.
