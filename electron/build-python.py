#!/usr/bin/env python3
"""Build script: package Python backend with PyInstaller for Electron distribution."""
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backend_bundle_layout import finalize_backend_bundle_layout

SRC_DIR = PROJECT_ROOT / "src"
CONSOLE_DIR = SRC_DIR / "lantai" / "console"
DIST_DIR = PROJECT_ROOT / "console" / "electron-dist"
SPEC_FILE = PROJECT_ROOT / "lantai-server.spec"
UV_LOCK_FILE = PROJECT_ROOT / "uv.lock"
GUANLAN_MIN_VERSION = Version("0.10.9")


def _release_version() -> str:
    package_json = PROJECT_ROOT / "console" / "package.json"
    payload = json.loads(package_json.read_text(encoding="utf-8"))
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError(f"Missing release version in {package_json}")
    return version.strip()


def _require_locked_python_dependencies() -> None:
    if not UV_LOCK_FILE.exists():
        raise RuntimeError("Missing uv.lock for release Python build")

    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to verify uv.lock before release build")

    env = os.environ.copy()
    env.setdefault("UV_PYTHON_DOWNLOADS", "never")
    subprocess.run(
        [uv, "lock", "--check"],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
    )
    subprocess.run(
        [uv, "sync", "--locked", "--extra", "dev", "--check"],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
    )


def _ensure_guanlan_runtime() -> None:
    """Make Guanlan importable before PyInstaller analyzes the backend."""

    _require_locked_python_dependencies()

    try:
        version = Version(importlib.metadata.version("guanlan"))
    except importlib.metadata.PackageNotFoundError:
        raise RuntimeError(
            "Guanlan is not installed in the build environment. "
            "Prepare release dependencies from uv.lock before packaging; "
            "this build step will not install or upgrade Python packages.",
        )

    mcp_spec = importlib.util.find_spec("guanlan.integrations.mcp_server")
    if version >= GUANLAN_MIN_VERSION and mcp_spec is not None:
        print(f"Guanlan bundled dependency ready: {version}")
        return

    raise RuntimeError(
        f"Guanlan {version} is unavailable for packaging; need "
        f">= {GUANLAN_MIN_VERSION} with guanlan.integrations.mcp_server "
        "installed from the locked release environment.",
    )


def _require_project_distribution_version() -> None:
    """Refuse to freeze stale editable-install metadata into the backend."""

    expected = _release_version()
    try:
        observed = importlib.metadata.version("lantai")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            "Lantai distribution metadata is missing from the build environment. "
            "Run `uv sync --locked --extra dev --reinstall-package lantai`."
        ) from exc
    if observed != expected:
        raise RuntimeError(
            "Lantai build-environment metadata is stale: "
            f"installed={observed}, release={expected}. Run "
            "`uv sync --locked --extra dev --reinstall-package lantai` before "
            "packaging."
        )


def _backend_smoke_timeout_seconds() -> float:
    raw = os.environ.get("LANTAI_BACKEND_SMOKE_TIMEOUT_SECONDS", "120")
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 30.0


