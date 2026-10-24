const {
  app,
  BrowserWindow,
  BrowserView,
  ipcMain,
  session,
} = require("electron");
const {
  createBrowserWorkbenchController,
} = require("./browserWorkbenchController.cjs");

const DEFAULT_URL = "https://mp.weixin.qq.com/";
const DEFAULT_PROFILE_ID = "wechat-work";

function readArg(name, fallback) {
  const prefix = `--${name}=`;
  const arg = process.argv.find((item) => item.startsWith(prefix));
  if (arg) return arg.slice(prefix.length);
  const envName = `LANTAI_BROWSER_WORKBENCH_${name.toUpperCase().replace(/-/g, "_")}`;
  return process.env[envName] || fallback;
}

async function main() {
  const url = readArg("url", DEFAULT_URL);
  const profileId = readArg("profile-id", DEFAULT_PROFILE_ID);
  const bridgeToken = readArg("token", "browser-workbench-manual-login-token");
  const controller = createBrowserWorkbenchController({
    app,
    BrowserWindow,
    BrowserView,
    ipcMain,
    session,
    electronDir: __dirname,
    randomToken: () => bridgeToken,
  });
  controller.installIpcHandlers();
  const bridgeBaseUrl = await controller.startBridgeServer();
  const opened = await controller.openWorkbench({
    profile_id: profileId,
    url,
  });
  if (!opened?.ok) {
    throw new Error(
      `${opened?.error_code || "browser_workbench_open_failed"}: ${
        opened?.error || "Browser Workbench could not open the requested URL."
      }`,
    );
  }

  console.log(
    JSON.stringify(
      {
        ok: true,
        message: "Browser Workbench manual login window is open.",
        profile_id: profileId,
        url,
        bridge_url: bridgeBaseUrl,
        bridge_token: bridgeToken,
        electron: process.versions.electron,
        chrome: process.versions.chrome,
      },
      null,
      2,
    ),
  );

  app.on("window-all-closed", () => {
    controller.stopBridgeServer();
    app.quit();
  });

  process.on("SIGINT", () => {
    controller.stopBridgeServer();
    app.quit();
  });
}

app.whenReady().then(() => {
  main().catch((error) => {
    console.error(error);
    app.exit(1);
  });
});
