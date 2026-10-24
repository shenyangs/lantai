import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import JSZip from 'jszip'
import { describe, expect, it } from 'vitest'
import { openPptx, savePptx, setElementFont } from '../src'

const here = dirname(fileURLToPath(import.meta.url))
const fixturePath = join(here, 'fixtures', '01_standard_business.pptx')

async function fixtureWithEmbeddedFont(): Promise<Uint8Array> {
  const zip = await JSZip.loadAsync(readFileSync(fixturePath))
  const presentation = await zip.file('ppt/presentation.xml')!.async('text')
  const relationships = await zip.file('ppt/_rels/presentation.xml.rels')!.async('text')
  const contentTypes = await zip.file('[Content_Types].xml')!.async('text')

  zip.file(
    'ppt/presentation.xml',
    presentation.replace(
      '</p:presentation>',
      '<p:embeddedFontLst><p:embeddedFont><p:font typeface="Example"/><p:regular r:id="rIdEmbeddedFont"/></p:embeddedFont></p:embeddedFontLst></p:presentation>',
    ),
  )
  zip.file(
    'ppt/_rels/presentation.xml.rels',
    relationships.replace(
      '</Relationships>',
      '<Relationship Id="rIdEmbeddedFont" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/font" Target="fonts/font1.fntdata"/></Relationships>',
    ),
  )
  zip.file(
    '[Content_Types].xml',
    contentTypes.replace(
      '</Types>',
      '<Default Extension="fntdata" ContentType="application/x-fontdata"/></Types>',
    ),
  )
  zip.file('ppt/fonts/font1.fntdata', new Uint8Array([1, 2, 3]))
  return zip.generateAsync({ type: 'uint8array' })
}

async function saveFixture(edit: boolean): Promise<JSZip> {
  const opened = await openPptx(await fixtureWithEmbeddedFont())
  if (edit) {
    const slide = opened.deck.slides.find((candidate) =>
      candidate.elements.some((element) => element.type === 'text' || element.type === 'shape'),
    )
    const element = slide?.elements.find(
      (candidate) => candidate.type === 'text' || candidate.type === 'shape',
    )
    if (!slide || !element) throw new Error('fixture has no editable text element')
    expect(setElementFont(slide, element.id, { bold: true })).toBe(true)
  }
  return JSZip.loadAsync(await savePptx(opened))
}

describe('embedded font cleanup', () => {
  it('preserves embedded fonts during an unchanged round trip', async () => {
    const zip = await saveFixture(false)
    expect(zip.file('ppt/fonts/font1.fntdata')).not.toBeNull()
    expect(await zip.file('ppt/presentation.xml')!.async('text')).toContain('embeddedFontLst')
  })

  it('removes stale embedded font subsets after an edit', async () => {
    const zip = await saveFixture(true)
    expect(zip.file('ppt/fonts/font1.fntdata')).toBeNull()
    expect(await zip.file('ppt/presentation.xml')!.async('text')).not.toContain('embeddedFontLst')
    expect(await zip.file('ppt/_rels/presentation.xml.rels')!.async('text')).not.toContain(
      '/relationships/font',
    )
    expect(await zip.file('[Content_Types].xml')!.async('text')).not.toContain(
      'Extension="fntdata"',
    )
  })
})