def _tail_text(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    return text[-limit:]


def _smoke_test_backend(executable: Path) -> None:
    if os.environ.get("LANTAI_SKIP_BACKEND_SMOKE", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        print("Skipping backend smoke test (LANTAI_SKIP_BACKEND_SMOKE=1)")
        return

    smoke_root = PROJECT_ROOT / "build" / "backend-smoke"
    smoke_workdir = smoke_root / "workdir"
    smoke_data_dir = smoke_root / "lantai-data"
    smoke_log = smoke_root / "backend-smoke.log"
    smoke_workdir.mkdir(parents=True, exist_ok=True)
    smoke_data_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["LANTAI_BACKEND_SMOKE_IMPORT"] = "1"
    env["LANTAI_WORKING_DIR"] = str(smoke_data_dir)
    env.setdefault("LANTAI_DISABLE_GITHUB_UPDATER", "1")
    # Frozen Office imports can initialize Accelerate/OpenBLAS while the build
    # machine is still under signing and compression load. Keep this gate
    # deterministic instead of allowing an oversized native thread pool to
    # look like a hung backend. Unbuffered phase markers preserve the exact
    # import boundary if a future runtime genuinely stalls.
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    env["VECLIB_MAXIMUM_THREADS"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [str(executable)]
    print("Running backend smoke test...")
    print(" ".join(cmd))

    try:
        with smoke_log.open("w", encoding="utf-8") as log_file:
            proc = subprocess.run(
                cmd,
                cwd=smoke_workdir,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=_backend_smoke_timeout_seconds(),
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Backend smoke test timed out during frozen backend import.\n"
            f"{_tail_text(smoke_log)}",
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            "Backend smoke test failed: frozen backend import exited with "
            f"code {proc.returncode}.\n{_tail_text(smoke_log)}",
        )
    print("Backend smoke test passed.")


def _backend_console_smoke_distribution() -> str:
    raw = os.environ.get("LANTAI_BACKEND_CONSOLE_SMOKE_DISTRIBUTION", "").lower()
    if raw in {"official", "mas"}:
        return raw
    if os.environ.get("LANTAI_APP_STORE", "").lower() in {"1", "true", "yes"}:
        return "mas"
    if "app store" in PROJECT_ROOT.name.lower():
        return "mas"
    return "official"


def _smoke_test_backend_console_path(executable: Path) -> None:
    if os.environ.get("LANTAI_SKIP_BACKEND_CONSOLE_SMOKE", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        print(
            "Skipping backend console smoke test "
            "(LANTAI_SKIP_BACKEND_CONSOLE_SMOKE=1)",
        )
        return

    smoke_script = PROJECT_ROOT / "scripts" / "frozen_backend_console_smoke.py"
    distribution = _backend_console_smoke_distribution()
    cmd = [
        sys.executable,
        str(smoke_script),
        "--backend",
        str(executable),
        "--distribution",
        distribution,
        "--timeout",
        str(max(60.0, _backend_smoke_timeout_seconds())),
        "--expected-version",
        _release_version(),
        "--desktop-auth",
        "--restart-cycle",
    ]
    print("Running backend console HTTP smoke test...")
    print(" ".join(cmd))
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)
    print("Backend console HTTP smoke test passed.")


def _check_frozen_backend_version(executable: Path) -> None:
    coherence_script = PROJECT_ROOT / "scripts" / "check_release_version_coherence.py"
    cmd = [
        sys.executable,
        str(coherence_script),
        "--project-root",
        str(PROJECT_ROOT),
        "--expected-version",
        _release_version(),
        "--frozen-backend",
        str(executable),
        "--backend-timeout",
        str(max(60.0, _backend_smoke_timeout_seconds())),
    ]
    print("Checking frozen backend version coherence...")
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)


def _bundle_officecli_backend() -> None:
    helper = PROJECT_ROOT / "scripts" / "pack" / "officecli_bundle.py"
    cmd = [
        sys.executable,
        str(helper),
        str(DIST_DIR / "lantai-server"),
    ]
    if _backend_console_smoke_distribution() == "official":
        cmd.append("--required")
    print("Bundling required OfficeCLI for official desktop builds...")
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)


def _sync_console_static_assets() -> None:
    """Make PyInstaller bundle the exact frontend bytes from console/dist."""

    sync_script = PROJECT_ROOT / "scripts" / "check_console_static_fresh.py"
    cmd = [sys.executable, str(sync_script), "--sync"]
    print("Syncing embedded console static assets...")
    print(" ".join(str(x) for x in cmd))
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)


# Clean previous build
if DIST_DIR.exists():
    shutil.rmtree(DIST_DIR)

_sync_console_static_assets()

# Ensure console static files exist (PyInstaller needs them at build time)
if not CONSOLE_DIR.exists() or not (CONSOLE_DIR / "index.html").exists():
    print("ERROR: console static files not found. Run 'npm run build' first.")
    sys.exit(1)

_ensure_guanlan_runtime()
_require_project_distribution_version()

cmd = [
    sys.executable,
    "-m",
    "PyInstaller",
    str(SPEC_FILE),
    "--distpath", str(DIST_DIR),
    "--workpath", str(PROJECT_ROOT / "build" / "pyinstaller"),
    "--noconfirm",
]

print("Running PyInstaller...")
print(" ".join(str(x) for x in cmd))
subprocess.run(cmd, check=True)

backend_layout = finalize_backend_bundle_layout(DIST_DIR)
_bundle_officecli_backend()
backend_executable = backend_layout.executable
_check_frozen_backend_version(backend_executable)
_smoke_test_backend(backend_executable)
_smoke_test_backend_console_path(backend_executable)

print(f"\n✅ Python backend built: {backend_layout.app}")
