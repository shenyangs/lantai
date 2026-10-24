import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { chatApi, parseRuntimeEventStreamFrames } from "./chat";

// chat.ts uses both fetch (uploadFile) and the request wrapper (others) — mock both
vi.mock("../request", () => ({ request: vi.fn() }));
vi.mock("../config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
  getApiToken: vi.fn(() => ""),
}));
vi.mock("../authHeaders", () => ({
  buildAuthHeaders: vi.fn(() => ({})),
  encodeHeaderValue: (value: string) => value,
}));

import { request } from "../request";
import { getApiToken } from "../config";

// ---------------------------------------------------------------------------
// filePreviewUrl — pure function, highest ROI
// ---------------------------------------------------------------------------
describe("chatApi.filePreviewUrl", () => {
  afterEach(() => vi.clearAllMocks());

  it("returns empty string for empty input", () => {
    expect(chatApi.filePreviewUrl("")).toBe("");
  });

  it("returns http URL as-is", () => {
    expect(chatApi.filePreviewUrl("http://cdn.com/img.png")).toBe(
      "http://cdn.com/img.png",
    );
  });

  it("returns https URL as-is", () => {
    expect(chatApi.filePreviewUrl("https://cdn.com/img.png")).toBe(
      "https://cdn.com/img.png",
    );
  });

  it("prepends /api/files/preview/ for relative paths", () => {
    const result = chatApi.filePreviewUrl("img.png");
    expect(result).toBe("/api/files/preview/img.png");
  });

  it("strips leading / from path", () => {
    const result = chatApi.filePreviewUrl("/img.png");
    expect(result).toBe("/api/files/preview/img.png");
  });

  it("appends ?token= param when token is present", () => {
    vi.mocked(getApiToken).mockReturnValue("my-token");
    const result = chatApi.filePreviewUrl("img.png");
    expect(result).toContain("?token=my-token");
  });

  it("URL-encodes token with special characters", () => {
    vi.mocked(getApiToken).mockReturnValue("tok en+1");
    const result = chatApi.filePreviewUrl("img.png");
    expect(result).toContain("token=tok%20en%2B1");
  });

  it("does not append query param when token is empty", () => {
    vi.mocked(getApiToken).mockReturnValue("");
    const result = chatApi.filePreviewUrl("img.png");
    expect(result).not.toContain("?token");
  });
});

describe("chatApi.getChatEvents", () => {
  it("forwards an abort signal to bounded recovery reads", async () => {
    const controller = new AbortController();
    vi.mocked(request).mockResolvedValue({
      chat_id: "chat-1",
      after_seq: 4,
      latest_seq: 4,
      events: [],
    });

    await chatApi.getChatEvents("chat-1", 4, {
      limit: 50,
      signal: controller.signal,
    });

    expect(request).toHaveBeenCalledWith(
      "/chats/chat-1/events?after_seq=4&limit=50",
      { signal: controller.signal },
    );
  });
});

