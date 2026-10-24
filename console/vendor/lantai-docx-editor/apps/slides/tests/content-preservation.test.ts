import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PlacedBox, RenderSlide, ShapeRenderNode } from '@genoffice/pptx-render'
import type { AgentToolCall } from '../src/shared/ipc'
import {
  createSlidesSkill,
  deckContentFingerprint,
  type DeckAccess,
} from '../src/renderer/ai/slides-skill'

const box: PlacedBox = {
  x: 10,
  y: 20,
  w: 300,
  h: 80,
  rotationDeg: 0,
  flipH: false,
  flipV: false,
  centerX: 160,
  centerY: 60,
}

const textNode = (text: string): ShapeRenderNode => ({
  id: 'title',
  sourceId: 'title',
  type: 'shape',
  box,
  fill: { kind: 'none' },
  text: {
    lines: [
      {
        runs: [
          {
            text,
            x: 0,
            baselineY: 20,
            fontFamily: 'Arial',
            fontSizePx: 24,
            color: '#000000',
            bold: false,
            italic: false,
            underline: false,
            widthPx: 100,
          },
        ],
        top: 0,
        height: 28,
      },
    ],
    insets: { l: 0, t: 0, r: 0, b: 0 },
    anchor: 'top',
    fontScale: 1,
    wrap: true,
    contentHeight: 28,
  },
})

const slide = (text: string): RenderSlide =>
  ({
    widthPx: 1280,
    heightPx: 720,
    scale: 1,
    background: { kind: 'solid', color: '#FFFFFF' },
    nodes: [textNode(text)],
  }) as RenderSlide

const call = (name: string, input: Record<string, unknown>): AgentToolCall => ({
  id: 'tool-1',
  name,
  input,
})

function access(overrides: Partial<DeckAccess> = {}): DeckAccess {
  return {
    getSlides: () => [slide('1,962 条 / 1,817 万')],
    getCurrent: () => 0,
    getSelectedIds: () => [],
    preserveContent: () => true,
    applySlide: vi.fn(),
    applyDeck: vi.fn(),
    fitWidthPx: 1280,
    ...overrides,
  }
}

beforeEach(() => {
  ;(window as any).slidesApi = {
    editText: vi.fn(),
    beginHistoryBatch: vi.fn(async () => true),
    endHistoryBatch: vi.fn(async () => 1),
    editChart: vi.fn(),
  }
})

describe('visual-only content preservation', () => {
  it('fingerprint ignores geometry/style but detects visible-copy drift', () => {
    const original = slide('1,962 条 / 1,817 万')
    const visualOnly = slide('1,962 条 / 1,817 万')
    visualOnly.nodes[0]!.box.x = 500
    const corrupted = slide('条,962 / 1,817万')

    expect(deckContentFingerprint([visualOnly])).toBe(deckContentFingerprint([original]))
    expect(deckContentFingerprint([corrupted])).not.toBe(deckContentFingerprint([original]))
  })

  it('fingerprint ignores renderer ids, grouping and z-order during visual edits', () => {
    const original = slide('same copy')
    const regrouped = slide('same copy')
    regrouped.nodes[0]!.sourceId = 'regenerated-render-id'
    regrouped.nodes.unshift({
      id: 'decoration',
      sourceId: 'decoration',
      type: 'shape',
      box,
      fill: { kind: 'solid', color: '#112233' },
    } as ShapeRenderNode)

    expect(deckContentFingerprint([regrouped])).toBe(deckContentFingerprint([original]))
  })

  it('does not treat automatic line wrapping as copy drift', () => {
    const original = slide('核心主线：从热点传播到议题经营')
    const rewrapped = slide('')
    const originalText = original.nodes[0] as ShapeRenderNode
    const rewrappedText = rewrapped.nodes[0] as ShapeRenderNode
    rewrappedText.text!.lines = [
      {
        ...rewrappedText.text!.lines[0]!,
        paraStart: true,
        trailingSpace: true,
        runs: [{ ...rewrappedText.text!.lines[0]!.runs[0]!, text: '核心主线：从热点传播到' }],
      },
      {
        ...rewrappedText.text!.lines[0]!,
        paraStart: false,
        runs: [{ ...rewrappedText.text!.lines[0]!.runs[0]!, text: '议题经营' }],
      },
    ]
    originalText.text!.lines[0]!.runs[0]!.text = '核心主线：从热点传播到 议题经营'

    expect(deckContentFingerprint([rewrapped])).toBe(deckContentFingerprint([original]))
  })

  it('still detects an authored paragraph break after ignoring automatic wrapping', () => {
    const oneParagraph = slide('Alpha Beta')
    const twoParagraphs = slide('')
    const text = twoParagraphs.nodes[0] as ShapeRenderNode
    text.text!.lines = [
      {
        ...text.text!.lines[0]!,
        paraStart: true,
        runs: [{ ...text.text!.lines[0]!.runs[0]!, text: 'Alpha' }],
      },
      {
        ...text.text!.lines[0]!,
        paraStart: true,
        runs: [{ ...text.text!.lines[0]!.runs[0]!, text: 'Beta' }],
      },
    ]

    expect(deckContentFingerprint([twoParagraphs])).not.toBe(
      deckContentFingerprint([oneParagraph]),
    )
  })

  it('blocks direct text replacement before IPC', async () => {
    const result = await createSlidesSkill(access()).executeTool!(
      call('set_element_text', {
        slideIndex: 0,
        sourceId: 'title',
        paragraphs: [{ text: '条,962' }],
      }),
    )

    expect(result.isError).toBe(true)
    expect(result.mutated).toBe(false)
    expect((window as any).slidesApi.editText).not.toHaveBeenCalled()
  })

  it('rejects an entire layout script when it contains setText', async () => {
    const result = await createSlidesSkill(access()).executeTool!(
      call('execute_slide_script', {
        slideIndex: 0,
        code: `moveBy('title', 20, 0); setText('title', '条,962');`,
      }),
    )

    expect(result.isError).toBe(true)
    expect(result.mutated).toBe(false)
    expect((window as any).slidesApi.beginHistoryBatch).not.toHaveBeenCalled()
  })

  it('blocks page regeneration and chart data edits while allowing visual chart styling', async () => {
    const regenerateSlide = vi.fn()
    const skill = createSlidesSkill(
      access({
        regenerateSlide,
        generatePageCloud: vi.fn(),
      }),
    )

    const regeneration = await skill.executeTool!(
      call('regenerate_slide', { slideIndex: 0, brief: 'make it prettier' }),
    )
    const dataEdit = await skill.executeTool!(
      call('edit_chart', {
        slideIndex: 0,
        sourceId: 'chart-1',
        series: [{ name: 'A', values: [1962] }],
      }),
    )

    expect(regeneration.isError).toBe(true)
    expect(dataEdit.isError).toBe(true)
    expect(regenerateSlide).not.toHaveBeenCalled()
    expect((window as any).slidesApi.editChart).not.toHaveBeenCalled()
  })
})
