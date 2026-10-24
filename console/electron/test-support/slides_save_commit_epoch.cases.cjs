const assert=require('node:assert/strict');
const test=require('node:test');
const path=require('node:path');
const {createHarness,topEvent,deferred,vendor}=require('./nativeOfficeHarness.cjs');
const settled=async p=>{try{return await p;}catch(error){return{error:String(error)};}};
async function setup(t){
  const h=createHarness('slides',{realEngine:true});t.after(h.close);
  const engine=h.load(path.join(vendor,'packages/pptx-engine/src/index.ts'));
  h.files.set(h.originalPath,Buffer.from(await engine.createBlankPptx()));
  await h.openStandalone();
  const state=h.load(path.join(vendor,'apps/slides/src/main/session-state.ts'));
  const session=state.sessions.get(h.wc.id);
  engine.addElement(session.opened.deck.slides[0],{kind:'rect',offset:{x:100,y:100,cx:10000,cy:10000},fillColor:'#BB6633'});
  session.metaDirty=true;
  let openedHooks=0;h.runtime.setSlidesOpenedHook(()=>openedHooks++);
  return{h,engine,state,session,hooks:()=>openedHooks};
}
function snapshot(session){return{
  path:session.path,metaDirty:session.metaDirty,fitWidthPx:session.fitWidthPx,
  undo:session.undoStack.length,redo:session.redoStack.length,deck:JSON.stringify(session.opened.deck),
  entries:[...session.opened.archive.entries].map(([key,value])=>[key,Buffer.from(value).toString('base64')]),
};}
for(const channel of ['slides:save','slides:save-as']){
  test(channel+': actual streamed save preserves successful commit and binding',async t=>{
    const{h,engine,session,hooks}=await setup(t);
    const result=await h.invoke(channel,topEvent(h.wc),'copy.pptx');
    assert.equal(result.ok,true,'legal actual save failed: '+JSON.stringify(result));
    assert.equal(session.metaDirty,false);
    const target=channel==='slides:save'?h.originalPath:'/synthetic/copy.pptx';
    assert.equal(session.path,target);assert.equal(hooks(),channel==='slides:save'?0:1);
    const parsed=await engine.openPptx(h.files.get(target));
    assert.equal(parsed.deck.slides[0].elements.length,1);
    assert.equal(h.promotions.filter(p=>p.to===target).length,1);
  });
  test(channel+': completed disk promotion cannot commit or rebind retired document after await',async t=>{
    const{h,engine,state,session,hooks}=await setup(t);
    const entered=deferred(),release=deferred();t.after(()=>release.resolve());
    const target=channel==='slides:save'?h.originalPath:'/synthetic/copy.pptx';
    h.afterRename=async(_from,to)=>{if(to===target){entered.resolve();await release.promise;}};
    const pending=settled(h.invoke(channel,topEvent(h.wc),'copy.pptx'));
    await entered.promise;const before=snapshot(session);
    const parsed=await engine.openPptx(h.files.get(target));
    assert.equal(parsed.deck.slides[0].elements.length,1,'disk promotion authorized before navigation must finish normally');
    h.wc.navigate(h.wc.getURL());release.resolve();await pending;
    assert.equal(state.sessions.get(h.wc.id),session);
    assert.deepEqual(snapshot(session),before,'old save request mutated/rebound shared session after navigation');
    assert.equal(hooks(),0,'old save request announced binding after navigation');
  });
}
