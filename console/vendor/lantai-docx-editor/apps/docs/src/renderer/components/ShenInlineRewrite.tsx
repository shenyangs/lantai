import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Editor } from '@tiptap/core'
import { captureDocxRewriteSelection, type DocxRewriteSelection } from '../lantai-double-write'
import { activeShenRewriteVersion, type ShenRewriteProposalState } from '../shen-rewrite-proposals'
import type { ShenRewriteSubmitMode } from '../shen-rewrite-workflow'

const SELECTION_SETTLE_MS = 120
const SURFACE_GAP = 10

const QUICK_INSTRUCTIONS = [
  '润色这段文字',
  '精简这段文字',
  '扩写这段内容',
  '修正语法和错别字',
] as const

export type { ShenRewriteSubmitMode } from '../shen-rewrite-workflow'

interface SurfacePosition {
  left: number
  top: number
}

export interface ShenInlineRewriteProps {
  editor: Editor
  enabled: boolean
  proposal: ShenRewriteProposalState | null
  busy: boolean
  onSubmit: (
    snapshot: DocxRewriteSelection,
    instruction: string,
    mode: ShenRewriteSubmitMode,
  ) => boolean
  onAccept: (proposalId: string) => void
  onReject: (proposalId: string) => void
  onDraftChange: (proposalId: string, replacementText: string) => void
  onRefine: (proposalId: string, instruction: string) => void
  onRetry: (proposalId: string) => void
  onOpenPanel: () => void
}

function selectionKey(snapshot: DocxRewriteSelection): string {
  return `${snapshot.from}:${snapshot.to}:${snapshot.selectedText}`
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum))
}

function positionForSelection(
  editor: Editor,
  snapshot: DocxRewriteSelection,
  width: number,
  estimatedHeight: number,
): SurfacePosition | null {
  try {
    const end = editor.view.coordsAtPos(snapshot.to, -1)
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || width + 24
    const viewportHeight =
      window.innerHeight || document.documentElement.clientHeight || estimatedHeight + 24
    const left = clamp(end.right + SURFACE_GAP, 12, viewportWidth - width - 12)
    const roomBelow = viewportHeight - end.bottom
    const preferredTop =
      roomBelow >= estimatedHeight + 24
        ? end.bottom + SURFACE_GAP
        : end.top - estimatedHeight - SURFACE_GAP
    return {
      left,
      top: clamp(preferredTop, 12, viewportHeight - estimatedHeight - 12),
    }
  } catch {
    return null
  }
}

function excerpt(text: string, limit = 96): string {
  const normalized = text.replace(/\s+/g, ' ').trim()
  return normalized.length > limit ? `${normalized.slice(0, limit)}…` : normalized
}

