import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ARTIFACT_WORKBENCH_MAX_TABS,
  activateArtifactTab,
  artifactWorkbenchStorageKey,
  closeArtifactTab,
  loadArtifactWorkbenchState,
  migrateArtifactWorkbenchState,
  openArtifactTab,
  reconcileArtifactWorkbenchState,
  saveArtifactWorkbenchState,
  type ArtifactWorkbenchState,
} from "./artifactWorkbenchState";

const EMPTY_STATE: ArtifactWorkbenchState = {
  openPaths: [],
  activePath: null,
  expanded: [],
  view: "files",
  selectedWorkId: null,
  selectedPageByWorkId: {},
};

describe("artifactWorkbenchState", () => {
  beforeEach(() => window.localStorage.clear());

  it("opens, activates, closes, and bounds artifact tabs", () => {
    let state = EMPTY_STATE;
    for (let index = 0; index < ARTIFACT_WORKBENCH_MAX_TABS + 2; index += 1) {
      state = openArtifactTab(state, `artifact-${index}.md`, "source");
    }

    expect(state.openPaths).toHaveLength(ARTIFACT_WORKBENCH_MAX_TABS);
    expect(state.openPaths[0]).toBe("artifact-2.md");
    expect(state.activePath).toBe("artifact-9.md");

    state = activateArtifactTab(state, "artifact-4.md");
    expect(state.activePath).toBe("artifact-4.md");

    state = closeArtifactTab(state, "artifact-4.md");
    expect(state.openPaths).not.toContain("artifact-4.md");
    expect(state.activePath).toBe("artifact-5.md");
  });

  it("restores one chat independently and falls back safely for corrupt data", () => {
    const saved: ArtifactWorkbenchState = {
      openPaths: ["index.html", "README.md"],
      activePath: "README.md",
      expanded: ["src", "docs"],
      view: "preview",
      selectedWorkId: "work-deck-v1",
      selectedPageByWorkId: { "work-deck-v1": 3 },
    };
    saveArtifactWorkbenchState("chat-a", saved);

    expect(loadArtifactWorkbenchState("chat-a")).toEqual(saved);
    expect(loadArtifactWorkbenchState("chat-b")).toEqual(EMPTY_STATE);

    window.localStorage.setItem(
      "lantai:artifact-workbench:v1:chat-corrupt",
      '{"openPaths":"wrong"}',
    );
    expect(loadArtifactWorkbenchState("chat-corrupt")).toEqual(EMPTY_STATE);
    expect(
      window.localStorage.getItem("lantai:artifact-workbench:v1:chat-corrupt"),
    ).toBeNull();
  });

  it("drops stale files and directories while keeping a recoverable active tab", () => {
    const state: ArtifactWorkbenchState = {
      openPaths: ["gone.md", "src/app.ts", "index.html"],
      activePath: "gone.md",
      expanded: ["src", "removed"],
      view: "source",
      selectedWorkId: "work-deck-v1",
      selectedPageByWorkId: { "work-deck-v1": 3 },
    };

    expect(
      reconcileArtifactWorkbenchState(
        state,
        new Set(["src/app.ts", "index.html"]),
        new Set(["src"]),
      ),
    ).toEqual({
      openPaths: ["src/app.ts", "index.html"],
      activePath: "index.html",
      expanded: ["src"],
      view: "source",
      selectedWorkId: "work-deck-v1",
      selectedPageByWorkId: { "work-deck-v1": 3 },
    });
  });

  it("migrates older state and rejects invalid remembered pages", () => {
    window.localStorage.setItem(
      "lantai:artifact-workbench:v1:chat-old",
      JSON.stringify({
        openPaths: [],
        activePath: null,
        expanded: [],
        view: "files",
        selectedWorkId: "work-deck-v1",
        selectedPageByWorkId: {
          "work-deck-v1": 3,
          zero: 0,
          fractional: 2.5,
          text: "4",
        },
      }),
    );

    expect(loadArtifactWorkbenchState("chat-old")).toEqual({
      ...EMPTY_STATE,
      selectedWorkId: "work-deck-v1",
      selectedPageByWorkId: { "work-deck-v1": 3 },
    });

    window.localStorage.setItem(
      "lantai:artifact-workbench:v1:chat-v0",
      JSON.stringify({
        openPaths: [],
        activePath: null,
        expanded: [],
        view: "files",
      }),
    );
    expect(loadArtifactWorkbenchState("chat-v0")).toEqual(EMPTY_STATE);
  });

  it("moves temporary chat preferences to the resolved canonical chat exactly once", () => {
    const temporaryState: ArtifactWorkbenchState = {
      openPaths: ["notes.md"],
      activePath: "notes.md",
      expanded: ["drafts"],
      view: "preview",
      selectedWorkId: "work-deck-v2",
      selectedPageByWorkId: { "work-deck-v2": 3 },
    };
    saveArtifactWorkbenchState("temp-1720000000000", temporaryState);

    expect(
      migrateArtifactWorkbenchState("temp-1720000000000", "chat-canonical"),
    ).toBe(true);
    expect(loadArtifactWorkbenchState("chat-canonical")).toEqual(
      temporaryState,
    );
    expect(
      window.localStorage.getItem(
        "lantai:artifact-workbench:v1:temp-1720000000000",
      ),
    ).toBeNull();
    expect(
      migrateArtifactWorkbenchState("temp-1720000000000", "chat-canonical"),
    ).toBe(false);
  });

  it("keeps canonical preferences when a temporary chat resolves onto existing state", () => {
    const temporaryState: ArtifactWorkbenchState = {
      ...EMPTY_STATE,
      selectedWorkId: "work-temp",
      selectedPageByWorkId: { "work-temp": 3 },
    };
    const canonicalState: ArtifactWorkbenchState = {
      ...EMPTY_STATE,
      selectedWorkId: "work-canonical",
      selectedPageByWorkId: { "work-canonical": 5 },
    };
    saveArtifactWorkbenchState("temp-1720000000001", temporaryState);
    saveArtifactWorkbenchState("chat-canonical", canonicalState);

    expect(
      migrateArtifactWorkbenchState("temp-1720000000001", "chat-canonical"),
    ).toBe(false);
    expect(loadArtifactWorkbenchState("chat-canonical")).toEqual(
      canonicalState,
    );
    expect(
      window.localStorage.getItem(
        "lantai:artifact-workbench:v1:temp-1720000000001",
      ),
    ).toBeNull();
  });

  it("removes a corrupt temporary alias instead of retrying it on every resolution", () => {
    const temporaryKey = "lantai:artifact-workbench:v1:temp-1720000000002";
    window.localStorage.setItem(temporaryKey, '{"openPaths":"wrong"}');

    expect(
      migrateArtifactWorkbenchState("temp-1720000000002", "chat-canonical"),
    ).toBe(false);
    expect(window.localStorage.getItem(temporaryKey)).toBeNull();
    expect(loadArtifactWorkbenchState("chat-canonical")).toEqual(EMPTY_STATE);
  });

  it("keeps temporary preferences when writing the canonical copy fails", () => {
    const temporaryChatId = "temp-1720000000003";
    const canonicalChatId = "chat-canonical";
    const temporaryState: ArtifactWorkbenchState = {
      ...EMPTY_STATE,
      activePath: "季度复盘.pptx",
      openPaths: ["季度复盘.pptx"],
      selectedWorkId: "work-deck-v2",
      selectedPageByWorkId: { "work-deck-v2": 3 },
    };
    saveArtifactWorkbenchState(temporaryChatId, temporaryState);
    const targetKey = artifactWorkbenchStorageKey(canonicalChatId);
    const originalSetItem = window.localStorage.setItem.bind(
      window.localStorage,
    );
    const setItemSpy = vi
      .spyOn(window.localStorage, "setItem")
      .mockImplementation((key, value) => {
        if (key === targetKey) throw new DOMException("quota exceeded");
        originalSetItem(key, value);
      });

    expect(
      migrateArtifactWorkbenchState(temporaryChatId, canonicalChatId),
    ).toBe(false);
    setItemSpy.mockRestore();

    expect(loadArtifactWorkbenchState(temporaryChatId)).toEqual(temporaryState);
    expect(loadArtifactWorkbenchState(canonicalChatId)).toEqual(EMPTY_STATE);
  });
});
