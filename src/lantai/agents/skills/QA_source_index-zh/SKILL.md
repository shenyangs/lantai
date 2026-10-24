---
name: QA_source_index
description: "将用户问题中的主题、关键词映射到澜台本地 README 与常见源码入口，减少盲目搜索。"
metadata:
  builtin_skill_version: "1.3"
  lantai:
    emoji: "🗂️"
    requires: {}
---

# 文档与源码速查

回答安装、配置、行为原理类问题时，先按关键词归类，再打开 1 到 2 个最可能命中的路径阅读。

## 主题 / 关键词入口

| 主题或关键词 | 优先文档 | 常见源码入口 |
| --- | --- | --- |
| 安装、依赖、首次使用 | `README_zh.md`、`scripts/README.md` | `src/lantai/cli/`、`pyproject.toml` |
| 更新、自动更新、release | `README_zh.md` | `console/electron/`、`console/src/layouts/constants.ts`、`.github/workflows/desktop-release.yml` |
| 配置、config.json、环境变量 | `README_zh.md` | `src/lantai/config/`、`src/lantai/constant.py` |
| Docker、部署 | `README_zh.md`、`docker-compose.yml` | `deploy/`、`scripts/docker_build.sh` |
| 桌面客户端 | `README_zh.md` | `console/electron/`、`scripts/pack/` |
| 频道、会话、回调 | `README_zh.md` | `src/lantai/app/channels/` |
| 技能、SKILL、skill_pool | `README_zh.md` | `src/lantai/agents/skills_manager.py`、`src/lantai/agents/skills/` |
| MCP、工具 | `README_zh.md` | `src/lantai/app/routers/`、`src/lantai/agents/tools/` |
| 多智能体、workspace、agent | `README_zh.md` | `src/lantai/app/routers/agents.py`、`src/lantai/app/migration.py` |
| 记忆、memory_search | `README_zh.md` | `src/lantai/agents/memory/`、`src/lantai/agents/tools/memory_search.py` |
| 模型、API Key、本地模型 | `README_zh.md` | `src/lantai/providers/`、`src/lantai/local_models/` |
| 安全、权限、沙箱 | `SECURITY.md` | `src/lantai/security/` |
| 报错、常见问题 | `README_zh.md` | 先按报错关键词 `rg`，再读命中源码 |

## 约定

- 首选本地文件，不访问已移除的网站。
- 无中文说明时，使用 `README_en.md` 兜底。
- 表中源码入口只是起点；定位后应读取具体文件和符号，再组织答案。
