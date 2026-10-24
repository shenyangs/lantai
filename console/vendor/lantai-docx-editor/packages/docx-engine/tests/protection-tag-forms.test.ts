import { describe, expect, it } from 'vitest'
import { parseDocx } from '../src'
import { buildDocx } from './helpers/build-docx'

const settingsContentType =
  'application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml'

async function parseSettings(documentProtection: string) {
  return parseDocx(
    await buildDocx({
      bodyXml: '<w:p><w:r><w:t>Protected</w:t></w:r></w:p>',
      extraParts: [
        {
          path: 'word/settings.xml',
          contentType: settingsContentType,
          xml: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">${documentProtection}</w:settings>`,
        },
      ],
    }),
  )
}

describe('document protection tag forms', () => {
  it('parses a paired documentProtection tag', async () => {
    const document = await parseSettings(
      '<w:documentProtection w:edit="readOnly" w:enforcement="true"></w:documentProtection>',
    )
    expect(document.protection).toMatchObject({
      edit: 'readOnly',
      enforced: true,
    })
  })

  it('treats enforcement="on" as enabled', async () => {
    const document = await parseSettings(
      '<w:documentProtection w:edit="comments" w:enforcement="on"/>',
    )
    expect(document.protection).toMatchObject({
      edit: 'comments',
      enforced: true,
    })
  })
})
