import type { CellContentBaseline } from './edit-journal'
import { toNeutralStyle } from './edit-journal'

type BaselineRange = {
  getValue(): unknown
  getFormula(): unknown
  getCellStyleData(): unknown
}

type BaselineWorksheet = {
  getRange(row: number, column: number): BaselineRange
}

/**
 * Preserve the immutable, first-seen cell content for this open session.
 *
 * Univer exposes several edit entry points with different event ordering. In
 * particular, the visible Electron keyboard bridge writes through FRange and
 * can reach BeforeCommandExecute after the live value has already changed.
 * Call this helper at the earliest available seam; repeated calls are safe and
 * never replace the original baseline.
 */
export function captureCellContentBaseline(
  baselineSheets: Map<string, Map<string, CellContentBaseline>>,
  sheetId: string,
  worksheet: BaselineWorksheet,
  row: number,
  column: number,
): void {
  let baselines = baselineSheets.get(sheetId)
  if (!baselines) {
    baselines = new Map()
    baselineSheets.set(sheetId, baselines)
  }
  const key = `${row}:${column}`
  if (baselines.has(key)) return

  const range = worksheet.getRange(row, column)
  const formula = range.getFormula()
  const styleData = range.getCellStyleData()
  const baselineStyle = styleData
    ? toNeutralStyle(styleData as Record<string, unknown>)
    : undefined
  baselines.set(key, {
    value:
      (range.getValue() as string | number | boolean | null | undefined) ??
      null,
    ...(typeof formula === 'string' && formula.length > 0
      ? { formula: formula.startsWith('=') ? formula : `=${formula}` }
      : {}),
    ...(baselineStyle === undefined ? {} : { style: baselineStyle }),
  })
}
