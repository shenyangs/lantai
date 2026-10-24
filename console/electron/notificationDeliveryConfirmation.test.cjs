const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");
const {
  createSystemBehaviorController,
} = require("./systemBehaviorController.cjs");
const {
  createNotificationDeliveryObserver,
} = require("./notificationDeliveryObserver.cjs");

const reminder = {
  id: "run-1",
  type: "reminder",
  run_id: "run-1",
  attempt_id: "attempt-1",
  agent_id: "agent-1",
  reminder_id: "reminder-1",
  caller_id: "desktop-1",
  receipt_token: "synthetic-receipt-token",
};

function makeHarness(observe, { platform = "darwin", focused = false } = {}) {
  const instances = [];
  const queries = [];
  const sent = [];
  class FakeNotification extends EventEmitter {
    static isSupported() {
      return true;
    }
    constructor(options) {
      super();
      this.options = options;
      instances.push(this);
    }
    show() {
      queueMicrotask(() => this.emit("show"));
    }
    close() {
      this.emit("close");
    }
  }
  const controller = createSystemBehaviorController({
    app: {},
    powerSaveBlocker: { start() {}, stop() {} },
    Notification: FakeNotification,
    platform,
    notificationConfirmationTimeoutMs: 40,
    notificationDeliveryPollIntervalMs: 2,
    observeNotificationDelivery: async (id) => {
      queries.push(id);
      return observe(id);
    },
    getMainWindow: () => ({
      isDestroyed: () => false,
      isFocused: () => focused,
      show() {},
      focus() {},
      webContents: { send: (...args) => sent.push(args) },
    }),
  });
  return { controller, instances, queries, sent };
}

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));

test("macOS denial is failed even when Electron emits show", async () => {
  const h = makeHarness((id) => ({
    id,
    authorization: "denied",
    delivered: false,
  }));
  try {
    const receipt = await h.controller.notify(reminder);
    assert.equal(receipt.shown, false);
    assert.equal(receipt.ok, false);
    assert.equal(receipt.status, "failed");
    assert.equal(receipt.reason, "permission_denied");
    assert.equal(h.queries.length, 1);
    assert.equal(receipt.run_id, reminder.run_id);
    assert.equal(receipt.attempt_id, reminder.attempt_id);
  } finally {
    h.controller.dispose();
  }
});

test("a denied snapshot with a matching OS record fails and permits a fresh attempt after permission recovery", async () => {
  let authorization = "denied";
  let nativeId;
  const observer = createNotificationDeliveryObserver({
    platform: "darwin",
    loadNative: () => ({
      readSnapshot: async () => ({
        authorization,
        deliveredIds: [nativeId],
      }),
    }),
  });
  const h = makeHarness((id) => {
    nativeId = id;
    return observer.observe(id);
  });
  try {
    const receipt = await h.controller.notify(reminder);
    assert.equal(receipt.ok, false);
    assert.equal(receipt.shown, false);
    assert.equal(receipt.status, "failed");
    assert.equal(receipt.reason, "permission_denied");
    for (const [key, value] of Object.entries(reminder)) {
      if (key !== "type") assert.equal(receipt[key], value);
    }
    assert.equal(h.queries.length, 1);
    authorization = "authorized";
    assert.equal((await h.controller.notify(reminder)).status, "shown");
    assert.equal(h.instances.length, 2);
    assert.notEqual(h.instances[0].options.id, h.instances[1].options.id);
  } finally {
    h.controller.dispose();
  }
});

for (const authorization of ["unknown", "not_determined", undefined]) {
  test(`a matching record with ${authorization ?? "missing"} permission remains unknown and cannot resend`, async () => {
    const h = makeHarness((id) => ({ id, authorization, delivered: true }));
    try {
      const receipt = await h.controller.notify(reminder);
      assert.equal(receipt.ok, false);
      assert.equal(receipt.shown, false);
      assert.equal(receipt.status, "unknown");
      assert.equal(
        (await h.controller.notify(reminder)).reason,
        "outcome_unknown",
      );
      assert.equal(h.instances.length, 1);
      assert.equal(h.queries.length, 1);
    } finally {
      h.controller.dispose();
    }
  });
}

