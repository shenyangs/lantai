import { describe, expect, it, vi } from 'vitest'

vi.mock('../src/renderer/export-render', () => ({
  renderSlidesToPngBase64: vi.fn(),
}))
vi.mock('../src/renderer/components/toast-bus', () => ({ showToast: vi.fn() }))
vi.mock('../src/renderer/i18n/locale', () => ({ t: (key: string) => key }))

import { adoptSavedSlides, save, saveAs } from '../src/renderer/file-actions'
import type { ActionCtx } from '../src/renderer/action-context'

function makeContext(): ActionCtx {
  return {
    flushActiveEditRef: { current: () => Promise.resolve() },
    editingActiveRef: { current: false },
    flushNotes: () => Promise.resolve(),
    slides: [],
    current: 0,
    setSlides: () => {},
    setSelectedIds: () => {},
    setEnteredGroupId: () => {},
    setEditing: () => {},
    setEditingCell: () => {},
    setDirty: () => {},
    setPath: () => {},
    setStatus: () => {},
    path: '/test/deck.pptx',
  } as unknown as ActionCtx
}

describe('slides save serialization', () => {
  it('clears stale save state when the current slide no longer exists', () => {
    const setSlides = vi.fn()
    const setSelectedIds = vi.fn()
    const setEnteredGroupId = vi.fn()
    const setEditing = vi.fn()
    const setEditingCell = vi.fn()
    const context = {
      ...makeContext(),
      slides: [],
      current: 0,
      setSlides,
      setSelectedIds,
      setEnteredGroupId,
      setEditing,
      setEditingCell,
    } as unknown as ActionCtx

    adoptSavedSlides(context, [])

    expect(setSlides).toHaveBeenCalledWith([])
    expect(setSelectedIds).toHaveBeenCalledWith([])
    expect(setEnteredGroupId).toHaveBeenCalledWith(null)
    expect(setEditing).toHaveBeenCalledWith(null)
    expect(setEditingCell).toHaveBeenCalledWith(null)
  })

  it('drops editing state whose node cannot be remapped after save', () => {
    const previous = {
      widthPx: 960,
      heightPx: 540,
      scale: 1,
      background: { kind: 'solid', color: 'FFFFFF' },
      nodes: [{ sourceId: 'old-node' }],
    } as ActionCtx['slides'][number]
    const saved = { ...previous, nodes: [] } as ActionCtx['slides'][number]
    const setEditing = vi.fn()
    const setEditingCell = vi.fn()
    const context = {
      ...makeContext(),
      slides: [previous],
      current: 0,
      setEditing,
      setEditingCell,
    } as unknown as ActionCtx

    adoptSavedSlides(context, [saved])

    const updateEditing = setEditing.mock.calls[0]?.[0]
    const updateEditingCell = setEditingCell.mock.calls[0]?.[0]
    expect(updateEditing({ sourceId: 'old-node' })).toBeNull()
    expect(updateEditingCell({ sourceId: 'old-node', row: 0, col: 0 })).toBeNull()
  })

  it('queues concurrent saves so only one disk write runs at a time', async () => {
    let inFlight = 0
    let maxConcurrent = 0
    const mockSave = vi.fn(async () => {
      inFlight += 1
      maxConcurrent = Math.max(maxConcurrent, inFlight)
      await new Promise((resolve) => setTimeout(resolve, 20))
      inFlight -= 1
      return { ok: true }
    })
    vi.stubGlobal('window', { slidesApi: { save: mockSave } })

    const results = await Promise.all([
      save(makeContext()),
      save(makeContext(), true),
      save(makeContext()),
    ])

    expect(results).toEqual([true, true, true])
    expect(mockSave).toHaveBeenCalledTimes(3)
    expect(maxConcurrent).toBe(1)
    vi.unstubAllGlobals()
  })

  it('does not let a failed save block the next caller', async () => {
    let calls = 0
    const mockSave = vi.fn(async () => {
      calls += 1
      return calls === 1 ? { ok: false, error: 'disk full' } : { ok: true }
    })
    vi.stubGlobal('window', { slidesApi: { save: mockSave } })

    expect(await Promise.all([save(makeContext(), true), save(makeContext(), true)])).toEqual([
      false,
      true,
    ])
    vi.unstubAllGlobals()
  })

  it('queues save-as behind an in-flight save', async () => {
    let releaseSave: (() => void) | undefined
    const firstSave = new Promise<void>((resolve) => {
      releaseSave = resolve
    })
    const order: string[] = []
    const mockSave = vi.fn(async () => {
      order.push('save:start')
      await firstSave
      order.push('save:end')
      return { ok: true }
    })
    const mockSaveAs = vi.fn(async () => {
      order.push('save-as')
      return { ok: true, path: '/test/copied.pptx' }
    })
    vi.stubGlobal('window', {
      slidesApi: { save: mockSave, saveAs: mockSaveAs },
    })

    const first = save(makeContext(), true)
    await vi.waitFor(() => expect(order).toEqual(['save:start']))
    const second = saveAs(makeContext())
    await Promise.resolve()
    expect(order).toEqual(['save:start'])

    releaseSave?.()
    await Promise.all([first, second])
    expect(order).toEqual(['save:start', 'save:end', 'save-as'])
    vi.unstubAllGlobals()
  })
})
