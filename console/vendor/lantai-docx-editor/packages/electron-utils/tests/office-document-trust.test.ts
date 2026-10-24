import { EventEmitter } from 'node:events'
import type { IpcMain, IpcMainInvokeEvent, WebContents } from 'electron'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  assertOfficeIpcDocument, captureOfficeIpcRequest, captureOfficeRendererDocument, guardOfficeOperation, registerOfficeRenderer, trustedOfficeIpcMain,
  type OfficeRendererKind,
} from '../src/office-document-trust'

class Contents extends EventEmitter {
  mainFrame = { url: '', parent: null, processId: 10, routingId: 1 }
  destroyed = false
  windowOpen: (() => { action: string }) | undefined
  constructor(readonly kind: OfficeRendererKind) {
    super()
    this.mainFrame.url = `file:///runtime/${kind}/index.html?mode=tab`
  }
  getURL() { return this.mainFrame.url }
  isDestroyed() { return this.destroyed }
  setWindowOpenHandler(callback: () => { action: string }) { this.windowOpen = callback }
  destroy() { this.destroyed = true; this.emit('destroyed') }
  navigate() {
    this.emit('did-start-navigation', {}, this.getURL(), false, true)
    this.mainFrame = { ...this.mainFrame, routingId: this.mainFrame.routingId + 1 }
  }
}
const owned: Contents[] = []
afterEach(() => { for (const contents of owned.splice(0)) contents.destroy() })
function renderer(kind: OfficeRendererKind = 'docs') {
  const contents = new Contents(kind)
  owned.push(contents)
  registerOfficeRenderer(contents as unknown as WebContents, {
    kind, rendererFile: `/runtime/${kind}/index.html`,
  })
  return contents
}
function event(contents: Contents) {
  return { sender: contents, senderFrame: contents.mainFrame } as unknown as IpcMainInvokeEvent
}
function ipc(kind: OfficeRendererKind = 'docs') {
  const handlers = new Map<string, (event: IpcMainInvokeEvent) => unknown>()
  const raw = Object.assign(new EventEmitter(), {
    handle(channel: string, listener: (event: IpcMainInvokeEvent) => unknown) { handlers.set(channel, listener) },
  })
  return { raw, handlers, trusted: trustedOfficeIpcMain(raw as unknown as IpcMain, kind) }
}
function deferred() {
  let release!: () => void
  return { promise: new Promise<void>(resolve => { release = resolve }), release: () => release() }
}

