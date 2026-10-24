/**
 * Embedded chart workbook support.
 *
 * A native chart is not fully editable in PowerPoint when its chart part only
 * contains strCache/numCache values.  PowerPoint's Edit Data command also needs
 * a chart externalData relationship to an embedded XLSX package.  Chart insert
 * and edit paths register the literal source matrix here; save materializes the
 * nested XLSX before the outer PPTX zip is written.
 */
import JSZip from 'jszip'
import { XMLParser } from 'fast-xml-parser'
import { asXmlNode, escapeXmlAttr, escapeXmlText, xmlArray } from './xml-utils'
import { appendRelationshipsXml, nextRelationshipId, readRelationshipsXml, relsPathFor, resolveTarget, type PackageArchive } from './zip'

export interface ChartWorkbookData {
  categories: Array<string | number>
  series: Array<{ name: string; values: number[] }>
}

const PACKAGE_REL_TYPE =
  'http://schemas.openxmlformats.org/officeDocument/2006/relationships/package'
const XLSX_CONTENT_TYPE =
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
const pending = new WeakMap<PackageArchive, Map<string, ChartWorkbookData>>()

export interface ChartWorkbookValueUpdate {
  sheetName: string
  ref: string
  before: number
  value: number
}
const pendingValues = new WeakMap<PackageArchive, Map<string, Map<string, ChartWorkbookValueUpdate>>>()

/** Save-time chart data is document state, even before its XLSX bytes exist. */
export interface ChartWorkbookSnapshot {
  matrices: Map<string, ChartWorkbookData>
  values: Map<string, Map<string, ChartWorkbookValueUpdate>>
}

export function captureChartWorkbooks(archive: PackageArchive): ChartWorkbookSnapshot {
  return structuredClone({
    matrices: pending.get(archive) ?? new Map<string, ChartWorkbookData>(),
    values: pendingValues.get(archive) ?? new Map<string, Map<string, ChartWorkbookValueUpdate>>(),
  })
}

/** Restore queues together with archive entries; never retain live history references. */
export function restoreChartWorkbooks(archive: PackageArchive, snapshot?: ChartWorkbookSnapshot): void {
  const fresh = snapshot ? structuredClone(snapshot) : undefined
  if (fresh?.matrices.size) pending.set(archive, fresh.matrices)
  else pending.delete(archive)
  if (fresh?.values.size) pendingValues.set(archive, fresh.values)
  else pendingValues.delete(archive)
}

/** A single-chart edit must not silently change another chart's data source. */
export function chartHasSharedWorkbook(archive: PackageArchive, chartPath: string): boolean {
  const paths = new Set([...archive.readRels(chartPath).values()]
    .filter((rel) => rel.type === PACKAGE_REL_TYPE && rel.targetMode !== 'External' && /\.xlsx$/i.test(rel.target))
    .map((rel) => resolveTarget(chartPath, rel.target)))
  if (!paths.size) return false
  // OPC parts need not live under ppt/charts. Follow relationship owners rather
  // than guessing chart filenames: imported decks may use nested/custom paths.
  for (const relationshipPath of archive.entries.keys()) {
    const owner = /^(?:(.*)\/)?_rels\/([^/]+)\.rels$/.exec(relationshipPath)
    if (!owner) continue
    const otherPath = `${owner[1] ? owner[1] + '/' : ''}${owner[2]}`
    if (otherPath === chartPath || !archive.has(otherPath)) continue
    for (const rel of archive.readRels(otherPath).values()) {
      if (rel.type === PACKAGE_REL_TYPE && rel.targetMode !== 'External'
          && paths.has(resolveTarget(otherPath, rel.target))) return true
    }
  }
  return false
}

/** Queue literal-cell updates without recreating an imported workbook. */
export function queueChartWorkbookValues(
  archive: PackageArchive, chartPath: string, chartXml: string,
  updates: ChartWorkbookValueUpdate[],
): boolean {
  const rid = /<c:externalData\b[^>]*\br:id="([^"]+)"/.exec(chartXml)?.[1]
  const rel = rid ? archive.readRels(chartPath).get(rid) : undefined
  if (!rel || rel.type !== PACKAGE_REL_TYPE || rel.targetMode === 'External' || !/\.xlsx$/i.test(rel.target)) return false
  const path = resolveTarget(chartPath, rel.target)
  if (!archive.has(path) && !pending.get(archive)?.has(path)) return false
  const scheduled = pendingValues.get(archive)
    ?? new Map<string, Map<string, ChartWorkbookValueUpdate>>()
  const merged = new Map<string, ChartWorkbookValueUpdate>(scheduled.get(path) ?? [])
  for (const update of updates) {
    const key = JSON.stringify([update.sheetName, update.ref])
    const previous = merged.get(key)
    if (previous && previous.value !== update.before) return false
    merged.set(key, { ...update, before: previous?.before ?? update.before })
  }
  scheduled.set(path, merged)
  pendingValues.set(archive, scheduled)
  return true
}

