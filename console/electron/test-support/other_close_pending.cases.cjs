const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const {createHarness, topEvent, deferred, vendor} = require('./nativeOfficeHarness.cjs');

function emit(h, channel, ...args) {
  const listeners = h.listeners.get(channel);
  assert.ok(listeners?.length, 'real native registration missing: ' + channel);
  for (const listener of listeners) listener(topEvent(h.wc), ...args);
}
async function setup(t, kind) {
  const h = createHarness(kind, {realEngine: kind === 'slides'}); t.after(h.close);
  if (kind === 'slides') {
    const engine = h.load(path.join(vendor, 'packages/pptx-engine/src/index.ts'));
    h.files.set(h.originalPath, Buffer.from(await engine.createBlankPptx()));
  }
  await h.openStandalone();
  if (kind === 'slides') {
    await h.invoke('slides:add-element', topEvent(h.wc), {
      slideIndex:0, kind:'textbox', xPx:0, yPx:0, wPx:100, hPx:30,
      fitWidthPx:1280, text:'synthetic unsaved edit',
    });
    assert.equal(h.runtime.slidesIsDirty(h.wc.id), true);
  } else {
    emit(h, 'workbook:pending-edits', 1);
    assert.equal(h.runtime.sheetsPendingEditCount(h.wc.id), 1);
  }
  const prefix = kind === 'slides' ? 'slides' : 'workbook';
  h.requestClose = () => kind === 'slides' ? h.runtime.requestSlidesClose(h.wc) : h.runtime.requestSheetsClose(h.wc);
  h.closeSaveRequest = prefix + ':close-save-request'; h.closeSaveResult = prefix + ':close-save-result';
  return h;
}

async function beginSave(h) {
  if (h.kind === 'slides') emit(h, 'slides:autosave-pref', true);
  const requested = deferred(), send = h.wc.send.bind(h.wc);
  h.wc.send = (channel, ...args) => {
    send(channel, ...args); if (channel === h.closeSaveRequest) requested.resolve();
  };
  const pending = h.requestClose(); await requested.promise; return {pending};
}

for (const kind of ['slides', 'sheets']) {
  test(kind + ': legal current-document close save still settles successfully', async t => {
    const h = await setup(t, kind), {pending} = await beginSave(h);
    emit(h, h.closeSaveResult, true);
    assert.equal(await pending, true);
  });
  test(kind + ': a replacement document cannot acknowledge the original close save', async t => {
    const h = await setup(t, kind), {pending} = await beginSave(h);
    h.messageDialog = async()=>({response:2});
    h.wc.navigate(h.wc.getURL()); emit(h, h.closeSaveResult, true);
    assert.equal(await pending, false,
      'replacement document confirmed original unsaved edits committed');
  });
  test(kind + ': legal Dont Save decision still permits its own document close', async t => {
    const h = await setup(t, kind); h.messageDialog = async()=>({response:1});
    assert.equal(await h.requestClose(), true);
  });
  test(kind + ': original native close dialog cannot close a replacement document', async t => {
    const h = await setup(t, kind), entered = deferred(), release = deferred();
    h.messageDialog = async()=>{entered.resolve(); return await release.promise;};
    const pending = h.requestClose(); await entered.promise;
    h.wc.navigate(h.wc.getURL()); release.resolve({response:1});
    assert.equal(await pending, false,
      'retired close-dialog decision allowed closing a replacement document');
  });
}
