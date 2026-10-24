#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
python_bin="${repo_root}/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "Missing project Python runtime: ${python_bin}" >&2
  echo "Create .venv and install the project test dependencies before running this gate." >&2
  exit 2
fi

cd "${repo_root}"

"${python_bin}" -m pytest -q \
  tests/unit/agents/memory/test_memory_ledger_golden.py \
  tests/unit/agents/memory/test_canonical_ledger.py \
  tests/unit/agents/memory/test_memory_policy.py \
  tests/unit/agents/memory/test_layered_memory.py \
  tests/unit/agents/memory/test_structured_user_memory.py \
  tests/unit/agents/memory/test_user_understanding_integration.py \
  tests/unit/agents/memory/test_memory_pack.py \
  tests/unit/agents/memory/test_memory_migration.py \
  tests/unit/agents/memory/test_memory_maintenance.py \
  tests/unit/agents/test_memory_usage_trace.py \
  tests/unit/app/test_workspace_memory_facts.py \
  tests/unit/app/test_workspace_memory_pack.py
