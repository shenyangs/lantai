const assert=require('node:assert/strict');
const test=require('node:test');
const {createHarness,Contents,topEvent,childEvent,deferred}=require('./nativeOfficeSimpleHarness.cjs');
const data=()=>Uint8Array.from(Buffer.from('PK\x03\x04synthetic edited document')).buffer;
async function save(h,event){
  if(h.kind==='docs')return h.invoke('docs:save',event,h.originalPath,data());
  if(h.kind==='pdf')return h.invoke('pdf:save',event,{path:h.originalPath,textEdits:[],imageEdits:[],annotations:[]});
  return h.invoke('slides:save',event);
}
async function settledInvoke(fn){try{return await fn();}catch(e){return{ok:false,error:String(e)};}}
for(const kind of ['docs','pdf','slides']) {
  test(kind+': actual host + native current top-document save remains available',async t=>{
    const h=createHarness(kind);t.after(h.close);await h.open();
    const result=await save(h,topEvent(h.wc));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,1);
  });
  for(const attack of ['child-frame','different-object-same-id','stale-frame-after-same-url-reload','foreign-document'])test(kind+': native save refuses '+attack,async t=>{
    const h=createHarness(kind);t.after(h.close);await h.open();let event;
    if(attack==='child-frame')event=childEvent(h.wc);
    if(attack==='different-object-same-id'){const other=new Contents(h.wc.url);other.id=h.wc.id;event=topEvent(other);}
    if(attack==='stale-frame-after-same-url-reload'){event=topEvent(h.wc);h.wc.navigate(h.wc.url);}
    if(attack==='foreign-document'){h.wc.navigate('https://untrusted.invalid/replacement');event=topEvent(h.wc);}
    const result=await settledInvoke(()=>save(h,event));
    assert.equal(h.writes.filter(w=>w.path===h.originalPath).length,0,'untrusted native IPC reached original-file write sink');
    assert.notEqual(result?.ok,true,'untrusted native IPC reported successful save');
  });
}
test('docs: unknown WebContents cannot silently create a new document through save-new',async t=>{
  const h=createHarness('docs');t.after(h.close);await h.open();
  const stranger=new Contents('https://untrusted.invalid/');
  const result=await settledInvoke(()=>h.invoke('docs:save-new',topEvent(stranger),'unexpected.docx',data()));
  assert.equal(h.writes.filter(w=>w.path.endsWith('/unexpected.docx')).length,0,'unknown sender created a native document');
  assert.notEqual(result?.ok,true);
});
for(const kind of ['docs','slides'])test(kind+': native Save As dialog cannot transfer a pending write across navigation',async t=>{
  const h=createHarness(kind);t.after(h.close);await h.open();const chosen=deferred(),entered=deferred();
  h.saveDialog=()=>{entered.resolve();return chosen.promise;};
  const pending=settledInvoke(()=>h.invoke(kind+':save-as',topEvent(h.wc),'copy'+(kind==='docs'?'.docx':'.pptx'),...(kind==='docs'?[data()]:[])));
  await entered.promise;h.wc.navigate(h.wc.url);chosen.resolve({canceled:false,filePath:'/synthetic/overwrite-existing'+(kind==='docs'?'.docx':'.pptx')});
  const result=await pending;
  assert.equal(h.writes.filter(w=>w.path.includes('overwrite-existing')).length,0,'old pending dialog authorization reached irreversible write after navigation');
  assert.notEqual(result?.ok,true);
});
