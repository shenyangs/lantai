const fs = require("fs");
const path = require("path");

const APP_ICON_CHANNELS = Object.freeze({
  get: "lantai:app-icon-get",
  select: "lantai:app-icon-select",
  importCustom: "lantai:app-icon-import-custom",
});

const BUILTIN_APP_ICONS = Object.freeze([
  { id: "brush-classic", name: "经典澜台", group: "brush" },
  { id: "brush-cobalt-glass", name: "冰蓝墨", group: "brush" },
  { id: "brush-titanium", name: "钛色墨", group: "brush" },
  { id: "brush-risograph-blue", name: "蓝印", group: "brush" },
  { id: "ink-horizon", name: "墨潮地平线", group: "signature" },
  { id: "quiet-tide", name: "静潮折台", group: "signature" },
  { id: "paper-window", name: "纸上澜窗", group: "signature" },
  { id: "incision", name: "纸上切口", group: "signature" },
]);

const APP_ICON_DESIGN_SPEC = `澜台自定义应用图标规范

1. 交付一张独立图标，不要交付联络表、带编号总览或手机模型图。
2. 画布必须是 1024 × 1024 px、sRGB、PNG；图形在画布中居中。
3. 应用图标的外形为圆角矩形，建议圆角半径约为边长的 18%–22%，四角外保持透明。
4. 关键符号、文字和边缘细节距四边至少 10%，避免被 Dock 的视觉缩放吃掉。
5. 视觉应保持澜台的核心气质：安静、可靠、清楚；优先使用水面、平台、墨迹、纸张与留白等母题。
6. 如果使用“澜台”、“澜”或“LANTAI”，必须逐字校对；不能用看似正确的伪文字。
7. 交付前同时在 64 px 和 32 px 预览，确认轮廓、对比和核心识别仍然清楚。
8. 导出后可在“设置 → 创新实验 → 主题与图标 → 导入自定义图标”中使用。`;

function getDockIconPath({ isDev, electronDir, resourcesPath }) {
  return isDev
    ? path.resolve(electronDir, "../build/icon.icns")
    : path.join(resourcesPath, "icon.icns");
}

function getClassicAppIconPath({ isDev, electronDir, resourcesPath }) {
  return isDev
    ? path.resolve(electronDir, "../public/lantai-mark.png")
    : path.join(resourcesPath, "lantai-mark.png");
}

function getBundledAppIconPath({ id, isDev, electronDir, resourcesPath }) {
  const root = isDev
    ? path.resolve(electronDir, "../public/app-icons")
    : path.join(resourcesPath, "console", "dist", "app-icons");
  return path.join(root, `${id}.png`);
}

