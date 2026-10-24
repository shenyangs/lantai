import { describe, expect, it, vi } from 'vitest'
import JSZip from 'jszip'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { addChart, editChartElement, openPptx, savePptx, type ChartElement } from '@genoffice/pptx-engine'
import { resolveTarget } from '../../../packages/pptx-engine/src/zip'
import {
  carryHistoryForReplacement, pushHistory, registerAiSnapshot, restoreAiSnapshot,
  restoreSnapshot, takeSnapshot, type Session,
} from '../src/main/session-state'

vi.mock('electron', () => ({ BrowserWindow: { getFocusedWindow: () => null } }))
vi.mock('../src/main/fonts', () => ({ createSystemFontMetrics: () => ({}) }))
vi.mock('../src/main/tiff-decode', () => ({ tiffToPng: () => null }))

const fixture = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../../packages/pptx-engine/tests/fixtures/01_standard_business.pptx'))

async function chartSession(cacheOnly: boolean): Promise<{ session: Session; chartId: string }> {
  let opened = await openPptx(fixture)
  addChart(opened, 0, { kind: 'bar', categories: ['Open', 'Files'], series: [{ name: 'Share', values: [31, 9] }],
    offset: { x: 914400, y: 914400, cx: 4572000, cy: 2743200 } })
  const zip = await JSZip.loadAsync(await savePptx(opened))
  if (cacheOnly) {
    const path = Object.keys(zip.files).find((entry) => /^ppt\/charts\/chart\d+\.xml$/.test(entry))!
    zip.file(path, (await zip.file(path)!.async('text')).replace(/<c:externalData\b[\s\S]*?<\/c:externalData>/, ''))
  }
  opened = await openPptx(await zip.generateAsync({ type: 'uint8array' }))
  const chart = opened.deck.slides[0]!.elements.find((el) => el.type === 'chart') as ChartElement
  return { session: { path: '', fitWidthPx: 1280, undoStack: [], redoStack: [], opened }, chartId: chart.id }
}

function edit(session: Session, chartId: string, value: number): void {
  pushHistory(session)
  expect(editChartElement(session.opened, 0, chartId, { series: [{ name: 'Share', values: [value, 9] }] })).toBe(true)
}

async function expectSavedValue(session: Session, value: number): Promise<void> {
  const opened = await openPptx(await savePptx(session.opened))
  const chart = opened.deck.slides[0]!.elements.find((el) => el.type === 'chart') as ChartElement
  expect(chart.chart.series[0]!.values).toEqual([value, 9])
  const path = [...opened.archive.entries.keys()].find((entry) => /^ppt\/charts\/chart\d+\.xml$/.test(entry))!
  const rid = /<c:externalData\b[^>]*r:id="([^"]+)"/.exec(opened.archive.readText(path)!)?.[1]
  if (!rid) return // cache-only source restored by undo has no Edit Data workbook
  const rel = opened.archive.readRels(path).get(rid)!
  const book = await JSZip.loadAsync(opened.archive.readBytes(resolveTarget(path, rel.target))!)
  const sheet = await book.file('xl/worksheets/sheet1.xml')!.async('text')
  expect(Number(/<c r="B2"[^>]*><v>([^<]+)<\/v>/.exec(sheet)![1])).toBe(value)
}

describe.each([false, true])('chart workbook undo/redo, cache-only=%s', (cacheOnly) => {
  it('undo then save restores the chart cache and workbook together', async () => {
    const { session, chartId } = await chartSession(cacheOnly)
    edit(session, chartId, 32)
    session.redoStack.push(takeSnapshot(session))
    restoreSnapshot(session, session.undoStack.pop()!)
    await expectSavedValue(session, 31)
  })

  it('undo, save, redo, then save and edit retains the accepted workbook matrix', async () => {
    const { session, chartId } = await chartSession(cacheOnly)
    edit(session, chartId, 32)
    session.redoStack.push(takeSnapshot(session))
    restoreSnapshot(session, session.undoStack.pop()!)
    await expectSavedValue(session, 31)
    session.undoStack.push(takeSnapshot(session))
    restoreSnapshot(session, session.redoStack.pop()!)
    await expectSavedValue(session, 32)
    edit(session, chartId, 33)
    await expectSavedValue(session, 33)
  })

  it('saved edits remain undoable and redoable after the pending queue was flushed', async () => {
    const { session, chartId } = await chartSession(cacheOnly)
    edit(session, chartId, 32)
    await expectSavedValue(session, 32)
    session.redoStack.push(takeSnapshot(session))
    restoreSnapshot(session, session.undoStack.pop()!)
    await expectSavedValue(session, 31)
    restoreSnapshot(session, session.redoStack.pop()!)
    await expectSavedValue(session, 32)
  })

  it('AI rollback restores pending chart data and is itself undoable', async () => {
    const { session, chartId } = await chartSession(cacheOnly)
    edit(session, chartId, 32)
    const id = registerAiSnapshot(session, takeSnapshot(session))
    edit(session, chartId, 33)
    expect(restoreAiSnapshot(session, id)).toBe(true)
    await expectSavedValue(session, 32)
    restoreSnapshot(session, session.undoStack.pop()!)
    await expectSavedValue(session, 33)
  })

  it('full-deck replacement can restore pending chart data into the new archive', async () => {
    const { session: previous, chartId } = await chartSession(cacheOnly)
    edit(previous, chartId, 32)
    const { session: replacement, chartId: replacementChartId } = await chartSession(false)
    edit(replacement, replacementChartId, 99)
    carryHistoryForReplacement(previous, replacement)
    const replacementState = takeSnapshot(replacement)
    restoreSnapshot(replacement, replacement.undoStack.pop()!)
    await expectSavedValue(replacement, 32)
    restoreSnapshot(replacement, replacementState)
    await expectSavedValue(replacement, 99)
  })
})
