const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const BUNDLE_ID = "com.lantai.app";
const LSREGISTER =
  "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister";
const PLIST_BUDDY = "/usr/libexec/PlistBuddy";

function findMacAppBundlePath(executablePath, pathImpl = path) {
  let current = pathImpl.resolve(String(executablePath || ""));
  while (current && current !== pathImpl.dirname(current)) {
    if (pathImpl.extname(current).toLowerCase() === ".app") return current;
    current = pathImpl.dirname(current);
  }
  return null;
}

function parseLaunchServicesRegistrations(dump, bundleId = BUNDLE_ID) {
  const registrations = [];
  for (const block of String(dump || "").split(/\n-{40,}\n/)) {
    const identifier = block.match(/^identifier:\s+(.+?)\s*$/m)?.[1]?.trim();
    if (!identifier || identifier.toLowerCase() !== bundleId.toLowerCase()) {
      continue;
    }
    const appPath = block
      .match(/^path:\s+(.*?)\s+\(0x[0-9a-f]+\)\s*$/im)?.[1]
      ?.trim();
    if (!appPath) continue;
    registrations.push({
      path: appPath,
      version: block.match(/^versionString:\s+(.+?)\s*$/m)?.[1]?.trim() || "",
    });
  }
  return registrations;
}

function normalizePath(value, pathImpl = path) {
  return pathImpl.resolve(String(value || ""));
}

function samePath(left, right, pathImpl = path) {
  return normalizePath(left, pathImpl) === normalizePath(right, pathImpl);
}

