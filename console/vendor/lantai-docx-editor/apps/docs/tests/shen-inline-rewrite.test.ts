import { act, createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { Editor, Node } from '@tiptap/core'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ShenInlineRewrite } from '../src/renderer/components/ShenInlineRewrite'
import { ShenRewritePanel } from '../src/renderer/components/ShenRewritePanel'
import type { ShenRewriteProposalState } from '../src/renderer/shen-rewrite-proposals'

function createEditor(): Editor {
  const TestDocument = Node.create({
    name: 'doc',
    topNode: true,
    content: 'docParagraph+',
  })
  const TestParagraph = Node.create({
    name: 'docParagraph',
    group: 'block',
    content: 'inline*',
    parseHTML: () => [{ tag: 'p' }],
    renderHTML: () => ['p', 0],
  })
  const TestText = Node.create({ name: 'text', group: 'inline' })
  const editor = new Editor({
    element: document.createElement('div'),
    extensions: [TestDocument, TestParagraph, TestText],
    content: '<p>原文需要写得更清楚，也要保留事实。</p>',
  })
  editor.view.coordsAtPos = vi.fn(() => ({
    left: 460,
    right: 460,
    top: 260,
    bottom: 282,
  }))
  return editor
}

function mount(editor: Editor, overrides: Record<string, unknown> = {}) {
  const props = {
    editor,
    enabled: true,
    proposal: null,
    busy: false,
    onSubmit: vi.fn(() => true),
    onAccept: vi.fn(),
    onReject: vi.fn(),
    onDraftChange: vi.fn(),
    onRefine: vi.fn(),
    onRetry: vi.fn(),
    onOpenPanel: vi.fn(),
    ...overrides,
  }
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => root.render(createElement(ShenInlineRewrite, props as never)))
  return {
    container,
    props,
    rerender: (next: Record<string, unknown>) =>
      act(() => root.render(createElement(ShenInlineRewrite, { ...props, ...next } as never))),
    unmount: () => {
      act(() => root.unmount())
      container.remove()
    },
  }
}

function click(element: Element) {
  act(() => element.dispatchEvent(new MouseEvent('click', { bubbles: true })))
}

