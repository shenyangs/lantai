#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint:disable=too-many-statements
"""
Create a temporary conda env, install Lantai from a wheel, run conda-pack.
Used by build_macos.sh and build_win.ps1. Run from repo root.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import shutil
import tempfile
import string
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PREFIX = "lantai_pack_"

# Packages affected by conda-unpack bug on Windows (conda-pack Issue #154)
# conda-unpack modifies Python source files to replace path prefixes, but uses
# simple byte replacement without considering Python syntax. This corrupts
# string literals containing backslash escapes, causing SyntaxError.
# Example: "\\\\?\\" (correct) -> "\\" (SyntaxError: unterminated string)
# Solution: After conda-unpack, reinstall these packages to restore correct files
# See: issue.md and https://github.com/conda/conda-pack/issues/154
CONDA_UNPACK_AFFECTED_PACKAGES = [
    "huggingface_hub",  # file_download.py, _local_folder.py use Windows long path prefix
    "discord.py",       # ARG_NAME_SUBREGEX contains \\?\* which gets corrupted
]


def _conda_exe() -> str:
    """Resolve conda executable (required on Windows where 'conda' is a batch)."""
    exe = os.environ.get("CONDA_EXE")
    if exe:
        return exe
    return "conda"


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Run command with optional environment variable overrides."""
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    subprocess.run(cmd, cwd=cwd or REPO_ROOT, env=run_env, check=True)


def _pick_wheel(wheel_arg: str | None) -> Path:
    if wheel_arg:
        wheel_path = Path(wheel_arg).expanduser()
        if not wheel_path.is_absolute():
            wheel_path = (REPO_ROOT / wheel_path).resolve()
        if not wheel_path.exists():
            raise FileNotFoundError(f"Wheel not found: {wheel_path}")
        return wheel_path

    wheels = list((REPO_ROOT / "dist").glob("lantai-*.whl"))
    if len(wheels) != 1:
        raise ValueError("Provide an exact --wheel; dist/ must contain exactly one Lantai wheel")
    return wheels[0]



def main() -> int:
    parser = argparse.ArgumentParser(
        description="Conda-pack Lantai (temp env).",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Output archive path (e.g. .tar.gz)",
    )
    parser.add_argument(
        "--format",
        "-f",
        default="infer",
        choices=["infer", "zip", "tar.gz", "tgz"],
        help="Archive format (default: infer from --output extension)",
    )
    parser.add_argument(
        "--python",
        default="3.10",
        help="Python version for conda env (default: 3.10)",
    )
    parser.add_argument(
        "--wheel",
        default=None,
        help=(
            "Exact wheel path to install. If omitted, dist/ must have exactly one wheel."
        ),
    )
    parser.add_argument(
        "--cache-wheels",
        action="store_true",
        help=(
            "Download wheels for packages affected by conda-unpack bug. "
            "Cached in the owned --inputs-dir for exact hash-verified repair."
        ),
    )
    parser.add_argument("--inputs-dir", type=Path)
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    out_path = Path(args.output).resolve()
    if out_path.exists() or out_path.is_symlink():
        raise FileExistsError(f"Refusing to overwrite a prior archive: {out_path}")
    _run([sys.executable, str(REPO_ROOT / "scripts/worktree_guard.py"), "check",
          "--require-clean", "--require-no-active-lease"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wheel_path = _pick_wheel(args.wheel)
    wheel_uri = wheel_path.resolve().as_uri()
    wheel_sha = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    if args.inputs_dir:
        inputs = args.inputs_dir.resolve()
        inputs.mkdir(parents=True, exist_ok=False)
    else:
        inputs = Path(tempfile.mkdtemp(prefix=".lantai-pack-inputs-", dir=out_path.parent))
    helper = REPO_ROOT / "scripts/pack/locked_runtime_inputs.py"
    tools_lock = REPO_ROOT / "scripts/pack/packaging-tools.lock.txt"
    requirements = inputs / "runtime.lock.txt"
    repair_requirements = inputs / "repair.lock.txt"
    uv = shutil.which("uv") or "uv"
    if bool(args.expected_source_sha) != bool(args.expected_version):
        raise ValueError("Both independently expected wheel source and version are required")
    if args.expected_source_sha:
        _run([sys.executable, str(helper), "verify-wheel", "--wheel", str(wheel_path),
              "--source-sha", args.expected_source_sha, "--version", args.expected_version])
    _run([uv, "export", "--frozen", "--no-dev", "--no-default-groups", "--extra", "full",
          "--no-emit-project", "--format", "requirements.txt", "--output-file", str(requirements)])
    env_name = f"{ENV_PREFIX}{''.join(random.choices(string.ascii_lowercase, k=8))}"
    conda = _conda_exe()
    runtime = [conda, "run", "-n", env_name, "python"]
    try:
        # Keep conda's runtime file ownership separate from pip's packaging
        # tools; conda-pack can enforce its normal missing-file checks.
        _run([conda, "create", "-n", env_name, f"python={args.python}",
              "--no-default-packages", "-y"],
             env={"CONDA_ADD_PIP_AS_PYTHON_DEPENDENCY": "false"})
        _run([*runtime, str(helper), "bootstrap", "--uv", uv,
              "--requirements", str(tools_lock), "--report", str(inputs / "tools-install.json")])
        _run([*runtime, "-m", "pip", "install", "--require-hashes", "--no-deps",
              "--no-build-isolation", "-r", str(requirements),
              "--report", str(inputs / "runtime-install.json")])
        # A local exact wheel is installed without resolving its extras; the
        # transitive runtime was already installed from frozen hashes above.
        _run([*runtime, "-m", "pip", "install", "--no-deps", "--require-hashes",
              f"lantai[full] @ {wheel_uri}#sha256={wheel_sha}",
              "--report", str(inputs / "lantai-install.json")])
        print("Verifying certifi is installed (required for SSL)...")
        _run(
            [
                conda,
                "run",
                "-n",
                env_name,
                "python",
                "-c",
                "import certifi; print(f'certifi OK: {certifi.where()}')",
            ],
        )
        print("Verifying built-in Guanlan MCP runtime...")
        _run(
            [
                conda,
                "run",
                "-n",
                env_name,
                "python",
                "-c",
                (
                    "import importlib.metadata as m; "
                    "import guanlan.integrations.mcp_server; "
                    "print('Guanlan OK:', m.version('guanlan'))"
                ),
            ],
        )
        if args.cache_wheels:
            wheels_cache = inputs / "repair-wheels"
            wheels_cache.mkdir(exist_ok=False)
            _run([sys.executable, str(helper), "select-repair", "--requirements", str(requirements),
                  "--output", str(repair_requirements),
                  *[part for package in CONDA_UNPACK_AFFECTED_PACKAGES for part in ("--package", package)]])
            _run([*runtime, "-m", "pip", "download", "--require-hashes", "--no-deps",
                  "--only-binary=:all:", "-r", str(repair_requirements), "-d", str(wheels_cache)])
        if hashlib.sha256(wheel_path.read_bytes()).hexdigest() != wheel_sha:
            raise ValueError("Wheel bytes changed during packing")
        pack_cmd = [
            conda,
            "run",
            "-n",
            env_name,
            "conda-pack",
            "-n",
            env_name,
            "-o",
            str(out_path),
        ]
        if args.format != "infer":
            pack_cmd.extend(["--format", args.format])
        _run(pack_cmd)
        print(f"Packed to {out_path}")
    finally:
        try:
            _run([conda, "env", "remove", "-n", env_name, "-y"])
        except Exception as e:
            print(f"Warning: Failed to remove temp env {env_name}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
