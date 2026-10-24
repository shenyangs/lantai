---
name: QA_source_index
description: "Map user topics and keywords to Lantai local README files and common source entry points to avoid blind searching."
metadata:
  builtin_skill_version: "1.3"
  lantai:
    emoji: "🗂️"
    requires: {}
---

# Documentation and Source Quick Reference

For questions about installation, configuration, or behavior, classify the topic first, then read the 1 to 2 paths most likely to contain the answer.

## Topic Entry Points

| Topic or keywords | Preferred docs | Common source entry points |
| --- | --- | --- |
| Installation, dependencies, first run | `README_en.md`, `scripts/README.md` | `src/lantai/cli/`, `pyproject.toml` |
| Updates, auto-update, releases | `README_en.md` | `console/electron/`, `console/src/layouts/constants.ts`, `.github/workflows/desktop-release.yml` |
| Configuration, config.json, environment variables | `README_en.md` | `src/lantai/config/`, `src/lantai/constant.py` |
| Docker, deployment | `README_en.md`, `docker-compose.yml` | `deploy/`, `scripts/docker_build.sh` |
| Desktop client | `README_en.md` | `console/electron/`, `scripts/pack/` |
| Channels, sessions, callbacks | `README_en.md` | `src/lantai/app/channels/` |
| Skills, SKILL, skill_pool | `README_en.md` | `src/lantai/agents/skills_manager.py`, `src/lantai/agents/skills/` |
| MCP, tools | `README_en.md` | `src/lantai/app/routers/`, `src/lantai/agents/tools/` |
| Multi-agent, workspace, agent | `README_en.md` | `src/lantai/app/routers/agents.py`, `src/lantai/app/migration.py` |
| Memory, memory_search | `README_en.md` | `src/lantai/agents/memory/`, `src/lantai/agents/tools/memory_search.py` |
| Models, API keys, local models | `README_en.md` | `src/lantai/providers/`, `src/lantai/local_models/` |
| Security, permissions, sandboxing | `SECURITY.md` | `src/lantai/security/` |
| Errors, FAQ-style questions | `README_en.md` | Search the exact error with `rg`, then read the matched source |

## Conventions

- Prefer local files. Do not use removed websites.
- If English docs are missing, use `README.md` as the fallback.
- Source entry points are starting points only; read the specific files and symbols before answering.
