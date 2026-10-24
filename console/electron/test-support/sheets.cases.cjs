const assert=require('node:assert/strict');
const test=require('node:test');
const {createHarness,Contents,topEvent,childEvent,deferred}=require('./nativeOfficeSimpleHarness.cjs');
function saveRequest(h,mode='save'){
  const request={sessionId:h.opened.sessionId,mode,edits:[{sheetId:'sheet1',row:0,column:0,writeValue:true,value:'synthetic edit'}],definedNamesState:null};
  for(const name of ['structuralOps','chartEdits','visualEdits','visualAdditions','tableAdditions','pivotAdditions','sheetOps','sheetOrder',
    'filterStates','hyperlinkEdits','cfStates','dvStates','pageSetupStates','noteStates','formulaValues','pivotCacheRefreshPaths','pivotRefreshUpdates','sheetProtections'])request[name]=[];
  return request;
}
async function settledInvoke(fn){try{return await fn();}catch(error){return{ok:false,error:String(error)};}}
test('sheets: actual host + native current top-document save remains available',async t=>{
  const h=createHarness('sheets');t.after(h.close);await h.open();
  const result=await h.invoke('workbook:save',topEvent(h.wc),saveRequest(h));
  assert.equal(result.canceled,false,JSON.stringify(result));assert.equal(result.file.path,h.originalPath);
  assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,1);
});
test('sheets: legal Save As keeps selected target and edited file',async t=>{
  const h=createHarness('sheets');t.after(h.close);await h.open();
  const result=await h.invoke('workbook:save',topEvent(h.wc),saveRequest(h,'save-as'));
  assert.equal(result.canceled,false,JSON.stringify(result));assert.equal(result.file.path,'/synthetic/copy.xlsx');
  assert.equal(h.writes.filter(w=>w.path==='/synthetic/copy.xlsx').length,1);
});
for(const attack of ['child-frame','different-object-same-id','stale-frame-after-same-url-reload','foreign-document'])test('sheets: native save refuses '+attack,async t=>{
  const h=createHarness('sheets');t.after(h.close);await h.open();let event;
  if(attack==='child-frame')event=childEvent(h.wc);
  if(attack==='different-object-same-id'){const other=new Contents(h.wc.url);other.id=h.wc.id;event=topEvent(other);}
  if(attack==='stale-frame-after-same-url-reload'){event=topEvent(h.wc);h.wc.navigate(h.wc.url);}
  if(attack==='foreign-document'){h.wc.navigate('https://untrusted.invalid/replacement');event=topEvent(h.wc);}
  const result=await settledInvoke(()=>h.invoke('workbook:save',event,saveRequest(h)));
  assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,0,'untrusted native IPC reached original-file workbook write sink');
  assert.notEqual(result?.canceled,false,'untrusted native IPC reported successful save');
});
test('sheets: native Save As does not transfer a pending write across navigation',async t=>{
  const h=createHarness('sheets');t.after(h.close);await h.open();const chosen=deferred(),entered=deferred();
  h.saveDialog=()=>{entered.resolve();return chosen.promise;};
  const pending=settledInvoke(()=>h.invoke('workbook:save',topEvent(h.wc),saveRequest(h,'save-as')));
  await entered.promise;h.wc.navigate(h.wc.url);chosen.resolve({canceled:false,filePath:'/synthetic/overwrite-existing.xlsx'});
  const result=await pending;
  assert.equal(h.writes.filter(w=>w.path.includes('overwrite-existing')).length,0,'old pending dialog authorization reached workbook write after navigation');
  assert.notEqual(result?.canceled,false);
});
test('sheets: native save rechecks after asynchronous source digest and before write',async t=>{
  const h=createHarness('sheets');t.after(h.close);await h.open();const digestEntered=deferred(),release=deferred();
  h.digestBarrier=async p=>{if(p===h.originalPath){digestEntered.resolve();await release.promise;}};
  const pending=settledInvoke(()=>h.invoke('workbook:save',topEvent(h.wc),saveRequest(h)));
  await digestEntered.promise;h.wc.navigate(h.wc.url);release.resolve();
  const result=await pending;
  assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,0,'digest crossed navigation then authorized old workbook write');
  assert.notEqual(result?.canceled,false);
});
module.exports={saveRequest};