function ensureXlsxContentType(archive: PackageArchive): void {
  const path = '[Content_Types].xml'
  const xml = archive.readText(path)
  if (!xml || /<Default\b[^>]*\bExtension="xlsx"/.test(xml)) return
  const entry = `<Default Extension="xlsx" ContentType="${XLSX_CONTENT_TYPE}"/>`
  const firstOverride = xml.indexOf('<Override')
  const next =
    firstOverride >= 0
      ? xml.slice(0, firstOverride) + entry + xml.slice(firstOverride)
      : xml.replace('</Types>', `${entry}</Types>`)
  archive.entries.set(path, Buffer.from(next, 'utf8'))
}

function nextWorkbookPath(archive: PackageArchive): string {
  const scheduled = pending.get(archive)
  let max = 0
  const inspect = (path: string) => {
    const match = /^ppt\/embeddings\/Microsoft_Excel_Worksheet(\d+)\.xlsx$/.exec(path)
    if (match) max = Math.max(max, Number(match[1]))
  }
  for (const path of archive.entries.keys()) inspect(path)
  for (const path of scheduled?.keys() ?? []) inspect(path)
  return `ppt/embeddings/Microsoft_Excel_Worksheet${max + 1}.xlsx`
}

/**
 * Add or reuse the chart's workbook relationship and return chart XML carrying
 * the matching externalData reference. Existing imported chart workbooks keep
 * their original package path; their bytes are replaced only after an edit.
 */
export function attachChartWorkbook(
  archive: PackageArchive,
  chartPath: string,
  chartXml: string,
  data: ChartWorkbookData,
): string {
  const relsPath = relsPathFor(chartPath)
  let relsXml =
    archive.readText(relsPath) ??
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>'
  const previousXml = archive.readText(chartPath) ?? ''
  const previousRid = /<c:externalData\b[^>]*\br:id="([^"]+)"/.exec(previousXml)?.[1]
  const relationships = [...readRelationshipsXml(relsXml).values()]
  const reusable =
    relationships.find((rel) => rel.id === previousRid && rel.type === PACKAGE_REL_TYPE && rel.targetMode !== 'External') ??
    relationships.find((rel) => rel.type === PACKAGE_REL_TYPE && rel.targetMode !== 'External' && /\.xlsx$/i.test(rel.target))

  let rid: string
  let workbookPath: string
  if (reusable?.id && reusable.target) {
    rid = reusable.id
    workbookPath = resolveTarget(chartPath, reusable.target)
  } else {
    rid = nextRelationshipId(relsXml)
    workbookPath = nextWorkbookPath(archive)
    const filename = workbookPath.slice(workbookPath.lastIndexOf('/') + 1)
    const rel = `<Relationship Id="${rid}" Type="${PACKAGE_REL_TYPE}" Target="../embeddings/${filename}"/>`
    relsXml = appendRelationshipsXml(relsXml, rel)
    archive.entries.set(relsPath, Buffer.from(relsXml, 'utf8'))
  }

  ensureXlsxContentType(archive)
  const scheduled = pending.get(archive) ?? new Map<string, ChartWorkbookData>()
  // The UI owns its patch arrays. Saving may happen after it mutates/reuses them;
  // retain the accepted matrix, not a live reference that can diverge from OOXML.
  scheduled.set(workbookPath, {
    categories: [...data.categories],
    series: data.series.map((series) => ({ name: series.name, values: [...series.values] })),
  })
  pending.set(archive, scheduled)
  // An explicit structural/type edit replaces the source matrix; older literal
  // updates must not be applied to that new matrix.
  pendingValues.get(archive)?.delete(workbookPath)

  const withoutExternalData = chartXml.replace(/<c:externalData\b[\s\S]*?<\/c:externalData>/g, '')
  const externalData = `<c:externalData r:id="${escapeXmlAttr(rid)}"><c:autoUpdate val="0"/></c:externalData>`
  return withoutExternalData.replace('</c:chartSpace>', `${externalData}</c:chartSpace>`)
}

function columnName(oneBased: number): string {
  let value = oneBased
  let out = ''
  while (value > 0) {
    value--
    out = String.fromCharCode(65 + (value % 26)) + out
    value = Math.floor(value / 26)
  }
  return out
}

function stringCell(ref: string, value: string): string {
  return `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${escapeXmlText(value)}</t></is></c>`
}

function numberCell(ref: string, value: number): string {
  return Number.isFinite(value) ? `<c r="${ref}"><v>${value}</v></c>` : ''
}

