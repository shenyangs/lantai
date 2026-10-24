import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const here = dirname(fileURLToPath(import.meta.url))
const editorRoot = join(here, '..', '..')
const readEditorSource = (relativePath: string): string =>
  readFileSync(join(editorRoot, relativePath), 'utf8')

describe('Lantai multi-editor host contract', () => {
  it('keeps spreadsheet session routing after another editor replaces shared project IPC', () => {
    const sheets = readEditorSource('sheets/src/main/sheets-main.ts')
    const docs = readEditorSource('docs/src/main/docs-main.ts')
    const slides = readEditorSource('slides/src/main/slides-main.ts')

    expect(sheets).toContain('registerProjectSessionPathResolver(resolveSheetsSessionPath)')
    expect(docs).toContain('resolveProjectSessionPath(event.sender.id, args.sessionId)')
    expect(slides).toContain('resolveProjectSessionPath(event.sender.id, args.sessionId)')
  })

  it('wires native spreadsheet printing from menu through preload to renderer', () => {
    const main = readEditorSource('sheets/src/main/sheets-main.ts')
    const preload = readEditorSource('sheets/src/preload/index.ts')
    const renderer = readEditorSource('sheets/src/renderer/App.tsx')
    const channels = readEditorSource('sheets/src/shared/ipc-channels.ts')

    expect(main).toContain("label: getUiLang() === 'zh' ? '打印…' : 'Print…'")
    expect(main).toContain('IPC_CHANNELS.printWorkbook')
    expect(preload).toContain('async printWorkbook(request)')
    expect(renderer).toMatch(/action\s*===\s*["']print["']/)
    expect(channels).toContain("printWorkbook: 'workbook:print'")
  })

  it('keeps the packaged visible cell editor on Univer native value and geometry semantics', () => {
    const renderer = readEditorSource('sheets/src/renderer/App.tsx')
    const styles = readEditorSource('sheets/src/renderer/styles.css')

    expect(renderer).toContain('cell = range.getCellRect()')
    expect(renderer).toMatch(
      /captureCellContentBaseline\([\s\S]*?editor\.column,[\s\S]*?\);[\s\S]*?\.setValue\(value\)/,
    )
    expect(renderer).toContain('.setValue(value)')
    expect(renderer).toMatch(
      /const appliedWorksheet =[\s\S]*?\.getWorkbook\(params\.unitId\)[\s\S]*?\.getSheetBySheetId\(params\.subUnitId\)/,
    )
    expect(renderer).toMatch(
      /recordSetRangeValues\([\s\S]*?params\.cellValue,[\s\S]*?\(row, column\) =>[\s\S]*?appliedWorksheet[\s\S]*?\.getRange\(row, column, 1, 1\)[\s\S]*?\.getCellDatas\(\)/,
    )
    expect(renderer).not.toContain('.setValues([[{ v: value, f: null, si: null }]])')
    expect(renderer).not.toContain('column * 80')
    expect(renderer).not.toContain('row * 20')
    expect(styles).not.toContain('min-width: 72px')
    expect(styles).not.toContain('min-height: 26px')
  })

  it('clears editor focus before inspecting the external toolbar workbook undo stack', () => {
    const renderer = readEditorSource('sheets/src/renderer/App.tsx')
    const historyFocus = readEditorSource(
      'sheets/src/renderer/workbook-history-focus.ts',
    )

    expect(renderer).toMatch(
      /focusWorkbookHistoryStack\([\s\S]*?activeWorkbook\.getId\(\),[\s\S]*?\);[\s\S]*?pitchTopUndoElement\(\)/,
    )
    expect(historyFocus).toMatch(
      /setContextValue\(EDITOR_ACTIVATED, false\);[\s\S]*?setContextValue\(FOCUSING_FX_BAR_EDITOR, false\);[\s\S]*?setContextValue\(FOCUSING_SHEET, true\);[\s\S]*?focusUnit\(workbookId\);/,
    )
    expect(renderer).toMatch(
      /menuActionRef\.current\s*=\s*\(action\)[\s\S]*?action\s*===\s*["']undo["'][\s\S]*?handleUndo\(\);[\s\S]*?handleRedo\(\);/,
    )
  })
})
