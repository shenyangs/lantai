import { Extension, type Editor } from '@tiptap/core'
import type { Node as PmNode } from '@tiptap/pm/model'
import { Plugin, PluginKey, type Transaction } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import type { TrackChangesStorage } from './editor/revisions'
import { TRACK_IGNORE } from './editor/revisions'

import { SHEN_XINGZHI_REVISION_AUTHOR } from './editor/revisions'

export const LANTAI_REVISION_AUTHOR = SHEN_XINGZHI_REVISION_AUTHOR

export interface DocxRewriteSelection {
  from: number
  to: number
  selectedText: string
  documentBefore: string
  documentAfter: string
  /** Immutable ProseMirror document identity used for stale-result checks. */
  document: PmNode
}

export function captureDocxRewriteSelection(editor: Editor): DocxRewriteSelection | null {
  const { doc, selection } = editor.state
  if (selection.empty) return null

  const from = selection.from
  const to = selection.to
  return {
    from,
    to,
    selectedText: doc.textBetween(from, to, '\n', '\n'),
    documentBefore: doc.textBetween(0, from, '\n', '\n'),
    documentAfter: doc.textBetween(to, doc.content.size, '\n', '\n'),
    document: doc,
  }
}

export function isDocxRewriteRangeCurrent(editor: Editor, snapshot: DocxRewriteSelection): boolean {
  const { doc } = editor.state
  return (
    doc === snapshot.document &&
    doc.textBetween(snapshot.from, snapshot.to, '\n', '\n') === snapshot.selectedText
  )
}

export function remapDocxRewriteSelection(
  snapshot: DocxRewriteSelection,
  transaction: Transaction,
): { snapshot: DocxRewriteSelection; conflict: boolean } {
  if (!transaction.docChanged) return { snapshot, conflict: false }
  const from = transaction.mapping.map(snapshot.from, 1)
  const to = transaction.mapping.map(snapshot.to, -1)
  const mapped = {
    ...snapshot,
    from,
    to,
    document: transaction.doc,
  }
  const conflict =
    from >= to || transaction.doc.textBetween(from, to, '\n', '\n') !== snapshot.selectedText
  return { snapshot: mapped, conflict }
}

/** Apply an explicitly untracked, atomic range replacement. */
export function applyDocxRewriteDirectly(
  editor: Editor,
  snapshot: DocxRewriteSelection,
  replacementText: string,
): boolean {
  if (!isDocxRewriteRangeCurrent(editor, snapshot) || !replacementText.trim()) return false
  editor.view.dispatch(
    editor.state.tr
      .insertText(replacementText, snapshot.from, snapshot.to)
      .setMeta(TRACK_IGNORE, true)
      .scrollIntoView(),
  )
  return true
}

export type ShenRewriteHighlightPhase = 'running' | 'proposal'
export interface ShenRewriteHighlightRange {
  from: number
  to: number
  phase: ShenRewriteHighlightPhase
}

const shenRewriteHighlightKey = new PluginKey<DecorationSet>('shenRewriteHighlight')

export const ShenRewriteHighlightExtension = Extension.create({
  name: 'shenRewriteHighlight',
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: shenRewriteHighlightKey,
        state: {
          init: () => DecorationSet.empty,
          apply(tr, previous) {
            const next = tr.getMeta(shenRewriteHighlightKey) as
              | ShenRewriteHighlightRange[]
              | null
              | undefined
            if (next === null) return DecorationSet.empty
            if (!next) return tr.docChanged ? previous.map(tr.mapping, tr.doc) : previous
            const decorations: Decoration[] = []
            for (const range of next) {
              decorations.push(
                Decoration.inline(range.from, range.to, {
                  class: `shen-rewrite-highlight shen-rewrite-highlight-${range.phase}`,
                }),
              )
              tr.doc.forEach((node, pos) => {
                const nodeFrom = pos
                const nodeTo = pos + node.nodeSize
                if (nodeTo <= range.from || nodeFrom >= range.to) return
                decorations.push(
                  Decoration.node(nodeFrom, nodeTo, {
                    class: `shen-rewrite-block shen-rewrite-block-${range.phase}`,
                  }),
                )
              })
            }
            return DecorationSet.create(tr.doc, decorations)
          },
        },
        props: {
          decorations: (state) => shenRewriteHighlightKey.getState(state),
        },
      }),
    ]
  },
})

export function setShenRewriteHighlight(
  editor: Editor,
  range: { from: number; to: number; phase: ShenRewriteHighlightPhase } | null,
): void {
  setShenRewriteHighlights(editor, range ? [range] : [])
}

export function setShenRewriteHighlights(
  editor: Editor,
  ranges: ShenRewriteHighlightRange[],
): void {
  editor.view.dispatch(
    editor.state.tr.setMeta(shenRewriteHighlightKey, ranges).setMeta(TRACK_IGNORE, true),
  )
}

export function isDocxRewriteSelectionCurrent(
  editor: Editor,
  snapshot: DocxRewriteSelection,
): boolean {
  const { doc, selection } = editor.state
  return (
    doc === snapshot.document &&
    selection.from === snapshot.from &&
    selection.to === snapshot.to &&
    doc.textBetween(snapshot.from, snapshot.to, '\n', '\n') === snapshot.selectedText
  )
}

export function applyDocxRewriteAsRevision(
  editor: Editor,
  snapshot: DocxRewriteSelection,
  replacementText: string,
): boolean {
  if (!isDocxRewriteRangeCurrent(editor, snapshot) || !replacementText.trim()) return false

  const storage = editor.storage.trackChanges as TrackChangesStorage | undefined
  if (!storage) return false
  const previous = { enabled: storage.enabled, author: storage.author }
  try {
    storage.enabled = true
    storage.author = LANTAI_REVISION_AUTHOR
    editor.view.dispatch(
      editor.state.tr.insertText(replacementText, snapshot.from, snapshot.to).scrollIntoView(),
    )
    return true
  } finally {
    storage.enabled = previous.enabled
    storage.author = previous.author
  }
}
