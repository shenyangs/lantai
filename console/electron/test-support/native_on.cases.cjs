const assert=require('node:assert/strict');
const test=require('node:test');
const {createHarness,topEvent,childEvent}=require('./nativeOfficeSimpleHarness.cjs');
function dirty(h,event,value){
  const listeners=h.listeners.get('pdf:dirty-changed');
  assert.ok(listeners?.length,'actual PDF dirty-change IPC registration missing');
  for(const fn of listeners)fn(event,value);
}
test('pdf: actual fire-and-forget dirty IPC accepts the current top document',async t=>{
  const h=createHarness('pdf');t.after(h.close);await h.open();
  assert.doesNotThrow(()=>dirty(h,topEvent(h.wc),true));
  assert.equal(h.runtime.pdfIsDirty(h.wc.id),true);
  dirty(h,topEvent(h.wc),false);assert.equal(h.runtime.pdfIsDirty(h.wc.id),false);
});
test('pdf: actual child-frame dirty IPC is discarded without throwing or changing state',async t=>{
  const h=createHarness('pdf');t.after(h.close);await h.open();
  assert.doesNotThrow(()=>dirty(h,childEvent(h.wc),true));
  assert.equal(h.runtime.pdfIsDirty(h.wc.id),false,'child IPC mutated native dirty/close-save state');
});
test('pdf: current top-document dirty IPC remains available after a same-URL reload',async t=>{
  const h=createHarness('pdf');t.after(h.close);await h.open();h.wc.navigate(h.wc.url);
  assert.doesNotThrow(()=>dirty(h,topEvent(h.wc),true));assert.equal(h.runtime.pdfIsDirty(h.wc.id),true);
});
test('pdf: old fire-and-forget IPC is retired at navigation start before the main frame changes',async t=>{
  const h=createHarness('pdf');t.after(h.close);await h.open();const event=topEvent(h.wc);
  h.wc.emit('did-start-navigation',{},h.wc.url,false,true,event.senderFrame.processId,event.senderFrame.routingId);
  assert.doesNotThrow(()=>dirty(h,event,true));
  assert.equal(h.runtime.pdfIsDirty(h.wc.id),false,'old top frame mutated state while replacement navigation was pending');
});
