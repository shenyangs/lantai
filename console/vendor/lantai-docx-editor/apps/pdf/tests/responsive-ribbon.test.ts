import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const appSource = readFileSync(resolve(__dirname, '../src/renderer/App.tsx'), 'utf8')
const styles = readFileSync(resolve(__dirname, '../src/renderer/styles.css'), 'utf8')

describe('responsive PDF ribbon', () => {
  it('switches to a compact icon ribbon before actions can be clipped', () => {
    expect(styles).toContain('@media (max-width: 2100px)')
    expect(styles).toMatch(/\.rb-big,\s*\n\s*\.rb-big\.rb-ai[\s\S]*?font-size:\s*0/)
    expect(styles).toMatch(/\.rb-big,\s*\n\s*\.rb-big\.rb-ai[\s\S]*?width:\s*34px/)
    expect(styles).toContain('padding-inline: 6px')
    expect(styles).toContain('scroll-snap-type: x proximity')
  })

  it('keeps accessible tooltips for page-management actions', () => {
    for (const key of ['rotateLeft', 'rotateRight', 'deletePage', 'extractPage', 'insertPdf']) {
      expect(appSource).toContain(`title={t('${key}')}`)
    }
  })
})
