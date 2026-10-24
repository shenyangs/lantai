import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  buildPreviewFixPrompt,
  isAllowedPreviewOrigin,
  isPreviewErrorType,
  usePreviewErrorBackfeed,
} from "./usePreviewErrorBackfeed";

describe("preview error backfeed helpers", () => {
  it("recognizes preview error message types", () => {
    expect(isPreviewErrorType("preview:error")).toBe(true);
    expect(isPreviewErrorType("preview:build:error")).toBe(true);
    expect(isPreviewErrorType("chat:message")).toBe(false);
  });

  it("accepts local preview origins only", () => {
    expect(isAllowedPreviewOrigin("http://localhost:5173")).toBe(true);
    expect(isAllowedPreviewOrigin("http://127.0.0.1:3000")).toBe(true);
    expect(isAllowedPreviewOrigin("https://example.com")).toBe(false);
  });

  it("builds a repair prompt from preview errors", () => {
    const prompt = buildPreviewFixPrompt({
      source: "preview:build:error",
      url: "http://localhost:5173",
      summary: "ReferenceError: App is not defined",
      detail: "at main.tsx:4:1",
    });

    expect(prompt).toContain("预览页面报错了");
    expect(prompt).toContain("ReferenceError");
    expect(prompt).toContain("main.tsx");
    expect(prompt).toContain("dev server");
    expect(prompt).toContain("失败的网络请求");
  });

  it("捕获预览错误时不会擅自改写用户输入框", () => {
    const textarea = document.createElement("textarea");
    const onInfo = vi.fn();
    const { result } = renderHook(() =>
      usePreviewErrorBackfeed({
        chatId: "chat-1",
        getTextarea: () => textarea,
        onInfo,
      }),
    );

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: "http://localhost:5173",
          data: {
            type: "preview:runtime:error",
            summary: "ReferenceError: App is not defined",
          },
        }),
      );
    });

    expect(textarea.value).toBe("");
    expect(result.current.draft?.summary).toContain("ReferenceError");
    expect(onInfo).toHaveBeenCalledWith(
      expect.stringContaining("可手动回填输入框"),
    );
  });
});
