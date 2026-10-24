import { execFile } from 'node:child_process'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

import { CellValueType, IUniverInstanceService, LocaleType, Univer, UniverInstanceType } from '@univerjs/core'
import { FUniver } from '@univerjs/core/facade'
import { SetRangeValuesMutation, UniverSheetsPlugin } from '@univerjs/sheets'
import '@univerjs/sheets/facade'
import { describe, expect, it, vi } from 'vitest'

import { blankXlsxBuffer } from '../src/gateway/csv-import'
import { applyCellEditsToXlsx, type CellEdit } from '../src/gateway/xlsx-gateway'
import {
  createEditJournal,
  recordSetRangeValues,
  toSaveEdits,
} from '../src/renderer/edit-journal'

const execFileAsync = promisify(execFile)

function createRuntime() {
  const univer = new Univer({
    locale: LocaleType.EN_US,
    locales: { [LocaleType.EN_US]: {} },
  })
  univer.registerPlugin(UniverSheetsPlugin)
  univer.createUnit(UniverInstanceType.UNIVER_SHEET, {
    id: 'workbook-1',
    sheetOrder: ['sheet-1'],
    sheets: {
      'sheet-1': {
        id: 'sheet-1', name: 'Sheet1', rowCount: 10, columnCount: 10,
        cellData: { 0: { 0: { v: 'original', t: CellValueType.STRING } } },
      },
    },
  })
  const api = FUniver.newAPI(univer)
  univer.__getInjector().get(IUniverInstanceService).focusUnit('workbook-1')
  const worksheet = api.getActiveWorkbook()!.getActiveSheet()!
  const journal = createEditJournal()
  const rawPayloads: unknown[] = []
  const listener = api.onCommandExecuted((event) => {
    if (event.id !== SetRangeValuesMutation.id) return
    const params = event.params as {
      unitId: string
      subUnitId: string
      cellValue: Record<number, Record<number, unknown>>
    }
    rawPayloads.push(params.cellValue)
    const appliedWorksheet = api.getWorkbook(params.unitId)?.getSheetBySheetId(params.subUnitId)
    recordSetRangeValues(journal, params.subUnitId, params.cellValue, (row, column) =>
      appliedWorksheet?.getRange(row, column, 1, 1).getCellDatas()[0]?.[0],
    )
  })
  return {
    api, worksheet, journal, rawPayloads,
    dispose() { listener.dispose(); univer.dispose() },
  }
}

