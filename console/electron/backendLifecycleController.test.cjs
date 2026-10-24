const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  DEFAULT_BACKEND_RESTART_READINESS_TIMEOUT_MS,
  createBackendLifecycleController,
  installAppTerminationSignalHandlers,
  listDescendantProcessIds,
  listMatchingExecutableProcessIds,
  preparePackagedRuntime,
} = require("./backendLifecycleController.cjs");

test("packaged backend restarts allow enough time for a cold Python startup", () => {
  assert.equal(DEFAULT_BACKEND_RESTART_READINESS_TIMEOUT_MS, 45_000);
});

test("process termination signals enter the app's graceful quit path once", () => {
  const processObject = new EventEmitter();
  let quitRequests = 0;
  const removeHandlers = installAppTerminationSignalHandlers({
    processObject,
    requestQuit: () => {
      quitRequests += 1;
    },
  });

  processObject.emit("SIGTERM");
  processObject.emit("SIGTERM");
  processObject.emit("SIGINT");
  assert.equal(quitRequests, 1);

  removeHandlers();
  processObject.emit("SIGTERM");
  assert.equal(quitRequests, 1);
});

test("backend descendant discovery returns deepest children first", () => {
  const result = listDescendantProcessIds(100, {
    platform: "darwin",
    execFileSyncImpl: () => "100 1\n101 100\n102 101\n103 100\n",
  });

  assert.deepEqual(result, [102, 101, 103]);
});

test("backend executable discovery includes detached helpers only for the exact bundle", () => {
  const executable = "/Applications/澜台.app/Contents/MacOS/lantai-server";
  const result = listMatchingExecutableProcessIds(executable, {
    platform: "darwin",
    execFileSyncImpl: () =>
      `100 ${executable} app --port 1234\n101 ${executable} guanlan-mcp\n102 /other/lantai-server guanlan-mcp\n`,
  });

  assert.deepEqual(result, [100, 101]);
});

test("packaged runtime executes an atomic working copy of bundled OfficeCLI", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-officecli-runtime-"));
  const source = path.join(root, "bundle", "officecli");
  const destination = path.join(root, "working", "runtime-tools", "officecli");
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(source, "signed-bundle-copy");
  fs.writeFileSync(destination, "self-mutated-runtime-copy");

  preparePackagedRuntime(
    {
      runtimeWorkingDir: path.join(root, "working"),
      runtimeLogDir: path.join(root, "logs"),
      runtimeToolBin: path.dirname(destination),
      bundledOfficeCli: source,
      runtimeOfficeCli: destination,
    },
    fs,
  );

  assert.equal(fs.readFileSync(source, "utf8"), "signed-bundle-copy");
  assert.equal(fs.readFileSync(destination, "utf8"), "signed-bundle-copy");
  assert.equal(fs.statSync(destination).mode & 0o111, 0o111);
  fs.rmSync(root, { recursive: true, force: true });
});

function createFakeChildProcess({ pid = 12345, onKill } = {}) {
  const child = new EventEmitter();
  child.pid = pid;
  child.exitCode = null;
  child.signalCode = null;
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.killSignals = [];
  child.kill = (signal) => {
    child.killed = true;
    child.killSignals.push(signal);
    onKill?.(signal, child);
    return true;
  };
  return child;
}

