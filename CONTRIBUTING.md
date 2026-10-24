# Contributing

Active development is paused. Forks and focused pull requests are welcome, but there is no promised review, support or release schedule.

Use a small, reproducible issue with synthetic inputs. Explain expected and actual behavior, platform, version and the smallest relevant test. Never include credentials, private documents or real conversation logs.

Keep changes scoped. Preserve workspace permissions, user confirmation boundaries, session ownership, credential handling and the contracts under `contracts/`. Run the relevant tests and the foundation gate; report failures and checks you did not run. Do not change a test or gate merely to label an unresolved behavior as passing.

Use isolated development data directories. Model-backed and external-effect tests require your own accounts, authorization and budgets. Do not reuse the original author's private paths, signing identities, release services or internal acceptance material.

Retain third-party licenses and attribution. Explain provenance and license for new assets and dependencies. See LICENSE, NOTICE and THIRD_PARTY_NOTICES.md.
