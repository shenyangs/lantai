const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const {createHarness, topEvent, deferred, vendor} = require('./nativeOfficeHarness.cjs');

async function setup(t) {
  const h = createHarness('slides', {realEngine: true}); t.after(h.close);
  const engine = h.load(path.join(vendor, 'packages/pptx-engine/src/index.ts'));
  h.files.set(h.originalPath, Buffer.from(await engine.createBlankPptx()));
  await h.openStandalone();
  const state = h.load(path.join(vendor, 'apps/slides/src/main/session-state.ts'));
  return {h, state};
}

test('actual native open-path preserves a legal real-engine session replacement', async t => {
  const {h, state} = await setup(t), previous = state.sessions.get(h.wc.id);
  const opened = await h.invoke('slides:open-path', topEvent(h.wc), h.originalPath, 1280);
  assert.equal(opened.path, h.originalPath);
  assert.notEqual(state.sessions.get(h.wc.id), previous);
  assert.equal(state.sessions.get(h.wc.id).opened.deck.slides.length, 1);
});

test('actual native open-path cannot replace current session after navigation during font readiness', async t => {
  const {h, state} = await setup(t), previous = state.sessions.get(h.wc.id);
  const entered = deferred(), release = deferred(); t.after(() => release.resolve());
  h.shapedBarrier = async () => {entered.resolve(); await release.promise;};
  const pending = h.invoke('slides:open-path', topEvent(h.wc), h.originalPath, 1280).catch(() => null);
  await entered.promise; h.wc.navigate(h.wc.getURL()); release.resolve(); await pending;
  assert.equal(state.sessions.get(h.wc.id), previous,
    'retired open-path request published a real newly parsed session for the new document');
});