// ---------------------------------------------------------------------------
// uploadFile — raw fetch, includes error handling logic
// ---------------------------------------------------------------------------
describe("chatApi.uploadFile", () => {
  afterEach(() => vi.clearAllMocks());

  it("prefers the desktop IPC transport over renderer fetch", async () => {
    const uploadChatAttachment = vi.fn(async (payload) => {
      expect(payload.fileName).toBe("clipboard.png");
      expect(payload.mimeType).toBe("image/png");
      expect(payload.bytes).toBeInstanceOf(ArrayBuffer);
      return {
        ok: true,
        url: "/uploads/clipboard.png",
        file_name: "clipboard.png",
        stored_name: "digest_clipboard.png",
        size: 5,
        digest: "sha256:clipboard",
        version: 1,
        upload_receipt: "upload-receipt:v1:desktop",
      };
    });
    (window as any).electronAPI = {
      isDesktop: true,
      uploadChatAttachment,
    };
    global.fetch = vi
      .fn()
      .mockRejectedValue(new Error("renderer transport blocked"));

    try {
      const result = await chatApi.uploadFile(
        new File(["image"], "clipboard.png", { type: "image/png" }),
      );

      expect(result).toEqual({
        url: "/uploads/clipboard.png",
        file_name: "clipboard.png",
        stored_name: "digest_clipboard.png",
        size: 5,
        digest: "sha256:clipboard",
        version: 1,
        upload_receipt: "upload-receipt:v1:desktop",
      });
      expect(uploadChatAttachment).toHaveBeenCalledOnce();
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      delete (window as any).electronAPI;
    }
  });

  it("surfaces a recoverable timeout from the desktop transport", async () => {
    (window as any).electronAPI = {
      isDesktop: true,
      uploadChatAttachment: vi.fn(async () => ({
        ok: false,
        error: "Attachment upload timed out.",
        error_code: "upload_timeout",
      })),
    };
    global.fetch = vi.fn();

    try {
      const error = await chatApi
        .uploadFile(new File(["file"], "notes.txt", { type: "text/plain" }))
        .catch((caught) => caught);

      expect(error).toBeInstanceOf(Error);
      expect(error.name).toBe("TimeoutError");
      expect(error.message).toBe("附件上传超时，请重试");
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      delete (window as any).electronAPI;
    }
  });

  it("returns url and file_name on successful upload", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          url: "/uploads/img.png",
          file_name: "img.png",
          stored_name: "digest_img.png",
          size: 7,
          digest: "sha256:image",
          version: 1,
          upload_receipt: "upload-receipt:v1:fetch",
        }),
    } as unknown as Response);

    const file = new File(["content"], "img.png", { type: "image/png" });
    const result = await chatApi.uploadFile(file);
    expect(result).toEqual({
      url: "/uploads/img.png",
      file_name: "img.png",
      stored_name: "digest_img.png",
      size: 7,
      digest: "sha256:image",
      version: 1,
      upload_receipt: "upload-receipt:v1:fetch",
    });
  });

  it("sends POST to /api/console/upload", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ url: "", file_name: "" }),
    } as unknown as Response);

    await chatApi.uploadFile(new File([""], "f.txt"));
    expect(fetch).toHaveBeenCalledWith(
      "/api/console/upload",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("passes an abort signal to the upload request", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ url: "", file_name: "" }),
    } as unknown as Response);
    const controller = new AbortController();

    await chatApi.uploadFile(new File([""], "f.txt"), {
      signal: controller.signal,
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/console/upload",
      expect.objectContaining({ signal: controller.signal }),
    );
  });

  it("throws error with status code on upload failure", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 413,
      statusText: "Payload Too Large",
      text: () => Promise.resolve("File too large"),
    } as unknown as Response);

    await expect(chatApi.uploadFile(new File([""], "big.bin"))).rejects.toThrow(
      "Upload failed: 413 Payload Too Large - File too large",
    );
  });

  it("throws error without dash when upload fails with empty body", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      text: () => Promise.resolve(""),
    } as unknown as Response);

    const err = await chatApi
      .uploadFile(new File([""], "f.bin"))
      .catch((e) => e);
    expect(err.message).toBe("Upload failed: 500 Internal Server Error");
  });
});

// ---------------------------------------------------------------------------
// listChats — query string construction
// ---------------------------------------------------------------------------
describe("chatApi.listChats", () => {
  beforeEach(() => vi.mocked(request).mockResolvedValue([]));
  afterEach(() => vi.clearAllMocks());

  it("calls /chats with no params", async () => {
    await chatApi.listChats();
    expect(request).toHaveBeenCalledWith("/chats");
  });

  it("builds query string with user_id", async () => {
    await chatApi.listChats({ user_id: "u1" });
    expect(request).toHaveBeenCalledWith("/chats?user_id=u1");
  });

  it("builds query string with channel", async () => {
    await chatApi.listChats({ channel: "console" });
    expect(request).toHaveBeenCalledWith("/chats?channel=console");
  });

  it("both params appear in query when both are provided", async () => {
    await chatApi.listChats({ user_id: "u1", channel: "dingtalk" });
    expect(request).toHaveBeenCalledWith(expect.stringContaining("user_id=u1"));
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining("channel=dingtalk"),
    );
  });

  it("can request only ordinary chat sessions", async () => {
    await chatApi.listChats({ mode: "chat" });
    expect(request).toHaveBeenCalledWith("/chats?mode=chat");
  });

  it("loads the lightweight active-run projection", async () => {
    await chatApi.listChatActivity();
    expect(request).toHaveBeenCalledWith("/chats/activity");
  });
});

