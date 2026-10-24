import { describe, expect, it, vi } from 'vitest'
import type { PlacedBox, RenderSlide, ShapeRenderNode } from '@genoffice/pptx-render'
import { createSlidesSkill, type DeckAccess } from '../src/renderer/ai/slides-skill'

const box = (x: number, y: number, w: number, h: number): PlacedBox => ({
  x,
  y,
  w,
  h,
  rotationDeg: 0,
  flipH: false,
  flipV: false,
  centerX: x + w / 2,
  centerY: y + h / 2,
})

const title: ShapeRenderNode = {
  id: 'title',
  sourceId: 'title',
  type: 'shape',
  box: box(80, 60, 1120, 100),
  fill: { kind: 'none' },
  placeholder: 'title',
  text: {
    lines: [],
    insets: { l: 8, t: 4, r: 8, b: 4 },
    anchor: 'top',
    fontScale: 1,
    wrap: true,
    contentHeight: 0,
  },
}

const slide: RenderSlide = {
  widthPx: 1280,
  heightPx: 720,
  scale: 1,
  background: { kind: 'solid', color: '#FFFFFF' },
  nodes: [title],
}

function access(overrides: Partial<DeckAccess> = {}): DeckAccess {
  return {
    getSlides: () => [slide],
    getCurrent: () => 0,
    getSelectedIds: () => [],
    applySlide: () => {},
    applyDeck: () => {},
    fitWidthPx: 1280,
    getDesignContext: async () => ({
      slideIndex: 0,
      slidePath: 'ppt/slides/slide1.xml',
      layout: {
        path: 'ppt/slideLayouts/slideLayout1.xml',
        name: 'Title Slide',
        layoutType: 'title',
      },
      master: { path: 'ppt/slideMasters/slideMaster1.xml', name: 'Office' },
      theme: {
        path: 'ppt/theme/theme1.xml',
        colors: { accent1: '#4472C4' },
        majorFont: 'Aptos Display',
        minorFont: 'Aptos',
      },
      placeholders: [{ sourceId: 'title', type: 'title', name: 'Title 1' }],
      availableLayouts: [
        {
          path: 'ppt/slideLayouts/slideLayout1.xml',
          name: 'Title Slide',
          layoutType: 'title',
          placeholders: [],
        },
      ],
      availableMasters: [{ path: 'ppt/slideMasters/slideMaster1.xml', name: 'Office' }],
      transition: 'fade',
      animations: [
        {
          sourceId: 'title',
          targetName: 'Title 1',
          effect: 'fade',
          trigger: 'onClick',
          durationMs: 500,
          delayMs: 0,
        },
      ],
    }),
    ...overrides,
  }
}

const call = (name: string, input: Record<string, unknown>) => ({ id: '1', name, input })

describe('PPT design semantics tools', () => {
  it('reads the live layout, master, theme, placeholders and motion chain', async () => {
    const result = await createSlidesSkill(access()).executeTool!(
      call('read_design_context', { slideIndex: 0 }),
    )
    const context = JSON.parse(result.output)
    expect(result.mutated).toBe(false)
    expect(context.layout.name).toBe('Title Slide')
    expect(context.master.name).toBe('Office')
    expect(context.theme.colors.accent1).toBe('#4472C4')
    expect(context.animations[0]).toEqual(expect.objectContaining({ sourceId: 'title' }))
  })

  it('allows only a live layout path and returns the post-apply audit', async () => {
    const setSlideLayout = vi.fn().mockResolvedValue({ ok: true })
    const skill = createSlidesSkill(access({ setSlideLayout }))
    const rejected = await skill.executeTool!(
      call('set_slide_layout', { slideIndex: 0, layoutPath: 'ppt/unknown.xml' }),
    )
    expect(rejected.isError).toBe(true)
    expect(setSlideLayout).not.toHaveBeenCalled()

    const applied = await skill.executeTool!(
      call('set_slide_layout', {
        slideIndex: 0,
        layoutPath: 'ppt/slideLayouts/slideLayout1.xml',
      }),
    )
    expect(applied.mutated).toBe(true)
    expect(applied.output).toContain('layout-audit')
    expect(setSlideLayout).toHaveBeenCalledWith(0, 'ppt/slideLayouts/slideLayout1.xml')
  })

  it('validates animation targets and writes a bounded native motion batch', async () => {
    const setSlideMotion = vi.fn().mockResolvedValue({ ok: true })
    const skill = createSlidesSkill(access({ setSlideMotion }))
    const rejected = await skill.executeTool!(
      call('set_slide_motion', {
        slideIndex: 0,
        animations: [
          {
            sourceId: 'missing',
            effect: 'fade',
            trigger: 'onClick',
            durationMs: 500,
            delayMs: 0,
          },
        ],
      }),
    )
    expect(rejected.isError).toBe(true)
    expect(setSlideMotion).not.toHaveBeenCalled()

    const applied = await skill.executeTool!(
      call('set_slide_motion', {
        slideIndex: 0,
        transition: 'morph',
        animations: [
          {
            sourceId: 'title',
            effect: 'fade',
            trigger: 'onClick',
            durationMs: 500,
            delayMs: 100,
          },
        ],
      }),
    )
    expect(applied.mutated).toBe(true)
    expect(setSlideMotion).toHaveBeenCalledWith({
      slideIndex: 0,
      transition: 'morph',
      animations: [
        {
          sourceId: 'title',
          effect: 'fade',
          trigger: 'onClick',
          durationMs: 500,
          delayMs: 100,
        },
      ],
    })
  })

  it('runs the deterministic geometry audit without changing the slide', async () => {
    const result = await createSlidesSkill(access()).executeTool!(
      call('audit_slide_layout', { slideIndex: 0 }),
    )
    const report = JSON.parse(result.output)
    expect(result.mutated).toBe(false)
    expect(report.passed).toBe(true)
    expect(report.issues).toEqual([])
  })
})
