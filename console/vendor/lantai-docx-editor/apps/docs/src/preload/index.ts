import { contextBridge, ipcRenderer, webUtils } from 'electron'
import type { IpcRendererEvent } from 'electron'
import type {
  AiChatRequest,
  AiSettings,
  AiStreamChunk,
  AiStreamRequest,
  DesktopApi,
  MenuCommand,
  UiTheme,
} from '../shared/ipc'
import type { ProjectApi } from '@genoffice/project-store'

const api: Pick<
  DesktopApi,
   | 'getLanguage'
   | 'onLanguageChanged'
   | 'getTheme'
   | 'onThemeChanged'
  | 'openDocx'
  | 'openDocxPath'
  | 'consumePendingOpenDocx'
  | 'consumeNewBlankDoc'
  | 'rewriteWithLantai'
  | 'onOpenDocx'
  | 'onRenamedDocx'
  | 'saveDocx'
  | 'writeRecoveryCopy'
  | 'onTeardown'
  | 'saveDocxAs'
  | 'saveDocxNew'
  | 'getRecentFiles'
  | 'pickImage'
  | 'getAiSettings'
  | 'setAiSettings'
  | 'aiChat'
  | 'aiStream'
  | 'aiStreamCancel'
  | 'aiGskStatus'
  | 'aiGskLogin'
  | 'webSearch'
  | 'imageSearch'
  | 'fetchImage'
  | 'pickAttachments'
  | 'addAttachmentPaths'
  | 'addPastedImage'
  | 'readAttachment'
  | 'readAttachmentImage'
  | 'onAiStream'
  | 'print'
  | 'exportPdf'
  | 'printPdfBuffer'
  | 'saveMergedPdf'
  | 'getPathForFile'
  | 'openNewTab'
  | 'listDocsTabs'
  | 'focusDocsTab'
   | 'onMenuCommand'
   | 'reportViewMenuState'
  | 'onCloseCheck'
  | 'reportCloseCheck'
  | 'onCloseSaveRequest'
  | 'reportCloseSaveResult'
