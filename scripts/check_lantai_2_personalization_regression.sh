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
  tests/unit/customization/test_customization_core.py \
  tests/unit/routers/test_customization.py \
  tests/unit/agents/tools/test_customization.py \
  tests/unit/scripts/test_pyinstaller_spec_policy.py::test_pyinstaller_spec_bundles_customization_registry_data

npm --prefix console run test:run -- \
  src/pages/Settings/InnovationLab/index.test.tsx \
  src/pages/Settings/Home/index.test.tsx \
  src/pages/Settings/Appearance/index.test.tsx \
  src/pages/Settings/Appearance/AppIconPicker.test.tsx \
  src/pages/Chat/components/customizationDraft.test.ts

node --test \
  console/electron/customizationRecoveryController.test.cjs \
  console/electron/dockIconController.test.cjs
