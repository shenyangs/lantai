import { describe, expect, it } from 'vitest'
import JSZip from 'jszip'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { addChart, captureChartWorkbooks, editChartElement, markChartEditable, openPptx, savePptx, type ChartElement } from '../src/index'
import { resolveTarget } from '../src/zip'

const fixture = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'fixtures/01_standard_business.pptx'))
const offset = { x: 914400, y: 914400, cx: 4572000, cy: 2743200 }

async function importedChart(kind: 'bar' | 'comboBarLine' = 'bar') {
  const deck = await openPptx(fixture)
  const series = [{ name: 'Share', values: [31, 9] }]
  if (kind === 'comboBarLine') series.push({ name: 'Trend', values: [4, 6] })
  addChart(deck, 0, { kind, categories: ['Open', 'Files'], series, offset })
  const zip = await JSZip.loadAsync(await savePptx(deck))
  const chartPath = Object.keys(zip.files).find((path) => /^ppt\/charts\/chart\d+\.xml$/.test(path))!
  let chartXml = await zip.file(chartPath)!.async('text')
  chartXml = chartXml.replace(/(<c:valAx>[\s\S]*?)<\/c:scaling>/, '$1<c:min val="0"/><c:max val="48"/></c:scaling>')
    .replace('<c:valAx>', '<c:valAx><c:numFmt formatCode="0.0" sourceLinked="0"/>')
    .replace('</c:valAx>', '<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="1200"><a:solidFill><a:srgbClr val="888888"/></a:solidFill><a:latin typeface="Noto Sans SC"/></a:defRPr></a:pPr><a:endParaRPr lang="zh-CN"/></a:p></c:txPr></c:valAx>')
    .replace('</c:chartSpace>', '<c:extLst><c:ext uri="fidelity-sentinel"><test:unknown xmlns:test="urn:test" keep="yes"/></c:ext></c:extLst></c:chartSpace>')
  zip.file(chartPath, chartXml)
  const rels = await zip.file(`ppt/charts/_rels/${chartPath.split('/').at(-1)}.rels`)!.async('text')
  const workbookPath = resolveTarget(chartPath, /Target="([^"]+\.xlsx)"/.exec(rels)![1]!)
  const workbook = await JSZip.loadAsync(await zip.file(workbookPath)!.async('uint8array'))
  const sheetPath = 'xl/worksheets/sheet1.xml'
  const sheetXml = (await workbook.file(sheetPath)!.async('text')).replace('<c r="B2">', '<c r="B2" s="2">')
    .replace('<sheetData>', '<cols><col min="1" max="3" width="23" customWidth="1"/></cols><sheetData>')
  workbook.file(sheetPath, sheetXml)
  workbook.file('customXml/item1.xml', '<metadata keep="yes"/>')
  zip.file(workbookPath, await workbook.generateAsync({ type: 'uint8array' }))
  const opened = await openPptx(await zip.generateAsync({ type: 'uint8array' }))
  const chart = opened.deck.slides[0]!.elements.find((el) => el.type === 'chart') as ChartElement
  // Same imported-chart opt-in as the real slide editor.
  chart.anchor.originalXml = chart.anchor.originalXml.replace(' descr="aislides-chart"', '')
  delete chart.descr
  return { opened, chart, chartPath, chartXml, workbookPath, sheetPath, sheetXml, workbook }
}

