import { describe, expect, it } from 'vitest'
import { Editor, Node } from '@tiptap/core'
import { UndoRedo } from '@tiptap/extensions'
import { parseDocx, saveDocx } from '@genoffice/docx-engine'
import { DelMark, InsMark } from '../src/renderer/editor/marks'
import { editorExtensions } from '../src/renderer/editor/extensions'
import {
  blocksToPmDoc,
  pmDocToSavePlan,
  type PmNode as PmJson,
} from '../src/renderer/editor/convert'
import {
  applyRevisionsBy,
  collectRevisions,
  TrackChangesExtension,
} from '../src/renderer/editor/revisions'
import {
  applyDocxRewriteDirectly,
  applyDocxRewriteAsRevision,
  captureDocxRewriteSelection,
  isDocxRewriteSelectionCurrent,
  LANTAI_REVISION_AUTHOR,
  remapDocxRewriteSelection,
} from '../src/renderer/lantai-double-write'
import {
  addShenRewriteProposal,
  appendShenRewriteVersion,
  editShenRewriteDraft,
  selectShenRewriteVersion,
  type ShenRewriteProposalState,
} from '../src/renderer/shen-rewrite-proposals'
import { buildDocx } from '../../../packages/docx-engine/tests/helpers/build-docx'

function createTestEditor(content: string | Record<string, unknown> = '<p>Hello world</p>') {
  const TestDocument = Node.create({
    name: 'doc',
    topNode: true,
    content: 'docParagraph+',
  })
  const TestParagraph = Node.create({
    name: 'docParagraph',
    group: 'block',
    content: 'inline*',
    addAttributes: () => ({
      docxIndex: { default: null },
      blockRevision: { default: null },
    }),
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
    content,
  })
}

describe('captureDocxRewriteSelection', () => {
  it('captures the selected text with the complete live document around it', () => {
    const doc = {
      content: { size: 2_000 },
      textBetween: (from: number, to: number) => `${from}:${to}`,
    }
    const editor = {
      state: {
        doc,
        selection: { from: 900, to: 930, empty: false },
      },
    }

    const snapshot = captureDocxRewriteSelection(editor as never)

    expect(snapshot).toMatchObject({
      from: 900,
      to: 930,
      selectedText: '900:930',
      documentBefore: '0:900',
      documentAfter: '930:2000',
      document: doc,
    })
  })
})

describe('Shen rewrite proposal queue', () => {
  it('keeps multiple non-overlapping cards and rejects an overlapping selection', () => {
    const document = {} as never
    const selection = (from: number, to: number, selectedText: string) => ({
      from,
      to,
      selectedText,
      documentBefore: '',
      documentAfter: '',
      document,
    })
    const first: ShenRewriteProposalState = {
      id: 'first',
      snapshot: selection(2, 8, 'first'),
      phase: 'ready',
      versions: [{ replacementText: 'FIRST', summary: '' }],
      versionIndex: 0,
    }
    const second = {
      ...first,
      id: 'second',
      snapshot: selection(12, 18, 'second'),
    }
    const overlapping = {
      ...first,
      id: 'overlap',
      snapshot: selection(7, 13, 'overlap'),
    }

    const withFirst = addShenRewriteProposal([], first)
    const withSecond = addShenRewriteProposal(withFirst.proposals, second)
    const rejected = addShenRewriteProposal(withSecond.proposals, overlapping)

    expect(withSecond.proposals.map((proposal) => proposal.id)).toEqual(['first', 'second'])
    expect(rejected).toEqual({
      proposals: withSecond.proposals,
      overlap: true,
    })
  })

  it('lets the user manually edit the active candidate without touching the document', () => {
    const proposal = {
      id: 'first',
      snapshot: {} as never,
      phase: 'ready' as const,
      versions: [{ replacementText: 'AI draft', summary: 'first pass' }],
      versionIndex: 0,
    }

    const edited = editShenRewriteDraft([proposal], 'first', 'Human-polished draft')

    expect(edited[0].versions[0].replacementText).toBe('Human-polished draft')
    expect(proposal.versions[0].replacementText).toBe('AI draft')
  })

  it('keeps earlier candidates when Shen Xingzhi produces another version', () => {
    const proposal: ShenRewriteProposalState = {
      id: 'first',
      snapshot: {} as never,
      phase: 'running',
      versions: [{ replacementText: 'Version one', summary: 'first pass' }],
      versionIndex: 0,
    }

    const refined = appendShenRewriteVersion([proposal], 'first', {
      replacementText: 'Version two',
      summary: 'shorter',
    })
    const restored = selectShenRewriteVersion(refined, 'first', 0)

    expect(refined[0]).toMatchObject({ phase: 'ready', versionIndex: 1 })
    expect(refined[0].versions.map((version) => version.replacementText)).toEqual([
      'Version one',
      'Version two',
    ])
    expect(restored[0].versionIndex).toBe(0)
  })
})

