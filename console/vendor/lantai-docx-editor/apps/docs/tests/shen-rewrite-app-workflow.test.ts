import { Editor, Node } from '@tiptap/core'
import { UndoRedo } from '@tiptap/extensions'
import { describe, expect, it } from 'vitest'
import { DelMark, InsMark } from '../src/renderer/editor/marks'
import { collectRevisions, TrackChangesExtension } from '../src/renderer/editor/revisions'
import { captureDocxRewriteSelection } from '../src/renderer/lantai-double-write'
import { appendShenRewriteVersion } from '../src/renderer/shen-rewrite-proposals'
import {
  acceptShenRewriteProposal,
  createShenRewriteProposal,
  inlineProposalIdForMode,
  isShenRewriteStartSnapshotCurrent,
  retryRequestForProposal,
} from '../src/renderer/shen-rewrite-workflow'

function createEditor() {
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
  return new Editor({
    element: document.createElement('div'),
    extensions: [
      TestDocument,
      TestParagraph,
      TestText,
      InsMark,
      DelMark,
      TrackChangesExtension,
      UndoRedo,
    ],
    content: '<p>需要改写的原文</p>',
  })
}

describe('App Shen Xingzhi rewrite workflow', () => {
  it('routes inline and panel requests without treating panel generation as a queue', () => {
    expect(inlineProposalIdForMode('proposal-1', 'inline')).toBe('proposal-1')
    expect(inlineProposalIdForMode('proposal-1', 'panel')).toBeNull()
  })

  it('rejects an App start snapshot after the selection moves or collapses', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 5 })
    const snapshot = captureDocxRewriteSelection(editor)!

    expect(isShenRewriteStartSnapshotCurrent(editor, snapshot)).toBe(true)
    editor.commands.setTextSelection({ from: 6, to: 9 })
    expect(editor.state.doc).toBe(snapshot.document)
    expect(isShenRewriteStartSnapshotCurrent(editor, snapshot)).toBe(false)
    editor.commands.setTextSelection(3)
    expect(isShenRewriteStartSnapshotCurrent(editor, snapshot)).toBe(false)
    editor.destroy()
  })

  it('keeps the original request for a safe retry after generation fails', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 5 })
    const snapshot = captureDocxRewriteSelection(editor)!
    const failed = {
      ...createShenRewriteProposal('proposal-1', snapshot, '精简这段文字'),
      phase: 'failed' as const,
      error: '网络错误',
    }

    expect(retryRequestForProposal(failed)).toEqual({
      snapshot,
      instruction: '精简这段文字',
      currentDraft: '',
    })
    editor.destroy()
  })

  it('keeps the refinement instruction and draft retryable after refinement fails', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 5 })
    const snapshot = captureDocxRewriteSelection(editor)!
    const failedRefinement = {
      ...createShenRewriteProposal('proposal-1', snapshot, '精简这段文字'),
      phase: 'ready' as const,
      versions: [{ replacementText: '第一版', summary: '' }],
      error: '网络错误',
      request: {
        instruction: '再短一点',
        currentDraft: '第一版',
      },
    }

    expect(retryRequestForProposal(failedRefinement)).toEqual({
      snapshot,
      instruction: '再短一点',
      currentDraft: '第一版',
    })
    editor.destroy()
  })

  it('accepts a ready suggestion as one undoable Shen Xingzhi revision operation', () => {
    const editor = createEditor()
    editor.commands.setTextSelection({ from: 1, to: 5 })
    const snapshot = captureDocxRewriteSelection(editor)!
    const running = createShenRewriteProposal('proposal-1', snapshot, '精简这段文字')
    const ready = appendShenRewriteVersion([running], 'proposal-1', {
      replacementText: '精简原文',
      summary: '压缩表达',
    })[0]

    editor.commands.setTextSelection(8)
    expect(acceptShenRewriteProposal(editor, ready)).toBe('applied')
    expect(collectRevisions(editor.state.doc).map((revision) => revision.author)).toEqual([
      '沈行知',
      '沈行知',
    ])

    expect(editor.commands.undo()).toBe(true)
    expect(editor.state.doc.textContent).toBe('需要改写的原文')
    expect(collectRevisions(editor.state.doc)).toEqual([])
    editor.destroy()
  })
})
