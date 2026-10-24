import { describe, expect, it } from "vitest";
import {
  buildCapabilityDraftTaskRunIds,
  resolveCollaborationDraftAnchor,
} from "./capabilityDraft";

describe("buildCapabilityDraftTaskRunIds", () => {
  it("prioritizes runtime run ids before legacy chat fallback", () => {
    expect(
      buildCapabilityDraftTaskRunIds("chat-1", {
        chat_id: "chat-1",
        meta: {
          last_turn_run_id: "run-1",
          pending_turn: { run_id: "run-2" },
        },
      }),
    ).toEqual(["experience:run-1", "experience:run-2", "chat:chat-1"]);
  });

  it("deduplicates ids and falls back to the active chat id", () => {
    expect(
      buildCapabilityDraftTaskRunIds("chat-1", {
        meta: {
          last_turn_run_id: "run-1",
          pending_turn: { run_id: "run-1" },
        },
      }),
    ).toEqual(["experience:run-1", "chat:chat-1"]);
  });
});

describe("resolveCollaborationDraftAnchor", () => {
  it("uses the run id carried by the clicked response instead of the latest chat", () => {
    expect(
      resolveCollaborationDraftAnchor({
        id: "response-2",
        metadata: { native_runtime_turn_id: "run-2" },
        output: [
          {
            metadata: { runtime_turn_id: "run-older" },
          },
        ],
      }),
    ).toEqual({ runId: "run-2", responseId: "response-2" });
  });

  it("finds nested legacy metadata and removes the experience prefix", () => {
    expect(
      resolveCollaborationDraftAnchor({
        id: "response-legacy",
        output: [
          {
            metadata: {
              metadata: { last_turn_run_id: "experience:run-legacy" },
            },
          },
        ],
      }),
    ).toEqual({ runId: "run-legacy", responseId: "response-legacy" });
  });
});
