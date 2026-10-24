#!/usr/bin/env python3
"""Verify data-driven Python imports inside the frozen Lantai backend.

Normal imports are visible to PyInstaller.  Lazy CLI commands, built-in channel
registries, and literal ``importlib`` calls are not.  This probe discovers those
surfaces from source and imports them inside the packaged interpreter so a
missing module blocks release instead of failing on a user's first invocation.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def _add(
    discovered: dict[str, set[str]],
    module: str,
    origin: str,
) -> None:
    normalized = str(module or "").strip()
    if normalized and not normalized.startswith("."):
        discovered.setdefault(normalized, set()).add(origin)


def _literal_dynamic_import(node: ast.Call) -> str:
    function = node.func
    is_import_module = (
        isinstance(function, ast.Attribute)
        and function.attr == "import_module"
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
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


def _assignment_name(node: ast.Assign | ast.AnnAssign) -> str:
    if isinstance(node, ast.AnnAssign):
        return node.target.id if isinstance(node.target, ast.Name) else ""
    if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return ""


def _assignment_value(node: ast.Assign | ast.AnnAssign) -> ast.expr | None:
    return node.value


def _mapping(value: ast.AST | None) -> dict[Any, Any]:
    if value is None:
        return {}
    try:
        candidate = ast.literal_eval(value)
    except (TypeError, ValueError):
        return {}
    return candidate if isinstance(candidate, dict) else {}


def _package_for(relative: Path) -> str:
    return ".".join(relative.with_suffix("").parts[:-1])


def discover_lazy_cli_commands(source_root: Path) -> dict[str, str]:
    source_root = source_root.expanduser().resolve()
    cli_source = source_root / "cli" / "main.py"
    if not cli_source.is_file():
        return {}
    tree = ast.parse(
        cli_source.read_text(encoding="utf-8"),
        filename=str(cli_source),
    )
    commands: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            for keyword in decorator.keywords:
                if keyword.arg != "lazy_subcommands":
                    continue
                for command, value in _mapping(keyword.value).items():
                    if isinstance(value, (list, tuple)) and value:
                        commands[str(command)] = str(value[0])
    return dict(sorted(commands.items()))


def discover_runtime_imports(source_root: Path) -> dict[str, set[str]]:
    source_root = source_root.expanduser().resolve()
    package_parent = source_root.parent
    discovered: dict[str, set[str]] = {}

    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(package_parent)
        origin_path = relative.as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                _add(
                    discovered,
                    _literal_dynamic_import(node),
                    f"{origin_path}:literal-dynamic-import",
                )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    for keyword in decorator.keywords:
                        if keyword.arg != "lazy_subcommands":
                            continue
                        for value in _mapping(keyword.value).values():
                            if isinstance(value, (list, tuple)) and value:
                                _add(
                                    discovered,
                                    str(value[0]),
                                    f"{origin_path}:lazy-cli",
                                )
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if _assignment_name(node) != "_BUILTIN_SPECS":
                continue
            package = _package_for(relative)
            for value in _mapping(_assignment_value(node)).values():
                if not isinstance(value, (list, tuple)) or not value:
                    continue
                module = str(value[0])
                resolved = f"{package}{module}" if module.startswith(".") else module
                _add(
                    discovered,
                    resolved,
                    f"{origin_path}:built-in-channel",
                )
    return discovered


@contextmanager
def _import_deadline(seconds: float) -> Iterator[None]:
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    if seconds <= 0 or not hasattr(signal, "setitimer"):
        try:
            yield
        finally:
            sys.dont_write_bytecode = previous_dont_write_bytecode
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
        sys.dont_write_bytecode = previous_dont_write_bytecode


def probe_runtime_imports(
    source_root: Path,
    *,
    per_import_timeout: float = 10.0,
) -> dict[str, object]:
    discovered = discover_runtime_imports(source_root)
    lazy_cli_commands = discover_lazy_cli_commands(source_root)
    failures: list[dict[str, object]] = []
    for module, origins in sorted(discovered.items()):
        if os.getenv("LANTAI_PACKAGED_IMPORT_PROBE_PROGRESS") == "1":
            print(f"probing {module}", file=sys.stderr, flush=True)
        try:
            with _import_deadline(per_import_timeout):
                importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - any import-time failure blocks release
            failures.append(
                {
                    "module": module,
                    "origins": sorted(origins),
                    "error_type": type(exc).__name__,
                    "missing_module": str(getattr(exc, "name", "") or ""),
                    "message": str(exc),
                }
            )
        if os.getenv("LANTAI_PACKAGED_IMPORT_PROBE_PROGRESS") == "1":
            print(f"probed {module}", file=sys.stderr, flush=True)
    return {
        "schema_version": 1,
        "status": "failed" if failures else "ok",
        "source_root": str(source_root.expanduser().resolve()),
        "module_count": len(discovered),
        "passed_count": len(discovered) - len(failures),
        "failed_count": len(failures),
        "lazy_cli_commands": lazy_cli_commands,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--per-import-timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    report = probe_runtime_imports(
        args.source_root,
        per_import_timeout=args.per_import_timeout,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
