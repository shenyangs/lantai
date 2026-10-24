import { linkSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { isSameRedactionCopyPath } from '../src/main/redaction-path'

const temporaryDirectories: string[] = []

function temporaryDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), 'lantai-pdf-redaction-path-'))
  temporaryDirectories.push(directory)
  return directory
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true })
  }
})

describe('isSameRedactionCopyPath', () => {
  it('rejects lexical aliases of the source PDF', () => {
    const directory = temporaryDirectory()
    const source = join(directory, 'source.pdf')
    writeFileSync(source, 'source')

    expect(isSameRedactionCopyPath(source, source)).toBe(true)
    expect(isSameRedactionCopyPath(source, join(directory, '.', 'source.pdf'))).toBe(true)
  })

  it('rejects symbolic-link and hard-link aliases of the source PDF', () => {
    const directory = temporaryDirectory()
    const source = join(directory, 'source.pdf')
    const symbolicLink = join(directory, 'symbolic.pdf')
    const hardLink = join(directory, 'hard.pdf')
    writeFileSync(source, 'source')
    symlinkSync(source, symbolicLink)
    linkSync(source, hardLink)

    expect(isSameRedactionCopyPath(source, symbolicLink)).toBe(true)
    expect(isSameRedactionCopyPath(source, hardLink)).toBe(true)
  })

  it('canonicalizes the parent directory for a path reached through a symlink', () => {
    const directory = temporaryDirectory()
    const realDirectory = join(directory, 'real')
    const aliasDirectory = join(directory, 'alias')
    mkdirSync(realDirectory)
    symlinkSync(realDirectory, aliasDirectory)
    const source = join(realDirectory, 'source.pdf')
    writeFileSync(source, 'source')

    expect(isSameRedactionCopyPath(source, join(aliasDirectory, 'source.pdf'))).toBe(true)
  })

  it('allows a genuinely different output path', () => {
    const directory = temporaryDirectory()
    const source = join(directory, 'source.pdf')
    writeFileSync(source, 'source')

    expect(isSameRedactionCopyPath(source, join(directory, 'redacted-copy.pdf'))).toBe(false)
  })
})
