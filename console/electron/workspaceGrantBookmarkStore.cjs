const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const STORE_VERSION = 1;

function createWorkspaceGrantBookmarkStore({
  app,
  safeStorage,
  fsImpl = fs,
  pathImpl = path,
  randomId = () => crypto.randomBytes(18).toString("base64url"),
  now = () => new Date(),
  appendLog = () => {},
} = {}) {
  const storePath = pathImpl.join(
    app.getPath("userData"),
    "security",
    "workspace-grant-bookmarks.v1.json",
  );
  const activeScopes = new Map();

  function encryptionAvailable() {
    return (
      safeStorage &&
      typeof safeStorage.encryptString === "function" &&
      typeof safeStorage.decryptString === "function" &&
      (typeof safeStorage.isEncryptionAvailable !== "function" ||
        safeStorage.isEncryptionAvailable())
    );
  }

  function emptyStore() {
    return { version: STORE_VERSION, records: {} };
  }

  function load() {
    if (!fsImpl.existsSync(storePath)) {
      return emptyStore();
    }
    const parsed = JSON.parse(fsImpl.readFileSync(storePath, "utf8"));
    if (
      !parsed ||
      parsed.version !== STORE_VERSION ||
      !parsed.records ||
      typeof parsed.records !== "object"
    ) {
      throw new Error("Unsupported workspace bookmark store schema.");
    }
    return parsed;
  }

  function save(store) {
    const parent = pathImpl.dirname(storePath);
    fsImpl.mkdirSync(parent, { recursive: true });
    const temporary = `${storePath}.${process.pid}.${randomId()}.tmp`;
    try {
      fsImpl.writeFileSync(
        temporary,
        `${JSON.stringify(store, null, 2)}\n`,
        { encoding: "utf8", mode: 0o600 },
      );
      try {
        fsImpl.chmodSync(temporary, 0o600);
      } catch {
        // Best effort on filesystems without POSIX modes.
      }
      fsImpl.renameSync(temporary, storePath);
    } finally {
      try {
        fsImpl.rmSync(temporary, { force: true });
      } catch {
        // Best effort temporary cleanup.
      }
    }
  }

  function encryptBookmark(bookmark) {
    if (!encryptionAvailable()) {
      throw new Error("Electron safeStorage encryption is unavailable.");
    }
    const encrypted = safeStorage.encryptString(
      JSON.stringify({ bookmark }),
    );
    return Buffer.from(encrypted).toString("base64");
  }

  function decryptBookmark(record) {
    if (
      record.encryption !== "electron_safe_storage_v1" ||
      !encryptionAvailable()
    ) {
      throw new Error("Workspace bookmark decryption is unavailable.");
    }
    const decrypted = safeStorage.decryptString(
      Buffer.from(String(record.encrypted_payload || ""), "base64"),
    );
    const payload = JSON.parse(decrypted);
    if (!payload || typeof payload.bookmark !== "string" || !payload.bookmark) {
      throw new Error("Workspace bookmark payload is invalid.");
    }
    return payload.bookmark;
  }

  function startScope(ref, bookmark) {
    if (
      !app ||
      typeof app.startAccessingSecurityScopedResource !== "function"
    ) {
      throw new Error("Security-scoped workspace access is unavailable.");
    }
    const stop = app.startAccessingSecurityScopedResource(bookmark);
    if (typeof stop !== "function") {
      throw new Error("Security-scoped workspace access could not be started.");
    }
    const previous = activeScopes.get(ref);
    if (typeof previous === "function") {
      previous();
    }
    activeScopes.set(ref, stop);
  }

  function authorizeSelection({ agentId, projectRoot, bookmark }) {
    if (!agentId || !projectRoot || !bookmark) {
      return {
        ok: false,
        error: "Workspace bookmark selection is incomplete.",
        error_code: "mas_security_scoped_bookmark_missing",
      };
    }
    const ref = `wgb_${randomId()}`;
    try {
      startScope(ref, bookmark);
      const store = load();
      const timestamp = now().toISOString();
      store.records[ref] = {
        ref,
        status: "active",
        agent_id: String(agentId),
        path_sha256: `sha256:${crypto
          .createHash("sha256")
          .update(String(projectRoot))
          .digest("hex")}`,
        bookmark_sha256: `sha256:${crypto
          .createHash("sha256")
          .update(String(bookmark))
          .digest("hex")}`,
        encryption: "electron_safe_storage_v1",
        encrypted_payload: encryptBookmark(bookmark),
        created_at: timestamp,
        updated_at: timestamp,
        last_restored_at: "",
        stale_reason: "",
      };
      save(store);
      return {
        ok: true,
        securityScopeRef: ref,
        pathSha256: store.records[ref].path_sha256,
      };
    } catch (error) {
      const stop = activeScopes.get(ref);
      activeScopes.delete(ref);
      if (typeof stop === "function") {
        try {
          stop();
        } catch {
          // Best effort rollback.
        }
      }
      return {
        ok: false,
        error: error?.message || "Workspace bookmark persistence failed.",
        error_code: "mas_security_scope_secure_store_failed",
      };
    }
  }

  function restoreAll() {
    let restored = 0;
    let stale = 0;
    try {
      const store = load();
      let changed = false;
      for (const [ref, record] of Object.entries(store.records)) {
        if (record?.status !== "active") continue;
        try {
          startScope(ref, decryptBookmark(record));
          record.last_restored_at = now().toISOString();
          record.stale_reason = "";
          restored += 1;
          changed = true;
        } catch (error) {
          record.status = "stale";
          record.stale_reason =
            error?.message || "Security-scoped bookmark restore failed.";
          record.updated_at = now().toISOString();
          stale += 1;
          changed = true;
        }
      }
      if (changed) save(store);
    } catch (error) {
      appendLog(
        `[WorkspaceGrantBookmarkStore] restore failed: ${error.message}`,
      );
      return { ok: false, restored, stale, error: error.message };
    }
    return { ok: stale === 0, restored, stale };
  }

  function revoke(ref) {
    const stop = activeScopes.get(ref);
    activeScopes.delete(ref);
    if (typeof stop === "function") {
      try {
        stop();
      } catch {
        // Best effort scope release.
      }
    }
    const store = load();
    if (!store.records[ref]) return false;
    store.records[ref].status = "revoked";
    store.records[ref].updated_at = now().toISOString();
    save(store);
    return true;
  }

  function closeAll() {
    for (const stop of activeScopes.values()) {
      try {
        stop();
      } catch {
        // Best effort shutdown.
      }
    }
    activeScopes.clear();
  }

  return {
    authorizeSelection,
    closeAll,
    restoreAll,
    revoke,
    storePath,
  };
}

module.exports = {
  createWorkspaceGrantBookmarkStore,
};
