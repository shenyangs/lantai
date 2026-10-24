import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearPendingChatMessageNavigation,
  consumePendingChatMessageNavigation,
  peekPendingChatMessageNavigation,
  requestChatMessageNavigation,
  subscribeChatMessageNavigation,
} from "../../vendor/lantaiChatMessageNavigation";

describe("chat message navigation requests", () => {
  afterEach(() => {
    clearPendingChatMessageNavigation();
  });

  it("keeps a search jump pending until the destination message is mounted", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeChatMessageNavigation(listener);

    const requested = requestChatMessageNavigation({
      chatId: "chat-7",
      messageId: "message-42",
      source: "search",
    });

    expect(listener).toHaveBeenCalledWith(requested);
    expect(peekPendingChatMessageNavigation()).toEqual(requested);
    expect(consumePendingChatMessageNavigation("a-different-request")).toBe(
      false,
    );
    expect(peekPendingChatMessageNavigation()).toEqual(requested);
    expect(consumePendingChatMessageNavigation(requested.requestId)).toBe(true);
    expect(peekPendingChatMessageNavigation()).toBeNull();

    unsubscribe();
  });
});
