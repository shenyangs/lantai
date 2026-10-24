/** Lossless literal-value editing. Do not serialize chart OOXML through a style template. */
import { XMLParser } from 'fast-xml-parser'
import { parseChartXml, type ChartModel } from './chart'
import { attachChartWorkbook, chartHasSharedWorkbook, queueChartWorkbookValues, type ChartWorkbookValueUpdate } from './chart-workbook'
import type { PackageArchive } from './zip'

const textParser = new XMLParser({ parseTagValue: false, trimValues: false })
const plotOrder = ['barChart', 'areaChart', 'lineChart', 'pieChart', 'doughnutChart', 'scatterChart', 'radarChart']

function columnName(oneBased: number): string {
  let value = oneBased, out = ''
  while (value > 0) {
    value--
    out = String.fromCharCode(65 + value % 26) + out
    value = Math.floor(value / 26)
  }
  return out
}

/** Only a literal single-column A1 range can be mapped safely to workbook cells. */
function cellsForFormula(xml: string, count: number): Array<{ sheetName: string; ref: string }> | null {
  const raw = /<c:f>([^<]+)<\/c:f>/.exec(xml)?.[1]
  if (!raw) return null
  const decoded = String(textParser.parse(`<text>${raw}</text>`).text ?? '')
  const match = /^(?:'((?:[^']|'')+)'|([^'!\[\]]+))!\$?([A-Z]+)\$?([1-9]\d*)(?::\$?([A-Z]+)\$?([1-9]\d*))?$/.exec(decoded)
  if (!match) return null
  const start = Number(match[4]), end = Number(match[6] ?? match[4])
  if ((match[5] ?? match[3]) !== match[3] || end - start + 1 !== count || count < 1
      || !Number.isSafeInteger(start) || !Number.isSafeInteger(end)) return null
  const sheetName = match[1]?.replace(/''/g, "'") ?? match[2]!
  return Array.from({ length: count }, (_, index) => ({ sheetName, ref: `${match[3]}${start + index}` }))
}

/** A shared category column can only represent identical dense series caches. */
function categoryCacheMatches(xml: string, expected: string[]): boolean {
  const cache = /<c:(strCache|numCache)\b[^>]*>[\s\S]*?<\/c:\1>/.exec(xml)?.[0]
  if (!cache || !/<c:(strRef|numRef)\b/.test(xml) || /<c:multiLvlStrRef\b/.test(xml)) return false
  const count = Number(/<c:ptCount\b[^>]*\bval="(\d+)"/.exec(cache)?.[1])
  const points = [...cache.matchAll(/<c:pt\b[^>]*\bidx="(\d+)"[^>]*>([\s\S]*?)<\/c:pt>/g)]
  if (count !== expected.length || points.length !== count
      || new Set(points.map((point) => Number(point[1]))).size !== count) return false
  return points.every((point) => {
    const index = Number(point[1])
    const value = /<c:v>([^<]*)<\/c:v>/.exec(point[2]!)
    return index < count && value != null
      && String(textParser.parse(`<text>${value[1]}</text>`).text ?? '') === expected[index]
  })
}

