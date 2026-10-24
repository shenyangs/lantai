export interface MemoryEditDraftOwner {
  agentId: string;
  entryId: string;
  revision: string;
}

const memoryEditDrafts = new Map<string, string>();

function draftKey(owner: MemoryEditDraftOwner) {
  return JSON.stringify([owner.agentId, owner.entryId, owner.revision]);
}

export function readMemoryEditDraft(owner: MemoryEditDraftOwner) {
  return memoryEditDrafts.get(draftKey(owner));
}

export function writeMemoryEditDraft(
  owner: MemoryEditDraftOwner,
  content: string,
) {
  memoryEditDrafts.set(draftKey(owner), content);
}

export function clearMemoryEditDraft(owner: MemoryEditDraftOwner) {
  memoryEditDrafts.delete(draftKey(owner));
}
