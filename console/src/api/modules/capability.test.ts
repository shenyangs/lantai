import { afterEach, describe, expect, it, vi } from "vitest";
import type { CapabilityDraft } from "../types";

vi.mock("../config", () => ({
  getApiUrl: (path: string) => `http://127.0.0.1:8000/api${path}`,
}));

vi.mock("../authHeaders", () => ({
  buildAuthHeaders: () => ({ Authorization: "Bearer test-token" }),
}));

import { capabilityApi } from "./capability";

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

describe("capabilityApi.streamCollaborationDraft", () => {
  afterEach(() => vi.restoreAllMocks());

  it("keeps partial SSE frames buffered and returns the review draft", async () => {
    const draft = {
      primary_asset: "skill",
      capability: { id: "cap-1", name: "复用研究流程" },
      suggested_skills: [{ name: "reusable-research" }],
    } as unknown as CapabilityDraft;
    const stage = {
      type: "stage",
      step: "distill",
      status: "running",
      title: "提炼纠正与稳定方法",
      detail: "沈行知正在整理协作方法。",
    };
    const onEvent = vi.fn();
    const controller = new AbortController();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      streamResponse([
        `data: ${JSON.stringify(stage).slice(0, 28)}`,
        `${JSON.stringify(stage).slice(28)}\n\n`,
        `data: ${JSON.stringify({ type: "result", draft })}\n\n`,
      ]),
    );

    const result = await capabilityApi.streamCollaborationDraft(
      {
        agent_id: "default",
        chat_id: "chat-1",
        anchor_run_id: "run-3",
        anchor_response_id: "response-3",
      },
      { onEvent, signal: controller.signal },
    );

    expect(result).toEqual(draft);
    expect(onEvent).toHaveBeenNthCalledWith(1, stage);
    expect(onEvent).toHaveBeenNthCalledWith(2, { type: "result", draft });
    expect(fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/task-runs/collaboration-drafts/stream",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        signal: controller.signal,
        headers: {
          Authorization: "Bearer test-token",
          "Content-Type": "application/json",
        },
      }),
    );
  });

  it("surfaces a streamed failure instead of ending silently", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      streamResponse([
        `data: ${JSON.stringify({
          type: "error",
          message: "所点回复已无法与运行记录匹配。",
        })}\n\n`,
      ]),
    );

    await expect(
      capabilityApi.streamCollaborationDraft({
        agent_id: "default",
        chat_id: "chat-1",
        anchor_run_id: "missing-run",
      }),
    ).rejects.toThrow("所点回复已无法与运行记录匹配。");
  });
});
