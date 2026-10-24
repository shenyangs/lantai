import { describe, expect, it } from "vitest";

import { buildUnifiedSearchResults } from "./unifiedSearch";

describe("buildUnifiedSearchResults", () => {
  it("merges sources and removes duplicate visible chat snippets", () => {
    const results = buildUnifiedSearchResults("Codex", {
      chats: [
        {
          id: "one",
          kind: "message",
          chat_id: "chat-1",
          chat_name: "Codex 协作",
          message_id: "m1",
          role: "assistant",
          excerpt: "Codex 已经完成",
        },
        {
          id: "two",
          kind: "message",
          chat_id: "chat-1",
          chat_name: "Codex 协作",
          message_id: "m2",
          role: "assistant",
          excerpt: "Codex 已经完成",
        },
      ],
      memories: [
        {
          id: "memory-1",
          type: "workflow",
          content: "经常让 Codex 协助开发",
          source: { kind: "chat", ref: "chat-1" },
          status: "active",
          confidence: 1,
          reason: "",
          created_at: "2026-09-07T00:00:00Z",
          updated_at: "2026-09-07T00:00:00Z",
        } as any,
      ],
      knowledge: [],
      works: [],
    });

    expect(results.map((item) => item.kind)).toEqual(["chat", "memory"]);
    expect(results[0].messageId).toBe("m1");
  });

  it("keeps identical snippets from different chats independently locatable", () => {
    const results = buildUnifiedSearchResults("完成", {
      chats: [
        {
          id: "one",
          kind: "message",
          chat_id: "chat-1",
          chat_name: "同名会话",
          message_id: "m1",
          role: "assistant",
          excerpt: "已经完成",
        },
        {
          id: "two",
          kind: "message",
          chat_id: "chat-2",
          chat_name: "同名会话",
          message_id: "m2",
          role: "assistant",
          excerpt: "已经完成",
        },
      ],
      memories: [],
      knowledge: [],
      works: [],
    });

    expect(results.map((item) => item.chatId)).toEqual(["chat-1", "chat-2"]);
  });
});
