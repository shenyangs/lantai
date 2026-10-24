---
name: xlsx
description: Create, analyze, or make bounded XLSX edits while preserving formulas and data meaning through Lantai's transactional Office boundary.
kind: artifact
domain: office
version: 2.0.0
license: Apache-2.0
source: builtin
enabled: true
---

# Lantai XLSX

Separate source data, formulas, presentation formatting, and immutable values before editing. Cite sheets, ranges, and calculation rules in analysis; never substitute demo values for missing input. Use documented openpyxl or OfficeCLI capabilities for structured operations. One writer owns each mutation, followed by isolated-copy execution, package checks, formula-recalculation capability checks, hash comparison, and atomic replacement. Continue from the newest workbook after a user edit. `scripts/preflight.py` only validates the container; see `references/runtime-boundary.md` for ownership.
