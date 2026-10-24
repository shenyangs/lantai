import { describe, expect, it, vi } from "vitest";
import {
  CHAT_RUNTIME_MESSAGE_SOURCE_KEY,
  resolveChatRuntimeMessageSource,
} from "./chatRuntimeMode";

describe("resolveChatRuntimeMessageSource", () => {
  it("defaults to the direct model stream", () => {
    expect(resolveChatRuntimeMessageSource(null)).toBe("direct");
  });

  it("supports an emergency direct-stream rollback", () => {
    const storage = {
      getItem: vi.fn((key: string) =>
        key === CHAT_RUNTIME_MESSAGE_SOURCE_KEY ? "direct" : null,
      ),
    };

    expect(resolveChatRuntimeMessageSource(storage)).toBe("direct");
  });

  it("falls back to the direct model stream when storage is unavailable", () => {
    const storage = {
      getItem: vi.fn(() => {
        throw new Error("storage unavailable");
      }),
    };

    expect(resolveChatRuntimeMessageSource(storage)).toBe("direct");
  });
});
