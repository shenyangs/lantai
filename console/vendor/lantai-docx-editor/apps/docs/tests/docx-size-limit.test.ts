import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

describe('DOCX size limit parity', () => {
  it('keeps the embedded runtime aligned with the 150 MB Lantai entry limit', () => {
    const source = readFileSync(join(process.cwd(), 'src/main/docs-main.ts'), 'utf8')
    expect(source).toContain('const DOCX_MAX_MB = 150')
    expect(source).toContain('const DOCX_MAX_BYTES = DOCX_MAX_MB * 1024 * 1024')
    expect(source).not.toContain('DOCX 文件不能超过 100 MB')
  })
})
