"use strict";
// Actual controller, synthetic EventEmitter only: never loads Electron or OS APIs.
const { EventEmitter } = require("node:events");
const path = require("node:path");
const { createSystemBehaviorController } = require(path.join(
  process.env.LANTAI_AUDIT_WORKSPACE || path.resolve(__dirname, "../../.."),
  "console/electron/systemBehaviorController.cjs",
));

function makeHarness() {
  const state = {
    supported: true, focused: false, mode: "show", deliveryConfirmed: true, instances: [],
    events: [], constructorCalls: 0, showCalls: 0,
  };
  class SyntheticNotification extends EventEmitter {
    static isSupported() { return state.supported; }
    constructor(options) {
      super();
      state.constructorCalls += 1;
      if (state.mode === "constructor_throw") throw new Error("synthetic constructor failure");
      this.options = options;
      state.instances.push(this);
    }
    show() {
      state.showCalls += 1;
      if (state.mode === "show_throw") throw new Error("synthetic show failure");
      if (state.mode === "show") queueMicrotask(() => {
        state.events.push("show"); this.emit("show", {});
      });
      if (state.mode === "failed") queueMicrotask(() => {
        state.events.push("failed"); this.emit("failed", {}, "synthetic permission denied");
      });
    }
    close() { this.emit("close", {}); }
  }
  const controller = createSystemBehaviorController({
    app: { getLoginItemSettings: () => ({ openAtLogin: false }) },
    powerSaveBlocker: { start: () => 1, stop() {}, isStarted: () => true },
    Notification: SyntheticNotification,
    platform: "darwin",
    observeNotificationDelivery: async (id) => ({
      id, authorization: "authorized", delivered: state.deliveryConfirmed,
    }),
    getMainWindow: () => ({ isDestroyed: () => false, isFocused: () => state.focused,
      show() {}, focus() {}, webContents: { send() {} } }),
  });
  return { state, controller };
}

module.exports = { makeHarness };
if (require.main === module) {
  (async () => {
    const { state, controller } = makeHarness();
    state.mode = process.argv[2] || "no_event";
    const result = await controller.notify({
      id: "synthetic-native-request", type: "daily_brief", receiptId: "synthetic-receipt",
      title: "Synthetic only", body: "No operating system notification",
    });
    await new Promise((resolve) => setImmediate(resolve));
    process.stdout.write(JSON.stringify({ result, events: state.events,
      constructorCalls: state.constructorCalls, showCalls: state.showCalls }));
    controller.dispose();
  })().catch((error) => { process.stderr.write(String(error)); process.exitCode = 1; });
}
