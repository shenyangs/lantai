const assert=require('node:assert/strict');
const test=require('node:test');
const {createHarness,Contents,topEvent,deferred}=require('./nativeOfficeHarness.cjs');
const settled=async p=>{try{return await p;}catch(error){return{error:String(error)};}};
async function setup(t,phase){
  const h=createHarness('slides');t.after(h.close);await h.openStandalone();
  const entered=deferred(),release=deferred();t.after(()=>release.resolve());
  const originalLoad=Contents.prototype.loadURL;
  let held=false,hidden,printCalls=0;
  const barrier=async p=>{if(!held&&p===phase){held=true;entered.resolve();await release.promise;}};
  Contents.prototype.loadURL=async function(url){hidden=this;await originalLoad.call(this,url);await barrier('load');};
  Contents.prototype.executeJavaScript=async function(){hidden=this;await barrier('fonts');};
  Contents.prototype.print=function(_options,callback){printCalls++;callback(true);};
  t.after(()=>{Contents.prototype.loadURL=originalLoad;delete Contents.prototype.executeJavaScript;delete Contents.prototype.print;});
  return{h,entered,release,printCalls:()=>printCalls,hidden:()=>hidden,
    invoke:()=>h.invoke('slides:print',topEvent(h.wc),{layout:'full',widthPx:1600,heightPx:900,pngsBase64:['c3ludGhldGljIG9mZmxpbmU=']})};
}
for(const phase of ['load','fonts']){
  test('slides: actual print remains usable after '+phase+' wait',async t=>{
    const p=await setup(t,phase),pending=p.invoke();await p.entered.promise;p.release.resolve();
    assert.equal((await pending).ok,true);assert.equal(p.printCalls(),1);assert.equal(p.hidden().isDestroyed(),true);
  });
  test('slides: actual print cannot invoke system print after '+phase+' wait retires original document',async t=>{
    const p=await setup(t,phase),pending=settled(p.invoke());await p.entered.promise;
    p.h.wc.navigate(p.h.wc.getURL());p.release.resolve();await pending;
    assert.equal(p.printCalls(),0,'retired native IPC invoked privileged print after navigation');
    assert.equal(p.hidden().isDestroyed(),true,'hidden print resource must settle after failure');
  });
}