describe('applyDocxRewriteDirectly', () => {
  it('atomically replaces a selection spanning multiple paragraphs', () => {
    const editor = createTestEditor('<p>First paragraph</p><p>Second paragraph</p>')
    editor.commands.setTextSelection({
      from: 1,
      to: editor.state.doc.content.size - 1,
    })
    const snapshot = captureDocxRewriteSelection(editor)!

    expect(applyDocxRewriteDirectly(editor, snapshot, 'Replacement paragraph')).toBe(true)
    expect(editor.state.doc.textContent).toBe('Replacement paragraph')
    expect(collectRevisions(editor.state.doc)).toEqual([])
    editor.destroy()
  })

  it('keeps a later pending suggestion anchored after accepting an earlier one', () => {
    const editor = createTestEditor('<p>Alpha beta gamma delta</p>')
    editor.commands.setTextSelection({ from: 7, to: 11 })
    const first = captureDocxRewriteSelection(editor)!
    editor.commands.setTextSelection({ from: 18, to: 23 })
    let second = captureDocxRewriteSelection(editor)!

    const onTransaction = ({
      transaction,
    }: {
      transaction: Parameters<typeof remapDocxRewriteSelection>[1]
    }) => {
      second = remapDocxRewriteSelection(second, transaction).snapshot
    }
    editor.on('transaction', onTransaction)

    expect(applyDocxRewriteDirectly(editor, first, 'B')).toBe(true)
    expect(applyDocxRewriteDirectly(editor, second, 'D')).toBe(true)
    expect(editor.state.doc.textContent).toBe('Alpha B gamma D')

    editor.off('transaction', onTransaction)
    editor.destroy()
  })

  it('marks a pending suggestion conflicted after its original text is edited', () => {
    const editor = createTestEditor('<p>Alpha beta gamma</p>')
    editor.commands.setTextSelection({ from: 7, to: 11 })
    const pending = captureDocxRewriteSelection(editor)!
    const transaction = editor.state.tr.insertText('changed', 7, 11)

    const mapped = remapDocxRewriteSelection(pending, transaction)

    expect(mapped.conflict).toBe(true)
    expect(mapped.snapshot.document).toBe(transaction.doc)
    editor.destroy()
  })
})

describe('applyDocxRewriteAsRevision', () => {
  it('keeps the original and replacement as rejectable Agent revisions', () => {
    const editor = createTestEditor()
    editor.commands.setTextSelection({ from: 7, to: 12 })
    const snapshot = captureDocxRewriteSelection(editor)!

    expect(applyDocxRewriteAsRevision(editor, snapshot, 'Lantai')).toBe(true)

    const revisions = collectRevisions(editor.state.doc)
    expect(revisions.map((revision) => [revision.kind, revision.author])).toEqual(
      expect.arrayContaining([
        ['del', '沈行知'],
        ['ins', '沈行知'],
      ]),
    )
    expect(editor.state.doc.textContent).toContain('world')
    expect(editor.state.doc.textContent).toContain('Lantai')
    editor.destroy()
  })

  it('accepts a still-current range after the cursor moves and undoes the revision atomically', () => {
    const editor = createTestEditor()
    editor.commands.setTextSelection({ from: 7, to: 12 })
    const snapshot = captureDocxRewriteSelection(editor)!
    editor.commands.setTextSelection(1)

    expect(applyDocxRewriteAsRevision(editor, snapshot, 'Lantai')).toBe(true)
    expect(collectRevisions(editor.state.doc)).toHaveLength(2)

    expect(editor.commands.undo()).toBe(true)
    expect(editor.state.doc.textContent).toBe('Hello world')
    expect(collectRevisions(editor.state.doc)).toEqual([])
    editor.destroy()
  })

  it('preserves Shen Xingzhi revision authors through a DOCX save and reopen', async () => {
    const bytes = await buildDocx({
      bodyXml: '<w:p><w:r><w:t>Hello world</w:t></w:r></w:p>',
    })
    const parsed = await parseDocx(bytes)
    const editor = new Editor({
      extensions: editorExtensions,
      content: blocksToPmDoc(parsed.blocks),
    })
    editor.commands.setTextSelection({ from: 7, to: 12 })

    expect(applyDocxRewriteAsRevision(editor, captureDocxRewriteSelection(editor)!, 'Lantai')).toBe(
      true,
    )
    const plan = pmDocToSavePlan(editor.getJSON() as PmJson, parsed.blocks)
    const reopenedDocx = await parseDocx(await saveDocx(parsed, plan.saveBlocks))
    expect(reopenedDocx.internal.documentXml).toContain(`w:author="${LANTAI_REVISION_AUTHOR}"`)

    const reopened = new Editor({
      extensions: editorExtensions,
      content: blocksToPmDoc(reopenedDocx.blocks),
    })
    expect(collectRevisions(reopened.state.doc).map((revision) => revision.author)).toEqual([
      LANTAI_REVISION_AUTHOR,
      LANTAI_REVISION_AUTHOR,
    ])
    applyRevisionsBy(reopened, LANTAI_REVISION_AUTHOR, 'reject')
    expect(reopened.state.doc.textContent).toBe('Hello world')

    editor.destroy()
    reopened.destroy()
  })

  it('restores the original on reject and keeps the replacement on accept', () => {
    const rejectEditor = createTestEditor()
    rejectEditor.commands.setTextSelection({ from: 7, to: 12 })
    applyDocxRewriteAsRevision(rejectEditor, captureDocxRewriteSelection(rejectEditor)!, 'Lantai')
    applyRevisionsBy(rejectEditor, LANTAI_REVISION_AUTHOR, 'reject')
    expect(rejectEditor.state.doc.textContent).toBe('Hello world')
    rejectEditor.destroy()

    const acceptEditor = createTestEditor()
    acceptEditor.commands.setTextSelection({ from: 7, to: 12 })
    applyDocxRewriteAsRevision(acceptEditor, captureDocxRewriteSelection(acceptEditor)!, 'Lantai')
    applyRevisionsBy(acceptEditor, LANTAI_REVISION_AUTHOR, 'accept')
    expect(acceptEditor.state.doc.textContent).toBe('Hello Lantai')
    acceptEditor.destroy()
  })
})

