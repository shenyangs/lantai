import { describe, expect, it } from 'vitest'
import JSZip from 'jszip'
import { parseChartXml } from '../src/chart'
import { addChart, addElement, createBlankPptx, openPptx, replaceAllInDeck, savePptx, type ChartElement } from '../src/index'

function barChart(categories: string, seriesText = '<c:tx><c:strRef><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>Revenue</c:v></c:pt></c:strCache></c:strRef></c:tx>') {
  return `<c:chartSpace xmlns:c="c"><c:chart><c:plotArea><c:barChart>
    <c:barDir val="col"/><c:grouping val="clustered"/><c:ser><c:idx val="0"/>
      ${seriesText}<c:cat>${categories}</c:cat>
      <c:val><c:numLit><c:ptCount val="4"/>
        <c:pt idx="0"><c:v>10</c:v></c:pt><c:pt idx="1"><c:v>20</c:v></c:pt>
        <c:pt idx="2"><c:v>30</c:v></c:pt><c:pt idx="3"><c:v>40</c:v></c:pt>
      </c:numLit></c:val>
    </c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>`
}

describe('literal chart label fidelity', () => {
  it('reads string literal categories in point-index order without changing lexical text or missing points', () => {
    const model = parseChartXml(barChart(`<c:strLit><c:ptCount val="4"/>
      <c:pt idx="3"><c:v>0</c:v></c:pt><c:pt idx="1"><c:v> Q&amp; A </c:v></c:pt>
      <c:pt idx="0"><c:v>001</c:v></c:pt>
    </c:strLit>`))!
    expect(model.categories).toEqual(['001', ' Q& A ', '', '0'])
    expect(model.series[0]!.name).toBe('Revenue')
    expect(model.series[0]!.values).toEqual([10, 20, 30, 40])
  })

  it('reads numeric literal categories as lexical labels rather than coercing them to numbers', () => {
    const model = parseChartXml(barChart(`<c:numLit><c:formatCode>General</c:formatCode><c:ptCount val="4"/>
      <c:pt idx="2"><c:v>-2.50</c:v></c:pt><c:pt idx="0"><c:v>001</c:v></c:pt>
      <c:pt idx="3"><c:v>1E+03</c:v></c:pt><c:pt idx="1"><c:v>0</c:v></c:pt>
    </c:numLit>`))!
    expect(model.categories).toEqual(['001', '0', '-2.50', '1E+03'])
    expect(model.series[0]!.values).toEqual([10, 20, 30, 40])
  })

  it.each([
    ['Revenue &amp; cost', 'Revenue & cost'],
    ['0', '0'],
    ['001', '001'],
    [' Q1 ', ' Q1 '],
    ['', ''],
  ])('reads the direct series name %j without dropping or normalizing it', (xmlValue, expected) => {
    const model = parseChartXml(barChart('<c:strLit><c:ptCount val="0"/></c:strLit>', `<c:tx><c:v>${xmlValue}</c:v></c:tx>`))!
    expect(model.series[0]!.name).toBe(expected)
    expect(model.series[0]!.values).toEqual([10, 20, 30, 40])
  })

  it('reads a direct series name with xml:space metadata as text rather than as an object', () => {
    const model = parseChartXml(barChart('', '<c:tx><c:v xml:space="preserve"> Q1 &amp; cost </c:v></c:tx>'))!
    expect(model.series[0]!.name).toBe(' Q1 & cost ')
  })

  const points = '<c:ptCount val="4"/><c:pt idx="3"><c:v>0</c:v></c:pt><c:pt idx="1"><c:v> Q&amp; A </c:v></c:pt><c:pt idx="0"><c:v>001</c:v></c:pt>'
  const numericPoints = '<c:ptCount val="4"/><c:pt idx="3"><c:v>1E+03</c:v></c:pt><c:pt idx="1"><c:v>0</c:v></c:pt><c:pt idx="0"><c:v>001</c:v></c:pt>'
  const leafPoints = points.replace('<c:ptCount val="4"/>', '')
  it.each([
    ['string reference', `<c:strRef><c:f>Sheet1!$A$2:$A$5</c:f><c:strCache>${points}</c:strCache></c:strRef>`, ['001', ' Q& A ', '', '0']],
    ['numeric reference', `<c:numRef><c:f>Sheet1!$A$2:$A$5</c:f><c:numCache>${numericPoints}</c:numCache></c:numRef>`, ['001', '0', '', '1E+03']],
    ['multilevel reference', `<c:multiLvlStrRef><c:f>Sheet1!$A$2:$B$5</c:f><c:multiLvlStrCache><c:ptCount val="4"/><c:lvl>${leafPoints}</c:lvl><c:lvl><c:pt idx="0"><c:v>Outer</c:v></c:pt></c:lvl></c:multiLvlStrCache></c:multiLvlStrRef>`, ['001', ' Q& A ', '', '0']],
  ] as const)('preserves existing %s category parsing and cached series names', (_, categories, expected) => {
    const model = parseChartXml(barChart(categories))!
    expect(model.categories).toEqual(expected)
    expect(model.series[0]!.name).toBe('Revenue')
    expect(model.series[0]!.values).toEqual([10, 20, 30, 40])
  })

  it('does not fabricate a series name when c:tx is absent', () => {
    expect(parseChartXml(barChart('', ''))!.series[0]!.name).toBeUndefined()
  })

  it.each(['strLit', 'numLit'] as const)('preserves native %s chart and workbook bytes while editing unrelated text through the public PPTX API', async (literalKind) => {
    const original = await openPptx(await createBlankPptx())
    addElement(original.deck.slides[0]!, {
      kind: 'textbox',
      offset: { x: 914400, y: 457200, cx: 6096000, cy: 457200 },
      paragraphs: [{ runs: [{ text: 'Label fidelity before', fontSize: 24 }] }],
    })
    const categories = ['001', '0', '-2.50', '1E+03']
    addChart(original, 0, {
      kind: 'bar', categories, series: [{ name: 'Revenue & cost', values: [10, 20, 30, 40] }],
      offset: { x: 914400, y: 1371600, cx: 6096000, cy: 3657600 },
    })
    const before = await JSZip.loadAsync(await savePptx(original))
    const chartPath = 'ppt/charts/chart1.xml'
    const literalPoints = categories.map((label, index) => `<c:pt idx="${index}"><c:v>${label}</c:v></c:pt>`).join('')
    const chartXml = (await before.file(chartPath)!.async('text'))
      .replace(/<c:cat>[\s\S]*?<\/c:cat>/, `<c:cat><c:${literalKind}><c:ptCount val="4"/>${literalPoints}</c:${literalKind}></c:cat>`)
      .replace(/<c:tx>[\s\S]*?<\/c:tx>/, '<c:tx><c:v>Revenue &amp; cost</c:v></c:tx>')
    before.file(chartPath, chartXml)

    const opened = await openPptx(await before.generateAsync({ type: 'uint8array' }))
    const chart = opened.deck.slides[0]!.elements.find((el) => el.type === 'chart') as ChartElement
    expect(chart.chart.categories).toEqual(categories)
    expect(chart.chart.series[0]!.name).toBe('Revenue & cost')
    expect(replaceAllInDeck(opened.deck, 'before', 'after').count).toBe(1)
    const bytes = await savePptx(opened)
    const after = await JSZip.loadAsync(bytes)
    const parts = Object.keys(before.files).filter((path) => !before.files[path]!.dir).sort()
    expect(Object.keys(after.files).filter((path) => !after.files[path]!.dir).sort()).toEqual(parts)
    const changed: string[] = []
    for (const path of parts) {
      const oldBytes = await before.file(path)!.async('uint8array')
      const newBytes = await after.file(path)!.async('uint8array')
      if (!Buffer.from(oldBytes).equals(Buffer.from(newBytes))) changed.push(path)
    }
    expect(changed).toEqual(['ppt/slides/slide1.xml'])
    expect(await after.file(chartPath)!.async('text')).toBe(chartXml)
    const reopened = await openPptx(bytes)
    const savedChart = reopened.deck.slides[0]!.elements.find((el) => el.type === 'chart') as ChartElement
    expect(savedChart.chart).toEqual(chart.chart)
    expect(savedChart.chart.series[0]!.values).toEqual([10, 20, 30, 40])
  })
})
