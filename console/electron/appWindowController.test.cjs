const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createAppWindowController } = require("./appWindowController.cjs");

function makeController({
  platform,
  appendBackendLog = () => {},
  getApiBaseUrl = () => "http://127.0.0.1:19090",
}) {
  const userDataPath = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-window-test-"),
  );
  class FakeBrowserWindow {
    constructor(options) {
      this.options = options;
      this.loadedUrl = "";
      this.showCount = 0;
      this.focusCount = 0;
      this.events = new Map();
      const webContentsEvents = new Map();
      this.webContents = {
        id: FakeBrowserWindow.instances.length + 1,
        getURL: () => this.loadedUrl,
        isDestroyed: () => false,
        mainFrame: null,
        events: webContentsEvents,
        on: (eventName, handler) => {
          webContentsEvents.set(eventName, handler);
        },
        setWindowOpenHandler: (handler) => {
          this.webContents.windowOpenHandler = handler;
        },
        openDevTools: () => {},
      };
      FakeBrowserWindow.instances.push(this);
    }

    loadURL(url) {
      this.loadedUrl = url;
      this.webContents.mainFrame = {
        url, parent: null, processId: 10, routingId: (this.webContents.mainFrame?.routingId || 0) + 1,
      };
    }

    once(eventName, handler) {
      this.events.set(`once:${eventName}`, handler);
    }

    on(eventName, handler) {
      this.events.set(eventName, handler);
    }

    show() {
      this.showCount += 1;
    }

    focus() {
      this.focusCount += 1;
    }

    hide() {}

    isDestroyed() {
      return false;
    }

    isMinimized() {
      return false;
    }

    isFullScreen() {
      return false;
    }

    getNormalBounds() {
      return { width: 1280, height: 820 };
    }
  }
  FakeBrowserWindow.instances = [];

  return {
    BrowserWindow: FakeBrowserWindow,
    controller: createAppWindowController({
      app: {
        getName: () => "澜台",
        getPath: () => userDataPath,
      },
      BrowserWindow: FakeBrowserWindow,
      screen: {
        getAllDisplays: () => [
          { workArea: { x: 0, y: 0, width: 1440, height: 900 } },
        ],
      },
      isDev: false,
      electronDir: __dirname,
      backendLifecycle: {
        getApiBaseUrl,
      },
      appendBackendLog,
      scheduleBackendRestart: () => {},
      processObject: { platform },
    }),
  };
}

test("macOS windows opt into the native sidebar material", () => {
  const { BrowserWindow, controller } = makeController({ platform: "darwin" });

  controller.createWindow();

  const options = BrowserWindow.instances[0].options;
  assert.equal(options.titleBarStyle, "hiddenInset");
  assert.equal(options.backgroundColor, "#00000000");
  assert.equal(options.vibrancy, "sidebar");
  assert.equal(options.visualEffectState, "active");
});

test("non-macOS windows keep the regular opaque options", () => {
  const { BrowserWindow, controller } = makeController({ platform: "linux" });

  controller.createWindow();

  const options = BrowserWindow.instances[0].options;
  assert.equal("backgroundColor" in options, false);
  assert.equal("vibrancy" in options, false);
  assert.equal("visualEffectState" in options, false);
});

test("Windows windows do not receive macOS material options", () => {
  const { BrowserWindow, controller } = makeController({ platform: "win32" });

  controller.createWindow();

  const options = BrowserWindow.instances[0].options;
  assert.equal(options.titleBarStyle, "hiddenInset");
  assert.equal("backgroundColor" in options, false);
  assert.equal("vibrancy" in options, false);
  assert.equal("visualEffectState" in options, false);
});

test("main windows enable Electron renderer sandbox", () => {
  const { BrowserWindow, controller } = makeController({ platform: "linux" });

  controller.createWindow();

  const options = BrowserWindow.instances[0].options;
  assert.equal(options.webPreferences.contextIsolation, true);
  assert.equal(options.webPreferences.nodeIntegration, false);
  assert.equal(options.webPreferences.sandbox, true);
});

