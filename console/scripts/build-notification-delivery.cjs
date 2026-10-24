const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

if (process.platform !== "darwin") process.exit(0);
const root = path.resolve(__dirname, "..");
const sdk = execFileSync("xcrun", ["--sdk", "macosx", "--show-sdk-path"], {
  encoding: "utf8",
}).trim();
const headers = require("node-api-headers").include_dir;
const output = path.join(
  root,
  "electron/native/bin/notification-delivery.node",
);
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
    "Foundation",
    "-framework",
    "UserNotifications",
    path.join(root, "electron/native/notification-delivery.mm"),
    "-o",
    output,
  ],
  { stdio: "inherit" },
);
console.log(
  "Built universal macOS notification delivery reader (public UserNotifications, Node-API).",
);
