const assert=require('node:assert/strict');
const test=require('node:test');
const {createHarness,topEvent,deferred}=require('./nativeOfficeHarness.cjs');
const settled=async p=>{try{return await p;}catch(error){return{error:String(error)};}};
function request(h){
  const op={sessionId:h.opened.sessionId,mode:'save',edits:[{sheetId:'sheet1',row:0,column:0,writeValue:true,value:'synthetic edit'}],definedNamesState:null};
  for(const name of ['structuralOps','chartEdits','visualEdits','visualAdditions','tableAdditions','pivotAdditions','sheetOps','sheetOrder',
    'filterStates','hyperlinkEdits','cfStates','dvStates','pageSetupStates','noteStates','formulaValues','pivotCacheRefreshPaths','pivotRefreshUpdates','sheetProtections'])op[name]=[];
  return op;
}
async function setup(t){
  const h=createHarness('sheets');t.after(h.close);await h.openStandalone();h.sidecarClosed=[];
  h.published=[];h.runtime.setSheetsWorkbookOpenedHook((wc,path)=>h.published.push({wc,path}));return h;
}
function retained(h){
  assert.equal(h.runtime.resolveSheetsSessionPath(h.wc.id,h.opened.sessionId),h.originalPath,'healthy original session was deleted');
  assert.equal(h.sidecarClosed.includes(h.opened.sessionId),false,'healthy original sidecar handle was closed');
}
function discarded(h,fresh){
  assert.equal(h.sidecarClosed.filter(id=>id===fresh.sessionId).length,1,'rejected fresh handle must close exactly once');
  assert.equal(h.runtime.resolveSheetsSessionPath(h.wc.id,fresh.sessionId),null,'rejected fresh handle was registered');
}

test('sheets: legal save reopens a fresh handle and closes only the previous handle',async t=>{
  const h=await setup(t),old=h.opened.sessionId;
  const result=await h.invoke('workbook:save',topEvent(h.wc),request(h));
  assert.equal(result.canceled,false);assert.equal(result.file.path,h.originalPath);
  assert.notEqual(result.file.sessionId,old);assert.deepEqual(h.sidecarClosed,[old]);
  assert.equal(h.runtime.resolveSheetsSessionPath(h.wc.id,old),null);
  assert.equal(h.runtime.resolveSheetsSessionPath(h.wc.id,result.file.sessionId),h.originalPath);
  assert.equal(h.published.length,1);
});

test('sheets: navigation during save reopen closes rejected fresh handle and retains healthy original',async t=>{
  const h=await setup(t),entered=deferred(),release=deferred();t.after(()=>release.resolve());
  let fresh;h.sidecarOpen=async(_path,opened)=>{fresh=opened;entered.resolve();await release.promise;};
  const pending=settled(h.invoke('workbook:save',topEvent(h.wc),request(h)));
  await entered.promise;
  assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,1,'this barrier must occur after authorized disk save');
  retained(h);h.wc.navigate(h.wc.getURL());release.resolve();const result=await pending;
  assert.notEqual(result.canceled,false,'retired save must not report completion');
  discarded(h,fresh);retained(h);assert.equal(h.published.length,0,'retired reopen published new document binding');
});

for(const fault of ['digest-error','invalid-schema'])test('sheets: '+fault+' during new open closes fresh handle and retains healthy original',async t=>{
  const h=await setup(t);let fresh;
  h.sidecarOpen=async(_path,opened)=>{fresh=opened;if(fault==='invalid-schema')opened.entryCount='invalid';};
  if(fault==='digest-error')h.digestBarrier=async()=>{throw Object.assign(new Error('synthetic EIO'),{code:'EIO'});};
  const result=await settled(h.invoke('workbook:select',topEvent(h.wc)));
  assert.ok(result.error,'real new-open fault must reject');discarded(h,fresh);retained(h);assert.equal(h.published.length,0);
});