export function patchNumericChartData(
  archive: PackageArchive, chartPath: string, existing: ChartModel,
  series: Array<{ name: string; values: number[] }>,
): string | null {
  const xml = archive.readText(chartPath)
  if (!xml) return null
  const current = parseChartXml(xml)
  if (!current || current.kind !== existing.kind || JSON.stringify(current.categories) !== JSON.stringify(existing.categories)
      || JSON.stringify(current.series.map((s) => [s.name ?? '', s.values]))
        !== JSON.stringify(existing.series.map((s) => [s.name ?? '', s.values]))) return null
  const plots = [...xml.matchAll(/<c:(barChart|areaChart|lineChart|pieChart|doughnutChart|scatterChart|radarChart)\b[^>]*>[\s\S]*?<\/c:\1>/g)]
  if (new Set(plots.map((plot) => plot[1])).size !== plots.length) return null
  plots.sort((a, b) => plotOrder.indexOf(a[1]!) - plotOrder.indexOf(b[1]!))
  const replacements: Array<{ start: number; end: number; xml: string }> = []
  const updates: ChartWorkbookValueUpdate[] = []
  const seriesXml: string[] = []
  let seriesIndex = 0
  for (const plot of plots) {
    for (const ser of plot[0].matchAll(/<c:ser\b[^>]*>[\s\S]*?<\/c:ser>/g)) {
      seriesXml.push(ser[0])
      const before = existing.series[seriesIndex]
      const after = series[seriesIndex++]
      if (!before || !after || after.name !== (before.name ?? '') || after.values.length !== before.values.length
          || after.values.some((value) => !Number.isFinite(value)) || before.values.some((value) => value == null || !Number.isFinite(value))) return null
      const valueTag = plot[1] === 'scatterChart' ? 'yVal' : 'val'
      const val = new RegExp(`<c:${valueTag}\\b[^>]*>[\\s\\S]*?</c:${valueTag}>`).exec(ser[0])
      const cache = val && /<c:numCache\b[^>]*>[\s\S]*?<\/c:numCache>/.exec(val[0])
      if (!val || !cache || !/<c:numRef\b/.test(val[0])) return null
      const cells = cellsForFormula(val[0], after.values.length)
      const points = [...cache[0].matchAll(/<c:pt\b[^>]*\bidx="(\d+)"[^>]*>([\s\S]*?)<\/c:pt>/g)]
      const count = Number(/<c:ptCount\b[^>]*\bval="(\d+)"/.exec(cache[0])?.[1])
      if (!cells || count !== after.values.length || points.length !== count
          || new Set(points.map((pt) => Number(pt[1]))).size !== count) return null
      for (const point of points) {
        const index = Number(point[1])
        const value = /<c:v>([^<]*)<\/c:v>/.exec(point[2]!)
        if (!value || !value[1]!.trim() || index >= count || Number(value[1]) !== before.values[index]) return null
        const next = after.values[index]!
        if (next === before.values[index]) continue
        const pointStart = plot.index! + ser.index! + val.index + cache.index + point.index!
        replacements.push({ start: pointStart, end: pointStart + point[0].length,
                            xml: point[0].replace(value[0], `<c:v>${next}</c:v>`) })
        updates.push({ ...cells[index]!, before: before.values[index]!, value: next })
      }
    }
  }
  if (seriesIndex !== series.length || seriesIndex !== existing.series.length) return null
  if (!updates.length) return xml
  if (chartHasSharedWorkbook(archive, chartPath)) return null
  let next = xml
  for (const replacement of replacements.sort((a, b) => b.start - a.start)) {
    next = next.slice(0, replacement.start) + replacement.xml + next.slice(replacement.end)
  }
  if (/<c:externalData\b/.test(xml)) {
    return queueChartWorkbookValues(archive, chartPath, xml, updates) ? next : null
  }
  // Cache-only exports are given an embedded literal workbook without rebuilding
  // the chart. Nonstandard/missing ranges cannot safely use the generated matrix.
  const formulas = [...xml.matchAll(/<c:f>([^<]+)<\/c:f>/g)].map((match) => match[1]!)
  if (existing.kind === 'scatter' || formulas.some((formula) => !/^Sheet1!\$[A-Z]+\$[1-9]\d*(?::\$[A-Z]+\$[1-9]\d*)?$/.test(formula))) return null
  for (let si = 0; si < series.length; si++) {
    const column = columnName(si + 2)
    const ser = seriesXml[si]!
    const values = /<c:val\b[^>]*>[\s\S]*?<\/c:val>/.exec(ser)?.[0]
    const valueCells = values && cellsForFormula(values, series[si]!.values.length)
    const header = /<c:tx\b[^>]*>[\s\S]*?<\/c:tx>/.exec(ser)?.[0]
    const headerCells = header && cellsForFormula(header, 1)
    const categories = /<c:cat\b[^>]*>[\s\S]*?<\/c:cat>/.exec(ser)?.[0]
    const categoryCells = categories && cellsForFormula(categories, existing.categories.length)
    if (!valueCells || valueCells.length !== existing.categories.length
        || valueCells.some((cell, index) => cell.sheetName !== 'Sheet1' || cell.ref !== `${column}${index + 2}`)
        || !headerCells || headerCells[0]!.sheetName !== 'Sheet1' || headerCells[0]!.ref !== `${column}1`
        || !categoryCells || categoryCells.some((cell, index) => cell.sheetName !== 'Sheet1' || cell.ref !== `A${index + 2}`)
        || !categories || !categoryCacheMatches(categories, existing.categories)) return null
  }
  return attachChartWorkbook(archive, chartPath, next, { categories: existing.categories, series })
}