describe('numeric chart editing preserves imported OOXML and workbook formatting', () => {
  it('changes one value without rebuilding axes, fonts, number formats, extensions or workbook cells', async () => {
    const original = await importedChart()
    const { opened, chart, chartPath, chartXml, workbookPath, sheetPath, sheetXml, workbook } = original
    expect(opened.archive.readText(chartPath)).toBe(chartXml)
    expect(markChartEditable(opened.deck.slides[0]!, chart.id)).toBe(true)
    expect(opened.archive.readText(chartPath)).toBe(chartXml)
    expect(editChartElement(opened, 0, chart.id, { categories: ['Open', 'Files'], series: [{ name: 'Share', values: [32, 9] }] })).toBe(true)
    const expectedXml = chartXml.replace('<c:pt idx="0"><c:v>31</c:v></c:pt>', '<c:pt idx="0"><c:v>32</c:v></c:pt>')
    expect(opened.archive.readText(chartPath)).toBe(expectedXml)

    const saved = await savePptx(opened)
    const zip = await JSZip.loadAsync(saved)
    expect(await zip.file(chartPath)!.async('text')).toBe(expectedXml)
    const changedWorkbook = await JSZip.loadAsync(await zip.file(workbookPath)!.async('uint8array'))
    expect(await changedWorkbook.file(sheetPath)!.async('text')).toBe(sheetXml.replace('<c r="B2" s="2"><v>31</v>', '<c r="B2" s="2"><v>32</v>'))
    expect(Object.keys(changedWorkbook.files).sort()).toEqual(Object.keys(workbook.files).sort())
    for (const path of Object.keys(workbook.files).filter((path) => !workbook.files[path]!.dir && path !== sheetPath)) {
      expect(await changedWorkbook.file(path)!.async('uint8array')).toEqual(await workbook.file(path)!.async('uint8array'))
    }
    const reopened = await openPptx(saved)
    const after = reopened.deck.slides[0]!.elements.find((el) => el.type === 'chart') as ChartElement
    expect(after.chart.series[0]!.values).toEqual([32, 9])
    expect(after.chart.valAxis?.min).toBe(0)
    expect(after.chart.valAxis?.max).toBe(48)
    expect(after.chart.valAxis?.labelSizePt).toBe(12)
    expect(after.chart.valAxis?.labelColor).toBe('#888888')
  })

  it('merges consecutive value edits before save and preserves every non-value byte', async () => {
    const { opened, chart, chartPath, chartXml, workbookPath, sheetPath, sheetXml } = await importedChart()
    markChartEditable(opened.deck.slides[0]!, chart.id)
    for (const values of [[32, 9], [33, 10], [31, 11]]) {
      expect(editChartElement(opened, 0, chart.id, { series: [{ name: 'Share', values }] })).toBe(true)
    }
    const saved = await JSZip.loadAsync(await savePptx(opened))
    expect(await saved.file(chartPath)!.async('text')).toBe(chartXml.replace('<c:pt idx="1"><c:v>9</c:v></c:pt>', '<c:pt idx="1"><c:v>11</c:v></c:pt>'))
    const workbook = await JSZip.loadAsync(await saved.file(workbookPath)!.async('uint8array'))
    expect(await workbook.file(sheetPath)!.async('text')).toBe(sheetXml.replace('<c r="B3"><v>9</v>', '<c r="B3"><v>11</v>'))
  })

  it('does not replace or rezip a workbook for an unchanged numeric patch', async () => {
    const { opened, chart, chartPath, chartXml, workbookPath } = await importedChart()
    const bytes = opened.archive.readBytes(workbookPath)
    markChartEditable(opened.deck.slides[0]!, chart.id)
    expect(editChartElement(opened, 0, chart.id, { series: [{ name: 'Share', values: [31, 9] }] })).toBe(true)
    const zip = await JSZip.loadAsync(await savePptx(opened))
    expect(await zip.file(chartPath)!.async('text')).toBe(chartXml)
    expect(await zip.file(workbookPath)!.async('uint8array')).toEqual(bytes)
  })

  it.each(['category-range', 'series-header'])('rejects a cache-only chart with mismatched %s instead of fabricating its workbook', async (part) => {
    const { opened, chart, chartPath, chartXml } = await importedChart()
    let raw = chartXml.replace(/<c:externalData\b[\s\S]*?<\/c:externalData>/, '')
    raw = part === 'category-range'
      ? raw.replace('Sheet1!$A$2:$A$3', 'Sheet1!$D$2:$D$3')
      : raw.replace('Sheet1!$B$1', 'Sheet1!$E$1')
    opened.archive.entries.set(chartPath, Buffer.from(raw))
    markChartEditable(opened.deck.slides[0]!, chart.id)
    expect(editChartElement(opened, 0, chart.id, { series: [{ name: 'Share', values: [32, 9] }] })).toBe(false)
    expect(opened.archive.readText(chartPath)).toBe(raw)
  })

  it('snapshots caller-owned data before scheduling a cache-only chart workbook', async () => {
    const { opened, chart, chartPath, chartXml, workbookPath, sheetPath } = await importedChart()
    opened.archive.entries.set(chartPath, Buffer.from(chartXml.replace(/<c:externalData\b[\s\S]*?<\/c:externalData>/, '')))
    markChartEditable(opened.deck.slides[0]!, chart.id)
    const patch = { categories: ['Open', 'Files'], series: [{ name: 'Share', values: [32, 9] }] }
    expect(editChartElement(opened, 0, chart.id, patch)).toBe(true)
    patch.categories[0] = 'Changed by caller'
    patch.series[0]!.name = 'Changed by caller'
    patch.series[0]!.values[0] = 999
    const bytes = await savePptx(opened)
    const zip = await JSZip.loadAsync(bytes)
    const book = await JSZip.loadAsync(await zip.file(workbookPath)!.async('uint8array'))
    const sheet = await book.file(sheetPath)!.async('text')
    expect(sheet).toContain('<c r="B2"><v>32</v>')
    expect(sheet).not.toContain('Changed by caller')
    expect(sheet).not.toContain('<v>999</v>')
    const reopened = await openPptx(bytes)
    const after = reopened.deck.slides[0]!.elements.find((el) => el.type === 'chart') as ChartElement
    expect(after.chart.categories).toEqual(['Open', 'Files'])
    expect(after.chart.series[0]!.values).toEqual([32, 9])
  })

  it('rejects inconsistent per-series category caches before adding a combo chart workbook', async () => {
    const { opened, chart, chartPath, chartXml } = await importedChart('comboBarLine')
    const raw = chartXml.replace(/<c:externalData\b[\s\S]*?<\/c:externalData>/, '')
      .replace(/(<c:lineChart>[\s\S]*?<c:cat>[\s\S]*?)<c:v>Open<\/c:v>/, '$1<c:v>Different</c:v>')
    expect(raw).toContain('<c:v>Different</c:v>')
    opened.archive.entries.set(chartPath, Buffer.from(raw))
    markChartEditable(opened.deck.slides[0]!, chart.id)
    const original = new Map(opened.archive.entries)
    expect(editChartElement(opened, 0, chart.id, { series: [{ name: 'Share', values: [32, 9] }, { name: 'Trend', values: [4, 6] }] })).toBe(false)
    expect(opened.archive.entries).toEqual(original)
  })

  it.each([
    { cacheOnly: false, location: 'standard' }, { cacheOnly: true, location: 'standard' },
    { cacheOnly: false, location: 'nested' }, { cacheOnly: true, location: 'nested' },
    { cacheOnly: false, location: 'other-directory' }, { cacheOnly: true, location: 'other-directory' },
  ])('rejects a shared workbook without mutation, cache-only=$cacheOnly, location=$location', async ({ cacheOnly, location }) => {
    let opened = await openPptx(fixture)
    for (const kind of ['bar', 'line'] as const) addChart(opened, 0, {
      kind, categories: ['Open', 'Files'], series: [{ name: 'Share', values: [31, 9] }], offset,
    })
    const zip = await JSZip.loadAsync(await savePptx(opened))
    const chartPath = 'ppt/charts/chart1.xml'
    const target = /Target="([^"]+\.xlsx)"/.exec(await zip.file('ppt/charts/_rels/chart1.xml.rels')!.async('text'))![1]!
    const otherRels = 'ppt/charts/_rels/chart2.xml.rels'
    zip.file(otherRels, (await zip.file(otherRels)!.async('text'))
      .replace(/Target="[^"]+\.xlsx"/, `Target="${target}"`))
    if (location !== 'standard') {
      const originalPath = 'ppt/charts/chart2.xml'
      const directory = location === 'nested' ? 'ppt/charts/sub' : 'ppt/alternate'
      const relocatedPath = `${directory}/chart2.xml`
      const relocatedTarget = location === 'nested' ? `../${target}` : target
      zip.file(relocatedPath, await zip.file(originalPath)!.async('uint8array'))
      zip.file(`${directory}/_rels/chart2.xml.rels`, (await zip.file(otherRels)!.async('text'))
        .replace(`Target="${target}"`, `Target="${relocatedTarget}"`))
      zip.remove(originalPath)
      zip.remove(otherRels)
      const slideRels = 'ppt/slides/_rels/slide1.xml.rels'
      zip.file(slideRels, (await zip.file(slideRels)!.async('text'))
        .replace('../charts/chart2.xml', `../${directory.slice(4)}/chart2.xml`))
      zip.file('[Content_Types].xml', (await zip.file('[Content_Types].xml')!.async('text'))
        .replace(`/${originalPath}`, `/${relocatedPath}`))
    }
    if (cacheOnly) zip.file(chartPath, (await zip.file(chartPath)!.async('text'))
      .replace(/<c:externalData\b[\s\S]*?<\/c:externalData>/, ''))
    opened = await openPptx(await zip.generateAsync({ type: 'uint8array' }))
    const charts = opened.deck.slides[0]!.elements.filter((el) => el.type === 'chart') as ChartElement[]
    expect(charts).toHaveLength(2)
    const chart = charts[0]!
    markChartEditable(opened.deck.slides[0]!, chart.id)
    const original = new Map(opened.archive.entries)
    const queues = captureChartWorkbooks(opened.archive)
    expect(editChartElement(opened, 0, chart.id, { series: [{ name: 'Share', values: [32, 9] }] })).toBe(false)
    expect(opened.archive.entries).toEqual(original)
    expect(chart.chart.series[0]!.values).toEqual([31, 9])
    expect(captureChartWorkbooks(opened.archive)).toEqual(queues)
  })

  it('refuses to save a formula-driven or stale workbook cell without overwriting the workbook', async () => {
    for (const cell of ['<c r="B2" s="2"><f>SUM(A1:A2)</f><v>31</v>', '<c r="B2" s="2"><v>30</v>']) {
      const { opened, chart, workbookPath, sheetPath, sheetXml } = await importedChart()
      const book = await JSZip.loadAsync(opened.archive.readBytes(workbookPath)!)
      book.file(sheetPath, sheetXml.replace('<c r="B2" s="2"><v>31</v>', cell))
      const original = await book.generateAsync({ type: 'uint8array' })
      opened.archive.entries.set(workbookPath, original)
      markChartEditable(opened.deck.slides[0]!, chart.id)
      expect(editChartElement(opened, 0, chart.id, { series: [{ name: 'Share', values: [32, 9] }] })).toBe(true)
      await expect(savePptx(opened)).rejects.toThrow('literal cell disagrees')
      expect(opened.archive.readBytes(workbookPath)).toEqual(original)
    }
  })
})
