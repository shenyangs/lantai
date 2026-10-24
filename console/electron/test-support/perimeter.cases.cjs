const assert = require('node:assert/strict');
const test = require('node:test');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const W = process.env.LANTAI_SOURCE_ROOT || path.resolve(__dirname, '../../..');
const {createDesktopSecurityHeaders} = require(path.join(W,'console/electron/desktopSecurityHeaders.cjs'));
const {createAppWindowController} = require(path.join(W,'console/electron/appWindowController.cjs'));
const {createDocxEditorController,DOCX_EDITOR_REWRITE_RESPONSE_CHANNEL} = require(path.join(W,'console/electron/docxEditorController.cjs'));
const API = 'http://127.0.0.1:19090';
const SYNTHETIC_TOKEN = 'synthetic-offline-only-token';
let route=10;
class Contents extends EventEmitter {
 constructor(id,url){ super(); this.id=id; this.sent=[]; this.dead=false; this.navigate(url, false); }
 getURL(){return this.url;}
 isDestroyed(){return this.dead;}
 setWindowOpenHandler(fn){this.windowOpenHandler=fn;}
 openDevTools(){}
 send(channel,payload){this.sent.push([channel,payload]);}
 navigate(url,emit=true){
  if(emit)this.emit('did-start-navigation',{},url,false,true,1,++route);
  this.url=url; this.mainFrame={url,parent:null,processId:1,routingId:++route}; this.mainFrame.top=this.mainFrame;
  if(emit)this.emit('did-frame-navigate',{},url,200,'OK',true,this.mainFrame.processId,this.mainFrame.routingId);
 }
}
function topEvent(wc){return{sender:wc,senderFrame:wc.mainFrame};}
function childEvent(wc){return{sender:wc,senderFrame:{url:wc.getURL(),parent:wc.mainFrame,top:wc.mainFrame,processId:1,routingId:++route}};}
function headerHarness(){
 const wc=new Contents(10,API+'/#/chat'); let hook;
 const trusted=(d)=>d.webContentsId===wc.id && d.frame===wc.mainFrame && d.frame?.parent===null && d.frame?.url===wc.getURL();
 const controller=createDesktopSecurityHeaders({
  session:{defaultSession:{webRequest:{onBeforeSendHeaders:(_f,fn)=>{hook=fn;}}}},
  backendLifecycle:{isCurrentBackendApiUrl:(url)=>url.startsWith(API+'/api/'),getDesktopToken:()=>SYNTHETIC_TOKEN},
  // This is supplied to specify the eventual admission contract; current implementation ignores it.
  isTrustedBackendRequest:trusted,isTrustedWebRequest:trusted,
 }); controller.install();
 return {wc,invoke:(details)=>{let result;hook({url:API+'/api/config',requestHeaders:{},resourceType:'xhr',...details},v=>result=v);return result;}};
}
test('token: valid current main request remains authenticated',()=>{
 const h=headerHarness(); assert.equal(h.invoke({webContentsId:h.wc.id,frame:h.wc.mainFrame}).requestHeaders['X-Lantai-Desktop-Token'],SYNTHETIC_TOKEN);
});
for(const kind of ['unregistered-webcontents','child-frame','no-document-owner'])test('token: refuses '+kind,()=>{
 const h=headerHarness();
 const details=kind==='unregistered-webcontents'?{webContentsId:987,frame:{url:'https://untrusted.invalid/',parent:null}}:kind==='child-frame'?{webContentsId:h.wc.id,frame:childEvent(h.wc).senderFrame}:{webContentsId:0};
 assert.equal(h.invoke(details).requestHeaders['X-Lantai-Desktop-Token'],undefined,'untrusted request must not receive desktop capability');
});
function appHarness(t){
 const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'lta021-offline-window-'));t.after(()=>fs.rmSync(tmp,{recursive:true,force:true}));
 class Window extends EventEmitter{
  constructor(options){super();this.options=options;this.webContents=new Contents(20,'');}
  loadURL(url){this.webContents.navigate(url);}
  isDestroyed(){return false;}isMinimized(){return false;}isFullScreen(){return false;}
  show(){}focus(){}hide(){}getNormalBounds(){return{width:1280,height:820};}
 }
 const controller=createAppWindowController({app:{getName:()=> 'synthetic',getPath:()=>tmp},BrowserWindow:Window,screen:{getAllDisplays:()=>[{workArea:{x:0,y:0,width:1440,height:900}}]},isDev:false,electronDir:path.join(W,'console/electron'),backendLifecycle:{getApiBaseUrl:()=>API},appendBackendLog:()=>{},scheduleBackendRestart:()=>{},processObject:{platform:'linux'}});
 controller.createWindow();return{controller,wc:controller.getMainWindow().webContents};
}
test('main: valid current top document and SPA route remain trusted',t=>{
 const h=appHarness(t); assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),true);
 h.wc.navigate(API+'/#/settings');assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),true);
 assert.equal(h.controller.isTrustedIpcEvent(childEvent(h.wc)),false);
});
test('main: missing senderFrame is not evidence of a current document',t=>{
 const h=appHarness(t);assert.equal(h.controller.isTrustedIpcEvent({sender:h.wc}),false);
});
test('main: old top frame loses authority after an actual reload',t=>{
 const h=appHarness(t),old=topEvent(h.wc);h.wc.navigate(h.wc.getURL());
 assert.equal(h.controller.isTrustedIpcEvent(old),false,'old process/routing identity is stale even at same URL');
 assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),true);
});
test('main: other backend HTML documents are not console documents',t=>{
 const h=appHarness(t);h.wc.navigate(API+'/api/cloud-account/watcha/callback?code=synthetic');
 assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),false);
});
function docxHarness(){
 const handlers=new Map(),listeners=new Map(),wc=new Contents(42,'file:///runtime/renderer/index.html');
 const view={webContents:wc},main=new Contents(21,API+'/#/chat');
 const runtime={configureDocsRuntime(){},setDocsShellWindow(){},registerDocsIpc(){},registerProjectIpc(){},createDocsView:()=>view};
 const isCurrentMain=e=>e?.sender===main && e.senderFrame===main.mainFrame && e.senderFrame.parent===null;
 const c=createDocxEditorController({ipcMain:{handle:(k,v)=>handlers.set(k,v),removeHandler:k=>handlers.delete(k),on:(k,v)=>listeners.set(k,v),removeListener:k=>listeners.delete(k)},fsImpl:{existsSync:()=>true,realpathSync:v=>v,statSync:()=>({isFile:()=>true,size:1024})},isTrustedIpcEvent:isCurrentMain,getMainWindow:()=>({isDestroyed:()=>false,webContents:main}),resolveRuntimePaths:()=>({modulePath:'/runtime/main/lantai.js',preloadPath:'/runtime/preload/index.js',rendererFile:'/runtime/renderer/index.html'}),loadEditorRuntime:()=>runtime,presentOfficeView:async opts=>{const v=await opts.createView();await opts.onActivate(v,{reused:false});return{success:true};}});
 c.installIpcHandlers();
 const respond=(request, result={success:true,replacement_text:'synthetic replacement'})=>listeners.get(DOCX_EDITOR_REWRITE_RESPONSE_CHANNEL)(topEvent(main),{request_id:request.request_id,result});
 return{c,wc,main,respond,payload:{instruction:'clarify',selected_text:'synthetic old',current_draft:'synthetic draft'}};
}
async function docxOpen(h){assert.equal((await h.c.openLocalDocx({path:'/synthetic/report.docx',source_chat_id:'synthetic-chat'})).success,true);}
test('rewrite: current registered top editor still rewrites with original chat ownership',async t=>{
 const h=docxHarness();t.after(()=>h.wc.emit('destroyed'));await docxOpen(h);
 const p=h.c.rewriteFromEditor(topEvent(h.wc),h.payload),[,request]=h.main.sent[0];assert.equal(request.payload.source_chat_id,'synthetic-chat');h.respond(request);
 assert.deepEqual(await p,{success:true,replacement_text:'synthetic replacement',summary:''});
});
for(const kind of ['child-frame','different-object-same-id'])test('rewrite: refuses '+kind,async t=>{
 const h=docxHarness();t.after(()=>h.wc.emit('destroyed'));await docxOpen(h);
 const event=kind==='child-frame'?childEvent(h.wc):{sender:new Contents(h.wc.id,h.wc.getURL()),senderFrame:h.wc.mainFrame};
 const p=h.c.rewriteFromEditor(event,h.payload),forwarded=h.main.sent.length;
 if(forwarded)h.respond(h.main.sent[0][1]);const result=await p;
 assert.equal(forwarded,0,'unauthorized frame must not forward draft to main');assert.equal(result.success,false);
});
for(const side of ['editor','main'])test('rewrite: pending result does not cross '+side+' navigation epoch',async t=>{
 const h=docxHarness();t.after(()=>h.wc.emit('destroyed'));await docxOpen(h);
 const p=h.c.rewriteFromEditor(topEvent(h.wc),h.payload),[,request]=h.main.sent[0];
 const target=side==='editor'?h.wc:h.main;target.navigate(target.getURL());h.respond(request);
 // A correctly retired request resolves as an explicit failure; destruction also cleans the old fixture.
 h.wc.emit('destroyed');const result=await p;assert.equal(result.success,false,'a new document must not inherit an old draft result');
});
