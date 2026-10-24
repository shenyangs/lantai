---
name: guidance
description: "回答用户关于 澜台 安装、配置、更新、部署的问题：优先读取本地 README 和源码，再基于已核实内容作答。"
metadata:
  builtin_skill_version: "1.3"
  lantai:
    emoji: "🧭"
    requires: {}
---

# 澜台安装与配置问答指南

当用户询问澜台的安装、初始化、环境配置、更新、桌面端、Docker、频道或常见配置项时，使用本 skill。

## 原则

- 先读本地文档和源码，再回答。
- 不臆测不存在的配置项、命令或网站。
- 回答语言与用户提问语言一致。
- 若文档缺失，明确说明缺口，并根据当前源码给出可验证结论。

## 文档定位

优先从以下位置查找：

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

若运行环境中存在源码根目录，按如下方式定位：

```bash
LANTAI_BIN=$(command -v lantai 2>/dev/null || true)
if [ -n "$LANTAI_BIN" ] && [[ "$LANTAI_BIN" == *"/.lantai/bin/lantai" ]]; then
  LANTAI_ROOT="${LANTAI_BIN%/.lantai/bin/lantai}"
else
  LANTAI_ROOT="${LANTAI_ROOT:-.}"
fi
```

随后在 `$LANTAI_ROOT` 中读取 README 或按关键词搜索源码：

```bash
find "$LANTAI_ROOT" -maxdepth 2 -name "README*.md" -o -name "SECURITY.md"
rg -n "install|update|docker|config|channel|skill|model" "$LANTAI_ROOT/src/lantai" "$LANTAI_ROOT/console" 2>/dev/null
```

## 常见主题入口

- 安装与启动：`README_zh.md`、`scripts/install.sh`、`scripts/install.ps1`、`src/lantai/cli/`
- 配置：`src/lantai/config/`、`src/lantai/constant.py`
- 更新：`console/electron/main.cjs`、`console/electron/updateBridge.cjs`、`console/src/layouts/constants.ts`
- Docker：`docker-compose.yml`、`deploy/Dockerfile`、`scripts/docker_build.sh`
- 频道：`src/lantai/app/channels/`
- 技能：`src/lantai/agents/skills/`、`src/lantai/agents/skills_manager.py`
- 模型：`src/lantai/providers/`、`src/lantai/local_models/`
- 安全：`SECURITY.md`、`src/lantai/security/`

## 作答要求

- 先给直接结论，再给步骤或命令。
- 路径、命令、配置键尽量给可复制片段。
- 涉及版本差异时，以当前本地文件为准。
- 如果仍无法确认，说明已经检查过哪些路径，以及还需要用户提供什么信息。
