# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import (
    collect_all,
    collect_submodules,
)
import importlib.util
import json
import os
import glob
import sys
from pathlib import Path

project_root = Path.cwd()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from scripts.check_pyinstaller_spec_policy import resolve_pyinstaller_policy
from scripts.packaged_runtime_import_probe import discover_runtime_imports
from scripts.packaged_skill_import_probe import discover_skill_imports
from scripts.pyinstaller_project_metadata import project_distribution_data

release_version = json.loads(
    (project_root / "console" / "package.json").read_text(encoding="utf-8")
).get("version", "0.0.0")

pyinstaller_policy = resolve_pyinstaller_policy(project_root, os.environ)
print(
    "PyInstaller backend policy: "
    f"distribution={pyinstaller_policy.distribution}, "
    f"target_arch={pyinstaller_policy.target_arch}, "
    f"entitlements={pyinstaller_policy.entitlements_file}, "
    f"codesign_identity={pyinstaller_policy.codesign_identity}"
)

src_path = str(project_root / "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

hiddenimports = [
    "lantai.app._app",
    "lantai.cli.main",
    "lantai.cli.app_cmd",
    "lantai.cli.acp_server_cmd",
    "lantai.integrations.acp_server.server",
    "uvicorn",
    "fastapi",
    "pydantic",
    "click",
    "chromadb.telemetry.product.posthog",
    "chromadb.telemetry.product.segment",
    "chromadb.api.rust",
    "lantai.providers.openai_chat_model_compat",
    "lantai.providers.openai_provider",
    "lantai.providers.provider_manager",
    "lantai.app.channels.console",
    "lantai.app.channels.console.channel",
]

datas = [
    ("src/lantai/agents/md_files", "lantai/agents/md_files"),
    ("src/lantai/tokenizer", "lantai/tokenizer"),
    (
        "src/lantai/security/tool_guard/rules",
        "lantai/security/tool_guard/rules",
    ),
    (
        "src/lantai/security/skill_scanner/rules",
        "lantai/security/skill_scanner/rules",
    ),
    (
        "src/lantai/security/skill_scanner/data",
        "lantai/security/skill_scanner/data",
    ),
    ("src/lantai/customization/data", "lantai/customization/data"),
    ("src/lantai/app/reading/data", "lantai/app/reading/data"),
    ("src/lantai/research/data", "lantai/research/data"),
    ("src/lantai/embedding_models", "lantai/embedding_models"),
    ("src/lantai/presentation/pptxgenjs_v3.mjs", "lantai/presentation"),
    ("src/lantai/presentation/pptxgenjs_security.cjs", "lantai/presentation"),
    (
        "src/lantai/presentation/style_profiles",
        "lantai/presentation/style_profiles",
    ),
]
datas.extend(
    (str(path), "lantai/presentation")
    for path in sorted(
        (project_root / "src/lantai/presentation").glob("*.mjs")
    )
)
# Source egg-info shadows installed metadata after src is added to sys.path.
# Copy the complete installed dist-info directory for the frozen version gate.
datas.extend(project_distribution_data(release_version))
binaries = []


def add_skill_data_files():
    source_root = project_root / "src" / "lantai" / "agents" / "skills"
    destination_root = Path("lantai/agents/skills")
    count = 0
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source_root)
        if (
            "__pycache__" in relative.parts
            or path.suffix.lower() == ".pyc"
            or path.name == ".DS_Store"
        ):
            continue
        datas.append((str(path), str(destination_root / relative.parent)))
        count += 1
    print(f"Collected Skill data files without runtime caches: {count}")


add_skill_data_files()


def require_package(package_name):
    if importlib.util.find_spec(package_name) is None:
        raise RuntimeError(
            f"Required bundled package '{package_name}' is missing. "
            "Run: uv sync --locked"
        )


def _runtime_submodule(module_name):
    parts = module_name.split(".")
    blocked = {"test", "tests", "testing", "_testing", "conftest"}
    return not any(
        part in blocked or part.startswith("test_") for part in parts
    )


def collect_package(package_name, *, runtime_only=False):
    package_datas, package_binaries, package_hiddenimports = collect_all(
        package_name,
        include_py_files=not runtime_only,
        filter_submodules=_runtime_submodule
        if runtime_only
        else (lambda _: True),
        exclude_datas=(
            ["**/test/**", "**/tests/**", "**/__pycache__/**"]
            if runtime_only
            else None
        ),
    )
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)
    print(
        f"Collected {package_name}: "
        f"{len(package_datas)} data, "
        f"{len(package_binaries)} binaries, "
        f"{len(package_hiddenimports)} hidden imports"
    )