function createLaunchServicesController({
  app,
  ipcMain = null,
  isTrustedIpcEvent = () => false,
  platform = process.platform,
  fsImpl = fs,
  pathImpl = path,
  homeDir = os.homedir(),
  execFileImpl = childProcess.execFile,
  readBundleMetadataImpl = null,
  waitImpl = (milliseconds) =>
    new Promise((resolve) => setTimeout(resolve, milliseconds)),
}) {
  function execFile(file, args = []) {
    return new Promise((resolve) => {
      execFileImpl(
        file,
        args,
        { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
        (error, stdout = "", stderr = "") => {
          resolve({
            ok: !error,
            code: Number.isInteger(error?.code) ? error.code : error ? 1 : 0,
            stdout: String(stdout || ""),
            stderr: String(stderr || error?.message || ""),
          });
        },
      );
    });
  }

  async function readBundleMetadata(appPath) {
    if (readBundleMetadataImpl) return readBundleMetadataImpl(appPath);
    const plistPath = pathImpl.join(appPath, "Contents", "Info.plist");
    if (!fsImpl.existsSync(plistPath)) return null;
    const [identifier, version] = await Promise.all([
      execFile(PLIST_BUDDY, ["-c", "Print :CFBundleIdentifier", plistPath]),
      execFile(PLIST_BUDDY, [
        "-c",
        "Print :CFBundleShortVersionString",
        plistPath,
      ]),
    ]);
    if (!identifier.ok) return null;
    return {
      bundle_id: identifier.stdout.trim(),
      version: version.ok ? version.stdout.trim() : "",
    };
  }

  function currentAppPath() {
    if (platform !== "darwin" || app?.isPackaged === false) return null;
    try {
      return findMacAppBundlePath(app?.getPath?.("exe"), pathImpl);
    } catch {
      return null;
    }
  }

  async function dumpRegistrations() {
    const result = await execFile(LSREGISTER, ["-dump"]);
    if (!result.ok) {
      throw new Error(
        result.stderr.trim() || "无法读取 macOS 文件打开方式注册表",
      );
    }
    return parseLaunchServicesRegistrations(result.stdout);
  }

  async function collectAppBundles(root, maxDepth = 2) {
    const found = [];
    async function visit(directory, depth) {
      let entries;
      try {
        entries = await fsImpl.promises.readdir(directory, {
          withFileTypes: true,
        });
      } catch {
        return;
      }
      for (const entry of entries) {
        if (!entry.isDirectory()) continue;
        const candidate = pathImpl.join(directory, entry.name);
        const plist = pathImpl.join(candidate, "Contents", "Info.plist");
        if (fsImpl.existsSync(plist)) {
          found.push(candidate);
          continue;
        }
        if (depth < maxDepth) await visit(candidate, depth + 1);
      }
    }
    await visit(root, 0);
    return found;
  }

  async function discoverCandidatePaths(registrations, currentPath) {
    const roots = [
      "/Applications",
      pathImpl.join(homeDir, "Applications"),
      pathImpl.join(homeDir, "Downloads"),
      pathImpl.join(homeDir, ".Trash"),
      currentPath ? pathImpl.dirname(currentPath) : "",
    ].filter(Boolean);
    const candidates = new Set(
      registrations.map((item) => normalizePath(item.path, pathImpl)),
    );
    for (const root of [
      ...new Set(roots.map((item) => normalizePath(item, pathImpl))),
    ]) {
      for (const candidate of await collectAppBundles(root))
        candidates.add(normalizePath(candidate, pathImpl));
    }
    return [...candidates];
  }

  async function inspectState() {
    if (platform !== "darwin") {
      return {
        ok: true,
        supported: false,
        bundle_id: BUNDLE_ID,
        registrations: [],
        stale_registrations: [],
        disk_copies: [],
      };
    }
    const currentPath = currentAppPath();
    if (!currentPath) {
      return {
        ok: true,
        supported: false,
        bundle_id: BUNDLE_ID,
        registrations: [],
        stale_registrations: [],
        disk_copies: [],
      };
    }
    const registrations = await dumpRegistrations();
    const candidates = await discoverCandidatePaths(registrations, currentPath);
    const diskCopies = [];
    let currentVersion = String(app?.getVersion?.() || "");
    for (const candidate of candidates) {
      const metadata = await readBundleMetadata(candidate);
      if (!metadata || metadata.bundle_id !== BUNDLE_ID) continue;
      if (samePath(candidate, currentPath, pathImpl)) {
        currentVersion = metadata.version || currentVersion;
        continue;
      }
      diskCopies.push({
        path: candidate,
        version: metadata.version || "未知",
        in_trash: candidate.startsWith(
          `${normalizePath(pathImpl.join(homeDir, ".Trash"), pathImpl)}${pathImpl.sep}`,
        ),
      });
    }
    const staleRegistrations = registrations.filter(
      (item) => !samePath(item.path, currentPath, pathImpl),
    );
    return {
      ok: true,
      supported: true,
      bundle_id: BUNDLE_ID,
      current_app: {
        path: normalizePath(currentPath, pathImpl),
        version: currentVersion,
      },
      registrations,
      stale_registrations: staleRegistrations,
      disk_copies: diskCopies.sort((left, right) =>
        left.path.localeCompare(right.path),
      ),
      stale_count: staleRegistrations.length,
      disk_copy_count: diskCopies.length,
    };
  }

  async function deleteValidatedCopy(candidate, currentPath) {
    const candidatePath = normalizePath(candidate.path, pathImpl);
    if (samePath(candidatePath, currentPath, pathImpl))
      throw new Error("拒绝删除当前正在运行的澜台");
    const metadata = await readBundleMetadata(candidatePath);
    if (!metadata || metadata.bundle_id !== BUNDLE_ID)
      throw new Error(`拒绝删除未验证的应用：${candidatePath}`);
    const allowedRoots = [
      "/Applications",
      pathImpl.join(homeDir, "Applications"),
      pathImpl.join(homeDir, "Downloads"),
      pathImpl.join(homeDir, ".Trash"),
    ].map((root) => `${normalizePath(root, pathImpl)}${pathImpl.sep}`);
    if (!allowedRoots.some((root) => candidatePath.startsWith(root))) {
      throw new Error(`旧副本不在允许清理的位置：${candidatePath}`);
    }
    await fsImpl.promises.rm(candidatePath, { recursive: true, force: false });
  }

  async function unregisterUntilStable(currentPath, maxPasses = 8) {
    for (let pass = 0; pass < maxPasses; pass += 1) {
      const registrations = await dumpRegistrations();
      const stale = registrations.filter(
        (item) => !samePath(item.path, currentPath, pathImpl),
      );
      for (const item of stale) await execFile(LSREGISTER, ["-u", item.path]);
      const registered = await execFile(LSREGISTER, ["-f", currentPath]);
      if (!registered.ok)
        throw new Error(registered.stderr.trim() || "无法注册当前澜台版本");
      if (stale.length === 0) return;
      await waitImpl(180);
    }
  }

  async function clean(payload = {}) {
    const before = await inspectState();
    if (!before.supported || !before.current_app) return before;
    if (payload.remove_copies === true) {
      for (const candidate of before.disk_copies)
        await deleteValidatedCopy(candidate, before.current_app.path);
    }
    await unregisterUntilStable(before.current_app.path);
    await execFile("/usr/bin/killall", ["sharedfilelistd"]);
    await execFile("/usr/bin/killall", ["Finder"]);
    await waitImpl(900);
    await unregisterUntilStable(before.current_app.path);
    const after = await inspectState();
    return {
      ...after,
      removed_copy_count:
        payload.remove_copies === true ? before.disk_copies.length : 0,
    };
  }

  function installIpcHandlers() {
    if (!ipcMain) return;
    ipcMain.handle("lantai:launch-services-state", async (event) => {
      if (!isTrustedIpcEvent(event))
        return {
          ok: false,
          supported: platform === "darwin",
          error: "untrusted_ipc_sender",
        };
      try {
        return await inspectState();
      } catch (error) {
        return {
          ok: false,
          supported: platform === "darwin",
          error: error?.message || String(error),
        };
      }
    });
    ipcMain.handle(
      "lantai:launch-services-clean",
      async (event, payload = {}) => {
        if (!isTrustedIpcEvent(event))
          return {
            ok: false,
            supported: platform === "darwin",
            error: "untrusted_ipc_sender",
          };
        try {
          return await clean(payload);
        } catch (error) {
          return {
            ok: false,
            supported: platform === "darwin",
            error: error?.message || String(error),
          };
        }
      },
    );
  }

  return { clean, inspectState, installIpcHandlers };
}

module.exports = {
  BUNDLE_ID,
  createLaunchServicesController,
  findMacAppBundlePath,
  parseLaunchServicesRegistrations,
};
