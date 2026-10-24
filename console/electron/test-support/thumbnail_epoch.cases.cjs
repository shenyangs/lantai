const assert=require('node:assert/strict');
const test=require('node:test');
const path=require('node:path');
const {createHarness,topEvent,deferred,vendor}=require('./nativeOfficeHarness.cjs');
const settled=async p=>{try{return await p;}catch(error){return{error:String(error)};}};
async function setup(t,kind){
  const h=createHarness('slides',{realEngine:true});t.after(h.close);
  const engine=h.load(path.join(vendor,'packages/pptx-engine/src/index.ts'));
  h.files.set(h.originalPath,Buffer.from(await engine.createBlankPptx()));
  await h.openStandalone();
  const selected='/synthetic/selected.'+(kind==='video'?'webm':'glb');
  const bytes=kind==='video'?Buffer.from([0x1a,0x45,0xdf,0xa3,0x80]):Buffer.from('glTF\x02\x00\x00\x00\x0c\x00\x00\x00','binary');
  h.files.set(selected,bytes);
  h.openDialog=async()=>({canceled:false,filePaths:[selected]});
  h.thumbnail=async()=>({isEmpty:()=>true});
  const state=h.load(path.join(vendor,'apps/slides/src/main/session-state.ts'));
  const invoke=()=>kind==='video'?h.invoke('slides:insert-media',topEvent(h.wc),0,'video',1200):h.invoke('slides:insert-model3d',topEvent(h.wc),0,1200);
  return{h,state,invoke};
}
function snapshot(session){return{
  path:session.path,fitWidthPx:session.fitWidthPx,undo:session.undoStack.length,redo:session.redoStack.length,
  deck:JSON.stringify(session.opened.deck),
  entries:[...session.opened.archive.entries].map(([key,value])=>[key,Buffer.from(value).toString('base64')]),
};}
for(const kind of ['video','model3d']){
  test('slides: actual '+kind+' preserves normal insertion and undo with empty thumbnail fallback',async t=>{
    const{h,state,invoke}=await setup(t,kind),session=state.sessions.get(h.wc.id),before=snapshot(session);
    const result=await invoke();
    assert.ok(result?.sourceId,'legal actual insertion failed: '+JSON.stringify(result));
    assert.equal(session.undoStack.length,before.undo+1,'legal insertion must remain undoable');
    assert.notEqual(JSON.stringify(snapshot(session)),JSON.stringify(before));
    assert.equal(session.opened.deck.slides[0].elements.length,1);
  });
  test('slides: actual '+kind+' cannot mutate deck/archive/history after thumbnail wait retires document',async t=>{
    const{h,state,invoke}=await setup(t,kind),session=state.sessions.get(h.wc.id),before=snapshot(session);
    const entered=deferred(),release=deferred();t.after(()=>release.resolve());
    h.thumbnail=async()=>{entered.resolve();await release.promise;return{isEmpty:()=>true};};
    const pending=settled(invoke());await entered.promise;
    h.wc.navigate(h.wc.getURL());release.resolve();await pending;
    assert.equal(state.sessions.get(h.wc.id),session);
    assert.deepEqual(snapshot(session),before,'retired original thumbnail request mutated live session');
  });
}
