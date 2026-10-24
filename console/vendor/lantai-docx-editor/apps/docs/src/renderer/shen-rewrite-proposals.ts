import type { Transaction } from '@tiptap/pm/state'
import { remapDocxRewriteSelection, type DocxRewriteSelection } from './lantai-double-write'

export interface ShenRewriteVersion {
  replacementText: string
  summary: string
}

export interface ShenRewriteProposalState {
  id: string
  snapshot: DocxRewriteSelection
  phase: 'running' | 'ready' | 'conflict' | 'failed'
  versions: ShenRewriteVersion[]
  versionIndex: number
  request?: {
    instruction: string
    currentDraft: string
  }
  error?: string
}

function selectionsOverlap(left: DocxRewriteSelection, right: DocxRewriteSelection): boolean {
  return left.from < right.to && right.from < left.to
}

export function hasShenRewriteOverlap(
  proposals: ShenRewriteProposalState[],
  selection: DocxRewriteSelection,
): boolean {
  return proposals.some((current) => selectionsOverlap(current.snapshot, selection))
}

export function addShenRewriteProposal(
  proposals: ShenRewriteProposalState[],
  proposal: ShenRewriteProposalState,
): { proposals: ShenRewriteProposalState[]; overlap: boolean } {
  if (hasShenRewriteOverlap(proposals, proposal.snapshot)) {
    return { proposals, overlap: true }
  }
  return { proposals: [...proposals, proposal], overlap: false }
}

export function activeShenRewriteVersion(
  proposal: ShenRewriteProposalState,
): ShenRewriteVersion | undefined {
  return proposal.versions[proposal.versionIndex]
}

export function remapShenRewriteProposals(
  proposals: ShenRewriteProposalState[],
  transaction: Transaction,
): ShenRewriteProposalState[] {
  if (!transaction.docChanged) return proposals
  return proposals.map((proposal) => {
    const mapped = remapDocxRewriteSelection(proposal.snapshot, transaction)
    return {
      ...proposal,
      snapshot: mapped.snapshot,
      phase: mapped.conflict ? 'conflict' : proposal.phase,
      error: mapped.conflict ? '原文已发生变化，请重新生成' : proposal.error,
    }
  })
}

export function editShenRewriteDraft(
  proposals: ShenRewriteProposalState[],
  proposalId: string,
  replacementText: string,
): ShenRewriteProposalState[] {
  return proposals.map((proposal) => {
    if (proposal.id !== proposalId || proposal.phase !== 'ready') return proposal
    return {
      ...proposal,
      versions: proposal.versions.map((version, index) =>
        index === proposal.versionIndex ? { ...version, replacementText } : version,
      ),
    }
  })
}

export function appendShenRewriteVersion(
  proposals: ShenRewriteProposalState[],
  proposalId: string,
  version: ShenRewriteVersion,
): ShenRewriteProposalState[] {
  return proposals.map((proposal) => {
    if (proposal.id !== proposalId) return proposal
    const versions = [...proposal.versions, version]
    return {
      ...proposal,
      phase: 'ready',
      versions,
      versionIndex: versions.length - 1,
      error: undefined,
    }
  })
}

export function selectShenRewriteVersion(
  proposals: ShenRewriteProposalState[],
  proposalId: string,
  versionIndex: number,
): ShenRewriteProposalState[] {
  return proposals.map((proposal) => {
    if (
      proposal.id !== proposalId ||
      versionIndex < 0 ||
      versionIndex >= proposal.versions.length
    ) {
      return proposal
    }
    return { ...proposal, versionIndex }
  })
}
