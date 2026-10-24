#!/usr/bin/env node

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { downloadArtifact } = require("@electron/get");

const electronRoot = path.dirname(require.resolve("electron/package.json"));
const electronPackage = require(path.join(electronRoot, "package.json"));
const distPath = path.join(electronRoot, "dist");
const pathFile = path.join(electronRoot, "path.txt");
const platform = process.env.ELECTRON_INSTALL_PLATFORM || process.env.npm_config_platform || process.platform;
const arch = process.env.ELECTRON_INSTALL_ARCH || process.env.npm_config_arch || process.arch;

function executableRelativePath() {
  if (platform === "darwin" || platform === "mas") return "Electron.app/Contents/MacOS/Electron";
  if (platform === "win32") return "electron.exe";
  return "electron";
}

function runtimeIsComplete(relativeExecutable) {
  if (!fs.existsSync(path.join(distPath, relativeExecutable))) return false;
  if (platform === "darwin" || platform === "mas") {
    return fs.existsSync(
      path.join(
        distPath,
        "Electron.app/Contents/Frameworks/Electron Framework.framework/Electron Framework",
      ),
    );
  }
  return true;
}

async function extractArchive(zipPath) {
  if (platform === "darwin" || platform === "mas") {
    const result = spawnSync("/usr/bin/ditto", ["-x", "-k", zipPath, distPath], {
      stdio: "inherit",
    });
    if (result.status !== 0) throw new Error(`ditto failed with exit code ${result.status}`);
    return;
  }
  const extract = require("extract-zip");
  await extract(zipPath, { dir: distPath });
}

async function main() {
  const relativeExecutable = executableRelativePath();
  if (runtimeIsComplete(relativeExecutable)) {
    fs.writeFileSync(pathFile, relativeExecutable, "utf8");
    console.log(`Electron runtime already complete: ${electronPackage.version} ${platform}-${arch}`);
    return;
  }

  const zipPath = await downloadArtifact({
    version: electronPackage.version,
    artifactName: "electron",
    platform,
    arch,
    checksums: require(path.join(electronRoot, "checksums.json")),
    cacheRoot: process.env.electron_config_cache,
  });

  const stalePath = `${distPath}.partial-${Date.now()}`;
  if (fs.existsSync(distPath)) fs.renameSync(distPath, stalePath);
  fs.mkdirSync(distPath, { recursive: true });
  try {
    await extractArchive(zipPath);
    if (!runtimeIsComplete(relativeExecutable)) {
      throw new Error("archive extraction completed without a usable Electron runtime");
    }
    fs.writeFileSync(pathFile, relativeExecutable, "utf8");
  } catch (error) {
    fs.rmSync(distPath, { recursive: true, force: true });
    if (fs.existsSync(stalePath)) fs.renameSync(stalePath, distPath);
    throw error;
  }
  if (fs.existsSync(stalePath)) fs.rmSync(stalePath, { recursive: true, force: true });
  console.log(`Electron runtime restored: ${electronPackage.version} ${platform}-${arch}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack : String(error));
  process.exitCode = 1;
});
