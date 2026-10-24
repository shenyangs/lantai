import { execFile } from 'node:child_process'
import { access } from 'node:fs/promises'
import { constants } from 'node:fs'
import type { OcrPage, OcrPagesRequest, OcrPagesResult } from '../shared/ipc'

const MAX_PAGES = 10
const MAX_OUTPUT_BYTES = 16 * 1024 * 1024

function validPage(page: unknown): page is OcrPage {
  return (
    !!page &&
    typeof page === 'object' &&
    Number.isInteger((page as OcrPage).pageIndex) &&
    Number.isFinite((page as OcrPage).width) &&
    Number.isFinite((page as OcrPage).height) &&
    Array.isArray((page as OcrPage).lines)
  )
}

export async function runVisionOcr(
  binaryPath: string | undefined,
  request: OcrPagesRequest,
): Promise<OcrPagesResult> {
  if (!binaryPath) return { ok: false, code: 'unavailable', error: 'PDF OCR runtime is unavailable' }
  try {
    await access(binaryPath, constants.X_OK)
  } catch {
    return { ok: false, code: 'unavailable', error: 'PDF OCR runtime is unavailable' }
  }
  const pages = request.pages
  if (
    !Array.isArray(pages) ||
    pages.length === 0 ||
    pages.length > MAX_PAGES ||
    pages.some((page) => !Number.isInteger(page) || page < 0)
  ) {
    return { ok: false, code: 'invalid_request', error: 'OCR requires 1 to 10 valid pages' }
  }
  const payload = JSON.stringify({
    path: request.path,
    pages,
    languages: (request.languages ?? []).filter((value) => typeof value === 'string').slice(0, 8),
  })
  try {
    const stdout = await new Promise<string>((resolve, reject) => {
      const child = execFile(
        binaryPath,
        [],
        { encoding: 'utf8', maxBuffer: MAX_OUTPUT_BYTES, timeout: 120_000 },
        (error, out) => (error ? reject(error) : resolve(out)),
      )
      child.stdin?.end(payload)
    })
    const parsed = JSON.parse(stdout) as { ok?: boolean; pages?: unknown; error?: unknown }
    if (parsed.ok !== true || !Array.isArray(parsed.pages) || !parsed.pages.every(validPage)) {
      return {
        ok: false,
        code: 'execution_failed',
        error: typeof parsed.error === 'string' ? parsed.error : 'PDF OCR returned an invalid result',
      }
    }
    return { ok: true, pages: parsed.pages }
  } catch (error) {
    return {
      ok: false,
      code: 'execution_failed',
      error: error instanceof Error ? error.message : String(error),
    }
  }
}
