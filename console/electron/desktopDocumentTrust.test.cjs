const assert = require('node:assert/strict');
const test = require('node:test');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const W = path.resolve(__dirname, '..', '..');
const {createDesktopSecurityHeaders} = require(path.join(W,'console/electron/desktopSecurityHeaders.cjs'));
const {createAppWindowController} = require(path.join(W,'console/electron/appWindowController.cjs'));
const {installDesktopIpcHandlers} = require('./desktopIpcController.cjs');
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
  this.emit('did-frame-navigate',{},url,200,'OK',true,1,this.mainFrame.routingId);
 }
 reloadSameFrame(){
  const {processId,routingId}=this.mainFrame;
  this.emit('did-start-navigation',{},this.url,false,true,processId,routingId);
  this.emit('did-frame-navigate',{},this.url,200,'OK',true,processId,routingId);
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
 controller.createWindow();return{controller,wc:controller.getMainWindow().webContents,tmp};
}

function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};}
for(const boundary of ['current','navigation-start','same-url-reload','same-frame-reload']) {
 for(const operation of ['save-file','copy-image','workspace-grant','open-with','html-work','restore-workspace']) {
  test(`main async effect: ${operation} at ${boundary}`,async t=>{
   const h=appHarness(t), handlers=new Map(), entered=deferred(), release=deferred();
   let effects=0, cleaned=0; const source=path.join(h.tmp,'source.txt'), target=path.join(h.tmp,'chosen.txt');
   fs.writeFileSync(source,'original synthetic source');
   const pause=()=>{entered.resolve();return release.promise;};
   const mockedFs={promises:{
    realpath:async p=>p,stat:async()=>({isFile:()=>true,size:25}),
    copyFile:async()=>{effects++;},readFile:async()=>JSON.stringify({id:'default'}),mkdir:async()=>{},
    cp:async()=>{await pause();},rm:async()=>{cleaned++;},
   }};
   class WorkWindow {
    constructor(){this.webContents={setWindowOpenHandler(){},on(){}};this.dead=false;}
    loadFile(){return pause();}isDestroyed(){return this.dead;}
    show(){effects++;}destroy(){this.dead=true;cleaned++;}
   }
   installDesktopIpcHandlers({app:{getPath:()=>h.tmp},ipcMain:{handle:(ch,fn)=>handlers.set(ch,fn)},
    getMainWindow:h.controller.getMainWindow,isTrustedIpcEvent:h.controller.isTrustedIpcEvent,
    dialog:{showSaveDialog:pause,showOpenDialog:operation==='restore-workspace'?async()=>({canceled:false,filePaths:[h.tmp]}):pause},shell:{},fsImpl:mockedFs,platform:'darwin',
    distributionChannel:operation==='workspace-grant'?'mas':'official',getDesktopToken:()=>SYNTHETIC_TOKEN,
    workspaceBookmarkStore:{authorizeSelection(){effects++;return{ok:true,securityScopeRef:'synthetic-ref'};}},
    clipboard:{writeImage(){effects++;}},nativeImage:{createFromBuffer:()=>({isEmpty:()=>false})},
    fetchImpl:async()=>({ok:true,status:200,headers:{get:()=> 'image/png'},arrayBuffer:async()=>{await pause();return new ArrayBuffer(4);}}),
    execFileImpl:(_cmd,_args,cb)=>{effects++;cb(null);},BrowserWindow:WorkWindow,
   });
   const channel={ 'save-file':'lantai:save-file-as','copy-image':'lantai:copy-image-from-url',
    'workspace-grant':'lantai:select-workspace-grant','open-with':'lantai:open-with',
    'html-work':'lantai:open-html-work','restore-workspace':'lantai:restore-workspace'}[operation];
   const input={'save-file':{sourcePath:source},'copy-image':{url:'https://synthetic.invalid/image.png'},
    'workspace-grant':{agentId:'default'},'open-with':source,'html-work':path.join(h.tmp,'work.html'),
    'restore-workspace':{agentId:'default',expectedPath:h.tmp}}[operation];
   const pending=handlers.get(channel)(topEvent(h.wc),input);await entered.promise;
   if(boundary==='navigation-start')h.wc.emit('did-start-navigation',{},h.wc.getURL(),false,true);
   if(boundary==='same-url-reload')h.wc.navigate(h.wc.getURL());
   if(boundary==='same-frame-reload')h.wc.reloadSameFrame();
   release.resolve({canceled:false,filePath:target,filePaths:[operation==='open-with'?'/Applications/Synthetic.app':h.tmp],bookmarks:['synthetic-bookmark']});
   const result=await pending;
   assert.equal(Boolean(result?.success||result?.ok),boundary==='current');
   if(operation!=='restore-workspace')assert.equal(effects,boundary==='current'?1:0,'retired original document must not perform the later effect');
   if(operation==='html-work'||operation==='restore-workspace')assert.equal(cleaned,boundary==='current'?0:1,'private created work is cleaned when the request expires');
   assert.equal(fs.readFileSync(source,'utf8'),'original synthetic source');
   if(boundary==='same-frame-reload'){
    const fresh=await handlers.get(channel)(topEvent(h.wc),input);
    assert.equal(Boolean(fresh?.success||fresh?.ok),true,'the committed new document can perform its own operation');
   }
  });
 }
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
test('main: navigation start immediately retires the old document before frame replacement',t=>{
 const h=appHarness(t),old=topEvent(h.wc);
 h.wc.emit('did-start-navigation',{},h.wc.getURL(),false,true,1,999);
 assert.equal(h.controller.isTrustedIpcEvent(old),false);
 assert.equal(h.controller.isTrustedBackendRequest({webContents:h.wc,webContentsId:h.wc.id,frame:old.senderFrame}),false);
 h.wc.navigate(h.wc.getURL(),false);
 assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),true);
});
test('main: same-document SPA navigation preserves the current document capability',t=>{
 const h=appHarness(t);
 h.wc.emit('did-start-navigation',{},API+'/#/settings',true,true,1,h.wc.mainFrame.routingId);
 assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),true);
});
test('main: a committed reload restores IPC and auth headers when Chromium reuses its frame',t=>{
 const h=appHarness(t), frame=h.wc.mainFrame;let hook;
 createDesktopSecurityHeaders({session:{defaultSession:{webRequest:{onBeforeSendHeaders:(_f,fn)=>hook=fn}}},
  backendLifecycle:{isCurrentBackendApiUrl:url=>url.startsWith(API+'/api/'),getDesktopToken:()=>SYNTHETIC_TOKEN},
  isTrustedBackendRequest:h.controller.isTrustedBackendRequest}).install();
 h.wc.reloadSameFrame();
 assert.equal(h.wc.mainFrame,frame,'reload can keep the same WebFrameMain object');
 assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),true);
 let response;hook({url:API+'/api/auth/status',webContents:h.wc,webContentsId:h.wc.id,frame,requestHeaders:{}},r=>response=r);
 assert.equal(response.requestHeaders['X-Lantai-Desktop-Token'],SYNTHETIC_TOKEN);
});
test('main: only the matching committed main frame can recover navigation authority',t=>{
 const h=appHarness(t),{processId,routingId}=h.wc.mainFrame,url=h.wc.getURL();
 h.wc.emit('did-start-navigation',{},url,false,true,processId,routingId);
 for(const args of [[url,200,'OK',false,processId,routingId],[url,200,'OK',true,processId+1,routingId],
  [url,200,'OK',true,processId,routingId+1],[API+'/other',200,'OK',true,processId,routingId]]){
  h.wc.emit('did-frame-navigate',{},...args);
  assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),false);
 }
 h.wc.emit('did-fail-provisional-load',{},-3,'ABORTED',url,true,processId,routingId);
 assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),false,'a failed navigation does not restore the retired document');
 h.wc.emit('did-frame-navigate',{},url,200,'OK',true,processId,routingId);
 assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),true);
});
test('main: Electron details events retire full navigation but preserve in-page navigation',t=>{
 const h=appHarness(t),url=h.wc.getURL();
 h.wc.emit('did-start-navigation',{url,isMainFrame:true,isSameDocument:true});
 assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),true);
 h.wc.emit('did-start-navigation',{url,isMainFrame:true,isSameDocument:false});
 assert.equal(h.controller.isTrustedIpcEvent(topEvent(h.wc)),false);
});
test('token: untrusted callers cannot supply their own desktop capability header',()=>{
 const h=headerHarness();
 const result=h.invoke({webContentsId:987,requestHeaders:{'x-lantai-desktop-token':'untrusted',Accept:'application/json'}});
 assert.equal(Object.keys(result.requestHeaders).some(k=>k.toLowerCase()==='x-lantai-desktop-token'),false);
 assert.equal(result.requestHeaders.Accept,'application/json');
});
test('token: no trust verifier fails closed even for a backend URL',()=>{
 let hook,result;
 createDesktopSecurityHeaders({session:{defaultSession:{webRequest:{onBeforeSendHeaders:(_f,fn)=>hook=fn}}},backendLifecycle:{isCurrentBackendApiUrl:()=>true,getDesktopToken:()=>SYNTHETIC_TOKEN}}).install();
 hook({url:API+'/api/config',requestHeaders:{}},v=>result=v);
 assert.equal(result.requestHeaders['X-Lantai-Desktop-Token'],undefined);
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
