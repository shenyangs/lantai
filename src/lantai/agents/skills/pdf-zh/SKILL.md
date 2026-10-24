---
name: pdf
description: 读取、提取、渲染或创建 PDF，并验证真实页面与最终文件，不把 PDF 冒充可完全原生编辑的 Office 文档。
kind: artifact
domain: office
version: 2.0.0
license: Apache-2.0
source: builtin
enabled: true
---

# 澜台 PDF

先判断任务是文本抽取、版面理解、页面渲染、合并拆分还是固定版式交付。使用澜台读取能力及许可证明确的 PyMuPDF、pypdf、pdfplumber 或 pypdfium2 公开接口。扫描件需要 OCR 时标记来源页与不确定内容。创建或修改后检查签名、尾标、页数，并渲染实际最终 PDF 做视觉复核。PDF 是固定版式产物，不能承诺 Office 对象级编辑。`scripts/preflight.py` 为只读基础检查，`scripts/render_pages.py` 使用集中式 PDFium 边界渲染实际页面；详见 `references/runtime-boundary.md`。
