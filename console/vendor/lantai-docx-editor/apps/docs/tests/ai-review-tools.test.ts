import { afterEach, describe, expect, it } from 'vitest'
import { Editor } from '@tiptap/core'
import type { CommentInfo } from '@genoffice/docx-engine'
import { editorExtensions } from '../src/renderer/editor/extensions'
import { executeTool } from '../src/renderer/ai/tools'
import { collectRevisions } from '../src/renderer/editor/revisions'

const NUM_IDS = { bullet: null, ordered: null }
const liveEditors: Editor[] = []

afterEach(() => {
  for (const editor of liveEditors.splice(0)) editor.destroy()
})

function createReviewEditor(): Editor {
  const editor = new Editor({
    element: document.createElement('div'),
    extensions: editorExtensions,
    content: {
      type: 'doc',
      content: [
        {
          type: 'docParagraph',
          attrs: { docxIndex: null },
          content: [
            {
              type: 'text',
              text: 'Current wording',
              marks: [
                {
                  type: 'comment',
                  attrs: { ids: '7' },
                },
                {
                  type: 'ins',
                  attrs: { author: 'Reviewer A', date: '2026-08-10T00:00:00Z' },
                },
              ],
            },
            { type: 'text', text: ' ' },
            {
              type: 'text',
              text: 'obsolete wording',
              marks: [
                {
                  type: 'del',
                  attrs: { author: 'Reviewer B', date: '2026-08-10T00:00:00Z' },
                },
              ],
            },
          ],
        },
      ],
    },
  })
  liveEditors.push(editor)
  return editor
}

describe('Word review-aware agent tools', () => {
  it('reads revisions, comments and the real anchored text without mutating the document', async () => {
    const editor = createReviewEditor()
    const comments: CommentInfo[] = [
      {
        id: '7',
        author: 'Reviewer A',
        initials: 'RA',
        date: '2026-08-10T00:00:00Z',
        text: 'Please make this clearer.',
        done: false,
      },
    ]
    const before = editor.getJSON()
    const result = await executeTool(
      editor,
      { id: 'review', name: 'read_review_items', input: {} },
      NUM_IDS,
      undefined,
      undefined,
      { getComments: () => comments },
    )
    const payload = JSON.parse(result.output)
    expect(payload.summary).toEqual({
      revisionCount: 2,
      commentCount: 1,
      unresolvedCommentCount: 1,
    })
    expect(payload.revisions).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ kind: 'ins', author: 'Reviewer A', text: 'Current wording' }),
        expect.objectContaining({ kind: 'del', author: 'Reviewer B', text: 'obsolete wording' }),
      ]),
    )
    expect(payload.comments[0]).toEqual(
      expect.objectContaining({
        id: '7',
        text: 'Please make this clearer.',
        anchoredText: 'Current wording',
      }),
    )
    expect(result.mutated).toBe(false)
    expect(editor.getJSON()).toEqual(before)
  })

  it('accepts only the explicitly selected author and reports the remaining revisions', async () => {
    const editor = createReviewEditor()
    const result = await executeTool(
      editor,
      {
        id: 'apply',
        name: 'apply_revision_action',
        input: { action: 'accept', scope: 'author', author: 'Reviewer A' },
      },
      NUM_IDS,
    )
    expect(result.mutated).toBe(true)
    expect(result.output).toContain('接受了 1 处修订')
    expect(collectRevisions(editor.state.doc)).toEqual([
      expect.objectContaining({ kind: 'del', author: 'Reviewer B' }),
    ])
    expect(editor.state.doc.textContent).toContain('Current wording')
  })

  it('refuses an implicit broad scope instead of silently changing revisions', async () => {
    const editor = createReviewEditor()
    const before = editor.getJSON()
    const result = await executeTool(
      editor,
      {
        id: 'apply',
        name: 'apply_revision_action',
        input: { action: 'reject', scope: 'author' },
      },
      NUM_IDS,
    )
    expect(result.isError).toBe(true)
    expect(result.mutated).toBe(false)
    expect(editor.getJSON()).toEqual(before)
  })
})
