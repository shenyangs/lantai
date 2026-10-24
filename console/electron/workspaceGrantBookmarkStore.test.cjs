const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  createWorkspaceGrantBookmarkStore,
} = require("./workspaceGrantBookmarkStore.cjs");

function fakeSafeStorage() {
  return {
    isEncryptionAvailable: () => true,
    encryptString: (value) => Buffer.from(`encrypted:${value}`, "utf8"),
    decryptString: (value) =>
      value.toString("utf8").replace(/^encrypted:/, ""),
  };
}

test("MAS workspace bookmarks persist encrypted and restore their scope", () => {
  const userData = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-workspace-bookmarks-"),
  );
  const started = [];
  let stopped = 0;
  const app = {
    getPath: () => userData,
    startAccessingSecurityScopedResource: (bookmark) => {
      started.push(bookmark);
      return () => {
        stopped += 1;
      };
    },
  };
  const store = createWorkspaceGrantBookmarkStore({
    app,
    safeStorage: fakeSafeStorage(),
    randomId: () => "bookmark-1",
  });

  const authorized = store.authorizeSelection({
    agentId: "default",
    projectRoot: "/Users/example/Projects/alpha",
    bookmark: "raw-security-bookmark",
  });

  assert.equal(authorized.ok, true);
  assert.equal(authorized.securityScopeRef, "wgb_bookmark-1");
  assert.deepEqual(started, ["raw-security-bookmark"]);
  const persisted = fs.readFileSync(store.storePath, "utf8");
  assert.equal(persisted.includes("raw-security-bookmark"), false);
  assert.equal(persisted.includes("/Users/example/Projects/alpha"), false);

  store.closeAll();
  assert.equal(stopped, 1);

  const restored = createWorkspaceGrantBookmarkStore({
    app,
    safeStorage: fakeSafeStorage(),
  }).restoreAll();
  assert.equal(restored.restored, 1);
  assert.deepEqual(started, [
    "raw-security-bookmark",
    "raw-security-bookmark",
  ]);
});
