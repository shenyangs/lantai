import { describe, expect, it } from 'vitest'
import JSZip from 'jszip'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { addChart, addMedia, addPicture, copyElementData, copySlide, createBlankPptx, duplicateSlide, editChartElement, ensureTableStylePart, getElementLink, getSlideNotes, insertBlankSlide, insertSlideWithLayout, markChartEditable, openPptx, pasteElements, pasteSlide, savePptx, setElementLink, setSlideLayout, setSlideNotes, type ChartElement, type PictureElement } from '../src/index'
import { ensureBuiltinLayout } from '../src/builtin-layouts'
import { listSlideLayouts } from '../src/layout'
import { appendRelationshipsXml, nextRelationshipId, PackageArchive, readRelationshipsXml, relsPathFor, removeRelationshipXml, replaceRelationshipTargetXml, resolveTarget } from '../src/zip'

const fixture = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'fixtures/01_standard_business.pptx'))
const relNamespace = 'http://schemas.openxmlformats.org/package/2006/relationships'

async function prefixRelationships(bytes: Uint8Array, prefix: string) {
  const zip = await JSZip.loadAsync(bytes)
  for (const path of Object.keys(zip.files).filter(path => path.endsWith('.rels'))) {
    const raw = await zip.file(path)!.async('text')
    const transformed = raw.replace(`xmlns="${relNamespace}"`, `xmlns:${prefix}="${relNamespace}"`)
      .replace(/(<\/?)(Relationships?|Relationship)\b/g, `$1${prefix}:$2`)
    zip.file(path, transformed)
  }
  return zip.generateAsync({ type: 'uint8array' })
}

const writeModes = ['whole-ns0', 'mixed', 'child-local', 'root-only'] as const
type RelationshipWriteMode = typeof writeModes[number] | 'whole-λ' | 'whole-关系' | 'single'
const offset = { x: 914400, y: 914400, cx: 1828800, cy: 914400 }
const png = Uint8Array.from(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64'))
const syntheticChart = {
  kind: 'bar' as const, categories: ['One', 'Two'], series: [{ name: 'Count', values: [4, 6] }], offset,
}

function rewriteRelationshipXml(raw: string, mode: RelationshipWriteMode): string {
  if (mode === 'single') return raw.replace(/\b(Id|Type|Target|TargetMode)="([^"]*)"/g,
    (_all, name, value) => `${name}='${value.replace(/'/g, '&apos;')}'`)
  if (mode.startsWith('whole-')) {
    const prefix = mode.slice('whole-'.length)
    return raw.replace(`xmlns="${relNamespace}"`, `xmlns:${prefix}="${relNamespace}"`)
      .replace(/(<\/?)(Relationships|Relationship)\b/g, `$1${prefix}:$2`)
  }
  if (mode === 'mixed') return raw.replace(`xmlns="${relNamespace}"`, `xmlns:root="${relNamespace}" xmlns:child="${relNamespace}"`)
    .replace(/(<\/?)(Relationships|Relationship)\b/g, (_all, start, name) => `${start}${name === 'Relationships' ? 'root' : 'child'}:${name}`)
  if (mode === 'child-local') return raw.replace(/<(\/?)Relationship\b/g,
    (_all, close) => close ? '</one:Relationship' : `<one:Relationship xmlns:one="${relNamespace}"`)
  return raw.replace('<Relationships ', `<root:Relationships xmlns:root="${relNamespace}" `)
    .replace('</Relationships>', '</root:Relationships>')
}

