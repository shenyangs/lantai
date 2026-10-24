const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const {Writable} = require('node:stream');
const {Contents, topEvent, deferred, W, vendor} = require('./nativeOfficeSimpleHarness.cjs');
const ts = require(path.join(W, 'console/node_modules/typescript/lib/typescript.js'));
const packageRequire = createRequire(path.join(vendor, 'package.json'));

// Real helper + real PPTX archive/parser/zip/save path. File/stream fault hooks
// are limited to this test's generated directory; no Office/user files are read.
async function setup(t) {
  const dir = await fsp.mkdtemp(path.join(require('node:os').tmpdir(), 'lantai-pptx-'));
  t.after(() => fsp.rm(dir, {recursive: true, force: true}));
  const target = path.join(dir, 'edited.pptx');
  const hooks = {dir, target, promotions: [], writes: [], renameFailures: 0};
  const permit = value => {
    const full = path.resolve(value);
    if (!full.startsWith(dir + path.sep)) throw new Error('outside isolated test output: ' + full);
    return full;
  };
  const filesystem = {
    createWriteStream(file, options) {
      permit(file); assert.equal(options.flags, 'wx');
      hooks.writes.push(file);
      // Finite synthetic platform stream. The PPTX producer/pipeline remains real.
      fs.writeFileSync(file, Buffer.alloc(0), {flag: 'wx'});
      return new Writable({
        write(chunk, _encoding, callback) {fsp.appendFile(file, chunk).then(() => callback(), callback);},
        final(callback) {
          if (hooks.streamEntered) {
            hooks.streamEntered.resolve();
            hooks.streamRelease.promise.then(() => callback(), callback);
          } else callback();
        },
      });
    },
  };
  const promises = {
    async rename(from, to) {
      permit(from); permit(to);
      if (hooks.renameFailures-- > 0) {
        hooks.renameEntered?.resolve();
        throw Object.assign(new Error('synthetic transient target lock'), {code: 'EACCES'});
      }
      hooks.promotions.push({from, to}); return fsp.rename(from, to);
    },
    rm(file, options) {permit(file); return fsp.rm(file, options);},
  };
  const cache = new Map();
  const realm = vm.createContext({Buffer, URL, Uint8Array, ArrayBuffer, TextEncoder, TextDecoder,
    setTimeout, clearTimeout, setImmediate, console});
  function load(file) {
    if (cache.has(file)) return cache.get(file).exports;
    if (!file.startsWith(vendor + path.sep)) throw new Error('unexpected product source: ' + file);
    const module = {exports: {}}; cache.set(file, module);
    const js = ts.transpileModule(fs.readFileSync(file, 'utf8'), {fileName: file, compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true,
    }}).outputText;
    const req = id => {
      if (id.startsWith('.')) return load(path.resolve(path.dirname(file), id + (path.extname(id) ? '' : '.ts')));
      if (id === 'node:fs') return filesystem;
      if (id === 'node:fs/promises') return promises;
      if (['node:async_hooks', 'node:path', 'node:url', 'node:crypto', 'node:zlib', 'node:stream/promises'].includes(id)) return require(id);
      if (id === 'jszip' || id === 'fast-xml-parser') return packageRequire(id);
      throw new Error('unexpected deep-save import: ' + id);
    };
    new vm.Script('(function(module,exports,require,__dirname,__filename){' + js + '\n})', {filename: file})
      .runInContext(realm)(module, module.exports, req, path.dirname(file), file);
    return module.exports;
  }
  const trust = load(path.join(vendor, 'packages/electron-utils/src/office-document-trust.ts'));
  const engine = load(path.join(vendor, 'packages/pptx-engine/src/index.ts'));
  const initial = Buffer.from(await engine.createBlankPptx());
  await fsp.writeFile(target, initial);
  const opened = await engine.openPptx(initial);
  engine.addElement(opened.deck.slides[0], {kind: 'textbox',
    offset: {x: 914400, y: 914400, cx: 6096000, cy: 914400},
    paragraphs: [{runs: [{text: 'synthetic independent edit'}]}],
  });
  const wc = new Contents('file:///synthetic/slides/index.html'); t.after(() => wc.destroy());
  trust.registerOfficeRenderer(wc, {kind: 'slides', rendererFile: '/synthetic/slides/index.html'});
  const handles = new Map();
  const ipc = trust.trustedOfficeIpcMain({handle(channel, listener) {handles.set(channel, listener);}}, 'slides');
  ipc.handle('slides:save', async () => {
    await engine.savePptxToFile(opened, target, trust.assertOfficeIpcDocument);
    engine.commitSaved(opened); return 'saved';
  });
  return {...hooks, hooks, wc, target, dir, initial, engine, save: () => handles.get('slides:save')(topEvent(wc))};
}

test('real PPTX stream and real IPC document preserve a legal edit', async t => {
  const h = await setup(t); assert.equal(await h.save(), 'saved');
  const reopened = await h.engine.openPptx(await fsp.readFile(h.target));
  assert.equal(reopened.deck.slides.length, 1);
  assert.match(reopened.archive.readText(reopened.deck.slides[0].path), /synthetic independent edit/);
  assert.deepEqual(await fsp.readdir(h.dir), ['edited.pptx']);
});

test('real PPTX stream preserves original when initial IPC document retires before promotion', async t => {
  const h = await setup(t); h.hooks.streamEntered = deferred(); h.hooks.streamRelease = deferred();
  const pending = h.save(); const rejected = assert.rejects(pending, /no longer authorized/);
  await h.hooks.streamEntered.promise;
  assert.deepEqual(await fsp.readFile(h.target), h.initial);
  h.wc.navigate(h.wc.getURL()); h.hooks.streamRelease.resolve(); await rejected;
  assert.deepEqual(await fsp.readFile(h.target), h.initial);
  assert.equal(h.hooks.promotions.length, 0);
  assert.deepEqual(await fsp.readdir(h.dir), ['edited.pptx']);
});

test('real PPTX stream preserves legal transient-rename retry', async t => {
  const h = await setup(t); h.hooks.renameFailures = 1;
  assert.equal(await h.save(), 'saved');
  const reopened = await h.engine.openPptx(await fsp.readFile(h.target));
  assert.match(reopened.archive.readText(reopened.deck.slides[0].path), /synthetic independent edit/);
  assert.equal(h.hooks.promotions.length, 1);
});

test('real PPTX rename retry retains initial IPC identity after navigation', async t => {
  const h = await setup(t); h.hooks.renameFailures = 1; h.hooks.renameEntered = deferred();
  const pending = h.save(); const rejected = assert.rejects(pending, /no longer authorized/);
  await h.hooks.renameEntered.promise; h.wc.navigate(h.wc.getURL()); await rejected;
  assert.deepEqual(await fsp.readFile(h.target), h.initial);
  assert.equal(h.hooks.promotions.length, 0);
  assert.deepEqual(await fsp.readdir(h.dir), ['edited.pptx']);
});