function createController({
  fetchImpl,
  tokens = [],
  spawnImpl,
  listDescendantPids,
  listMatchingExecutablePids,
  killProcess,
  processTerminationGraceMs,
  processForceKillGraceMs,
} = {}) {
  const spawnCalls = [];
  const tokenQueue = [...tokens];
  const children = [];
  const retiredHelpers = new Set();
  const controller = createBackendLifecycleController({
    isDev: false,
    electronDir: __dirname,
    getResourcesPath: () => "/Applications/Lantai.app/Contents/Resources",
    getUserDataPath: () => "/Users/test/Library/Application Support/Lantai",
    getBaseEnv: () => ({}),
    appendBackendLog: () => {},
    consoleObject: { log: () => {}, error: () => {} },
    fsImpl: {
      mkdirSync: () => {},
    },
    spawnImpl: (executable, args, options) => {
      const child = spawnImpl ? spawnImpl(executable, args, options) : (() => {
        spawnCalls.push({ executable, args, options });
        return createFakeChildProcess();
      })();
      children.push(child);
      return child;
    },
    listProcessRecords: () => {
      const root = children.at(-1);
      const descendants = listDescendantPids?.() || [];
      const helperPids = [...new Set([...descendants, ...(listMatchingExecutablePids?.() || [])])];
      return [
        ...children.filter((child) => child.signalCode === null && child.exitCode === null)
          .map((child) => ({pid: child.pid, ppid: 1, birth: String(child.pid), owned: true, alive: true})),
        ...helperPids.filter((pid) => pid !== root?.pid && !retiredHelpers.has(pid))
          .map((pid) => ({pid, ppid: descendants.includes(pid) ?
            (descendants[descendants.indexOf(pid) + 1] || root.pid) : 1,
            birth: String(pid), owned: true, alive: true})),
      ];
    },
    killOwnedProcess: (record, signal) => {
      const child = children.find((candidate) => candidate.pid === record.pid);
      if (child) return child.kill(signal);
      killProcess?.(record.pid, signal);
      retiredHelpers.add(record.pid);
    },
    settlementIntervalMs: 1,
    fetchImpl,
    randomToken: () => tokenQueue.shift() || "extra-token",
    pickPort: async () => 19090,
    sleep: async () => {},
    readinessIntervalMs: 1,
    processTerminationGraceMs,
    processForceKillGraceMs,
  });

  return { controller, spawnCalls };
}

test("backend replacement waits until the previous process has exited", async () => {
  const children = [];
  const spawnCalls = [];
  const { controller } = createController({
    spawnImpl: (executable, args, options) => {
      spawnCalls.push({ executable, args, options });
      const child = createFakeChildProcess({ pid: 12000 + children.length });
      children.push(child);
      return child;
    },
    processTerminationGraceMs: 1000,
    processForceKillGraceMs: 1000,
  });

  await controller.start();
  const replacement = controller.start();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(spawnCalls.length, 1);
  assert.deepEqual(children[0].killSignals, ["SIGTERM"]);

  children[0].exitCode = 0;
  children[0].emit("close", 0, "SIGTERM");
  await replacement;

  assert.equal(spawnCalls.length, 2);
});

test("backend stop signals managed descendants before their parent", async () => {
  const signals = [];
  const { controller } = createController({
    spawnImpl: () =>
      createFakeChildProcess({
        pid: 12500,
        onKill: (signal, child) => {
          child.signalCode = signal;
          signals.push([child.pid, signal]);
          queueMicrotask(() => child.emit("close", null, signal));
        },
      }),
    listDescendantPids: () => [12502, 12501],
    killProcess: (pid, signal) => signals.push([pid, signal]),
    processTerminationGraceMs: 50,
    processForceKillGraceMs: 50,
  });

  await controller.start();
  await controller.stopBackendProcess();

  assert.deepEqual(signals, [
    [12502, "SIGTERM"],
    [12501, "SIGTERM"],
    [12500, "SIGTERM"],
  ]);
});

test("unexpected backend exit terminates descendants observed before reparenting", async () => {
  const signals = [];
  let child;
  const { controller } = createController({
    spawnImpl: () => {
      child = createFakeChildProcess({ pid: 12700 });
      return child;
    },
    listDescendantPids: () => [],
    listMatchingExecutablePids: () => [12700, 12702, 12701],
    killProcess: (pid, signal) => signals.push([pid, signal]),
  });

  await controller.start();
  child.signalCode = "SIGKILL";
  child.emit("exit", null, "SIGKILL");
  await controller.stopBackendProcess();
  controller.clearRestartTimer();

  assert.deepEqual(signals, [
    [12702, "SIGTERM"],
    [12701, "SIGTERM"],
  ]);
});

test("backend stop catches a helper spawned after the first process snapshot", async () => {
  const signals = [];
  let scanCount = 0;
  const { controller } = createController({
    spawnImpl: () =>
      createFakeChildProcess({
        pid: 12800,
        onKill: (signal, child) => {
          signals.push([child.pid, signal]);
          child.signalCode = signal;
          queueMicrotask(() => child.emit("exit", null, signal));
        },
      }),
    listDescendantPids: () => [],
    listMatchingExecutablePids: () => {
      scanCount += 1;
      return scanCount === 1 ? [12800] : [12800, 12801];
    },
    killProcess: (pid, signal) => signals.push([pid, signal]),
    processTerminationGraceMs: 50,
  });

  await controller.start();
  await controller.stopBackendProcess();

  assert.deepEqual(signals, [
    [12800, "SIGTERM"],
    [12801, "SIGTERM"],
  ]);
});

