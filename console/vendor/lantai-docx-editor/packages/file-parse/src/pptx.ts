import JSZip from 'jszip'
import { XMLParser } from 'fast-xml-parser'

// Text fidelity: no trim (xml:space="preserve" runs carry the spaces between words),
// no numeric coercion of tag values (otherwise <a:t>02139</a:t> becomes a number and loses characters).
// preserveOrder keeps <a:br> and <a:fld> in sequence with the <a:r> runs around them; grouped by
// tag name they lose that position, and a deck's soft breaks and field text land in the wrong place.
const parser = new XMLParser({
  ignoreAttributes: true,
  trimValues: false,
  parseTagValue: false,
  preserveOrder: true,
})

const manifestParser = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: '@_',
  trimValues: false,
  parseTagValue: false,
  attributeValueProcessor: (_name, value) => value.trim(),
})

function asArray<T>(value: T | T[] | undefined): T[] {
  if (value === undefined || value === null) return []
  return Array.isArray(value) ? value : [value]
}

function slideNumber(path: string): number {
  const m = /slide(\d+)\.xml$/.exec(path)
  return m ? Number(m[1]) : 0
}

async function presentationSlideEntries(zip: JSZip): Promise<(string | null)[] | null> {
  const presentationXml = await zipText(zip, 'ppt/presentation.xml')
  if (presentationXml === undefined) return null
  const presentation = manifestParser.parse(presentationXml) as {
    'p:presentation'?: {
      'p:sldIdLst'?: { 'p:sldId'?: Record<string, string> | Record<string, string>[] }
    }
  }
  const slideIds = asArray(presentation['p:presentation']?.['p:sldIdLst']?.['p:sldId'])

  const relationships = new Map<string, { target: string; type: string; external: boolean }>()
  const relationshipsXml = await zipText(zip, 'ppt/_rels/presentation.xml.rels')
  if (relationshipsXml) {
    const document = manifestParser.parse(relationshipsXml) as {
      Relationships?: { Relationship?: Record<string, string> | Record<string, string>[] }
    }
    for (const relationship of asArray(document.Relationships?.Relationship)) {
      const id = String(relationship['@_Id'] ?? '')
      if (!id) continue
      relationships.set(id, {
        target: String(relationship['@_Target'] ?? ''),
        type: String(relationship['@_Type'] ?? ''),
        external: String(relationship['@_TargetMode'] ?? '').toLowerCase() === 'external',
      })
    }
  }

  return slideIds.map((slideId) => {
    const relationship = relationships.get(slideId['@_r:id'] ?? '')
    return relationship &&
      !relationship.external &&
      relationship.target &&
      relationship.type.endsWith('/slide')
      ? resolveTarget('ppt/presentation.xml', relationship.target)
      : null
  })
}

function legacySlidePaths(zip: JSZip): string[] {
  return Object.keys(zip.files)
    .filter((path) => /^ppt\/slides\/slide\d+\.xml$/.test(path))
    .sort((left, right) => slideNumber(left) - slideNumber(right))
}

async function zipText(zip: JSZip, path: string): Promise<string | undefined> {
  const file = zip.files[path]
  return file ? file.async('text') : undefined
}

function resolveTarget(basePart: string, target: string): string {
  if (target.startsWith('/')) return target.slice(1)
  const parts = basePart.slice(0, basePart.lastIndexOf('/')).split('/').filter(Boolean)
  // Some Windows producers emit backslash separators; OPC uses forward slashes.
  // Clamp '..' at the package root so hostile or malformed targets cannot escape it.
  for (const segment of target.replace(/\\/g, '/').split('/')) {
    if (segment === '.' || segment === '') continue
    if (segment === '..') {
      if (parts.length > 0) parts.pop()
    } else parts.push(segment)
  }
  return parts.join('/')
}

/**
 * One paragraph's text in document order. Only #text directly under a:t counts: untrimmed, the
 * whitespace laying out any other element is a value too. <a:br> is a soft line break, <a:tab>
 * is a tab stop between runs, and <a:fld> (slide number, date) contributes its own a:t where it sits.
 */
function collectText(nodes: readonly unknown[], out: string[], isText = false): void {
  for (const node of nodes) {
    if (node == null || typeof node !== 'object') continue
    for (const [key, value] of Object.entries(node)) {
      if (key === '#text') {
        if (isText) out.push(String(value))
      } else if (key === 'a:br') {
        out.push('\n')
      } else if (key === 'a:tab') {
        out.push('\t')
      } else if (Array.isArray(value)) {
        collectText(value, out, key === 'a:t')
      }
    }
  }
}

/** walk the slide tree; each a:p paragraph becomes one output entry (a:br splits it further) */
function collectParagraphs(nodes: readonly unknown[], out: string[]): void {
  for (const node of nodes) {
    if (node == null || typeof node !== 'object') continue
    for (const [key, value] of Object.entries(node)) {
      if (!Array.isArray(value)) continue
      if (key === 'a:p') {
        const texts: string[] = []
        collectText(value, texts)
        const line = texts.join('')
        if (line.trim()) out.push(line)
      } else {
        collectParagraphs(value, out)
      }
    }
  }
}

/** extract slide text from a pptx: one "## Slide N" section per slide, a line per paragraph */
export async function pptxToText(bytes: Uint8Array): Promise<string> {
  const zip = await JSZip.loadAsync(bytes)
  const slideEntries = await presentationSlideEntries(zip)
  if (slideEntries) {
    const sections: string[] = []
    for (const [index, path] of slideEntries.entries()) {
      if (path === null) continue
      const xml = await zipText(zip, path)
      if (!xml) continue
      const paragraphs: string[] = []
      collectParagraphs(parser.parse(xml), paragraphs)
      sections.push([`## Slide ${index + 1}`, ...paragraphs].join('\n'))
    }
    return sections.join('\n\n')
  }
  const sections: string[] = []
  for (const path of legacySlidePaths(zip)) {
    const xml = await zipText(zip, path)
    if (!xml) continue
    const paragraphs: string[] = []
    collectParagraphs(parser.parse(xml), paragraphs)
    sections.push([`## Slide ${slideNumber(path)}`, ...paragraphs].join('\n'))
  }
  return sections.join('\n\n')
}
