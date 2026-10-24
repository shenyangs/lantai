const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

if (process.platform !== "darwin") {
  console.log("Mac Use helper is macOS-only; skipping native build.");
  process.exit(0);
}

const consoleRoot = path.resolve(__dirname, "..");
const sourceRoot = path.join(
  consoleRoot,
  "electron",
  "native",
  "mac-use-helper",
);
const appRoot = path.join(
  consoleRoot,
  "electron-dist",
  "mac-use",
  "Lantai Mac Use Helper.app",
);
const executableDir = path.join(appRoot, "Contents", "MacOS");
const executablePath = path.join(executableDir, "lantai-mac-use-helper");
const temporaryRoot = fs.mkdtempSync(
  path.join(process.env.TMPDIR || "/tmp", "lantai-mac-use-build-"),
);

function run(command, args) {
  const moduleCache = path.join(temporaryRoot, "module-cache");
  fs.mkdirSync(moduleCache, { recursive: true });
  const result = spawnSync(command, args, {
    stdio: "inherit",
    env: {
      ...process.env,
      CLANG_MODULE_CACHE_PATH: moduleCache,
      SWIFT_MODULECACHE_PATH: moduleCache,
    },
  });
  if (result.status !== 0) {
    throw new Error(`${command} failed with exit code ${result.status}`);
  }
}

try {
  fs.rmSync(appRoot, { recursive: true, force: true });
  fs.mkdirSync(executableDir, { recursive: true });
  const binaries = [];
  for (const arch of ["arm64", "x86_64"]) {
    const output = path.join(temporaryRoot, `lantai-mac-use-helper-${arch}`);
    run("xcrun", [
      "swiftc",
      path.join(sourceRoot, "main.swift"),
      "-O",
      "-target",
      `${arch}-apple-macos14.0`,
      "-framework",
      "AppKit",
      "-framework",
      "ApplicationServices",
      "-framework",
      "CoreGraphics",
      "-o",
      output,
    ]);
    binaries.push(output);
  }
  run("xcrun", ["lipo", "-create", ...binaries, "-output", executablePath]);
  fs.copyFileSync(
    path.join(sourceRoot, "Info.plist"),
    path.join(appRoot, "Contents", "Info.plist"),
  );
  const appVersion = JSON.parse(
    fs.readFileSync(path.join(consoleRoot, "package.json"), "utf8"),
  ).version;
  for (const key of ["CFBundleShortVersionString", "CFBundleVersion"]) {
    run("/usr/libexec/PlistBuddy", [
      "-c",
      `Set :${key} ${appVersion}`,
      path.join(appRoot, "Contents", "Info.plist"),
    ]);
  }
  fs.chmodSync(executablePath, 0o755);
  run("codesign", ["--force", "--sign", "-", appRoot]);
  console.log(`Built Mac Use helper: ${appRoot}`);
} finally {
  fs.rmSync(temporaryRoot, { recursive: true, force: true });
}
