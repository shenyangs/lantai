/**
 * pptx package management — open the zip, archive the original by SHA-256, and read
 * parts and .rels.
 *
 * Byte fidelity: PackageArchive holds the original bytes of every entry; on save,
 * unmodified entries are written back byte-for-byte (handled by the patch layer).
 * Relationship edits use surgical insertion; unrelated XML bytes stay intact.
 */
import JSZip from 'jszip'
import { createHash } from 'node:crypto'
import { XMLParser } from 'fast-xml-parser'
import type { SlideSize } from './types'
import { asXmlNode, escapeXmlAttr, xmlArray } from './xml-utils'

/** XML attribute references are decoded once, including decimal/hex references.
 * The dependency's default XML mode leaves numeric references undecoded. Doing
 * a second numeric pass after its ampersand pass would incorrectly double-decode
 * `&amp;#55;`, so relationships use a single XML-only decoding pass instead.
 */
function decodeXmlAttribute(value: string): string {
  const named: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" }
  return value.replace(/&(amp|lt|gt|quot|apos|#(?:[0-9]+|x[0-9a-fA-F]+));/g, (_all, entity: string) => {
    if (entity[0] !== '#') return named[entity]!
    const hex = entity[1] === 'x'
    const code = Number.parseInt(entity.slice(hex ? 2 : 1), hex ? 16 : 10)
    if (!(code === 9 || code === 10 || code === 13 || (code >= 0x20 && code <= 0xd7ff)
      || (code >= 0xe000 && code <= 0xfffd) || (code >= 0x10000 && code <= 0x10ffff))) {
      throw new Error('OPC relationship attribute contains an invalid XML character reference')
    }
    return String.fromCodePoint(code)
  })
}

const relsParser = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: '@_',
  // OPC .rels may bind the package namespace to a prefix (e.g. ElementTree's
  // ns0). Reading must not depend on the author choosing the default namespace;
  // the original XML bytes remain in entries and are unchanged on a no-op save.
  // Normalize element names only. Namespace-qualified extension attributes
  // must never alias or override the unqualified OPC Id/Type/Target authority.
  removeNSPrefix: false,
  transformTagName: name => name.slice(name.lastIndexOf(':') + 1),
  processEntities: false,
  parseAttributeValue: false,
  trimValues: false,
  attributeValueProcessor: (_name, value) => decodeXmlAttribute(value),
  isArray: (name) => name === 'Relationship' || name === 'sldId' || name === 'Override',
})

export interface Relationship {
  id: string
  type: string
  target: string
  targetMode?: string
}

const PACKAGE_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'

/** Parse OPC relationships without depending on the author's namespace prefix. */
export function readRelationshipsXml(xml: string): Map<string, Relationship> {
  const rels = new Map<string, Relationship>()
  const doc = asXmlNode(relsParser.parse(xml))
  for (const r of xmlArray(asXmlNode(doc.Relationships).Relationship)) {
    const id = String(r['@_Id'] ?? '')
    if (!id || rels.has(id)) throw new Error('OPC relationship id is missing or duplicated')
    rels.set(id, {
      id, type: String(r['@_Type'] ?? ''), target: String(r['@_Target'] ?? ''),
      ...(r['@_TargetMode'] != null ? { targetMode: String(r['@_TargetMode']) } : {}),
    })
  }
  return rels
}

export function nextRelationshipId(xml: string): string {
  let max = 0
  for (const id of readRelationshipsXml(xml).keys()) {
    const match = /^rId(\d+)$/.exec(id)
    if (!match) continue
    const value = Number(match[1])
    if (!Number.isSafeInteger(value) || value >= Number.MAX_SAFE_INTEGER) {
      throw new Error('OPC relationship id exceeds its safe numeric range')
    }
    max = Math.max(max, value)
  }
  return `rId${max + 1}`
}

/**
 * Append generated Relationship fragments in the existing root's namespace.
 * This is not a whole-document normalization: prefixes, whitespace, comments
 * and every pre-existing entry remain byte-identical. Unsupported/malformed
 * roots fail explicitly rather than returning a silently unchanged document.
 */