test("an actual click stays shown when a pending read later returns denial with a matching record", async () => {
  let complete;
  const h = makeHarness(
    (id) =>
      new Promise((resolve) => {
        complete = () => resolve({ id, authorization: "denied", delivered: true });
      }),
  );
  try {
    const pending = h.controller.notify(reminder);
    await nextTurn();
    h.instances[0].emit("click");
    assert.equal((await pending).status, "shown");
    complete();
    await nextTurn();
    assert.equal((await h.controller.notify(reminder)).reason, "duplicate");
    assert.deepEqual(h.sent, [
      [
        "lantai:reminder-open",
        { reminderId: reminder.reminder_id, agentId: reminder.agent_id },
      ],
    ]);
  } finally {
    h.controller.dispose();
  }
});

test("macOS submission without a delivery record stays unknown and cannot resend", async () => {
  const h = makeHarness((id) => ({
    id,
    authorization: "authorized",
    delivered: false,
  }));
  try {
    const receipt = await h.controller.notify(reminder);
    assert.equal(receipt.shown, false);
    assert.equal(receipt.status, "unknown");
    const duplicate = await h.controller.notify(reminder);
    assert.equal(duplicate.reason, "outcome_unknown");
    assert.equal(h.instances.length, 1);
  } finally {
    h.controller.dispose();
  }
});

test("a delivery record for another native identifier cannot confirm this reminder", async () => {
  const h = makeHarness(() => ({
    id: "other-native-id",
    authorization: "authorized",
    delivered: true,
  }));
  try {
    const receipt = await h.controller.notify(reminder);
    assert.equal(receipt.shown, false);
    assert.equal(receipt.status, "unknown");
  } finally {
    h.controller.dispose();
  }
});

test("a matching delivery record confirms all original reminder bindings and exact click routing", async () => {
  const h = makeHarness((id) => ({
    id,
    authorization: "authorized",
    delivered: true,
  }));
  try {
    const receipt = await h.controller.notify(reminder);
    assert.deepEqual(receipt, {
      ...Object.fromEntries(
        Object.entries(reminder).filter(([key]) => key !== "type"),
      ),
      ok: true,
      shown: true,
      status: "shown",
    });
    assert.match(h.instances[0].options.id, /^[0-9a-f-]{36}$/);
    assert.notEqual(h.instances[0].options.id, reminder.id);
    assert.deepEqual(h.queries, [h.instances[0].options.id]);
    h.instances[0].emit("click");
    assert.deepEqual(h.sent, [
      [
        "lantai:reminder-open",
        { reminderId: reminder.reminder_id, agentId: reminder.agent_id },
      ],
    ]);
    assert.equal((await h.controller.notify(reminder)).reason, "duplicate");
  } finally {
    h.controller.dispose();
  }
});

test("delivery confirmation polls for an eventual matching OS record", async () => {
  let reads = 0;
  const h = makeHarness((id) => ({
    id,
    authorization: "provisional",
    delivered: ++reads === 2,
  }));
  try {
    assert.equal((await h.controller.notify(reminder)).status, "shown");
    assert.equal(reads, 2);
  } finally {
    h.controller.dispose();
  }
});

test("permission denial allows a later attempt with a fresh native identifier", async () => {
  let denied = true;
  const h = makeHarness((id) => ({
    id,
    authorization: denied ? "denied" : "authorized",
    delivered: !denied,
  }));
  try {
    assert.equal((await h.controller.notify(reminder)).status, "failed");
    denied = false;
    assert.equal((await h.controller.notify(reminder)).status, "shown");
    assert.equal(h.instances.length, 2);
    assert.notEqual(h.instances[0].options.id, h.instances[1].options.id);
  } finally {
    h.controller.dispose();
  }
});

