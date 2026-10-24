const assert=require('node:assert/strict');
const test=require('node:test');
const {createHarness,topEvent,childEvent}=require('./nativeOfficeSimpleHarness.cjs');
const data=()=>Uint8Array.from(Buffer.from('PK\x03\x04synthetic edited document')).buffer;
function sheetRequest(h){
  const r={sessionId:h.opened.sessionId,mode:'save',edits:[{sheetId:'sheet1',row:0,column:0,writeValue:true,value:'synthetic edit'}],definedNamesState:null};
  for(const k of ['structuralOps','chartEdits','visualEdits','visualAdditions','tableAdditions','pivotAdditions','sheetOps','sheetOrder',
    'filterStates','hyperlinkEdits','cfStates','dvStates','pageSetupStates','noteStates','formulaValues','pivotCacheRefreshPaths','pivotRefreshUpdates','sheetProtections'])r[k]=[];
  return r;
}
async function save(h,event){
  if(h.kind==='docs')return h.invoke('docs:save',event,h.originalPath,data());
  if(h.kind==='pdf')return h.invoke('pdf:save',event,{path:h.originalPath,textEdits:[],imageEdits:[],annotations:[]});
  if(h.kind==='sheets')return h.invoke('workbook:save',event,sheetRequest(h));
  return h.invoke('slides:save',event);
}
function assertSuccess(h,result){if(h.kind==='sheets')assert.equal(result.canceled,false,JSON.stringify(result));else assert.equal(result.ok,true,JSON.stringify(result));}
async function settled(fn){try{return await fn();}catch(e){return{ok:false,error:String(e)};}}
for(const kind of ['docs','pdf','slides','sheets']){
  test(kind+': current document after same-URL reload can still save granted file',async t=>{
    const h=createHarness(kind);t.after(h.close);await h.open();h.wc.navigate(h.wc.url);
    const result=await save(h,topEvent(h.wc));assertSuccess(h,result);
    assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,1);
  });
  test(kind+': actual standalone factory admits its current top document',async t=>{
    const h=createHarness(kind);t.after(h.close);await h.openStandalone();
    const result=await save(h,topEvent(h.wc));assertSuccess(h,result);
    assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,1);
  });
  test(kind+': actual standalone factory rejects child frame save',async t=>{
    const h=createHarness(kind);t.after(h.close);await h.openStandalone();
    const result=await settled(()=>save(h,childEvent(h.wc)));
    assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,0,'standalone iframe reached original-file write sink');
    if(kind==='sheets')assert.notEqual(result.canceled,false);else assert.notEqual(result.ok,true);
  });
}
for(const kind of ['docs','slides'])test(kind+': current document can legally Save As chosen target',async t=>{
  const h=createHarness(kind);t.after(h.close);await h.open();
  const result=await h.invoke(kind+':save-as',topEvent(h.wc),'copy'+(kind==='docs'?'.docx':'.pptx'),...(kind==='docs'?[data()]:[]));
  assert.equal(result.ok,true,JSON.stringify(result));
  assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,0);
  assert.equal(h.writes.filter(w=>w.path==='/synthetic/copy'+(kind==='docs'?'.docx':'.pptx')).length,1);
});
test('pdf: legal Save As uses actual choose-save-as, renderer save and acknowledgement',async t=>{
  const h=createHarness('pdf');t.after(h.close);await h.open();let rendererResult;
  const send=h.wc.send.bind(h.wc);h.wc.send=(channel,...args)=>{
    send(channel,...args);if(channel!=='pdf:save-as-request')return;
    queueMicrotask(async()=>{
      rendererResult=await settled(()=>h.invoke('pdf:save',topEvent(h.wc),{path:h.originalPath,targetPath:args[0],textEdits:[],imageEdits:[],annotations:[]}));
      for(const listener of h.listeners.get('pdf:save-as-result')||[])listener(topEvent(h.wc),rendererResult.ok===true);
    });
  };
  const result=await h.invoke('pdf:choose-save-as',topEvent(h.wc),h.originalPath);
  assert.equal(result,true);assert.equal(rendererResult.ok,true,JSON.stringify(rendererResult));
  assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,0);
  assert.equal(h.writes.filter(w=>w.path==='/synthetic/copy.pdf').length,1);
});
