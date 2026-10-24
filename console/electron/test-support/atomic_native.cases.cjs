const assert=require('node:assert/strict');
const test=require('node:test');
const {createHarness,topEvent,deferred}=require('./nativeOfficeSimpleHarness.cjs');
const saved=Buffer.from('PK\x03\x04actual native atomic synthetic edit');
function save(h){return h.invoke('docs:save',topEvent(h.wc),h.originalPath,Uint8Array.from(saved).buffer);}
async function settled(fn){try{return await fn();}catch(e){return{ok:false,error:String(e)};}}
test('docs: actual native atomic helper commits a legal current document',async t=>{
  const h=createHarness('docs',{realAtomic:true});t.after(h.close);await h.open();const result=await save(h);
  assert.equal(result.ok,true,JSON.stringify(result));assert.deepEqual(h.files.get(h.originalPath),saved);
  assert.equal(h.promotions.filter(p=>p.to===h.originalPath).length,1);
});
test('docs: actual atomic promotion retains initial IPC authorization across temporary write',async t=>{
  const h=createHarness('docs',{realAtomic:true});t.after(h.close);await h.open();const entered=deferred(),release=deferred();
  h.afterWrite=async p=>{if(p.endsWith('.tmp')){entered.resolve();await release.promise;}};
  const pending=settled(()=>save(h));await entered.promise;h.wc.navigate(h.wc.url);release.resolve();const result=await pending;
  assert.deepEqual(h.files.get(h.originalPath),h.initial,'old IPC promoted bytes over original after temporary-file await crossed reload');
  assert.equal(h.promotions.filter(p=>p.to===h.originalPath).length,0);assert.notEqual(result.ok,true);
});
test('docs: actual native atomic helper preserves legal transient-rename retry',async t=>{
  const h=createHarness('docs',{realAtomic:true});t.after(h.close);await h.open();h.renameTransientFailures=1;
  const result=await save(h);assert.equal(result.ok,true,JSON.stringify(result));assert.deepEqual(h.files.get(h.originalPath),saved);
});
test('docs: actual atomic retry rechecks original document after rename backoff',async t=>{
  const h=createHarness('docs',{realAtomic:true});t.after(h.close);await h.open();h.renameTransientFailures=1;h.renameFaultEntered=deferred();
  const pending=settled(()=>save(h));await h.renameFaultEntered.promise;h.wc.navigate(h.wc.url);const result=await pending;
  assert.deepEqual(h.files.get(h.originalPath),h.initial,'old IPC renamed original after transient retry crossed reload');
  assert.equal(h.promotions.filter(p=>p.to===h.originalPath).length,0);assert.notEqual(result.ok,true);
});