describe('Univer value mutations reaching the save journal', () => {
  it('retains the native numeric type of a visible editor string input without changing its mutation', () => {
    const runtime = createRuntime()
    try {
      runtime.worksheet.getRange(1, 1, 1, 1).setValue('123.5')
      expect(runtime.rawPayloads[0]).toEqual({ 1: { 1: { v: '123.5', p: null, f: null } } })
      expect(runtime.worksheet.getRange(1, 1, 1, 1).getCellDatas()[0]?.[0]).toMatchObject({
        v: 123.5, t: CellValueType.NUMBER,
      })
      expect(toSaveEdits(runtime.journal)[0]?.value).toBe(123.5)
    } finally { runtime.dispose() }
  })

  it('preserves native text formats, explicit text types, and numbers beyond safe precision', () => {
    const runtime = createRuntime()
    try {
      runtime.worksheet.getRange(1, 0, 1, 1).setValue({ v: '007', t: CellValueType.STRING })
      runtime.worksheet.getRange(1, 1, 1, 1).setValue({ v: '00042', t: CellValueType.FORCE_STRING })
      runtime.worksheet.getRange(1, 2, 1, 1).setValue({ v: '123.5', s: { n: { pattern: '@' } } })
      runtime.worksheet.getRange(1, 3, 1, 1).setValue('9007199254740993')
      expect(toSaveEdits(runtime.journal).map((edit) => edit.value)).toEqual([
        '007', '00042', '123.5', '9007199254740993',
      ])
      expect(runtime.worksheet.getRange(1, 2, 1, 1).getValue()).toBe('123.5')
    } finally { runtime.dispose() }
  })

  it('retains the native boolean type and an adjacent formula', () => {
    const runtime = createRuntime()
    try {
      runtime.worksheet.getRange(1, 0, 1, 1).setValue('TRUE')
      runtime.worksheet.getRange(1, 1, 1, 1).setValue('=B2*2')
      expect(toSaveEdits(runtime.journal)).toMatchObject([
        { row: 1, column: 0, value: true },
        { row: 1, column: 1, formula: '=B2*2' },
      ])
    } finally { runtime.dispose() }
  })

  it('uses the native undo and redo mutations rather than a second synthetic edit', async () => {
    const runtime = createRuntime()
    try {
      const cell = runtime.worksheet.getRange(0, 0, 1, 1)
      cell.setValue('123.5')
      expect(cell.getValue()).toBe(123.5)
      expect(await runtime.api.undo()).toBe(true)
      expect(cell.getValue()).toBe('original')
      expect(toSaveEdits(runtime.journal)[0]?.value).toBe('original')
      expect(await runtime.api.redo()).toBe(true)
      expect(cell.getValue()).toBe(123.5)
      expect(toSaveEdits(runtime.journal)[0]?.value).toBe(123.5)
    } finally { runtime.dispose() }
  })

  it('does not consult the live scalar for explicit text, style-only changes, or deletion', () => {
    const readAppliedCell = vi.fn(() => ({ v: 999, t: CellValueType.NUMBER }))
    const journal = createEditJournal()
    recordSetRangeValues(journal, 'sheet-1', {
      0: { 0: { v: '007', t: CellValueType.STRING }, 1: { s: { bl: 1 } }, 2: null },
    }, readAppliedCell)
    expect(readAppliedCell).not.toHaveBeenCalled()
    expect(toSaveEdits(journal)).toMatchObject([
      { value: '007', writeValue: true }, { writeValue: false }, { value: null, writeValue: true },
    ])
  })

  it('keeps the raw value if the applied cell is unavailable or remains text', () => {
    for (const applied of [undefined, { v: '007', t: CellValueType.STRING }]) {
      const journal = createEditJournal()
      recordSetRangeValues(journal, 'sheet-1', { 0: { 0: { v: '007' } } }, () => applied)
      expect(toSaveEdits(journal)[0]?.value).toBe('007')
    }
  })

  it('saves real facade inputs and reopens typed values in an independent process', async () => {
    const runtime = createRuntime()
    const temporaryDirectory = await mkdtemp(join(tmpdir(), 'lantai-native-value-reopen-'))
    try {
      runtime.worksheet.getRange(1, 1, 1, 1).setValue('123.5')
      runtime.worksheet.getRange(1, 2, 1, 1).setValue('=B2*2')
      runtime.worksheet.getRange(2, 0, 1, 1).setValue('TRUE')
      runtime.worksheet.getRange(2, 1, 1, 1).setValue({ v: '007', t: CellValueType.STRING })
      runtime.worksheet.getRange(3, 0, 1, 1).setValue('12.5%')
      runtime.worksheet.getRange(3, 1, 1, 1).setValue('2025-01-01')
      const edits: CellEdit[] = toSaveEdits(runtime.journal).map((entry) => ({
        sheetName: 'Sheet1', row: entry.row, column: entry.column,
        writeValue: entry.writeValue,
        cell: { value: entry.value, formula: entry.formula },
        style: entry.style, rich: entry.rich, styleReset: entry.styleReset,
      }))
      const mutation = await applyCellEditsToXlsx(await blankXlsxBuffer(), edits)
      const workbookPath = join(temporaryDirectory, 'roundtrip.xlsx')
      await writeFile(workbookPath, mutation.buffer)
      const repositoryRoot = fileURLToPath(new URL('../../../../../../', import.meta.url))
      const python = process.env.LANTAI_TEST_PYTHON ?? join(repositoryRoot, '.venv', 'bin', 'python')
      const { stdout } = await execFileAsync(python, ['-c', [
        'import datetime, json, openpyxl, sys',
        'sheet = openpyxl.load_workbook(sys.argv[1], data_only=False).active',
        'def value(cell):',
        '    v = cell.value',
        '    return [v.isoformat() if isinstance(v, (datetime.datetime, datetime.date)) else v, cell.data_type]',
        'print(json.dumps({key: value(sheet[key]) for key in ("B2", "C2", "A3", "B3", "A4", "B4")}))',
      ].join('\n'), workbookPath], { encoding: 'utf8' })
      expect(JSON.parse(stdout)).toEqual({
        B2: [123.5, 'n'], C2: ['=B2*2', 'f'], A3: [true, 'b'], B3: ['007', 's'],
        A4: [0.125, 'n'], B4: ['2025-01-01T00:00:00', 'd'],
      })
    } finally {
      runtime.dispose()
      await rm(temporaryDirectory, { recursive: true, force: true })
    }
  })
})
