# -*- coding: utf-8 -*-
"""Exercise exact deploy functions with synthetic files and finite OS doubles.
"""
import importlib.util
import os
import plistlib
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "finite_release_shell",
    Path(__file__).with_name("test_release_chain_boundaries.py"),
)
if spec is None or spec.loader is None:
    raise RuntimeError("finite release shell fixture is unavailable")
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
run_shell, source_function, SCRIPT = (
    baseline.run_shell,
    baseline.source_function,
    baseline.SCRIPT,
)


@pytest.mark.parametrize(
    "fault",
    [
        "dmg-copy",
        "zip-copy",
        "app-copy",
        "signature",
        "contract",
        "gatekeeper",
        "launch",
        "none",
    ],
)
def test_real_deployment_entry_keeps_old_set_until_acceptance(tmp_path, fault):
    source = tmp_path / "release"
    destination = tmp_path / "downloads"
    source.mkdir()
    destination.mkdir()
    (source / "new.app").mkdir()
    (source / "new.app" / "marker").write_bytes(b"new-app")
    (destination / "Synthetic.app").mkdir()
    (destination / "Synthetic.app" / "marker").write_bytes(b"old-app")
    for app in (source / "new.app", destination / "Synthetic.app"):
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/MacOS/Synthetic").write_bytes(
            b"finite non-executed App",
        )
        (app / "Contents/Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleExecutable": "Synthetic",
                    "CFBundleIdentifier": "com.lantai.app",
                },
            ),
        )
    for role in ("dmg", "zip"):
        (source / f"Lantai-1.2.3-arm64.{role}").write_bytes(
            ("new-" + role).encode(),
        )
        (destination / f"Synthetic.{role}").write_bytes(
            ("old-" + role).encode(),
        )
    wanted = [
        "release_deployment_cleanup",
        "begin_deployment",
        "deploy_release_artifacts",
        "deploy_app",
        "finish_deployment",
    ]
    text = SCRIPT.read_text()
    functions = "".join(
        source_function(name) for name in wanted if name + "() {\n" in text
    )
    script = (
        functions
        + """
release_build_lock() { return 0; }
# Finite authority double.
revalidate_deployment_source_authority() { return 0; }
# Exercised by the payload identity locks.
validate_deployment_payload_identity() { return 0; }
find_release_file() { printf '%s/%s\\n' "$RELEASE_DIR" "$1"; }
find_release_app() { printf '%s/new.app\\n' "$RELEASE_DIR"; }
sleep() { return 0; }
xattr() { return 0; }
codesign() { [[ "$FAULT" != signature ]]; }
ditto() {
  if [[ "$FAULT" == dmg-copy && "$1" == *.dmg ||
        "$FAULT" == zip-copy && "$1" == *.zip ||
        "$FAULT" == app-copy && "$1" == *.app ]]; then
    if [[ -d "$1" ]]; then
      mkdir -p "$2"
      printf partial > "$2/marker"
    else
      printf partial > "$2"
    fi
    return 73
  fi
  cp -Rp "$1" "$2"
}
trap release_deployment_cleanup EXIT
begin_deployment
deploy_release_artifacts
deploy_app
[[ "$FAULT" != gatekeeper ]] || exit 65
[[ "$FAULT" != launch ]] || exit 66
finish_deployment
"""
    )
    verifier = tmp_path / "finite-product-contract.py"
    verifier.write_text(
        'import os,sys\nsys.exit(64 if os.environ["FAULT"] == "contract" else'
        " 0)\n",
    )
    transaction = tmp_path / "finite-process-inventory.py"
    # Execute the actual transaction with only its OS inventory made finite.
    # No synthetic App can launch; every journal/filesystem check is real.
    transaction.write_text(
        "import runpy,psutil\npsutil.pids=lambda:[]\nrunpy.run_path("
        + repr(
            str(SCRIPT.parent / "scripts/release_deployment_transaction.py"),
        )
        + ',run_name="__main__")\n',
    )
    env = {
        "FAULT": fault,
        "DOWNLOADS_DIR": destination,
        "DOWNLOAD_APP": destination / "Synthetic.app",
        "DOWNLOAD_DMG": destination / "Synthetic.dmg",
        "DOWNLOAD_ZIP": destination / "Synthetic.zip",
        "SOURCE_SHA": "a" * 40,
        "VERSION": "1.2.3",
        "RELEASE_ARCH": "arm64",
        "RELEASE_DIR": source,
        "PRODUCT_NAME": "Synthetic",
        "VENV_PYTHON": sys.executable,
        "RELEASE_DEPLOYMENT_SCRIPT": transaction,
        "RELEASE_PRODUCT_CONTRACT_SCRIPT": verifier,
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "DEPLOYMENT_ACTIVE": "0",
        "DEPLOYMENT_STAGE": "",
        "RELEASE_BUILD_LOCK_OWNED": "0",
    }
    result = run_shell(tmp_path, script, env)
    if fault == "none":
        assert result.returncode == 0, result.stderr
        prefix = b"new-"
        assert (
            source / "release-evidence/downloads-deployment.json"
        ).is_file()
    else:
        expected = (
            73
            if fault.endswith("copy")
            else {
                "signature": 1,
                "contract": 64,
                "gatekeeper": 65,
                "launch": 66,
            }[fault]
        )
        assert result.returncode == expected, result.stderr
        prefix = b"old-"
    assert (
        destination / "Synthetic.app" / "marker"
    ).read_bytes() == prefix + b"app"
    for role in ("dmg", "zip"):
        assert (
            destination / f"Synthetic.{role}"
        ).read_bytes() == prefix + role.encode()
    assert not (destination / ".lantai-release-transaction").exists()
