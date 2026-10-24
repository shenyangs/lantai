const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Contents, topEvent, childEvent, deferred, W, vendor} = require('./nativeOfficeSimpleHarness.cjs');
const ts = require(path.join(W, 'console/node_modules/typescript/lib/typescript.js'));
const helper = path.join(vendor, 'packages/electron-utils/src/office-document-trust.ts');

// Simulate separately bundled copies in the same Electron main-process realm.
// The trust code and AsyncLocalStorage are real; only Electron objects are inert.
function bundles() {
  const source = fs.readFileSync(helper, 'utf8');
  const js = ts.transpileModule(source, {fileName: helper, compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  }}).outputText;
  const context = vm.createContext({URL, console});
  const requireBuiltin = id => {
    if (!['node:async_hooks', 'node:path', 'node:url'].includes(id)) throw new Error('unexpected real helper import: ' + id);
    return require(id);
  };
  function load() {
    const module = {exports: {}};
    new vm.Script('(function(module,exports,require){' + js + '\n})', {filename: helper})
      .runInContext(context)(module, module.exports, requireBuiltin);
    return module.exports;
  }
  return [load(), load()];
}
function rawIpc() {
  const handles = new Map();
  return {handles, handle(channel, listener) {handles.set(channel, listener);}};
}
function owner(t, bundle, kind = 'docs') {
  const file = '/synthetic/' + kind + '/index.html';
  const wc = new Contents('file://' + file);
  bundle.registerOfficeRenderer(wc, {kind, rendererFile: file});
  t.after(() => wc.destroy());
  return wc;
}

test('actual separately bundled helpers share registered owner and initial IPC async identity', async t => {
  const [registration, deepSink] = bundles();
  const wc = owner(t, registration), raw = rawIpc();
  const entered = deferred(), release = deferred();
  let writes = 0;
  deepSink.trustedOfficeIpcMain(raw, 'docs').handle('docs:save', async () => {
    entered.resolve(); await release.promise;
    registration.assertOfficeIpcDocument(); writes++;
    return 'saved';
  });
  const pending = Promise.resolve(raw.handles.get('docs:save')(topEvent(wc)));
  const rejected = assert.rejects(pending, /no longer authorized/);
  await entered.promise;
  wc.navigate(wc.getURL());
  release.resolve(); await rejected;
  assert.equal(writes, 0);
});

test('actual shared helper permits a legal current document across bundle boundaries', async t => {
  const [a, b] = bundles();
  const wc = owner(t, a), raw = rawIpc();
  b.trustedOfficeIpcMain(raw, 'docs').handle('docs:save', () => {
    a.assertOfficeIpcDocument(); return 'saved';
  });
  assert.equal(await raw.handles.get('docs:save')(topEvent(wc)), 'saved');
});

test('actual shared helper fences the old document at navigation start before frame replacement', async t => {
  const [a] = bundles();
  const wc = owner(t, a), raw = rawIpc();
  let writes = 0;
  a.trustedOfficeIpcMain(raw, 'docs').handle('docs:save', () => ++writes);
  assert.equal(await raw.handles.get('docs:save')(topEvent(wc)), 1);
  // Electron still reports the old top frame during navigation start.
  wc.emit('did-start-navigation', {}, wc.getURL(), false, true, 1, wc.mainFrame.routingId);
  assert.throws(() => raw.handles.get('docs:save')(topEvent(wc)), /Unauthorized/);
  assert.equal(writes, 1);
});

test('actual shared helper admits the new current document after same-URL reload', async t => {
  const [a] = bundles();
  const wc = owner(t, a), raw = rawIpc();
  a.trustedOfficeIpcMain(raw, 'docs').handle('docs:save', () => 'saved');
  const old = topEvent(wc);
  wc.navigate(wc.getURL());
  assert.throws(() => raw.handles.get('docs:save')(old), /Unauthorized/);
  assert.equal(await raw.handles.get('docs:save')(topEvent(wc)), 'saved');
});

test('actual shared async context isolates concurrent documents when only one navigates', async t => {
  const [a, b] = bundles();
  const docs = owner(t, a, 'docs'), pdf = owner(t, b, 'pdf'), raw = rawIpc();
  const enteredDocs = deferred(), enteredPdf = deferred(), release = deferred();
  const writes = [];
  const install = (bundle, kind, entered) => bundle.trustedOfficeIpcMain(raw, kind).handle(kind + ':save', async () => {
    entered.resolve(); await release.promise;
    a.assertOfficeIpcDocument(); b.assertOfficeIpcDocument();
    writes.push(kind); return kind;
  });
  install(a, 'docs', enteredDocs); install(b, 'pdf', enteredPdf);
  const one = Promise.resolve(raw.handles.get('docs:save')(topEvent(docs)));
  const rejected = assert.rejects(one, /no longer authorized/);
  const two = Promise.resolve(raw.handles.get('pdf:save')(topEvent(pdf)));
  await Promise.all([enteredDocs.promise, enteredPdf.promise]);
  docs.navigate(docs.getURL()); release.resolve();
  await rejected; assert.equal(await two, 'pdf');
  assert.deepEqual(writes, ['pdf']);
});

test('actual helper rejects child frames and same-id unregistered WebContents before listener admission', t => {
  const [a] = bundles();
  const wc = owner(t, a), raw = rawIpc();
  let calls = 0;
  a.trustedOfficeIpcMain(raw, 'docs').handle('docs:save', () => ++calls);
  assert.throws(() => raw.handles.get('docs:save')(childEvent(wc)), /Unauthorized/);
  const impostor = new Contents(wc.getURL()); impostor.id = wc.id;
  t.after(() => impostor.destroy());
  assert.throws(() => raw.handles.get('docs:save')(topEvent(impostor)), /Unauthorized/);
  assert.equal(calls, 0);
});

test('actual helper accepts Electron single-event allowed navigation and blocks foreign navigation/window open', t => {
  const [a] = bundles(); const wc = owner(t, a);
  let blocked = false;
  wc.emit('will-frame-navigate', {url: wc.getURL(), preventDefault() {blocked = true;}});
  assert.equal(blocked, false);
  wc.emit('will-frame-navigate', {url: 'https://untrusted.invalid/', preventDefault() {blocked = true;}});
  assert.equal(blocked, true);
  assert.equal(wc.windowOpenHandler({url: 'https://untrusted.invalid/'}).action, 'deny');
});

test('actual helper preserves a main-owned operation outside renderer IPC async context', async () => {
  const [a] = bundles(); let writes = 0;
  assert.equal(await a.guardOfficeOperation(async () => ++writes)(), 1);
  assert.equal(writes, 1);
});