describe('Office IPC document ownership', () => {
  it('permits the registered current top frame and rejects an unregistered object', () => {
    const contents = renderer(), { trusted, handlers } = ipc()
    trusted.handle('docs:save', () => 'saved')
    expect(handlers.get('docs:save')!(event(contents))).toBe('saved')
    const unknown = new Contents('docs')
    expect(() => handlers.get('docs:save')!(event(unknown))).toThrow('Unauthorized')
  })

  it('rejects a child frame even when its URL matches the authorized renderer', () => {
    const contents = renderer(), { trusted, handlers } = ipc()
    trusted.handle('docs:save', () => 'saved')
    const child = { ...contents.mainFrame, parent: contents.mainFrame }
    expect(() => handlers.get('docs:save')!({ sender: contents, senderFrame: child } as unknown as IpcMainInvokeEvent)).toThrow('Unauthorized')
  })

  it('retires the document at navigation start, before Electron changes the frame', () => {
    const contents = renderer(), { trusted, handlers } = ipc()
    trusted.handle('docs:save', () => 'saved')
    contents.emit('did-start-navigation', {}, contents.getURL(), false, true)
    expect(() => handlers.get('docs:save')!(event(contents))).toThrow('Unauthorized')
  })

  it('admits the new top frame after a same-URL reload and rejects the old event', () => {
    const contents = renderer(), { trusted, handlers } = ipc(), old = event(contents)
    trusted.handle('docs:save', () => 'saved')
    contents.navigate()
    expect(() => handlers.get('docs:save')!(old)).toThrow('Unauthorized')
    expect(handlers.get('docs:save')!(event(contents))).toBe('saved')
  })

  it('keeps in-page navigation usable', () => {
    const contents = renderer(), { trusted, handlers } = ipc()
    trusted.handle('docs:save', () => 'saved')
    contents.emit('did-start-navigation', {}, contents.getURL(), true, true)
    expect(handlers.get('docs:save')!(event(contents))).toBe('saved')
  })

  it('checks the original document before invoking a writer after an await', async () => {
    const contents = renderer(), { trusted, handlers } = ipc(), gate = deferred()
    let writes = 0
    const writer = guardOfficeOperation(() => ++writes)
    trusted.handle('docs:save', async () => { await gate.promise; return writer() })
    const pending = handlers.get('docs:save')!(event(contents)) as Promise<unknown>
    const rejection = expect(pending).rejects.toThrow('no longer authorized')
    contents.navigate(); gate.release()
    await rejection
    expect(writes).toBe(0)
  })

  it('isolates concurrent request contexts and leaves another editor usable', async () => {
    const docs = renderer(), pdf = renderer('pdf'), gate = deferred(), { trusted, handlers } = ipc()
    const writes: string[] = []
    trusted.handle('app:save', async (e) => {
      await gate.promise; assertOfficeIpcDocument()
      writes.push(e.sender === docs as unknown as WebContents ? 'docs' : 'pdf')
    })
    const first = handlers.get('app:save')!(event(docs)) as Promise<unknown>
    const rejection = expect(first).rejects.toThrow('no longer authorized')
    const second = handlers.get('app:save')!(event(pdf)) as Promise<unknown>
    docs.navigate(); gate.release()
    await rejection; await second
    expect(writes).toEqual(['pdf'])
  })

  it('uses one registry and original async context across independently loaded helper copies', async () => {
    const contents = renderer(), { trusted, handlers } = ipc(), gate = deferred()
    vi.resetModules()
    const other = await import('../src/office-document-trust')
    trusted.handle('docs:save', async () => { await gate.promise; other.assertOfficeIpcDocument() })
    const pending = handlers.get('docs:save')!(event(contents)) as Promise<unknown>
    const rejection = expect(pending).rejects.toThrow('no longer authorized')
    contents.navigate(); gate.release()
    await rejection
  })

  it('preserves shared file attachment handlers for every registered editor', () => {
    const pdf = renderer('pdf'), { trusted, handlers } = ipc('docs')
    trusted.handle('files:read-image', () => 'attachment')
    expect(handlers.get('files:read-image')!(event(pdf))).toBe('attachment')
  })

  it('restricts slide mutation channels to slide documents', () => {
    const pdf = renderer('pdf'), { trusted, handlers } = ipc('slides')
    let mutations = 0
    trusted.handle('ai:insert-image-url', () => ++mutations)
    expect(() => handlers.get('ai:insert-image-url')!(event(pdf))).toThrow('Unauthorized')
    expect(mutations).toBe(0)
  })

  it('blocks unknown navigation and popups using Electron single-event signatures', () => {
    const contents = renderer()
    let blocked = 0
    contents.emit('will-frame-navigate', { url: 'https://example.invalid/', preventDefault() { blocked++ } })
    contents.emit('will-navigate', { url: contents.getURL(), preventDefault() { blocked++ } })
    expect(blocked).toBe(1)
    expect(contents.windowOpen!()).toEqual({ action: 'deny' })
  })

  it('drops fire-and-forget events from child frames without throwing', () => {
    const contents = renderer(), { trusted, raw } = ipc()
    let effects = 0
    trusted.on('docs:dirty', () => { effects++ })
    expect(() => raw.emit('docs:dirty', { sender: contents, senderFrame: { ...contents.mainFrame, parent: contents.mainFrame } })).not.toThrow()
    raw.emit('docs:dirty', event(contents))
    expect(effects).toBe(1)
  })

  it('permits main-owned menu work with no IPC context', () => {
    expect(guardOfficeOperation(() => 'native-menu')()).toBe('native-menu')
  })

  it('binds native menu work to the original registered document and settles it at navigation start', () => {
    const contents = renderer(), original = captureOfficeRendererDocument(contents as unknown as WebContents)!
    const cancelled = vi.fn(), dispose = original.onInvalidated(cancelled)
    expect(original.isCurrent()).toBe(true)
    contents.emit('did-start-navigation', {}, contents.getURL(), false, true)
    expect(original.isCurrent()).toBe(false)
    expect(cancelled).toHaveBeenCalledTimes(1)
    expect(captureOfficeRendererDocument(contents as unknown as WebContents)).toBeNull()
    dispose()
    contents.navigate()
    expect(captureOfficeRendererDocument(contents as unknown as WebContents)?.isCurrent()).toBe(true)
    expect(cancelled).toHaveBeenCalledTimes(1)
  })

  it('does not admit a native menu request from an unregistered renderer', () => {
    const unknown = new Contents('pdf')
    expect(captureOfficeRendererDocument(unknown as unknown as WebContents)).toBeNull()
  })

  it('accepts a native menu acknowledgement only from its original current document', () => {
    const docs = renderer(), pdf = renderer('pdf'), { trusted, handlers } = ipc()
    const original = captureOfficeRendererDocument(docs as unknown as WebContents)!
    trusted.handle('app:ack-save', () => original.isSameCaller())
    expect(handlers.get('app:ack-save')!(event(docs))).toBe(true)
    expect(handlers.get('app:ack-save')!(event(pdf))).toBe(false)
    docs.navigate()
    expect(handlers.get('app:ack-save')!(event(docs))).toBe(false)
  })

  it('binds callbacks to the original document outside the IPC async context', () => {
    const contents = renderer(), { trusted, handlers } = ipc()
    trusted.handle('ai:stream', () => captureOfficeIpcRequest())
    const request = handlers.get('ai:stream')!(event(contents)) as ReturnType<typeof captureOfficeIpcRequest>
    const abort = vi.fn(), dispose = request.onInvalidated(abort)
    expect(request.isCurrent()).toBe(true)
    expect(request.isSameCaller()).toBe(false)
    contents.navigate()
    expect(request.isCurrent()).toBe(false)
    expect(abort).toHaveBeenCalledTimes(1)
    dispose(); contents.navigate()
    expect(abort).toHaveBeenCalledTimes(1)
  })

  it('allows cancellation by the original document and rejects another editor or reloaded document', () => {
    const docs = renderer(), pdf = renderer('pdf'), { trusted, handlers } = ipc()
    trusted.handle('ai:stream', () => captureOfficeIpcRequest())
    const request = handlers.get('ai:stream')!(event(docs)) as ReturnType<typeof captureOfficeIpcRequest>
    trusted.handle('ai:stream-cancel', () => request.isSameCaller())
    expect(handlers.get('ai:stream-cancel')!(event(docs))).toBe(true)
    expect(handlers.get('ai:stream-cancel')!(event(pdf))).toBe(false)
    docs.navigate()
    expect(handlers.get('ai:stream-cancel')!(event(docs))).toBe(false)
  })

  it('removes a reused listener from the requested channel only', () => {
    const contents = renderer(), { trusted, raw } = ipc()
    const listener = vi.fn()
    trusted.on('docs:dirty', listener); trusted.on('docs:menu', listener)
    trusted.removeListener('docs:dirty', listener)
    raw.emit('docs:dirty', event(contents)); raw.emit('docs:menu', event(contents))
    expect(listener).toHaveBeenCalledTimes(1)
  })
})
