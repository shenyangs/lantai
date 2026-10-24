const fs = require("fs");
const { getTrayIconPath } = require("./runtimePaths.cjs");

function getRetinaTrayIconPath(iconPath) {
  return iconPath.replace(/\.png$/, "@2x.png");
}

function createTrayController({
  app,
  Tray,
  Menu,
  nativeImage,
  isDev,
  electronDir,
  getResourcesPath,
  getMainWindow,
  createWindow,
  getRecoveryMenuItems = () => [],
  fsModule = fs,
  processObject = process,
}) {
  let tray = null;

  function createTrayIcon() {
    const iconPath = getTrayIconPath({
      isDev,
      electronDir,
      resourcesPath: getResourcesPath(),
    });

    try {
      let trayIcon = nativeImage.createFromPath(iconPath);
      if (processObject.platform === "darwin") {
        const retinaIconPath = getRetinaTrayIconPath(iconPath);
        if (
          fsModule.existsSync(retinaIconPath) &&
          typeof trayIcon.addRepresentation === "function"
        ) {
          const retinaIcon = nativeImage.createFromPath(retinaIconPath);
          trayIcon.addRepresentation({
            scaleFactor: 2,
            dataURL: retinaIcon.toDataURL(),
          });
        }
        if (typeof trayIcon.setTemplateImage === "function") {
          trayIcon.setTemplateImage(true);
        }
      } else {
        trayIcon = trayIcon.resize({ width: 18, height: 18 });
      }
      return trayIcon;
    } catch {
      return nativeImage.createEmpty();
    }
  }

  function showFromMenu() {
    const mainWindow = getMainWindow();
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.show();
      mainWindow.focus();
      return;
    }
    createWindow();
  }

  function handleClick() {
    const mainWindow = getMainWindow();
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isVisible()) {
        mainWindow.focus();
      } else {
        mainWindow.show();
      }
      return;
    }
    createWindow();
  }

  function createTray() {
    tray = new Tray(createTrayIcon());
    tray.setToolTip("澜台");

    const contextMenu = Menu.buildFromTemplate([
      {
        label: "显示澜台",
        click: showFromMenu,
      },
      ...getRecoveryMenuItems(),
      { type: "separator" },
      {
        label: "退出",
        click: () => {
          app.quit();
        },
      },
    ]);

    tray.setContextMenu(contextMenu);
    tray.on("click", handleClick);
    return tray;
  }

  function getTray() {
    return tray;
  }

  return {
    createTray,
    getTray,
  };
}

module.exports = {
  createTrayController,
  getRetinaTrayIconPath,
};