test("headless lifecycle windows remain hidden after the renderer is ready", () => {
  const { BrowserWindow, controller } = makeController({ platform: "linux" });
  controller.createWindow({ showOnReady: false });
  const window = BrowserWindow.instances[0];

  window.events.get("once:ready-to-show")();

  assert.equal(window.showCount, 0);
  assert.equal(window.focusCount, 0);
});

test("document-first startup can prepare a hidden host without loading the backend", () => {
  const { BrowserWindow, controller } = makeController({ platform: "darwin" });

  const window = controller.createWindow({
    showOnReady: false,
    deferInitialLoad: true,
  });

  assert.equal(window.loadedUrl, "");
  assert.equal(window.showCount, 0);
  assert.equal(window.focusCount, 0);
});

test("macOS keeps the active chat while the same window stays open", () => {
  const { BrowserWindow, controller } = makeController({ platform: "darwin" });
  const window = controller.createWindow();
  window.loadURL("http://127.0.0.1:19090/#/chat/chat-current");

  const reopenedWindow = controller.showMainWindow({ focus: true });

  assert.equal(reopenedWindow, window);
  assert.equal(
    reopenedWindow.loadedUrl,
    "http://127.0.0.1:19090/#/chat/chat-current",
  );
  assert.equal(BrowserWindow.instances.length, 1);
});

test("only the first real app page in an Electron process carries the cold-start new-chat marker", () => {
  const { BrowserWindow, controller } = makeController({ platform: "darwin" });

  const firstWindow = controller.createWindow();
  assert.equal(
    firstWindow.loadedUrl,
    "http://127.0.0.1:19090/#/chat?lantaiColdStart=1",
  );

  firstWindow.events.get("closed")();
  const nextWindow = controller.createWindow();
  assert.equal(nextWindow.loadedUrl, "http://127.0.0.1:19090");
  assert.equal(BrowserWindow.instances.length, 2);
});

test("macOS close ends the current renderer without turning a same-process reopen into a cold start", () => {
  const { BrowserWindow, controller } = makeController({ platform: "darwin" });
  const firstWindow = controller.createWindow();
  firstWindow.loadURL("http://127.0.0.1:19090/#/chat/chat-current");
  const closeEvent = {
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };

  firstWindow.events.get("close")(closeEvent);

  assert.equal(closeEvent.prevented, false);
  firstWindow.events.get("closed")();

  const nextWindow = controller.createWindow();
  assert.notEqual(nextWindow, firstWindow);
  assert.equal(nextWindow.loadedUrl, "http://127.0.0.1:19090");
  assert.equal(BrowserWindow.instances.length, 2);
});

test("main window blocks top-level navigation outside backend origin", () => {
  const { BrowserWindow, controller } = makeController({ platform: "linux" });
  controller.createWindow();
  const window = BrowserWindow.instances[0];
  const handler = window.webContents.events.get("will-navigate");
  const event = {
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };

  handler(event, "https://evil.example/phish");

  assert.equal(event.prevented, true);
});

test("main window allows navigation to a console SPA route", () => {
  const { BrowserWindow, controller } = makeController({ platform: "linux" });
  controller.createWindow();
  const window = BrowserWindow.instances[0];
  const handler = window.webContents.events.get("will-navigate");
  const event = {
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };

  handler(event, "http://127.0.0.1:19090/#/chat");

  assert.equal(event.prevented, false);
});

test("main window blocks redirects outside backend origin", () => {
  const { BrowserWindow, controller } = makeController({ platform: "linux" });
  controller.createWindow();
  const window = BrowserWindow.instances[0];
  const handler = window.webContents.events.get("will-redirect");
  const event = {
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };

  handler(event, "https://evil.example/redirect");

  assert.equal(event.prevented, true);
});

