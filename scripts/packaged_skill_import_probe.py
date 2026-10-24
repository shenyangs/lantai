#!/usr/bin/env python3
"""Fail when a bundled Python Skill references an unavailable import.

This script is intentionally executed by the frozen backend during release
smoke.  It parses every Python resource below every built-in ``scripts/``
directory, then imports each absolute dependency inside that same frozen
runtime.  New Skill scripts therefore enter the release gate automatically.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _python_skill_scripts(skills_root: Path) -> list[Path]:
    return sorted(
        path for path in skills_root.glob("*/scripts/**/*.py") if path.is_file()
    )


def _literal_dynamic_import(node: ast.Call) -> str:
    function = node.func
    is_import_module = (
        isinstance(function, ast.Attribute)
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
        and function.attr == "import_module"
    )
    is_dunder_import = isinstance(function, ast.Name) and function.id == "__import__"
    if not (is_import_module or is_dunder_import) or not node.args:
        return ""
    argument = node.args[0]
    return (
        argument.value
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        else ""
    )


def _absolute_imports(script: Path) -> list[str]:
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Call) and (module := _literal_dynamic_import(node)):
            modules.add(module)
    modules.discard("__future__")
    return sorted(modules)


def discover_skill_imports(skills_root: Path) -> dict[str, set[str]]:
    skills_root = skills_root.expanduser().resolve()
    discovered: dict[str, set[str]] = {}
    for script in _python_skill_scripts(skills_root):
        relative = script.relative_to(skills_root).as_posix()
        for module in _absolute_imports(script):
            discovered.setdefault(module, set()).add(relative)
    return discovered


@contextmanager
def _script_import_path(script: Path) -> Iterator[None]:
    parent = str(script.parent)
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, parent)
    try:
        yield
    finally:
        try:
            sys.path.remove(parent)
        except ValueError:  # pragma: no cover - defensive against imported code
            pass
        sys.dont_write_bytecode = previous_dont_write_bytecode


@contextmanager
def _import_deadline(seconds: float) -> Iterator[None]:
    if seconds <= 0 or not hasattr(signal, "setitimer"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def timed_out(_signum, _frame) -> None:
        raise TimeoutError(f"import exceeded {seconds:g} seconds")

    signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def probe_skill_scripts(
    skills_root: Path,
    *,
    per_import_timeout: float = 10.0,
) -> dict[str, object]:
    skills_root = skills_root.expanduser().resolve()
    scripts = _python_skill_scripts(skills_root)
    discovered = discover_skill_imports(skills_root)
    failures: list[dict[str, str]] = []
    import_count = 0
    failed_scripts: set[str] = set()

    for script in scripts:
        relative = script.relative_to(skills_root).as_posix()
        try:
            modules = _absolute_imports(script)
        except (OSError, SyntaxError, UnicodeError) as exc:
            failed_scripts.add(relative)
            failures.append(
                {
                    "script": relative,
                    "module": "",
                    "error_type": type(exc).__name__,
                    "missing_module": "",
                    "message": str(exc),
                }
            )
            continue
        with _script_import_path(script):
            for module in modules:
                import_count += 1
                try:
                    with _import_deadline(per_import_timeout):
                        importlib.import_module(module)
                except Exception as exc:  # noqa: BLE001 - any import failure blocks release
                    failed_scripts.add(relative)
                    failures.append(
                        {
                            "script": relative,
                            "module": module,
                            "error_type": type(exc).__name__,
                            "missing_module": str(getattr(exc, "name", "") or ""),
                            "message": str(exc),
                        }
                    )

    # ``discover_skill_imports`` is also consumed by the PyInstaller spec. Keep
    # the count in the package evidence so source discovery and frozen import
    # verification cannot silently drift apart.
    unique_import_count = len(discovered)

    return {
        "schema_version": 1,
        "status": "failed" if failures else "ok",
        "skills_root": str(skills_root),
        "script_count": len(scripts),
        "import_count": import_count,
        "unique_import_count": unique_import_count,
        "passed_count": len(scripts) - len(failed_scripts),
        "failed_count": len(failed_scripts),
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-root", required=True, type=Path)
    parser.add_argument("--per-import-timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    report = probe_skill_scripts(
        args.skills_root,
        per_import_timeout=args.per_import_timeout,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
