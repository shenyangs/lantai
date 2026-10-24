import { mkdtemp as officeRawMkdtemp, rm, writeFile as officeRawWriteFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { guardOfficeOperation } from '@genoffice/electron-utils'

const mkdtemp = guardOfficeOperation(officeRawMkdtemp)
const writeFile = guardOfficeOperation(officeRawWriteFile)


export interface PdfExportWindow {
  loadFile(path: string): Promise<void>
  webContents: {
    executeJavaScript(script: string, userGesture?: boolean): Promise<unknown>
    printToPDF(options: Record<string, unknown>): Promise<Uint8Array>
  }
  destroy(): void
}

export interface ExportSlidesPdfOptions {
  html: string
  filePath: string
  printOptions: Record<string, unknown>
  createWindow(): PdfExportWindow
}

/**
 * Load export markup from a temporary file. A data: URL grows by another third
 * after base64 encoding and can exceed Chromium's URL limit on image-heavy decks.
 */
export async function exportSlidesPdf({
  html,
  filePath,
  printOptions,
  createWindow,
}: ExportSlidesPdfOptions): Promise<void> {
  const tempDirectory = await mkdtemp(join(tmpdir(), 'lantai-slides-pdf-'))
  const htmlPath = join(tempDirectory, 'slides.html')
  let win: PdfExportWindow | undefined
  try {
    win = createWindow()
    await writeFile(htmlPath, html, 'utf8')
    await win.loadFile(htmlPath)
    await win.webContents.executeJavaScript(
      'Promise.all([document.fonts.ready, ...Array.from(document.images).map((i) => i.decode().catch(() => {}))])',
      true,
    )
    const pdf = await win.webContents.printToPDF(printOptions)
    await writeFile(filePath, pdf)
  } finally {
    try {
      win?.destroy()
    } finally {
      await rm(tempDirectory, { recursive: true, force: true })
    }
  }
}
