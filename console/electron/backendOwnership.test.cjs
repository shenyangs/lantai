"use strict";
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");
const { createBackendLifecycleController } = require("./backendLifecycleController.cjs");

// Every PID and signal in this fixture is in memory. No operating-system
// process is spawned, queried, or signalled.
function fixture({ descendants = [], matches = () => [], rootExits = true,
  signalHelper = () => {}, inspectFailure = false } = {}) {
  const children = [];
  const signals = [];
  const retiredHelpers = new Set();
  const controller = createBackendLifecycleController({
    isDev: false,
    electronDir: "/fixture/electron",
    getResourcesPath: () => "/fixture/Lantai.app/Contents/Resources",
    getUserDataPath: () => "/fixture/user-data",
    getBaseEnv: () => ({}),
    appendBackendLog: () => {},
    consoleObject: { log() {}, error() {} },
    fsImpl: { mkdirSync() {} },
    spawnImpl() {
      const child = new EventEmitter();
      child.pid = 4100 + children.length * 100;
      child.exitCode = null;
      child.signalCode = null;
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.kill = (signal) => {
        signals.push([child.pid, signal]);
        if (rootExits) {
          child.signalCode = signal;
          queueMicrotask(() => child.emit("exit", null, signal));
        }
        return true;
      };
      children.push(child);
      return child;
    },
    randomToken: () => "fixture-instance-token",
    pickPort: async () => 19090,
    sleep: async () => {},
    isQuitting: () => true,
    listProcessRecords: () => {
      if (inspectFailure) throw new Error("process inspection denied");
      const root = children.at(-1);
      return [
        ...children.filter((child) => child.signalCode === null).map((child) => ({
          pid: child.pid, ppid: 1, birth: String(child.pid), owned: true, alive: true,
        })),
        ...[...new Set([...descendants, ...matches(children)])]
          .filter((pid) => pid !== root?.pid && !retiredHelpers.has(pid))
          .map((pid) => ({pid, ppid: descendants.includes(pid) ? root.pid : 1,
            birth: String(pid), owned: descendants.includes(pid), alive: true})),
      ];
    },
    killOwnedProcess(record, signal) {
      const child = children.find((candidate) => candidate.pid === record.pid);
      if (child) return child.kill(signal);
      signals.push([record.pid, signal]);
      signalHelper(record.pid, signal);
      if (!matches(children).includes(record.pid)) retiredHelpers.add(record.pid);
    },
    settlementIntervalMs: 1,
    listDescendantPids: () => descendants,
    listMatchingExecutablePids: () => matches(children),
    killProcess(pid, signal) {
      signals.push([pid, signal]);
      signalHelper(pid, signal);
    },
    processTerminationGraceMs: 2,
    processForceKillGraceMs: 2,
  });
  return { controller, children, signals };
}

test("stopping one instance never signals a foreign instance using the same executable", async () => {
  const f = fixture({ matches: () => [4100, 4200] });
  await f.controller.start();
  await f.controller.stopBackendProcess();
  assert.deepEqual(f.signals.filter(([pid]) => pid === 4200), []);
});

test("unexpected root exit never attributes an unrelated same-executable process to this instance", async () => {
  const f = fixture({ matches: () => [4100, 4200] });
  await f.controller.start();
  f.children[0].signalCode = "SIGKILL";
  f.children[0].emit("exit", null, "SIGKILL");
  f.controller.clearRestartTimer();
  assert.deepEqual(f.signals.filter(([pid]) => pid === 4200), []);
});

test("stop does not settle while an owned descendant survives TERM", async () => {
  let helperAlive = true;
  const f = fixture({
    descendants: [4101],
    matches: () => helperAlive ? [4101] : [],
    signalHelper(pid, signal) {
      if (pid === 4101 && signal === "SIGKILL") helperAlive = false;
    },
  });
  await f.controller.start();
  await f.controller.stopBackendProcess();
  assert.equal(helperAlive, false, "owned helper must be confirmed dead before stop settles");
  assert.ok(f.signals.some(([pid, signal]) => pid === 4101 && signal === "SIGKILL"));
});

test("replacement does not spawn a second root when death of the first root remains unconfirmed", async () => {
  const f = fixture({ rootExits: false });
  await f.controller.start();
  try { await f.controller.start(); } catch (_) { /* an explicit unsettled error is acceptable */ }
  assert.equal(f.children.length, 1, "a still-live managed root must retain exclusive ownership");
  assert.equal(f.controller.getState().backendPid, 4100);
});

test("a cooperative owned helper and root are signalled in descendant-first order", async () => {
  let helperAlive = true;
  const f = fixture({
    descendants: [4101],
    matches: () => helperAlive ? [4101] : [],
    signalHelper(pid, signal) {
      if (pid === 4101 && signal === "SIGTERM") helperAlive = false;
    },
  });
  await f.controller.start();
  await f.controller.stopBackendProcess();
  assert.deepEqual(f.signals, [[4101, "SIGTERM"], [4100, "SIGTERM"]]);
});


test("inspection failure retains ownership and blocks replacement", async () => {
  const f = fixture({inspectFailure: true});
  await f.controller.start();
  await assert.rejects(f.controller.stopBackendProcess(), /inspect|settle/i);
  assert.equal(f.controller.getState().backendPid, 4100);
  await assert.rejects(f.controller.start(), /inspect|settle/i);
  assert.equal(f.children.length, 1);
});

test("failed helper kill cannot become successful settlement", async () => {
  const f = fixture({
    descendants: [4101], matches: () => [4101],
    signalHelper() { const err = new Error("permission denied"); err.code = "EPERM"; throw err; },
  });
  await f.controller.start();
  await assert.rejects(f.controller.stopBackendProcess(), /settle|permission/i);
  assert.equal(f.controller.getState().backendPid, 4100);
  await assert.rejects(f.controller.start(), /settle|permission/i);
  assert.equal(f.children.length, 1);
});
