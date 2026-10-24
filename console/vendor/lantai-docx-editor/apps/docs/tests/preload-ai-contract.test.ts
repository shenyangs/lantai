import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const source = readFileSync(join(process.cwd(), 'src/preload/index.ts'), 'utf8')

describe('DOCX preload AI contract', () => {
  it('exposes every method used during the first Agent render and turn', () => {
    for (const method of [
      'getAiSettings',
      'aiStream',
      'aiStreamCancel',
      'onAiStream',
      'pickAttachments',
      'readAttachment',
      'readAttachmentImage',
    ]) {
      expect(source).toContain(`${method}:`)
    }
  })
})
