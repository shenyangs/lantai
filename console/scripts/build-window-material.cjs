const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
if (process.platform !== "darwin") process.exit(0);
const root = path.resolve(__dirname, "..");
const sdk = execFileSync("xcrun", ["--sdk", "macosx", "--show-sdk-path"], {
  encoding: "utf8",
}).trim();
const version = execFileSync(
  "xcrun",
  ["--sdk", "macosx", "--show-sdk-version"],
  { encoding: "utf8" },
).trim();
if (Number(version.split(".")[0]) < 26)
  throw new Error("Window material requires the macOS 26 SDK or newer");
const headers = require("node-api-headers").include_dir;
const output = path.join(root, "electron/native/bin/window-material.node");
fs.mkdirSync(path.dirname(output), { recursive: true });
execFileSync(
  "xcrun",
  [
    "clang++",
    "-std=c++17",
    "-fobjc-arc",
    "-shared",
    "-undefined",
    "dynamic_lookup",
    "-isysroot",
    sdk,
    "-mmacosx-version-min=14.0",
    "-arch",
    "arm64",
    "-arch",
    "x86_64",
    "-I",
    headers,
    "-framework",
    "AppKit",
    path.join(root, "electron/native/window-material.mm"),
    "-o",
    output,
  ],
  { stdio: "inherit" },
);
console.log(
  "Built universal macOS window material bridge (public AppKit, Node-API).",
);