# Collect package metadata for importlib.metadata
def add_dist_info(package_name):
    spec = importlib.util.find_spec(package_name)
    if spec and spec.origin:
        pkg_dir = os.path.dirname(spec.origin)
        site_dir = os.path.dirname(pkg_dir)
        for dist_dir in glob.glob(
            os.path.join(site_dir, f"{package_name}-*.dist-info")
        ):
            dest = os.path.basename(dist_dir)
            datas.append((dist_dir, dest))
            print(f"Adding dist-info: {dist_dir} -> {dest}")


# Guanlan must be bundled into the desktop backend. Users should never need
# network access or pip for the built-in Guanlan MCP module to start.
for package in (
    "acp",
    "guanlan",
    "feedparser",
    "loguru",
    "yt_dlp",
    "requests",
    "rich",
    "mcp",
    "pypi_attestations",
    "sigstore",
):
    require_package(package)
    collect_package(package)

# Office skills execute Python libraries dynamically from generated scripts,
# so PyInstaller cannot discover them from normal import analysis.
for package in (
    "pandas",
    "openpyxl",
    "pypdf",
    "pdfplumber",
    "reportlab",
    "docx",
    "pptx",
    "markitdown",
    "mammoth",
    "cobble",
    "magika",
    "pypdfium2",
    "bibtexparser",
    "rispy",
    "quickjs",
):
    require_package(package)
    collect_package(package, runtime_only=True)

# Local semantic retrieval imports these lazily.  Bundle their native runtime
# and tokenizer assets explicitly so the signed backend works fully offline.
for package in ("onnxruntime", "tokenizers"):
    require_package(package)
    collect_package(package, runtime_only=True)

for package in ("acp", "fastmcp", "guanlan", "mcp"):
    add_dist_info(package)

# OpenTelemetry's OTLP exporters import generated protobuf modules dynamically.
# PyInstaller misses namespace-package children unless we collect them
# explicitly.
for package in (
    "opentelemetry.proto",
    "opentelemetry.exporter.otlp.proto.grpc",
    "opentelemetry.exporter.otlp.proto.http",
):
    hiddenimports.extend(collect_submodules(package))

# Agent tools are registered through lantai.agents.tools.__init__ and new tool
# modules are easy to miss in frozen builds. Collect the package explicitly so
# frozen backend startup fails at build time instead of as a white page.
hiddenimports.extend(collect_submodules("lantai.agents.tools"))

# The stable presentation entry point is loaded from a bundled Skill script via
# ``runpy.run_path``. That dynamic import is invisible to PyInstaller's normal
# analysis, so collect the complete local runtime explicitly. Shipping only
# the wrapper script would make every packaged PPT task fail at first use.
hiddenimports.extend(collect_submodules("lantai.presentation"))

# Discover data-driven imports from the actual source registries and every
# bundled Python Skill. This turns future lazy CLI/channel/Skill additions into
# build inputs automatically instead of relying on a hand-maintained list.
runtime_dynamic_imports = discover_runtime_imports(
    project_root / "src" / "lantai"
)
skill_dynamic_imports = discover_skill_imports(
    project_root / "src" / "lantai" / "agents" / "skills"
)
for module_name in sorted(runtime_dynamic_imports):
    hiddenimports.append(module_name)
for module_name in sorted(skill_dynamic_imports):
    if module_name.startswith("lantai."):
        hiddenimports.append(module_name)

hiddenimports = sorted(set(hiddenimports))
datas = list(dict.fromkeys(datas))
binaries = list(dict.fromkeys(binaries))

a = Analysis(
    ["electron/entry.py"],
    pathex=[src_path],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Header/footer templates are opened via parts/../templates. Keep the
    # physical package directories as well as PYZ code for those paths.
    module_collection_mode={"docx": "pyz+py"},
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lantai-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=pyinstaller_policy.upx,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=pyinstaller_policy.target_arch,
    codesign_identity=pyinstaller_policy.codesign_identity,
    entitlements_file=pyinstaller_policy.entitlements_file,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=pyinstaller_policy.upx,
    upx_exclude=[],
    name="lantai-server",
)

backend_app = BUNDLE(
    coll,
    name="Lantai Backend.app",
    icon="console/build/icon.icns",
    bundle_identifier="com.lantai.app.backend",
    version=release_version,
    info_plist={
        "CFBundleDisplayName": "澜台",
        "CFBundleName": "澜台后台服务",
    },
)
