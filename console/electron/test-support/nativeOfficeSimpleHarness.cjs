const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const {pathToFileURL} = require('node:url');
const W = process.env.LANTAI_SOURCE_ROOT || path.resolve(__dirname, '../../..');
const ts = require(path.join(W, 'console/node_modules/typescript/lib/typescript.js'));
const vendor = path.join(W, 'console/vendor/lantai-docx-editor');
let nextId = 70, route = 100;
const unavailable = label => (..._args) => {throw new Error('offline harness forbids: ' + label);};
function unusedModule(label, members={}) {
  return new Proxy(members,{get:(target,key)=>key==='__esModule'?false:key in target?target[key]:unavailable(label+'.'+String(key))});
}
class Contents extends EventEmitter {
  constructor(url='') {super();this.id=++nextId;this.sent=[];this.dead=false;this.session={getPartition:()=>''};this.navigate(url,false);}
  navigate(url,emit=true) {
    if(emit)this.emit('did-start-navigation',{},url,false,true,1,++route);
    this.url=url;this.mainFrame={url,parent:null,processId:1,routingId:++route};this.mainFrame.top=this.mainFrame;
    if(emit){
      this.emit('did-frame-navigate',{},url,200,'OK',true,this.mainFrame.processId,this.mainFrame.routingId);
      this.emit('did-navigate',{},url,200,'OK');
    }
  }
  getURL(){return this.url;}isDestroyed(){return this.dead;}
  send(...args){this.sent.push(args);}setWindowOpenHandler(fn){this.windowOpenHandler=fn;}
  loadURL(url){this.navigate(url);return Promise.resolve();}
  loadFile(file,options={}){const u=pathToFileURL(file);for(const[k,v]of Object.entries(options.query||{}))u.searchParams.set(k,v);return this.loadURL(u.href);}
  destroy(){if(!this.dead){this.dead=true;this.emit('destroyed');}}
  openDevTools(){}getOSProcessId(){return 123;}getProcessId(){return 123;}
}
function topEvent(wc){return{sender:wc,senderFrame:wc.mainFrame};}
function childEvent(wc){return{sender:wc,senderFrame:{url:wc.url,parent:wc.mainFrame,top:wc.mainFrame,processId:1,routingId:++route}};}
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};}
function createHarness(kind,{realAtomic=false}={}) {
  const suffix={docs:'.docx',pdf:'.pdf',slides:'.pptx',sheets:'.xlsx'}[kind];
  const originalPath='/synthetic/material'+suffix;
  const initial=Buffer.from(kind==='pdf'?'%PDF-1.7\nsynthetic offline bytes':'PK\x03\x04synthetic offline bytes');
  const files=new Map([[originalPath,initial]]),writes=[],promotions=[],handles=new Map(),listeners=new Map(),views=[],windows=[];
  const read=p=>{if(!files.has(p))throw Object.assign(new Error('synthetic ENOENT: '+p),{code:'ENOENT'});return Buffer.from(files.get(p));};
  const stat=p=>{const b=read(p);return{isFile:()=>true,isDirectory:()=>false,size:b.length,mtimeMs:1000};};
  const write=(p,b)=>{writes.push({path:p,bytes:Buffer.from(b)});files.set(p,Buffer.from(b));};
  const memoryFs={existsSync:p=>files.has(p),readFileSync:(p,encoding)=>encoding?read(p).toString(encoding):read(p),statSync:stat,
    mkdirSync(){},readdirSync:()=>[],unlinkSync:p=>files.delete(p),writeFileSync:write,renameSync:(a,b)=>{files.set(b,read(a));files.delete(a);}};
  const memoryFsp={readFile:async(p,encoding)=>encoding?read(p).toString(encoding):read(p),stat:async p=>stat(p),mkdir:async()=>{},readdir:async()=>[],
    copyFile:async(a,b)=>files.set(b,read(a)),unlink:async p=>files.delete(p),rm:async p=>files.delete(p),writeFile:async(p,b)=>{write(p,b);await h.afterWrite?.(p,b);},
    rename:async(a,b)=>{if(h.renameTransientFailures>0){h.renameTransientFailures--;h.renameFaultEntered?.resolve();throw Object.assign(new Error('synthetic transient target lock'),{code:'EACCES'});}promotions.push({from:a,to:b});files.set(b,read(a));files.delete(a);},open:async p=>({read:async b=>{read(p).copy(b);return{bytesRead:Math.min(read(p).length,b.length)};},close:async()=>{}})};
  const ipcMain={handle:(key,fn)=>{if(handles.has(key))throw new Error('duplicate IPC '+key);handles.set(key,fn);},removeHandler:key=>handles.delete(key),
    on:(key,fn)=>{const a=listeners.get(key)||[];a.push(fn);listeners.set(key,a);},removeListener:(key,fn)=>listeners.set(key,(listeners.get(key)||[]).filter(v=>v!==fn))};
  const mainWc=new Contents('http://127.0.0.1:19090/#/chat');
  const main={webContents:mainWc,isDestroyed:()=>false};
  class View {constructor(options){this.options=options;this.webContents=new Contents();views.push(this);}setBackgroundColor(){}setBounds(){}}
  class Window extends EventEmitter {
    constructor(options){super();this.options=options;this.webContents=new Contents();windows.push(this);}
    static getFocusedWindow(){return null;}static fromWebContents(){return main;}static getAllWindows(){return [];}
    isDestroyed(){return false;}loadURL(u){return this.webContents.loadURL(u);}loadFile(p,o){return this.webContents.loadFile(p,o);}
    getNormalBounds(){return{width:1280,height:820};}setBounds(){}setMenu(){}show(){}setTitle(){}destroy(){this.webContents.destroy();}
  }
  const app=new EventEmitter();Object.assign(app,{getPath:key=>'/synthetic/'+key,getName:()=> 'offline',whenReady:()=>Promise.resolve(),getVersion:()=> '0.0',getAppPath:()=>'/synthetic/app'});
  const h={kind,originalPath,initial,files,writes,promotions,handles,listeners,views,mainWc,ipcMain,memoryFs,memoryFsp,
    saveDialog:async()=>({canceled:false,filePath:'/synthetic/copy'+suffix}),openDialog:async()=>({canceled:false,filePaths:[originalPath]})};
  memoryFs.createReadStream=p=>{
    const stream=new EventEmitter();
    queueMicrotask(async()=>{
      try{await h.digestBarrier?.(p);stream.emit('data',read(p));stream.emit('end');}
      catch(error){stream.emit('error',error);}
    });
    return stream;
  };
  const dialog={showSaveDialog:(...args)=>h.saveDialog(...args),showOpenDialog:(...args)=>h.openDialog(...args),showMessageBox:async()=>({response:0}),showErrorBox:unavailable('real dialog')};
  const electron={app,ipcMain,dialog,BrowserWindow:Window,WebContentsView:View,Menu:{buildFromTemplate:()=>({popup(){}}),setApplicationMenu(){},getApplicationMenu:()=>null},
    screen:{getAllDisplays:()=>[{workArea:{x:0,y:0,width:1440,height:900}}],getPrimaryDisplay:()=>({workArea:{x:0,y:0,width:1440,height:900}}),getDisplayMatching:()=>({workArea:{x:0,y:0,width:1440,height:900}})},shell:{openExternal:unavailable('external browser')},
    session:{defaultSession:{setDisplayMediaRequestHandler(){}}},desktopCapturer:{getSources:unavailable('screen capture')},clipboard:unusedModule('clipboard'),nativeImage:unusedModule('nativeImage')};
  // All electron-utils exports are filled from the actual package source below.
  // In particular, document admission and AsyncLocalStorage may never be stubbed.
  const utils=unusedModule('electron-utils');
  const cache=new Map();
  const deck=()=>({deck:{size:{cx:9144000,cy:5143500},slides:[]},archive:{entries:new Map(),readPresentation:()=>({slidePaths:[]})}});
  const imports={electron,'node:fs':memoryFs,'node:fs/promises':memoryFsp,'node:path':path,'node:crypto':require('node:crypto'),
    'node:async_hooks':require('node:async_hooks'),'node:url':require('node:url'),
    'node:dns/promises':{lookup:unavailable('network DNS')},
    'node:net':{BlockList:require('node:net').BlockList,isIP:require('node:net').isIP},
    'node:child_process':{execFile:unavailable('subprocess')},'node:os':{userInfo:()=>({username:'synthetic'})},
    '@genoffice/electron-utils':utils,'@genoffice/i18n':{createI18n:()=> (_lang,key)=>key,getUiLang:()=> 'en',normalizeLang:v=>v,setUiLang(){}},
    'node:http':{createServer:unavailable('network listener')},zod:require(path.join(W,'console/node_modules/zod')),
    '@genoffice/project-store':unusedModule('project-store',{registerProjectSessionPathResolver(){}}), '@genoffice/file-parse':unusedModule('file-parse'),
    '@genoffice/ai-provider':unusedModule('Provider'), '@lantai/office-ai-bridge':unusedModule('bridge',{hasLantaiDesktopBridge:()=>false}),
    '@genoffice/pptx-engine':unusedModule('pptx-engine',{openPptx:async()=>deck(),savePptxToFile:async(_opened,p)=>write(p,Buffer.from('PK\x03\x04saved')),commitSaved(){}}),
    '@genoffice/pptx-render':unusedModule('pptx-render',{EMU_PER_PX_96:9525,buildRenderSlide:()=>({nodes:[]})})};
  class MemorySidecar {
    start(){}stop(){}async close(){}
    async open(p){return{
      sessionId:require('node:crypto').randomUUID(),name:path.basename(p),entryCount:1,
      sheets:[{id:'sheet1',name:'Sheet1',rowCount:10,columnCount:10,columnWidths:[],defaultRowHeight:null,defaultColumnWidth:null,
        freeze:null,hidden:false,tabColor:null,showGridLines:true,tables:[],comments:[],pivotRanges:[]}],
      styles:[],dxfStyles:[],visuals:[],definedNames:[]
    };}
  }
  const localStubs={
    'apps/docs/src/main/atomic-write.ts':{atomicWriteFile:async(p,b)=>write(p,b),looksLikeZip:b=>Buffer.from(b).subarray(0,2).toString()==='PK'},
    'apps/docs/src/main/window-bounds.ts':{fitDocsWindowToWorkArea:()=>({x:0,y:0,width:1280,height:820})},
    'apps/pdf/src/main/save-pdf.ts':{savePdfToPath:async(_source,target)=>{write(target,Buffer.from('%PDF-1.7\nsaved'));return{skippedTextEdits:[],skippedImageEdits:[]};}},
    'apps/pdf/src/main/redaction-path.ts':unusedModule('redaction'), 'apps/pdf/src/main/vision-ocr.ts':unusedModule('OCR'),
    'apps/slides/src/main/fonts.ts':{createSystemFontMetrics:()=>null},
    'apps/slides/src/main/tiff-decode.ts':unusedModule('TIFF'), 'apps/slides/src/main/pdf-export.ts':unusedModule('PDF export'),
    'apps/slides/src/main/shaped-metrics.ts':{shapedMetricsReady:async()=>{},refineComplexWidths:async()=>false},
    'apps/slides/src/main/edit-text.ts':unusedModule('edit-text'), 'apps/slides/src/main/cfb-sniff.ts':{isCfbHeader:()=>false},
    'apps/slides/src/main/mp4-audio-sniff.ts':unusedModule('mp4'), 'apps/slides/src/main/i18n-main.ts':{tm:key=>key},
    'apps/slides/src/main/ai-ipc.ts':{registerAiIpc(){},registerSlidesOnlyAiIpc(){}},'apps/slides/src/main/presenter-show.ts':{registerPresenterIpc(){}},
    'apps/slides/src/main/attachments-ipc.ts':{registerAttachmentIpc(){}},
    'apps/sheets/src/main/xlsx-sidecar-client.ts':{XlsxSidecarClient:MemorySidecar},
    'apps/sheets/src/main/pdf-export.ts':unusedModule('sheets PDF export'),
    'apps/sheets/src/gateway/csv-import.ts':unusedModule('CSV conversion'),
    'apps/sheets/src/gateway/xlsx-pivot.ts':unusedModule('pivot parser'),
    'apps/sheets/src/gateway/xlsx-package-io.ts':unusedModule('xlsx archive',{saveWorkbookViaSidecar:async request=>{
      write(request.targetPath,Buffer.from('PK\x03\x04saved workbook'));return{touchedEntries:[]};
    }})};
  function load(file) {
    if(cache.has(file))return cache.get(file).exports;
    const rel=path.relative(vendor,file);if(rel in localStubs&&!(realAtomic&&rel==='apps/docs/src/main/atomic-write.ts'))return localStubs[rel];
    if(!rel || rel.startsWith('..'))throw new Error('unexpected source outside vendor: '+file);
    const module={exports:{}};cache.set(file,module);
    const source=fs.readFileSync(file,'utf8');
    const js=ts.transpileModule(source,{fileName:file,compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
    const req=id=>{if(id in imports)return imports[id];if(id.startsWith('.'))return load(path.resolve(path.dirname(file),id+(path.extname(id)?'':'.ts')));throw new Error('unapproved import '+id);};
    const context=vm.createContext({module,exports:module.exports,require:req,__dirname:path.dirname(file),__filename:file,
      process:{env:{},argv:[],platform:'linux',on(){},exit:unavailable('process exit')},Buffer,URL,Uint8Array,ArrayBuffer,structuredClone,setTimeout,clearTimeout,setImmediate,
      // Background freeze/autosave loops are not ticked during finite IPC tests.
      setInterval:()=>({unref(){}}),clearInterval(){},console});
    new vm.Script(js,{filename:file}).runInContext(context,{timeout:15000});return module.exports;
  }
  Object.assign(utils,load(path.join(vendor,'packages/electron-utils/src/index.ts')));
  const runtime=load(path.join(vendor,`apps/${kind}/src/main/${kind}-main.ts`));
  // OS menu presentation is inert. Registered IPC handlers, public view creation,
  // file grants, close decisions and per-renderer sessions execute actual source.
  runtime.setDocsMenuGate?.(()=>false);
  const hostedRuntime={...runtime,installPdfMenu(){},installSlidesMenu(){},installSheetsMenu(){}};
  const paths={docs:{modulePath:'/runtime/docs/main/lantai.js',preloadPath:'/runtime/docs/preload/index.js',rendererFile:'/runtime/docs/renderer/index.html'},
    pdf:{modulePath:'/runtime/pdf/main/lantai.js',preloadPath:'/runtime/pdf/preload/index.js',rendererFile:'/runtime/pdf/renderer/index.html',visionOcrPath:'/runtime/pdf/ocr.js'},
    slides:{modulePath:'/runtime/slides/main/lantai.js',preloadPath:'/runtime/slides/preload/index.js',rendererFile:'/runtime/slides/renderer/index.html'},
    sheets:{modulePath:'/runtime/sheets/main/lantai.js',preloadPath:'/runtime/sheets/preload/index.js',rendererFile:'/runtime/sheets/renderer/index.html',sidecarPath:'/runtime/sheets/sidecar'}};
  const {createOfficeEditorController}=require(path.join(W,'console/electron/officeEditorController.cjs'));
  const {createDocxEditorController}=require(path.join(W,'console/electron/docxEditorController.cjs'));
  const rootFs={existsSync:()=>true,realpathSync:p=>p,statSync:p=>({isFile:()=>true,size:files.get(p)?.length||1024})};
  const presentOfficeView=async opts=>{const view=await opts.createView();await opts.onActivate(view,{reused:false});return{success:true};};
  const trust=e=>e?.sender===mainWc && e?.senderFrame===mainWc.mainFrame;
  const office=createOfficeEditorController({ipcMain,fsImpl:rootFs,getMainWindow:()=>main,isTrustedIpcEvent:trust,resolveRuntimePaths:()=>paths,
    loadEditorRuntime:()=>hostedRuntime,presentOfficeView,fetchImpl:unavailable('network')});
  const docs=createDocxEditorController({ipcMain,fsImpl:rootFs,getMainWindow:()=>main,isTrustedIpcEvent:trust,
    resolveRuntimePaths:()=>paths.docs,loadEditorRuntime:()=>hostedRuntime,presentOfficeView,
    installOfficeAiHandlers:()=>office.installOfficeAiHandlers(),registerOfficeAiEditorWindow:(v,c)=>office.registerEditorWindow(v,c),resolveOfficeModelBinding:()=>null});
  h.open=async()=>{
    const result=kind==='docs'?await docs.openLocalDocx({path:originalPath}):await office.openLocalOffice({path:originalPath});
    if(!result.success)throw new Error('host view failed: '+JSON.stringify(result));
    h.wc=views[0].webContents;
    const channel={docs:'docs:consume-pending-open',pdf:'pdf:consume-pending',slides:'slides:consume-pending-open',sheets:'workbook:select'}[kind];
    const opened=await h.invoke(channel,topEvent(h.wc),1280);
    if(kind==='pdf'?opened!==originalPath:opened?.path!==originalPath)throw new Error('actual public open did not grant original path: '+JSON.stringify(opened));
    h.opened=opened;writes.length=0;
  };
  h.invoke=(channel,event,...args)=>{if(!handles.has(channel))throw new Error('missing actual IPC '+channel);return handles.get(channel)(event,...args);};
  h.openStandalone=async()=>{
    const cap=kind[0].toUpperCase()+kind.slice(1);
    runtime['configure'+cap+'Runtime']?.(kind==='slides'?{...paths.slides,rendererFilePath:paths.slides.rendererFile}:paths[kind]);
    runtime['register'+cap+'Ipc']?.();
    const win=await runtime['create'+cap+'Window'](kind==='sheets'?{}:originalPath);
    h.wc=win.webContents;
    const channel={docs:'docs:consume-pending-open',pdf:'pdf:consume-pending',slides:'slides:open-path',sheets:'workbook:select'}[kind];
    h.opened=kind==='slides'?await h.invoke(channel,topEvent(h.wc),originalPath,1280):await h.invoke(channel,topEvent(h.wc));
    if(kind==='pdf'?h.opened!==originalPath:h.opened?.path!==originalPath)throw new Error('actual standalone open did not grant original path: '+JSON.stringify(h.opened));
    writes.length=0;
  };
  h.close=()=>{for(const owner of [...views,...windows])owner.webContents.destroy();mainWc.destroy();};
  h.runtime=runtime;h.hostOffice=office;h.hostDocs=docs;
  return h;
}
module.exports={createHarness,Contents,topEvent,childEvent,deferred,W,vendor};
