/// PDF export: renders the print HTML (laid out by the renderer) in a hidden
/// scripting-disabled window and writes webContents.printToPDF's output where
/// the save dialog points.

import { mkdtemp as officeRawMkdtemp, rm, writeFile as officeRawWriteFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { BrowserWindow, dialog } from 'electron'

import { showSaveDialogWithMemory } from '@genoffice/electron-utils'

import type { IpcMainInvokeEvent } from 'electron'
import type { WorkbookExportPdfRequest, WorkbookExportPdfResult } from '../shared/desktop-api'
import { guardOfficeOperation } from '@genoffice/electron-utils'

const mkdtemp = guardOfficeOperation(officeRawMkdtemp)
const writeFile = guardOfficeOperation(officeRawWriteFile)


export async function exportPdf(
  event: IpcMainInvokeEvent,
  request: WorkbookExportPdfRequest,
): Promise<WorkbookExportPdfResult> {
  const parent = BrowserWindow.fromWebContents(event.sender)
  const dialogOptions = {
    defaultPath: request.fileName,
    filters: [{ name: 'PDF', extensions: ['pdf'] }],
  }
  const selection = await showSaveDialogWithMemory(dialog, parent, dialogOptions)
  if (selection.canceled || !selection.filePath) return { canceled: true }

  const workDir = await mkdtemp(join(tmpdir(), 'ai-excel-pdf-'))
  const htmlPath = join(workDir, 'print.html')
  const window = new BrowserWindow({
    show: false,
    webPreferences: { sandbox: true, javascript: false },
  })
  try {
    await writeFile(htmlPath, request.html, 'utf8')
    await window.loadFile(htmlPath)
    const pdf = await window.webContents.printToPDF({
      landscape: request.landscape,
      pageSize: request.pageSize,
      margins: request.margins,
      scale: request.scale,
      printBackground: true,
    })
    await writeFile(selection.filePath, pdf)
    return { canceled: false, path: selection.filePath }
  } finally {
    window.destroy()
    await rm(workDir, { recursive: true, force: true })
  }
}

/** Print the same layout used by PDF export, without exposing the workbook UI
 * chrome to the system print dialog. */
export async function printWorkbook(
  request: WorkbookExportPdfRequest,
): Promise<{ success: boolean; error?: string }> {
  const workDir = await mkdtemp(join(tmpdir(), 'lantai-excel-print-'))
  const htmlPath = join(workDir, 'print.html')
  const window = new BrowserWindow({
    show: false,
    webPreferences: { sandbox: true, javascript: false },
  })
  try {
    await writeFile(htmlPath, request.html, 'utf8')
    await window.loadFile(htmlPath)
    return await new Promise((resolve) => {
      window.webContents.print(
        {
          silent: false,
          printBackground: true,
          landscape: request.landscape,
          pageSize: request.pageSize,
        },
        (success, failureReason) =>
          resolve({
            success,
            ...(success || !failureReason ? {} : { error: failureReason }),
          }),
      )
    })
  } finally {
    window.destroy()
    await rm(workDir, { recursive: true, force: true })
  }
}
