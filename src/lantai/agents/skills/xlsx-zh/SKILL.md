---
name: xlsx
description: 创建、分析或有边界地修改 XLSX，保留公式与数据语义，并通过澜台 Office 事务层验证交付。
kind: artifact
domain: office
version: 2.0.0
license: Apache-2.0
source: builtin
enabled: true
---

# 澜台 XLSX

先区分原始数据、公式、展示格式和不可改数值。分析应引用工作表、范围和计算口径；不得制造示例数据替代缺失输入。结构化读写使用许可证明确的 openpyxl 或 OfficeCLI 公开能力。一次修改只允许一个 writer，经隔离副本、包检查、公式重算能力检查、哈希比较和原子替换后交付。用户手改后必须从最新工作簿继续。`scripts/preflight.py` 仅验证容器；职责边界见 `references/runtime-boundary.md`。
