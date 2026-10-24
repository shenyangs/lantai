const assert=require('node:assert/strict');
const test=require('node:test');
const {EventEmitter}=require('node:events');
const fs=require('node:fs');const os=require('node:os');const path=require('node:path');
const {Contents,topEvent,W}=require('./nativeOfficeSimpleHarness.cjs');
const {createAppWindowController}=require(path.join(W,'console/electron/appWindowController.cjs'));
const {createOfficeEditorController}=require(path.join(W,'console/electron/officeEditorController.cjs'));
const API='http://127.0.0.1:19090';
function appOwner(t){
 const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'lta021-synthetic-navigation-'));t.after(()=>fs.rmSync(tmp,{recursive:true,force:true}));
 class Window extends EventEmitter {
  constructor(){super();this.webContents=new Contents('');}loadURL(u){return this.webContents.loadURL(u);}
  isDestroyed(){return false;}isMinimized(){return false;}isFullScreen(){return false;}
  show(){}focus(){}hide(){}getNormalBounds(){return{width:1280,height:820};}
 }
 const controller=createAppWindowController({app:{getName:()=> 'synthetic',getPath:()=>tmp},BrowserWindow:Window,
  screen:{getAllDisplays:()=>[{workArea:{x:0,y:0,width:1440,height:900}}]},isDev:false,electronDir:path.join(W,'console/electron'),
  backendLifecycle:{getApiBaseUrl:()=>API},appendBackendLog(){},scheduleBackendRestart(){},processObject:{platform:'linux'}});
 controller.createWindow();const wc=controller.getMainWindow().webContents;t.after(()=>wc.destroy());
 assert.equal(controller.isTrustedIpcEvent(topEvent(wc)),true);return wc;
}
function officeOwner(t){
 const wc=new Contents('file:///runtime/slides/renderer/index.html');t.after(()=>wc.destroy());
 const c=createOfficeEditorController({ipcMain:{handle(){},removeHandler(){}},
  resolveRuntimePaths:()=>({slides:{rendererFile:'/runtime/slides/renderer/index.html'}}),fetchImpl:()=>{throw new Error('network forbidden');}});
 c.registerEditorWindow({webContents:wc},{kind:'slides',agent_id:'synthetic'});return wc;
}
for(const [kind,factory] of [['main',appOwner],['office',officeOwner]]){
 test(kind+': real single-event will-frame-navigate preserves the allowed current renderer URL',t=>{
  const wc=factory(t);let prevented=false;
  // Electron electron.d.ts:17545: listener receives Event<WebContentsWillFrameNavigateEventParams> only.
  wc.emit('will-frame-navigate',{url:wc.getURL(),isMainFrame:true,isSameDocument:false,frame:wc.mainFrame,preventDefault:()=>{prevented=true;}});
  assert.equal(prevented,false,'allowed current URL was mistaken for missing URL under the real Electron event signature');
 });
 test(kind+': real single-event will-frame-navigate blocks an external frame URL',t=>{
  const wc=factory(t);let prevented=false;
  wc.emit('will-frame-navigate',{url:'https://untrusted.invalid/frame',isMainFrame:false,isSameDocument:false,frame:wc.mainFrame,preventDefault:()=>{prevented=true;}});
  assert.equal(prevented,true);assert.deepEqual(wc.windowOpenHandler?.({url:'https://untrusted.invalid/'}),{action:'deny'});
 });
}
