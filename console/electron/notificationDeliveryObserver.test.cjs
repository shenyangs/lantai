const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const {
  createNotificationDeliveryObserver,
} = require("./notificationDeliveryObserver.cjs");

test("simultaneous observations share one read and match only their exact identifiers", async () => {
  let complete;
  let reads = 0;
  const observer = createNotificationDeliveryObserver({
    platform: "darwin",
    loadNative: () => ({
      readSnapshot: () => {
        reads += 1;
        return new Promise((resolve) => {
          complete = resolve;
        });
      },
    }),
  });
  const first = observer.observe("native-1");
  const second = observer.observe("native-2");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(reads, 1);
  complete({
    authorization: "authorized",
    deliveredIds: ["native-1", "unrelated-local-record"],
  });
  assert.deepEqual(await first, {
    id: "native-1",
    authorization: "authorized",
    delivered: true,
  });
  assert.deepEqual(await second, {
    id: "native-2",
    authorization: "authorized",
    delivered: false,
  });
});

test("a later observation reads fresh OS settings after permission changes", async () => {
  let loads = 0;
  let reads = 0;
  const observer = createNotificationDeliveryObserver({
    platform: "darwin",
    loadNative: () => {
      loads += 1;
      return {
        readSnapshot: async () =>
          ++reads === 1
            ? { authorization: "denied", deliveredIds: [] }
            : { authorization: "authorized", deliveredIds: ["native-2"] },
      };
    },
  });
  assert.deepEqual(await observer.observe("native-1"), {
    id: "native-1",
    authorization: "denied",
    delivered: false,
  });
  assert.deepEqual(await observer.observe("native-2"), {
    id: "native-2",
    authorization: "authorized",
    delivered: true,
  });
  assert.equal(loads, 1);
  assert.equal(reads, 2);
});

for (const loadNative of [
  () => {
    throw new Error("missing addon");
  },
  () => ({}),
  () => ({
    readSnapshot: async () => {
      throw new Error("read failed");
    },
  }),
]) {
  test("unavailable native reads produce unknown instead of fabricated success", async () => {
    const observer = createNotificationDeliveryObserver({
      platform: "darwin",
      loadNative,
    });
    assert.deepEqual(await observer.observe("native-1"), {
      id: "native-1",
      authorization: "unknown",
      delivered: false,
    });
  });
}

for (const snapshot of [
  null,
  {},
  { authorization: "made_up", deliveredIds: ["native-1"] },
  { authorization: "authorized", deliveredIds: "native-1" },
  { authorization: "authorized", deliveredIds: ["native-1", null] },
]) {
  test("malformed OS snapshots cannot confirm delivery", async () => {
    const observer = createNotificationDeliveryObserver({
      platform: "darwin",
      loadNative: () => ({ readSnapshot: async () => snapshot }),
    });
    assert.deepEqual(await observer.observe("native-1"), {
      id: "native-1",
      authorization: "unknown",
      delivered: false,
    });
  });
}

test("an unavailable snapshot can recover on the next read without reloading the addon", async () => {
  let reads = 0;
  const observer = createNotificationDeliveryObserver({
    platform: "darwin",
    loadNative: () => ({
      readSnapshot: async () => {
        if (++reads === 1) throw new Error("transient failure");
        return { authorization: "authorized", deliveredIds: ["native-1"] };
      },
    }),
  });
  assert.equal((await observer.observe("native-1")).authorization, "unknown");
  assert.equal((await observer.observe("native-1")).delivered, true);
  assert.equal(reads, 2);
});

test("other platforms and empty identifiers never load macOS services", async () => {
  const loadNative = () => {
    assert.fail("must not load the macOS addon");
  };
  const other = createNotificationDeliveryObserver({
    platform: "linux",
    loadNative,
  });
  assert.equal((await other.observe("native-1")).delivered, false);
  const mac = createNotificationDeliveryObserver({
    platform: "darwin",
    loadNative,
  });
  assert.equal((await mac.observe("")).delivered, false);
});

test("packaging includes the observer and unpacks its native reader through the existing build chain", () => {
  const root = path.resolve(__dirname, "..");
  const manifest = JSON.parse(
    fs.readFileSync(path.join(root, "package.json"), "utf8"),
  );
  assert.ok(
    manifest.build.files.includes("electron/notificationDeliveryObserver.cjs"),
  );
  assert.ok(
    manifest.build.files.includes(
      "electron/native/bin/notification-delivery.node",
    ),
  );
  assert.ok(
    manifest.build.asarUnpack.includes(
      "electron/native/bin/notification-delivery.node",
    ),
  );
  assert.ok(
    manifest.scripts["build:native-helpers"]
      .split(" && ")
      .includes("npm run build:notification-delivery"),
  );
  const script = manifest.scripts["build:notification-delivery"];
  assert.equal(script, "node scripts/build-notification-delivery.cjs");
  assert.ok(
    fs.statSync(path.join(root, script.slice("node ".length))).isFile(),
  );
  assert.ok(
    fs
      .statSync(path.join(root, "electron/native/notification-delivery.mm"))
      .isFile(),
  );
});
