import { existsSync } from 'node:fs'
import {
  readFile as officeRawReadFile,
  rename as officeRawRename,
  rm,
  writeFile as officeRawWriteFile,
} from 'node:fs/promises'
import { dirname, join } from 'node:path'
import {
  BrowserWindow,
  Menu,
  WebContentsView,
  app,
  dialog as electronDialog,
  ipcMain as electronIpcMain,
  shell,
} from 'electron'
import type { WebContents } from 'electron'
import {
  contextMenuLabels,
  installContextMenu,
  installNavigationGuard,
  safeExternalUrl,
  showOpenDialogWithMemory,
  showSaveDialogWithMemory,
} from '@genoffice/electron-utils'
import { createI18n, getUiLang } from '@genoffice/i18n'
import { generateOfficeImage, hasLantaiDesktopBridge } from '@lantai/office-ai-bridge'
import { PDF_CHANNELS } from '../shared/ipc'
import type {
  ExportImagesRequest,
  ExportImagesResult,
  ExtractPagesRequest,
  ExtractPagesResult,
  InsertPdfRequest,
  InsertPdfResult,
  PagePreviewRequest,
  SavePdfRequest,
  SavePdfResult,
  TextEditValidation,
  ValidateTextEditsRequest,
  OcrPagesRequest,
  OcrPagesResult,
  SecureRedactRequest,
  SecureRedactResult,
} from '../shared/ipc'
import { extractPagesBytes, insertPdfBytes, sanitizePdfBytes, savePdfToPath } from './save-pdf'
import { isSameRedactionCopyPath } from './redaction-path'
import { runVisionOcr } from './vision-ocr'
import {
  captureOfficeIpcRequest,
  captureOfficeRendererDocument,
  guardOfficeOperation,
  trustedOfficeIpcMain,
  registerOfficeRenderer,
  guardOfficeOperations,
  type OfficeIpcRequest,
} from '@genoffice/electron-utils'

const ipcMain = trustedOfficeIpcMain(electronIpcMain, 'pdf')
const dialog = guardOfficeOperations(electronDialog)
const readFile = guardOfficeOperation(officeRawReadFile)
const rename = guardOfficeOperation(officeRawRename)
const writeFile = guardOfficeOperation(officeRawWriteFile)