function createDockIconController({
  app,
  nativeImage,
  isDev,
  electronDir,
  getResourcesPath,
  appendLog = () => {},
  fsObject = fs,
  processObject = process,
}) {
  const builtinIds = new Set(BUILTIN_APP_ICONS.map((icon) => icon.id));

  function getAppearanceDirectory() {
    if (typeof app.getPath !== "function") return "";
    return path.join(app.getPath("userData"), "appearance");
  }

  function getPreferencePath() {
    const directory = getAppearanceDirectory();
    return directory ? path.join(directory, "app-icon.json") : "";
  }

  function getCustomIconPath() {
    const directory = getAppearanceDirectory();
    return directory ? path.join(directory, "custom-app-icon.png") : "";
  }

  function isValidSelection(id) {
    return id === "default" || id === "custom" || builtinIds.has(id);
  }

  function loadSelection() {
    const preferencePath = getPreferencePath();
    if (!preferencePath || typeof fsObject.readFileSync !== "function") {
      return "default";
    }
    try {
      const payload = JSON.parse(fsObject.readFileSync(preferencePath, "utf8"));
      const id = String(payload?.selectedId || "");
      if (isValidSelection(id)) return id;
    } catch {
      // Missing or malformed preferences fall back to the shipped icon.
    }
    return "default";
  }

  function writeFileAtomically(targetPath, contents) {
    const directory = path.dirname(targetPath);
    const temporaryPath = `${targetPath}.${processObject.pid || "tmp"}.tmp`;
    fsObject.mkdirSync(directory, { recursive: true });
    try {
      fsObject.writeFileSync(temporaryPath, contents, { mode: 0o600 });
      fsObject.renameSync(temporaryPath, targetPath);
    } finally {
      if (
        typeof fsObject.existsSync === "function" &&
        fsObject.existsSync(temporaryPath)
      ) {
        fsObject.unlinkSync(temporaryPath);
      }
    }
  }

  function saveSelection(selectedId) {
    const preferencePath = getPreferencePath();
    if (!preferencePath) return;
    writeFileAtomically(
      preferencePath,
      `${JSON.stringify({ selectedId, updatedAt: new Date().toISOString() }, null, 2)}\n`,
    );
  }

  function resolveIconPath(selectedId) {
    if (selectedId === "custom") return getCustomIconPath();
    if (builtinIds.has(selectedId)) {
      return getBundledAppIconPath({
        id: selectedId,
        isDev,
        electronDir,
        resourcesPath: getResourcesPath(),
      });
    }
    return getClassicAppIconPath({
      isDev,
      electronDir,
      resourcesPath: getResourcesPath(),
    });
  }

  function applyDockIcon(requestedId) {
    if (
      processObject.platform !== "darwin" ||
      !app.dock ||
      typeof app.dock.setIcon !== "function"
    ) {
      return { applied: false, reason: "dock_unavailable" };
    }

    let selectedId = requestedId || loadSelection();
    if (!isValidSelection(selectedId)) selectedId = "default";
    let iconPath = resolveIconPath(selectedId);
    if (selectedId === "custom" && !fsObject.existsSync(iconPath)) {
      selectedId = "default";
      iconPath = resolveIconPath(selectedId);
    }
    if (!fsObject.existsSync(iconPath)) {
      appendLog(`[Dock Icon] Missing icon: ${iconPath}`);
      return { applied: false, reason: "icon_missing", iconPath, selectedId };
    }

    try {
      const icon = nativeImage.createFromPath(iconPath);
      if (!icon || (typeof icon.isEmpty === "function" && icon.isEmpty())) {
        appendLog(`[Dock Icon] Invalid icon: ${iconPath}`);
        return { applied: false, reason: "icon_invalid", iconPath, selectedId };
      }
      app.dock.setIcon(icon);
      appendLog(`[Dock Icon] Applied ${selectedId}: ${iconPath}`);
      return { applied: true, reason: "applied", iconPath };
    } catch (error) {
      appendLog(
        `[Dock Icon] Failed: ${iconPath} (${error?.message || String(error)})`,
      );
      return { applied: false, reason: "set_failed", iconPath, selectedId };
    }
  }

  function getCustomPreview() {
    const customPath = getCustomIconPath();
    if (!customPath || !fsObject.existsSync(customPath)) return "";
    try {
      const image = nativeImage.createFromPath(customPath);
      if (!image || image.isEmpty()) return "";
      return image
        .resize({ width: 256, height: 256, quality: "best" })
        .toDataURL();
    } catch {
      return "";
    }
  }

  function getState(overrides = {}) {
    const persistedId = loadSelection();
    const customPreviewUrl = getCustomPreview();
    const selectedId =
      persistedId === "custom" && !customPreviewUrl ? "default" : persistedId;
    return {
      ok: true,
      supported:
        processObject.platform === "darwin" &&
        Boolean(app.dock && typeof app.dock.setIcon === "function"),
      selectedId,
      defaultIcon: {
        id: "default",
        name: "黑白澜台",
        group: "default",
        previewUrl: "/lantai-mark.png",
      },
      builtinIcons: BUILTIN_APP_ICONS.map((icon) => ({
        ...icon,
        previewUrl: `/app-icons/${icon.id}.png`,
      })),
      customIcon: {
        id: "custom",
        name: "我的图标",
        group: "custom",
        available: Boolean(customPreviewUrl),
        previewUrl: customPreviewUrl,
      },
      designSpec: APP_ICON_DESIGN_SPEC,
      ...overrides,
    };
  }

  function selectAppIcon(id) {
    const selectedId = String(id || "");
    if (!isValidSelection(selectedId)) {
      return getState({ ok: false, error: "invalid_icon_id" });
    }
    if (selectedId === "custom" && !fsObject.existsSync(getCustomIconPath())) {
      return getState({ ok: false, error: "custom_icon_missing" });
    }
    const result = applyDockIcon(selectedId);
    if (!result.applied) {
      return getState({ ok: false, error: result.reason });
    }
    try {
      saveSelection(selectedId);
      return getState({ selectedId });
    } catch (error) {
      appendLog(
        `[Dock Icon] Failed to persist selection (${error?.message || String(error)})`,
      );
      return getState({ ok: false, error: "preference_write_failed" });
    }
  }

  async function importCustomAppIcon({ dialog, getMainWindow }) {
    const result = await dialog.showOpenDialog(getMainWindow?.() || undefined, {
      title: "导入自定义应用图标",
      properties: ["openFile"],
      filters: [
        {
          name: "图像",
          extensions: ["png", "jpg", "jpeg", "webp", "icns"],
        },
      ],
    });
    if (result.canceled || !result.filePaths?.[0]) {
      return getState({ canceled: true });
    }

    const sourcePath = result.filePaths[0];
    try {
      const stats = fsObject.statSync(sourcePath);
      if (stats.size > 20 * 1024 * 1024) {
        return getState({ ok: false, error: "icon_too_large" });
      }
      const source = nativeImage.createFromPath(sourcePath);
      if (!source || source.isEmpty()) {
        return getState({ ok: false, error: "icon_invalid" });
      }
      const size = source.getSize();
      if (size.width !== size.height) {
        return getState({ ok: false, error: "icon_not_square" });
      }
      if (size.width < 1024) {
        return getState({ ok: false, error: "icon_too_small" });
      }
      const normalized = source.resize({
        width: 1024,
        height: 1024,
        quality: "best",
      });
      writeFileAtomically(getCustomIconPath(), normalized.toPNG());
      const applied = applyDockIcon("custom");
      if (!applied.applied) {
        return getState({ ok: false, error: applied.reason });
      }
      saveSelection("custom");
      return getState({ selectedId: "custom" });
    } catch (error) {
      appendLog(
        `[Dock Icon] Import failed (${error?.message || String(error)})`,
      );
      return getState({ ok: false, error: "icon_import_failed" });
    }
  }

  function installIpcHandlers({
    ipcMain,
    dialog,
    getMainWindow,
    isTrustedIpcEvent = () => true,
  }) {
    const register = (channel, handler) => {
      if (typeof ipcMain.removeHandler === "function") {
        ipcMain.removeHandler(channel);
      }
      ipcMain.handle(channel, async (event, payload = {}) => {
        if (!isTrustedIpcEvent(event)) {
          return { ok: false, error: "untrusted_ipc_sender" };
        }
        return handler(payload);
      });
    };
    register(APP_ICON_CHANNELS.get, () => getState());
    register(APP_ICON_CHANNELS.select, (payload) => selectAppIcon(payload?.id));
    register(APP_ICON_CHANNELS.importCustom, () =>
      importCustomAppIcon({ dialog, getMainWindow }),
    );
  }

  return {
    applyDockIcon,
    getState,
    importCustomAppIcon,
    installIpcHandlers,
    selectAppIcon,
  };
}

module.exports = {
  APP_ICON_CHANNELS,
  APP_ICON_DESIGN_SPEC,
  BUILTIN_APP_ICONS,
  createDockIconController,
  getBundledAppIconPath,
  getClassicAppIconPath,
  getDockIconPath,
};