async function rewritePackageRelationships(bytes: Uint8Array, mode: RelationshipWriteMode, nested = false) {
  const zip = await JSZip.loadAsync(bytes)
  for (const path of Object.keys(zip.files).filter(path => path.endsWith('.rels'))) {
    zip.file(path, rewriteRelationshipXml(await zip.file(path)!.async('text'), mode))
  }
  if (nested) {
    for (const path of Object.keys(zip.files).filter(path => path.endsWith('.xlsx'))) {
      const workbook = await JSZip.loadAsync(await zip.file(path)!.async('uint8array'))
      for (const relPath of Object.keys(workbook.files).filter(path => path.endsWith('.rels'))) {
        workbook.file(relPath, rewriteRelationshipXml(await workbook.file(relPath)!.async('text'), mode))
      }
      zip.file(path, await workbook.generateAsync({ type: 'uint8array' }))
    }
  }
  return zip.generateAsync({ type: 'uint8array' })
}

function workbookForChart(opened: Awaited<ReturnType<typeof openPptx>>) {
  const slide = opened.deck.slides[0]!
  const chartRel = [...opened.archive.readRels(slide.path).values()].find(rel => rel.type.endsWith('/chart'))!
  const chartPath = resolveTarget(slide.path, chartRel.target)
  const relationship = [...opened.archive.readRels(chartPath).values()].find(rel => rel.type.endsWith('/package'))!
  return { relationship, path: resolveTarget(chartPath, relationship.target) }
}

