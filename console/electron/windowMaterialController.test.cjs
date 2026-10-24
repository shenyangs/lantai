const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const {
  createWindowMaterialController,
} = require("./windowMaterialController.cjs");
function fixture({ platform = "darwin", supported = true } = {}) {
  const theme = Object.assign(new EventEmitter(), {
    prefersReducedTransparency: false,
  });
  const calls = [],
    handlers = {};
  const win = Object.assign(new EventEmitter(), {
    isDestroyed: () => false,
    getNativeWindowHandle: () => Buffer.alloc(8),
    setVibrancy: (value) => calls.push(["vibrancy", value]),
    setBackgroundColor: (value) => calls.push(["background", value]),
    webContents: Object.assign(new EventEmitter(), {
      send: (...args) => calls.push(args),
      isDestroyed: () => false,
    }),
  });
  const native = {
    setMaterial: (_handle, enabled, dark) => {
      calls.push(["native", enabled, dark]);
      return supported;
    },
  };
  const controller = createWindowMaterialController({
    ipcMain: {
      handle: (name, cb) => {
        handlers[name] = cb;
      },
    },
    nativeTheme: theme,
    getMainWindow: () => win,
    isTrustedIpcEvent: (event) => event.trusted,
    platform,
    loadNative: () => native,
  });
  controller.installIpcHandlers();
  return {
    theme,
    calls,
    win,
    invoke: (request, trusted = true) =>
      handlers["window-material:set"]({ trusted }, request),
  };
}
test("rejects untrusted senders and malformed requests before native access", () => {
  const f = fixture();
  assert.throws(
    () => f.invoke({ enabled: true, dark: false }, false),
    /trusted/,
  );
  assert.throws(() => f.invoke({ enabled: "yes", dark: false }), /Invalid/);
  assert.equal(f.calls.length, 0);
});
test("glass, dark appearance, and solid switches are reversible", () => {
  const f = fixture();
  assert.equal(f.invoke({ enabled: true, dark: true }), "native");
  assert.ok(f.calls.some((c) => c[0] === "native" && c[1] && c[2]));
  assert.equal(f.invoke({ enabled: false, dark: false }), "solid");
  assert.ok(f.calls.some((c) => c[0] === "native" && c[1] === false));
});
test("reduced transparency reacts live, restores requested glass when disabled", () => {
  const f = fixture();
  f.invoke({ enabled: true, dark: false });
  f.theme.prefersReducedTransparency = true;
  f.theme.emit("updated");
  assert.deepEqual(f.calls.at(-1), ["window-material:changed", "solid"]);
  f.theme.prefersReducedTransparency = false;
  f.theme.emit("updated");
  assert.deepEqual(f.calls.at(-1), ["window-material:changed", "native"]);
});
test("unsupported macOS falls back to vibrancy; other platforms never load native", () => {
  assert.equal(
    fixture({ supported: false }).invoke({ enabled: true, dark: false }),
    "vibrancy",
  );
  const f = fixture({ platform: "win32" });
  assert.equal(f.invoke({ enabled: true, dark: false }), "fallback");
  assert.equal(f.calls.length, 0);
});
test("closing detaches accessibility listeners", () => {
  const f = fixture();
  f.invoke({ enabled: true, dark: false });
  assert.equal(f.theme.listenerCount("updated"), 1);
  f.win.emit("closed");
  assert.equal(f.theme.listenerCount("updated"), 0);
});

test("unchanged requests and unrelated native theme events retain the material", () => {
  const f = fixture();
  f.invoke({ enabled: true, dark: false });
  f.calls.length = 0;
  assert.equal(f.invoke({ enabled: true, dark: false }), "native");
  assert.deepEqual(f.calls, []);
  f.theme.emit("updated");
  assert.deepEqual(f.calls, []);
});
