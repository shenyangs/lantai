import { describe, it, test, expect, vi } from "vitest";
import {
  extractCopyableText,
  extractUserMessageText,
  stripAttachmentSubmitMarker,
  isLocalTimestampSessionId,
  resolveStableChatId,
  buildModelError,
  buildFailedChatResponse,
  buildFailedChatPayload,
  toStoredName,
  normalizeContentUrls,
  toDisplayUrl,
  htmlToPlainTextWithLayout,
  extractClipboardTextWithLayout,
} from "./utils";
import type { CopyableResponse } from "./utils";

// toDisplayUrl depends on chatApi.filePreviewUrl, needs to be mocked
vi.mock("@/api/modules/chat", () => ({
  chatApi: {
    filePreviewUrl: vi.fn((p: string) => `http://localhost:8000${p}`),
  },
}));

// ---------------------------------------------------------------------------
// extractCopyableText
// ---------------------------------------------------------------------------
describe("extractCopyableText", () => {
  it("extracts string content from assistant role", () => {
    const response: CopyableResponse = {
      output: [
        { role: "user", content: "你好" },
        { role: "assistant", content: "你好，有什么可以帮你？" },
      ],
    };
    expect(extractCopyableText(response)).toBe("你好，有什么可以帮你？");
  });

  it("extracts text from structured content array", () => {
    const response: CopyableResponse = {
      output: [
        {
          role: "assistant",
          content: [
            { type: "text", text: "第一段" },
            { type: "text", text: "第二段" },
          ],
        },
      ],
    };
    expect(extractCopyableText(response)).toBe("第一段\n\n第二段");
  });

  it("extracts refusal type content", () => {
    const response: CopyableResponse = {
      output: [
        {
          role: "assistant",
          content: [{ type: "refusal", refusal: "无法回答此问题" }],
        },
      ],
    };
    expect(extractCopyableText(response)).toBe("无法回答此问题");
  });

  it("returns no copyable text when no assistant answer is present", () => {
    const response: CopyableResponse = {
      output: [{ role: "user", content: "仅用户消息" }],
    };
    expect(extractCopyableText(response)).toBe("");
  });

  it("returns no copyable text when output is empty", () => {
    const response: CopyableResponse = { output: [] };
    expect(extractCopyableText(response)).toBe("");
  });

  it("does not throw when output is undefined", () => {
    expect(() => extractCopyableText({})).not.toThrow();
  });

  it("merges multiple assistant messages with double newlines", () => {
    const response: CopyableResponse = {
      output: [
        { role: "assistant", content: "第一句" },
        { role: "assistant", content: "第二句" },
      ],
    };
    expect(extractCopyableText(response)).toBe("第一句\n\n第二句");
  });

  it("copies only the final answer and excludes assistant thinking", () => {
    const response: CopyableResponse = {
      output: [
        {
          type: "reasoning",
          role: "assistant",
          content: [{ type: "text", text: "这里是 Thinking，不应被复制。" }],
        },
        {
          type: "message",
          role: "assistant",
          content: [{ type: "text", text: "这是最终回答。" }],
        },
      ],
    };

    expect(extractCopyableText(response)).toBe("这是最终回答。");
  });

  it("keeps semantic ququ outside copied and saved answer content", () => {
    const response: CopyableResponse = {
      output: [
        {
          type: "message",
          role: "assistant",
          content: [
            {
              type: "text",
              text: "~>[lead] 这句是开场蛐蛐。\n\n# 正式方案\n\n这是可复制、可保存的正文。\n\n~>[echo] 这句是余韵蛐蛐。",
            },
          ],
        },
      ],
    };

    expect(extractCopyableText(response)).toBe(
      "# 正式方案\n\n这是可复制、可保存的正文。",
    );
  });

  it("returns no copyable text when a response contains only thinking", () => {
    const response: CopyableResponse = {
      output: [
        {
          type: "reasoning",
          role: "assistant",
          content: [{ type: "text", text: "仅有 Thinking。" }],
        },
      ],
    };

    expect(extractCopyableText(response)).toBe("");
  });
});

