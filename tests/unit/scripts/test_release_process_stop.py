# -*- coding: utf-8 -*-
"""Real finite process locks; only processes created by this fixture are
scanned.
"""
from __future__ import annotations

import importlib.util
import os
import plistlib
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_stop():
    spec = importlib.util.spec_from_file_location(
        "release_process_stop_lock",
        ROOT / "scripts/stop_release_app_instance.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="scope")
def process_scope_fixture(tmp_path, monkeypatch):
    module = load_stop()
    code = tmp_path / "finite.c"
    code.write_text(
        r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
int main(int argc, char **argv) {
  alarm(30);
  if (argc > 1 && !strcmp(argv[1], "root")) {
    pid_t child = fork();
    if (!child) {
      setenv("LANTAI_DESKTOP_INSTANCE_NONCE", argv[3], 1);
      execl(argv[2], argv[2], "ignore", NULL);
      _exit(91);
    }
    printf("%d\n", child); fflush(stdout);
  } else if (argc > 1 && !strcmp(argv[1], "helper")) {
    int ready[2];
    if (pipe(ready)) return 93;
    pid_t child = fork();
    if (child < 0) return 94;
    if (!child) {
      close(ready[0]); alarm(30);
      if (setsid() < 0) _exit(95);
      signal(SIGTERM, SIG_IGN);
      if (write(ready[1], "r", 1) != 1) _exit(96);
      close(ready[1]);
      while (1) pause();
    }
    close(ready[1]);
    char settled;
    if (read(ready[0], &settled, 1) != 1 || settled != 'r') return 97;
    close(ready[0]);
    printf("%d\n", child); fflush(stdout);
  } else if (argc > 1 && !strcmp(argv[1], "desktop-helper")) {
    pid_t child = fork();
    if (!child) {
      unsetenv("LANTAI_DESKTOP_INSTANCE_NONCE");
      execl(argv[2], argv[2], "ignore", NULL); _exit(92);
    }
    printf("%d\n", child); fflush(stdout);
  } else {
    if (argc > 1 && !strcmp(argv[1], "ignore")) signal(SIGTERM, SIG_IGN);
    puts("ready"); fflush(stdout);
  }
  while (1) pause();
}
""",
    )
    binary = tmp_path / "finite"
    subprocess.run(
        ["/usr/bin/clang", str(code), "-o", str(binary)],
        check=True,
        capture_output=True,
        timeout=15,
    )

    class Scope:
        nonce = uuid.uuid4().hex
        births = {}
        children = []

        def app(self, name="Synthetic.app"):
            app = tmp_path / name
            root_exe = app / "Contents/MacOS/Synthetic"
            backend = (
                app
                / "Contents/Resources/lantai-server/Lantai Backend.app"
                / "Contents/MacOS/lantai-server"
            )
            for exe in (root_exe, backend):
                exe.parent.mkdir(parents=True, exist_ok=True)
                exe.write_bytes(binary.read_bytes())
                exe.chmod(0o700)
            (app / "Contents/Info.plist").write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleExecutable": "Synthetic",
                        "CFBundleIdentifier": "com.lantai.app",
                    },
                ),
            )
            return app, root_exe, backend

        def register(self, pid):
            self.births[pid] = psutil.Process(pid).create_time()

        def spawn(self, exe, *args, nonce=None):
            env = {"PATH": "/usr/bin:/bin"}
            if nonce is not None:
                env[module.probe.OWNER_ENV] = nonce
            # The explicit owner settles this process in cleanup.
            # pylint: disable-next=consider-using-with
            child = subprocess.Popen(
                [str(exe), *map(str, args)],
                env=env,
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self.children.append(child)
            self.register(child.pid)
            line = child.stdout.readline().strip()
            if line.isdigit():
                self.register(int(line))
                # Wait for fork/exec to establish the executable identity.
                deadline = time.monotonic() + 2
                if args and args[0] in ("root", "desktop-helper"):
                    while psutil.Process(int(line)).exe() != str(args[1]):
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
            else:
                assert line == "ready"
            return child, int(line) if line.isdigit() else None

        def live(self, pid):
            try:
                process = psutil.Process(pid)
                return (
                    process.create_time() == self.births[pid]
                    and process.status()
                    not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
                    and process.is_running()
                )
            except psutil.NoSuchProcess:
                return False

    current = Scope()
    monkeypatch.setattr(
        module.probe.psutil,
        "pids",
        lambda: list(current.births),
    )
    try:
        yield module, current
    finally:
        for pid in current.births:
            if current.live(pid):
                # Cleanup is outside the finite signal-denial test boundary.
                os.kill(pid, signal.SIGKILL)
        for child in current.children:
            child.wait(timeout=5)
            child.stdout.close()
        deadline = time.monotonic() + 3
        while (
            any(current.live(pid) for pid in current.births)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert not any(current.live(pid) for pid in current.births)


def test_argv_lookalike_does_not_authorize_signals(scope):
    stop, own = scope
    app, _, backend = own.app()
    target, _ = own.spawn(backend, "ignore", nonce=own.nonce)
    _other_app, _, other_backend = own.app("Other.app")
    foreign, _ = own.spawn(other_backend, str(backend), nonce=uuid.uuid4().hex)
    owner = stop.bootstrap(app)
    assert owner.had_instance
    stop.settle(owner, term_timeout=0.15, kill_timeout=1)
    target.wait(timeout=3)
    assert not own.live(target.pid)
    assert own.live(foreign.pid)


def test_detached_same_nonce_helper_must_settle(scope):
    stop, own = scope
    app, _, backend = own.app()
    parent, helper = own.spawn(backend, "helper", nonce=own.nonce)
    assert os.getpgid(parent.pid) != os.getpgid(helper)
    stop.settle(stop.bootstrap(app), term_timeout=0.15, kill_timeout=1)
    parent.wait(timeout=3)
    assert not own.live(helper)


def test_verified_nonceless_desktop_helper_must_settle(scope):
    stop, own = scope
    app, _, backend = own.app()
    helper_exe = (
        app
        / "Contents/Frameworks/Synthetic Helper.app"
        / "Contents/MacOS/Synthetic Helper"
    )
    helper_exe.parent.mkdir(parents=True)
    helper_exe.write_bytes(backend.read_bytes())
    helper_exe.chmod(0o700)
    parent, helper = own.spawn(
        backend,
        "desktop-helper",
        helper_exe,
        nonce=own.nonce,
    )
    assert module_nonce(helper, stop) is None
    stop.settle(stop.bootstrap(app), term_timeout=0.15, kill_timeout=1)
    assert not own.live(parent.pid) and not own.live(helper)


def test_root_without_nonce_and_verified_backend_both_settle(scope):
    stop, own = scope
    app, root, backend = own.app()
    parent, child = own.spawn(root, "root", backend, own.nonce)
    assert module_nonce(parent.pid, stop) is None
    stop.settle(stop.bootstrap(app), term_timeout=0.15, kill_timeout=1)
    parent.wait(timeout=3)
    assert not own.live(parent.pid)
    assert not own.live(child)


def module_nonce(pid, stop):
    return psutil.Process(pid).environ().get(stop.probe.OWNER_ENV)


def test_denied_signal_is_failure_and_owner_is_retained(scope, monkeypatch):
    stop, own = scope
    app, _, backend = own.app()
    target, _ = own.spawn(backend, "ignore", nonce=own.nonce)
    owner = stop.bootstrap(app)

    def denied(*args, **kwargs):
        raise psutil.AccessDenied(target.pid)

    monkeypatch.setattr(stop.probe, "signal_owned", denied)
    with pytest.raises(stop.ProcessStopError):
        stop.settle(owner, term_timeout=0.1, kill_timeout=0.1)
    assert own.live(target.pid)
    assert owner.nonce == own.nonce


def test_multiple_instances_are_blocked_before_any_signal(scope):
    stop, own = scope
    app, _, backend = own.app()
    a, _ = own.spawn(backend, nonce=own.nonce)
    b, _ = own.spawn(backend, nonce=uuid.uuid4().hex)
    with pytest.raises(stop.ProcessStopError):
        stop.bootstrap(app)
    assert own.live(a.pid) and own.live(b.pid)


def test_missing_nonce_is_observable_block(scope):
    stop, own = scope
    app, _, backend = own.app()
    child, _ = own.spawn(backend)
    with pytest.raises(stop.ProcessStopError):
        stop.bootstrap(app)
    assert own.live(child.pid)


def test_empty_instance_requires_two_complete_scans(scope, monkeypatch):
    stop, own = scope
    app, _, _ = own.app()
    owner = stop.bootstrap(app)
    assert not owner.had_instance
    calls = []
    original = stop.scan_app

    def observed(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(stop, "scan_app", observed)
    stop.settle(owner, term_timeout=0.1, kill_timeout=0.1)
    assert len(calls) >= 2


def test_bundle_replacement_during_stop_is_blocked(scope):
    stop, own = scope
    app, _, backend = own.app()
    child, _ = own.spawn(backend, "ignore", nonce=own.nonce)
    owner = stop.bootstrap(app)
    backend.rename(backend.with_suffix(".old"))
    backend.write_bytes(b"different inode")
    with pytest.raises(stop.ProcessStopError):
        stop.settle(owner, term_timeout=0.1, kill_timeout=0.1)
    assert own.live(child.pid)


def test_new_instance_after_bootstrap_is_blocked(scope):
    stop, own = scope
    app, _, backend = own.app()
    owner = stop.bootstrap(app)
    child, _ = own.spawn(backend, nonce=own.nonce)
    with pytest.raises(stop.ProcessStopError):
        stop.settle(owner, term_timeout=0.1, kill_timeout=0.1)
    assert own.live(child.pid)


def test_scan_access_denial_is_not_empty_success(scope, monkeypatch):
    stop, own = scope
    app, _, _ = own.app()
    owner = stop.bootstrap(app)
    monkeypatch.setattr(
        stop,
        "scan_app",
        lambda *_: (_ for _ in ()).throw(psutil.AccessDenied()),
    )
    with pytest.raises(stop.ProcessStopError):
        stop.settle(owner, term_timeout=0.1, kill_timeout=0.1)


def test_relocated_owner_stops_registered_root_without_touching_new_target(
    scope,
):
    stop, own = scope
    app, root, backend = own.app()
    parent, child = own.spawn(root, "root", backend, own.nonce)
    owner = stop.bootstrap(app)
    backup = app.with_name("old-app")
    app.rename(backup)
    new_app, _, new_backend = own.app()
    foreign, _ = own.spawn(new_backend, nonce=uuid.uuid4().hex)
    stop.settle_registered(
        owner,
        [new_app, backup],
        term_timeout=0.15,
        kill_timeout=1,
    )
    assert not own.live(parent.pid) and not own.live(child)
    assert own.live(foreign.pid)


def test_relocated_proof_settles_detached_helper_after_anchor_exits(scope):
    stop, own = scope
    app, _, backend = own.app()
    parent, child = own.spawn(backend, "helper", nonce=own.nonce)
    owner = stop.bootstrap(app)
    parent.kill()
    parent.wait(timeout=3)
    backup = app.with_name("old-app")
    app.rename(backup)
    stop.settle_registered(owner, [backup], term_timeout=0.15, kill_timeout=1)
    assert not own.live(child)


def test_relocated_foreign_bundle_cannot_authorize_registered_owner(scope):
    stop, own = scope
    app, _, backend = own.app()
    parent, _ = own.spawn(backend, nonce=own.nonce)
    owner = stop.bootstrap(app)
    other, _, _ = own.app("Unrelated.app")
    with pytest.raises(stop.ProcessStopError):
        stop.settle_registered(
            owner,
            [other],
            term_timeout=0.1,
            kill_timeout=0.1,
        )
    assert own.live(parent.pid)


def test_backend_exit_between_scan_and_nonce_read_does_not_block_settlement(
    scope,
    monkeypatch,
):
    stop, own = scope
    app, _, backend = own.app()
    child, _ = own.spawn(backend, nonce=own.nonce)
    owner = stop.bootstrap(app)
    original_scan = stop.scan_app

    def exits_after_scan(binding):
        records = original_scan(binding)
        if own.live(child.pid):
            child.kill()
            child.wait(timeout=3)
        return records

    monkeypatch.setattr(stop, "scan_app", exits_after_scan)
    stop.settle(owner, term_timeout=0.1, kill_timeout=0.2)
    assert not own.live(child.pid)
