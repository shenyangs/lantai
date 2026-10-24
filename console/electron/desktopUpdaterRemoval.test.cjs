const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const consoleRoot = path.join(__dirname, "..");

function read(relativePath) {
  return fs.readFileSync(path.join(consoleRoot, relativePath), "utf8");
}

test("desktop package has no automatic updater capability", () => {
  const pkg = JSON.parse(read("package.json"));
  const packagedFiles = pkg.build?.files || [];

  assert.equal(Object.hasOwn(pkg.dependencies || {}, "electron-updater"), false);
  assert.equal(pkg.build?.publish, null);
  assert.equal(
    packagedFiles.some((entry) => /desktopUpdate|electron-updater/.test(entry)),
    false,
  );
  assert.equal(fs.existsSync(path.join(__dirname, "desktopUpdateController.cjs")), false);
  assert.equal(fs.existsSync(path.join(__dirname, "desktopUpdateUtils.cjs")), false);
});

test("desktop runtime exposes only a read-only local version", () => {
  const main = read("electron/main.cjs");
  const ipc = read("electron/desktopIpcController.cjs");
  const preload = read("electron/preload.js");
  const rendererTypes = read("src/vite-env.d.ts");

  for (const source of [main, ipc, preload, rendererTypes]) {
    assert.doesNotMatch(source, /lantai:update-(?:status|check|install)/);
    assert.doesNotMatch(source, /checkForUpdates|installUpdate|onUpdateStatus/);
  }
  assert.doesNotMatch(main, /desktopUpdateController|scheduleChecks|clearTimers/);
  assert.match(ipc, /lantai:app-version/);
  assert.match(preload, /getAppVersion/);
  assert.match(rendererTypes, /getAppVersion/);
});

test("settings and sidebar have no updater surface", () => {
  const about = read("src/pages/Settings/About/index.tsx");
  const settingsHome = read("src/pages/Settings/Home/index.tsx");
  const sidebar = read("src/layouts/Sidebar.tsx");

  for (const source of [about, settingsHome, sidebar]) {
    assert.doesNotMatch(source, /useDesktopUpdateStatus|updateStatus|updateReady/);
    assert.doesNotMatch(source, /检查更新|桌面 App 更新|更新日志|自动静默检查更新/);
  }
  assert.match(about, /getAppVersion/);
});
