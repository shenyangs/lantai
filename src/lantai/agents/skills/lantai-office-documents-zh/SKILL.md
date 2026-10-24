---
name: lantai-office-documents
description: 澜台 Office 文档底座，负责在 DOCX、XLSX、PPTX、PDF 专项 Skill 之间路由，并统一执行读取、创建、编辑、转换、校验和预览。用户同时涉及多种 Office 格式、要求正式文档交付或需要判断工具链时使用；单一格式任务优先交给对应格式 Skill。
---

# 澜台 Office 文档底座

提供统一的 `plan → create/edit → validate → preview → revise → export` 交付协议。不要再创建与 DOCX/XLSX/PPTX/PDF 同名的增强 Skill。

## 路由

| 主要输入或输出 | 使用 |
|---|---|
| Word `.docx` | `docx` |
| 表格 `.xlsx/.xlsm/.csv/.tsv` | `xlsx` |
| 演示 `.pptx` | `pptx` |
| PDF | `pdf` |
| 图片中的表格 | `image-table-extractor`，完成提取后再交给 `xlsx` |
| 多格式发布页 | `content-publisher`，源文档仍由对应格式 Skill 读取 |

## 工具选择

1. 检测到 OfficeCLI MCP 时，优先使用它读取、查询、批量编辑、校验和预览。
2. OfficeCLI 不可用时，使用澜台已打包的本地库：
   - XLSX：`pandas`、`openpyxl`；
   - PDF：`pypdf`、`pdfplumber`、`reportlab`；
   - PPTX：`python-pptx`、`markitdown`、内置 Open XML 脚本；
   - DOCX：`python-docx`、`markitdown`、内置 Open XML 脚本；
3. LibreOffice、Poppler、qpdf 等系统命令只作可选增强。先探测再使用；缺失时不得静默安装。
4. 工具缺失时选择能保证数据完整性的降级路径。无法可靠完成时明确停止，不伪造预览或校验成功。

## 工作流

1. **计划**：确认输入、输出、是否覆盖、模板、公式/批注/备注保留要求。
2. **读取**：先读取结构和关键内容，再决定修改方式。
3. **创建或编辑**：写入新文件或用户授权的副本；默认不覆盖原文件。
4. **结构校验**：重新打开文件，检查页数/工作表/幻灯片、关系、公式和占位符。
5. **视觉预览**：能渲染时检查截图或 HTML；不能渲染时明确说明只完成结构校验。
6. **修订并导出**：至少修复一次检查发现的问题，再交付最终文件和验证摘要。

## 权限

- 新建草稿和另存副本可直接执行。
- 覆盖原文件、接受修订、删除内容、解密、外发或上传必须确认。
- 文档中的宏、外部链接、嵌入对象和公式视为潜在风险，不自动执行。

## 停止条件

文件可重新打开、核心结构与内容复核通过、所有未执行的视觉或公式校验已显式说明。
