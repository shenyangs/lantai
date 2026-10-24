const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const { getTrayIconPath } = require("./runtimePaths.cjs");
const {
  createTrayController,
  getRetinaTrayIconPath,
} = require("./trayController.cjs");

function makeNativeImage() {
  const images = [];
  return {
    images,
    createFromPath(iconPath) {
      const image = {
        iconPath,
        resizeOptions: null,
        templateImage: false,
        representations: [],
        resize(options) {
          this.resizeOptions = options;
          return this;
        },
        addRepresentation(options) {
          this.representations.push(options);
        },
        setTemplateImage(value) {
          this.templateImage = value;
        },
        toDataURL() {
          return `data:image/png;base64,${Buffer.from(iconPath).toString(
            "base64",
          )}`;
        },
      };
      images.push(image);
      return image;
    },
    createEmpty() {
      return { empty: true };
    },
  };
}

function makeController({ platform = "darwin" } = {}) {
  const nativeImage = makeNativeImage();
  class FakeTray {
    constructor(icon) {
      this.icon = icon;
      this.events = new Map();
      FakeTray.instances.push(this);
    }

    setToolTip(value) {
      this.toolTip = value;
    }

    setContextMenu(value) {
      this.contextMenu = value;
    }

    on(eventName, handler) {
      this.events.set(eventName, handler);
    }
  }
  FakeTray.instances = [];

  const controller = createTrayController({
    app: { quit: () => {} },
    Tray: FakeTray,
    Menu: {
      buildFromTemplate(template) {
        return { template };
      },
    },
    nativeImage,
    isDev: true,
    electronDir: __dirname,
    getResourcesPath: () => "/Applications/Lantai.app/Contents/Resources",
    getMainWindow: () => null,
    createWindow: () => {},
    fsModule: { existsSync: () => true },
    processObject: { platform },
  });

  return { controller, nativeImage, FakeTray };
}

test("tray icon path uses the dedicated flat template asset", () => {
  assert.equal(
    getTrayIconPath({
      isDev: true,
      electronDir: __dirname,
      resourcesPath: "/unused/resources",
    }),
    path.resolve(__dirname, "../..", "console/public/lantai-trayTemplate.png"),
  );
  assert.equal(
    getTrayIconPath({
      isDev: false,
      electronDir: __dirname,
      resourcesPath: "/Applications/Lantai.app/Contents/Resources",
    }),
    "/Applications/Lantai.app/Contents/Resources/lantai-trayTemplate.png",
  );
});

test("macOS tray icon loads a retina template representation", () => {
  const { controller, nativeImage, FakeTray } = makeController({
    platform: "darwin",
  });

  controller.createTray();

  const tray = FakeTray.instances[0];
  assert.equal(tray.toolTip, "澜台");
  assert.equal(nativeImage.images[0].resizeOptions, null);
  assert.equal(nativeImage.images[0].templateImage, true);
  assert.equal(
    nativeImage.images[0].iconPath.endsWith(
      "console/public/lantai-trayTemplate.png",
    ),
    true,
  );
  assert.equal(
    nativeImage.images[1].iconPath.endsWith(
      "console/public/lantai-trayTemplate@2x.png",
    ),
    true,
  );
  assert.deepEqual(nativeImage.images[0].representations, [
    {
      scaleFactor: 2,
      dataURL: nativeImage.images[1].toDataURL(),
    },
  ]);
  assert.equal(tray.icon, nativeImage.images[0]);
});

test("non-macOS tray icon keeps regular image rendering", () => {
  const { controller, nativeImage } = makeController({ platform: "linux" });

  controller.createTray();

  assert.equal(nativeImage.images[0].templateImage, false);
  assert.deepEqual(nativeImage.images[0].resizeOptions, {
    width: 18,
    height: 18,
  });
});

test("retina tray icon path follows macOS asset naming", () => {
  assert.equal(
    getRetinaTrayIconPath("/Applications/Lantai.app/icon.png"),
    "/Applications/Lantai.app/icon@2x.png",
  );
});