describe('manual track changes isolation', () => {
  it('does not duplicate paragraphs when typing punctuation with tracking enabled', () => {
    const editor = createTestEditor('<p>本文档根据录音整理归纳而成。</p>')
    const storage = editor.storage.trackChanges
    storage.enabled = true
    storage.author = 'Tester'
    editor.commands.setTextSelection(editor.state.doc.content.size - 1)

    for (let index = 0; index < 6; index += 1) {
      const at = editor.state.selection.from
      editor.view.dispatch(editor.state.tr.insertText('。', at).scrollIntoView())
    }

    expect(editor.state.doc.childCount).toBe(1)
    expect(editor.state.doc.textContent).toBe('本文档根据录音整理归纳而成。。。。。。。')
    editor.destroy()
  })

  it('does not re-track an existing deleted block that shares the live block anchor', () => {
    const text = '本文档根据录音整理归纳而成。'
    const paragraph = (blockRevision: Record<string, string> | null) => ({
      type: 'docParagraph',
      attrs: { docxIndex: 7, blockRevision },
      content: [{ type: 'text', text }],
    })
    const editor = createTestEditor({
      type: 'doc',
      content: [paragraph({ kind: 'del', author: 'Tester' }), paragraph(null)],
    })
    const storage = editor.storage.trackChanges
    storage.enabled = true
    storage.author = 'Tester'
    editor.commands.setTextSelection(editor.state.doc.content.size - 1)

    for (let index = 0; index < 4; index += 1) {
      const at = editor.state.selection.from
      editor.view.dispatch(editor.state.tr.insertText('。', at))
    }

    expect(editor.state.doc.childCount).toBe(2)
    expect(editor.state.doc.textContent).toBe(`${text}${text}。。。。`)
    editor.destroy()
  })
})

describe('isDocxRewriteSelectionCurrent', () => {
  it('rejects a result after the document or active selection changed', () => {
    const document = {
      content: { size: 20 },
      textBetween: () => 'selected',
    }
    const snapshot = {
      from: 2,
      to: 10,
      selectedText: 'selected',
      contextBefore: '',
      contextAfter: '',
      document,
    }

    expect(
      isDocxRewriteSelectionCurrent(
        { state: { doc: document, selection: { from: 2, to: 10 } } } as never,
        snapshot as never,
      ),
    ).toBe(true)
    expect(
      isDocxRewriteSelectionCurrent(
        { state: { doc: document, selection: { from: 3, to: 10 } } } as never,
        snapshot as never,
      ),
    ).toBe(false)
    expect(
      isDocxRewriteSelectionCurrent(
        {
          state: { doc: { ...document }, selection: { from: 2, to: 10 } },
        } as never,
        snapshot as never,
      ),
    ).toBe(false)
  })
})
