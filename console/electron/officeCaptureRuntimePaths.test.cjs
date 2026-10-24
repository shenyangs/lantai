"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const consoleRoot = process.env.LANTAI_OFFICE_HARNESS_TEST_ROOT
  ? path.resolve(process.env.LANTAI_OFFICE_HARNESS_TEST_ROOT)
  : path.resolve(__dirname, "..");
const { resolveOfficeEditorRuntimePaths } = require(
  path.join(consoleRoot, "electron", "officeEditorController.cjs"),
);

async function runSheetsStartup(scriptName, platform, arch, assetPresent) {
  const scriptPath = path.join(consoleRoot, "scripts", scriptName);
  const sidecarPath = resolveOfficeEditorRuntimePaths({
    isDev: true,
    electronDir: path.join(consoleRoot, "electron"),
    resourcesPath: "/unused-packaged-resources",
    platform,
    arch,
  }).sheets.sidecarPath;
  const state = { accessPaths: [], configured: null, queued: null, created: 0 };
  const stopped = new Error("fake-only-stop-before-native-or-renderer");
  const fixture = path.resolve("/test-fixtures/populated-workbook.xlsx");
  let ready;
  const runtime = {
    configureSheetsRuntime(config) { state.configured = config; },
    registerProjectIpc() {},
    installSheetsMenu() {},
    setForcedWorkbookPath(file) { state.queued = file; },
    createSheetsWindow() { state.created += 1; throw stopped; },
  };
  const fakeFs = {
    ...fs,
    mkdirSync() {},
    writeFileSync() { throw new Error("fake harness must not write output files"); },
    accessSync(file, mode) {
      state.accessPaths.push({ file, mode });
      if (!assetPresent || file !== sidecarPath) {
        const error = new Error("missing-native-asset");
        error.code = "ENOENT";
        throw error;
      }
    },
  };
  const app = {
    setPath() {},
    whenReady() {
      return { then(callback) { ready = callback; return { catch() {} }; } };
    },
    exit() { throw new Error("unexpected-app-exit"); },
    quit() { throw new Error("unexpected-app-quit"); },
  };
  vm.runInNewContext(fs.readFileSync(scriptPath, "utf8"), {
    __dirname: path.dirname(scriptPath),
    process: {
      argv: ["fake-electron", scriptPath, "sheets", "/test-output/capture.png", fixture],
      platform, arch, resourcesPath: "/unused-packaged-resources", env: {},
    },
    require(request) {
      if (request === "electron") return { app, BrowserWindow: {}, ipcMain: { handle() {} }, Menu: {} };
      if (request === "node:fs") return fakeFs;
      if (request.endsWith("lantai.js")) return runtime;
      if (request.startsWith(".")) return require(path.resolve(path.dirname(scriptPath), request));
      return require(request);
    },
    setTimeout() { return 1; },
    clearTimeout() {},
    console: { log() {}, error() {} },
  }, { filename: scriptPath });
  assert.equal(typeof ready, "function");
  try { await ready(); } catch (error) { state.error = error; }
  return { state, sidecarPath, stopped, fixture };
}

for (const scriptName of ["capture-office-shell.cjs", "smoke-office-editor-content.cjs"]) {
  for (const [platform, arch] of [["darwin", "arm64"], ["linux", "x64"], ["win32", "x64"]]) {
    test(`${scriptName} opens queued Sheets with the production ${platform}-${arch} native asset`, async () => {
      const { state, sidecarPath, stopped, fixture } = await runSheetsStartup(scriptName, platform, arch, true);
      assert.equal(state.error, stopped, "the fake runtime stops before any native launch");
      assert.equal(state.configured.sidecarPath, sidecarPath);
      assert.deepEqual(state.accessPaths, [{ file: sidecarPath, mode: fs.constants.X_OK }]);
      assert.equal(state.created, 1);
      if (scriptName === "capture-office-shell.cjs") assert.equal(state.queued, fixture);
      else assert.match(state.queued, /compatibility-basic\.xlsx$/);
      assert.doesNotMatch(sidecarPath, /target[\\/]release/);
    });
  }
  test(`${scriptName} rejects a missing native asset before opening a window`, async () => {
    const { state, sidecarPath } = await runSheetsStartup(scriptName, "darwin", "arm64", false);
    assert.equal(state.error?.code, "ENOENT");
    assert.deepEqual(state.accessPaths, [{ file: sidecarPath, mode: fs.constants.X_OK }]);
    assert.equal(state.configured, null);
    assert.equal(state.created, 0);
  });
}
