const assert = require('node:assert/strict');
const test = require('node:test');
const {createHarness, topEvent, deferred} = require('./nativeOfficeHarness.cjs');

function emit(h, channel, ...args) {
  const listeners = h.listeners.get(channel);
  assert.ok(listeners?.length, 'real native registration missing: ' + channel);
  for (const listener of listeners) listener(topEvent(h.wc), ...args);
}
async function setup(t) {
  const h = createHarness('docs'); t.after(h.close); await h.openStandalone(); return h;
}

test('docs: a current document can report its actual dirty state', async t => {
  const h = await setup(t);
  const pending = h.runtime.docsQueryDirty(h.wc);
  emit(h, 'docs:close-check-result', {dirty: true, autoSave: false});
  assert.equal(await pending, true);
  const clean = h.runtime.docsQueryDirty(h.wc);
  emit(h, 'docs:close-check-result', {dirty: false, autoSave: false});
  assert.equal(await clean, false);
});

test('docs: a new document cannot answer the retired document dirty query', async t => {
  const h = await setup(t);
  const pending = h.runtime.docsQueryDirty(h.wc);
  h.wc.navigate(h.wc.getURL());
  emit(h, 'docs:close-check-result', {dirty: false, autoSave: false});
  assert.equal(await pending, true,
    'a new document falsely settled the retired document dirty query as clean');
});

async function beginAutosave(h) {
  const requested = deferred(), send = h.wc.send.bind(h.wc);
  h.wc.send = (channel, ...args) => {
    send(channel, ...args); if (channel === 'docs:close-save-request') requested.resolve();
  };
  const pending = h.runtime.requestDocsClose(h.wc);
  emit(h, 'docs:close-check-result', {dirty: true, autoSave: true, filePath: h.originalPath});
  await requested.promise;
  return {pending};
}

test('docs: current-document autosave acknowledgement still allows its close', async t => {
  const h = await setup(t), {pending} = await beginAutosave(h);
  emit(h, 'docs:close-save-result', true);
  assert.equal(await pending, true);
});

test('docs: a new document cannot acknowledge the retired document close save', async t => {
  const h = await setup(t); h.messageDialog = async()=>({response:2});
  const {pending} = await beginAutosave(h);
  h.wc.navigate(h.wc.getURL());
  emit(h, 'docs:close-save-result', true);
  assert.equal(await pending, false,
    'a new document declared retired unsaved edits committed and allowed the close');
});

test('docs: a native close dialog result cannot close a replacement document', async t => {
  const h = await setup(t), entered = deferred(), release = deferred();
  h.messageDialog = async()=>{entered.resolve(); return await release.promise;};
  const pending = h.runtime.requestDocsClose(h.wc);
  emit(h, 'docs:close-check-result', {dirty: true, autoSave: false, filePath: h.originalPath});
  await entered.promise;
  h.wc.navigate(h.wc.getURL());
  release.resolve({response:1});
  assert.equal(await pending, false,
    'a Dont Save choice for the retired document allowed a replacement document to close');
});
