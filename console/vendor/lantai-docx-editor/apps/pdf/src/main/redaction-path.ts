import { realpathSync, statSync } from 'node:fs'
import { basename, dirname, join, resolve } from 'node:path'

function canonicalPath(path: string): string {
  const absolutePath = resolve(path)
  try {
    return realpathSync.native(absolutePath)
  } catch {
    try {
      return join(realpathSync.native(dirname(absolutePath)), basename(absolutePath))
    } catch {
      return absolutePath
    }
  }
}

/**
 * A secure-redaction copy must never target the source document, including
 * aliases created by symbolic links, hard links, or a symlinked parent path.
 */
export function isSameRedactionCopyPath(sourcePath: string, targetPath: string): boolean {
  if (canonicalPath(sourcePath) === canonicalPath(targetPath)) return true

  try {
    const source = statSync(sourcePath)
    const target = statSync(targetPath)
    return source.dev === target.dev && source.ino === target.ino
  } catch {
    return false
  }
}