test("main window blocks iframe navigation outside backend origin", () => {
  const { BrowserWindow, controller } = makeController({ platform: "linux" });
  controller.createWindow();
  const window = BrowserWindow.instances[0];
  const handler = window.webContents.events.get("will-frame-navigate");
  const event = {
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };

  handler(event, {
    url: "file:///tmp/evil.html",
    isMainFrame: false,
  });

  assert.equal(event.prevented, true);
});

test("main window denies target blank windows", () => {
  const { BrowserWindow, controller } = makeController({ platform: "linux" });
  controller.createWindow();
  const window = BrowserWindow.instances[0];

  const result = window.webContents.windowOpenHandler({
    url: "http://127.0.0.1:19090/chat",
  });

  assert.deepEqual(result, { action: "deny" });
});

test("window IPC trust rejects startup data pages", () => {
  const { BrowserWindow, controller } = makeController({ platform: "linux" });
  controller.createWindow({ startupErrorMessage: "slow backend" });
  const window = BrowserWindow.instances[0];

  assert.equal(
    controller.isTrustedIpcEvent({
      sender: window.webContents,
      senderFrame: {
        url: window.loadedUrl,
        parent: null,
      },
    }),
    false,
  );
});

test("startup status page does not retry a stale backend port", () => {
  let backendUrl = "http://127.0.0.1:19090";
  const { BrowserWindow, controller } = makeController({
    platform: "linux",
    getApiBaseUrl: () => backendUrl,
  });
  controller.createWindow({ startupErrorMessage: "slow backend" });
  const window = BrowserWindow.instances[0];

  backendUrl = "http://127.0.0.1:19091";
  const statusHtml = decodeURIComponent(window.loadedUrl);

  assert.doesNotMatch(statusHtml, /127\.0\.0\.1:19090/);
  assert.doesNotMatch(statusHtml, /window\.location/);
});

test("startup status pages do not consume the process cold-start intent", () => {
  const { BrowserWindow, controller } = makeController({ platform: "darwin" });
  controller.createWindow({ startupErrorMessage: "slow backend" });
  const window = BrowserWindow.instances[0];

  assert.match(window.loadedUrl, /^data:text\/html/);

  assert.equal(controller.loadMainAppUrl("http://127.0.0.1:19090"), true);
  assert.equal(
    window.loadedUrl,
    "http://127.0.0.1:19090/#/chat?lantaiColdStart=1",
  );
});

test("window IPC trust accepts only main frame from backend origin", () => {
  const { BrowserWindow, controller } = makeController({ platform: "linux" });
  controller.createWindow();
  const window = BrowserWindow.instances[0];

  assert.equal(
    controller.isTrustedIpcEvent({
      sender: window.webContents,
      senderFrame: window.webContents.mainFrame,
    }),
    true,
  );
  assert.equal(
    controller.isTrustedIpcEvent({
      sender: window.webContents,
      senderFrame: {
        ...window.webContents.mainFrame,
        parent: {},
      },
    }),
    false,
  );
});

test("renderer console messages are redacted before local logs", () => {
  const logs = [];
  const { BrowserWindow, controller } = makeController({
    platform: "linux",
    appendBackendLog: (message) => logs.push(message),
  });

  controller.createWindow();
  const window = BrowserWindow.instances[0];
  const handler = window.webContents.events.get("console-message");
  const fakeOpenAiToken = ["sk", "secret1234567890abcdef"].join("-");
  handler(
    {},
    2,
    `Renderer leaked Authorization: Basic dXNlcjpwYXNz and Bearer ${fakeOpenAiToken} and sam@example.com at /Users/example/private/file.txt?token=abc`,
    9,
    "file:///Users/example/app.js?token=abc",
  );

  const logText = logs.join("\n");
  assert.match(logText, /redacted/i);
  assert.doesNotMatch(logText, new RegExp(fakeOpenAiToken));
  assert.doesNotMatch(logText, /dXNlcjpwYXNz/);
  assert.doesNotMatch(logText, /sam@example\.com/);
  assert.doesNotMatch(logText, /\/Users\/sam/);
  assert.doesNotMatch(logText, /token=abc/);

});
