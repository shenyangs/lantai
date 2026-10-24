const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  BUILTIN_APP_ICONS,
  createDockIconController,
  getClassicAppIconPath,
  getDockIconPath,
} = require("./dockIconController.cjs");

test("ships the curated icon variants without duplicate ids", () => {
  assert.equal(BUILTIN_APP_ICONS.length, 8);
  assert.deepEqual(BUILTIN_APP_ICONS[0], {
    id: "brush-classic",
    name: "经典澜台",
    group: "brush",
  });
  assert.equal(
    new Set(BUILTIN_APP_ICONS.map((icon) => icon.id)).size,
    BUILTIN_APP_ICONS.length,
  );
  assert.deepEqual(
    BUILTIN_APP_ICONS.filter((icon) => icon.group === "signature").map(
      (icon) => icon.id,
    ),
    ["ink-horizon", "quiet-tide", "paper-window", "incision"],
  );
  assert.equal(
    BUILTIN_APP_ICONS.some((icon) => icon.id === "brush-neon-lime"),
    false,
  );
  for (const icon of BUILTIN_APP_ICONS) {
    assert.equal(
      fs.existsSync(
        path.resolve(__dirname, "../public/app-icons", `${icon.id}.png`),
      ),
      true,
      `missing app icon asset: ${icon.id}`,
    );
  }
});

test("falls back to the black and white default when a retired variant was selected", () => {
  const controller = createDockIconController({
    app: { getPath: () => "/users/lantai" },
    nativeImage: {},
    isDev: true,
    electronDir: "/project/console/electron",
    getResourcesPath: () => "/resources",
    fsObject: {
      existsSync: () => false,
      readFileSync: () => JSON.stringify({ selectedId: "wave-stage" }),
    },
    processObject: { platform: "darwin" },
  });

  assert.equal(controller.getState().selectedId, "default");
  assert.deepEqual(controller.getState().defaultIcon, {
    id: "default",
    name: "黑白澜台",
    group: "default",
    previewUrl: "/lantai-mark.png",
  });
});

test("packaged dock icon resolves from Contents/Resources", () => {
  assert.equal(
    getDockIconPath({
      isDev: false,
      electronDir: "/app/Contents/Resources/app.asar/electron",
      resourcesPath: "/app/Contents/Resources",
    }),
    path.join("/app/Contents/Resources", "icon.icns"),
  );
});

test("classic runtime icon resolves to the packaged PNG resource", () => {
  assert.equal(
    getClassicAppIconPath({
      isDev: false,
      electronDir: "/app/Contents/Resources/app.asar/electron",
      resourcesPath: "/app/Contents/Resources",
    }),
    path.join("/app/Contents/Resources", "lantai-mark.png"),
  );
});

test("applies a validated icon to the macOS dock", () => {
  const calls = [];
  const icon = { isEmpty: () => false };
  const controller = createDockIconController({
    app: { dock: { setIcon: (value) => calls.push(value) } },
    nativeImage: { createFromPath: () => icon },
    isDev: false,
    electronDir: "/app/electron",
    getResourcesPath: () => "/app/Contents/Resources",
    fsObject: { existsSync: () => true },
    processObject: { platform: "darwin" },
  });

  assert.deepEqual(controller.applyDockIcon(), {
    applied: true,
    reason: "applied",
    iconPath: "/app/Contents/Resources/lantai-mark.png",
  });
  assert.deepEqual(calls, [icon]);
});

test("does not set a missing or invalid dock icon", () => {
  const calls = [];
  const missingController = createDockIconController({
    app: { dock: { setIcon: (value) => calls.push(value) } },
    nativeImage: { createFromPath: () => ({ isEmpty: () => false }) },
    isDev: false,
    electronDir: "/app/electron",
    getResourcesPath: () => "/missing",
    fsObject: { existsSync: () => false },
    processObject: { platform: "darwin" },
  });
  const invalidController = createDockIconController({
    app: { dock: { setIcon: (value) => calls.push(value) } },
    nativeImage: { createFromPath: () => ({ isEmpty: () => true }) },
    isDev: false,
    electronDir: "/app/electron",
    getResourcesPath: () => "/app/Contents/Resources",
    fsObject: { existsSync: () => true },
    processObject: { platform: "darwin" },
  });

  assert.equal(missingController.applyDockIcon().reason, "icon_missing");
  assert.equal(invalidController.applyDockIcon().reason, "icon_invalid");
  assert.deepEqual(calls, []);
});

