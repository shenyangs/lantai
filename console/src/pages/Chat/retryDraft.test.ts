import { afterEach, describe, expect, it } from "vitest";
import { consumeRetryDraft, saveRetryDraft } from "./retryDraft";

describe("retryDraft", () => {
  afterEach(() => {
    window.sessionStorage.clear();
  });

  it("stores and consumes retry text for a chat", () => {
    saveRetryDraft("chat-1", "  重新来过  ");

    expect(consumeRetryDraft("chat-1")).toBe("重新来过");
    expect(consumeRetryDraft("chat-1")).toBe("");
  });

  it("ignores empty chat ids and empty text", () => {
    saveRetryDraft("", "hello");
    saveRetryDraft("chat-1", "   ");

    expect(consumeRetryDraft("chat-1")).toBe("");
  });
});
