import { describe, expect, it } from 'vitest'

import { StylesheetEditor } from '../src/gateway/xlsx-styles'

const STYLES = `<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font/></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf/></cellStyleXfs>
  <cellXfs count="2"><xf/><xf applyAlignment="1"><alignment horizontal="right" readingOrder="2" shrinkToFit="1" justifyLastLine="1" relativeIndent="2"/></xf></cellXfs>
</styleSheet>`

const CARRIED = ['readingOrder', 'shrinkToFit', 'justifyLastLine', 'relativeIndent']

function xfAt(xml: string, index: number): string {
  const cellXfs = /<cellXfs\b[^>]*>([\s\S]*?)<\/cellXfs>/.exec(xml)?.[1] ?? ''
  const xfs = [...cellXfs.matchAll(/<xf\b[^>]*\/>|<xf\b[^>]*>[\s\S]*?<\/xf>/g)]
  const xf = xfs[index]?.[0]
  if (!xf) throw new Error(`no xf at index ${index} in: ${cellXfs}`)
  return xf
}

describe('StylesheetEditor alignment carries', () => {
  it('keeps the unmodeled alignment attributes through an unrelated edit', () => {
    const editor = new StylesheetEditor(STYLES)
    const index = editor.resolveStyle(1, { fillColor: '#FF0000' })
    const xf = xfAt(editor.serialize(), index)
    for (const name of CARRIED) expect(xf).toContain(name)
    expect(xf).toContain('applyFill="1"')
    expect(xf).toContain('horizontal="right"')
  })

  it('keeps them when the edit changes alignment itself', () => {
    const editor = new StylesheetEditor(STYLES)
    const index = editor.resolveStyle(1, { horizontalAlignment: 'center' })
    const xf = xfAt(editor.serialize(), index)
    expect(xf).toContain('horizontal="center"')
    expect(xf).toContain('readingOrder="2"')
  })

  it('does not read a namespace-prefixed name off the alignment element', () => {
    const prefixed = STYLES.replace(
      '<alignment horizontal="right" readingOrder="2" shrinkToFit="1" justifyLastLine="1" relativeIndent="2"/>',
      '<alignment horizontal="right" x14:readingOrder="2" x14:shrinkToFit="1"/>',
    )
    const editor = new StylesheetEditor(prefixed)
    const index = editor.resolveStyle(1, { fillColor: '#FF0000' })
    const xf = xfAt(editor.serialize(), index)
    expect(xf).toContain('applyFill="1"')
    expect(xf).not.toContain('readingOrder=')
    expect(xf).not.toContain('shrinkToFit=')
  })

  it('does not let a namespaced applyAlignment decide the carry', () => {
    const prefixed = STYLES.replace(
      '<xf applyAlignment="1"><alignment horizontal="right" readingOrder="2"',
      '<xf x14:applyAlignment="1"><alignment readingOrder="2"',
    )
    const editor = new StylesheetEditor(prefixed)
    const index = editor.resolveStyle(1, { fillColor: '#FF0000' })
    const xf = xfAt(editor.serialize(), index)
    expect(xf).toContain('applyFill="1"')
    expect(xf).not.toContain('readingOrder=')
  })

  it('carries nothing when the xf holds an element that could own an alignment', () => {
    const vendor = STYLES.replace(
      '<xf applyAlignment="1"><alignment horizontal="right" readingOrder="2" shrinkToFit="1" justifyLastLine="1" relativeIndent="2"/></xf>',
      '<xf><settings xmlns="urn:vendor" applyAlignment="1"><alignment readingOrder="2"/></settings></xf>',
    )
    const editor = new StylesheetEditor(vendor)
    const index = editor.resolveStyle(1, { fillColor: '#FF0000' })
    const xf = xfAt(editor.serialize(), index)
    expect(xf).toContain('applyFill="1"')
    expect(xf).not.toContain('<alignment')
    expect(xf).not.toContain('applyAlignment')
  })

  it('carries nothing out of an mc:AlternateContent branch', () => {
    const alternate = STYLES.replace(
      '<xf applyAlignment="1"><alignment horizontal="right" readingOrder="2" shrinkToFit="1" justifyLastLine="1" relativeIndent="2"/></xf>',
      '<xf><mc:AlternateContent><mc:Choice Requires="x14">' +
        '<alignment horizontal="general" readingOrder="2"/>' +
        '</mc:Choice><mc:Fallback/></mc:AlternateContent></xf>',
    )
    const editor = new StylesheetEditor(alternate)
    const index = editor.resolveStyle(1, { fillColor: '#FF0000' })
    const xf = xfAt(editor.serialize(), index)
    expect(xf).toContain('applyFill="1"')
    expect(xf).not.toContain('readingOrder=')
  })

  it('does not create an alignment out of carried attributes alone', () => {
    const carriedOnly = STYLES.replace(
      '<xf applyAlignment="1"><alignment horizontal="right" readingOrder="2" shrinkToFit="1" justifyLastLine="1" relativeIndent="2"/></xf>',
      '<xf><alignment readingOrder="2"/></xf>',
    )
    const editor = new StylesheetEditor(carriedOnly)
    const index = editor.resolveStyle(1, { fillColor: '#FF0000' })
    const xf = xfAt(editor.serialize(), index)
    expect(xf).not.toContain('<alignment')
    expect(xf).not.toContain('applyAlignment')
  })

  it('carries them when a modeled attribute keeps the element alive anyway', () => {
    const notApplied = STYLES.replace('<xf applyAlignment="1">', '<xf>')
    const editor = new StylesheetEditor(notApplied)
    const index = editor.resolveStyle(1, { fillColor: '#FF0000' })
    const xf = xfAt(editor.serialize(), index)
    expect(xf).toContain('horizontal="right"')
    expect(xf).toContain('readingOrder="2"')
  })

  it('accepts applyAlignment spelled the other xsd:boolean way', () => {
    const spelled = STYLES.replace('applyAlignment="1"', 'applyAlignment="true"').replace(
      'horizontal="right" readingOrder="2"',
      'readingOrder="2"',
    )
    const editor = new StylesheetEditor(spelled)
    const index = editor.resolveStyle(1, { fillColor: '#FF0000' })
    expect(xfAt(editor.serialize(), index)).toContain('readingOrder="2"')
  })

  it('adds nothing when the base has none of them', () => {
    const editor = new StylesheetEditor(STYLES)
    const index = editor.resolveStyle(0, { fillColor: '#FF0000' })
    const xf = xfAt(editor.serialize(), index)
    for (const name of CARRIED) expect(xf).not.toContain(name)
  })
})
