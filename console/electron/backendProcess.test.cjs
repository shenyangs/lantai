const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const packageJson = require("../package.json");
const {
  BACKEND_HOST,
  applyRuntimeDefaultEnv,
  buildBackendAuthHeaders,
  buildBackendBaseUrl,
  buildBackendLaunchPlan,
  getBackendHealthUrl,
  getBackendReadinessUrl,
  getBackendRestartDecision,
  getDesktopDistributionChannel,
  isCurrentBackendApiUrl,
  isMasDistributionPackage,
} = require("./backendProcess.cjs");

const isMasBuild = isMasDistributionPackage(packageJson);

test("electron npm scripts point to the checked-in main process entry", () => {
  const mainEntry = path.join(__dirname, "main.cjs");

  assert.equal(fs.existsSync(mainEntry), true);
  assert.match(packageJson.scripts["electron:dev"], /electron\/main\.cjs/);
  assert.match(packageJson.scripts["electron:preview"], /electron\/main\.cjs/);
});

test("smoke userData isolation is configured before the single-instance lock", () => {
  const mainEntry = path.join(__dirname, "main.cjs");
  const source = fs.readFileSync(mainEntry, "utf8");

  const userDataIndex = source.indexOf('app.setPath(\n    "userData"');
  const singleInstanceIndex = source.indexOf("app.requestSingleInstanceLock()");

  assert.notEqual(userDataIndex, -1);
  assert.notEqual(singleInstanceIndex, -1);
  assert.ok(userDataIndex < singleInstanceIndex);
});

test("backend launch plan locks production paths and distribution env", () => {
  const resourcesPath = "/Applications/Lantai.app/Contents/Resources";
  const userDataPath = "/Users/test/Library/Application Support/Lantai";
  const plan = buildBackendLaunchPlan({
    isDev: false,
    electronDir: __dirname,
    resourcesPath,
    userDataPath,
    baseEnv: { EXISTING_ENV: "kept" },
    backendPort: 19090,
    desktopToken: "desktop-token",
    desktopInstanceNonce: "desktop-nonce",
  });

  assert.equal(
    plan.executable,
    path.join(
      resourcesPath,
      "lantai-server",
      "Lantai Backend.app",
      "Contents",
      "MacOS",
      "lantai-server",
    ),
  );
  assert.equal(plan.cwd, userDataPath);
  assert.deepEqual(plan.args, [
    "app",
    "--host",
    BACKEND_HOST,
    "--port",
    "19090",
  ]);
  assert.equal(plan.env.EXISTING_ENV, "kept");
  assert.equal(plan.env.PYTHONUNBUFFERED, "1");
  assert.equal(plan.env.LANTAI_DESKTOP_TOKEN, "desktop-token");
  assert.equal(plan.env.LANTAI_DESKTOP_INSTANCE_NONCE, "desktop-nonce");
  assert.equal(plan.env.LANTAI_DESKTOP_APP, "1");
  assert.equal(plan.env.LANTAI_WORKING_DIR, path.join(userDataPath, "working"));
  assert.equal(plan.env.LANTAI_LOG_DIR, path.join(userDataPath, "logs"));
  assert.equal(
    plan.env.LANTAI_DESKTOP_LOG_PATH,
    path.join(userDataPath, "backend.log"),
  );
  assert.equal(
    plan.env.LANTAI_CONSOLE_STATIC_DIR,
    path.join(resourcesPath, "console", "dist"),
  );
  assert.equal(plan.env.LANTAI_DISABLE_KEYRING, "1");
  assert.equal(plan.env.LANTAI_SKIP_KEYRING, "1");
  assert.equal(
    plan.env.OFFICECLI_PATH,
    path.join(userDataPath, "working", "runtime-tools", "officecli"),
  );
  assert.equal(plan.env.OFFICECLI_SKIP_UPDATE, "1");
  assert.equal(
    plan.env.PATH.split(path.delimiter)[0],
    path.join(userDataPath, "working", "runtime-tools"),
  );
  assert.equal(
    plan.bundledOfficeCli,
    path.join(resourcesPath, "lantai-server", "bin", "officecli"),
  );
  assert.equal(
    plan.runtimeOfficeCli,
    path.join(userDataPath, "working", "runtime-tools", "officecli"),
  );

  if (isMasBuild) {
    assert.equal(plan.env.LANTAI_DISTRIBUTION_CHANNEL, "mas");
    assert.equal(plan.env.LANTAI_RELEASE_CHANNEL, "mas");
    assert.equal(plan.env.LANTAI_APP_STORE, "1");
    assert.equal(plan.env.LANTAI_DISABLE_GITHUB_UPDATER, "1");
    assert.equal(plan.env.LANTAI_RUNTIME_ENGINE_DEFAULT, "native");
  } else {
    assert.equal(plan.env.LANTAI_DISTRIBUTION_CHANNEL, undefined);
    assert.equal(plan.env.LANTAI_RELEASE_CHANNEL, undefined);
    assert.equal(plan.env.LANTAI_APP_STORE, undefined);
    assert.equal(plan.env.LANTAI_DISABLE_GITHUB_UPDATER, undefined);
    assert.equal(plan.env.LANTAI_RUNTIME_ENGINE_DEFAULT, "native");
  }
});

