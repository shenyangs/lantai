---
name: pdf
description: Read, extract, render, or create PDF files while verifying the actual pages and without promising native Office-style editability.
kind: artifact
domain: office
version: 2.0.0
license: Apache-2.0
source: builtin
enabled: true
---

# Lantai PDF

Classify the task as text extraction, layout understanding, page rendering, merge/split, or fixed-layout delivery. Use Lantai reading facilities and documented PyMuPDF, pypdf, pdfplumber, or pypdfium2 APIs. When OCR is required, retain page references and mark uncertain text. After creation or mutation, check the signature, trailer, and page count, then render the actual final PDF for visual review. A PDF is fixed-layout output and must not be described as fully editable Office content. `scripts/preflight.py` performs a read-only basic check and `scripts/render_pages.py` renders actual pages through the centralized PDFium boundary; see `references/runtime-boundary.md`.
