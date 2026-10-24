#!/usr/bin/env node
/**
 * Exercise the exact GenOffice PPTX engine used by Lantai's slide editor:
 * open an authored deck, opt the first native chart into editing, change one
 * literal value, save, reopen, and prove the chart still owns an embedded XLSX.
 */
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import JSZip from 'jszip'
import {
  editChartElement,
  markChartEditable,
  openPptx,
  savePptx,
  type ChartElement,
} from '../../console/vendor/lantai-docx-editor/packages/pptx-engine/src/index.ts'
import { resolveTarget } from '../../console/vendor/lantai-docx-editor/packages/pptx-engine/src/zip.ts'

interface Args {
  input: string
  output: string
  report: string
}

function args(): Args {
  const values = new Map<string, string>()
  for (let index = 2; index < process.argv.length; index += 2) {
    const key = process.argv[index]
    const value = process.argv[index + 1]
    if (!key?.startsWith('--') || !value) throw new Error(`invalid argument near ${key ?? '<end>'}`)
    values.set(key.slice(2), value)
  }
  const input = values.get('input')
  const output = values.get('output')
  const report = values.get('report')
  if (!input || !output || !report) throw new Error('required: --input <pptx> --output <pptx> --report <json>')
  return { input: resolve(input), output: resolve(output), report: resolve(report) }
}

function firstChart(opened: Awaited<ReturnType<typeof openPptx>>): {
  slideIndex: number
  chartIndex: number
  chart: ChartElement
} {
  for (let slideIndex = 0; slideIndex < opened.deck.slides.length; slideIndex++) {
    const charts = opened.deck.slides[slideIndex]!.elements.filter(
      (element): element is ChartElement => element.type === 'chart',
    )
    if (charts[0]) return { slideIndex, chartIndex: 0, chart: charts[0] }
  }
  throw new Error('deck has no native chart')
}

function chartPartPath(
  opened: Awaited<ReturnType<typeof openPptx>>,
  slideIndex: number,
  chart: ChartElement,
): string {
  const slide = opened.deck.slides[slideIndex]!
  const rid = /<c:chart\b[^>]*\br:id="([^"]+)"/.exec(chart.anchor.originalXml)?.[1]
  if (!rid) throw new Error('chart frame has no relationship id')
  const rel = opened.archive.readRels(slide.path).get(rid)
  if (!rel || !rel.type.endsWith('/chart')) throw new Error(`chart relationship ${rid} missing`)
  return resolveTarget(slide.path, rel.target)
}

/** Literal numeric editing may change cache values and attach externalData only. */
function numericChartSkeleton(xml: string): string {
  return xml.replace(/<c:externalData\b[\s\S]*?<\/c:externalData>/g, '')
    .replace(/<c:numCache\b[^>]*>[\s\S]*?<\/c:numCache>/g, (cache) =>
      cache.replace(/(<c:pt\b[^>]*>[\s\S]*?<c:v>)[^<]*(<\/c:v>)/g, '$1#VALUE#$2'))
}

async function main(): Promise<void> {
  const cli = args()
  const original = await openPptx(await readFile(cli.input))
  const target = firstChart(original)
  const firstSeries = target.chart.chart.series[0]
  if (!firstSeries || firstSeries.values[0] == null) throw new Error('first chart has no editable numeric value')
  const before = firstSeries.values[0]
  const after = before + 1
  const originalChartPath = chartPartPath(original, target.slideIndex, target.chart)
  const originalChartXml = original.archive.readText(originalChartPath) ?? ''
  const expectedSeries = target.chart.chart.series.map((series, seriesIndex) => ({
    name: series.name ?? '',
    values: series.values.map((value, pointIndex) =>
      seriesIndex === 0 && pointIndex === 0 ? after : (value ?? 0)),
  }))
  const marked = markChartEditable(original.deck.slides[target.slideIndex]!, target.chart.id)
  if (!marked) throw new Error('failed to opt imported chart into editing')
  const edited = editChartElement(original, target.slideIndex, target.chart.id, {
    series: expectedSeries,
  })
  if (!edited) throw new Error('GenOffice chart edit was rejected')

  const outputBytes = await savePptx(original)
  await mkdir(dirname(cli.output), { recursive: true })
  await writeFile(cli.output, outputBytes)

  const reopened = await openPptx(outputBytes)
  const reopenedCharts = reopened.deck.slides[target.slideIndex]!.elements.filter(
    (element): element is ChartElement => element.type === 'chart',
  )
  const reopenedChart = reopenedCharts[target.chartIndex]
  if (!reopenedChart) throw new Error('edited chart missing after reopen')
  const reopenedValue = reopenedChart.chart.series[0]?.values[0]
  if (reopenedValue !== after) throw new Error(`edited value did not persist: expected ${after}, got ${reopenedValue}`)
  if (JSON.stringify(reopenedChart.chart.series.map((series) => ({ name: series.name ?? '', values: series.values })))
      !== JSON.stringify(expectedSeries)) throw new Error('an unrequested chart series value changed')

  const chartPath = chartPartPath(reopened, target.slideIndex, reopenedChart)
  const chartXml = reopened.archive.readText(chartPath) ?? ''
  if (numericChartSkeleton(chartXml) !== numericChartSkeleton(originalChartXml)) {
    throw new Error('numeric edit changed chart style, axes or unknown OOXML structure')
  }
  const externalRid = /<c:externalData\b[^>]*\br:id="([^"]+)"/.exec(chartXml)?.[1]
  if (!externalRid) throw new Error('edited chart lost externalData')
  const packageRel = reopened.archive.readRels(chartPath).get(externalRid)
  if (!packageRel || !packageRel.type.endsWith('/package')) {
    throw new Error(`edited chart externalData ${externalRid} lacks package relationship`)
  }
  const workbookPath = resolveTarget(chartPath, packageRel.target)
  const workbookBytes = reopened.archive.readBytes(workbookPath)
  if (!workbookBytes?.length) throw new Error(`embedded workbook missing at ${workbookPath}`)
  const workbook = await JSZip.loadAsync(workbookBytes)
  const sheetXml = await workbook.file('xl/worksheets/sheet1.xml')?.async('text')
  if (!sheetXml) throw new Error(`embedded workbook has no first worksheet: ${workbookPath}`)
  const workbookValue = Number(/<c\b[^>]*\br="B2"[^>]*>\s*<v>([^<]+)<\/v>/.exec(sheetXml)?.[1])
  if (workbookValue !== after) {
    throw new Error(`embedded workbook diverged from chart: expected ${after}, got ${workbookValue}`)
  }

  const report = {
    input: cli.input,
    output: cli.output,
    slideCount: reopened.deck.slides.length,
    editedSlide: target.slideIndex + 1,
    chartPart: chartPath,
    workbookPart: workbookPath,
    workbookBytes: workbookBytes.length,
    before,
    after,
    reopenedValue,
    workbookValue,
    nativeChartCount: reopened.deck.slides.reduce(
      (count, slide) => count + slide.elements.filter((element) => element.type === 'chart').length,
      0,
    ),
    chartStylesAndStructurePreserved: true,
    passed: true,
  }
  await mkdir(dirname(cli.report), { recursive: true })
  await writeFile(cli.report, JSON.stringify(report, null, 2) + '\n', 'utf8')
  process.stdout.write(JSON.stringify(report) + '\n')
}

await main()
