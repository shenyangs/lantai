"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const {
  makeHarness,
} = require("../../tests/unit/app/reminder_native_notification_probe.cjs");
const payload = {
  id: "synthetic-retry-id",
  type: "daily_brief",
  receiptId: "synthetic-receipt",
  title: "Synthetic only",
};

for (const failure of [
  "unsupported",
  "constructor_throw",
  "show_throw",
  "failed",
]) {
  test(`same ID can retry after native ${failure}`, async () => {
    const { state, controller } = makeHarness();
    try {
      if (failure === "unsupported") state.supported = false;
      else state.mode = failure;
      try {
        await controller.notify(payload);
      } catch (error) {
        assert.match(String(error), /synthetic .* failure/);
      }
      await new Promise((resolve) => setImmediate(resolve));
      const previousShowCalls = state.showCalls;
      state.supported = true;
      state.mode = "show";
      const retry = await controller.notify(payload);
      assert.equal(
        state.showCalls,
        previousShowCalls + 1,
        "failed/unsupported ID was reserved as if it had already been delivered",
      );
      assert.notEqual(retry.reason, "duplicate");
    } finally {
      controller.dispose();
    }
  });
}

test("confirmed native delivery keeps duplicate suppression", async () => {
  const { state, controller } = makeHarness();
  try {
    await controller.notify(payload);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(state.events, ["show"]);
    const duplicate = await controller.notify(payload);
    assert.equal(state.showCalls, 1);
    assert.equal(duplicate.reason, "duplicate");
  } finally {
    controller.dispose();
  }
});

test("focused preference suppresses the native effect", async () => {
  const { state, controller } = makeHarness();
  try {
    state.focused = true;
    const result = await controller.notify(payload);
    assert.equal(result.shown, false);
    assert.equal(state.constructorCalls, 0);
    assert.equal(state.showCalls, 0);
  } finally {
    controller.dispose();
  }
});

const bound = {
  id: "r",
  type: "reminder",
  run_id: "r",
  attempt_id: "r:a",
  agent_id: "a",
  reminder_id: "m",
  caller_id: "desktop",
  receipt_token: "synthetic",
};

test("a reminder show receipt binds every original identity", async () => {
  const { controller } = makeHarness();
  try {
    assert.deepEqual(await controller.notify(bound), {
      ...Object.fromEntries(
        Object.entries(bound).filter(([key]) => key !== "type"),
      ),
      ok: true,
      shown: true,
      status: "shown",
    });
    assert.equal(
      (await controller.notify({ ...bound, attempt_id: "foreign" })).ok,
      false,
    );
  } finally {
    controller.dispose();
  }
});

test("unknown native outcome cannot be sent again", async () => {
  const { state, controller } = makeHarness();
  try {
    state.mode = "no_event";
    const first = await controller.notify(bound);
    assert.equal(first.status, "unknown");
    state.mode = "show";
    const retry = await controller.notify(bound);
    assert.equal(retry.status, "unknown");
    assert.equal(state.showCalls, 1);
  } finally {
    controller.dispose();
  }
});

test("dispose completes every unconfirmed promise without success", async () => {
  const { state, controller } = makeHarness();
  state.mode = "no_event";
  const pending = controller.notify(bound);
  controller.dispose();
  controller.dispose();
  const receipt = await pending;
  assert.equal(receipt.status, "unknown");
  assert.equal(receipt.shown, false);
});