// ---------------------------------------------------------------------------
// extractUserMessageText
// ---------------------------------------------------------------------------
describe("extractUserMessageText", () => {
  it("returns string content directly", () => {
    expect(extractUserMessageText({ content: "你好" })).toBe("你好");
  });

  it("removes the attachment-only submit marker from text content", () => {
    expect(stripAttachmentSubmitMarker("\u200B你好")).toBe("你好");
    expect(extractUserMessageText({ content: "\u200B" })).toBe("");
  });

  it("extracts text type items from array content and joins with newlines", () => {
    const msg = {
      content: [
        { type: "text", text: "你好" },
        { type: "image_url", image_url: "http://..." },
        { type: "text", text: "世界" },
      ],
    };
    expect(extractUserMessageText(msg)).toBe("你好\n世界");
  });

  it("returns empty string for non-string non-array content", () => {
    expect(extractUserMessageText({ content: null })).toBe("");
    expect(extractUserMessageText({ content: 123 })).toBe("");
  });
});

describe("chat id helpers", () => {
  it("detects local timestamp placeholders", () => {
    expect(isLocalTimestampSessionId("1779671611982")).toBe(true);
    expect(isLocalTimestampSessionId("fd6ea2d5-fe9e-4c06-be8d-6d362117cbf8")).toBe(
      false,
    );
  });

  it("prefers real chat ids and skips local timestamp placeholders", () => {
    expect(
      resolveStableChatId(
        "",
        "1779671611982",
        "fd6ea2d5-fe9e-4c06-be8d-6d362117cbf8",
      ),
    ).toBe("fd6ea2d5-fe9e-4c06-be8d-6d362117cbf8");
    expect(resolveStableChatId("", "1779671611982", "")).toBe("");
  });
});

// ---------------------------------------------------------------------------
// Clipboard layout extraction
// ---------------------------------------------------------------------------
describe("clipboard layout extraction", () => {
  it("keeps plain text when it already has line breaks", () => {
    const clipboard = {
      getData: (type: string) =>
        type === "text/plain"
          ? "今日市场结论\n当前市场阶段"
          : "<div>今日市场结论</div><div>当前市场阶段</div>",
    };

    expect(extractClipboardTextWithLayout(clipboard)).toBe(
      "今日市场结论\n当前市场阶段",
    );
  });

  it("uses HTML blocks when plain text has been collapsed", () => {
    const clipboard = {
      getData: (type: string) =>
        type === "text/plain"
          ? "今日市场结论 当前市场阶段 强趋势上涨 高位震荡"
          : [
              "<div>今日市场结论</div>",
              "<div>当前市场阶段</div>",
              "<ul><li>强趋势上涨</li><li>高位震荡</li></ul>",
            ].join(""),
    };

    expect(extractClipboardTextWithLayout(clipboard)).toBe(
      "今日市场结论\n当前市场阶段\n- 强趋势上涨\n- 高位震荡",
    );
  });

  it("converts table-like HTML into readable plain text", () => {
    expect(
      htmlToPlainTextWithLayout(
        "<table><tr><th>风险</th><th>等级</th></tr><tr><td>AI 拥挤度</td><td>中高</td></tr></table>",
      ),
    ).toBe("风险\t等级\nAI 拥挤度\t中高");
  });
});

// ---------------------------------------------------------------------------
// buildModelError
// ---------------------------------------------------------------------------
describe("buildModelError", () => {
  it("returns 400 status code", () => {
    const response = buildModelError();
    expect(response.status).toBe(400);
  });

  it("response body contains error and message fields", async () => {
    const response = buildModelError();
    const body = await response.json();
    expect(body).toHaveProperty("error");
    expect(body).toHaveProperty("message");
    expect(body.object).toBe("response");
    expect(body.status).toBe("failed");
    expect(body.type).toBe("response.failed");
    expect(body.error.message).toBe("Please configure a model first");
  });

  it("Content-Type is application/json", () => {
    const response = buildModelError();
    expect(response.headers.get("Content-Type")).toBe("application/json");
  });
});

