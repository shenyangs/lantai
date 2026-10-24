const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createSystemBehaviorController,
} = require("./systemBehaviorController.cjs");

function makeHarness({ focused = false } = {}) {
  const calls = {
    login: [],
    blockerStarts: [],
    blockerStops: [],
    notifications: [],
    focus: 0,
    sent: [],
    notificationInstances: [],
  };
  let openAtLogin = false;
  let nextBlockerId = 10;
  const started = new Set();
  class NotificationMock {
    static isSupported() {
      return true;
    }

    constructor(options) {
      this.options = options;
      this.listeners = {};
      calls.notifications.push(options);
      calls.notificationInstances.push(this);
    }

    on(name, callback) {
      this.listeners[name] = callback;
    }

    show() {
      queueMicrotask(() => this.listeners.show?.());
    }
  }
  const controller = createSystemBehaviorController({
    app: {
      getLoginItemSettings: () => ({ openAtLogin }),
      setLoginItemSettings: (settings) => {
        openAtLogin = settings.openAtLogin;
        calls.login.push(settings);
      },
    },
    powerSaveBlocker: {
      start: (mode) => {
        const id = nextBlockerId++;
        started.add(id);
        calls.blockerStarts.push(mode);
        return id;
      },
      stop: (id) => {
        started.delete(id);
        calls.blockerStops.push(id);
      },
      isStarted: (id) => started.has(id),
    },
    Notification: NotificationMock,
    platform: "darwin",
    observeNotificationDelivery: async (id) => ({
      id,
      authorization: "authorized",
      delivered: true,
    }),
    getMainWindow: () => ({
      isDestroyed: () => false,
      isFocused: () => focused,
      show: () => {},
      focus: () => {
        calls.focus += 1;
      },
      webContents: {
        send: (...args) => calls.sent.push(args),
      },
    }),
  });
  return { calls, controller };
}

test("persists launch-at-login through the native app API", () => {
  const { calls, controller } = makeHarness();
  const result = controller.updatePreferences({ launch_at_login: true });

  assert.equal(result.ok, true);
  assert.deepEqual(calls.login, [{ openAtLogin: true }]);
  assert.equal(controller.getPreferences().launch_at_login, true);
});

test("blocks display sleep only while both preference and task are active", () => {
  const { calls, controller } = makeHarness();
  controller.setTaskActivity({ sourceId: "global", running: true });
  assert.equal(calls.blockerStarts.length, 0);

  controller.updatePreferences({ keep_awake_while_running: true });
  assert.deepEqual(calls.blockerStarts, ["prevent-display-sleep"]);
  assert.equal(controller.getPreferences().keep_awake_active, true);

  controller.setTaskActivity({ sourceId: "global", running: false });
  assert.deepEqual(calls.blockerStops, [10]);
  assert.equal(controller.getPreferences().keep_awake_active, false);
});

test("completion notifications respect focus mode and deduplicate", async () => {
  const { calls, controller } = makeHarness({ focused: false });
  const first = await controller.notify({
    id: "chat-1:done",
    type: "turn_completed",
    body: "三维迷宫已完成",
  });
  const duplicate = await controller.notify({
    id: "chat-1:done",
    type: "turn_completed",
  });

  assert.equal(first.shown, true);
  assert.equal(duplicate.shown, false);
  assert.equal(calls.notifications.length, 1);
});

test("permission notifications stay quiet while the app is focused", async () => {
  const { calls, controller } = makeHarness({ focused: true });
  const result = await controller.notify({
    id: "approval-1",
    type: "permission_required",
  });

  assert.equal(result.shown, false);
  assert.equal(calls.notifications.length, 0);
});

test("daily brief notification click opens the exact receipt in the renderer", async () => {
  const { calls, controller } = makeHarness({ focused: false });
  const result = await controller.notify({
    id: "brief-delivery-1",
    type: "daily_brief",
    receiptId: "delivery-1",
    title: "澜台简报",
    body: "有一份新的简报可以阅读",
  });

  assert.equal(result.shown, true);
  calls.notificationInstances[0].listeners.click();
  assert.deepEqual(calls.sent, [
    ["lantai:daily-brief-open", { receiptId: "delivery-1" }],
  ]);
});

test("daily brief notifications stay quiet while the app is focused", async () => {
  const { calls, controller } = makeHarness({ focused: true });
  const result = await controller.notify({
    id: "brief-delivery-focused",
    type: "daily_brief",
    receiptId: "delivery-focused",
  });

  assert.equal(result.shown, false);
  assert.equal(result.reason, "preference");
  assert.equal(calls.notifications.length, 0);
  assert.equal(calls.sent.length, 0);
});
