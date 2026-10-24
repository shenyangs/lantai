const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const {createHarness, topEvent, deferred, vendor} = require('./nativeOfficeHarness.cjs');
async function settled(promise) {try {return await promise;} catch (error) {return {error:String(error)};}}

async function slides(t) {
  const h=createHarness('slides',{realEngine:true});t.after(h.close);
  const engine=h.load(path.join(vendor,'packages/pptx-engine/src/index.ts'));
  h.files.set(h.originalPath,Buffer.from(await engine.createBlankPptx()));
  await h.openStandalone();
  const state=h.load(path.join(vendor,'apps/slides/src/main/session-state.ts'));
  return {h,state};
}
test('slides: actual new-blank retains legal current-document session creation',async t=>{
  const {h,state}=await slides(t),previous=state.sessions.get(h.wc.id);
  const result=await h.invoke('slides:new-blank',topEvent(h.wc),1280);
  assert.equal(result.path,'');assert.notEqual(state.sessions.get(h.wc.id),previous);
  assert.equal(state.sessions.get(h.wc.id).opened.deck.slides.length,1);
});
test('slides: an asynchronous new-blank cannot overwrite a replacement document session',async t=>{
  const {h,state}=await slides(t),previous=state.sessions.get(h.wc.id);
  const pending=settled(h.invoke('slides:new-blank',topEvent(h.wc),1280));
  h.wc.navigate(h.wc.getURL());await pending;
  assert.equal(state.sessions.get(h.wc.id),previous,
    'retired new-blank IPC published a newly parsed session into replacement document');
});

async function sheets(t) {
  const h=createHarness('sheets');t.after(h.close);await h.openStandalone();
  h.workbookPublished=[];
  h.runtime.setSheetsWorkbookOpenedHook((wc,path)=>h.workbookPublished.push({wc,path}));
  return h;
}
test('sheets: legal sidecar open publishes a current-document session and binding',async t=>{
  const h=await sheets(t);
  const opened=await h.invoke('workbook:select',topEvent(h.wc));
  assert.equal(opened.path,h.originalPath);assert.equal(h.workbookPublished.length,1);
  assert.equal(h.workbookPublished[0].wc,h.wc);
});
test('sheets: sidecar result after navigation cannot publish a replacement document binding',async t=>{
  const h=await sheets(t),entered=deferred(),release=deferred();t.after(()=>release.resolve());
  h.sidecarClosed=[];
  h.sidecarOpen=async(_path,opened)=>{h.pendingSidecar=opened;entered.resolve();await release.promise;};
  h.digestFinished=deferred();
  const pending=settled(h.invoke('workbook:select',topEvent(h.wc)));
  await entered.promise;await h.digestFinished.promise;
  h.wc.navigate(h.wc.getURL());release.resolve();await pending;
  assert.equal(h.workbookPublished.length,0,
    'retired sidecar open published original IPC file binding to replacement document');
  assert.deepEqual(h.sidecarClosed,[h.pendingSidecar.sessionId],'rejected new sidecar handle must be closed');
  assert.equal(h.runtime.resolveSheetsSessionPath(h.wc.id,h.pendingSidecar.sessionId),null,'rejected new handle must not be registered');
  assert.equal(h.runtime.resolveSheetsSessionPath(h.wc.id,h.opened.sessionId),h.originalPath,'original healthy session must remain registered');
  assert.equal(h.sidecarClosed.includes(h.opened.sessionId),false,'original healthy handle must not be closed');
});
