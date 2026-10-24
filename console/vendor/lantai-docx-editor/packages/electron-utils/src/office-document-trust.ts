import { AsyncLocalStorage } from 'node:async_hooks'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { IpcMain, IpcMainEvent, IpcMainInvokeEvent, WebContents } from 'electron'

export type OfficeRendererKind = 'docs' | 'sheets' | 'pdf' | 'slides'
export interface OfficeRendererLocation {
  kind: OfficeRendererKind
  rendererFile?: string | undefined
  rendererUrl?: string | undefined
}

interface DocumentIdentity {
  url: string
  processId: number
  routingId: number
}
interface RendererOwner {
  contents: WebContents
  location: OfficeRendererLocation
  epoch: number
  retired: DocumentIdentity | null
  invalidations: Set<() => void>
}
interface OfficeDocument extends DocumentIdentity {
  owner: RendererOwner
  epoch: number
}
interface TrustState {
  owners: Map<WebContents, RendererOwner>
  document: AsyncLocalStorage<OfficeDocument>
}

// Each editor has its own bundle. The ownership registry and async context must
// nevertheless be shared by all four bundles in the Electron main process.
const STATE = Symbol.for('lantai.office-document-trust.v1')
const holder = globalThis as unknown as Record<symbol, TrustState | undefined>
const state = (holder[STATE] ??= {
  owners: new Map<WebContents, RendererOwner>(),
  document: new AsyncLocalStorage<OfficeDocument>(),
})

const sharedChannels = new Set([
  'files:pick', 'files:add', 'files:read', 'files:read-image', 'files:add-pasted-image',
  'ai:get-settings', 'ai:set-settings', 'ai:gsk-status', 'ai:gsk-login',
  'ai:stream', 'ai:stream-cancel', 'ai:chat', 'ai:web-search', 'ai:image-search',
  'ai:fetch-image', 'ai:generate-image', 'ai:analyze-media',
])

function sameIdentity(a: DocumentIdentity, b: DocumentIdentity | null): boolean {
  return !!b && a.url === b.url && a.processId === b.processId && a.routingId === b.routingId
}

function allowedUrl(value: string, location: OfficeRendererLocation): boolean {
  try {
    const url = new URL(value)
    if (url.username || url.password) return false
    if (url.protocol === 'file:') {
      if (!location.rendererFile || (url.hostname && url.hostname !== 'localhost')) return false
      return resolve(fileURLToPath(url)) === resolve(location.rendererFile)
    }
    if (!location.rendererUrl) return false
    const expected = new URL(location.rendererUrl)
    return (url.protocol === 'http:' || url.protocol === 'https:') &&
      url.origin === expected.origin && url.pathname === expected.pathname
  } catch {
    return false
  }
}

function identity(contents: WebContents, frame = contents.mainFrame): DocumentIdentity | null {
  try {
    if (contents.isDestroyed() || !frame || frame.parent !== null) return null
    const main = contents.mainFrame
    const url = contents.getURL()
    if (!url || !main || main.parent !== null || frame.url !== url || main.url !== url ||
        !Number.isInteger(frame.processId) || !Number.isInteger(frame.routingId) ||
        frame.processId !== main.processId || frame.routingId !== main.routingId) return null
    return { url, processId: frame.processId, routingId: frame.routingId }
  } catch {
    return null
  }
}

function capture(event: IpcMainEvent | IpcMainInvokeEvent, kind: OfficeRendererKind,
                 channel: string): OfficeDocument | null {
  try {
    const owner = state.owners.get(event.sender)
    if (!owner) return null
    // Shared application/project handlers are replaced when another editor is
    // activated. They still serve every registered editor's current document.
    const shared = /^(app|project):/.test(channel) || sharedChannels.has(channel)
    if (!shared && owner.location.kind !== kind) return null
    const current = identity(event.sender, event.senderFrame ?? undefined)
    if (!event.senderFrame || !current || !allowedUrl(current.url, owner.location) ||
        sameIdentity(current, owner.retired)) return null
    return { ...current, owner, epoch: owner.epoch }
  } catch {
    return null
  }
}

function assertCurrent(document: OfficeDocument): void {
  const owner = document.owner
  const current = identity(owner.contents)
  if (state.owners.get(owner.contents) !== owner || owner.epoch !== document.epoch ||
      !current || !sameIdentity(current, document) || sameIdentity(current, owner.retired) ||
      !allowedUrl(current.url, owner.location)) {
    throw new Error('Office renderer document is no longer authorized')
  }
}

/** Register the actual contents before loading its renderer, including standalone windows. */
export function registerOfficeRenderer(contents: WebContents,
                                       location: OfficeRendererLocation): void {
  const previous = state.owners.get(contents)
  const invalidate = (value: RendererOwner) => {
    value.epoch++
    for (const callback of [...value.invalidations]) {
      try { callback() } catch { /* Cancellation must not interrupt navigation. */ }
    }
  }
  if (previous) invalidate(previous)
  const owner: RendererOwner = { contents, location: { ...location }, epoch: 0, retired: null,
    invalidations: new Set() }
  state.owners.set(contents, owner)
  const retire = () => {
    owner.retired = identity(contents)
    invalidate(owner)
  }
  const preventNavigation = (event: { preventDefault(): void; url?: string },
                             details?: string | { url?: string }) => {
    const url = typeof details === 'string' ? details : details?.url ?? event.url
    if (!url || !allowedUrl(url, owner.location)) event.preventDefault()
  }
  contents.setWindowOpenHandler(() => ({ action: 'deny' }))
  contents.on('will-navigate', preventNavigation)
  contents.on('will-redirect', preventNavigation)
  contents.on('will-frame-navigate', preventNavigation)
  contents.on('did-start-navigation', (_event, _url, isInPlace, isMainFrame) => {
    if (isMainFrame && !isInPlace) retire()
  })
  contents.on('render-process-gone', retire)
  contents.once('destroyed', () => {
    invalidate(owner)
    if (state.owners.get(contents) === owner) state.owners.delete(contents)
  })
}

