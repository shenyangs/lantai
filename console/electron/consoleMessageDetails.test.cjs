const assert = require("node:assert/strict");
const test = require("node:test");

const { normalizeConsoleMessage } = require("./consoleMessageDetails.cjs");

test("normalizes Electron 42 console-message event details", () => {
  assert.deepEqual(
    normalizeConsoleMessage({
      level: "error",
      message: "renderer failed",
      lineNumber: 17,
      sourceId: "app://renderer.js",
    }),
    {
      level: 3,
      levelName: "error",
      message: "renderer failed",
      line: 17,
      sourceId: "app://renderer.js",
    },
  );
});

test("keeps compatibility with legacy positional console-message arguments", () => {
  assert.deepEqual(
    normalizeConsoleMessage({}, 2, "legacy warning", 9, "legacy.js"),
    {
      level: 2,
      levelName: "warning",
      message: "legacy warning",
      line: 9,
      sourceId: "legacy.js",
    },
  );
});
