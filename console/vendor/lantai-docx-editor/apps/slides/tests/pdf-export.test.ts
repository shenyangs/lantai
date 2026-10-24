import { existsSync } from 'node:fs'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { exportSlidesPdf, type PdfExportWindow } from '../src/main/pdf-export'

const roots: string[] = []

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })))
})

function fakeWindow(overrides: Partial<PdfExportWindow> = {}): PdfExportWindow {
  return {
    loadFile: vi.fn(async () => undefined),
    webContents: {
      executeJavaScript: vi.fn(async () => undefined),
      printToPDF: vi.fn(async () => new Uint8Array([37, 80, 68, 70])),
    },
    destroy: vi.fn(),
    ...overrides,
  }
}

describe('slides PDF export', () => {
  it('loads large markup from a temporary file and removes it afterward', async () => {
    const root = await mkdtemp(join(tmpdir(), 'lantai-pdf-test-'))
    roots.push(root)
    const filePath = join(root, 'output.pdf')
    const html = `<!doctype html><img src="data:image/png;base64,${'A'.repeat(2_100_000)}">`
    let loadedHtml = ''
    let temporaryDirectory = ''
    const win = fakeWindow({
      loadFile: vi.fn(async (path: string) => {
        temporaryDirectory = dirname(path)
        loadedHtml = await readFile(path, 'utf8')
      }),
    })

    await exportSlidesPdf({
      html,
      filePath,
      printOptions: { printBackground: true },
      createWindow: () => win,
    })

    expect(loadedHtml).toBe(html)
    expect(await readFile(filePath)).toEqual(Buffer.from([37, 80, 68, 70]))
    expect(win.destroy).toHaveBeenCalledOnce()
    expect(existsSync(temporaryDirectory)).toBe(false)
  })

  it('cleans up the temporary file when printing fails', async () => {
    const root = await mkdtemp(join(tmpdir(), 'lantai-pdf-test-'))
    roots.push(root)
    let temporaryDirectory = ''
    const win = fakeWindow({
      loadFile: vi.fn(async (path: string) => {
        temporaryDirectory = dirname(path)
      }),
      webContents: {
        executeJavaScript: vi.fn(async () => undefined),
        printToPDF: vi.fn(async () => {
          throw new Error('print failed')
        }),
      },
    })

    await expect(
      exportSlidesPdf({
        html: '<html></html>',
        filePath: join(root, 'output.pdf'),
        printOptions: {},
        createWindow: () => win,
      }),
    ).rejects.toThrow('print failed')
    expect(win.destroy).toHaveBeenCalledOnce()
    expect(existsSync(temporaryDirectory)).toBe(false)
  })

  it('cleans up the temporary directory when window creation fails', async () => {
    const before = new Set(
      (await readdir(tmpdir())).filter((path) => path.startsWith('lantai-slides-pdf-')),
    )
    await expect(
      exportSlidesPdf({
        html: '<html></html>',
        filePath: join(tmpdir(), 'unreachable.pdf'),
        printOptions: {},
        createWindow: () => {
          throw new Error('window failed')
        },
      }),
    ).rejects.toThrow('window failed')
    const after = (await readdir(tmpdir())).filter((path) => path.startsWith('lantai-slides-pdf-'))
    expect(after.filter((path) => !before.has(path))).toEqual([])
  })
})
