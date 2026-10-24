const assert = require('node:assert/strict');
const test = require('node:test');
const {createHarness, topEvent, deferred} = require('./nativeOfficeHarness.cjs');

function emit(h, channel, ...args) {
  const listeners = h.listeners.get(channel);
  assert.ok(listeners?.length, 'real native registration missing: ' + channel);
  for (const listener of listeners) listener(topEvent(h.wc), ...args);
}
async function setup(t) {
  const h = createHarness('pdf'); t.after(h.close); await h.openStandalone(); return h;
}
async function result(promise) {
  try {return await promise;} catch (error) {return {error: String(error)};}
}

test('pdf: legal current-document Save As retains its chosen grant and acknowledgement', async t => {
  const h = await setup(t), requested = deferred();
  const send = h.wc.send.bind(h.wc);
  h.wc.send = (channel, ...args) => {
    send(channel, ...args); if (channel === 'pdf:save-as-request') requested.resolve(args[0]);
  };
  const pending = result(h.invoke('pdf:choose-save-as', topEvent(h.wc), h.originalPath));
  const targetPath = await requested.promise;
  const saved = await h.invoke('pdf:save', topEvent(h.wc), {
    path: h.originalPath, targetPath, textEdits: [], imageEdits: [], annotations: [],
  });
  emit(h, 'pdf:save-as-result', saved.ok === true);
  assert.equal(await pending, true);
  assert.equal(saved.ok, true);
  assert.equal(h.writes.filter(w => w.path === targetPath).length, 1);
  assert.equal(h.writes.filter(w => w.path === h.originalPath).length, 0);
});

test('pdf: Save As destination from an old document is retired before a new document can write', async t => {
  const h = await setup(t), requested = deferred();
  const send = h.wc.send.bind(h.wc);
  h.wc.send = (channel, ...args) => {
    send(channel, ...args); if (channel === 'pdf:save-as-request') requested.resolve(args[0]);
  };
  const pending = result(h.invoke('pdf:choose-save-as', topEvent(h.wc), h.originalPath));
  const targetPath = await requested.promise;
  h.wc.navigate(h.wc.getURL());
  const saved = await result(h.invoke('pdf:save', topEvent(h.wc), {
    path: h.originalPath, targetPath, textEdits: [], imageEdits: [], annotations: [],
  }));
  emit(h, 'pdf:save-as-result', saved.ok === true);
  await pending;
  assert.equal(h.writes.filter(w => w.path === targetPath).length, 0,
    'new document consumed a Save As destination belonging to the retired document');
  assert.notEqual(saved.ok, true);
});

test('pdf: a new document cannot acknowledge an old menu Save As request', async t => {
  const h = await setup(t);
  const pending = h.runtime.requestPdfSaveAs(h.wc, '/synthetic/menu-copy.pdf');
  h.wc.navigate(h.wc.getURL());
  emit(h, 'pdf:save-as-result', true);
  assert.equal(await pending, false,
    'new current document acknowledged another document\'s pending native Save As');
});

test('pdf: current-document menu save acknowledgement still settles successfully', async t => {
  const h = await setup(t); emit(h, 'pdf:dirty-changed', true);
  const pending = h.runtime.flushPdfSave(h.wc);
  emit(h, 'pdf:close-save-result', true);
  assert.equal(await pending, true);
});

test('pdf: a new document cannot acknowledge an old native close-save request', async t => {
  const h = await setup(t); emit(h, 'pdf:dirty-changed', true);
  const pending = h.runtime.flushPdfSave(h.wc);
  h.wc.navigate(h.wc.getURL());
  emit(h, 'pdf:close-save-result', true);
  assert.equal(await pending, false,
    'new current document declared the retired document\'s unsaved edits committed');
});
