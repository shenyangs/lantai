---
name: docx
description: 创建、读取或有边界地修改 DOCX，并通过澜台 Office 事务层验证和交付原生文档。
kind: artifact
domain: office
version: 2.0.0
license: Apache-2.0
source: builtin
enabled: true
---

# 澜台 DOCX

先确认文档用途、受众、来源与必须保留的事实，再组织标题层级、正文、表格和引用。读取优先使用澜台文件抽取；结构化创建或修改使用许可证明确的 python-docx 或 OfficeCLI 公开能力。一次修改只能有一个 writer，并经隔离副本、容器验证、哈希检查和原子替换后交付。用户在 GenOffice 中修改后必须从最新文件继续，禁止用旧状态覆盖。交付时说明路径、校验结果与可回滚版本。`scripts/preflight.py` 仅做只读包检查；工具职责见 `references/runtime-boundary.md`。
