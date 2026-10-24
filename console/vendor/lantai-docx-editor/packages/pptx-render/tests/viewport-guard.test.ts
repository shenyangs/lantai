import { describe, expect, it } from 'vitest'
import { makeViewport } from '../src/coords'

const DEFAULT_CX = 9144000
const DEFAULT_CY = 6858000

function assertFiniteViewport(viewport: { widthPx: number; heightPx: number; scale: number }) {
  for (const value of [viewport.widthPx, viewport.heightPx, viewport.scale]) {
    expect(Number.isFinite(value)).toBe(true)
    expect(value).toBeGreaterThan(0)
  }
}

describe('makeViewport degenerate guard', () => {
  it('keeps the normal path unchanged', () => {
    const viewport = makeViewport({ cx: 12192000, cy: 6858000 }, 1280)
    expect(viewport.widthPx).toBe(1280)
    expect(viewport.heightPx).toBeCloseTo(720, 0)
    expect(viewport.scale).toBeCloseTo(1, 6)
  })

  it('falls back to the default slide width for zero cx', () => {
    const guarded = makeViewport({ cx: 0, cy: DEFAULT_CY }, 960)
    const expected = makeViewport({ cx: DEFAULT_CX, cy: DEFAULT_CY }, 960)
    expect(guarded).toEqual(expected)
    assertFiniteViewport(guarded)
  })

  it('keeps negative, NaN and infinite inputs finite', () => {
    const bad = [-1, -9144000, 0, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]
    for (const cx of bad) {
      for (const cy of bad) {
        for (const fit of bad) assertFiniteViewport(makeViewport({ cx, cy }, fit))
      }
    }
  })

  it('falls back to scale 1 for a degenerate fit width', () => {
    const viewport = makeViewport({ cx: DEFAULT_CX, cy: DEFAULT_CY }, 0)
    expect(viewport.widthPx).toBeCloseTo(DEFAULT_CX / 9525, 6)
    expect(viewport.heightPx).toBeCloseTo(DEFAULT_CY / 9525, 6)
    expect(viewport.scale).toBeCloseTo(1, 6)
  })
})