function worksheetXml(data: ChartWorkbookData): string {
  const rows = Math.max(data.categories.length, ...data.series.map((series) => series.values.length), 0)
  const lastColumn = columnName(data.series.length + 1)
  const header =
    stringCell('A1', 'Category') +
    data.series.map((series, index) => stringCell(`${columnName(index + 2)}1`, series.name)).join('')
  const body = Array.from({ length: rows }, (_, index) => {
    const rowNumber = index + 2
    const category = data.categories[index]
    const categoryCell =
      typeof category === 'number'
        ? numberCell(`A${rowNumber}`, category)
        : category == null
          ? ''
          : stringCell(`A${rowNumber}`, category)
    const values = data.series
      .map((series, seriesIndex) => numberCell(`${columnName(seriesIndex + 2)}${rowNumber}`, series.values[index]!))
      .join('')
    return `<row r="${rowNumber}">${categoryCell}${values}</row>`
  }).join('')
  const lastRow = Math.max(1, rows + 1)
  return (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
    `<dimension ref="A1:${lastColumn}${lastRow}"/><sheetViews><sheetView workbookViewId="0"/></sheetViews>` +
    `<sheetData><row r="1">${header}</row>${body}</sheetData>` +
    '</worksheet>'
  )
}

async function buildWorkbook(data: ChartWorkbookData): Promise<Uint8Array> {
  const zip = new JSZip()
  zip.file(
    '[Content_Types].xml',
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
      '<Default Extension="xml" ContentType="application/xml"/>' +
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
      '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
      '</Types>',
  )
  zip.file(
    '_rels/.rels',
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
      '</Relationships>',
  )
  zip.file(
    'xl/workbook.xml',
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
      '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets><calcPr fullCalcOnLoad="1"/></workbook>',
  )
  zip.file(
    'xl/_rels/workbook.xml.rels',
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' +
      '</Relationships>',
  )
  zip.file('xl/worksheets/sheet1.xml', worksheetXml(data))
  return zip.generateAsync({ type: 'uint8array', compression: 'DEFLATE', compressionOptions: { level: 6 } })
}

const workbookParser = new XMLParser({ ignoreAttributes: false, attributeNamePrefix: '@_', parseTagValue: false })

async function patchWorkbookValues(bytes: Uint8Array, updates: Iterable<ChartWorkbookValueUpdate>): Promise<Uint8Array> {
  const zip = await JSZip.loadAsync(bytes)
  const workbook = zip.file('xl/workbook.xml')
  const rels = zip.file('xl/_rels/workbook.xml.rels')
  if (!workbook || !rels) throw new Error('chart workbook is missing its sheet relationship map')
  const doc = asXmlNode(workbookParser.parse(await workbook.async('text')))
  const sheets = xmlArray(asXmlNode(asXmlNode(doc.workbook).sheets).sheet)
  const relationships = readRelationshipsXml(await rels.async('text'))
  const changed = new Map<string, string>()
  for (const update of updates) {
    const sheet = sheets.find((entry) => entry['@_name'] === update.sheetName)
    const rel = relationships.get(String(sheet?.['@_r:id'] ?? ''))
    if (!rel?.target || rel.targetMode === 'External' || !rel.type.endsWith('/worksheet')) {
      throw new Error('chart workbook value references an unavailable sheet')
    }
    const path = resolveTarget('xl/workbook.xml', rel.target)
    const part = zip.file(path)
    if (!part) throw new Error('chart workbook worksheet is missing')
    let xml = changed.get(path) ?? await part.async('text')
    let matches = 0
    xml = xml.replace(/<c\b([^>]*)>([\s\S]*?)<\/c>/g, (whole, attrs: string, body: string) => {
      if (/(?:^|\s)r="([^"]+)"/.exec(attrs)?.[1] !== update.ref) return whole
      matches++
      const value = /<v>([^<]*)<\/v>/.exec(body)
      const cellType = /(?:^|\s)t="([^"]+)"/.exec(attrs)?.[1]
      if (/<f\b/.test(body) || (cellType != null && cellType !== 'n') || !value
          || !value[1]!.trim() || Number(value[1]) !== update.before || !Number.isFinite(update.value)) {
        throw new Error('chart workbook literal cell disagrees with its cache or contains a formula')
      }
      return whole.replace(value[0], `<v>${update.value}</v>`)
    })
    if (matches !== 1) throw new Error('chart workbook value references a missing or duplicate cell')
    changed.set(path, xml)
  }
  for (const [path, xml] of changed) zip.file(path, xml)
  return zip.generateAsync({ type: 'uint8array', compression: 'DEFLATE', compressionOptions: { level: 6 } })
}

/** Materialize new matrices and lossless literal updates atomically before saving. */
export async function materializeChartWorkbooks(archive: PackageArchive): Promise<void> {
  const scheduled = pending.get(archive)
  const valueUpdates = pendingValues.get(archive)
  const materialized = new Map<string, Uint8Array>()
  for (const [path, data] of scheduled ?? []) materialized.set(path, await buildWorkbook(data))
  for (const [path, updates] of valueUpdates ?? []) {
    const bytes = materialized.get(path) ?? archive.readBytes(path)
    if (!bytes) throw new Error('chart workbook disappeared before save')
    materialized.set(path, await patchWorkbookValues(bytes, updates.values()))
  }
  for (const [path, bytes] of materialized) archive.entries.set(path, bytes)
  scheduled?.clear()
  valueUpdates?.clear()
}