const tDlg = createI18n({
  zh: {
    dlgExportImages: '导出图片到文件夹',
    dlgExtract: '抽取页面为 PDF',
    dlgInsert: '选择要插入的 PDF',
    filterPdf: 'PDF 文档',
    closeUnsavedMsg: '此 PDF 有未保存的更改。',
    closeUnsavedDetail: '关闭前是否保存？',
    btnSave: '保存',
    btnDontSave: '不保存',
    btnCancel: '取消',
  },
  en: {
    dlgExportImages: 'Export Images to Folder',
    dlgExtract: 'Extract Pages as PDF',
    dlgInsert: 'Choose a PDF to Insert',
    filterPdf: 'PDF Documents',
    closeUnsavedMsg: 'This PDF has unsaved changes.',
    closeUnsavedDetail: 'Do you want to save them before closing?',
    btnSave: 'Save',
    btnDontSave: "Don't Save",
    btnCancel: 'Cancel',
  },
  ja: {
    dlgExportImages: '画像をフォルダに書き出す',
    dlgExtract: 'ページを PDF として抽出',
    dlgInsert: '挿入する PDF を選択',
    filterPdf: 'PDF ドキュメント',
    closeUnsavedMsg: 'この PDF に未保存の変更があります。',
    closeUnsavedDetail: '閉じる前に保存しますか？',
    btnSave: '保存',
    btnDontSave: '保存しない',
    btnCancel: 'キャンセル',
  },
  ko: {
    dlgExportImages: '이미지를 폴더로 내보내기',
    dlgExtract: '페이지를 PDF로 추출',
    dlgInsert: '삽입할 PDF 선택',
    filterPdf: 'PDF 문서',
    closeUnsavedMsg: '이 PDF에 저장하지 않은 변경 사항이 있습니다.',
    closeUnsavedDetail: '닫기 전에 저장하시겠습니까?',
    btnSave: '저장',
    btnDontSave: '저장 안 함',
    btnCancel: '취소',
  },
  fr: {
    dlgExportImages: 'Exporter les images vers un dossier',
    dlgExtract: 'Extraire les pages en PDF',
    dlgInsert: 'Choisir un PDF à insérer',
    filterPdf: 'Documents PDF',
    closeUnsavedMsg: 'Ce PDF contient des modifications non enregistrées.',
    closeUnsavedDetail: 'Voulez-vous les enregistrer avant de fermer ?',
    btnSave: 'Enregistrer',
    btnDontSave: 'Ne pas enregistrer',
    btnCancel: 'Annuler',
  },
  de: {
    dlgExportImages: 'Bilder in Ordner exportieren',
    dlgExtract: 'Seiten als PDF extrahieren',
    dlgInsert: 'Einzufügendes PDF wählen',
    filterPdf: 'PDF-Dokumente',
    closeUnsavedMsg: 'Dieses PDF enthält ungespeicherte Änderungen.',
    closeUnsavedDetail: 'Vor dem Schließen speichern?',
    btnSave: 'Speichern',
    btnDontSave: 'Nicht speichern',
    btnCancel: 'Abbrechen',
  },
  es: {
    dlgExportImages: 'Exportar imágenes a una carpeta',
    dlgExtract: 'Extraer páginas como PDF',
    dlgInsert: 'Elegir un PDF para insertar',
    filterPdf: 'Documentos PDF',
    closeUnsavedMsg: 'Este PDF tiene cambios sin guardar.',
    closeUnsavedDetail: '¿Quieres guardarlos antes de cerrar?',
    btnSave: 'Guardar',
    btnDontSave: 'No guardar',
    btnCancel: 'Cancelar',
  },
  th: {
    dlgExportImages: 'ส่งออกรูปภาพไปยังโฟลเดอร์',
    dlgExtract: 'แยกหน้าเป็น PDF',
    dlgInsert: 'เลือก PDF ที่จะแทรก',
    filterPdf: 'เอกสาร PDF',
    closeUnsavedMsg: 'PDF นี้มีการเปลี่ยนแปลงที่ยังไม่ได้บันทึก',
    closeUnsavedDetail: 'ต้องการบันทึกก่อนปิดหรือไม่?',
    btnSave: 'บันทึก',
    btnDontSave: 'ไม่บันทึก',
    btnCancel: 'ยกเลิก',
  },
  id: {
    dlgExportImages: 'Ekspor gambar ke folder',
    dlgExtract: 'Ekstrak halaman sebagai PDF',
    dlgInsert: 'Pilih PDF untuk disisipkan',
    filterPdf: 'Dokumen PDF',
    closeUnsavedMsg: 'PDF ini memiliki perubahan yang belum disimpan.',
    closeUnsavedDetail: 'Simpan sebelum menutup?',
    btnSave: 'Simpan',
    btnDontSave: 'Jangan Simpan',
    btnCancel: 'Batal',
  },
  ru: {
    dlgExportImages: 'Экспорт изображений в папку',
    dlgExtract: 'Извлечь страницы в PDF',
    dlgInsert: 'Выберите PDF для вставки',
    filterPdf: 'Документы PDF',
    closeUnsavedMsg: 'В этом PDF есть несохранённые изменения.',
    closeUnsavedDetail: 'Сохранить их перед закрытием?',
    btnSave: 'Сохранить',
    btnDontSave: 'Не сохранять',
    btnCancel: 'Отмена',
  },
  ar: {
    dlgExportImages: 'تصدير الصور إلى مجلد',
    dlgExtract: 'استخراج الصفحات كملف PDF',
    dlgInsert: 'اختر PDF للإدراج',
    filterPdf: 'مستندات PDF',
    closeUnsavedMsg: 'يحتوي هذا الـ PDF على تغييرات غير محفوظة.',
    closeUnsavedDetail: 'هل تريد حفظها قبل الإغلاق؟',
    btnSave: 'حفظ',
    btnDontSave: 'عدم الحفظ',
    btnCancel: 'إلغاء',
  },
  pt: {
    dlgExportImages: 'Exportar imagens para pasta',
    dlgExtract: 'Extrair páginas como PDF',
    dlgInsert: 'Escolher um PDF para inserir',
    filterPdf: 'Documentos PDF',
    closeUnsavedMsg: 'Este PDF tem alterações não salvas.',
    closeUnsavedDetail: 'Deseja salvá-las antes de fechar?',
    btnSave: 'Salvar',
    btnDontSave: 'Não Salvar',
    btnCancel: 'Cancelar',
  },
  it: {
    dlgExportImages: 'Esporta immagini in una cartella',
    dlgExtract: 'Estrai pagine come PDF',
    dlgInsert: 'Scegli un PDF da inserire',
    filterPdf: 'Documenti PDF',
    closeUnsavedMsg: 'Questo PDF contiene modifiche non salvate.',
    closeUnsavedDetail: 'Vuoi salvarle prima di chiudere?',
    btnSave: 'Salva',
    btnDontSave: 'Non salvare',
    btnCancel: 'Annulla',
  },
  pl: {
    dlgExportImages: 'Eksportuj obrazy do folderu',
    dlgExtract: 'Wyodrębnij strony jako PDF',
    dlgInsert: 'Wybierz PDF do wstawienia',
    filterPdf: 'Dokumenty PDF',
    closeUnsavedMsg: 'Ten PDF ma niezapisane zmiany.',
    closeUnsavedDetail: 'Czy zapisać je przed zamknięciem?',
    btnSave: 'Zapisz',
    btnDontSave: 'Nie zapisuj',
    btnCancel: 'Anuluj',
  },
  nl: {
    dlgExportImages: 'Afbeeldingen naar map exporteren',
    dlgExtract: "Pagina's extraheren als PDF",
    dlgInsert: 'Kies een PDF om in te voegen',
    filterPdf: 'PDF-documenten',
    closeUnsavedMsg: 'Deze PDF bevat niet-opgeslagen wijzigingen.',
    closeUnsavedDetail: 'Wilt u ze opslaan voordat u sluit?',
    btnSave: 'Opslaan',
    btnDontSave: 'Niet opslaan',
    btnCancel: 'Annuleren',
  },
  ms: {
    dlgExportImages: 'Eksport imej ke folder',
    dlgExtract: 'Ekstrak halaman sebagai PDF',
    dlgInsert: 'Pilih PDF untuk disisipkan',
    filterPdf: 'Dokumen PDF',
    closeUnsavedMsg: 'PDF ini mempunyai perubahan yang belum disimpan.',
    closeUnsavedDetail: 'Simpan sebelum menutup?',
    btnSave: 'Simpan',
    btnDontSave: 'Jangan Simpan',
    btnCancel: 'Batal',
  },
  he: {
    dlgExportImages: 'ייצוא תמונות לתיקייה',
    dlgExtract: 'חילוץ עמודים כ-PDF',
    dlgInsert: 'בחרו PDF להוספה',
    filterPdf: 'מסמכי PDF',
    closeUnsavedMsg: 'ב-PDF הזה יש שינויים שלא נשמרו.',
    closeUnsavedDetail: 'האם לשמור אותם לפני הסגירה?',
    btnSave: 'שמירה',
    btnDontSave: 'אל תשמור',
    btnCancel: 'ביטול',
  },
  hi: {
    dlgExportImages: 'चित्र फ़ोल्डर में निर्यात करें',
    dlgExtract: 'पृष्ठों को PDF के रूप में निकालें',
    dlgInsert: 'सम्मिलित करने के लिए PDF चुनें',
    filterPdf: 'PDF दस्तावेज़',
    closeUnsavedMsg: 'इस PDF में सहेजे नहीं गए परिवर्तन हैं।',
    closeUnsavedDetail: 'क्या बंद करने से पहले उन्हें सहेजना चाहते हैं?',
    btnSave: 'सहेजें',
    btnDontSave: 'न सहेजें',
    btnCancel: 'रद्द करें',
  },
  'zh-TW': {
    dlgExportImages: '匯出圖片到資料夾',
    dlgExtract: '擷取頁面為 PDF',
    dlgInsert: '選擇要插入的 PDF',
    filterPdf: 'PDF 文件',
    closeUnsavedMsg: '此 PDF 有未儲存的變更。',
    closeUnsavedDetail: '關閉前是否儲存？',
    btnSave: '儲存',
    btnDontSave: '不儲存',
    btnCancel: '取消',
  },
})
type DlgKey =
  | 'dlgExportImages'
  | 'dlgExtract'
  | 'dlgInsert'
  | 'filterPdf'
  | 'closeUnsavedMsg'
  | 'closeUnsavedDetail'
  | 'btnSave'
  | 'btnDontSave'
  | 'btnCancel'
