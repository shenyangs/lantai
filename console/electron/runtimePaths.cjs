const path = require("path");
const {
  buildBackendLaunchPlan,
  getProjectRoot,
} = require("./backendProcess.cjs");

function getWindowStatePath(userDataPath, fileName) {
  return path.join(userDataPath, fileName);
}

function buildPresentationRuntimeEnv({
  isDev,
  electronDir,
  resourcesPath,
  executablePath,
}) {
  return {
    PRESENTATION_NODE_EXECUTABLE: executablePath,
    PRESENTATION_NODE_MODULES: isDev
      ? path.resolve(electronDir, "..", "node_modules")
      : path.join(resourcesPath, "app.asar.unpacked", "node_modules"),
    PRESENTATION_NODE_MODE: "electron",
  };
}

function getTrayIconPath({ isDev, electronDir, resourcesPath }) {
  return isDev
    ? path.join(
        getProjectRoot(electronDir),
        "console/public/lantai-trayTemplate.png",
      )
    : path.join(resourcesPath, "lantai-trayTemplate.png");
}

module.exports = {
  buildBackendLaunchPlan,
  buildPresentationRuntimeEnv,
  getProjectRoot,
  getTrayIconPath,
  getWindowStatePath,
};
