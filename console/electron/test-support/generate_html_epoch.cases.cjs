const assert=require('node:assert/strict');
const test=require('node:test');
const path=require('node:path');
const {createHarness,topEvent,deferred,vendor}=require('./nativeOfficeHarness.cjs');
const settled=async p=>{try{return await p;}catch(error){return{error:String(error)};}};

async function setup(t){
  let generated;
  const bridge={hasLantaiDesktopBridge:()=>true,generateOfficeSlide:async()=>({bytes:generated,model:'synthetic-offline'})};
  const h=createHarness('slides',{realEngine:true,bridge});t.after(h.close);
  const engine=h.load(path.join(vendor,'packages/pptx-engine/src/index.ts'));
  const blank=await engine.createBlankPptx();h.files.set(h.originalPath,Buffer.from(blank));
  const page=await engine.openPptx(blank);
  engine.addElement(page.deck.slides[0],{kind:'rect',offset:{x:100,y:100,cx:10000,cy:10000},fillColor:'#E02222'});
  generated=await engine.savePptx(page);
  await h.openStandalone();
  const issued=await h.invoke('slides:cloud-page-generate',topEvent(h.wc),{brief:'synthetic offline page'});
  assert.equal(issued.ok,true);assert.ok(issued.marker.startsWith('cloudpptx:'));
  const state=h.load(path.join(vendor,'apps/slides/src/main/session-state.ts'));
  return{h,engine,state,marker:issued.marker,source:generated};
}
function snapshot(session){return{
  path:session.path,fitWidthPx:session.fitWidthPx,undo:session.undoStack.length,redo:session.redoStack.length,
  slides:JSON.stringify(session.opened.deck.slides),
  entries:[...session.opened.archive.entries].map(([key,value])=>[key,Buffer.from(value).toString('base64')]),
};}
for(const mode of ['append','replace_at','insert_at','replace']){
  test('slides: actual generated-page '+mode+' preserves legal edits and undo',async t=>{
    const {h,state,marker}=await setup(t),old=state.sessions.get(h.wc.id),before=snapshot(old),history=old.undoStack.length;
    const result=await h.invoke('slides:html-to-pptx',topEvent(h.wc),[marker],1280,mode,0,'synthetic');
    assert.ok(result&&!result.error,'legal real generated-page operation failed: '+JSON.stringify(result));
    const current=state.sessions.get(h.wc.id);
    assert.equal(current.opened.deck.slides.length,mode==='append'||mode==='insert_at'?2:1);
    assert.equal(current.undoStack.length,history+1,'normal generated-page edit must remain undoable');
    assert.notEqual(JSON.stringify(snapshot(current)),JSON.stringify(before));
  });
  test('slides: actual generated-page '+mode+' cannot mutate a retired document after ZIP parsing wait',async t=>{
    const {h,state,marker,source}=await setup(t),old=state.sessions.get(h.wc.id),before=snapshot(old);
    const entered=deferred(),release=deferred();t.after(()=>release.resolve());
    let held=false;
    h.zipLoadBarrier=async bytes=>{
      if(!held&&Buffer.from(bytes).equals(Buffer.from(source))){held=true;entered.resolve();await release.promise;}
    };
    const pending=settled(h.invoke('slides:html-to-pptx',topEvent(h.wc),[marker],1280,mode,0,'synthetic'));
    await entered.promise;h.wc.navigate(h.wc.getURL());release.resolve();await pending;
    assert.equal(state.sessions.get(h.wc.id),old,'retired generated-page operation replaced live session');
    assert.deepEqual(snapshot(old),before,'retired generated-page operation mutated live deck/archive after real ZIP wait');
  });
}