export function appendRelationshipsXml(xml: string, entries: string): string {
  const lexical = xml.replace(/<!--[\s\S]*?-->|<\?[\s\S]*?\?>/g,
    match => ' '.repeat(match.length))
  const root = /^\s*<([^\s/>]+)((?:"[^"]*"|'[^']*'|[^'">])*?)(\/?)>/.exec(lexical)
  if (!root) throw new Error('OPC relationships root is missing')
  const qname = root[1]!
  const names = qname.split(':')
  if (names.length > 2 || names.at(-1) !== 'Relationships') {
    throw new Error('OPC relationships root has an unsupported name')
  }
  const prefix = names.length === 2 ? `${names[0]}:` : ''
  const declaration = prefix ? `xmlns:${names[0]}` : 'xmlns'
  const rootParser = new XMLParser({ ignoreAttributes: false, attributeNamePrefix: '@_', parseAttributeValue: false,
    processEntities: false, attributeValueProcessor: (_name, value) => decodeXmlAttribute(value) })
  const document = asXmlNode(rootParser.parse(xml))
  if (asXmlNode(document[qname])[`@_${declaration}`] !== PACKAGE_REL_NS) {
    throw new Error('OPC relationships root has an unsupported namespace')
  }
  const qualified = entries.replace(/(<\/?)Relationship(?=[\s/>])/g, `$1${prefix}Relationship`)
  const existing = readRelationshipsXml(xml)
  const insertedXml = `<Relationships xmlns="${PACKAGE_REL_NS}">${entries}</Relationships>`
  const inserted = readRelationshipsXml(insertedXml)
  const insertedNodes = xmlArray(asXmlNode(asXmlNode(relsParser.parse(insertedXml)).Relationships).Relationship)
  if (!inserted.size || inserted.size !== insertedNodes.length || [...inserted.keys()].some(id => !id || existing.has(id))) {
    throw new Error('OPC relationship insertion is empty or duplicates an existing id')
  }
  const start = lexical.indexOf('<', root.index)
  const end = root.index + root[0].length
  if (root[3] === '/') {
    const opening = xml.slice(start, end).replace(/\/>$/, '>')
    return xml.slice(0, start) + opening + qualified + `</${qname}>` + xml.slice(end)
  }
  const close = new RegExp(`</${qname.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*>`, 'g')
  const matches = [...lexical.matchAll(close)]
  if (matches.length !== 1) throw new Error('OPC relationships closing tag is missing or ambiguous')
  const at = matches[0]!.index!
  return xml.slice(0, at) + qualified + xml.slice(at)
}

/** Retarget one existing relationship without normalizing its lexical form. */
export function replaceRelationshipTargetXml(xml: string, id: string, target: string): string {
  const lexical = xml.replace(/<!--[\s\S]*?-->|<\?[\s\S]*?\?>/g,
    match => ' '.repeat(match.length))
  const tags = /<(?:[^\s/:<>]+:)?Relationship(?=[\s/>])(?:"[^"]*"|'[^']*'|[^'"<>])*>/g
  const matches = [...lexical.matchAll(tags)].filter(match => {
    const parsed = xmlArray(asXmlNode(relsParser.parse(match[0])).Relationship)[0]
    return parsed != null && String(parsed['@_Id'] ?? '') === id
  })
  if (matches.length !== 1) throw new Error('OPC relationship target is missing or ambiguous')
  const match = matches[0]!
  const at = match.index!
  const original = xml.slice(at, at + match[0].length)
  const attribute = /(?:^|\s)Target\s*=\s*(["'])([\s\S]*?)\1/.exec(original)
  if (!attribute) throw new Error('OPC relationship Target attribute is missing')
  const valueAt = attribute.index + attribute[0].indexOf(attribute[1]!) + 1
  const replacement = original.slice(0, valueAt) + escapeXmlAttr(target).replace(/'/g, '&apos;')
    + original.slice(valueAt + attribute[2]!.length)
  return xml.slice(0, at) + replacement + xml.slice(at + original.length)
}

/** Remove one relationship by its decoded ID; preserve every unrelated byte. */
export function removeRelationshipXml(xml: string, id: string): string {
  const lexical = xml.replace(/<!--[\s\S]*?-->|<\?[\s\S]*?\?>/g,
    match => ' '.repeat(match.length))
  const tags = /<((?:[^\s/:<>]+:)?Relationship)(?=[\s/>])(?:"[^"]*"|'[^']*'|[^'"<>])*>/g
  const matches = [...lexical.matchAll(tags)].filter(match => {
    const parsed = xmlArray(asXmlNode(relsParser.parse(match[0])).Relationship)[0]
    return parsed != null && String(parsed['@_Id'] ?? '') === id
  })
  if (matches.length !== 1) throw new Error('OPC relationship removal target is missing or ambiguous')
  const match = matches[0]!
  const at = match.index!
  let end = at + match[0].length
  if (!match[0].endsWith('/>')) {
    const escapedName = match[1]!.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const closing = new RegExp(`^\\s*</${escapedName}\\s*>`).exec(lexical.slice(end))
    if (!closing) throw new Error('OPC relationship removal target is not empty or lacks its closing tag')
    end += closing[0].length
  }
  return xml.slice(0, at) + xml.slice(end)
}

export class PackageArchive {
  private constructor(
    private readonly zip: JSZip,
    /** Original bytes of every entry, keyed by path inside the zip */
    readonly entries: Map<string, Uint8Array>,
    readonly originalHash: string,
  ) {}

  static async open(bytes: Uint8Array): Promise<PackageArchive> {
    const originalHash = createHash('sha256').update(bytes).digest('hex')
    const zip = await JSZip.loadAsync(bytes)
    const entries = new Map<string, Uint8Array>()
    const names = Object.keys(zip.files)
    for (const name of names) {
      const file = zip.files[name]
      if (file.dir) continue
      entries.set(name, await file.async('uint8array'))
    }
    return new PackageArchive(zip, entries, originalHash)
  }

