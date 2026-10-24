# -*- coding: utf-8 -*-
"""Finite own-Python stand-in for external health execution; never run archive
bytes."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace


def _record(path, payload):
    temporary = Path(str(path) + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


# pylint: disable=R0915
def offline_health_worker(payload, queue):
    import lantai.local_models.llamacpp as module
    from tests.unit.local_models.test_source_install_regressions import (
        _StagedHealthCommandDouble,
    )

    root = Path(payload["fixture_root"])
    mode = payload["fixture_mode"]
    token = payload["fixture_token"]
    data = payload["fixture_archive"]
    fields = payload["trusted_asset"]
    if (
        hashlib.sha256(data).hexdigest() != fields["sha256"]
        or len(data) != fields["size_bytes"]
    ):
        raise AssertionError(
            "own-worker fixture archive lost its independent pin",
        )
    _record(
        root / "worker.json",
        {"pid": os.getpid(), "pgid": os.getpgid(0), "token": token},
    )
    if mode == "before_target":
        while not (root / "allow-target").exists():
            time.sleep(0.01)

    class Response:
        headers = {"Content-Length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            for offset in range(0, len(data), chunk_size):
                yield data[offset : offset + chunk_size]

    class Client:
        def __init__(self, **kwargs):
            if kwargs.get("trust_env") is not False:
                raise AssertionError(
                    (
                        "offline health worker must disable ambient HTTP conf"
                        "iguration"
                    ),
                )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def stream(self, method, url, **kwargs):
            del kwargs
            if method != "GET" or url != fields["url"]:
                raise AssertionError(
                    "offline health worker rejects any other HTTP request",
                )
            return Response()

    def denied_network(*args, **kwargs):
        raise AssertionError(
            "real network is forbidden in own-health-worker fixture",
        )

    substitute = _StagedHealthCommandDouble()
    substitute.register_license_asset(module.TrustedLlamaCppAsset(**fields))
    substitute.register(
        SimpleNamespace(task=SimpleNamespace(payload=payload)),
        data,
    )
    module.httpx.Client = Client
    module.httpx.AsyncClient = denied_network
    module.reviewed_license_manifest = substitute.reviewed_license_manifest

    def own_python_command(command, **kwargs):
        # Run the full finite external-command precondition checks first.
        result = substitute(command, **kwargs)
        option = list(command)[1]
        marker = root / (
            "child.json" if option == "--version" else "license-child.json"
        )
        duration = "0.08" if mode == "normal" else "30"
        # worker_exit deliberately bypasses cleanup; the public controller owns
        # settlement.
        # pylint: disable=consider-using-with
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("own_health_child.py")),
                str(marker),
                token,
                duration,
                "ignore" if mode == "ignore_term" else "normal",
            ],
            cwd=str(root),
            env={
                "PATH": os.defpath,
                "LC_ALL": "C",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        # pylint: enable=consider-using-with
        deadline = time.monotonic() + 4
        while (
            not marker.exists()
            and child.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not marker.exists():
            child.kill()
            child.wait(timeout=2)
            raise AssertionError(
                "own Python health child failed to report its identity",
            )
        _record(
            root / "health-pending.json",
            {
                "option": option,
                "token": token,
                "child_pid": child.pid,
                "worker_pid": os.getpid(),
            },
        )
        if mode == "worker_exit":
            observed = root / "worker-exit-observed.ready"
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline:
                try:
                    if observed.read_text() == token:
                        break
                except FileNotFoundError:
                    pass
                time.sleep(0.01)
            else:
                raise AssertionError(
                    "health child was not witnessed before worker exit",
                )
            os._exit(83)
        try:
            returncode = child.wait(timeout=kwargs["timeout"])
        except BaseException:
            # This stand-in behaves like subprocess.run on timeout. Killing the
            # supervisor prevents this finally path from running,
            # the reported bug.
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            raise
        if returncode != 0:
            raise AssertionError(
                "own Python health stand-in exited unsuccessfully",
            )
        return result

    module.run_command = own_python_command
    getattr(module.LlamaCppBackend, "_download_worker")(payload, queue)


# pylint: enable=R0915