test("backend dev launch applies distribution runtime defaults", () => {
  const projectRoot = path.resolve(__dirname, "../..");
  const plan = buildBackendLaunchPlan({
    isDev: true,
    electronDir: __dirname,
    resourcesPath: "/unused/resources",
    userDataPath: "/unused/userData",
    baseEnv: { EXISTING_ENV: "kept" },
    backendPort: 19091,
    desktopToken: "desktop-token",
    desktopInstanceNonce: "desktop-nonce",
  });

  assert.equal(plan.executable, path.join(projectRoot, ".venv/bin/python"));
  assert.equal(plan.cwd, projectRoot);
  assert.deepEqual(plan.args, [
    "-m",
    "lantai",
    "app",
    "--host",
    BACKEND_HOST,
    "--port",
    "19091",
  ]);
  assert.equal(plan.env.EXISTING_ENV, "kept");
  assert.equal(plan.env.LANTAI_DESKTOP_INSTANCE_NONCE, "desktop-nonce");
  assert.equal(plan.env.LANTAI_RUNTIME_ENGINE, undefined);
  if (isMasBuild) {
    assert.equal(plan.env.LANTAI_RUNTIME_ENGINE_DEFAULT, "native");
    assert.equal(plan.env.LANTAI_DISTRIBUTION_CHANNEL, "mas");
    assert.equal(plan.env.LANTAI_RELEASE_CHANNEL, "mas");
    assert.equal(plan.env.LANTAI_APP_STORE, "1");
    assert.equal(plan.env.LANTAI_DISABLE_GITHUB_UPDATER, "1");
  } else {
    assert.equal(plan.env.LANTAI_RUNTIME_ENGINE_DEFAULT, "native");
    assert.equal(plan.env.LANTAI_DISTRIBUTION_CHANNEL, undefined);
    assert.equal(plan.env.LANTAI_RELEASE_CHANNEL, undefined);
    assert.equal(plan.env.LANTAI_APP_STORE, undefined);
    assert.equal(plan.env.LANTAI_DISABLE_GITHUB_UPDATER, undefined);
  }
  assert.equal(
    plan.env.LANTAI_CONSOLE_STATIC_DIR,
    path.join(projectRoot, "console", "dist"),
  );
});

test("MAS package detection follows electron-builder metadata", () => {
  assert.equal(
    getDesktopDistributionChannel({
      build: { appId: "com.example.appstore", mac: { target: ["dmg"] } },
    }),
    "mas",
  );
  assert.equal(
    getDesktopDistributionChannel({
      build: { appId: "com.example.desktop", mac: { target: ["dmg", "zip"] } },
    }),
    undefined,
  );
  assert.equal(
    isMasDistributionPackage({
      build: { appId: "com.example.appstore", mac: { target: ["dmg"] } },
    }),
    true,
  );
  assert.equal(
    isMasDistributionPackage({
      build: { appId: "com.example.desktop", mac: { target: ["mas"] } },
    }),
    true,
  );
  assert.equal(
    isMasDistributionPackage({
      build: { appId: "com.example.desktop", mac: { target: ["dmg", "zip"] } },
    }),
    false,
  );
});

