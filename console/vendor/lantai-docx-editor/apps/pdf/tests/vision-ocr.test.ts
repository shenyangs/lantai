import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { PDFDocument, StandardFonts } from 'pdf-lib'
import { runVisionOcr } from '../src/main/vision-ocr'

describe('bundled Vision OCR', () => {
  it('fails closed when the signed helper is unavailable', async () => {
    const result = await runVisionOcr('/missing/lantai-pdf-vision', {
      path: '/tmp/missing.pdf',
      pages: [0],
    })
    expect(result).toEqual(
      expect.objectContaining({ ok: false, code: 'unavailable' }),
    )
  })

  it.runIf(process.platform === 'darwin')(
    'recognizes a real rendered PDF page with the bundled offline helper',
    async () => {
      const fixture = await PDFDocument.create()
      const page = fixture.addPage([612, 792])
      const font = await fixture.embedFont(StandardFonts.Helvetica)
      page.drawText('VISION OCR 314159', { x: 72, y: 680, size: 32, font })
      const dir = mkdtempSync(join(tmpdir(), 'lantai-vision-ocr-'))
      const path = join(dir, 'scan.pdf')
      writeFileSync(path, await fixture.save({ useObjectStreams: false }))
      const binary = resolve(
        dirname(fileURLToPath(import.meta.url)),
        `../native-bin/darwin-${process.arch === 'x64' ? 'x64' : 'arm64'}/lantai-pdf-vision`,
      )

      const result = await runVisionOcr(binary, { path, pages: [0], languages: ['en-US'] })

      expect(result.ok).toBe(true)
      if (!result.ok) return
      expect(result.pages).toHaveLength(1)
      expect(result.pages[0]!.lines.map((line) => line.text).join(' ')).toContain('314159')
    },
    60_000,
  )
})