> = {
  getLanguage: () => ipcRenderer.invoke('app:get-language'),
  onLanguageChanged: (handler) => {
    const listener = (
      _event: IpcRendererEvent,
      lang: 'zh' | 'en' | 'ja' | 'ko' | 'fr' | 'de' | 'es' | 'th' | 'id' | 'ru' | 'ar',
    ) => handler(lang)
    ipcRenderer.on('app:language-changed', listener)
    return () => ipcRenderer.removeListener('app:language-changed', listener)
  },
  getTheme: () => ipcRenderer.invoke('app:get-theme'),
  onThemeChanged: (handler) => {
    const listener = (_event: IpcRendererEvent, theme: UiTheme) => handler(theme)
    ipcRenderer.on('app:theme-changed', listener)
    return () => ipcRenderer.removeListener('app:theme-changed', listener)
  },
  openDocx: () => ipcRenderer.invoke('docs:open'),
  openDocxPath: (path: string) => ipcRenderer.invoke('docs:open-path', path),
  consumePendingOpenDocx: () => ipcRenderer.invoke('docs:consume-pending-open'),
  consumeNewBlankDoc: () => ipcRenderer.invoke('docs:consume-new-blank'),
  rewriteWithLantai: (request) => ipcRenderer.invoke('lantai:docx-editor-rewrite', request),
  onOpenDocx: (handler) => {
    const listener = (_event: IpcRendererEvent, result: Parameters<typeof handler>[0]) =>
      handler(result)
    ipcRenderer.on('docs:opened', listener)
    return () => ipcRenderer.removeListener('docs:opened', listener)
  },
  onRenamedDocx: (handler) => {
    const listener = (_event: IpcRendererEvent, paths: Parameters<typeof handler>[0]) =>
      handler(paths)
    ipcRenderer.on('docs:renamed', listener)
    return () => ipcRenderer.removeListener('docs:renamed', listener)
  },
  saveDocx: (path: string, data: ArrayBuffer, auto?: boolean) =>
    ipcRenderer.invoke('docs:save', path, data, auto === true),
  writeRecoveryCopy: (path: string, data: ArrayBuffer) =>
    ipcRenderer.invoke('docs:write-recovery', path, data),
  onTeardown: (handler) => {
    const listener = () => handler()
    ipcRenderer.on('docs:teardown', listener)
    return () => ipcRenderer.removeListener('docs:teardown', listener)
  },
  saveDocxAs: (defaultName: string, data: ArrayBuffer) =>
    ipcRenderer.invoke('docs:save-as', defaultName, data),
  saveDocxNew: (defaultName: string, data: ArrayBuffer) =>
    ipcRenderer.invoke('docs:save-new', defaultName, data),
  getRecentFiles: () => ipcRenderer.invoke('docs:recent'),
  pickImage: () => ipcRenderer.invoke('docs:pick-image'),
  getAiSettings: () => ipcRenderer.invoke('ai:get-settings'),
  setAiSettings: (settings) => ipcRenderer.invoke('ai:set-settings', settings),
  aiChat: (request) => ipcRenderer.invoke('ai:chat', request),
  aiStream: (request) => ipcRenderer.invoke('ai:stream', request),
  aiStreamCancel: (requestId) => ipcRenderer.invoke('ai:stream-cancel', requestId),
  aiGskStatus: (withEmail) => ipcRenderer.invoke('ai:gsk-status', withEmail),
  aiGskLogin: () => ipcRenderer.invoke('ai:gsk-login'),
  webSearch: (query, maxResults) => ipcRenderer.invoke('ai:web-search', query, maxResults),
  imageSearch: (query, maxResults) => ipcRenderer.invoke('ai:image-search', query, maxResults),
  fetchImage: (url) => ipcRenderer.invoke('ai:fetch-image', url),
  pickAttachments: () => ipcRenderer.invoke('files:pick'),
  addAttachmentPaths: (paths) => ipcRenderer.invoke('files:add', paths),
  addPastedImage: (data, ext) => ipcRenderer.invoke('files:add-pasted-image', data, ext),
  readAttachment: (filePath, offset, maxChars) =>
    ipcRenderer.invoke('files:read', filePath, offset, maxChars),
  readAttachmentImage: (filePath) => ipcRenderer.invoke('files:read-image', filePath),
  onAiStream: (handler) => {
    const listener = (_event: IpcRendererEvent, chunk: Parameters<typeof handler>[0]) =>
      handler(chunk)
    ipcRenderer.on('ai:stream-chunk', listener)
    return () => ipcRenderer.removeListener('ai:stream-chunk', listener)
  },
  print: () => ipcRenderer.invoke('docs:print'),
  exportPdf: (
    defaultName: string,
    pageWidthTwips: number,
    pageHeightTwips: number,
    outPath?: string,
  ) => ipcRenderer.invoke('docs:export-pdf', defaultName, pageWidthTwips, pageHeightTwips, outPath),
  printPdfBuffer: (pageWidthTwips: number, pageHeightTwips: number) =>
    ipcRenderer.invoke('docs:print-pdf-buffer', pageWidthTwips, pageHeightTwips),
  saveMergedPdf: (defaultName: string, base64Parts: string[], outPath?: string) =>
    ipcRenderer.invoke('docs:save-merged-pdf', defaultName, base64Parts, outPath),
  getPathForFile: (file: File) => webUtils.getPathForFile(file),
  openNewTab: (openPath?: string | null) => ipcRenderer.invoke('win:new', openPath ?? null),
  listDocsTabs: () => ipcRenderer.invoke('win:list'),
  focusDocsTab: (id: string) => ipcRenderer.invoke('win:focus', id),
  onMenuCommand: (handler: (command: MenuCommand, payload?: string) => void) => {
    const listener = (_event: IpcRendererEvent, command: MenuCommand, payload?: string) =>
      handler(command, payload)
    ipcRenderer.on('menu:command', listener)
    return () => ipcRenderer.removeListener('menu:command', listener)
  },
  onCloseCheck: (handler: () => void) => {
    const listener = () => handler()
    ipcRenderer.on('docs:close-check', listener)
    return () => ipcRenderer.removeListener('docs:close-check', listener)
  },
  reportViewMenuState: (state: { aiSidebar: boolean; darkCanvas: boolean }) =>
    ipcRenderer.send('docs:view-menu-state', {
      aiSidebar: state?.aiSidebar === true,
      darkCanvas: state?.darkCanvas === true,
    }),
  reportCloseCheck: (state: { dirty: boolean; autoSave: boolean; filePath?: string | null }) =>
    ipcRenderer.send('docs:close-check-result', {
      dirty: state?.dirty === true,
      autoSave: state?.autoSave === true,
      filePath: typeof state?.filePath === 'string' ? state.filePath : null,
    }),
  onCloseSaveRequest: (handler: () => void) => {
    const listener = () => handler()
    ipcRenderer.on('docs:close-save-request', listener)
    return () => ipcRenderer.removeListener('docs:close-save-request', listener)
  },
  reportCloseSaveResult: (ok: boolean) => ipcRenderer.send('docs:close-save-result', ok === true),
}

contextBridge.exposeInMainWorld('desktop', api)
