---
name: guidance
description: "Answer questions about Lantai installation, configuration, updates, and deployment by reading local README files and source code first."
metadata:
  builtin_skill_version: "1.3"
  lantai:
    emoji: "🧭"
    requires: {}
---

# Lantai Installation and Configuration Guide

Use this skill when the user asks about Lantai installation, initialization, configuration, updates, desktop packaging, Docker, channels, or common runtime options.

## Principles

- Read local documentation and source code before answering.
- Do not invent configuration keys, commands, or websites.
- Answer in the same language the user used.
- If documentation is missing, state the gap and answer from verified local source code.

## Documentation Lookup

Check these files first:

```bash
README.md
README_zh.md
README_en.md
README_ja.md
README_ko.md
CONTRIBUTING.md
SECURITY.md
scripts/README.md
```

If a source tree is available, resolve it from the installed command when possible:

```bash
LANTAI_BIN=$(command -v lantai 2>/dev/null || true)
if [ -n "$LANTAI_BIN" ] && [[ "$LANTAI_BIN" == *"/.lantai/bin/lantai" ]]; then
  LANTAI_ROOT="${LANTAI_BIN%/.lantai/bin/lantai}"
else
  LANTAI_ROOT="${LANTAI_ROOT:-.}"
fi
```

Then read README files or search focused source paths:

```bash
find "$LANTAI_ROOT" -maxdepth 2 -name "README*.md" -o -name "SECURITY.md"
rg -n "install|update|docker|config|channel|skill|model" "$LANTAI_ROOT/src/lantai" "$LANTAI_ROOT/console" 2>/dev/null
```

## Common Entry Points

- Install and startup: `README_en.md`, `scripts/install.sh`, `scripts/install.ps1`, `src/lantai/cli/`
- Configuration: `src/lantai/config/`, `src/lantai/constant.py`
- Updates: `console/electron/main.cjs`, `console/electron/updateBridge.cjs`, `console/src/layouts/constants.ts`
- Docker: `docker-compose.yml`, `deploy/Dockerfile`, `scripts/docker_build.sh`
- Channels: `src/lantai/app/channels/`
- Skills: `src/lantai/agents/skills/`, `src/lantai/agents/skills_manager.py`
- Models: `src/lantai/providers/`, `src/lantai/local_models/`
- Security: `SECURITY.md`, `src/lantai/security/`

## Answer Requirements

- Give the direct answer first, then steps or commands.
- Use copy-pasteable paths, commands, and config keys.
- For version differences, rely on the current local files.
- If the answer is still uncertain, say which paths were checked and what additional input is needed.