test("is a no-op outside macOS", () => {
  const controller = createDockIconController({
    app: {},
    nativeImage: {},
    isDev: true,
    electronDir: "/project/console/electron",
    getResourcesPath: () => "/resources",
    processObject: { platform: "linux" },
  });

  assert.deepEqual(controller.applyDockIcon(), {
    applied: false,
    reason: "dock_unavailable",
  });
});

for (const selectedId of ["ink-horizon", "brush-classic"]) {
  test(`persists ${selectedId} on next launch and resets to the new default`, () => {
    const temporaryRoot = fs.mkdtempSync(
      path.join(os.tmpdir(), "lantai-icon-"),
    );
    const resourcesPath = path.join(temporaryRoot, "Resources");
    const iconPath = path.join(
      resourcesPath,
      "console",
      "dist",
      "app-icons",
      `${selectedId}.png`,
    );
    const defaultIconPath = path.join(resourcesPath, "lantai-mark.png");
    fs.mkdirSync(path.dirname(iconPath), { recursive: true });
    fs.writeFileSync(iconPath, "icon");
    fs.writeFileSync(defaultIconPath, "default");
    const calls = [];
    const createController = () =>
      createDockIconController({
        app: {
          dock: { setIcon: (value) => calls.push(value) },
          getPath: () => temporaryRoot,
        },
        nativeImage: {
          createFromPath: (value) => ({
            path: value,
            isEmpty: () => false,
          }),
        },
        isDev: false,
        electronDir: "/app/electron",
        getResourcesPath: () => resourcesPath,
        processObject: { platform: "darwin", pid: 101 },
      });

    try {
      const selected = createController().selectAppIcon(selectedId);
      assert.equal(selected.ok, true);
      assert.equal(selected.selectedId, selectedId);

      const restored = createController().applyDockIcon();
      assert.equal(restored.applied, true);
      assert.equal(restored.iconPath, iconPath);
      assert.equal(calls.length, 2);

      const reset = createController().selectAppIcon("default");
      assert.equal(reset.ok, true);
      assert.equal(reset.selectedId, "default");
      assert.equal(calls.at(-1).path, defaultIconPath);
      assert.equal(createController().getState().selectedId, "default");
    } finally {
      fs.rmSync(temporaryRoot, { recursive: true, force: true });
    }
  });
}

test("rejects untrusted app icon IPC requests", async () => {
  const handlers = new Map();
  const controller = createDockIconController({
    app: {},
    nativeImage: {},
    isDev: true,
    electronDir: "/project/console/electron",
    getResourcesPath: () => "/resources",
    processObject: { platform: "linux" },
  });
  controller.installIpcHandlers({
    ipcMain: {
      removeHandler() {},
      handle: (channel, handler) => handlers.set(channel, handler),
    },
    dialog: {},
    getMainWindow: () => null,
    isTrustedIpcEvent: () => false,
  });

  const response = await handlers.get("lantai:app-icon-select")(
    {},
    {
      id: "ink-horizon",
    },
  );
  assert.deepEqual(response, {
    ok: false,
    error: "untrusted_ipc_sender",
  });
});

test("normalizes and selects an imported custom icon", async () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-icon-"));
  const sourcePath = path.join(temporaryRoot, "source.png");
  fs.writeFileSync(sourcePath, "source");
  const resizeCalls = [];
  const dockCalls = [];
  const image = {
    isEmpty: () => false,
    getSize: () => ({ width: 1200, height: 1200 }),
    resize: (options) => {
      resizeCalls.push(options);
      return {
        toPNG: () => Buffer.from("normalized"),
        toDataURL: () => "data:image/png;base64,cHJldmlldw==",
      };
    },
  };
  const controller = createDockIconController({
    app: {
      dock: { setIcon: (value) => dockCalls.push(value) },
      getPath: () => temporaryRoot,
    },
    nativeImage: { createFromPath: () => image },
    isDev: true,
    electronDir: "/project/console/electron",
    getResourcesPath: () => "/resources",
    processObject: { platform: "darwin", pid: 202 },
  });

  try {
    const state = await controller.importCustomAppIcon({
      dialog: {
        showOpenDialog: async () => ({
          canceled: false,
          filePaths: [sourcePath],
        }),
      },
      getMainWindow: () => null,
    });

    assert.equal(state.ok, true);
    assert.equal(state.selectedId, "custom");
    assert.equal(state.customIcon.available, true);
    assert.deepEqual(resizeCalls[0], {
      width: 1024,
      height: 1024,
      quality: "best",
    });
    assert.equal(dockCalls.length, 1);
    assert.equal(
      fs.readFileSync(
        path.join(temporaryRoot, "appearance", "custom-app-icon.png"),
        "utf8",
      ),
      "normalized",
    );
  } finally {
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
});