test("backend replacement force-kills a process that ignores SIGTERM", async () => {
  const children = [];
  const { controller } = createController({
    spawnImpl: () => {
      const child = createFakeChildProcess({
        pid: 13000 + children.length,
        onKill: (signal, currentChild) => {
          if (signal === "SIGKILL") {
            currentChild.signalCode = "SIGKILL";
            queueMicrotask(() => currentChild.emit("close", null, "SIGKILL"));
          }
        },
      });
      children.push(child);
      return child;
    },
    processTerminationGraceMs: 1,
    processForceKillGraceMs: 50,
  });

  await controller.start();
  await controller.start();

  assert.deepEqual(children[0].killSignals, ["SIGTERM", "SIGKILL"]);
  assert.equal(children.length, 2);
});

test("backend exit schedules restart even when stdio keeps close from firing", async () => {
  const children = [];
  const tokenQueue = [
    "initial-token",
    "initial-nonce",
    "restarted-token",
    "restarted-nonce",
  ];
  let nextPort = 19090;
  const controller = createBackendLifecycleController({
    isDev: false,
    electronDir: __dirname,
    getResourcesPath: () => "/Applications/Lantai.app/Contents/Resources",
    getUserDataPath: () => "/Users/test/Library/Application Support/Lantai",
    getBaseEnv: () => ({}),
    appendBackendLog: () => {},
    consoleObject: { log: () => {}, error: () => {} },
    fsImpl: { mkdirSync: () => {} },
    listProcessRecords: () => [],
    settlementIntervalMs: 1,
    spawnImpl: () => {
      const child = createFakeChildProcess({
        pid: 15000 + children.length,
      });
      children.push(child);
      return child;
    },
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({
        status: "ok",
        desktop: true,
        instance_nonce: "restarted-nonce",
        pid: children[1].pid,
      }),
    }),
    randomToken: () => tokenQueue.shift() || "unexpected-token",
    pickPort: async () => nextPort++,
    sleep: async () => {},
    readinessIntervalMs: 1,
    restartDelayMs: 0,
    restartReadinessTimeoutMs: 10,
  });

  await controller.start();
  children[0].signalCode = "SIGKILL";
  children[0].emit("exit", null, "SIGKILL");
  await new Promise((resolve) => setTimeout(resolve, 30));

  assert.equal(children.length, 2);
  assert.equal(controller.getState().backendPid, children[1].pid);

  children[0].emit("close", null, "SIGKILL");
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(children.length, 2);
});

test("backend readiness rejects a fake health response without instance proof", async () => {
  const fetchCalls = [];
  const { controller } = createController({
    tokens: ["desktop-token", "desktop-nonce"],
    fetchImpl: async (url, options) => {
      fetchCalls.push({ url, options });
      return {
        ok: true,
        json: async () => ({
          status: "ok",
          name: "fake-service",
        }),
      };
    },
  });

  await controller.start();
  const ready = await controller.waitForReady(5);

  assert.equal(ready, false);
  assert.equal(fetchCalls.length > 0, true);
  assert.equal(
    fetchCalls[0].url,
    "http://127.0.0.1:19090/api/desktop/readiness",
  );
  assert.deepEqual(fetchCalls[0].options.headers, {
    "X-Lantai-Desktop-Token": "desktop-token",
  });
});

test("backend readiness accepts only the matching desktop instance proof", async () => {
  const fetchCalls = [];
  const { controller, spawnCalls } = createController({
    tokens: ["desktop-token", "desktop-nonce"],
    fetchImpl: async (url, options) => {
      fetchCalls.push({ url, options });
      return {
        ok: true,
        json: async () => ({
          status: "ok",
          desktop: true,
          instance_nonce: "desktop-nonce",
          pid: 12345,
        }),
      };
    },
  });

  await controller.start();
  const ready = await controller.waitForReady(5);

  assert.equal(ready, true);
  assert.equal(
    fetchCalls[0].url,
    "http://127.0.0.1:19090/api/desktop/readiness",
  );
  assert.equal(
    spawnCalls[0].options.env.LANTAI_DESKTOP_INSTANCE_NONCE,
    "desktop-nonce",
  );
});

