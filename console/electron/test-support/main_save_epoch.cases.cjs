const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const {Contents, topEvent, deferred, W} = require('./nativeOfficeSimpleHarness.cjs');
const {createAppWindowController} = require(path.join(W, 'console/electron/appWindowController.cjs'));
const {installDesktopIpcHandlers} = require(path.join(W, 'console/electron/desktopIpcController.cjs'));

function setup(t) {
  const dir = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'lantai-main-save-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  class Window extends EventEmitter {
    constructor() {super(); this.webContents = new Contents();}
    loadURL(url) {return this.webContents.loadURL(url);}
    isDestroyed() {return false;} isMinimized() {return false;} isFullScreen() {return false;}
    show() {} focus() {} hide() {} getNormalBounds() {return {width: 1280, height: 820};}
  }
  const app = {getName: () => 'synthetic', getPath: () => dir};
  const controller = createAppWindowController({app, BrowserWindow: Window,
    screen: {getAllDisplays: () => [{workArea: {x: 0, y: 0, width: 1440, height: 900}}]},
    isDev: false, electronDir: path.join(W, 'console/electron'),
    backendLifecycle: {getApiBaseUrl: () => 'http://127.0.0.1:19090'},
    appendBackendLog() {}, scheduleBackendRestart() {}, processObject: {platform: 'linux'}});
  controller.createWindow();
  const wc = controller.getMainWindow().webContents; t.after(() => wc.destroy());
  const handles = new Map(), entered = deferred(), release = deferred();
  const source = path.join(dir, 'source.txt'), destination = path.join(dir, 'chosen.txt');
  const bytes = Buffer.from('synthetic independent main-window source'); fs.writeFileSync(source, bytes);
  installDesktopIpcHandlers({app, ipcMain: {handle: (channel, listener) => handles.set(channel, listener)},
    dialog: {async showSaveDialog() {entered.resolve(); return release.promise;}},
    shell: {}, getMainWindow: controller.getMainWindow,
    isTrustedIpcEvent: controller.isTrustedIpcEvent,
    fetchImpl: () => {throw new Error('network forbidden');}});
  return {wc, controller, source, destination, bytes, entered, release,
    save: () => handles.get('lantai:save-file-as')(topEvent(wc), {sourcePath: source})};
}

test('actual main save-file-as preserves a legal current-document native dialog copy', async t => {
  const h = setup(t); const pending = h.save(); await h.entered.promise;
  h.release.resolve({canceled: false, filePath: h.destination});
  assert.equal((await pending).success, true);
  assert.deepEqual(fs.readFileSync(h.destination), h.bytes);
  assert.deepEqual(fs.readFileSync(h.source), h.bytes);
});

for (const boundary of ['navigation-start', 'same-url-reload']) {
  test('actual main save-file-as does not copy after ' + boundary + ' during the native dialog', async t => {
    const h = setup(t); const event = topEvent(h.wc); const pending = h.save(); await h.entered.promise;
    if (boundary === 'navigation-start') h.wc.emit('did-start-navigation', {}, h.wc.getURL(), false, true, 1, h.wc.mainFrame.routingId);
    else h.wc.navigate(h.wc.getURL());
    assert.equal(h.controller.isTrustedIpcEvent(event), false, 'real main document admission is retired');
    h.release.resolve({canceled: false, filePath: h.destination});
    const result = await pending;
    assert.equal(fs.existsSync(h.destination), false, 'old document copied the source after its authority ended');
    assert.equal(result.success, false);
    assert.deepEqual(fs.readFileSync(h.source), h.bytes);
  });
}
