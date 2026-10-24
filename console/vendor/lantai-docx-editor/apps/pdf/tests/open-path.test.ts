import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'

/**
 * pdf-main open-path bookkeeping: the renderer calls consume-pending on every
 * mount — including remounts caused by View > Reload (Cmd+R). The path must
 * survive re-consumption or a reload strands the tab on "No file to open".
 */

type IpcHandler = (event: { sender: { id: number } }, ...args: unknown[]) => unknown
const handlers = new Map<string, IpcHandler>()

interface FakeWebContents {
  id: number
  once: ReturnType<typeof vi.fn>
  setWindowOpenHandler: ReturnType<typeof vi.fn>
  loadURL: ReturnType<typeof vi.fn>
  loadFile: ReturnType<typeof vi.fn>
  listeners: Map<string, () => void>
}

let nextWcId = 1
let lastWebContents: FakeWebContents

function makeFakeWebContents(): FakeWebContents {
  const listeners = new Map<string, () => void>()
  const wc: FakeWebContents = {
    id: nextWcId++,
    listeners,
    once: vi.fn((event: string, handler: () => void) => {
      listeners.set(event, handler)
    }),
    setWindowOpenHandler: vi.fn(),
    loadURL: vi.fn(),
    loadFile: vi.fn(),
  }
  lastWebContents = wc
  return wc
}

vi.mock('electron', () => ({
  app: { on: vi.fn(), whenReady: vi.fn(() => new Promise(() => {})) },
  dialog: {},
  shell: {},
  BrowserWindow: class {},
  WebContentsView: class {
    webContents = makeFakeWebContents()
  },
  ipcMain: {
    handle: vi.fn((channel: string, handler: IpcHandler) => {
      handlers.set(channel, handler)
    }),
    on: vi.fn((channel: string, handler: IpcHandler) => {
      handlers.set(channel, handler)
    }),
    removeHandler: vi.fn(),
  },
}))

import { PDF_CHANNELS } from '../src/shared/ipc'
import { configurePdfRuntime, createPdfView } from '../src/main/pdf-main'

afterEach(() => configurePdfRuntime({ preloadPath: '' }))

function makePdfFile(): string {
  const dir = mkdtempSync(join(tmpdir(), 'pdf-open-path-'))
  const path = join(dir, 'doc.pdf')
  writeFileSync(path, '%PDF-1.4\n%%EOF\n')
  return path
}

const consume = (wcId: number) =>
  handlers.get(PDF_CHANNELS.consumePending)?.({ sender: { id: wcId } })

describe('pdf open-path lifecycle', () => {
  it('marks embedded dev renderers as tabs without losing existing query parameters', () => {
    configurePdfRuntime({ preloadPath: '/preload.cjs', rendererUrl: 'http://localhost:5176/?lang=zh#pdf' })
    createPdfView()
    const url = new URL(lastWebContents.loadURL.mock.calls[0]![0] as string)
    expect(url.searchParams.get('mode')).toBe('tab')
    expect(url.searchParams.get('lang')).toBe('zh')
    expect(url.hash).toBe('#pdf')
  })

  it('marks packaged embedded renderers as tabs', () => {
    configurePdfRuntime({ preloadPath: '/preload.cjs', rendererFile: '/pdf/index.html' })
    createPdfView()
    expect(lastWebContents.loadFile).toHaveBeenCalledWith('/pdf/index.html', { query: { mode: 'tab' } })
  })

  it('registers the bootstrap theme contract for embedded windows', () => {
    createPdfView()
    expect(handlers.get(PDF_CHANNELS.getTheme)?.({ sender: { id: lastWebContents.id } })).toBe(
      'system',
    )
  })

  it('consume-pending returns the path again after a reload (second consume)', () => {
    const path = makePdfFile()
    createPdfView(path)
    const wcId = lastWebContents.id

    expect(consume(wcId)).toBe(path)
    // View > Reload remounts the renderer, which consumes again
    expect(consume(wcId)).toBe(path)
  })

  it('drops the path when the view is destroyed', () => {
    const path = makePdfFile()
    createPdfView(path)
    const wc = lastWebContents

    expect(consume(wc.id)).toBe(path)
    wc.listeners.get('destroyed')?.()
    expect(consume(wc.id)).toBeNull()
  })
})
