const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const {
  BUNDLE_ID,
  createLaunchServicesController,
  findMacAppBundlePath,
  parseLaunchServicesRegistrations,
} = require("./launchServicesController.cjs");

const LSREGISTER =
  "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister";

function renderDump(registrations) {
  return [...registrations]
    .map(
      (appPath, index) => `
--------------------------------------------------------------------------------
path: ${appPath} (0x${(index + 1).toString(16)})
identifier: ${BUNDLE_ID}
versionString: ${appPath.includes("old") ? "1.0.43" : "1.0.48"}
`,
    )
    .join("");
}

test("finds an enclosing macOS app bundle", () => {
  assert.equal(
    findMacAppBundlePath("/Users/example/Downloads/澜台.app/Contents/MacOS/澜台"),
    "/Users/example/Downloads/澜台.app",
  );
  assert.equal(findMacAppBundlePath("/usr/local/bin/lantai"), null);
});

test("parses only the exact Lantai bundle identifier", () => {
  const dump = `${renderDump(["/Applications/old-澜台.app"])}
--------------------------------------------------------------------------------
path: /Applications/helper.app (0xff)
identifier: com.lantai.app.helper
versionString: 1.0.43
`;
  assert.deepEqual(parseLaunchServicesRegistrations(dump), [
    { path: "/Applications/old-澜台.app", version: "1.0.43" },
  ]);
});

test("removes only verified stale copies, refreshes Finder, and converges", async () => {
  const homeDir = "/Users/test";
  const current = `${homeDir}/Downloads/澜台.app`;
  const oldApplication = "/Applications/old-澜台.app";
  const oldTrash = `${homeDir}/.Trash/old-澜台.app`;
  const metadata = new Map([
    [current, { bundle_id: BUNDLE_ID, version: "1.0.48" }],
    [oldApplication, { bundle_id: BUNDLE_ID, version: "1.0.43" }],
    [oldTrash, { bundle_id: BUNDLE_ID, version: "1.0.43" }],
  ]);
  const registrations = new Set([current, oldApplication, oldTrash]);
  const removed = [];
  const commands = [];

  const directoryEntries = new Map([
    ["/Applications", ["old-澜台.app"]],
    [`${homeDir}/Applications`, []],
    [`${homeDir}/Downloads`, ["澜台.app"]],
    [`${homeDir}/.Trash`, ["old-澜台.app"]],
  ]);
  const fsImpl = {
    existsSync(plistPath) {
      const appPath = path.dirname(path.dirname(plistPath));
      return metadata.has(appPath);
    },
    promises: {
      async readdir(directory) {
        return (directoryEntries.get(directory) || [])
          .map((name) => path.join(directory, name))
          .filter((appPath) => metadata.has(appPath))
          .map((appPath) => ({
            name: path.basename(appPath),
            isDirectory: () => true,
          }));
      },
      async rm(appPath) {
        removed.push(appPath);
        metadata.delete(appPath);
        registrations.delete(appPath);
      },
    },
  };
  function execFileImpl(file, args, _options, callback) {
    commands.push([file, ...args]);
    if (file === LSREGISTER && args[0] === "-dump") {
      callback(null, renderDump(registrations), "");
      return;
    }
    if (file === LSREGISTER && args[0] === "-u") {
      registrations.delete(args[1]);
    }
    if (file === LSREGISTER && args[0] === "-f") {
      registrations.add(args[1]);
    }
    callback(null, "", "");
  }

  const controller = createLaunchServicesController({
    app: {
      isPackaged: true,
      getPath: () => `${current}/Contents/MacOS/澜台`,
      getVersion: () => "1.0.48",
    },
    platform: "darwin",
    homeDir,
    fsImpl,
    execFileImpl,
    readBundleMetadataImpl: async (appPath) => metadata.get(appPath) || null,
    waitImpl: async () => {},
  });

  const before = await controller.inspectState();
  assert.deepEqual(
    before.disk_copies.map((copy) => copy.path),
    [oldApplication, oldTrash],
  );
  assert.equal(before.disk_copies[1].in_trash, true);

  const after = await controller.clean({ remove_copies: true });
  assert.deepEqual(removed, [oldApplication, oldTrash]);
  assert.equal(after.removed_copy_count, 2);
  assert.deepEqual(after.disk_copies, []);
  assert.deepEqual(after.stale_registrations, []);
  assert.equal(metadata.has(current), true);
  assert.equal(
    commands.some(
      ([file, name]) => file === "/usr/bin/killall" && name === "Finder",
    ),
    true,
  );
});

test("does not expose cleanup to an untrusted renderer", async () => {
  const handlers = new Map();
  const controller = createLaunchServicesController({
    app: {
      isPackaged: true,
      getPath: () => "/tmp/澜台.app/Contents/MacOS/澜台",
    },
    ipcMain: { handle: (name, callback) => handlers.set(name, callback) },
    isTrustedIpcEvent: () => false,
    platform: "darwin",
  });
  controller.installIpcHandlers();
  assert.equal(handlers.size, 2);
  assert.deepEqual(await handlers.get("lantai:launch-services-clean")({}), {
    ok: false,
    supported: true,
    error: "untrusted_ipc_sender",
  });
});
