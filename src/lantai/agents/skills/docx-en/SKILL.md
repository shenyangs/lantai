---
name: docx
description: Create, read, or make bounded DOCX edits through Lantai's transactional Office boundary and deliver a validated native document.
kind: artifact
domain: office
version: 2.0.0
license: Apache-2.0
source: builtin
enabled: true
---

# Lantai DOCX

Establish purpose, audience, sources, and immutable facts before structuring headings, prose, tables, and citations. Prefer Lantai file extraction for reading and use documented python-docx or OfficeCLI capabilities for structured creation and bounded changes. Each mutation has one writer and is delivered only after isolated-copy execution, container validation, hash comparison, and atomic replacement. After a GenOffice edit, continue from the latest file rather than overwriting it with stale state. Report the output path, validation result, and rollback version. `scripts/preflight.py` is read-only; see `references/runtime-boundary.md` for ownership.
