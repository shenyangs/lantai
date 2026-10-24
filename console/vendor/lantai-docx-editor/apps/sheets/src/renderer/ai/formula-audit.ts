import { formatAddress, type RangeBounds } from '../../domain/cell-address'
import type { CellScalar } from '../../domain/workbook.types'

export type FormulaAuditIssueKind =
  | 'broken_reference'
  | 'error_value'
  | 'missing_formula'
  | 'constant_override'
  | 'pattern_outlier'

export interface FormulaAuditIssue {
  readonly address: string
  readonly kind: FormulaAuditIssueKind
  readonly severity: 'error' | 'warning'
  readonly message: string
  readonly formula?: string
  readonly value?: CellScalar
  readonly expectedPattern?: string
}

export interface FormulaAuditReport {
  readonly formulaCount: number
  readonly issueCount: number
  readonly errorCount: number
  readonly warningCount: number
  readonly issues: readonly FormulaAuditIssue[]
}

type CellRead = { value: CellScalar; formula?: string }

/**
 * Convert A1 references into position-relative tokens. Repeated formulas that
 * were filled down/right then share a fingerprint, while absolute references
 * stay absolute. Quoted strings are deliberately left untouched.
 */
export function formulaFingerprint(formula: string, row: number, column: number): string {
  const segments = formula.split(/("(?:[^"]|"")*")/)
  return segments
    .map((segment, index) => {
      if (index % 2 === 1) return segment
      return segment.replace(
        /(^|[^A-Z0-9_.])(\$?)([A-Z]{1,3})(\$?)([1-9][0-9]*)(?![A-Z0-9_])/gi,
        (
          whole,
          lead: string,
          absColumn: string,
          letters: string,
          absRow: string,
          digits: string,
          offset: number,
        ) => {
          const rest = segment.slice(offset + whole.length).trimStart()
          if (rest.startsWith('(')) return whole
          let referencedColumn = 0
          for (const character of letters.toUpperCase()) {
            referencedColumn = referencedColumn * 26 + character.charCodeAt(0) - 64
          }
          referencedColumn -= 1
          const referencedRow = Number(digits) - 1
          const rowToken = absRow ? `R$${referencedRow + 1}` : `R[${referencedRow - row}]`
          const columnToken = absColumn
            ? `C$${referencedColumn + 1}`
            : `C[${referencedColumn - column}]`
          return `${lead}${rowToken}${columnToken}`
        },
      )
    })
    .join('')
    .replace(/\s+/g, '')
    .toUpperCase()
}

export function auditFormulaRange(
  bounds: RangeBounds,
  cells: Readonly<Record<string, CellRead>>,
): FormulaAuditReport {
  const issues = new Map<string, FormulaAuditIssue>()
  const add = (issue: FormulaAuditIssue): void => {
    const key = `${issue.kind}:${issue.address}`
    if (!issues.has(key)) issues.set(key, issue)
  }
  let formulaCount = 0
  for (let row = bounds.startRow; row <= bounds.endRow; row += 1) {
    for (let column = bounds.startColumn; column <= bounds.endColumn; column += 1) {
      const address = formatAddress(row, column)
      const cell = cells[address]
      if (!cell?.formula) continue
      formulaCount += 1
      if (/#REF!/i.test(cell.formula)) {
        add({
          address,
          kind: 'broken_reference',
          severity: 'error',
          formula: cell.formula,
          message: '公式文本包含 #REF! 断裂引用。',
        })
      }
      if (
        typeof cell.value === 'string' &&
        /^#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!|SPILL!|CALC!)/i.test(cell.value)
      ) {
        add({
          address,
          kind: 'error_value',
          severity: 'error',
          formula: cell.formula,
          value: cell.value,
          message: `公式计算结果为 ${cell.value}。`,
        })
      }
    }
  }

  const scanLine = (coordinates: readonly { row: number; column: number }[]): void => {
    const formulas = coordinates.flatMap(({ row, column }, index) => {
      const address = formatAddress(row, column)
      const formula = cells[address]?.formula
      return formula
        ? [{ index, row, column, address, formula, fingerprint: formulaFingerprint(formula, row, column) }]
        : []
    })
    if (formulas.length < 2) return
    const counts = new Map<string, number>()
    for (const item of formulas) counts.set(item.fingerprint, (counts.get(item.fingerprint) ?? 0) + 1)
    const dominant = [...counts].sort((left, right) => right[1] - left[1])[0]
    if (!dominant || dominant[1] < 2) return
    const dominantFingerprint = dominant[0]

    if (formulas.length >= 3) {
      for (const item of formulas) {
        if (item.fingerprint === dominantFingerprint) continue
        const previous = formulas.find((candidate) => candidate.index === item.index - 1)
        const next = formulas.find((candidate) => candidate.index === item.index + 1)
        if (previous?.fingerprint !== dominantFingerprint && next?.fingerprint !== dominantFingerprint)
          continue
        add({
          address: item.address,
          kind: 'pattern_outlier',
          severity: 'warning',
          formula: item.formula,
          expectedPattern: dominantFingerprint,
          message: '公式形状与相邻填充序列的主流模式不一致。',
        })
      }
    }

    for (let index = 1; index < coordinates.length - 1; index += 1) {
      const current = coordinates[index]
      const previous = coordinates[index - 1]
      const next = coordinates[index + 1]
      if (!current || !previous || !next) continue
      const currentAddress = formatAddress(current.row, current.column)
      const currentCell = cells[currentAddress]
      if (currentCell?.formula) continue
      const previousCell = cells[formatAddress(previous.row, previous.column)]
      const nextCell = cells[formatAddress(next.row, next.column)]
      if (!previousCell?.formula || !nextCell?.formula) continue
      const beforePattern = formulaFingerprint(previousCell.formula, previous.row, previous.column)
      const afterPattern = formulaFingerprint(nextCell.formula, next.row, next.column)
      if (beforePattern !== afterPattern) continue
      const hasConstant = currentCell !== undefined && currentCell.value !== null
      add({
        address: currentAddress,
        kind: hasConstant ? 'constant_override' : 'missing_formula',
        severity: 'warning',
        ...(hasConstant ? { value: currentCell.value } : {}),
        expectedPattern: beforePattern,
        message: hasConstant
          ? '一个常量覆盖了上下/左右连续且同型的公式序列。'
          : '空白单元格打断了上下/左右连续且同型的公式序列。',
      })
    }
  }

  for (let column = bounds.startColumn; column <= bounds.endColumn; column += 1) {
    scanLine(
      Array.from({ length: bounds.endRow - bounds.startRow + 1 }, (_, offset) => ({
        row: bounds.startRow + offset,
        column,
      })),
    )
  }
  for (let row = bounds.startRow; row <= bounds.endRow; row += 1) {
    scanLine(
      Array.from({ length: bounds.endColumn - bounds.startColumn + 1 }, (_, offset) => ({
        row,
        column: bounds.startColumn + offset,
      })),
    )
  }

  const ordered = [...issues.values()].sort((left, right) =>
    left.address.localeCompare(right.address, undefined, { numeric: true }),
  )
  return {
    formulaCount,
    issueCount: ordered.length,
    errorCount: ordered.filter((issue) => issue.severity === 'error').length,
    warningCount: ordered.filter((issue) => issue.severity === 'warning').length,
    issues: ordered,
  }
}
