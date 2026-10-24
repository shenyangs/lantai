const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function quitFixture(stop) {
  const source = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
  const start = source.indexOf('app.on("before-quit",');
  const end = source.indexOf('process.on("uncaughtException",', start);
  const app = new EventEmitter();
  let quits = 0;
  let disposals = 0;
  let notices = 0;
  app.quit = () => { quits += 1; };
  const context = vm.createContext({
    app, backendLifecycle: {clearRestartTimer() {}, stopBackendProcess: stop},
    customizationRecoveryController: null, getMainWindow: () => null,
    windowController: null, browserWorkbenchController: null, macUseController: null,
    officeWorkbenchController: {dispose() { disposals += 1; }},
    systemBehaviorController: null, workspaceGrantBookmarkStore: null,
    appendBackendLog() {}, console: {error() {}},
    dialog: {showErrorBox() { notices += 1; }},
  });
  vm.runInContext(`let isQuitting = false; let quitCleanupStarted = false; let quitCleanupFinished = false;\n${source.slice(start, end)}`, context);
  return { app, get quits() { return quits; }, get disposals() { return disposals; },
    get notices() { return notices; } };
}

test("the real before-quit handler cannot quit after unsettled backend cleanup", async () => {
  const f = quitFixture(async () => { throw new Error("backend ownership unsettled"); });
  f.app.emit("before-quit", {preventDefault() {}});
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(f.quits, 0);
  assert.equal(f.disposals, 0);
  assert.equal(f.notices, 1);
});

test("a rejected quit cleanup can retry and finishes only after confirmed settlement", async () => {
  let attempts = 0;
  const f = quitFixture(async () => {
    attempts += 1;
    if (attempts === 1) throw new Error("temporary inspection failure");
  });
  f.app.emit("before-quit", {preventDefault() {}});
  await new Promise((resolve) => setImmediate(resolve));
  f.app.emit("before-quit", {preventDefault() {}});
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(attempts, 2);
  assert.equal(f.quits, 1);
  assert.equal(f.disposals, 1);
});