const tm = (key: DlgKey) => tDlg(getUiLang(), key)

interface RuntimePaths {
  preloadPath: string
  rendererUrl?: string
  rendererFile?: string
  visionOcrPath?: string
}

let runtime: RuntimePaths = { preloadPath: '' }

export function configurePdfRuntime(paths: RuntimePaths): void {
  runtime = paths
}

/** Open path per view, queued at tab creation; the renderer consumes it after mount
 * (avoids did-finish-load races). Kept until the view is destroyed: a reload
 * (View > Reload) remounts the renderer and consumes again — a one-shot entry
 * would strand the tab on "No file to open". */
const openPathByWc = new Map<number, string>()
/** File paths granted to each view — readFile only allows these */
const allowedByWc = new Map<number, Set<string>>()
/** Unsaved-changes flags mirrored from the renderer; drives the save prompt before closing a tab/window */
const dirtyByWc = new Set<number>()
interface PendingPdfSave {
  origin: OfficeIpcRequest
  finish(ok: boolean): void
}
const closeSaveWaiters = new Map<number, PendingPdfSave>()
const saveAsWaiters = new Map<number, PendingPdfSave>()
/** Save As destination granted per view (main-process dialog pick); the save handler refuses any other non-source target */
const saveAsTargetByWc = new Map<number, { path: string; origin: OfficeIpcRequest }>()

export function pdfIsDirty(webContentsId: number): boolean {
  return dirtyByWc.has(webContentsId)
}

