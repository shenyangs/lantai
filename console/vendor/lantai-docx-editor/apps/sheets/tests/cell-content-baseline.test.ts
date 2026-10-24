import { describe, expect, it, vi } from 'vitest'

import { captureCellContentBaseline } from '../src/renderer/cell-content-baseline'

describe('captureCellContentBaseline', () => {
  it('preserves the value, normalized formula, and neutral style seen before a bridge edit', () => {
    const getRange = vi.fn(() => ({
      getValue: () => 7,
      getFormula: () => 'SUM(B1:B2)',
      getCellStyleData: () => ({ bl: 1, ff: 'Calibri', fs: 11 }),
    }))
    const sheets = new Map()

    captureCellContentBaseline(sheets, 'sheet-1', { getRange }, 3, 4)

    expect(getRange).toHaveBeenCalledWith(3, 4)
    expect(sheets.get('sheet-1')?.get('3:4')).toEqual({
      value: 7,
      formula: '=SUM(B1:B2)',
      style: { bold: true, fontFamily: 'Calibri', fontSize: 11 },
    })
  })

  it('never replaces the first session baseline after the live value changes', () => {
    let value = 'original'
    const getRange = () => ({
      getValue: () => value,
      getFormula: () => undefined,
      getCellStyleData: () => undefined,
    })
    const sheets = new Map()

    captureCellContentBaseline(sheets, 'sheet-1', { getRange }, 0, 0)
    value = 'edited'
    captureCellContentBaseline(sheets, 'sheet-1', { getRange }, 0, 0)

    expect(sheets.get('sheet-1')?.get('0:0')).toEqual({ value: 'original' })
  })
})
