const assert = require("node:assert/strict");
const test = require("node:test");
const { EventEmitter } = require("node:events");

const {
  createOfficeWorkbenchController,
} = require("./officeWorkbenchController.cjs");

function makeView(id) {
  const bounds = [];
  let destroyed = false;
  let currentBounds = { x: 0, y: 0, width: 0, height: 0 };
  let visible = false;
  const webContents = new EventEmitter();
  webContents.id = id;
  webContents.isDestroyed = () => destroyed;
  return {
    bounds,
    visible: () => visible,
    isDestroyed: () => destroyed,
    setBounds: (value) => {
      currentBounds = value;
      bounds.push(value);
    },
    getBounds: () => currentBounds,
    setVisible: (value) => {
      visible = value;
    },
    webContents,
    destroy: () => {
      destroyed = true;
    },
  };
}

function makeHarness({ trusted = true } = {}) {
  const handlers = new Map();
  const children = new Set();
  const calls = { add: [], remove: [], present: [] };
  const mainWindow = Object.assign(new EventEmitter(), {
    isDestroyed: () => false,
    isVisible: () => true,
    getContentBounds: () => ({ x: 0, y: 0, width: 1200, height: 800 }),
    contentView: {
      addChildView: (view) => {
        children.add(view);
        calls.add.push(view.webContents.id);
      },
      removeChildView: (view) => {
        children.delete(view);
        calls.remove.push(view.webContents.id);
      },
    },
    webContents: {
      isDestroyed: () => false,
      send: (channel, payload) => calls.present.push([channel, payload]),
    },
  });
  const controller = createOfficeWorkbenchController({
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => handlers.delete(channel),
    },
    isTrustedIpcEvent: () => trusted,
    getMainWindow: () => mainWindow,
  });
  controller.installIpcHandlers();
  return { calls, children, controller, handlers, mainWindow };
}

test("reuses the same local editor view without losing its session", async () => {
  const { calls, controller } = makeHarness();
  const created = [];
  const createView = async () => {
    const view = makeView(created.length + 1);
    created.push(view);
    return view;
  };

  const first = await controller.open({
    path: "/tmp/deck.pptx",
    kind: "slides",
    source_chat_id: "chat-source-1",
    createView,
  });
  const second = await controller.open({
    path: "/tmp/deck.pptx",
    kind: "slides",
    createView,
  });

  assert.equal(created.length, 1);
  assert.equal(first.workbench_id, second.workbench_id);
  assert.equal(first.reused, false);
  assert.equal(second.reused, true);
  assert.equal(first.route, "/office-workbench");
  const state = await controller.getState({});
  assert.equal(state.source_chat_id, "chat-source-1");
  assert.deepEqual(
    calls.present.map(([channel]) => channel),
    ["lantai:office-workbench-present", "lantai:office-workbench-present"],
  );
});

test("switches and hides views without destroying recoverable editor state", async () => {
  const { calls, children, controller, handlers } = makeHarness();
  const firstView = makeView(1);
  const secondView = makeView(2);
  const first = await controller.open({
    path: "/tmp/first.pptx",
    kind: "slides",
    createView: async () => firstView,
  });

  assert.deepEqual(
    await handlers.get("lantai:office-workbench-layout")(
      {},
      {
        workbench_id: first.workbench_id,
        bounds: { x: -20, y: 10, width: 2000, height: 1000 },
      },
    ),
    {
      success: true,
      deferred: false,
      visible: true,
      bounds: { x: 0, y: 10, width: 1200, height: 790 },
    },
  );
  assert.deepEqual(firstView.bounds, [
    { x: 0, y: 10, width: 1200, height: 790 },
  ]);
  assert.equal(children.has(firstView), true);
  assert.equal(firstView.visible(), true);

  await controller.open({
    path: "/tmp/second.pptx",
    kind: "slides",
    createView: async () => secondView,
  });
  assert.equal(children.has(firstView), false);
  assert.equal(firstView.isDestroyed(), false);
  assert.deepEqual(calls.remove, [1]);

  const state = await handlers.get("lantai:office-workbench-state")({});
  assert.equal(state.display_name, "second.pptx");
  assert.equal(state.visible, false);
  assert.deepEqual(await handlers.get("lantai:office-workbench-hide")({}, {}), {
    success: true,
  });
  assert.equal(secondView.isDestroyed(), false);
  assert.equal(
    (await handlers.get("lantai:office-workbench-state")({})).active,
    false,
  );
});

test("defers a zero-sized host and attaches exactly when usable bounds arrive", async () => {
  const { calls, children, controller, handlers } = makeHarness();
  const view = makeView(7);
  const opened = await controller.open({
    path: "/tmp/cold.xlsx",
    kind: "sheets",
    createView: async () => view,
  });

  const deferred = await handlers.get("lantai:office-workbench-layout")(
    {},
    {
      workbench_id: opened.workbench_id,
      bounds: { x: 0, y: 0, width: 0, height: 0 },
    },
  );
  assert.deepEqual(deferred, {
    success: true,
    deferred: true,
    visible: false,
    bounds: null,
  });
  assert.equal(children.has(view), false);
  assert.equal(view.bounds.length, 0);

  const attached = await handlers.get("lantai:office-workbench-layout")(
    {},
    {
      workbench_id: opened.workbench_id,
      bounds: { x: 20, y: 30, width: 900, height: 600 },
    },
  );
  assert.equal(attached.visible, true);
  assert.deepEqual(attached.bounds, { x: 20, y: 30, width: 900, height: 600 });
  assert.equal(children.has(view), true);
  assert.equal(view.visible(), true);
  assert.deepEqual(calls.add, [7]);
});

test("replays the last verified bounds after hidden-show and editor reload", async () => {
  const { calls, controller, handlers, mainWindow } = makeHarness();
  let windowVisible = false;
  mainWindow.isVisible = () => windowVisible;
  const view = makeView(8);
  const opened = await controller.open({
    path: "/tmp/replay.xlsx",
    kind: "sheets",
    createView: async () => view,
  });

  const hidden = await handlers.get("lantai:office-workbench-layout")(
    {},
    {
      workbench_id: opened.workbench_id,
      bounds: { x: 5, y: 6, width: 800, height: 500 },
    },
  );
  assert.equal(hidden.deferred, true);
  assert.equal(view.bounds.length, 0);

  windowVisible = true;
  mainWindow.emit("show");
  assert.deepEqual(view.bounds, [{ x: 5, y: 6, width: 800, height: 500 }]);
  assert.deepEqual(calls.add, [8]);

  view.webContents.emit("did-finish-load");
  mainWindow.emit("resize");
  assert.deepEqual(view.bounds, [
    { x: 5, y: 6, width: 800, height: 500 },
    { x: 5, y: 6, width: 800, height: 500 },
    { x: 5, y: 6, width: 800, height: 500 },
  ]);
  assert.equal(view.visible(), true);
  assert.equal(calls.add.length, 1);
});

test("rejects untrusted layout mutations", async () => {
  const { handlers } = makeHarness({ trusted: false });
  assert.equal(
    (await handlers.get("lantai:office-workbench-layout")({}, {})).error_code,
    "office_workbench_untrusted_sender",
  );
  assert.equal(
    (await handlers.get("lantai:office-workbench-hide")({}, {})).error_code,
    "office_workbench_untrusted_sender",
  );
});