// ---------------------------------------------------------------------------
// Other methods — verify path and HTTP method
// ---------------------------------------------------------------------------
describe("chatApi CRUD", () => {
  beforeEach(() => vi.mocked(request).mockResolvedValue(undefined));
  afterEach(() => vi.clearAllMocks());

  it("getChat encodes chatId and sends GET", async () => {
    await chatApi.getChat("chat/1");
    expect(request).toHaveBeenCalledWith("/chats/chat%2F1");
  });

  it("continues a ququ through the isolated non-turn endpoint", async () => {
    const payload = {
      response_id: "response-1",
      user_text: "最近有点累。",
      answer_text: "那就先歇一会儿。",
      ququ_text: "我不太想在这时候递鸡汤。",
    };

    await chatApi.continueQuqu(payload);

    expect(request).toHaveBeenCalledWith(
      "/console/ququ/continue",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  });

  it("branchChat sends the stable user message id", async () => {
    await chatApi.branchChat("chat/1", {
      user_message_id: "user-message-2",
    });
    expect(request).toHaveBeenCalledWith(
      "/chats/chat%2F1/branch",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ user_message_id: "user-message-2" }),
      }),
    );
  });

  it("recoverLegacyHistory explicitly copies the quarantined source", async () => {
    await chatApi.recoverLegacyHistory("chat/legacy");
    expect(request).toHaveBeenCalledWith(
      "/chats/chat%2Flegacy/legacy-history-recovery",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("updateChat sends PUT to the correct path", async () => {
    await chatApi.updateChat("chat-1", { name: "New Name" });
    expect(request).toHaveBeenCalledWith(
      "/chats/chat-1",
      expect.objectContaining({ method: "PUT" }),
    );
  });

  it("deleteChat sends DELETE to the correct path", async () => {
    await chatApi.deleteChat("chat-1");
    expect(request).toHaveBeenCalledWith(
      "/chats/chat-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("stopChat encodes chatId and appends query param", async () => {
    await chatApi.stopChat("chat/1");
    expect(request).toHaveBeenCalledWith(
      "/console/chat/stop?chat_id=chat%2F1",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("stopChat carries the observed turn id as an attempt fence", async () => {
    await chatApi.stopChat("chat/1", "turn/2");

    expect(request).toHaveBeenCalledWith(
      "/console/chat/stop?chat_id=chat%2F1&turn_id=turn%2F2",
      { method: "POST" },
    );
  });

  it("getChatEvents encodes chatId and after_seq", async () => {
    await chatApi.getChatEvents("chat/1", 7);
    expect(request).toHaveBeenCalledWith("/chats/chat%2F1/events?after_seq=7");
  });

  it("getChatEvents can request a bounded event page", async () => {
    await chatApi.getChatEvents("chat/1", 7, { limit: 25 });
    expect(request).toHaveBeenCalledWith(
      "/chats/chat%2F1/events?after_seq=7&limit=25",
    );
  });

  it("getChatEvents can request only collaboration tool event types", async () => {
    await chatApi.getChatEvents("chat/1", 0, {
      eventTypes: [
        "tool.call.started",
        "tool.call.delta",
        "tool.call.completed",
      ],
    });
    expect(request).toHaveBeenCalledWith(
      "/chats/chat%2F1/events?after_seq=0&event_type=tool.call.started&event_type=tool.call.delta&event_type=tool.call.completed",
    );
  });

  it("subscribeChatEvents streams runtime event replay with auth-capable fetch", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'data: {"chat_id":"chat/1","after_seq":7,"latest_seq":8,"events":[{"id":"evt-1","seq":8,"type":"turn.started","source":"user","payload":{},"created_at":"2026-05-21T10:00:00Z"}]}\n\n',
          ),
        );
        controller.close();
      },
    });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body,
    } as unknown as Response);
    const onUpdate = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      afterSeq: 7,
      reconnectMs: 1,
    });

    await vi.waitFor(() => {
      expect(onUpdate).toHaveBeenCalledWith(
        expect.objectContaining({
          chat_id: "chat/1",
          latest_seq: 8,
        }),
      );
    });
    unsubscribe();

    expect(fetch).toHaveBeenCalledWith(
      "/api/chats/chat%2F1/events/stream?after_seq=7",
      expect.objectContaining({
        headers: {},
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("subscribes to a child chat in the child agent workspace", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.close();
      },
    });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body,
    } as unknown as Response);

    const unsubscribe = chatApi.subscribeChatEvents("child-chat", vi.fn(), {
      agentId: "Lantai_Water_Agent",
      maxReconnectAttempts: 0,
    });
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    unsubscribe();

    expect(fetch).toHaveBeenCalledWith(
      "/api/chats/child-chat/events/stream?after_seq=0",
      expect.objectContaining({
        headers: { "X-Agent-Id": "Lantai_Water_Agent" },
      }),
    );
  });

  it("subscribeChatEvents reports transport lifecycle and closes after retry limit", async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error("network down"));
    const onUpdate = vi.fn();
    const onError = vi.fn();
    const onTransportStatus = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      reconnectMs: 1,
      maxReconnectAttempts: 1,
      onError,
      onTransportStatus,
    });

    await vi.waitFor(() => {
      expect(onTransportStatus).toHaveBeenCalledWith(
        "closed",
        expect.objectContaining({ message: "network down" }),
      );
    });
    unsubscribe();

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(onUpdate).not.toHaveBeenCalled();
    expect(onTransportStatus.mock.calls.map((call) => call[0])).toEqual([
      "connecting",
      "reconnecting",
      "closed",
    ]);
  });

  it("subscribeChatEvents rapidly retries an initial workspace 404 without surfacing a transport failure", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'data: {"chat_id":"chat/1","after_seq":0,"latest_seq":1,"events":[{"id":"evt-1","seq":1,"type":"turn.started","source":"user","payload":{},"created_at":"2026-05-21T10:00:00Z"}]}\n\n',
          ),
        );
        controller.close();
      },
    });
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 404,
        body: null,
      } as unknown as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body,
      } as unknown as Response);
    const onUpdate = vi.fn();
    const onError = vi.fn();
    const onTransportStatus = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      reconnectMs: 10000,
      maxReconnectAttempts: 1,
      onError,
      onTransportStatus,
    });

    await vi.waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    unsubscribe();

    expect(fetch).toHaveBeenCalledTimes(2);
    expect(onError).not.toHaveBeenCalled();
    expect(onTransportStatus.mock.calls.map((call) => call[0])).not.toContain(
      "closed",
    );
  });

  it("subscribeChatEvents de-duplicates replayed runtime events", async () => {
    const body = new ReadableStream({
      start(controller) {
        const frame =
          'data: {"chat_id":"chat/1","after_seq":7,"latest_seq":8,"events":[{"id":"evt-dup","seq":8,"type":"message.delta","source":"assistant","payload":{"delta":"你"},"created_at":"2026-05-21T10:00:00Z"}]}\n\n';
        controller.enqueue(new TextEncoder().encode(frame));
        controller.enqueue(new TextEncoder().encode(frame));
        controller.close();
      },
    });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body,
    } as unknown as Response);
    const onUpdate = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      afterSeq: 7,
      reconnectMs: 10000,
    });

    await vi.waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    unsubscribe();

    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onUpdate).toHaveBeenCalledWith(
      expect.objectContaining({
        latest_seq: 8,
        events: [expect.objectContaining({ id: "evt-dup" })],
      }),
    );
  });

  it("subscribeChatEvents reconnects after EOF and resumes from the latest cursor", async () => {
    const firstBody = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'data: {"chat_id":"chat/1","after_seq":0,"latest_seq":4,"events":[{"id":"evt-running","seq":4,"type":"turn.started","status":"running","source":"system","payload":{},"created_at":"2026-05-21T10:00:00Z"}]}\n\n',
          ),
        );
        controller.close();
      },
    });
    const secondBody = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'data: {"chat_id":"chat/1","after_seq":4,"latest_seq":5,"events":[{"id":"evt-terminal","seq":5,"type":"turn.completed","status":"completed","source":"system","payload":{"final_task_status":"finished"},"created_at":"2026-05-21T10:00:01Z"}]}\n\n',
          ),
        );
      },
    });
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: firstBody,
      } as unknown as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: secondBody,
      } as unknown as Response);
    const onUpdate = vi.fn();
    const onTransportStatus = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      afterSeq: 0,
      reconnectMs: 1,
      onTransportStatus,
    });

    await vi.waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(2));
    unsubscribe();

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/api/chats/chat%2F1/events/stream?after_seq=0",
      expect.any(Object),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/chats/chat%2F1/events/stream?after_seq=4",
      expect.any(Object),
    );
    expect(onUpdate.mock.calls[0][0]).toMatchObject({
      latest_seq: 4,
      events: [expect.objectContaining({ id: "evt-running" })],
    });
    expect(onUpdate.mock.calls[1][0]).toMatchObject({
      latest_seq: 5,
      events: [expect.objectContaining({ id: "evt-terminal" })],
    });
    expect(onTransportStatus.mock.calls.map((call) => call[0])).toContain(
      "reconnecting",
    );
  });

  it("reconnects from the last applied cursor when state application fails", async () => {
    const firstBody = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'data: {"chat_id":"chat/1","stream_epoch":"epoch-a","after_seq":0,"latest_seq":4,"events":[{"id":"evt-running","seq":4,"type":"turn.started","status":"running","source":"system","payload":{},"created_at":"2026-05-21T10:00:00Z"}]}\n\n',
          ),
        );
      },
    });
    const secondBody = new ReadableStream({ start() {} });
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: firstBody,
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: secondBody,
      } as Response);
    const applyError = new Error("TurnStore commit failed");
    const onUpdate = vi.fn(() => {
      throw applyError;
    });
    const onError = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      reconnectMs: 1,
      onError,
    });

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    unsubscribe();

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/chats/chat%2F1/events/stream?after_seq=0",
      expect.any(Object),
    );
    expect(onError).toHaveBeenCalledWith(applyError);
  });

  it("subscribeChatEvents reconnects with the server epoch and SSE cursor", async () => {
    const firstBody = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'id: epoch-a:4\nevent: runtime\ndata: {"chat_id":"chat/1","stream_epoch":"epoch-a","cursor":"epoch-a:4","after_seq":0,"latest_seq":4,"events":[{"id":"evt-running","seq":4,"type":"turn.started","source":"system","payload":{},"created_at":"2026-05-21T10:00:00Z"}]}\n\n',
          ),
        );
        controller.close();
      },
    });
    const secondBody = new ReadableStream({ start() {} });
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: firstBody,
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: secondBody,
      } as Response);

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", vi.fn(), {
      reconnectMs: 1,
    });

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    unsubscribe();

    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/chats/chat%2F1/events/stream?after_seq=4&stream_epoch=epoch-a",
      expect.objectContaining({
        headers: expect.objectContaining({ "Last-Event-ID": "epoch-a:4" }),
      }),
    );
  });

  it("subscribeChatEvents clears replay de-duplication after explicit resync", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            [
              'data: {"chat_id":"chat/1","stream_epoch":"epoch-a","after_seq":0,"latest_seq":4,"events":[{"id":"evt-reused","seq":4,"type":"message.delta","source":"assistant","payload":{"delta":"旧"},"created_at":"2026-05-21T10:00:00Z"}]}',
              "",
              'data: {"chat_id":"chat/1","stream_epoch":"epoch-b","after_seq":4,"latest_seq":1,"resync_required":true,"resync_reason":"stream_epoch_mismatch","events":[{"id":"evt-reused","seq":1,"type":"message.delta","source":"assistant","payload":{"delta":"新"},"created_at":"2026-05-21T10:00:01Z"}]}',
              "",
              "",
            ].join("\n"),
          ),
        );
      },
    });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body,
    } as Response);
    const onUpdate = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      reconnectMs: 10000,
    });

    await vi.waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(2));
    unsubscribe();

    expect(onUpdate.mock.calls[1][0]).toMatchObject({
      stream_epoch: "epoch-b",
      resync_required: true,
      events: [expect.objectContaining({ id: "evt-reused", seq: 1 })],
    });
  });

  it("subscribeChatEvents flushes a final runtime event frame at EOF", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'data: {"chat_id":"chat/1","after_seq":7,"latest_seq":8,"events":[{"id":"evt-final","seq":8,"type":"turn.completed","source":"system","payload":{},"created_at":"2026-05-21T10:00:00Z"}]}',
          ),
        );
        controller.close();
      },
    });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body,
    } as unknown as Response);
    const onUpdate = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      afterSeq: 7,
      reconnectMs: 1,
    });

    await vi.waitFor(() => {
      expect(onUpdate).toHaveBeenCalledWith(
        expect.objectContaining({
          latest_seq: 8,
          events: [expect.objectContaining({ id: "evt-final" })],
        }),
      );
    });
    unsubscribe();
  });

  it("subscribeChatEvents parses standard multiline data frames", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            [
              "event: runtime",
              'data: {"chat_id":"chat/1",',
              'data: "after_seq":7,',
              'data: "latest_seq":9,',
              'data: "events":[{"id":"evt-multiline","seq":9,"type":"turn.completed","source":"system","payload":{},"created_at":"2026-05-21T10:00:00Z"}]}',
              "",
              "",
            ].join("\n"),
          ),
        );
        controller.close();
      },
    });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body,
    } as unknown as Response);
    const onUpdate = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      afterSeq: 7,
      reconnectMs: 1,
    });

    await vi.waitFor(() => {
      expect(onUpdate).toHaveBeenCalledWith(
        expect.objectContaining({
          latest_seq: 9,
          events: [expect.objectContaining({ id: "evt-multiline" })],
        }),
      );
    });
    unsubscribe();
  });

  it("subscribeChatEvents reports malformed runtime event frames", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("data: {not json}\n\n"));
        controller.close();
      },
    });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body,
    } as unknown as Response);
    const onUpdate = vi.fn();
    const onError = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      reconnectMs: 1,
      onError,
    });

    await vi.waitFor(() => {
      expect(onError).toHaveBeenCalledWith(expect.any(Error));
    });
    unsubscribe();
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it("subscribeChatEvents reports runtime event payloads without events", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'data: {"detail":"runtime stream unavailable"}\n\n',
          ),
        );
        controller.close();
      },
    });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body,
    } as unknown as Response);
    const onUpdate = vi.fn();
    const onError = vi.fn();

    const unsubscribe = chatApi.subscribeChatEvents("chat/1", onUpdate, {
      reconnectMs: 1,
      onError,
    });

    await vi.waitFor(() => {
      expect(onError).toHaveBeenCalledWith(
        expect.objectContaining({ message: "runtime stream unavailable" }),
      );
    });
    unsubscribe();
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it("interruptTurn posts to the runtime turn endpoint", async () => {
    await chatApi.interruptTurn("chat/1", "turn/1");
    expect(request).toHaveBeenCalledWith(
      "/chats/chat%2F1/turns/turn%2F1/interrupt",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("retryTurn posts to the runtime turn endpoint", async () => {
    await chatApi.retryTurn("chat/1", "turn/1");
    expect(request).toHaveBeenCalledWith(
      "/chats/chat%2F1/turns/turn%2F1/retry",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("batchDeleteChats sends POST with list of ids", async () => {
    await chatApi.batchDeleteChats(["id1", "id2"]);
    expect(request).toHaveBeenCalledWith(
      "/chats/batch-delete",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(["id1", "id2"]),
      }),
    );
  });
});

describe("parseRuntimeEventStreamFrames", () => {
  it("keeps partial SSE data buffered until a frame boundary or EOF", () => {
    const partial = parseRuntimeEventStreamFrames('data: {"ok":true}');
    expect(partial).toEqual({ payloads: [], rest: 'data: {"ok":true}' });

    const flushed = parseRuntimeEventStreamFrames(partial.rest, {
      flush: true,
    });
    expect(flushed).toEqual({ payloads: ['{"ok":true}'], rest: "" });
  });

  it("joins multiline data fields with a newline", () => {
    expect(
      parseRuntimeEventStreamFrames(
        'event: update\ndata: {"a":1,\ndata: "b":2}\n\n',
      ),
    ).toEqual({
      payloads: ['{"a":1,\n"b":2}'],
      rest: "",
    });
  });
});