/** Recheck the original IPC document at each file write, rename, or other effect. */
export function assertOfficeIpcDocument(): void {
  const document = state.document.getStore()
  if (document) assertCurrent(document)
}

export interface OfficeIpcRequest {
  isCurrent(): boolean
  isSameCaller(): boolean
  onInvalidated(callback: () => void): () => void
}

/** Bind callbacks and cancellation to the original request, even outside its async context. */
export function captureOfficeIpcRequest(): OfficeIpcRequest {
  const original = state.document.getStore()
  if (!original) throw new Error('Office IPC request context is required')
  assertCurrent(original)
  return bindDocument(original)
}

/** Bind a native menu request to the registered document before awaiting user input. */
export function captureOfficeRendererDocument(contents: WebContents): OfficeIpcRequest | null {
  const owner = state.owners.get(contents)
  const current = identity(contents)
  if (!owner || !current || !allowedUrl(current.url, owner.location) ||
      sameIdentity(current, owner.retired)) return null
  const original = { ...current, owner, epoch: owner.epoch }
  try { assertCurrent(original) } catch { return null }
  return bindDocument(original)
}

function bindDocument(original: OfficeDocument): OfficeIpcRequest {
  const isCurrent = () => {
    try { assertCurrent(original); return true } catch { return false }
  }
  return {
    isCurrent,
    isSameCaller() {
      const caller = state.document.getStore()
      return !!caller && caller.owner === original.owner && caller.epoch === original.epoch &&
        sameIdentity(caller, original) && isCurrent()
    },
    onInvalidated(callback) {
      original.owner.invalidations.add(callback)
      if (!isCurrent()) callback()
      return () => { original.owner.invalidations.delete(callback) }
    },
  }
}

/** Check before invoking a collaborator and again after its asynchronous result. */
export function guardOfficeOperation<T extends (...args: any[]) => any>(operation: T): T {
  return function (this: unknown, ...args: Parameters<T>) {
    assertOfficeIpcDocument()
    const result = operation.apply(this, args)
    if (result && typeof result.then === 'function') {
      return Promise.resolve(result).then((value) => {
        assertOfficeIpcDocument()
        return value
      })
    }
    assertOfficeIpcDocument()
    return result
  } as T
}

/** Preserve Electron collaborator overloads while checking each method call. */
export function guardOfficeOperations<T extends object>(operations: T): T {
  return new Proxy(operations, {
    get(target, property) {
      const value = Reflect.get(target, property, target)
      return typeof value === 'function' ? guardOfficeOperation(value.bind(target)) : value
    },
  })
}

/** An editor-local facade: the global Electron IPC object is never patched. */
export function trustedOfficeIpcMain(raw: IpcMain, kind: OfficeRendererKind): IpcMain {
  const listeners = new Map<string, Map<(...args: any[]) => any, (...args: any[]) => any>>()
  return new Proxy(raw, {
    get(target, property) {
      if (property === 'handle' || property === 'handleOnce') {
        return (channel: string, listener: (event: IpcMainInvokeEvent, ...args: any[]) => any) => {
          target[property](channel, (event, ...args) => {
            const document = capture(event, kind, channel)
            if (!document) throw new Error('Unauthorized Office renderer document')
            return state.document.run(document, guardOfficeOperation(() => listener(event, ...args)))
          })
        }
      }
      if (property === 'on' || property === 'once' || property === 'addListener') {
        return (channel: string, listener: (event: IpcMainEvent, ...args: any[]) => any) => {
          const wrapped = (event: IpcMainEvent, ...args: any[]) => {
            const document = capture(event, kind, channel)
            if (!document) return
            // Fire-and-forget IPC has no rejection receiver. A detached document
            // is discarded without an uncaught exception in the main process.
            try {
              const result = state.document.run(document,
                guardOfficeOperation(() => listener(event, ...args)))
              if (result && typeof result.catch === 'function') result.catch(() => {})
            } catch {
              // No payload or document content is logged.
            }
          }
          let channelListeners = listeners.get(channel)
          if (!channelListeners) listeners.set(channel, channelListeners = new Map())
          channelListeners.set(listener, wrapped)
          target[property](channel, wrapped)
        }
      }
      if (property === 'removeListener' || property === 'off') {
        return (channel: string, listener: (...args: any[]) => any) => {
          const channelListeners = listeners.get(channel)
          target[property](channel, channelListeners?.get(listener) ?? listener)
          channelListeners?.delete(listener)
          if (channelListeners?.size === 0) listeners.delete(channel)
        }
      }
      const value = Reflect.get(target, property, target)
      return typeof value === 'function' ? value.bind(target) : value
    },
  })
}