describe("buildFailedChatResponse", () => {
  it("builds a protocol-compatible failed response payload", () => {
    const body = buildFailedChatPayload({
      errorCode: "Backend unavailable",
      message: "service warming up",
    });

    expect(body.object).toBe("response");
    expect(body.status).toBe("failed");
    expect(body.type).toBe("response.failed");
    expect(body.output).toEqual([]);
    expect(body.error).toEqual({
      code: "Backend unavailable",
      message: "service warming up",
    });
  });

  it("returns a response.failed payload that the chat card renderer can show", async () => {
    const response = buildFailedChatResponse({
      statusCode: 500,
      errorCode: "Chat request failed",
      message: "network down",
    });

    expect(response.status).toBe(500);
    const body = await response.json();
    expect(body.object).toBe("response");
    expect(body.status).toBe("failed");
    expect(body.output).toEqual([]);
    expect(body.error).toEqual({
      code: "Chat request failed",
      message: "network down",
    });
  });
});

// ---------------------------------------------------------------------------
// toStoredName
// ---------------------------------------------------------------------------
describe("toStoredName", () => {
  test.each([
    [
      "extracts path after /files/preview/",
      "http://host/files/preview/uploads/img.png",
      "/uploads/img.png",
    ],
    [
      "strips query parameters",
      "http://host/files/preview/img.png?token=abc",
      "/img.png",
    ],
    [
      "strips hash fragment",
      "http://host/files/preview/img.png#section",
      "/img.png",
    ],
    [
      "returns input as-is when marker is absent",
      "/local/path/file.txt",
      "/local/path/file.txt",
    ],
    [
      "correctly decodes URL-encoded path",
      "http://host/files/preview/%E4%B8%AD%E6%96%87.txt",
      "/中文.txt",
    ],
  ])("%s", (_: string, input: string, expected: string) => {
    expect(toStoredName(input)).toBe(expected);
  });
});

// ---------------------------------------------------------------------------
// normalizeContentUrls
// ---------------------------------------------------------------------------
describe("normalizeContentUrls", () => {
  it("converts image_url for image type", () => {
    const part = {
      type: "image",
      image_url: "http://host/files/preview/img.png",
    };
    const result = normalizeContentUrls(part);
    expect(result.image_url).toBe("/img.png");
  });

  it("converts file_url for file type", () => {
    const part = {
      type: "file",
      file_url: "http://host/files/preview/doc.pdf",
    };
    const result = normalizeContentUrls(part);
    expect(result.file_url).toBe("/doc.pdf");
  });

  it("converts data for audio type", () => {
    const part = { type: "audio", data: "http://host/files/preview/audio.mp3" };
    const result = normalizeContentUrls(part);
    expect(result.data).toBe("/audio.mp3");
  });

  it("does not affect text type", () => {
    const part = { type: "text", text: "hello" };
    expect(normalizeContentUrls(part)).toEqual(part);
  });

  it("does not mutate the original object (shallow copy)", () => {
    const part = {
      type: "image",
      image_url: "http://host/files/preview/img.png",
    };
    normalizeContentUrls(part);
    expect(part.image_url).toBe("http://host/files/preview/img.png");
  });
});

// ---------------------------------------------------------------------------
// toDisplayUrl
// ---------------------------------------------------------------------------
describe("toDisplayUrl", () => {
  it("returns http URL as-is", () => {
    expect(toDisplayUrl("http://cdn.com/img.png")).toBe(
      "http://cdn.com/img.png",
    );
  });

  it("returns https URL as-is", () => {
    expect(toDisplayUrl("https://cdn.com/file")).toBe("https://cdn.com/file");
  });

  it("returns browser-native data and blob URLs as-is", () => {
    expect(toDisplayUrl("data:image/png;base64,AAAA")).toBe(
      "data:image/png;base64,AAAA",
    );
    expect(toDisplayUrl("blob:http://localhost/generated-image")).toBe(
      "blob:http://localhost/generated-image",
    );
  });

  it("returns empty string for undefined", () => {
    expect(toDisplayUrl(undefined)).toBe("");
  });

  it("returns empty string for empty string", () => {
    expect(toDisplayUrl("")).toBe("");
  });

  it("calls chatApi.filePreviewUrl for relative paths", () => {
    expect(toDisplayUrl("/uploads/img.png")).toBe(
      "http://localhost:8000/uploads/img.png",
    );
  });

  it("strips file:// prefix then resolves full URL", () => {
    expect(toDisplayUrl("file:///uploads/img.png")).toBe(
      "http://localhost:8000/uploads/img.png",
    );
  });
});