test("backend readiness observer receives lifecycle state after proof", async () => {
  const readyStates = [];
  const spawnCalls = [];
  const tokenQueue = ["desktop-token", "desktop-nonce"];
  const controller = createBackendLifecycleController({
    isDev: false,
    electronDir: __dirname,
    getResourcesPath: () => "/Applications/Lantai.app/Contents/Resources",
    getUserDataPath: () => "/Users/test/Library/Application Support/Lantai",
    getBaseEnv: () => ({}),
    appendBackendLog: () => {},
    consoleObject: { log: () => {}, error: () => {} },
    fsImpl: {
      mkdirSync: () => {},
    },
    spawnImpl: (executable, args, options) => {
      spawnCalls.push({ executable, args, options });
      return createFakeChildProcess();
    },
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({
        status: "ok",
        desktop: true,
        instance_nonce: "desktop-nonce",
        pid: 12345,
      }),
    }),
    randomToken: () => tokenQueue.shift() || "extra-token",
    pickPort: async () => 19090,
    sleep: async () => {},
    readinessIntervalMs: 1,
    onBackendReady: (state) => readyStates.push(state),
  });

  await controller.start();
  const ready = await controller.waitForReady(5);

  assert.equal(ready, true);
  assert.equal(spawnCalls.length, 1);
  assert.deepEqual(readyStates, [
    {
      apiBaseUrl: "http://127.0.0.1:19090",
      backendPort: 19090,
      backendPid: 12345,
      backendRestartCount: 0,
      desktopToken: "desktop-token",
      desktopInstanceNonce: "desktop-nonce",
      hasBackendProcess: true,
      hasRestartTimer: false,
    },
  ]);
});

test("initial readiness timeout preserves a live cold backend and reloads when ready", async () => {
  const children = [];
  const spawnCalls = [];
  const readyStates = [];
  const reloadedUrls = [];
  const tokenQueue = ["initial-token", "initial-nonce"];
  let nextPort = 19090;
  let backendReady = false;
  const controller = createBackendLifecycleController({
    isDev: false,
    electronDir: __dirname,
    getResourcesPath: () => "/Applications/Lantai.app/Contents/Resources",
    getUserDataPath: () => "/Users/test/Library/Application Support/Lantai",
    getBaseEnv: () => ({}),
    appendBackendLog: () => {},
    consoleObject: { log: () => {}, error: () => {} },
    fsImpl: { mkdirSync: () => {} },
    spawnImpl: (executable, args, options) => {
      const child = createFakeChildProcess({
        pid: 14000 + children.length,
        onKill: (signal, currentChild) => {
          currentChild.signalCode = signal;
          queueMicrotask(() => currentChild.emit("close", null, signal));
        },
      });
      children.push(child);
      spawnCalls.push({ executable, args, options });
      return child;
    },
    fetchImpl: async () => {
      if (!backendReady) {
        throw new Error("initial backend is still starting");
      }
      return {
        ok: true,
        json: async () => ({
          status: "ok",
          desktop: true,
          instance_nonce: "initial-nonce",
          pid: children[0].pid,
        }),
      };
    },
    randomToken: () => tokenQueue.shift() || "unexpected-token",
    pickPort: async () => nextPort++,
    sleep: () => new Promise((resolve) => setTimeout(resolve, 1)),
    readinessIntervalMs: 1,
    restartDelayMs: 0,
    restartReadinessTimeoutMs: 10,
    slowReadinessTimeoutMs: 100,
    onBackendReady: (state) => readyStates.push(state),
    reloadWindowAfterBackendRestart: (url) => reloadedUrls.push(url),
  });

  await controller.start();
  assert.equal(await controller.waitForReady(2), false);

  const recovery = controller.handleReadinessTimeout(
    "initial backend readiness timed out",
  );
  await new Promise((resolve) => setTimeout(resolve, 5));

  assert.equal(spawnCalls.length, 1);
  assert.deepEqual(children[0].killSignals, []);

  backendReady = true;
  assert.equal(await recovery, true);

  assert.equal(spawnCalls.length, 1);
  assert.deepEqual(children[0].killSignals, []);
  assert.deepEqual(reloadedUrls, ["http://127.0.0.1:19090"]);
  assert.deepEqual(readyStates, [
    {
      apiBaseUrl: "http://127.0.0.1:19090",
      backendPort: 19090,
      backendPid: children[0].pid,
      backendRestartCount: 0,
      desktopToken: "initial-token",
      desktopInstanceNonce: "initial-nonce",
      hasBackendProcess: true,
      hasRestartTimer: false,
    },
  ]);
});