for (const outcome of ["reject", "missing", "not_determined"]) {
  test(`a ${outcome} OS read never becomes delivery success or a resend`, async () => {
    const h = makeHarness((id) => {
      if (outcome === "reject") throw new Error("synthetic read unavailable");
      if (outcome === "missing") return undefined;
      return { id, authorization: "not_determined", delivered: false };
    });
    try {
      assert.equal((await h.controller.notify(reminder)).status, "unknown");
      assert.equal(
        (await h.controller.notify(reminder)).reason,
        "outcome_unknown",
      );
      assert.equal(h.instances.length, 1);
      assert.equal(h.queries.length, 1);
    } finally {
      h.controller.dispose();
    }
  });
}

test("concurrent requests for the same reminder join the pending confirmation", async () => {
  let complete;
  const h = makeHarness(
    (id) =>
      new Promise((resolve) => {
        complete = () =>
          resolve({ id, authorization: "authorized", delivered: true });
      }),
  );
  try {
    const first = h.controller.notify(reminder);
    const duplicate = h.controller.notify(reminder);
    assert.equal(first, duplicate);
    await nextTurn();
    complete();
    assert.equal((await first).status, "shown");
    assert.equal(h.instances.length, 1);
    assert.equal(h.queries.length, 1);
  } finally {
    h.controller.dispose();
  }
});

test("a real notification click confirms delivery during an unfinished OS read", async () => {
  let complete;
  const h = makeHarness(
    (id) =>
      new Promise((resolve) => {
        complete = () =>
          resolve({ id, delivered: false, authorization: "denied" });
      }),
  );
  try {
    const pending = h.controller.notify(reminder);
    await nextTurn();
    h.instances[0].emit("click");
    assert.equal((await pending).status, "shown");
    complete();
    await nextTurn();
    assert.equal((await h.controller.notify(reminder)).reason, "duplicate");
    assert.equal(h.sent.length, 1);
  } finally {
    h.controller.dispose();
  }
});

for (const end of ["timeout", "dispose", "close", "failed"]) {
  test(`late OS confirmation cannot replace a ${end} settlement`, async () => {
    let complete;
    const h = makeHarness(
      (id) =>
        new Promise((resolve) => {
          complete = () => resolve({ id, delivered: true });
        }),
    );
    try {
      const pending = h.controller.notify(reminder);
      await nextTurn();
      if (end === "dispose") h.controller.dispose();
      if (end === "close") h.instances[0].emit("close");
      if (end === "failed")
        h.instances[0].emit("failed", {}, "synthetic failed");
      const receipt = await pending;
      assert.equal(receipt.shown, false);
      assert.equal(receipt.status, end === "failed" ? "failed" : "unknown");
      complete();
      await nextTurn();
      assert.equal(receipt.shown, false);
      assert.equal(h.queries.length, 1);
      if (end !== "failed")
        assert.equal(
          (await h.controller.notify(reminder)).reason,
          "outcome_unknown",
        );
    } finally {
      h.controller.dispose();
    }
  });
}

test("foreground suppression never submits a notification or queries the OS", async () => {
  const h = makeHarness(
    () => {
      throw new Error("must not query");
    },
    { focused: true },
  );
  try {
    assert.equal((await h.controller.notify(reminder)).reason, "preference");
    assert.equal(h.instances.length, 0);
    assert.equal(h.queries.length, 0);
  } finally {
    h.controller.dispose();
  }
});

for (const platform of ["win32", "linux"]) {
  test(`${platform} retains native show confirmation without loading the macOS reader`, async () => {
    const h = makeHarness(
      () => {
        throw new Error("macOS only");
      },
      { platform },
    );
    try {
      assert.equal((await h.controller.notify(reminder)).status, "shown");
      assert.equal(h.instances[0].options.id, undefined);
      assert.equal(h.queries.length, 0);
    } finally {
      h.controller.dispose();
    }
  });
}

test("daily brief keeps its original receipt click route", async () => {
  const h = makeHarness((id) => ({
    id,
    authorization: "authorized",
    delivered: true,
  }));
  try {
    assert.equal(
      (
        await h.controller.notify({
          type: "daily_brief",
          id: "brief-1",
          receiptId: "receipt-1",
        })
      ).shown,
      true,
    );
    h.instances[0].emit("click");
    assert.deepEqual(h.sent, [
      ["lantai:daily-brief-open", { receiptId: "receipt-1" }],
    ]);
  } finally {
    h.controller.dispose();
  }
});
