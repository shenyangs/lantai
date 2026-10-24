const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  createCustomizationRecoveryController,
  modeFromArgv,
} = require("./customizationRecoveryController.cjs");

function fixture(argv = []) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-recovery-"));
  const restarts = [];
  const controller = createCustomizationRecoveryController({
    app: { relaunch() {}, quit() {} },
    dialog: {
      async showMessageBox() {
        return { response: 0 };
      },
    },
    MenuItem: class MenuItem {
      constructor(value) {
        Object.assign(this, value);
      }
    },
    getUserDataPath: () => root,
    argv,
    now: () => "2026-08-13T00:00:00Z",
    restartApp: () => restarts.push(true),
  });
  return { controller, root, restarts };
}

test("explicit safe-mode argv wins over stored mode", () => {
  assert.equal(modeFromArgv(["lantai", "--lantai-safe-mode"]), "safe");
  assert.equal(modeFromArgv(["lantai", "--lantai-factory-mode"]), "factory");
});

test("boot state becomes healthy only after backend and renderer are ready", () => {
  const { controller } = fixture();
  controller.beginBoot();
  assert.equal(controller.getStatus().boot.completed, false);
  controller.markBackendReady();
  assert.equal(controller.getStatus().boot.completed, false);
  controller.markRendererReady();
  assert.equal(controller.getStatus().boot.completed, true);
  assert.equal(controller.getStatus().boot.consecutive_failures, 0);
});

test("two incomplete normal boots first request LKG recovery", () => {
  const first = fixture();
  first.controller.beginBoot();

  const second = createCustomizationRecoveryController({
    app: {},
    dialog: {},
    MenuItem: class {},
    getUserDataPath: () => first.root,
    argv: [],
    now: () => "2026-08-13T00:01:00Z",
  });
  second.beginBoot();
  const third = createCustomizationRecoveryController({
    app: {},
    dialog: {},
    MenuItem: class {},
    getUserDataPath: () => first.root,
    argv: [],
    now: () => "2026-08-13T00:02:00Z",
  });

  const status = third.beginBoot();

  assert.equal(status.mode, "normal");
  assert.equal(status.boot.consecutive_failures, 2);
  assert.equal(third.buildBackendEnv().LANTAI_CUSTOMIZATION_RECOVER_LKG, "1");
});

test("an incomplete LKG recovery boot escalates to safe mode", () => {
  const first = fixture();
  first.controller.beginBoot();
  const second = createCustomizationRecoveryController({
    app: {}, dialog: {}, MenuItem: class {}, getUserDataPath: () => first.root,
    argv: [], now: () => "2026-08-13T00:01:00Z",
  });
  second.beginBoot();
  const lkg = createCustomizationRecoveryController({
    app: {}, dialog: {}, MenuItem: class {}, getUserDataPath: () => first.root,
    argv: [], now: () => "2026-08-13T00:02:00Z",
  });
  lkg.beginBoot();
  const safe = createCustomizationRecoveryController({
    app: {}, dialog: {}, MenuItem: class {}, getUserDataPath: () => first.root,
    argv: [], now: () => "2026-08-13T00:03:00Z",
  });

  const status = safe.beginBoot();

  assert.equal(status.mode, "safe");
  assert.equal(safe.buildBackendEnv().LANTAI_RECOVERY_SAFE_MODE, "1");
});

test("factory recovery writes mode before restarting", async () => {
  const { controller, restarts } = fixture();
  controller.beginBoot();

  const accepted = await controller.confirmAndRestart("factory");

  assert.equal(accepted, true);
  assert.equal(controller.readStoredMode(), "factory");
  assert.deepEqual(restarts, [true]);
  assert.equal(controller.getStatus().boot.intentional_exit, true);
});

test("safe mode disables extension runtime until the user explicitly exits", async () => {
  const { controller } = fixture();
  controller.writeMode("safe", "test");
  controller.beginBoot();

  assert.equal(controller.buildBackendEnv().LANTAI_RECOVERY_SAFE_MODE, "1");
  controller.markBackendReady();
  controller.markRendererReady();

  assert.equal(controller.readStoredMode(), "safe");
  assert.equal(
    controller.recoveryTemplateItems().some((item) => item.id === "lantai-customization-normal-mode"),
    true,
  );
  await controller.confirmAndRestart("normal");
  assert.equal(controller.readStoredMode(), "normal");
});
