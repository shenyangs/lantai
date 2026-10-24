# -*- coding: utf-8 -*-
"""Actual cohort AST: short process settlement must preserve identity safety.

No Process, process inventory, OS signal, App, or thread is invoked: psutil/os
are finite fake namespaces, and the actual class is constructed with __new__.
The source is always taken from this worktree.
"""
from __future__ import annotations

# These behavior locks intentionally inspect private harness operations.
# pylint: disable=protected-access,redefined-outer-name

import ast
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

DEFAULT_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "packaged_office_support.py"
)


class NoSuchProcess(Exception):
    pass


class AccessDenied(Exception):
    pass


class ProcessView:
    """Cache creation time as a psutil Process identity can do."""

    def __init__(self, world, pid):
        self.world = world
        self.pid = pid
        self.birth = world.births[pid]

    def create_time(self):
        return self.birth

    def status(self):
        return "running"

    def ppid(self):
        return self.world.parents[self.pid]

    def children(self, recursive=False):
        assert recursive is False
        if self.pid != 101:
            return []
        cached_child = ProcessView(self.world, 201)
        self.world.events.append(
            {
                "event": "capture_edge",
                "captured_birth": self.world.cohort._owners.get(201),
            },
        )
        if self.world.mode == "exited":
            del self.world.births[201]
        elif self.world.mode == "reparented_registered":
            self.world.parents[201] = 1
        elif self.world.mode == "reused":
            self.world.births[201] = 200.0
        elif self.world.mode == "denied":
            self.world.denied.add(201)
        elif self.world.mode == "unproven_live":
            self.world.parents[201] = 999
        return [cached_child]


class World:
    def __init__(self, mode):
        self.mode = mode
        self.births = {101: 100.0, 201: 101.0}
        self.parents = {101: 1, 201: 101}
        self.denied = set()
        self.events = []
        self.cohort = None

    def process(self, pid):
        if pid in self.denied:
            raise AccessDenied(pid)
        if pid not in self.births:
            raise NoSuchProcess(pid)
        return ProcessView(self, pid)

    def pids(self):
        # Only this positive case is registered through the actual SID path.
        return [201] if self.mode == "reparented_registered" else []


@pytest.fixture
def cohort_factory():
    source_path = DEFAULT_SOURCE
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OwnedProcessCohort"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            cls,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)

    def make(mode):
        world = World(mode)
        ns = {
            "threading": threading,
            "psutil": SimpleNamespace(
                Process=world.process,
                pids=world.pids,
                NoSuchProcess=NoSuchProcess,
                AccessDenied=AccessDenied,
                STATUS_ZOMBIE="zombie",
            ),
            "os": SimpleNamespace(getsid=lambda pid: 101),
        }
        exec(compile(module, str(source_path), "exec"), ns)
        cohort_cls = ns["OwnedProcessCohort"]
        cohort = cohort_cls.__new__(cohort_cls)
        cohort.process = SimpleNamespace(pid=101)
        cohort.born = 100.0
        cohort.sid = 101
        cohort._owners = {101: 100.0}
        cohort._lock = threading.RLock()
        cohort._monitor_error = None
        cohort._sid_compromised = False
        world.cohort = cohort
        return cohort, world

    return make


def test_confirmed_exited_short_child_does_not_poison_inventory(
    cohort_factory,
):
    cohort, world = cohort_factory("exited")
    snapshot = cohort._snapshot()
    assert (
        201 not in world.births
    )  # The candidate is explicitly NoSuchProcess.
    assert cohort._owners == {
        101: 100.0,
    }  # No authority over a new PID granted.
    assert snapshot["unknown"] == []
    assert cohort._monitor_error is None


def test_same_birth_sid_registered_reparent_does_not_poison_inventory(
    cohort_factory,
):
    cohort, world = cohort_factory("reparented_registered")
    snapshot = cohort._snapshot()
    assert world.events == [{"event": "capture_edge", "captured_birth": 101.0}]
    assert cohort._owners == {101: 100.0, 201: 101.0}
    assert {item["pid"] for item in snapshot["live"]} == {101, 201}
    assert snapshot["unknown"] == []
    assert cohort._monitor_error is None


@pytest.mark.parametrize("mode", ["reused", "denied", "unproven_live"])
def test_unproven_or_unreadable_live_identity_remains_sticky_block(
    cohort_factory,
    mode,
):
    cohort, world = cohort_factory(mode)
    snapshot = cohort._snapshot()
    assert world.births.get(201) is not None
    assert 201 not in cohort._owners
    assert snapshot["unknown"]
    assert cohort._monitor_error is not None