describe('OPC relationships are identified by namespace, not a literal prefix', () => {
  it.each(['ns0', 'pkg', 'r'])('loads chart/workbook/layout relationships with %s prefix', async prefix => {
    const authored = await openPptx(fixture)
    addChart(authored, 0, {
      kind: 'bar', categories: ['One', 'Two'], series: [{ name: 'Count', values: [4, 6] }],
      offset: { x: 914400, y: 914400, cx: 4572000, cy: 2743200 },
    })
    const bytes = await prefixRelationships(await savePptx(authored), prefix)
    const opened = await openPptx(bytes)
    expect(opened.deck.slides.length).toBe(authored.deck.slides.length)
    const slide = opened.deck.slides[0]!
    expect(opened.archive.readRels(slide.path).size).toBeGreaterThan(0)
    expect(opened.archive.resolveSlideChain(slide.path).layoutPath).toBeTruthy()
    const chart = slide.elements.find(element => element.type === 'chart') as ChartElement | undefined
    expect(chart).toBeDefined()
    expect(chart!.chart.series[0]!.values).toEqual([4, 6])
    expect(markChartEditable(slide, chart!.id)).toBe(true)
    expect(editChartElement(opened, 0, chart!.id, { series: [{ name: 'Count', values: [5, 6] }] })).toBe(true)
    const reopened = await openPptx(await savePptx(opened))
    expect((reopened.deck.slides[0]!.elements.find(element => element.type === 'chart') as ChartElement).chart.series[0]!.values).toEqual([5, 6])
  })

  it.each(['ns0', 'pkg', 'r'])('retains all unmodified %s relationship XML bytes on save', async prefix => {
    const bytes = await prefixRelationships(fixture, prefix)
    const opened = await openPptx(bytes)
    expect(opened.deck.slides.length).toBeGreaterThan(0)
    const saved = await PackageArchive.open(await savePptx(opened))
    for (const [path, value] of opened.archive.entries) {
      if (path.endsWith('.rels')) expect(saved.readBytes(path)).toEqual(value)
    }
  })

  it.each(writeModes)('persists new native charts in %s relationship documents', async mode => {
    const opened = await openPptx(await rewritePackageRelationships(await createBlankPptx(), mode))
    expect(addChart(opened, 0, syntheticChart)).not.toBeNull()
    const reopened = await openPptx(await savePptx(opened))
    const charts = reopened.deck.slides[0]!.elements.filter(element => element.type === 'chart') as ChartElement[]
    expect(charts).toHaveLength(1)
    expect(charts[0]!.chart.series[0]!.values).toEqual([4, 6])
    expect(reopened.archive.has(workbookForChart(reopened).path)).toBe(true)
  })

  it.each(writeModes)('persists picture relationships and actual media bytes in %s documents', async mode => {
    const opened = await openPptx(await rewritePackageRelationships(await createBlankPptx(), mode))
    expect(addPicture(opened, opened.deck.slides[0]!, { bytes: png, ext: 'png', offset })).not.toBeNull()
    const reopened = await openPptx(await savePptx(opened))
    const picture = reopened.deck.slides[0]!.elements.find(element => element.type === 'picture') as PictureElement
    expect(picture.mediaRef).toBeTruthy()
    expect(reopened.archive.readBytes(picture.mediaRef!)).toEqual(png)
  })

  it.each(writeModes)('persists new speaker notes in %s relationship documents', async mode => {
    const opened = await openPptx(await rewritePackageRelationships(await createBlankPptx(), mode))
    expect(setSlideNotes(opened, 0, 'Synthetic new note')).toBe(true)
    const reopened = await openPptx(await savePptx(opened))
    expect(getSlideNotes(reopened.archive, reopened.deck.slides[0]!.path)).toBe('Synthetic new note')
  })

  it.each(writeModes)('reuses the real XLSX relationship for structural chart edits in %s documents', async mode => {
    const authored = await openPptx(await createBlankPptx())
    addChart(authored, 0, syntheticChart)
    const opened = await openPptx(await rewritePackageRelationships(await savePptx(authored), mode))
    const previous = workbookForChart(opened)
    const slide = opened.deck.slides[0]!
    const chart = slide.elements.find(element => element.type === 'chart') as ChartElement
    expect(markChartEditable(slide, chart.id)).toBe(true)
    expect(editChartElement(opened, 0, chart.id, {
      categories: ['Revised one', 'Revised two'], series: [{ name: 'Count', values: [7, 8] }],
    })).toBe(true)
    const reopened = await openPptx(await savePptx(opened))
    expect(workbookForChart(reopened)).toEqual(previous)
    const workbook = await JSZip.loadAsync(reopened.archive.readBytes(previous.path)!)
    expect(await workbook.file('xl/worksheets/sheet1.xml')!.async('text')).toMatch(/<c\b[^>]*\br="B2"[^>]*>[\s\S]*?<v>7<\/v>/)
    const editedChart = reopened.deck.slides[0]!.elements.find(element => element.type === 'chart') as ChartElement
    expect(editedChart.chart.series[0]!.values).toEqual([7, 8])
  })

  it.each(writeModes)('updates literal values through nested XLSX %s relationship maps', async mode => {
    const authored = await openPptx(await createBlankPptx())
    addChart(authored, 0, syntheticChart)
    const opened = await openPptx(await rewritePackageRelationships(await savePptx(authored), mode, true))
    const source = workbookForChart(opened)
    const originalWorkbook = await JSZip.loadAsync(opened.archive.readBytes(source.path)!)
    const originalMap = await originalWorkbook.file('xl/_rels/workbook.xml.rels')!.async('uint8array')
    const slide = opened.deck.slides[0]!
    const chart = slide.elements.find(element => element.type === 'chart') as ChartElement
    expect(markChartEditable(slide, chart.id)).toBe(true)
    expect(editChartElement(opened, 0, chart.id, { series: [{ name: 'Count', values: [5, 6] }] })).toBe(true)
    const reopened = await openPptx(await savePptx(opened))
    expect(workbookForChart(reopened)).toEqual(source)
    const workbook = await JSZip.loadAsync(reopened.archive.readBytes(source.path)!)
    expect(await workbook.file('xl/_rels/workbook.xml.rels')!.async('uint8array')).toEqual(originalMap)
    expect(await workbook.file('xl/worksheets/sheet1.xml')!.async('text')).toMatch(/<c\b[^>]*\br="B2"[^>]*>[\s\S]*?<v>5<\/v>/)
    const editedChart = reopened.deck.slides[0]!.elements.find(element => element.type === 'chart') as ChartElement
    expect(editedChart.chart.series[0]!.values).toEqual([5, 6])
  })

  it.each(writeModes)('switches exactly one existing layout relationship in %s documents', async mode => {
    const opened = await openPptx(await rewritePackageRelationships(fixture, mode))
    const slidePath = opened.deck.slides[0]!.path
    const original = [...opened.archive.readRels(slidePath).values()].find(rel => rel.type.endsWith('/slideLayout'))!
    const current = opened.archive.resolveSlideChain(slidePath).layoutPath
    const target = listSlideLayouts(opened.archive).find(layout => layout.path !== current)!
    expect(setSlideLayout(opened, 0, target.path)).not.toBeNull()
    const reopened = await openPptx(await savePptx(opened))
    const layouts = [...reopened.archive.readRels(slidePath).values()].filter(rel => rel.type.endsWith('/slideLayout'))
    expect(layouts).toHaveLength(1)
    expect(layouts[0]!.id).toBe(original.id)
    expect(reopened.archive.resolveSlideChain(slidePath).layoutPath).toBe(target.path)
  })

  for (const operation of ['duplicate', 'blank', 'layout'] as const) {
    it.each(writeModes)(`registers a ${operation} slide in %s presentation relationships`, async mode => {
      const opened = await openPptx(await rewritePackageRelationships(fixture, mode))
      const count = opened.deck.slides.length
      const inserted = operation === 'duplicate' ? duplicateSlide(opened, 0)
        : operation === 'blank' ? insertBlankSlide(opened, 0)
          : insertSlideWithLayout(opened, 0, listSlideLayouts(opened.archive)[0]!.path)
      expect(inserted).not.toBeNull()
      const reopened = await openPptx(await savePptx(opened))
      expect(reopened.deck.slides).toHaveLength(count + 1)
      expect(reopened.deck.slides[1]!.path).toBe(inserted!.path)
      expect(reopened.archive.resolveSlideChain(inserted!.path).layoutPath).toBeTruthy()
    })
  }

  it.each(writeModes)('persists element hyperlinks with %s relationship roots', async mode => {
    const opened = await openPptx(await rewritePackageRelationships(fixture, mode))
    const element = opened.deck.slides[0]!.elements[0]!
    const target = { kind: 'url' as const, url: 'https://example.invalid/synthetic?a=1&b=2' }
    expect(setElementLink(opened, 0, element.id, target)).not.toBeNull()
    const reopened = await openPptx(await savePptx(opened))
    expect(getElementLink(reopened, 0, reopened.deck.slides[0]!.elements[0]!.id)).toEqual(target)
  })

  it.each(writeModes)('persists dual video and poster relationships in %s documents', async mode => {
    const opened = await openPptx(await rewritePackageRelationships(await createBlankPptx(), mode))
    const bytes = Uint8Array.from([0, 0, 0, 8, 102, 116, 121, 112])
    expect(addMedia(opened, 0, { kind: 'video', bytes, ext: 'mp4', poster: { bytes: png, ext: 'png' }, offset })).not.toBeNull()
    const reopened = await openPptx(await savePptx(opened))
    const relationships = [...reopened.archive.readRels(reopened.deck.slides[0]!.path).values()]
    expect(relationships.filter(rel => rel.type.endsWith('/video') || rel.type.endsWith('/media'))).toHaveLength(2)
    const video = relationships.find(rel => rel.type.endsWith('/video'))!
    expect(reopened.archive.readBytes(resolveTarget(reopened.deck.slides[0]!.path, video.target))).toEqual(bytes)
    expect(reopened.deck.slides[0]!.elements).toHaveLength(1)
  })

  it.each(writeModes)('persists copied picture relationships in %s destination documents', async mode => {
    const initial = await openPptx(await createBlankPptx())
    insertBlankSlide(initial, 0)
    const opened = await openPptx(await rewritePackageRelationships(await savePptx(initial), mode))
    addPicture(opened, opened.deck.slides[0]!, { bytes: png, ext: 'png', offset })
    const picture = opened.deck.slides[0]!.elements.find(element => element.type === 'picture')!
    const item = copyElementData(opened, opened.deck.slides[0]!, picture)
    const destination = opened.deck.slides[1]!
    const path = relsPathFor(destination.path)
    opened.archive.entries.set(path, Buffer.from(rewriteRelationshipXml(opened.archive.readText(path)!, mode)))
    expect(pasteElements(opened, 1, [item], { dx: 0, dy: 0 })).not.toBeNull()
    const reopened = await openPptx(await savePptx(opened))
    const copied = reopened.deck.slides[1]!.elements.find(element => element.type === 'picture') as PictureElement
    expect(copied.mediaRef).toBeTruthy()
    expect(reopened.archive.readBytes(copied.mediaRef!)).toEqual(png)
  })

  it.each(writeModes)('registers newly generated builtin layouts in %s master relationships', async mode => {
    const opened = await openPptx(await rewritePackageRelationships(await createBlankPptx(), mode))
    for (const layout of listSlideLayouts(opened.archive).filter(layout => layout.name === 'Title and Content')) {
      opened.archive.entries.delete(layout.path)
      const masterPath = opened.archive.resolveSlideChain(opened.deck.slides[0]!.path).masterPath!
      const relsPath = relsPathFor(masterPath)
      const raw = opened.archive.readText(relsPath)!
      const rel = [...opened.archive.readRels(masterPath).values()].find(rel => resolveTarget(masterPath, rel.target) === layout.path)!
      opened.archive.entries.set(relsPath, Buffer.from(raw.replace(new RegExp(`<(?:[\\w-]+:)?Relationship\\b[^>]*Id="${rel.id}"[^>]*\\/>`), '')))
    }
    const path = ensureBuiltinLayout(opened.archive, opened.deck.size, 'titleContent')
    expect(path).toBeTruthy()
    const reopened = await openPptx(await savePptx(opened))
    expect(listSlideLayouts(reopened.archive).map(layout => layout.path)).toContain(path)
    const master = reopened.archive.resolveSlideChain(reopened.deck.slides[0]!.path).masterPath!
    expect([...reopened.archive.readRels(master).values()].filter(rel => rel.type.endsWith('/slideLayout') && resolveTarget(master, rel.target) === path)).toHaveLength(1)
  })

  it.each(writeModes)('registers newly created table styles in %s presentation relationships', async mode => {
    const opened = await openPptx(await rewritePackageRelationships(await createBlankPptx(), mode))
    const path = 'ppt/_rels/presentation.xml.rels'
    // Make this a genuinely missing part/relationship, not the reuse branch.
    opened.archive.entries.delete('ppt/tableStyles.xml')
    const raw = opened.archive.readText(path)!
    opened.archive.entries.set(path, Buffer.from(raw.replace(/<(?:[\w-]+:)?Relationship\b[^>]*Type="[^"]*\/tableStyles"[^>]*\/>/g, '')))
    ensureTableStylePart(opened, '{00000000-0000-0000-0000-000000000123}', '<a:tblStyle styleId="{00000000-0000-0000-0000-000000000123}" styleName="Synthetic"><a:wholeTbl/></a:tblStyle>')
    const reopened = await openPptx(await savePptx(opened))
    const styles = [...reopened.archive.readRels('ppt/presentation.xml').values()].filter(rel => rel.type.endsWith('/tableStyles'))
    expect(styles).toHaveLength(1)
    expect(reopened.archive.has(resolveTarget('ppt/presentation.xml', styles[0]!.target))).toBe(true)
  })

  it.each(writeModes)('registers imported source masters in %s presentation relationships', async mode => {
    const source = await openPptx(fixture)
    const bundle = copySlide(source, 0)!
    const destination = await openPptx(await rewritePackageRelationships(await createBlankPptx(), mode))
    const masterCount = [...destination.archive.readRels('ppt/presentation.xml').values()].filter(rel => rel.type.endsWith('/slideMaster')).length
    expect(pasteSlide(destination, 0, bundle, { keepSourceFormatting: true })).not.toBeNull()
    const reopened = await openPptx(await savePptx(destination))
    expect(reopened.deck.slides).toHaveLength(2)
    expect([...reopened.archive.readRels('ppt/presentation.xml').values()].filter(rel => rel.type.endsWith('/slideMaster'))).toHaveLength(masterCount + 1)
    expect(reopened.archive.resolveSlideChain(reopened.deck.slides[1]!.path).masterPath).toBeTruthy()
  })
})

describe('relationship insertion lexical boundaries', () => {
  const entry = '<Relationship Id="rId8" Type="synthetic" Target="x&amp;y"/>'
  it('preserves XML declarations, comments, unicode prefixes and closing-tag whitespace', () => {
    const original = `<?xml version="1.0"?>\n<!-- </λ:Relationships> -->\n<λ:Relationships xmlns:λ='${relNamespace}'><λ:Relationship Id='rId7' Type='synthetic' Target='old'/></λ:Relationships >\n<!-- tail -->`
    const saved = appendRelationshipsXml(original, entry)
    expect(saved).toBe(original.replace('</λ:Relationships >', entry.replace('<Relationship ', '<λ:Relationship ') + '</λ:Relationships >'))
    expect(nextRelationshipId(saved)).toBe('rId9')
  })
  it('expands an empty self-closing root without losing its surrounding bytes', () => {
    const original = `<?xml version="1.0"?>\n<ns0:Relationships xmlns:ns0="${relNamespace}" />\n`
    expect(appendRelationshipsXml(original, entry)).toBe(original.replace(' />', ` >${entry.replace('<Relationship ', '<ns0:Relationship ')}</ns0:Relationships>`))
  })
  it('rejects duplicate identifiers within one insertion', () => {
    expect(() => appendRelationshipsXml(`<Relationships xmlns="${relNamespace}"/>`, entry + entry)).toThrow()
  })
  it('rejects duplicate existing identifiers, unsupported namespaces and overflowing numeric ids', () => {
    expect(() => appendRelationshipsXml(`<Relationships xmlns="${relNamespace}">${entry}</Relationships>`, entry)).toThrow()
    expect(() => appendRelationshipsXml('<Relationships xmlns="urn:not-opc"/>', entry)).toThrow()
    expect(() => nextRelationshipId(`<Relationships xmlns="${relNamespace}"><Relationship Id="rId9007199254740991" Type="synthetic" Target="x"/></Relationships>`)).toThrow()
  })
  it.each(['"', "'"])('retargets only the intended attribute with %s quoting and keeps all other bytes', quote => {
    const original = `<?xml version="1.0"?>\n<λ:Relationships xmlns:λ="${relNamespace}" xmlns:ext="urn:synthetic"><!-- <λ:Relationship Id="rId7" Target="comment"/> --><λ:Relationship ext:Target="unchanged" Id=${quote}rId7${quote} Type="synthetic" Target = ${quote}old${quote}></λ:Relationship></λ:Relationships>\n`
    expect(replaceRelationshipTargetXml(original, 'rId7', "new&a'b")).toBe(original.replace(`Target = ${quote}old${quote}`, `Target = ${quote}new&amp;a&apos;b${quote}`))
  })
  it('rejects ambiguous or missing retargeting identifiers', () => {
    const raw = `<Relationships xmlns="${relNamespace}">${entry}${entry}</Relationships>`
    expect(() => replaceRelationshipTargetXml(raw, 'rId8', 'new')).toThrow()
    expect(() => replaceRelationshipTargetXml(raw, 'missing', 'new')).toThrow()
  })
})

describe('independent OPC regression boundaries', () => {
  const modes: RelationshipWriteMode[] = [...writeModes, 'whole-λ', 'whole-关系', 'single']
  it.each(modes)('duplicates without sharing or overwriting source notes in %s documents', async mode => {
    const authored = await openPptx(await createBlankPptx())
    expect(setSlideNotes(authored, 0, 'Synthetic retained')).toBe(true)
    const opened = await openPptx(await rewritePackageRelationships(await savePptx(authored), mode))
    const sourcePath = opened.deck.slides[0]!.path
    const originalRels = opened.archive.readBytes(relsPathFor(sourcePath))!
    const notesRel = [...opened.archive.readRels(sourcePath).values()].find(rel => rel.type.endsWith('/notesSlide'))!
    const notesPath = resolveTarget(sourcePath, notesRel.target)
    const originalNotes = opened.archive.readBytes(notesPath)!
    expect(duplicateSlide(opened, 0)).not.toBeNull()
    const duplicated = await openPptx(await savePptx(opened))
    expect([...duplicated.archive.readRels(duplicated.deck.slides[1]!.path).values()].filter(rel => rel.type.endsWith('/notesSlide'))).toHaveLength(0)
    expect(setSlideNotes(duplicated, 1, 'Synthetic changed')).toBe(true)
    const reopened = await openPptx(await savePptx(duplicated))
    expect(getSlideNotes(reopened.archive, sourcePath)).toBe('Synthetic retained')
    expect(getSlideNotes(reopened.archive, reopened.deck.slides[1]!.path)).toBe('Synthetic changed')
    expect(reopened.archive.readBytes(relsPathFor(sourcePath))).toEqual(originalRels)
    expect(reopened.archive.readBytes(notesPath)).toEqual(originalNotes)
  })

  it.each(modes)('keeps the imported layout/master/theme chain reachable in %s documents', async mode => {
    const authored = await openPptx(await createBlankPptx())
    const sourceChain = authored.archive.resolveSlideChain(authored.deck.slides[0]!.path)
    authored.archive.entries.set(sourceChain.masterPath!, Buffer.from(authored.archive.readText(sourceChain.masterPath!)!.replace('<p:cSld>', '<p:cSld name="Synthetic Source">')))
    authored.archive.entries.set(sourceChain.themePath!, Buffer.from(authored.archive.readText(sourceChain.themePath!)!.replace('C43E1C', '123456')))
    const source = await openPptx(await rewritePackageRelationships(await savePptx(authored), mode))
    const destination = await openPptx(await rewritePackageRelationships(await createBlankPptx(), mode))
    const originalChain = destination.archive.resolveSlideChain(destination.deck.slides[0]!.path)
    const originalSlide = destination.archive.readBytes(destination.deck.slides[0]!.path)!
    expect(pasteSlide(destination, 0, copySlide(source, 0)!, { keepSourceFormatting: true })).not.toBeNull()
    const reopened = await openPptx(await savePptx(destination))
    const importedChain = reopened.archive.resolveSlideChain(reopened.deck.slides[1]!.path)
    expect(importedChain.masterPath).not.toBe(originalChain.masterPath)
    const masters = [...reopened.archive.readRels('ppt/presentation.xml').values()].filter(rel => rel.type.endsWith('/slideMaster')).map(rel => resolveTarget('ppt/presentation.xml', rel.target))
    expect(masters).toHaveLength(2)
    expect(masters).toContain(importedChain.masterPath)
    expect([...reopened.archive.readRels(importedChain.masterPath!).values()].filter(rel => rel.type.endsWith('/slideLayout')).map(rel => resolveTarget(importedChain.masterPath!, rel.target))).toContain(importedChain.layoutPath)
    expect(reopened.archive.readBytes(importedChain.themePath!)).toEqual(source.archive.readBytes(sourceChain.themePath!))
    expect(reopened.archive.readBytes(reopened.deck.slides[0]!.path)).toEqual(originalSlide)
    expect(reopened.archive.resolveSlideChain(reopened.deck.slides[0]!.path)).toEqual(originalChain)
  })

  it.each(['&#55;', '&#x37;', '&#x0037;'])('identifies and retargets a numeric XML entity in an ID (%s)', entity => {
    const raw = `<Relationships xmlns="${relNamespace}"><Relationship Id='rId${entity}' Type='synthetic' Target='old'/></Relationships>`
    expect(readRelationshipsXml(raw).has('rId7')).toBe(true)
    expect(nextRelationshipId(raw)).toBe('rId8')
    expect(() => appendRelationshipsXml(raw, '<Relationship Id="rId7" Type="synthetic" Target="new"/>')).toThrow()
    expect(replaceRelationshipTargetXml(raw, 'rId7', 'new')).toBe(raw.replace("Target='old'", "Target='new'"))
  })
  it('rejects semantically duplicated inserted IDs with different XML spellings', () => {
    const entries = '<Relationship Id="rId7" Type="synthetic" Target="one"/><Relationship Id="rId&#55;" Type="synthetic" Target="two"/>'
    expect(() => appendRelationshipsXml(`<Relationships xmlns="${relNamespace}"/>`, entries)).toThrow()
  })
  it('does not double-decode an escaped ampersand before a numeric reference', () => {
    const raw = `<Relationships xmlns="${relNamespace}"><Relationship Id="rId&amp;#55;" Type="synthetic" Target="x"/></Relationships>`
    expect([...readRelationshipsXml(raw).keys()]).toEqual(['rId&#55;'])
    expect(nextRelationshipId(raw)).toBe('rId1')
    expect(() => appendRelationshipsXml(raw, '<Relationship Id="rId7" Type="synthetic" Target="new"/>')).not.toThrow()
  })

  describe.each(['before', 'after'])('unknown attributes written %s standard attributes', order => {
    const standard = 'Id="rId7" Type="synthetic" Target="old"'
    const extension = 'ext:Id="rId900" ext:Type="unknown" ext:Target="untouched" ext:TargetMode="External"'
    const entry = `<pkg:Relationship ${order === 'before' ? `${extension} ${standard}` : `${standard} ${extension}`}/>`
    const raw = `<pkg:Relationships xmlns:pkg="${relNamespace}" xmlns:ext="urn:synthetic:extension">${entry}</pkg:Relationships>`
    it('reads only unqualified standard attribute authority', () => {
      expect([...readRelationshipsXml(raw).values()]).toEqual([{ id: 'rId7', type: 'synthetic', target: 'old' }])
    })
    it('allocates the ID from the standard rather than extension attribute', () => {
      expect(nextRelationshipId(raw)).toBe('rId8')
    })
    it('rejects an insertion duplicating the standard ID', () => {
      expect(() => appendRelationshipsXml(raw, '<Relationship Id="rId7" Type="synthetic" Target="new"/>')).toThrow()
    })
    it('retargets only the requested standard target and preserves extensions byte for byte', () => {
      expect(replaceRelationshipTargetXml(raw, 'rId7', 'new')).toBe(raw.replace('Target="old"', 'Target="new"'))
    })
    it('removes by standard identity without mistaking the extension for another relationship', () => {
      expect(removeRelationshipXml(raw, 'rId7')).toBe(raw.replace(entry, ''))
    })
  })
  it('cannot override External authority with a trailing extension attribute', () => {
    const raw = `<Relationships xmlns="${relNamespace}" xmlns:ext="urn:synthetic:extension"><Relationship Id="rId7" Type="synthetic" Target="https://example.invalid/untouched" TargetMode="External" ext:TargetMode="Internal"/></Relationships>`
    expect(readRelationshipsXml(raw).get('rId7')?.targetMode).toBe('External')
  })
  it('rejects duplicate standard IDs even when extension IDs differ', () => {
    const raw = `<Relationships xmlns="${relNamespace}" xmlns:ext="urn:synthetic:extension"><Relationship Id="rId7" Type="synthetic" Target="one" ext:Id="rId8"/><Relationship Id="rId&#55;" Type="synthetic" Target="two" ext:Id="rId9"/></Relationships>`
    expect(() => readRelationshipsXml(raw)).toThrow()
  })
})
