import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearModelSetupInputDraft,
  composerContextStorageKey,
  loadArtifactEditDraft,
  loadModelSetupInputDraft,
  saveArtifactEditDraft,
  saveModelSetupInputDraft,
  subscribeComposerContextDrafts,
} from "./composerContextDrafts";

const owner = { agentId: "default", sessionId: "chat-1" };
const artifact = {
  title: "季度复盘",
  target: {
    schema_version: 1 as const,
    chat_id: "chat-1",
    asset_id: "deck",
    work_id: "deck-v1",
    edit_path: "/tmp/deck.pptx",
    base_revision: { version: 1, digest: "sha256:deck" },
    selection: { kind: "page" as const, page: 3 },
  },
};

describe("composer context draft ownership", () => {
  beforeEach(() => window.sessionStorage.clear());

  it.each([
    "owner",
    "chat",
    "schema",
    "revision",
    "version",
    "selection",
    "json",
  ])("ignores a corrupt or misowned stored artifact (%s)", (fault) => {
    saveArtifactEditDraft(owner, artifact);
    const key = composerContextStorageKey(owner, "artifact-edit");
    const stored = JSON.parse(window.sessionStorage.getItem(key)!);
    if (fault === "owner") stored.agentId = "secondary";
    if (fault === "chat") stored.value.target.chat_id = "chat-2";
    if (fault === "schema") stored.schema_version = 0;
    if (fault === "revision") stored.revision = "";
    if (fault === "version") stored.value.target.base_revision.version = -1;
    if (fault === "selection") stored.value.target.selection.page = "3";
    window.sessionStorage.setItem(
      key,
      fault === "json" ? "{" : JSON.stringify(stored),
    );
    expect(loadArtifactEditDraft(owner)).toBeNull();
  });

  it("does not let acceptance of a previous input revision clear a newly prepared draft", () => {
    const input = [
      { role: "user", content: [{ type: "file", file_url: "/brief.pdf" }] },
    ];
    const first = saveModelSetupInputDraft(owner, input);
    const replacement = saveModelSetupInputDraft(owner, input);
    expect(replacement.revision).not.toBe(first.revision);
    clearModelSetupInputDraft(owner, first.revision);
    expect(loadModelSetupInputDraft(owner)?.revision).toBe(
      replacement.revision,
    );
    clearModelSetupInputDraft(owner, replacement.revision);
    expect(loadModelSetupInputDraft(owner)).toBeNull();
  });

  it("does not collide when agent and conversation identifiers contain separators", () => {
    const other = { agentId: "default:chat", sessionId: "1" };
    const original = { agentId: "default", sessionId: "chat:1" };
    saveModelSetupInputDraft(original, [{ role: "user", content: "original" }]);
    saveModelSetupInputDraft(other, [{ role: "user", content: "other" }]);
    clearModelSetupInputDraft(other);
    expect(loadModelSetupInputDraft(original)?.value[0].content).toBe(
      "original",
    );
  });

  it("synchronizes a returned composer only when its own draft changes", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeComposerContextDrafts(owner, listener);
    saveModelSetupInputDraft({ ...owner, agentId: "secondary" }, [
      { role: "user", content: "other" },
    ]);
    expect(listener).not.toHaveBeenCalled();
    saveArtifactEditDraft(owner, artifact);
    expect(listener).toHaveBeenCalledExactlyOnceWith("artifact-edit");
    unsubscribe();
    saveArtifactEditDraft(owner, artifact);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("leaves the mounted composer usable when storage is unavailable", () => {
    const blocked = vi
      .spyOn(window.sessionStorage, "setItem")
      .mockImplementation(() => {
        throw new Error("storage unavailable");
      });
    try {
      const draft = saveArtifactEditDraft(owner, artifact);
      expect(draft.persisted).toBe(false);
      expect(draft.value).toEqual(artifact);
      expect(loadArtifactEditDraft(owner)).toBeNull();
    } finally {
      blocked.mockRestore();
    }
  });
});
