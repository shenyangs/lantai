"use strict";
const assert = require("node:assert/strict");
const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { createBackendLifecycleController } = require("./backendLifecycleController.cjs");

const python = process.env.LANTAI_PROCESS_TEST_PYTHON;
const sourceRoot = path.resolve(__dirname, "../..", "src");
const probePath = [sourceRoot, process.env.LANTAI_PROCESS_TEST_DEPENDENCIES]
  .filter(Boolean).join(path.delimiter);
const OWNER = "LANTAI_DESKTOP_INSTANCE_NONCE";
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(predicate, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await delay(10);
  }
  throw new Error("owned test process did not reach its barrier");
}

function probe(env, nonce, signals = []) {
  const isolated = {...env, LANTAI_METADATA_ONLY: "1"};
  delete isolated[OWNER];
  return JSON.parse(execFileSync(python, ["-m", "lantai", "--desktop-process-probe"], {
    env: isolated, input: JSON.stringify({nonce, signals}), encoding: "utf8",
    timeout: 5000, stdio: ["pipe", "pipe", "pipe"],
  })).records;
}

async function processFixture(t, crash) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-owner-test-"));
  const receipt = path.join(directory, "receipt.json");
  const ready = path.join(directory, "helper-ready");
  const env = {...process.env, PYTHONDONTWRITEBYTECODE: "1", LANTAI_SKIP_KEYRING: "1",
    LANTAI_METADATA_ONLY: "1", PYTHONPATH: probePath,
    LANTAI_WORKING_DIR: path.join(directory, "profile"),
    LANTAI_SECRET_DIR: path.join(directory, "profile.secret")};
  delete env[OWNER];
  const children = [];
  const helperCode = "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(" +
    JSON.stringify(ready) + ").write_text('ready'); time.sleep(60)";
  const rootCode = [
    "import os,sys,time,json,subprocess,pathlib",
    "from lantai.security.tool_execution_context import build_sandboxed_tool_env",
    `child=subprocess.Popen([sys.executable,'-c',${JSON.stringify(helperCode)}],env=build_sandboxed_tool_env(),start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)`,
    `while not pathlib.Path(${JSON.stringify(ready)}).exists(): time.sleep(.01)`,
    `pathlib.Path(${JSON.stringify(receipt)}).write_text(json.dumps({'root':os.getpid(),'helper':child.pid}))`,
    crash ? "os._exit(23)" : "time.sleep(60)",
  ].join("\n");
  const controller = createBackendLifecycleController({
    isDev: true, electronDir: __dirname, getResourcesPath: () => directory,
    getUserDataPath: () => directory, getBaseEnv: () => env,
    appendBackendLog() {}, consoleObject: {log() {}, error() {}},
    pickPort: async () => 19090, isQuitting: () => true,
    processTerminationGraceMs: 80, processForceKillGraceMs: 1000,
    settlementIntervalMs: 10,
    buildLaunchPlan({desktopInstanceNonce, desktopToken}) {
      return {executable: python, args: ["-c", rootCode], cwd: directory,
        env: {...env, [OWNER]: desktopInstanceNonce, LANTAI_DESKTOP_TOKEN: desktopToken}};
    },
    spawnImpl(executable, args, options) {
      const child = spawn(executable, args, options);
      children.push(child);
      return child;
    },
  });
  let identities;
  let nonce;
  t.after(async () => {
    controller.clearRestartTimer();
    // Only the identities created by this fixture are eligible for cleanup.
    if (nonce) {
      const owned = probe(env, nonce).filter((record) => record.alive && record.owned &&
        (record.pid === identities?.root || record.pid === identities?.helper ||
          children.some((child) => child.pid === record.pid)));
      if (owned.length) probe(env, nonce, owned.map((record) => ({...record, signal: "SIGKILL"})));
    }
    for (const child of children) {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    }
    await until(() => children.every((child) => child.exitCode !== null || child.signalCode !== null));
    fs.rmSync(directory, {recursive: true, force: true});
  });
  await controller.start();
  nonce = controller.getState().desktopInstanceNonce;
  await until(() => fs.existsSync(receipt));
  identities = JSON.parse(fs.readFileSync(receipt, "utf8"));
  return {controller, env, identities, nonce, children};
}

test("real stop reaps a detached TERM-ignoring tool and preserves a foreign same-executable instance", {
  skip: !python && "set LANTAI_PROCESS_TEST_PYTHON to run native process settlement",
}, async (t) => {
  const f = await processFixture(t, false);
  const foreign = spawn(python, ["-c", "import time; time.sleep(60)", `${OWNER}=${f.nonce}`], {
    env: {...f.env, [OWNER]: "fedcba9876543210".repeat(4)}, stdio: "ignore",
  });
  f.children.push(foreign);
  const before = probe(f.env, f.nonce);
  assert.equal(before.find((record) => record.pid === f.identities.helper)?.owned, true);
  assert.equal(before.find((record) => record.pid === foreign.pid)?.owned, false);
  await f.controller.stopBackendProcess();
  const after = probe(f.env, f.nonce);
  assert.equal(after.some((record) => record.alive && [f.identities.root, f.identities.helper].includes(record.pid)), false);
  assert.equal(foreign.exitCode, null);
  assert.equal(foreign.signalCode, null);
  assert.equal(f.controller.getState().hasBackendProcess, false);
});

test("real crash cleanup finds the reparented tool after its root has already exited", {
  skip: !python && "set LANTAI_PROCESS_TEST_PYTHON to run native process settlement",
}, async (t) => {
  const f = await processFixture(t, true);
  await f.controller.stopBackendProcess();
  assert.equal(f.children[0].exitCode, 23);
  assert.equal(probe(f.env, f.nonce).some((record) => record.pid === f.identities.helper && record.alive), false);
  assert.equal(f.controller.getState().hasBackendProcess, false);
});