function mountPanel(proposals: ShenRewriteProposalState[], overrides: Record<string, unknown> = {}) {
  const props = {
    proposals,
    busy: false,
    onAccept: vi.fn(),
    onReject: vi.fn(),
    onDraftChange: vi.fn(),
    onRefine: vi.fn(),
    onRetry: vi.fn(),
    onSelectVersion: vi.fn(),
    ...overrides,
  }
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => root.render(createElement(ShenRewritePanel, props as never)))
  return {
    container,
    props,
    unmount: () => {
      act(() => root.unmount())
      container.remove()
    },
  }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('ShenInlineRewrite', () => {
  it('shows a lightweight entry only after a non-empty selection stabilizes', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const view = mount(editor)

    expect(view.container.querySelector('.shen-inline-trigger')).toBeNull()
    act(() => vi.advanceTimersByTime(140))
    expect(view.container.querySelector('.shen-inline-trigger')?.textContent).toContain(
      '沈行知随改',
    )

    act(() => editor.commands.setTextSelection(4))
    act(() => vi.advanceTimersByTime(140))
    expect(view.container.querySelector('.shen-inline-trigger')).toBeNull()

    view.unmount()
    editor.destroy()
  })

  it('keeps the document untouched while a quick instruction is submitted', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const original = editor.state.doc.toJSON()
    const onSubmit = vi.fn(
      (_snapshot: unknown, _instruction: string, _mode: 'inline' | 'panel') => true,
    )
    const view = mount(editor, { onSubmit })
    act(() => vi.advanceTimersByTime(140))

    click(view.container.querySelector('.shen-inline-trigger')!)
    expect(view.container.querySelector('[role="dialog"]')).not.toBeNull()
    click(
      [...view.container.querySelectorAll('button')].find(
        (button) => button.textContent === '精简这段文字',
      )!,
    )
    click(
      [...view.container.querySelectorAll('button')].find(
        (button) => button.textContent === '立即建议',
      )!,
    )

    expect(onSubmit).toHaveBeenCalledOnce()
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ from: 1, to: 7 })
    expect(onSubmit.mock.calls[0][1]).toBe('精简这段文字')
    expect(onSubmit.mock.calls[0][2]).toBe('inline')
    expect(editor.state.doc.toJSON()).toEqual(original)

    view.unmount()
    editor.destroy()
  })

  it('closes an open composer when the selection changes and never submits the stale range', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const onSubmit = vi.fn(
      (_snapshot: unknown, _instruction: string, _mode: 'inline' | 'panel') => true,
    )
    const view = mount(editor, { onSubmit })
    act(() => vi.advanceTimersByTime(140))
    click(view.container.querySelector('.shen-inline-trigger')!)
    expect(view.container.querySelector('[role="dialog"]')).not.toBeNull()

    act(() => editor.commands.setTextSelection({ from: 8, to: 12 }))

    expect(view.container.querySelector('[role="dialog"]')).toBeNull()
    expect(onSubmit).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(140))
    expect(view.container.querySelector('.shen-inline-trigger')).not.toBeNull()

    view.unmount()
    editor.destroy()
  })

  it('closes an open composer when the selection collapses', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const view = mount(editor)
    act(() => vi.advanceTimersByTime(140))
    click(view.container.querySelector('.shen-inline-trigger')!)

    act(() => editor.commands.setTextSelection(4))

    expect(view.container.querySelector('[role="dialog"]')).toBeNull()
    act(() => vi.advanceTimersByTime(140))
    expect(view.container.querySelector('.shen-inline-trigger')).toBeNull()

    view.unmount()
    editor.destroy()
  })

  it('accurately labels panel generation and submits it as an immediate panel request', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const onSubmit = vi.fn(
      (_snapshot: unknown, _instruction: string, _mode: 'inline' | 'panel') => true,
    )
    const view = mount(editor, { onSubmit })
    act(() => vi.advanceTimersByTime(140))
    click(view.container.querySelector('.shen-inline-trigger')!)
    click(
      [...view.container.querySelectorAll('button')].find(
        (button) => button.textContent === '润色这段文字',
      )!,
    )

    const panelButton = [...view.container.querySelectorAll('button')].find(
      (button) => button.textContent === '生成并转到清单',
    )!
    expect(panelButton).toBeTruthy()
    click(panelButton)

    expect(onSubmit).toHaveBeenCalledOnce()
    expect(onSubmit.mock.calls[0][2]).toBe('panel')
    expect(view.container.textContent).not.toContain('加入随改清单')

    view.unmount()
    editor.destroy()
  })

  it('does not expose the entry in read-only or protected states', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const view = mount(editor, { enabled: false })
    act(() => vi.advanceTimersByTime(140))

    expect(view.container.querySelector('.shen-inline-trigger')).toBeNull()

    view.unmount()
    editor.destroy()
  })

  it('renders a nearby result card with explicit accept, reject, refine, and panel actions', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const snapshot = {
      from: 1,
      to: 7,
      selectedText: editor.state.doc.textBetween(1, 7, '\n', '\n'),
      documentBefore: '',
      documentAfter: '',
      document: editor.state.doc,
    }
    const proposal: ShenRewriteProposalState = {
      id: 'proposal-1',
      snapshot,
      phase: 'ready',
      versions: [{ replacementText: '更清楚的表达。', summary: '保留事实，压缩句子。' }],
      versionIndex: 0,
    }
    const onAccept = vi.fn()
    const onReject = vi.fn()
    const onOpenPanel = vi.fn()
    const view = mount(editor, { proposal, onAccept, onReject, onOpenPanel })

    expect(view.container.querySelector('.shen-inline-card')?.textContent).toContain('建议已就绪')
    click(
      [...view.container.querySelectorAll('button')].find(
        (button) => button.textContent === '接受替换',
      )!,
    )
    click(
      [...view.container.querySelectorAll('button')].find(
        (button) => button.textContent === '拒绝',
      )!,
    )
    click(
      [...view.container.querySelectorAll('button')].find(
        (button) => button.textContent === '转到随改清单',
      )!,
    )

    expect(onAccept).toHaveBeenCalledWith('proposal-1')
    expect(onReject).toHaveBeenCalledWith('proposal-1')
    expect(onOpenPanel).toHaveBeenCalledOnce()

    view.unmount()
    editor.destroy()
  })

  it('collapses a result into the rewrite list without treating it as a rejection', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const proposal: ShenRewriteProposalState = {
      id: 'proposal-1',
      snapshot: {
        from: 1,
        to: 7,
        selectedText: editor.state.doc.textBetween(1, 7, '\n', '\n'),
        documentBefore: '',
        documentAfter: '',
        document: editor.state.doc,
      },
      phase: 'ready',
      versions: [{ replacementText: '更清楚的表达。', summary: '' }],
      versionIndex: 0,
    }
    const onReject = vi.fn()
    const onOpenPanel = vi.fn()
    const view = mount(editor, { proposal, onReject, onOpenPanel })

    click(view.container.querySelector<HTMLButtonElement>('[aria-label="收起到随改清单"]')!)

    expect(onReject).not.toHaveBeenCalled()
    expect(onOpenPanel).toHaveBeenCalledOnce()

    view.unmount()
    editor.destroy()
  })

  it('keeps a failed suggestion visible without exposing an accept action', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const proposal: ShenRewriteProposalState = {
      id: 'proposal-1',
      snapshot: {
        from: 1,
        to: 7,
        selectedText: editor.state.doc.textBetween(1, 7, '\n', '\n'),
        documentBefore: '',
        documentAfter: '',
        document: editor.state.doc,
      },
      phase: 'failed',
      versions: [],
      versionIndex: 0,
      error: '模型暂时不可用',
    }
    const onRetry = vi.fn()
    const view = mount(editor, { proposal, onRetry })

    expect(view.container.querySelector('.shen-inline-card')?.textContent).toContain('随改失败')
    expect(view.container.querySelector('.shen-inline-card')?.textContent).toContain(
      '模型暂时不可用',
    )
    expect(
      [...view.container.querySelectorAll('button')].some(
        (button) => button.textContent === '接受替换',
      ),
    ).toBe(false)
    click(
      [...view.container.querySelectorAll('button')].find(
        (button) => button.textContent === '重试生成',
      )!,
    )
    expect(onRetry).toHaveBeenCalledWith('proposal-1')

    view.unmount()
    editor.destroy()
  })

  it('keeps the prior version usable and exposes retry after refinement fails', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const proposal: ShenRewriteProposalState = {
      id: 'proposal-1',
      snapshot: {
        from: 1,
        to: 7,
        selectedText: editor.state.doc.textBetween(1, 7, '\n', '\n'),
        documentBefore: '',
        documentAfter: '',
        document: editor.state.doc,
      },
      phase: 'ready',
      versions: [{ replacementText: '上一版仍然可用。', summary: '' }],
      versionIndex: 0,
      request: { instruction: '再短一点', currentDraft: '上一版仍然可用。' },
      error: '继续修改失败',
    }
    const onRetry = vi.fn()
    const onAccept = vi.fn()
    const view = mount(editor, { proposal, onRetry, onAccept })

    expect(view.container.textContent).toContain('继续修改失败')
    click(
      [...view.container.querySelectorAll('button')].find(
        (button) => button.textContent === '重试继续修改',
      )!,
    )
    click(
      [...view.container.querySelectorAll('button')].find(
        (button) => button.textContent === '接受替换',
      )!,
    )

    expect(onRetry).toHaveBeenCalledWith('proposal-1')
    expect(onAccept).toHaveBeenCalledWith('proposal-1')
    view.unmount()
    editor.destroy()
  })

  it('exposes the saved refinement retry from the rewrite panel too', () => {
    const editor = createEditor()
    const proposal: ShenRewriteProposalState = {
      id: 'proposal-1',
      snapshot: {
        from: 1,
        to: 7,
        selectedText: editor.state.doc.textBetween(1, 7, '\n', '\n'),
        documentBefore: '',
        documentAfter: '',
        document: editor.state.doc,
      },
      phase: 'ready',
      versions: [{ replacementText: '上一版仍然可用。', summary: '' }],
      versionIndex: 0,
      request: { instruction: '再短一点', currentDraft: '上一版仍然可用。' },
      error: '继续修改失败',
    }
    const onRetry = vi.fn()
    const view = mountPanel([proposal], { onRetry })

    const retry = [...view.container.querySelectorAll('button')].find(
      (button) => button.textContent === '重试继续修改',
    )!
    expect(retry).toBeTruthy()
    click(retry)
    expect(onRetry).toHaveBeenCalledWith('proposal-1')

    view.unmount()
    editor.destroy()
  })

  it('closes the composer on Escape without changing the selection', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 7 })
    const view = mount(editor)
    act(() => vi.advanceTimersByTime(140))
    click(view.container.querySelector('.shen-inline-trigger')!)

    act(() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })))

    expect(view.container.querySelector('[role="dialog"]')).toBeNull()
    expect(editor.state.selection).toMatchObject({ from: 1, to: 7 })

    view.unmount()
    editor.destroy()
  })
})