  has(path: string): boolean {
    return this.entries.has(path)
  }

  /** Read a part as a UTF-8 string (for XML parts). */
  readText(path: string): string | null {
    const bytes = this.entries.get(path)
    if (!bytes) return null
    return Buffer.from(bytes).toString('utf8')
  }

  readBytes(path: string): Uint8Array | null {
    return this.entries.get(path) ?? null
  }

  /**
   * Read a part's relationships file. partPath e.g. 'ppt/slides/slide1.xml' →
   * 'ppt/slides/_rels/slide1.xml.rels'.
   */
  readRels(partPath: string): Map<string, Relationship> {
    const relsPath = relsPathFor(partPath)
    const xml = this.readText(relsPath)
    return xml ? readRelationshipsXml(xml) : new Map<string, Relationship>()
  }

  /**
   * Read the presentation's slide size and the slide part paths in order.
   */
  readPresentation(): { size: SlideSize; slidePaths: string[] } {
    const presXml = this.readText('ppt/presentation.xml')
    if (!presXml) throw new Error('pptx: missing ppt/presentation.xml')

    const parser = new XMLParser({
      ignoreAttributes: false,
      attributeNamePrefix: '@_',
      isArray: (name) => name === 'p:sldId',
    })
    const pres = asXmlNode(parser.parse(presXml))
    const rootRaw = pres['p:presentation'] ?? pres.presentation
    if (!rootRaw) throw new Error('pptx: malformed presentation.xml')
    const root = asXmlNode(rootRaw)

    // Slide size
    const szRaw = root['p:sldSz'] ?? root.sldSz
    const sz = szRaw ? asXmlNode(szRaw) : null
    const slideDimension = (raw: unknown, fallback: number): number => {
      const parsed = parseInt(String(raw), 10)
      return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback
    }
    const size: SlideSize = {
      cx: slideDimension(sz?.['@_cx'], 9144000),
      cy: slideDimension(sz?.['@_cy'], 6858000),
    }

    // Slide order: presentation.xml.rels maps r:id to slide parts
    const rels = this.readRels('ppt/presentation.xml')
    const sldIdLst = asXmlNode(root['p:sldIdLst'] ?? root.sldIdLst)
    const slidePaths: string[] = []
    for (const id of xmlArray(sldIdLst['p:sldId'])) {
      const rId = id['@_r:id'] ?? id['@_id']
      if (!rId) continue
      const rel = rels.get(String(rId))
      if (!rel) continue
      slidePaths.push(resolveTarget('ppt/presentation.xml', rel.target))
    }
    return { size, slidePaths }
  }

  /** Resolve a slide's layout / master part paths (via the rels chain). */
  resolveSlideChain(slidePath: string): {
    layoutPath?: string
    masterPath?: string
    themePath?: string
  } {
    const slideRels = this.readRels(slidePath)
    let layoutPath: string | undefined
    for (const rel of slideRels.values()) {
      if (rel.type.endsWith('/slideLayout')) {
        layoutPath = resolveTarget(slidePath, rel.target)
        break
      }
    }
    let masterPath: string | undefined
    let themePath: string | undefined
    if (layoutPath) {
      const layoutRels = this.readRels(layoutPath)
      for (const rel of layoutRels.values()) {
        if (rel.type.endsWith('/slideMaster')) {
          masterPath = resolveTarget(layoutPath, rel.target)
          break
        }
      }
    }
    if (masterPath) {
      const masterRels = this.readRels(masterPath)
      for (const rel of masterRels.values()) {
        if (rel.type.endsWith('/theme')) {
          themePath = resolveTarget(masterPath, rel.target)
          break
        }
      }
    }
    return { layoutPath, masterPath, themePath }
  }
}

/** 'ppt/slides/slide1.xml' → 'ppt/slides/_rels/slide1.xml.rels' */
export function relsPathFor(partPath: string): string {
  const idx = partPath.lastIndexOf('/')
  const dir = idx >= 0 ? partPath.slice(0, idx) : ''
  const file = idx >= 0 ? partPath.slice(idx + 1) : partPath
  return `${dir ? dir + '/' : ''}_rels/${file}.rels`
}

/**
 * Resolve a relative target into an absolute path inside the zip.
 * basePart is the referencing part's path (its directory is the base); target may be
 * something like '../slideLayouts/slideLayout1.xml'.
 */
export function resolveTarget(basePart: string, target: string): string {
  if (target.startsWith('/')) return target.slice(1)
  const baseDir = basePart.slice(0, basePart.lastIndexOf('/'))
  const parts = baseDir.split('/').filter(Boolean)
  for (const seg of target.split('/')) {
    if (seg === '.' || seg === '') continue
    if (seg === '..') parts.pop()
    else parts.push(seg)
  }
  return parts.join('/')
}