export function ShenInlineRewrite({
  editor,
  enabled,
  proposal,
  busy,
  onSubmit,
  onAccept,
  onReject,
  onDraftChange,
  onRefine,
  onRetry,
  onOpenPanel,
}: ShenInlineRewriteProps) {
  const [settledSelection, setSettledSelection] = useState<DocxRewriteSelection | null>(null)
  const [composerSelection, setComposerSelection] = useState<DocxRewriteSelection | null>(null)
  const [instruction, setInstruction] = useState('')
  const [refineInstruction, setRefineInstruction] = useState('')
  const [layoutRevision, setLayoutRevision] = useState(0)
  const dismissedSelectionRef = useRef<string | null>(null)
  const draggingRef = useRef(false)

  const settleCurrentSelection = useCallback(() => {
    if (!enabled || draggingRef.current || proposal) {
      setSettledSelection(null)
      return
    }
    const snapshot = captureDocxRewriteSelection(editor)
    if (!snapshot || dismissedSelectionRef.current === selectionKey(snapshot)) {
      setSettledSelection(null)
      return
    }
    setSettledSelection(snapshot)
  }, [editor, enabled, proposal])

  useEffect(() => {
    let timer: number | undefined
    const schedule = () => {
      window.clearTimeout(timer)
      setSettledSelection(null)
      if (composerSelection) {
        const currentSelection = captureDocxRewriteSelection(editor)
        const composerIsStillCurrent =
          currentSelection?.document === composerSelection.document &&
          selectionKey(currentSelection) === selectionKey(composerSelection)
        if (!composerIsStillCurrent) {
          setComposerSelection(null)
          setInstruction('')
        }
      }
      if (!enabled || proposal) return
      timer = window.setTimeout(settleCurrentSelection, SELECTION_SETTLE_MS)
    }
    const onMouseDown = () => {
      draggingRef.current = true
      setSettledSelection(null)
    }
    const onMouseUp = () => {
      draggingRef.current = false
      schedule()
    }
    const onViewportChange = () => setLayoutRevision((current) => current + 1)

    editor.on('selectionUpdate', schedule)
    editor.view.dom.addEventListener('mousedown', onMouseDown)
    window.addEventListener('mouseup', onMouseUp)
    window.addEventListener('resize', onViewportChange)
    window.addEventListener('scroll', onViewportChange, true)
    schedule()

    return () => {
      window.clearTimeout(timer)
      editor.off('selectionUpdate', schedule)
      editor.view.dom.removeEventListener('mousedown', onMouseDown)
      window.removeEventListener('mouseup', onMouseUp)
      window.removeEventListener('resize', onViewportChange)
      window.removeEventListener('scroll', onViewportChange, true)
    }
  }, [editor, enabled, proposal, composerSelection, settleCurrentSelection])

  useEffect(() => {
    if (enabled) return
    setSettledSelection(null)
    setComposerSelection(null)
  }, [enabled])

  const dismissComposer = useCallback(() => {
    if (composerSelection) dismissedSelectionRef.current = selectionKey(composerSelection)
    setComposerSelection(null)
    setSettledSelection(null)
    setInstruction('')
  }, [composerSelection])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || (!composerSelection && !settledSelection)) return
      event.preventDefault()
      dismissComposer()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [composerSelection, settledSelection, dismissComposer])

  const selectionPosition = useMemo(() => {
    void layoutRevision
    const snapshot = composerSelection ?? settledSelection
    if (!snapshot) return null
    return positionForSelection(
      editor,
      snapshot,
      composerSelection ? 432 : 150,
      composerSelection ? 330 : 46,
    )
  }, [editor, composerSelection, settledSelection, layoutRevision])

  const proposalPosition = useMemo(() => {
    void layoutRevision
    if (!proposal) return null
    return positionForSelection(
      editor,
      proposal.snapshot,
      440,
      proposal.phase === 'ready' ? 390 : 180,
    )
  }, [editor, proposal, layoutRevision])

  const submit = (mode: ShenRewriteSubmitMode) => {
    if (!composerSelection || !instruction.trim()) return
    if (onSubmit(composerSelection, instruction.trim(), mode)) {
      dismissedSelectionRef.current = selectionKey(composerSelection)
      setComposerSelection(null)
      setSettledSelection(null)
      setInstruction('')
    }
  }

  const activeVersion = proposal ? activeShenRewriteVersion(proposal) : undefined

  return (
    <>
      {enabled && settledSelection && !composerSelection && !proposal && selectionPosition && (
        <button
          type="button"
          className="shen-inline-trigger"
          style={selectionPosition}
          aria-label="请沈行知修改选中内容"
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            setComposerSelection(settledSelection)
            setInstruction('')
          }}
        >
          <span aria-hidden="true">✦</span> 沈行知随改
        </button>
      )}

      {enabled && composerSelection && selectionPosition && (
        <section
          className="shen-inline-popover"
          style={selectionPosition}
          role="dialog"
          aria-label="对选中内容提出修改"
        >
          <header className="shen-inline-header">
            <div>
              <strong>对选中内容提出修改</strong>
              <p>{excerpt(composerSelection.selectedText)}</p>
            </div>
            <button
              type="button"
              className="shen-inline-icon-button"
              onClick={dismissComposer}
              aria-label="关闭"
            >
              ×
            </button>
          </header>
          <textarea
            className="shen-inline-instruction"
            value={instruction}
            autoFocus
            rows={2}
            placeholder="描述想要的修改…"
            onChange={(event) => setInstruction(event.target.value)}
          />
          <div className="shen-inline-quick-actions" aria-label="快捷修改方式">
            {QUICK_INSTRUCTIONS.map((quickInstruction) => (
              <button
                type="button"
                key={quickInstruction}
                className={instruction === quickInstruction ? 'active' : ''}
                onClick={() => setInstruction(quickInstruction)}
              >
                {quickInstruction}
              </button>
            ))}
          </div>
          <footer className="shen-inline-actions">
            <button
              type="button"
              className="secondary"
              disabled={!instruction.trim()}
              onClick={() => submit('inline')}
            >
              立即建议
            </button>
            <button
              type="button"
              className="primary"
              disabled={!instruction.trim()}
              onClick={() => submit('panel')}
            >
              生成并转到清单
            </button>
          </footer>
        </section>
      )}

      {proposal && proposalPosition && (
        <section
          className={`shen-inline-card shen-inline-card-${proposal.phase}`}
          style={proposalPosition}
          aria-live="polite"
        >
          <header className="shen-inline-header">
            <div>
              <strong>
                {proposal.phase === 'running' && '沈行知正在随改'}
                {proposal.phase === 'ready' && '建议已就绪'}
                {proposal.phase === 'conflict' && '原文已变化'}
                {proposal.phase === 'failed' && '随改失败'}
              </strong>
              <p>原文：{excerpt(proposal.snapshot.selectedText, 72)}</p>
            </div>
            <button
              type="button"
              className="shen-inline-icon-button"
              onClick={onOpenPanel}
              aria-label="收起到随改清单"
            >
              ×
            </button>
          </header>

          {proposal.phase === 'running' && (
            <div className="shen-inline-progress">
              <span className="shen-inline-spinner" aria-hidden="true" />
              正在结合上下文组织建议…
            </div>
          )}

          {proposal.phase === 'conflict' && (
            <p className="shen-inline-conflict">
              {proposal.error || '原文已发生变化，请重新选择后再试。'}
            </p>
          )}

          {proposal.phase === 'failed' && (
            <>
              <p className="shen-inline-conflict">
                {proposal.error || '这次没有生成建议，原文保持不变。'}
              </p>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => onRetry(proposal.id)}
              >
                重试生成
              </button>
            </>
          )}

          {proposal.phase === 'ready' && proposal.error && (
            <div className="shen-inline-retry">
              <p className="shen-inline-conflict">{proposal.error}</p>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => onRetry(proposal.id)}
              >
                重试继续修改
              </button>
            </div>
          )}

          {proposal.phase === 'ready' && activeVersion && (
            <>
              <textarea
                className="shen-inline-result"
                rows={5}
                value={activeVersion.replacementText}
                onChange={(event) => onDraftChange(proposal.id, event.target.value)}
              />
              {activeVersion.summary && (
                <p className="shen-inline-summary">{activeVersion.summary}</p>
              )}
              <div className="shen-inline-refine">
                <input
                  value={refineInstruction}
                  placeholder="告诉沈行知还要怎么改…"
                  onChange={(event) => setRefineInstruction(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && refineInstruction.trim() && !busy) {
                      onRefine(proposal.id, refineInstruction.trim())
                      setRefineInstruction('')
                    }
                  }}
                />
                <button
                  type="button"
                  disabled={!refineInstruction.trim() || busy}
                  onClick={() => {
                    onRefine(proposal.id, refineInstruction.trim())
                    setRefineInstruction('')
                  }}
                >
                  继续改
                </button>
              </div>
            </>
          )}

          <footer className="shen-inline-actions">
            <button type="button" className="ghost" onClick={() => onReject(proposal.id)}>
              拒绝
            </button>
            <button type="button" className="secondary" onClick={onOpenPanel}>
              转到随改清单
            </button>
            {proposal.phase === 'ready' && (
              <button
                type="button"
                className="primary"
                disabled={busy || !activeVersion?.replacementText.trim()}
                onClick={() => onAccept(proposal.id)}
              >
                接受替换
              </button>
            )}
          </footer>
        </section>
      )}
    </>
  )
}
