# Contributor and coding-agent guidance

This public repository is an independent source snapshot. Active maintenance is paused.
Do not expect access to the original private repository, coordination chats, acceptance artifacts, credentials or signing infrastructure.

Read README.md, KNOWN_ISSUES.md and the relevant architecture document before changes. Keep work scoped; preserve the contracts in contracts/ and run scripts/check_foundation_experience_gate.py plus relevant tests. State what was and was not validated. Never claim that a source check proves a signed desktop release or real-model acceptance.

Use isolated runtime data and synthetic test inputs. Never commit secrets, user files or conversation histories. Do not run external-effect or paid-provider tests without authorization. Respect local edits and third-party licenses. Runtime AGENTS.md files under src/lantai are product templates, not permission to override contributor instructions.