/**
 * Close guard for the pdf renderer: true means proceed with closing.
 * Clean → true; dirty → Save / Don't Save / Cancel. On Save, ask the renderer to
 * write to disk and await the result; on failure or timeout stay open (renderer
 * has already shown the error).
 */
export async function requestPdfClose(
  contents: WebContents,
  parent?: BrowserWindow | null,
): Promise<boolean> {
  if (!dirtyByWc.has(contents.id) || contents.isDestroyed()) return true
  const origin = captureOfficeRendererDocument(contents)
  if (!origin) return false
  const options = {
    type: 'warning' as const,
    message: tm('closeUnsavedMsg'),
    detail: tm('closeUnsavedDetail'),
    buttons: [tm('btnSave'), tm('btnDontSave'), tm('btnCancel')],
    defaultId: 0,
    cancelId: 2,
    noLink: true,
  }
  const { response } =
    parent && !parent.isDestroyed()
      ? await dialog.showMessageBox(parent, options)
      : await dialog.showMessageBox(options)
  if (!origin.isCurrent()) return false
  if (response === 2) return false
  if (response === 1) return true
  return await requestRendererSave(contents, origin)
}

function requestDocumentSave(
  contents: WebContents,
  waiters: Map<number, PendingPdfSave>,
  origin: OfficeIpcRequest | null,
  channel: string,
  targetPath?: string,
): Promise<boolean> {
  if (!origin?.isCurrent() || waiters.has(contents.id)) return Promise.resolve(false)
  return new Promise<boolean>((resolve) => {
    let settled = false
    let dispose = () => {}
    const pending: PendingPdfSave = {
      origin,
      finish(ok) {
        if (settled) return
        settled = true
        clearTimeout(timer)
        dispose()
        if (waiters.get(contents.id) === pending) waiters.delete(contents.id)
        if (saveAsTargetByWc.get(contents.id)?.origin === origin)
          saveAsTargetByWc.delete(contents.id)
        resolve(ok && origin.isCurrent())
      },
    }
    const timer = setTimeout(() => pending.finish(false), 120_000)
    waiters.set(contents.id, pending)
    if (targetPath) saveAsTargetByWc.set(contents.id, { path: targetPath, origin })
    dispose = origin.onInvalidated(() => pending.finish(false))
    if (origin.isCurrent()) contents.send(channel, ...(targetPath ? [targetPath] : []))
    else pending.finish(false)
  })
}

function requestRendererSave(
  contents: WebContents,
  origin = captureOfficeRendererDocument(contents),
): Promise<boolean> {
  return requestDocumentSave(contents, closeSaveWaiters, origin, PDF_CHANNELS.closeSaveRequest)
}

/** Menu Save: ask the renderer to write pending edits to disk; clean views resolve true immediately */
export function flushPdfSave(contents: WebContents): Promise<boolean> {
  if (contents.isDestroyed() || !dirtyByWc.has(contents.id)) return Promise.resolve(true)
  return requestRendererSave(contents)
}

/**
 * Marks the whole Save As flow (dialog included) for the renderer, which pauses
 * autosave meanwhile: opening the save dialog blurs the window, and a
 * blur-triggered autosave would write the pending edits into the original file.
 */
export function setPdfSaveAsInFlight(contents: WebContents, inFlight: boolean): void {
  if (captureOfficeRendererDocument(contents)?.isCurrent())
    contents.send(PDF_CHANNELS.saveAsFlow, inFlight)
}

/**
 * Menu Save As: grant targetPath to the view, then ask the renderer to apply its
 * pending edits onto the source bytes and write the result to targetPath only.
 * The original file is never written (non-destructive Save As).
 */
export function requestPdfSaveAs(contents: WebContents, targetPath: string): Promise<boolean> {
  return requestDocumentSave(
    contents,
    saveAsWaiters,
    captureOfficeRendererDocument(contents),
    PDF_CHANNELS.saveAsRequest,
    targetPath,
  )
}

let ipcRegistered = false

