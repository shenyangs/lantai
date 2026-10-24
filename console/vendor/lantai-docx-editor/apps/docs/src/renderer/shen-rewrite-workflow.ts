import type { Editor } from '@tiptap/core'
import {
  applyDocxRewriteAsRevision,
  isDocxRewriteSelectionCurrent,
  type DocxRewriteSelection,
} from './lantai-double-write'
import { activeShenRewriteVersion, type ShenRewriteProposalState } from './shen-rewrite-proposals'

export type ShenRewriteSubmitMode = 'inline' | 'panel'

export interface ShenRewriteRequest {
  snapshot: DocxRewriteSelection
  instruction: string
  currentDraft: string
}

export function createShenRewriteProposal(
  id: string,
  snapshot: DocxRewriteSelection,
  instruction: string,
): ShenRewriteProposalState {
  return {
    id,
    snapshot,
    phase: 'running',
    versions: [],
    versionIndex: 0,
    request: { instruction, currentDraft: '' },
  }
}

export function inlineProposalIdForMode(
  proposalId: string,
  mode: ShenRewriteSubmitMode,
): string | null {
  return mode === 'inline' ? proposalId : null
}

export function isShenRewriteStartSnapshotCurrent(
  editor: Editor,
  snapshot: DocxRewriteSelection,
): boolean {
  return isDocxRewriteSelectionCurrent(editor, snapshot)
}

export function acceptShenRewriteProposal(
  editor: Editor,
  proposal: ShenRewriteProposalState | undefined,
): 'applied' | 'not-ready' | 'conflict' {
  const version = proposal ? activeShenRewriteVersion(proposal) : undefined
  if (!proposal || proposal.phase !== 'ready' || !version) return 'not-ready'
  return applyDocxRewriteAsRevision(editor, proposal.snapshot, version.replacementText)
    ? 'applied'
    : 'conflict'
}

export function retryRequestForProposal(
  proposal: ShenRewriteProposalState | undefined,
): ShenRewriteRequest | null {
  const retryable =
    proposal?.phase === 'failed' || (proposal?.phase === 'ready' && Boolean(proposal.error))
  if (!proposal || !retryable || !proposal.request) return null
  return {
    snapshot: proposal.snapshot,
    instruction: proposal.request.instruction,
    currentDraft: proposal.request.currentDraft,
  }
}
