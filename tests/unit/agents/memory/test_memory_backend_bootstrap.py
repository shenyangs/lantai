# -*- coding: utf-8 -*-
"""Memory backend selection must work without prior manager imports."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_RESULT_PREFIX = "MEMORY_FACTORY_RESULT="
_FACTORY_IMPORT = """
from lantai.agents.memory.base_memory_manager import (
    get_memory_manager_backend, memory_registry,
)
"""


def _run_cold(script: str, tmp_path: Path) -> dict:
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(_PROJECT_ROOT / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        LANTAI_WORKING_DIR=str(tmp_path / "working"),
        LANTAI_SECRET_DIR=str(tmp_path / "working.secret"),
        LANTAI_DISABLE_KEYRING="1",
        LANTAI_DISABLE_DOTENV="1",
    )
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payloads = [
        line.removeprefix(_RESULT_PREFIX)
        for line in result.stdout.splitlines()
        if line.startswith(_RESULT_PREFIX)
    ]
    assert len(payloads) == 1, result.stdout
    return json.loads(payloads[0])


@pytest.mark.parametrize(
    "leaf_module",
    [
        "",
        "lantai.agents.tools.shell",
        "lantai.agents.persona.user_understanding",
    ],
)
def test_builtin_backend_resolves_after_cold_leaf_import(
    leaf_module: str,
    tmp_path: Path,
) -> None:
    result = _run_cold(
        "import importlib, json\n"
        + (
            f"importlib.import_module({leaf_module!r})\n"
            if leaf_module
            else ""
        )
        + _FACTORY_IMPORT
        + "assert not memory_registry.list_registered()\n"
        + "cls = get_memory_manager_backend('remelight')\n"
        + "from lantai.agents.memory import ReMeLightMemoryManager\n"
        + "assert cls is ReMeLightMemoryManager\n"
        + "print('MEMORY_FACTORY_RESULT=' + json.dumps({"
        + "'backend': cls.__name__, "
        + "'registered': memory_registry.list_registered()}))\n",
        tmp_path,
    )
    assert result == {
        "backend": "ReMeLightMemoryManager",
        "registered": ["remelight"],
    }


@pytest.mark.parametrize("requested", ["REMELIGHT", "missing-backend"])
def test_cold_case_insensitive_and_unknown_selection_use_builtin(
    requested: str,
    tmp_path: Path,
) -> None:
    result = _run_cold(
        "import json\n"
        + _FACTORY_IMPORT
        + f"cls = get_memory_manager_backend({requested!r})\n"
        + "print('MEMORY_FACTORY_RESULT=' + json.dumps({"
        + "'backend': cls.__name__}))\n",
        tmp_path,
    )
    assert result == {"backend": "ReMeLightMemoryManager"}


@pytest.mark.parametrize("requested", ["custom", "missing-backend"])
def test_registered_backend_and_existing_fallback_keep_selection(
    requested: str,
    tmp_path: Path,
) -> None:
    result = _run_cold(
        "import json, sys\n"
        + _FACTORY_IMPORT
        + "class CustomBackend: pass\n"
        + "class LaterBackend: pass\n"
        + "memory_registry.register('custom')(CustomBackend)\n"
        + "memory_registry.register('later')(LaterBackend)\n"
        + f"cls = get_memory_manager_backend({requested!r})\n"
        + "assert cls is CustomBackend\n"
        + "print('MEMORY_FACTORY_RESULT=' + json.dumps({"
        + "'registered': memory_registry.list_registered(), "
        + "'builtin_loaded': "
        + "'lantai.agents.memory.reme_light_memory_manager'"
        + " in sys.modules}))\n",
        tmp_path,
    )
    assert result == {
        "registered": ["custom", "later"],
        "builtin_loaded": False,
    }


def test_builtin_request_does_not_fall_back_to_custom_backend(
    tmp_path: Path,
) -> None:
    result = _run_cold(
        "import json\n"
        + _FACTORY_IMPORT
        + "class CustomBackend: pass\n"
        + "memory_registry.register('custom')(CustomBackend)\n"
        + "cls = get_memory_manager_backend('remelight')\n"
        + "from lantai.agents.memory import ReMeLightMemoryManager\n"
        + "assert cls is ReMeLightMemoryManager\n"
        + "assert get_memory_manager_backend('missing-backend') "
        + "is CustomBackend\n"
        + "print('MEMORY_FACTORY_RESULT=' + json.dumps({"
        + "'backend': cls.__name__}))\n",
        tmp_path,
    )
    assert result == {"backend": "ReMeLightMemoryManager"}


def test_base_import_remains_lazy_for_persona_memory_fences(
    tmp_path: Path,
) -> None:
    result = _run_cold(
        "import json, sys\n"
        + _FACTORY_IMPORT
        + "print('MEMORY_FACTORY_RESULT=' + json.dumps({"
        + "'registered': memory_registry.list_registered(), "
        + "'builtin_loaded': "
        + "'lantai.agents.memory.reme_light_memory_manager'"
        + " in sys.modules}))\n",
        tmp_path,
    )
    assert result == {"registered": [], "builtin_loaded": False}


def test_builtin_import_failure_is_visible_without_custom_fallback(
    tmp_path: Path,
) -> None:
    result = _run_cold(
        "import builtins, json\n"
        + _FACTORY_IMPORT
        + "class CustomBackend: pass\n"
        + "memory_registry.register('custom')(CustomBackend)\n"
        + "original_import = builtins.__import__\n"
        + "def blocked_import(name, *args, **kwargs):\n"
        + "    if name == 'reme_light_memory_manager':\n"
        + "        raise ModuleNotFoundError("
        + "'simulated unavailable builtin backend')\n"
        + "    return original_import(name, *args, **kwargs)\n"
        + "builtins.__import__ = blocked_import\n"
        + "try:\n"
        + "    get_memory_manager_backend('remelight')\n"
        + "except ModuleNotFoundError as exc:\n"
        + "    assert str(exc) == 'simulated unavailable builtin backend'\n"
        + "else:\n"
        + "    raise AssertionError("
        + "'unavailable builtin backend was silently replaced')\n"
        + "print('MEMORY_FACTORY_RESULT=' + json.dumps({"
        + "'registered': memory_registry.list_registered()}))\n",
        tmp_path,
    )
    assert result == {"registered": ["custom"]}
