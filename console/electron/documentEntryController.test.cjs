const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createDocumentEntryController,
} = require("./documentEntryController.cjs");

function makeHarness({ openResult, openError } = {}) {
  const listeners = new Map();
  const calls = { open: [], focus: 0, host: 0, menu: [], errors: [], logs: [] };
  const menu = {
    buildFromTemplate: (template) => {
      calls.menu.push(template);
      return { template };
    },
    setApplicationMenu: () => {},
  };
  const controller = createDocumentEntryController({
    app: {
      name: "澜台",
      on: (event, listener) => listeners.set(event, listener),
    },
    Menu: menu,
    dialog: {
      showOpenDialog: async () => ({
        canceled: false,
        filePaths: ["/tmp/from-menu.docx"],
      }),
    },
    platform: "darwin",
    openDocx: async (target) => {
      calls.open.push(target.path);
      if (openError) throw openError;
      return openResult;
    },
    ensureDocumentHost: async () => {
      calls.host += 1;
    },
    showMainWindow: () => {
      calls.focus += 1;
    },
    getMainWindow: () => ({ isDestroyed: () => false }),
    appendLog: (message) => calls.logs.push(message),
    showOpenError: async (details) => calls.errors.push(details),
  });
  return { calls, controller, listeners };
}

test("queues Finder open-file events until the DOCX editor is ready", async () => {
  const { calls, controller, listeners } = makeHarness();
  controller.installEventHandlers([]);
  const event = { preventDefault: () => {} };

  listeners.get("open-file")(event, "/tmp/from-finder.docx");
  assert.deepEqual(calls.open, []);

  await controller.markReady();
  assert.deepEqual(calls.open, ["/tmp/from-finder.docx"]);
  assert.equal(calls.host, 1);
});

test("routes a second-instance DOCX argument and the Open Document menu", async () => {
  const { calls, controller, listeners } = makeHarness();
  controller.installEventHandlers([]);
  await controller.markReady();

  listeners.get("second-instance")({}, ["澜台", "/tmp/second.docx"]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls.open, ["/tmp/second.docx"]);

  controller.installApplicationMenu();
  const fileMenu = calls.menu[0].find((item) => item.label === "文件");
  const openItem = fileMenu.submenu.find((item) => item.label === "打开文档…");
  await openItem.click();
  assert.deepEqual(calls.open, ["/tmp/second.docx", "/tmp/from-menu.docx"]);
});

test("accepts spreadsheet, presentation, and PDF native entry paths", async () => {
  const { calls, controller, listeners } = makeHarness();
  controller.installEventHandlers([]);
  await controller.markReady();
  for (const file of ["book.xlsx", "legacy.xls", "data.csv", "deck.pptx", "paper.pdf"]) {
    listeners.get("open-file")({ preventDefault() {} }, `/tmp/${file}`);
  }
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls.open, [
    "/tmp/book.xlsx",
    "/tmp/legacy.xls",
    "/tmp/data.csv",
    "/tmp/deck.pptx",
    "/tmp/paper.pdf",
  ]);
  assert.equal(calls.focus, 0);
});

test("keeps Finder failures visible and records the exact native-open error", async () => {
  const { calls, controller } = makeHarness({
    openResult: {
      success: false,
      error: "编辑器运行时缺失",
      error_code: "office_editor_runtime_missing",
    },
  });
  await controller.markReady();

  assert.equal(await controller.openPath("/tmp/broken.xlsx"), false);
  assert.equal(calls.focus, 1);
  assert.equal(calls.errors[0].error_code, "office_editor_runtime_missing");
  assert.match(calls.logs[0], /office_editor_runtime_missing/);
});
