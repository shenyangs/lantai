import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

describe('DOCX window ownership', () => {
  it('keeps the document editor independent from the Lantai main-window z-order', () => {
    const source = readFileSync(
      resolve(process.cwd(), 'src/main/docs-main.ts'),
      'utf8',
    )
    const createWindowSource = source.slice(
      source.indexOf('export function createDocsWindow'),
      source.indexOf('/** tab-mode equivalent of createDocsWindow'),
    )

    expect(createWindowSource).not.toContain(
      'parent: docsShellWindow && !docsShellWindow.isDestroyed()',
    )
  })
})
