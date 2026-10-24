const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const {createHarness, Contents, topEvent, deferred, vendor} = require('./nativeOfficeHarness.cjs');

const request = requestId => ({requestId, settings: {provider: 'synthetic', providers: {
  synthetic: {apiKey: 'synthetic-offline-only', model: 'synthetic-finite'}}}, system: '', messages: [], tools: []});
const settle = async pending => {try {return await pending;} catch (error) {return {error: String(error)};}};
function streamProvider() {
  const entered = deferred(), release = deferred();
  let options;
  class SyntheticAiError extends Error {}
  return {entered, release, get options() {return options;}, provider: {
    AiCreditsError: SyntheticAiError, AiTimeoutError: SyntheticAiError,
    async streamForProvider(_provider, _config, _system, _messages, _tools, _budget, supplied) {
      options = supplied; entered.resolve(); await release.promise;
      if (!options.signal.aborted) options.onDelta('synthetic finite chunk');
    },
  }};
}
async function streaming(t, kind) {
  const p = streamProvider();
  const h = createHarness(kind, {realAi: true, provider: p.provider});
  t.after(() => {p.release.resolve(); h.close();});
  await h.openStandalone();
  if (!h.handles.has('ai:stream')) {
    const native = kind === 'docs' ? h.runtime : h.load(path.join(vendor, 'apps/slides/src/main/ai-ipc.ts'));
    native.registerAiIpc();
  }
  return {h, p};
}

for (const kind of ['docs', 'slides']) {
  test(kind + ': actual native AI stream preserves legal delta and completion', async t => {
    const {h, p} = await streaming(t, kind);
    const pending = h.invoke('ai:stream', topEvent(h.wc), request('legal'));
    await p.entered.promise; p.release.resolve(); await pending;
    assert.deepEqual(h.wc.sent.filter(([c]) => c === 'ai:stream-chunk').map(([, v]) => v.type), ['delta', 'done']);
  });
  test(kind + ': actual native AI stream cannot deliver old-document chunks after reload', async t => {
    const {h, p} = await streaming(t, kind);
    const pending = settle(h.invoke('ai:stream', topEvent(h.wc), request('retired')));
    await p.entered.promise; h.wc.sent.length = 0;
    h.wc.navigate(h.wc.getURL()); p.release.resolve(); await pending;
    assert.equal(h.wc.sent.filter(([c]) => c === 'ai:stream-chunk').length, 0,
      'original stream delivered data/completion into a new renderer document');
    assert.equal(p.options.signal.aborted, true, 'navigation must retire the active original stream');
  });
  test(kind + ': actual native AI cancellation is scoped to the original renderer owner', async t => {
    const {h, p} = await streaming(t, kind);
    const pending = settle(h.invoke('ai:stream', topEvent(h.wc), request('owner-private')));
    await p.entered.promise;
    const otherKind = kind === 'docs' ? 'slides' : 'docs';
    const file = '/runtime/' + otherKind + '/renderer/index.html';
    const other = new Contents('file://' + file); t.after(() => other.destroy());
    h.utils.registerOfficeRenderer(other, {kind: otherKind, rendererFile: file});
    await settle(h.invoke('ai:stream-cancel', topEvent(other), 'owner-private'));
    const crossOwnerAborted = p.options.signal.aborted;
    await h.invoke('ai:stream-cancel', topEvent(h.wc), 'owner-private');
    const legitimateOwnerAborted = p.options.signal.aborted;
    p.release.resolve(); await pending;
    assert.equal(crossOwnerAborted, false, 'another current editor canceled a stream it does not own');
    assert.equal(legitimateOwnerAborted, true, 'same current owner must retain its stop action');
  });
}

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1sAAAAASUVORK5CYII=', 'base64');
async function images(t, mode) {
  const h = createHarness('slides', {realAi: true, realEngine: true}); t.after(h.close);
  const engine = h.load(path.join(vendor, 'packages/pptx-engine/src/index.ts'));
  const bytes = Buffer.from(await engine.createBlankPptx()); h.files.set(h.originalPath, bytes);
  await h.openStandalone();
  const state = h.load(path.join(vendor, 'apps/slides/src/main/session-state.ts'));
  const session = state.sessions.get(h.wc.id), slide = session.opened.deck.slides[0];
  let sourceId;
  if (mode === 'replace') {
    sourceId = engine.addPicture(session.opened, slide, {bytes: Uint8Array.from(png), ext: 'png',
      offset: {x: 0, y: 0, cx: 952500, cy: 952500}}).id;
  }
  assert.equal(h.handles.has('ai:insert-image-url'), true, 'native slides factory must register image insertion');
  assert.equal(h.handles.has('ai:replace-picture-url'), true, 'native slides factory must register picture replacement');
  const snapshot = () => ({slides: JSON.stringify(session.opened.deck.slides),
    entries: [...session.opened.archive.entries].map(([k, v]) => [k, Buffer.from(v).toString('base64')]),
    undo: session.undoStack.length, redo: session.redoStack.length});
  const entered = deferred(), release = deferred();
  const response = {ok: true, status: 200, headers: {get: key => key === 'content-type' ? 'image/png' : null},
    async arrayBuffer() {return Uint8Array.from(png).buffer;}};
  h.fetch = async () => response;
  return {h, session, snapshot, entered, release, response,
    invoke: () => settle(h.invoke(mode === 'replace' ? 'ai:replace-picture-url' : 'ai:insert-image-url', topEvent(h.wc), {
      slideIndex: 0, sourceId, url: 'https://93.184.216.34/synthetic.png',
      xPx: 0, yPx: 0, wPx: 100, hPx: 100, fitWidthPx: 960,
    }))};
}
for (const mode of ['insert', 'replace']) {
  test('slides: actual image URL ' + mode + ' preserves a legal current-document edit', async t => {
    const h = await images(t, mode); const before = h.snapshot();
    const result = await h.invoke();
    assert.equal(Boolean(result && !result.error), true, 'legal real-engine image mutation must complete');
    assert.equal(h.session.undoStack.length, before.undo + 1);
  });
  for (const boundary of ['fetch', 'array-buffer']) {
    test('slides: actual image URL ' + mode + ' cannot mutate after navigation during ' + boundary, async t => {
      const h = await images(t, mode), before = h.snapshot();
      if (boundary === 'fetch') h.h.fetch = async () => {h.entered.resolve(); await h.release.promise; return h.response;};
      else h.response.arrayBuffer = async () => {h.entered.resolve(); await h.release.promise; return Uint8Array.from(png).buffer;};
      const pending = h.invoke(); await h.entered.promise;
      h.h.wc.navigate(h.h.wc.getURL()); h.release.resolve(); await pending;
      assert.deepEqual(h.snapshot(), before, 'old image URL IPC mutated real deck/archive/undo history after its document retired');
    });
  }
}
