const fs = require("fs");
const path = require("path");

const VALID_MODES = new Set(["normal", "factory", "safe"]);
const SAFE_MODE_ARG = "--lantai-safe-mode";
const FACTORY_MODE_ARG = "--lantai-factory-mode";

function readJson(fsImpl, filePath, fallback = {}) {
  try {
    return JSON.parse(fsImpl.readFileSync(filePath, "utf8"));
  } catch {
    return { ...fallback };
  }
}

function atomicWriteJson(fsImpl, filePath, payload) {
  fsImpl.mkdirSync(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.${process.pid}.tmp`;
  try {
    fsImpl.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    fsImpl.renameSync(temporary, filePath);
  } finally {
    try {
      fsImpl.rmSync(temporary, { force: true });
    } catch {
      // A failed cleanup must not prevent recovery.
    }
  }
}

function modeFromArgv(argv) {
  if (argv.includes(SAFE_MODE_ARG)) return "safe";
  if (argv.includes(FACTORY_MODE_ARG)) return "factory";
  return null;
}

function createCustomizationRecoveryController({
  app,
  dialog,
  MenuItem,
  getUserDataPath,
  getMainWindow = () => null,
  appendLog = () => {},
  fsImpl = fs,
  argv = process.argv,
  now = () => new Date().toISOString(),
  restartApp = () => {
    app.relaunch();
    app.quit();
  },
}) {
  const recoveryDir = path.join(getUserDataPath(), "recovery");
  const modePath = path.join(recoveryDir, "customization-mode.json");
  const bootHealthPath = path.join(recoveryDir, "boot-health.json");
  let currentMode = "normal";
  let bootId = `boot_${Date.now().toString(36)}`;
  let recoverLkg = false;

  function readStoredMode() {
    const payload = readJson(fsImpl, modePath);
    return VALID_MODES.has(payload.mode) ? payload.mode : "normal";
  }

  function writeMode(mode, reason = "user") {
    if (!VALID_MODES.has(mode)) {
      throw new Error(`Invalid customization recovery mode: ${mode}`);
    }
    currentMode = mode;
    atomicWriteJson(fsImpl, modePath, {
      mode,
      reason,
      updated_at: now(),
    });
    return mode;
  }

  function beginBoot() {
    const previous = readJson(fsImpl, bootHealthPath, {});
    const requested = modeFromArgv(argv) || readStoredMode();
    const previousIncomplete =
      previous.started_at &&
      previous.completed !== true &&
      previous.intentional_exit !== true;
    const consecutiveFailures = previousIncomplete
      ? Number(previous.consecutive_failures || 0) + 1
      : 0;
    currentMode = requested;
    recoverLkg = false;
    if (consecutiveFailures >= 2 && requested === "normal") {
      if (previous.lkg_recovery === true) {
        currentMode = "safe";
        writeMode("safe", "startup_loop_protection");
      } else {
        recoverLkg = true;
      }
    }
    bootId = `boot_${Date.now().toString(36)}`;
    atomicWriteJson(fsImpl, bootHealthPath, {
      boot_id: bootId,
      mode: currentMode,
      started_at: now(),
      backend_ready: false,
      renderer_ready: false,
      completed: false,
      intentional_exit: false,
      consecutive_failures: consecutiveFailures,
      lkg_recovery: recoverLkg,
    });
    appendLog(
      `[CustomizationRecovery] boot=${bootId} mode=${currentMode} failures=${consecutiveFailures}`,
    );
    return getStatus();
  }

  function updateBoot(patch) {
    const current = readJson(fsImpl, bootHealthPath, {});
    if (current.boot_id && current.boot_id !== bootId) return current;
    const next = { ...current, ...patch, updated_at: now() };
    if (next.backend_ready && next.renderer_ready) {
      next.completed = true;
      next.consecutive_failures = 0;
    }
    atomicWriteJson(fsImpl, bootHealthPath, next);
    return next;
  }

  function markBackendReady() {
    return updateBoot({ backend_ready: true });
  }

  function markRendererReady() {
    const result = updateBoot({ renderer_ready: true });
    if (result.completed && currentMode === "factory") {
      writeMode("normal", "recovery_boot_completed");
    }
    return result;
  }

  function markIntentionalExit() {
    return updateBoot({ intentional_exit: true });
  }

  function getStatus() {
    return {
      mode: currentMode,
      boot: readJson(fsImpl, bootHealthPath, {}),
      mode_path: modePath,
    };
  }

  function buildBackendEnv() {
    return {
      LANTAI_CUSTOMIZATION_MODE: currentMode,
      LANTAI_CUSTOMIZATION_BOOT_ID: bootId,
      LANTAI_RECOVERY_SAFE_MODE: currentMode === "safe" ? "1" : "0",
      LANTAI_CUSTOMIZATION_RECOVER_LKG: recoverLkg ? "1" : "0",
    };
  }

  async function confirmAndRestart(mode) {
    const safe = mode === "safe";
    const normal = mode === "normal";
    const options = {
      type: "question",
      title: normal
        ? "退出安全模式并重新启动？"
        : safe
          ? "以安全模式重新启动？"
          : "回到原生澜台？",
      message: normal
        ? "退出后会重新启用你的个性化、第三方扩展和定时自动化。"
        : safe
          ? "安全模式会隔离个性化和第三方扩展，并暂停自动执行。"
          : "所有个性化覆盖会停用，应用将以原生外观和工作方式重新启动。",
      detail: normal
        ? "请确认你已处理导致启动失败的配置。聊天、记忆、文件和配置档不受影响。"
        : "聊天、记忆、模型配置、工作区、自动化定义和配置档都不会删除。",
      buttons: [
        normal ? "退出并重新启动" : safe ? "进入安全模式" : "回到原生澜台",
        "取消",
      ],
      defaultId: 0,
      cancelId: 1,
      noLink: true,
    };
    const window = getMainWindow();
    const response =
      window && !window.isDestroyed?.()
        ? await dialog.showMessageBox(window, options)
        : await dialog.showMessageBox(options);
    if (response.response !== 0) return false;
    writeMode(mode, "user_recovery");
    markIntentionalExit();
    restartApp();
    return true;
  }

  function recoveryTemplateItems() {
    const items = [
      {
        id: "lantai-customization-factory-mode",
        label: "以原生模式重新启动…",
        click: () => void confirmAndRestart("factory"),
      },
      {
        id: "lantai-customization-safe-mode",
        label: "以安全模式重新启动…",
        click: () => void confirmAndRestart("safe"),
      },
    ];
    if (currentMode === "safe") {
      items.push({
        id: "lantai-customization-normal-mode",
        label: "退出安全模式并重新启动…",
        click: () => void confirmAndRestart("normal"),
      });
    }
    return items;
  }

  function decorateApplicationMenu(Menu) {
    const applicationMenu = Menu.getApplicationMenu?.();
    const appSubmenu = applicationMenu?.items?.[0]?.submenu;
    if (!appSubmenu || typeof appSubmenu.append !== "function") return false;
    if (applicationMenu.getMenuItemById?.("lantai-customization-factory-mode")) {
      return true;
    }
    appSubmenu.append(new MenuItem({ type: "separator" }));
    for (const item of recoveryTemplateItems()) {
      appSubmenu.append(new MenuItem(item));
    }
    return true;
  }

  return {
    beginBoot,
    buildBackendEnv,
    confirmAndRestart,
    decorateApplicationMenu,
    getStatus,
    markBackendReady,
    markIntentionalExit,
    markRendererReady,
    readStoredMode,
    recoveryTemplateItems,
    writeMode,
  };
}

module.exports = {
  FACTORY_MODE_ARG,
  SAFE_MODE_ARG,
  atomicWriteJson,
  createCustomizationRecoveryController,
  modeFromArgv,
};
