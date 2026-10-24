import { describe, expect, it } from 'vitest'
import { parseDocx, saveDocx } from '../src'
import { buildDocx } from './helpers/build-docx'

function paragraphText(document: Awaited<ReturnType<typeof parseDocx>>): string {
  const paragraph = document.blocks[0]
  expect(paragraph?.type).toBe('paragraph')
  return paragraph?.type === 'paragraph' && paragraph.runs
    ? paragraph.runs.map((run) => run.text).join('')
    : ''
}

describe('XML entity decoding', () => {
  it('does not double-decode named entities already handled by the XML parser', async () => {
    const input = await buildDocx({
      bodyXml: '<w:p><w:r><w:t>&amp;lt;tag&amp;gt; &amp;amp;</w:t></w:r></w:p>',
    })
    const document = await parseDocx(input)
    expect(paragraphText(document)).toBe('&lt;tag&gt; &amp;')
    const originalBlocks = document.blocks.flatMap((block) =>
      block.hidden || block.docxIndex === null
        ? []
        : [{ kind: 'original' as const, docxIndex: block.docxIndex }],
    )
    expect(paragraphText(await parseDocx(await saveDocx(document, originalBlocks)))).toBe(
      '&lt;tag&gt; &amp;',
    )
  })

  it('still decodes numeric character references from parsed text', async () => {
    const input = await buildDocx({
      bodyXml: '<w:p><w:r><w:t>&amp;#x4E2D;&amp;#25991;</w:t></w:r></w:p>',
    })
    expect(paragraphText(await parseDocx(input))).toBe('中文')
  })
})
