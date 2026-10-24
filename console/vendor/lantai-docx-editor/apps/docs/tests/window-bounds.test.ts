import { describe, expect, it } from 'vitest'
import { fitDocsWindowToWorkArea } from '../src/main/window-bounds'

describe('fitDocsWindowToWorkArea', () => {
  it('keeps the document drag bar below the macOS menu bar', () => {
    expect(
      fitDocsWindowToWorkArea({ x: 44, y: 33, width: 1468, height: 949 }),
    ).toEqual({ x: 98, y: 57, width: 1360, height: 900 })
  })

  it('shrinks the editor on a smaller work area instead of placing chrome off-screen', () => {
    expect(
      fitDocsWindowToWorkArea({ x: 0, y: 25, width: 1200, height: 775 }),
    ).toEqual({ x: 16, y: 41, width: 1168, height: 743 })
  })
})