function registerPdfIpc(): void {
  if (ipcRegistered) return
  ipcRegistered = true

  ipcMain.handle(PDF_CHANNELS.consumePending, (e) => openPathByWc.get(e.sender.id) ?? null)

  ipcMain.handle(PDF_CHANNELS.chooseOpen, async (e) => {
    const win =
      BrowserWindow.fromWebContents(e.sender) ?? BrowserWindow.getFocusedWindow() ?? undefined
    const picked = await dialog.showOpenDialog(win!, {
      title: '打开 PDF',
      filters: [{ name: tm('filterPdf'), extensions: ['pdf'] }],
      properties: ['openFile'],
    })
    const selected = picked.filePaths[0]
    if (picked.canceled || !selected) return null
    const grants = allowedByWc.get(e.sender.id) ?? new Set<string>()
    grants.add(selected)
    allowedByWc.set(e.sender.id, grants)
    return selected
  })

  ipcMain.handle(PDF_CHANNELS.chooseSaveAs, async (e, sourcePath: unknown) => {
    if (typeof sourcePath !== 'string' || !allowedByWc.get(e.sender.id)?.has(sourcePath))
      return false
    const win =
      BrowserWindow.fromWebContents(e.sender) ?? BrowserWindow.getFocusedWindow() ?? undefined
    const origin = captureOfficeIpcRequest()
    setPdfSaveAsInFlight(e.sender, true)
    try {
      const picked = await dialog.showSaveDialog(win!, {
        title: 'PDF 另存为',
        defaultPath: join(dirname(sourcePath), sourcePath.split(/[/\\]/).pop() ?? 'document.pdf'),
        filters: [{ name: tm('filterPdf'), extensions: ['pdf'] }],
      })
      if (picked.canceled || !picked.filePath) return false
      return await requestPdfSaveAs(e.sender, picked.filePath)
    } finally {
      if (origin.isCurrent()) setPdfSaveAsInFlight(e.sender, false)
    }
  })

  ipcMain.handle(PDF_CHANNELS.readFile, async (e, path: unknown) => {
    if (typeof path !== 'string' || !allowedByWc.get(e.sender.id)?.has(path)) {
      throw new Error('pdf: path not granted to this view')
    }
    const buf = await readFile(path)
    return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)
  })

  ipcMain.handle(PDF_CHANNELS.save, async (e, request: SavePdfRequest): Promise<SavePdfResult> => {
    const path = request?.path
    if (typeof path !== 'string' || !allowedByWc.get(e.sender.id)?.has(path)) {
      return { ok: false, error: 'pdf: path not granted to this view' }
    }
    // Save As targets must have been granted by requestPdfSaveAs (main-process dialog pick)
    const target = typeof request.targetPath === 'string' ? request.targetPath : path
    const grant = saveAsTargetByWc.get(e.sender.id)
    if (target !== path && (grant?.path !== target || !grant.origin.isSameCaller())) {
      return { ok: false, error: 'pdf: target path not granted to this view' }
    }
    try {
      const { skippedTextEdits, skippedImageEdits } = await savePdfToPath(path, target, request)
      return {
        ok: true,
        ...(skippedTextEdits.length > 0 ? { skippedTextEdits } : {}),
        ...(skippedImageEdits.length > 0 ? { skippedImageEdits } : {}),
      }
    } catch (err) {
      return { ok: false, error: err instanceof Error ? err.message : String(err) }
    }
  })

  ipcMain.handle(PDF_CHANNELS.listPageImages, async (e, path: unknown) => {
    if (typeof path !== 'string' || !allowedByWc.get(e.sender.id)?.has(path)) {
      throw new Error('pdf: path not granted to this view')
    }
    // Lazy import like the text-edit paths: pdfium wasm only loads when the feature is used
    const { listPageImages } = await import('./image-edit')
    return listPageImages(new Uint8Array(await readFile(path)))
  })

  ipcMain.handle(
    PDF_CHANNELS.pageImagePng,
    async (
      e,
      request: { path: string; pageIndex: number; rect: [number, number, number, number] },
    ) => {
      const { path, pageIndex, rect } = request ?? {}
      if (
        typeof path !== 'string' ||
        !allowedByWc.get(e.sender.id)?.has(path) ||
        typeof pageIndex !== 'number' ||
        !Array.isArray(rect)
      ) {
        throw new Error('pdf: path not granted to this view')
      }
      const { renderImagePng } = await import('./image-edit')
      return renderImagePng(new Uint8Array(await readFile(path)), pageIndex, rect)
    },
  )

  ipcMain.handle(PDF_CHANNELS.pagePreviewPng, async (e, request: PagePreviewRequest) => {
    const { path, pageIndex, excludeRects, clip, pxWidth, rotate } = request ?? {}
    if (
      typeof path !== 'string' ||
      !allowedByWc.get(e.sender.id)?.has(path) ||
      typeof pageIndex !== 'number' ||
      !Array.isArray(excludeRects) ||
      typeof clip !== 'object' ||
      typeof pxWidth !== 'number' ||
      typeof rotate !== 'number'
    ) {
      throw new Error('pdf: path not granted to this view')
    }
    const { renderPagePreviewPng } = await import('./image-edit')
    return renderPagePreviewPng(new Uint8Array(await readFile(path)), {
      pageIndex,
      excludeRects,
      clip,
      pxWidth,
      rotate,
    })
  })

  ipcMain.handle(
    PDF_CHANNELS.validateTextEdits,
    async (e, request: ValidateTextEditsRequest): Promise<TextEditValidation[]> => {
      const { path, edits } = request ?? {}
      if (
        typeof path !== 'string' ||
        !allowedByWc.get(e.sender.id)?.has(path) ||
        !Array.isArray(edits)
      ) {
        throw new Error('pdf: path not granted to this view')
      }
      // Same lazy import as the save path: the pdfium wasm only loads when text editing is used
      const { validateTextEdits } = await import('./text-edit')
      return validateTextEdits(new Uint8Array(await readFile(path)), edits)
    },
  )

  ipcMain.handle(PDF_CHANNELS.listEditFonts, async (): Promise<string[]> => {
    const { listEditFonts } = await import('./text-edit')
    return listEditFonts()
  })

  ipcMain.handle(
    PDF_CHANNELS.ocrPages,
    async (e, request: OcrPagesRequest): Promise<OcrPagesResult> => {
      const { path, pages } = request ?? {}
      if (
        typeof path !== 'string' ||
        !allowedByWc.get(e.sender.id)?.has(path) ||
        !Array.isArray(pages)
      ) {
        return { ok: false, code: 'invalid_request', error: 'pdf: path not granted to this view' }
      }
      return runVisionOcr(runtime.visionOcrPath, request)
    },
  )

  ipcMain.handle(
    PDF_CHANNELS.extractPages,
    async (e, request: ExtractPagesRequest): Promise<ExtractPagesResult> => {
      const { path, pages, suggestedName } = request ?? {}
      if (
        typeof path !== 'string' ||
        !allowedByWc.get(e.sender.id)?.has(path) ||
        !Array.isArray(pages)
      ) {
        return { ok: false, error: 'pdf: path not granted to this view' }
      }
      const win =
        BrowserWindow.fromWebContents(e.sender) ?? BrowserWindow.getFocusedWindow() ?? undefined
      const picked = await showSaveDialogWithMemory(dialog, win, {
        title: tm('dlgExtract'),
        defaultPath: join(dirname(path), String(suggestedName || 'pages.pdf')),
        filters: [{ name: tm('filterPdf'), extensions: ['pdf'] }],
      })
      if (picked.canceled || !picked.filePath) return { ok: true, canceled: true }
      try {
        const bytes = await extractPagesBytes(new Uint8Array(await readFile(path)), pages)
        await writeFile(picked.filePath, bytes)
        return { ok: true, savedPath: picked.filePath }
      } catch (err) {
        return { ok: false, error: err instanceof Error ? err.message : String(err) }
      }
    },
  )

  ipcMain.handle(
    PDF_CHANNELS.insertPdf,
    async (e, request: InsertPdfRequest): Promise<InsertPdfResult> => {
      const { path, afterPageIndex } = request ?? {}
      if (typeof path !== 'string' || !allowedByWc.get(e.sender.id)?.has(path)) {
        return { ok: false, error: 'pdf: path not granted to this view' }
      }
      const win =
        BrowserWindow.fromWebContents(e.sender) ?? BrowserWindow.getFocusedWindow() ?? undefined
      const picked = await showOpenDialogWithMemory(dialog, win, {
        title: tm('dlgInsert'),
        filters: [{ name: tm('filterPdf'), extensions: ['pdf'] }],
        properties: ['openFile'],
      })
      const other = picked.filePaths[0]
      if (picked.canceled || !other) return { ok: true, canceled: true }
      try {
        const { merged, count } = await insertPdfBytes(
          new Uint8Array(await readFile(path)),
          new Uint8Array(await readFile(other)),
          typeof afterPageIndex === 'number' ? afterPageIndex : -1,
        )
        const tmp = `${path}.gensave-${process.pid}.tmp`
        await writeFile(tmp, merged)
        await rename(tmp, path)
        return { ok: true, insertedCount: count }
      } catch (err) {
        return { ok: false, error: err instanceof Error ? err.message : String(err) }
      }
    },
  )

  ipcMain.handle(
    PDF_CHANNELS.secureRedact,
    async (e, request: SecureRedactRequest): Promise<SecureRedactResult> => {
      const { path, pages, suggestedName } = request ?? {}
      const totalImageChars = Array.isArray(pages)
        ? pages.reduce((total, page) => total + String(page?.image ?? '').length, 0)
        : 0
      if (
        typeof path !== 'string' ||
        !allowedByWc.get(e.sender.id)?.has(path) ||
        !Array.isArray(pages) ||
        pages.length === 0 ||
        pages.length > 10 ||
        totalImageChars > 120 * 1024 * 1024
      ) {
        return { ok: false, error: 'pdf: invalid secure-redaction request' }
      }
      const win =
        BrowserWindow.fromWebContents(e.sender) ?? BrowserWindow.getFocusedWindow() ?? undefined
      const picked = await showSaveDialogWithMemory(dialog, win, {
        title: '安全脱敏副本另存为',
        defaultPath: join(dirname(path), String(suggestedName || 'redacted.pdf')),
        filters: [{ name: tm('filterPdf'), extensions: ['pdf'] }],
      })
      if (picked.canceled || !picked.filePath) return { ok: true, canceled: true }
      if (isSameRedactionCopyPath(path, picked.filePath)) {
        return { ok: false, error: '安全脱敏必须另存为新文件，以保留可回滚的原文档。' }
      }
      const tmp = `${picked.filePath}.gensafe-${process.pid}.tmp`
      try {
        const output = await sanitizePdfBytes(new Uint8Array(await readFile(path)), pages)
        await writeFile(tmp, output)
        await rename(tmp, picked.filePath)
        return { ok: true, savedPath: picked.filePath, rasterizedPages: pages.length }
      } catch (error) {
        await rm(tmp, { force: true })
        return { ok: false, error: error instanceof Error ? error.message : String(error) }
      }
    },
  )

  ipcMain.handle(
    PDF_CHANNELS.exportImages,
    async (e, request: ExportImagesRequest): Promise<ExportImagesResult> => {
      const { images, pageNumbers, baseName } = request ?? {}
      if (!Array.isArray(images) || images.length === 0)
        return { ok: false, error: 'pdf: no images' }
      const win =
        BrowserWindow.fromWebContents(e.sender) ?? BrowserWindow.getFocusedWindow() ?? undefined
      const picked = await showOpenDialogWithMemory(dialog, win, {
        title: tm('dlgExportImages'),
        properties: ['openDirectory', 'createDirectory'],
      })
      const dir = picked.filePaths[0]
      if (picked.canceled || !dir) return { ok: true, canceled: true }
      try {
        const safeBase = String(baseName || 'page').replace(/[/\\:*?"<>|]/g, '_')
        for (const [i, b64] of images.entries()) {
          const no = pageNumbers?.[i] ?? i + 1
          await writeFile(join(dir, `${safeBase}-p${no}.png`), Buffer.from(b64, 'base64'))
        }
        return { ok: true, savedDir: dir, count: images.length }
      } catch (err) {
        return { ok: false, error: err instanceof Error ? err.message : String(err) }
      }
    },
  )

  // pdf-owned (unlike ai:image-search / ai:fetch-image, which the shell registers app-wide):
  // slides' ai:generate-image is only registered once a slides view exists, so pdf needs its own
  ipcMain.handle(
    PDF_CHANNELS.generateImage,
    async (_e, op: { prompt?: unknown; aspectRatio?: unknown }) => {
      if (!hasLantaiDesktopBridge())
        return {
          error: '请通过澜台主程序使用生图能力',
        }
      const prompt = String(op?.prompt ?? '').trim()
      if (!prompt) return { error: 'prompt must not be empty' }
      try {
        const r = await generateOfficeImage({
          prompt,
          aspectRatio: op?.aspectRatio ? String(op.aspectRatio) : undefined,
        })
        return { url: r.url }
      } catch (err) {
        return { error: err instanceof Error ? err.message : String(err) }
      }
    },
  )

  ipcMain.on(PDF_CHANNELS.dirtyChanged, (e, dirty: unknown) => {
    if (dirty === true) dirtyByWc.add(e.sender.id)
    else dirtyByWc.delete(e.sender.id)
  })

  ipcMain.on(PDF_CHANNELS.closeSaveResult, (e, ok: unknown) => {
    const waiter = closeSaveWaiters.get(e.sender.id)
    if (waiter?.origin.isSameCaller()) waiter.finish(ok === true)
  })

  ipcMain.on(PDF_CHANNELS.saveAsResult, (e, ok: unknown) => {
    const waiter = saveAsWaiters.get(e.sender.id)
    if (waiter?.origin.isSameCaller()) waiter.finish(ok === true)
  })

  // Language channel shared with other modules; removeHandler tolerates duplicate registration
  ipcMain.removeHandler(PDF_CHANNELS.getLanguage)
  ipcMain.handle(PDF_CHANNELS.getLanguage, () => getUiLang())
  // Embedded and standalone PDF windows share the host's system appearance.
  // Always register the channel here: a missing handler used to reject during
  // renderer bootstrap and could leave the editor on a blank shell.
  ipcMain.removeHandler(PDF_CHANNELS.getTheme)
  ipcMain.handle(PDF_CHANNELS.getTheme, () => 'system')
}

function grantAndTrack(wc: WebContents, openPath?: string | null): void {
  registerOfficeRenderer(wc, { kind: 'pdf', ...runtime })
  const wcId = wc.id
  if (openPath && existsSync(openPath)) {
    openPathByWc.set(wcId, openPath)
    allowedByWc.set(wcId, new Set([openPath]))
  }
  // External links inside the PDF (Link annots with target=_blank) go to the system browser
  wc.setWindowOpenHandler(({ url }) => {
    const target = safeExternalUrl(url, { allowedProtocols: ['http:', 'https:', 'mailto:'] })
    if (target) void shell.openExternal(target)
    return { action: 'deny' }
  })
  wc.once('destroyed', () => {
    openPathByWc.delete(wcId)
    allowedByWc.delete(wcId)
    dirtyByWc.delete(wcId)
    saveAsTargetByWc.delete(wcId)
    closeSaveWaiters.get(wcId)?.finish(false)
    closeSaveWaiters.delete(wcId)
    saveAsWaiters.get(wcId)?.finish(false)
    saveAsWaiters.delete(wcId)
  })
}

export function createPdfView(openPath?: string | null): WebContentsView {
  registerPdfIpc()
  const view = new WebContentsView({
    webPreferences: {
      preload: runtime.preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  grantAndTrack(view.webContents, openPath)
  if (runtime.rendererUrl) {
    const url = new URL(runtime.rendererUrl)
    url.searchParams.set('mode', 'tab')
    void view.webContents.loadURL(url.toString())
  } else if (runtime.rendererFile) {
    void view.webContents.loadFile(runtime.rendererFile, { query: { mode: 'tab' } })
  }
  return view
}

/** Standalone editor window used by the 澜台 host application. */
export function createPdfWindow(openPath?: string | null): BrowserWindow {
  registerPdfIpc()
  const win = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 900,
    minHeight: 620,
    show: false,
    title: '澜台 PDF',
    ...(process.platform === 'darwin' ? { titleBarStyle: 'hiddenInset' as const } : {}),
    webPreferences: {
      preload: runtime.preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  grantAndTrack(win.webContents, openPath)
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  win.webContents.on('will-navigate', (event) => event.preventDefault())
  win.once('ready-to-show', () => win.show())
  win.on('close', (event) => {
    if (!pdfIsDirty(win.webContents.id)) return
    event.preventDefault()
    void requestPdfClose(win.webContents, win).then((proceed) => {
      if (proceed && !win.isDestroyed()) win.destroy()
    })
  })
  if (runtime.rendererUrl) void win.loadURL(runtime.rendererUrl)
  else if (runtime.rendererFile) void win.loadFile(runtime.rendererFile)
  return win
}

/** Installs a traditional desktop File menu for the currently focused PDF window. */
export function installPdfMenu(): void {
  const send = (action: 'open' | 'save' | 'save-as' | 'print' | 'export-images') => {
    const contents = BrowserWindow.getFocusedWindow()?.webContents
    if (contents && allowedByWc.has(contents.id)) contents.send(PDF_CHANNELS.menuAction, action)
  }
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([
      ...(process.platform === 'darwin' ? [{ role: 'appMenu' as const }] : []),
      {
        label: '文件',
        submenu: [
          { label: '打开…', accelerator: 'CmdOrCtrl+O', click: () => send('open') },
          { type: 'separator' as const },
          { label: '保存', accelerator: 'CmdOrCtrl+S', click: () => send('save') },
          { label: '另存为…', accelerator: 'Shift+CmdOrCtrl+S', click: () => send('save-as') },
          { label: '导出为图片…', click: () => send('export-images') },
          { label: '打印…', accelerator: 'CmdOrCtrl+P', click: () => send('print') },
          { type: 'separator' as const },
          { role: 'close' as const },
        ],
      },
      { role: 'editMenu' as const },
      { role: 'viewMenu' as const },
      { role: 'windowMenu' as const },
    ]),
  )
}

/** Standalone window mode: `npm run dev -w @genoffice/pdf`, pdf path passed via argv */
export function startPdfStandalone(): void {
  installNavigationGuard(app)
  installContextMenu(app, () => contextMenuLabels(getUiLang()))
  const generatedVision = join(
    __dirname,
    `../../generated-native/darwin-${process.arch === 'x64' ? 'x64' : 'arm64'}/lantai-pdf-vision`,
  )
  configurePdfRuntime({
    preloadPath: join(__dirname, '../preload/index.js'),
    rendererUrl: process.env.ELECTRON_RENDERER_URL,
    rendererFile: join(__dirname, '../renderer/index.html'),
    visionOcrPath: existsSync(generatedVision)
      ? generatedVision
      : join(
          __dirname,
          `../../native-bin/darwin-${process.arch === 'x64' ? 'x64' : 'arm64'}/lantai-pdf-vision`,
        ),
  })
  void app.whenReady().then(() => {
    registerPdfIpc()
    const win = new BrowserWindow({
      width: 1200,
      height: 850,
      webPreferences: {
        preload: runtime.preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    })
    const argPath = process.argv.slice(1).find((a) => /\.pdf$/i.test(a) && existsSync(a))
    grantAndTrack(win.webContents, argPath)
    if (runtime.rendererUrl) void win.loadURL(runtime.rendererUrl)
    else if (runtime.rendererFile) void win.loadFile(runtime.rendererFile)
  })
  app.on('window-all-closed', () => app.quit())
}
