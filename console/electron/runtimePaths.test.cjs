const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const { buildPresentationRuntimeEnv } = require("./runtimePaths.cjs");

test("presentation runtime points at Electron and bundled modules in production", () => {
  const env = buildPresentationRuntimeEnv({
    isDev: false,
    electronDir: "/source/console/electron",
    resourcesPath: "/Applications/澜台.app/Contents/Resources",
    executablePath: "/Applications/澜台.app/Contents/MacOS/澜台",
  });

  assert.deepEqual(env, {
    PRESENTATION_NODE_EXECUTABLE: "/Applications/澜台.app/Contents/MacOS/澜台",
    PRESENTATION_NODE_MODULES: path.join(
      "/Applications/澜台.app/Contents/Resources",
      "app.asar.unpacked",
      "node_modules",
    ),
    PRESENTATION_NODE_MODE: "electron",
  });
});

test("presentation runtime resolves repository node_modules in development", () => {
  const env = buildPresentationRuntimeEnv({
    isDev: true,
    electronDir: "/source/console/electron",
    resourcesPath: "/unused",
    executablePath:
      "/source/console/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron",
  });

  assert.equal(
    env.PRESENTATION_NODE_MODULES,
    path.resolve("/source/console/electron", "..", "node_modules"),
  );
});
