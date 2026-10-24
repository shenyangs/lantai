const path = require("node:path");

/** Only the trusted main renderer can choose a material; no native handles cross IPC. */
function createWindowMaterialController({
  ipcMain,
  nativeTheme,
  getMainWindow,
  isTrustedIpcEvent,
  platform = process.platform,
  loadNative = () =>
    require(path.join(__dirname, "native/bin/window-material.node")),
  appendLog = () => {},
}) {
  let binding;
  let attempted = false;
  let trackedWindow;
  let requested = { enabled: false, dark: false };
  let applied;
  function nativeBinding() {
    if (!attempted) {
      attempted = true;
      try {
        binding = loadNative();
      } catch {
        appendLog(
          "[window-material] Native glass unavailable; using system vibrancy.",
        );
      }
    }
    return binding;
  }
  function apply() {
    const win = getMainWindow();
    if (!win || win.isDestroyed()) return "solid";
    const reduced =
      nativeTheme.prefersReducedTransparency ||
      nativeTheme.shouldUseHighContrastColors;
    const enabled = requested.enabled && !reduced;
    if (platform !== "darwin") return enabled ? "fallback" : "solid";
    if (
      applied?.win === win &&
      applied.enabled === enabled &&
      applied.dark === requested.dark
    ) {
      return applied.mode;
    }
    // Remove Electron's visual-effect sibling before installing the AppKit material.
    win.setVibrancy(null);
    let native = false;
    try {
      native = Boolean(
        nativeBinding()?.setMaterial(
          win.getNativeWindowHandle(),
          enabled,
          requested.dark,
        ),
      );
    } catch {
      appendLog(
        "[window-material] Could not apply native glass; using system vibrancy.",
      );
    }
    win.setBackgroundColor(
      enabled ? "#00000000" : requested.dark ? "#202124" : "#faf9f7",
    );
    const mode = !enabled ? "solid" : native ? "native" : "vibrancy";
    if (mode === "vibrancy") win.setVibrancy("sidebar");
    applied = { win, enabled, dark: requested.dark, mode };
    return mode;
  }
  function changed() {
    const previous = applied;
    const mode = apply();
    if (previous === applied) return;
    const win = getMainWindow();
    if (win && !win.isDestroyed() && !win.webContents.isDestroyed()) {
      win.webContents.send("window-material:changed", mode);
    }
  }
  function track(win) {
    if (trackedWindow === win) return;
    nativeTheme.removeListener("updated", changed);
    trackedWindow = win;
    nativeTheme.on("updated", changed);
    win.once("closed", () => {
      if (trackedWindow === win) {
        nativeTheme.removeListener("updated", changed);
        trackedWindow = undefined;
        applied = undefined;
      }
    });
  }
  function installIpcHandlers() {
    ipcMain.handle("window-material:set", (event, value) => {
      if (!isTrustedIpcEvent(event))
        throw new Error("Untrusted window material request");
      if (
        !value ||
        typeof value.enabled !== "boolean" ||
        typeof value.dark !== "boolean"
      ) {
        throw new Error("Invalid window material request");
      }
      const win = getMainWindow();
      if (!win || win.isDestroyed()) return "solid";
      requested = { enabled: value.enabled, dark: value.dark };
      track(win);
      return apply();
    });
  }
  return { installIpcHandlers };
}
module.exports = { createWindowMaterialController };