test("backend dev launch keeps explicit runtime override", () => {
  const plan = buildBackendLaunchPlan({
    isDev: true,
    electronDir: __dirname,
    resourcesPath: "/unused/resources",
    userDataPath: "/unused/userData",
    baseEnv: { LANTAI_RUNTIME_ENGINE: "legacy" },
    backendPort: 19091,
    desktopToken: "desktop-token",
  });

  assert.equal(plan.env.LANTAI_RUNTIME_ENGINE, "legacy");
  assert.equal(plan.env.LANTAI_RUNTIME_ENGINE_DEFAULT, undefined);
});

test("backend production launch keeps explicit runtime override", () => {
  const plan = buildBackendLaunchPlan({
    isDev: false,
    electronDir: __dirname,
    resourcesPath: "/Applications/Lantai.app/Contents/Resources",
    userDataPath: "/Users/test/Library/Application Support/Lantai",
    baseEnv: { LANTAI_RUNTIME_ENGINE: "legacy" },
    backendPort: 19091,
    desktopToken: "desktop-token",
  });

  assert.equal(plan.env.LANTAI_RUNTIME_ENGINE, "legacy");
  assert.equal(plan.env.LANTAI_RUNTIME_ENGINE_DEFAULT, undefined);
});

test("runtime default helper promotes every desktop distribution", () => {
  const desktopEnv = {};
  applyRuntimeDefaultEnv(desktopEnv, {
    build: { appId: "com.example.desktop", mac: { target: ["dmg", "zip"] } },
  });
  assert.equal(desktopEnv.LANTAI_RUNTIME_ENGINE_DEFAULT, "native");

  const masEnv = {};
  applyRuntimeDefaultEnv(masEnv, {
    build: { appId: "com.example.appstore", mac: { target: ["mas"] } },
  });
  assert.equal(masEnv.LANTAI_RUNTIME_ENGINE_DEFAULT, "native");
});

test("backend helpers keep desktop auth, health and restart behavior stable", () => {
  const apiBaseUrl = buildBackendBaseUrl(19090);

  assert.equal(apiBaseUrl, "http://127.0.0.1:19090");
  assert.equal(
    getBackendHealthUrl(apiBaseUrl),
    "http://127.0.0.1:19090/health",
  );
  assert.equal(
    getBackendReadinessUrl(apiBaseUrl),
    "http://127.0.0.1:19090/api/desktop/readiness",
  );
  assert.deepEqual(buildBackendAuthHeaders("token"), {
    "X-Lantai-Desktop-Token": "token",
  });
  assert.equal(
    isCurrentBackendApiUrl("http://127.0.0.1:19090/api/settings", 19090),
    true,
  );
  assert.equal(
    isCurrentBackendApiUrl("http://127.0.0.1:8088/api/settings", 19090),
    false,
  );
  assert.deepEqual(
    getBackendRestartDecision({
      isDev: false,
      isQuitting: false,
      hasRestartTimer: false,
      restartCount: 0,
      maxRestartCount: 2,
    }),
    { shouldSchedule: true, nextRestartCount: 1 },
  );
  assert.deepEqual(
    getBackendRestartDecision({
      isDev: false,
      isQuitting: false,
      hasRestartTimer: true,
      restartCount: 0,
      maxRestartCount: 2,
    }),
    { shouldSchedule: false, reason: "already-scheduled" },
  );
  assert.deepEqual(
    getBackendRestartDecision({
      isDev: false,
      isQuitting: false,
      hasRestartTimer: false,
      restartCount: 2,
      maxRestartCount: 2,
    }),
    { shouldSchedule: false, reason: "max-restarts" },
  );
});
