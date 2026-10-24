import { beforeEach, describe, expect, it, vi } from "vitest";
import sessionApi, { resolveContentItemUrl } from "./index";
import {
  LANTAI_REQUEST_CARD_CODE,
  LANTAI_RESPONSE_CARD_CODE,
} from "../chatCardCodes";
import { buildNativeRuntimeStreamMessages } from "../runtimeEventStream";
import { LantaiRuntimeMessageType } from "@/vendor/lantaiChatTypes";
import {
  getQuquContinuation,
  resetQuquContinuationStoreForTests,
  upsertQuquContinuation,
} from "../ququContinuationStore";

const {
  mockGetChat,
  mockListChats,
  mockEnsureTopicWorkspace,
  mockDeleteChat,
  mockBranchChat,
  mockUpdateActiveTopicWorkspace,
} = vi.hoisted(() => ({
  mockGetChat: vi.fn(),
  mockListChats: vi.fn(),
  mockEnsureTopicWorkspace: vi.fn(),
  mockDeleteChat: vi.fn(),
  mockBranchChat: vi.fn(),
  mockUpdateActiveTopicWorkspace: vi.fn(() => true),
}));

vi.mock("../../../api", () => ({
  default: {
    getChat: mockGetChat,
    listChats: mockListChats,
    ensureTopicWorkspace: mockEnsureTopicWorkspace,
    deleteChat: mockDeleteChat,
    branchChat: mockBranchChat,
  },
}));

vi.mock("../../../utils/topicWorkspace", () => ({
  setActiveTopicWorkspace: vi.fn(),
  updateActiveTopicWorkspaceFromMeta: mockUpdateActiveTopicWorkspace,
}));

vi.mock("../utils", () => ({
  toDisplayUrl: (url: string) => url,
}));

describe("sessionApi running turn recovery", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    resetQuquContinuationStoreForTests();
    mockUpdateActiveTopicWorkspace.mockClear();
    Object.assign(sessionApi as any, {
      sessionList: [],
      activeAgentId: "default",
      agentContextGeneration: 0,
      selectionEpoch: 0,
      activeSelection: null,
      realIdResolvers: new Map(),
      preferredChatId: null,
      sessionListRequest: null,
      sessionListFetchedAt: 0,
      sessionRequests: new Map(),
      lastSelectedSessionId: null,
      onSessionIdResolved: null,
      onSessionRemoved: null,
      onSessionSelected: null,
      onSessionCreated: null,
    });
    (window as any).currentSessionId = undefined;
    (window as any).currentUserId = undefined;
    (window as any).currentChannel = undefined;
    mockGetChat.mockReset();
    mockListChats.mockReset();
    mockEnsureTopicWorkspace.mockReset();
    mockDeleteChat.mockReset();
    mockBranchChat.mockReset();
  });

  it("preserves a local file source while resolving its display URL", () => {
    expect(
      resolveContentItemUrl({
        type: "file",
        file_url: "file:///tmp/topic/%E5%B8%82%E5%9C%BA%E6%8A%A5%E5%91%8A.md",
        filename: "市场报告.md",
      }),
    ).toEqual(
      expect.objectContaining({
        file_source_url:
          "file:///tmp/topic/%E5%B8%82%E5%9C%BA%E6%8A%A5%E5%91%8A.md",
        file_url: "file:///tmp/topic/%E5%B8%82%E5%9C%BA%E6%8A%A5%E5%91%8A.md",
        file_name: "市场报告.md",
      }),
    );
  });

  it("normalizes tool-delivered source URLs into visible file attachments", () => {
    expect(
      resolveContentItemUrl({
        type: "file",
        source: {
          type: "url",
          url: "file:///tmp/topic/%E9%A1%B9%E7%9B%AE%E8%B7%AF%E6%BC%94PPT.pptx",
        },
        filename: "项目路演PPT.pptx",
      } as any),
    ).toEqual(
      expect.objectContaining({
        file_source_url:
          "file:///tmp/topic/%E9%A1%B9%E7%9B%AE%E8%B7%AF%E6%BC%94PPT.pptx",
        file_url:
          "file:///tmp/topic/%E9%A1%B9%E7%9B%AE%E8%B7%AF%E6%BC%94PPT.pptx",
        file_name: "项目路演PPT.pptx",
      }),
    );
  });

  it("normalizes a tool-delivered video source for history playback", () => {
    expect(
      resolveContentItemUrl({
        type: "video",
        source: {
          type: "url",
          url: "file:///tmp/topic/generated-video.mp4",
        },
      } as any),
    ).toEqual(
      expect.objectContaining({
        type: "video",
        video_url: "file:///tmp/topic/generated-video.mp4",
      }),
    );
  });

  it("does not overwrite a preserved local file source when normalized twice", () => {
    expect(
      resolveContentItemUrl({
        type: "file",
        file_source_url:
          "file:///tmp/topic/%E5%B8%82%E5%9C%BA%E6%8A%A5%E5%91%8A.docx",
        file_url:
          "http://127.0.0.1:8088/api/files/preview/tmp/topic/%E5%B8%82%E5%9C%BA%E6%8A%A5%E5%91%8A.docx",
        filename: "市场报告.docx",
      }),
    ).toEqual(
      expect.objectContaining({
        file_source_url:
          "file:///tmp/topic/%E5%B8%82%E5%9C%BA%E6%8A%A5%E5%91%8A.docx",
        file_url:
          "http://127.0.0.1:8088/api/files/preview/tmp/topic/%E5%B8%82%E5%9C%BA%E6%8A%A5%E5%91%8A.docx",
      }),
    );
  });

  it("loads regular and coding conversations into one session history", async () => {
    mockListChats.mockResolvedValue([
      {
        id: "regular-chat",
        name: "产品讨论",
        session_id: "console:regular",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
      {
        id: "coding-chat",
        name: "修复登录流程",
        session_id: "console:coding:login",
        user_id: "default",
        channel: "console",
        meta: { coding_mode: true },
        status: "idle",
      },
    ]);

    const sessions = await sessionApi.getSessionList();

    expect(mockListChats).toHaveBeenCalledWith({
      mode: "all",
      include_archived: true,
    });
    expect(sessions.map((session) => session.id).sort()).toEqual([
      "coding-chat",
      "regular-chat",
    ]);
  });

  it("serves a warm session list without another blocking backend request", async () => {
    mockListChats.mockResolvedValue([
      {
        id: "cached-chat",
        name: "缓存会话",
        session_id: "console:cached",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);

    const first = await sessionApi.getSessionList();
    const warm = await sessionApi.getSessionList();

    expect(first.map((session) => session.id)).toEqual(["cached-chat"]);
    expect(warm.map((session) => session.id)).toEqual(["cached-chat"]);
    expect(mockListChats).toHaveBeenCalledTimes(1);
  });

  it("cannot commit a late session list from the previously selected agent", async () => {
    let resolveAgentOne:
      | ((value: Array<Record<string, unknown>>) => void)
      | undefined;
    let resolveAgentTwo:
      | ((value: Array<Record<string, unknown>>) => void)
      | undefined;
    mockListChats
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveAgentOne = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveAgentTwo = resolve;
          }),
      );

    window.sessionStorage.setItem(
      "lantai-agent-storage",
      JSON.stringify({ state: { selectedAgent: "agent-one" } }),
    );
    const agentOneRequest = sessionApi.getSessionList();

    window.sessionStorage.setItem(
      "lantai-agent-storage",
      JSON.stringify({ state: { selectedAgent: "agent-two" } }),
    );
    const agentTwoRequest = sessionApi.getSessionList();

    resolveAgentTwo?.([
      {
        id: "agent-two-chat",
        session_id: "console:agent-two",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await expect(agentTwoRequest).resolves.toEqual([
      expect.objectContaining({ id: "agent-two-chat" }),
    ]);

    resolveAgentOne?.([
      {
        id: "agent-one-chat",
        session_id: "console:agent-one",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await expect(agentOneRequest).resolves.toEqual([]);
    expect((sessionApi as any).sessionList).toEqual([
      expect.objectContaining({ id: "agent-two-chat" }),
    ]);
  });

  it("persists unsent drafts independently across sessions and known aliases", () => {
    const draftSessionApi = sessionApi as any;
    draftSessionApi.sessionList = [
      {
        id: "local-session-1",
        realId: "real-session-1",
        sessionId: "console:session-1",
        messages: [],
      },
      {
        id: "session-2",
        realId: "session-2",
        sessionId: "console:session-2",
        messages: [],
      },
    ];
    draftSessionApi.rememberSessionAlias("local-session-1", "real-session-1");

    draftSessionApi.saveDraftText("local-session-1", "会话一草稿");
    draftSessionApi.saveDraftText("session-2", "会话二草稿");

    expect(draftSessionApi.loadDraftText("real-session-1")).toBe("会话一草稿");
    expect(draftSessionApi.loadDraftText("session-2")).toBe("会话二草稿");

    draftSessionApi.clearDraftText("real-session-1");

    expect(draftSessionApi.loadDraftText("local-session-1")).toBe("");
    expect(draftSessionApi.loadDraftText("session-2")).toBe("会话二草稿");
  });

  it("persists ready attachment drafts by agent, session, and alias", () => {
    const draftSessionApi = sessionApi as any;
    window.sessionStorage.setItem(
      "lantai-agent-storage",
      JSON.stringify({ state: { selectedAgent: "agent-one" } }),
    );
    draftSessionApi.sessionList = [
      {
        id: "local-attachment-session",
        realId: "real-attachment-session",
        sessionId: "console:attachment-session",
        messages: [],
      },
    ];
    draftSessionApi.rememberSessionAlias(
      "local-attachment-session",
      "real-attachment-session",
    );

    draftSessionApi.saveDraftAttachments("local-attachment-session", [
      {
        id: "attachment-1",
        name: "研究资料.pdf",
        type: "application/pdf",
        size: 2048,
        url: "/files/preview/research.pdf",
        status: "ready",
      },
    ]);

    expect(
      draftSessionApi.loadDraftAttachments("real-attachment-session"),
    ).toEqual([
      expect.objectContaining({
        name: "研究资料.pdf",
        url: "/files/preview/research.pdf",
        status: "ready",
      }),
    ]);

    window.sessionStorage.setItem(
      "lantai-agent-storage",
      JSON.stringify({ state: { selectedAgent: "agent-two" } }),
    );
    expect(
      draftSessionApi.loadDraftAttachments("real-attachment-session"),
    ).toEqual([]);

    window.sessionStorage.setItem(
      "lantai-agent-storage",
      JSON.stringify({ state: { selectedAgent: "agent-one" } }),
    );
    draftSessionApi.clearDraftAttachments("real-attachment-session");
    expect(
      draftSessionApi.loadDraftAttachments("local-attachment-session"),
    ).toEqual([]);
  });

  it("fails closed when a shared session alias could join two chats", () => {
    const guardedSessionApi = sessionApi as any;
    guardedSessionApi.sessionList = [
      {
        id: "chat-one",
        realId: "chat-one",
        sessionId: "console:shared",
        messages: [],
      },
      {
        id: "chat-two",
        realId: "chat-two",
        sessionId: "console:shared",
        messages: [],
      },
    ];
    window.sessionStorage.setItem(
      "lantai_session_alias_default:chat-one",
      JSON.stringify(["chat-one", "chat-two", "console:shared"]),
    );

    guardedSessionApi.stageLastUserMessage(
      ["chat-one", "console:shared"],
      "只属于会话一",
    );

    expect(
      window.sessionStorage.getItem("lantai_pending_user_msg_default:chat-one"),
    ).toBe("只属于会话一");
    expect(
      window.sessionStorage.getItem("lantai_pending_user_msg_default:chat-two"),
    ).toBeNull();
    expect(
      window.sessionStorage.getItem(
        "lantai_pending_user_msg_default:console%3Ashared",
      ),
    ).toBeNull();
  });

  it("does not restore accepted or pending text from another independent chat that reused a session alias", async () => {
    const isolatedSessionApi = sessionApi as any;
    isolatedSessionApi.sessionList = [
      {
        id: "chat-a",
        name: "把这个做成PPT",
        realId: "chat-a",
        sessionId: "console:reused-transport",
        messages: [],
      },
    ];
    isolatedSessionApi.acceptLastUserMessage(
      ["chat-a", "console:reused-transport"],
      "ONLY_CHAT_A_OLD_MESSAGE",
      "turn-chat-a",
    );
    isolatedSessionApi.stageLastUserMessage(
      ["chat-a", "console:reused-transport"],
      "ONLY_CHAT_A_PENDING",
    );

    isolatedSessionApi.sessionList = [
      {
        id: "chat-b",
        name: "把这个做成PPT",
        realId: "chat-b",
        sessionId: "console:reused-transport",
        messages: [],
      },
    ];
    mockGetChat.mockResolvedValue({
      chat_id: "chat-b",
      session_id: "console:reused-transport",
      status: "running",
      messages: [],
    });

    const chatB = (await sessionApi.getSession("chat-b")) as any;
    const rendered = JSON.stringify(chatB.messages);

    expect(rendered).not.toContain("ONLY_CHAT_A_OLD_MESSAGE");
    expect(rendered).not.toContain("ONLY_CHAT_A_PENDING");
  });

  it("rejects history whose canonical chat owner differs from the requested chat", async () => {
    mockGetChat.mockResolvedValue({
      chat_id: "chat-a",
      session_id: "console:chat-a",
      status: "idle",
      messages: [
        {
          id: "chat-a-user",
          role: "user",
          type: "message",
          content: [
            { type: "text", text: "ONLY_CHAT_A_HISTORY" },
            {
              type: "file",
              file_url: "/files/ONLY_CHAT_A_ATTACHMENT.pdf",
              filename: "ONLY_CHAT_A_ATTACHMENT.pdf",
            },
          ],
        },
      ],
    });

    await expect(sessionApi.getSession("chat-b")).rejects.toThrow(
      "different chat",
    );
  });

  it("removes text, attachment, and pending-message state with a session", async () => {
    const draftSessionApi = sessionApi as any;
    window.sessionStorage.setItem(
      "lantai-agent-storage",
      JSON.stringify({ state: { selectedAgent: "agent-one" } }),
    );
    draftSessionApi.rememberSessionAlias(
      "local-deleted-session",
      "real-deleted-session",
      "console:deleted-session",
    );
    Object.assign(draftSessionApi, {
      sessionList: [
        {
          id: "local-deleted-session",
          realId: "real-deleted-session",
          sessionId: "console:deleted-session",
          messages: [],
        },
      ],
    });
    draftSessionApi.saveDraftText("local-deleted-session", "不要残留");
    draftSessionApi.saveDraftAttachments("local-deleted-session", [
      {
        id: "attachment-deleted",
        name: "待删除.pdf",
        type: "application/pdf",
        size: 2048,
        url: "/files/deleted.pdf",
        status: "ready",
      },
    ]);
    window.sessionStorage.setItem(
      "lantai_pending_user_msg_real-deleted-session",
      "待发送内容",
    );
    sessionApi.setLastUserMessage(
      "real-deleted-session",
      "删除时也必须清理的已接受输入",
    );

    await sessionApi.removeSession({ id: "local-deleted-session" });

    expect(mockDeleteChat).toHaveBeenCalledWith("real-deleted-session");
    expect(draftSessionApi.loadDraftText("real-deleted-session")).toBe("");
    expect(
      draftSessionApi.loadDraftAttachments("real-deleted-session"),
    ).toEqual([]);
    expect(
      window.sessionStorage.getItem(
        "lantai_pending_user_msg_real-deleted-session",
      ),
    ).toBeNull();
    expect(
      window.sessionStorage.getItem(
        "lantai_accepted_user_messages_v1_real-deleted-session",
      ),
    ).toBeNull();
  });

  it("creates an authoritative branch session from a persisted user message", async () => {
    const onSessionSelected = vi.fn();
    sessionApi.onSessionSelected = onSessionSelected;
    Object.assign(sessionApi as any, {
      sessionList: [
        {
          id: "source-chat",
          realId: "source-chat",
          sessionId: "console:source",
          userId: "default",
          channel: "console",
          name: "原会话",
          messages: [],
          meta: {},
        },
      ],
    });
    mockBranchChat.mockResolvedValue({
      chat_id: "branch-chat",
      session_id: "console:branch:branch-chat",
      name: "原会话 · 分支",
      user_id: "default",
      channel: "console",
      status: "idle",
      meta: { branched_from_chat_id: "source-chat" },
      messages: [
        {
          role: "user",
          content: [{ type: "text", text: "保留的第一问" }],
          metadata: { original_id: "user-1" },
        },
      ],
    });

    const sessions = await sessionApi.branchSession("source-chat", "user-2");

    expect(mockBranchChat).toHaveBeenCalledWith("source-chat", {
      user_message_id: "user-2",
    });
    expect(sessions[0]).toMatchObject({
      id: "branch-chat",
      realId: "branch-chat",
      sessionId: "console:branch:branch-chat",
      generating: false,
    });
    expect(sessions[0].messages?.[0]).toMatchObject({
      id: "user-1",
      role: "user",
      metadata: {
        client_turn_id: "user-1",
        client_turn_id_origin: "message_id_fallback",
      },
      cards: [
        {
          data: {
            client_turn_id: "user-1",
            client_turn_id_origin: "message_id_fallback",
            metadata: {
              client_turn_id: "user-1",
              client_turn_id_origin: "message_id_fallback",
            },
            input: [
              {
                metadata: {
                  client_turn_id: "user-1",
                  client_turn_id_origin: "message_id_fallback",
                },
              },
            ],
          },
        },
      ],
    });
    expect((window as any).currentSessionId).toBe("console:branch:branch-chat");
    expect(onSessionSelected).toHaveBeenCalledWith(
      "branch-chat",
      "branch-chat",
    );
  });

  it("does not hydrate continuations from an invalid branch response", async () => {
    const leaked = {
      continuation_id: "ququ-continuation:invalid-branch",
      request_id: "request-invalid-branch",
      agent_id: "default",
      workspace_scope_id: "workspace-invalid-branch",
      chat_id: "invalid-branch",
      session_id: "console:invalid-branch",
      user_id: "default",
      channel: "console",
      source_message_id: "message-invalid-branch",
      source_message_digest: "digest-invalid-branch",
      source_ququ_id: "source-invalid-branch",
      ququ_ordinal: 0,
      placement: "echo" as const,
      text: "不应进入恢复态",
      status: "completed" as const,
      created_at: "2026-09-20T01:00:00Z",
      updated_at: "2026-09-20T01:00:01Z",
    };
    const locator = {
      chat_id: leaked.chat_id,
      source_message_id: leaked.source_message_id,
      source_message_digest: leaked.source_message_digest,
      ququ_ordinal: leaked.ququ_ordinal,
      placement: leaked.placement,
    };
    Object.assign(sessionApi as any, {
      sessionList: [
        {
          id: "source-chat",
          realId: "source-chat",
          sessionId: "console:source",
          userId: "default",
          channel: "console",
          name: "原会话",
          messages: [],
          meta: {},
        },
      ],
    });
    mockBranchChat.mockResolvedValue({
      chat_id: "",
      session_id: "",
      ququ_continuations: [leaked],
      messages: [],
    });

    await expect(
      sessionApi.branchSession("source-chat", "user-2"),
    ).rejects.toThrow("后端没有返回有效的分支会话");
    expect(getQuquContinuation(locator)).toBeUndefined();
  });

  it("patches pending_user_message from backend into a running chat", async () => {
    mockGetChat.mockResolvedValue({
      messages: [],
      status: "running",
      chat_id: "chat-real-1",
      session_id: "temp-session-1",
      pending_user_message: "测试一下",
    });

    const session = (await sessionApi.getSession("chat-real-1")) as any;
    const firstCard = session.messages[0]?.cards?.[0];

    expect(firstCard?.code).toBe(LANTAI_REQUEST_CARD_CODE);
    expect(firstCard?.data?.input?.[0]?.content?.[0]?.text).toBe("测试一下");
  });

  it("hydrates saved ququ continuations without letting stale history clear or replace them", async () => {
    const restored = {
      continuation_id: "ququ-continuation:history",
      request_id: "request-history",
      agent_id: "default",
      workspace_scope_id: "workspace-history",
      chat_id: "chat-ququ-history",
      session_id: "console:ququ-history",
      user_id: "default",
      channel: "console",
      source_message_id: "message-ququ-history",
      source_message_digest: "digest-history",
      source_ququ_id: "source-ququ-history",
      ququ_ordinal: 0,
      placement: "echo" as const,
      text: "历史恢复的原文",
      status: "completed" as const,
      created_at: "2026-09-20T01:00:00Z",
      updated_at: "2026-09-20T01:00:01Z",
    };
    const locator = {
      chat_id: restored.chat_id,
      source_message_id: restored.source_message_id,
      source_message_digest: restored.source_message_digest,
      ququ_ordinal: restored.ququ_ordinal,
      placement: restored.placement,
    };
    mockGetChat.mockResolvedValue({
      messages: [],
      status: "idle",
      chat_id: restored.chat_id,
      session_id: restored.session_id,
      user_id: restored.user_id,
      channel: restored.channel,
      ququ_continuations: [restored],
    });

    await sessionApi.getSession(restored.chat_id);
    expect(getQuquContinuation(locator)?.text).toBe("历史恢复的原文");
    expect(getQuquContinuation(locator)?.continuation_id).toBe(
      "ququ-continuation:history",
    );

    upsertQuquContinuation({
      ...restored,
      text: "响应先到的已确认原文",
      updated_at: "2026-09-20T01:00:03Z",
    });
    mockGetChat.mockResolvedValue({
      messages: [],
      status: "idle",
      chat_id: restored.chat_id,
      session_id: restored.session_id,
      user_id: restored.user_id,
      channel: restored.channel,
      ququ_continuations: [restored],
    });
    await sessionApi.getSession(restored.chat_id);
    expect(getQuquContinuation(locator)?.text).toBe("响应先到的已确认原文");
    expect(getQuquContinuation(locator)?.continuation_id).toBe(
      "ququ-continuation:history",
    );

    mockGetChat.mockResolvedValue({
      messages: [],
      status: "idle",
      chat_id: restored.chat_id,
      session_id: restored.session_id,
      user_id: restored.user_id,
      channel: restored.channel,
    });
    await sessionApi.getSession(restored.chat_id);
    expect(getQuquContinuation(locator)?.text).toBe("响应先到的已确认原文");
    expect(getQuquContinuation(locator)?.continuation_id).toBe(
      "ququ-continuation:history",
    );
    expect(mockGetChat).toHaveBeenCalledTimes(3);
  });

  it("uses runtime_state as the primary running source", async () => {
    mockGetChat.mockResolvedValue({
      messages: [],
      status: "idle",
      chat_id: "chat-runtime-state",
      session_id: "console:runtime-state",
      pending_user_message: "切页回来还在跑",
      runtime_state: {
        status: "running",
        run_id: "run-runtime-state",
        summary: "本轮已开始",
        events_preview: [
          {
            id: "evt-runtime-state",
            ts: "2026-05-23T10:00:00Z",
            kind: "chat.turn.started",
            status: "running",
            label: "开始运行",
            detail: "文本：切页回来还在跑",
          },
        ],
        can_reconnect: true,
        can_stop: true,
      },
    });

    const session = (await sessionApi.getSession("chat-runtime-state")) as any;
    const firstCard = session.messages[0]?.cards?.[0];

    expect(session.generating).toBe(true);
    expect(session.runtimeState?.run_id).toBe("run-runtime-state");
    expect(firstCard?.data?.input?.[0]?.content?.[0]?.text).toBe(
      "切页回来还在跑",
    );
  });

  it("keeps cached user text visible when a new chat has not started streaming yet", async () => {
    sessionApi.setLastUserMessage(
      ["chat-real-idle", "session-idle"],
      "还没打到聊天接口的 prompt",
    );
    mockGetChat.mockResolvedValue({
      messages: [],
      status: "idle",
      chat_id: "chat-real-idle",
      session_id: "session-idle",
    });

    const session = (await sessionApi.getSession("chat-real-idle")) as any;
    const firstCard = session.messages[0]?.cards?.[0];

    expect(firstCard?.code).toBe(LANTAI_REQUEST_CARD_CODE);
    expect(firstCard?.data?.input?.[0]?.content?.[0]?.text).toBe(
      "还没打到聊天接口的 prompt",
    );
  });

  it("does not promote a staged prompt until the backend accepts the turn", () => {
    const ids = ["chat-staged", "console:staged"];

    sessionApi.stageLastUserMessage(ids, "尚未被后端接受");

    expect(
      window.sessionStorage.getItem(
        "lantai_pending_user_msg_default:chat-staged",
      ),
    ).toBe("尚未被后端接受");
    expect(
      window.sessionStorage.getItem(
        "lantai_accepted_user_messages_v1_default:chat-staged",
      ),
    ).toBeNull();

    sessionApi.discardPendingUserMessage(ids);
    expect(
      window.sessionStorage.getItem(
        "lantai_pending_user_msg_default:chat-staged",
      ),
    ).toBeNull();

    sessionApi.acceptLastUserMessage(ids, "已被后端接受", "client-turn-staged");
    expect(
      window.sessionStorage.getItem(
        "lantai_accepted_user_messages_v1_default:chat-staged",
      ),
    ).toContain("client-turn:client-turn-staged");
    expect(
      window.sessionStorage.getItem(
        "lantai_accepted_user_messages_v1_default:chat-staged",
      ),
    ).toContain('"clientTurnId":"client-turn-staged"');
  });

  it("restores the real client turn id instead of the cache-key prefix", async () => {
    const ids = ["chat-cached-client-id", "console:cached-client-id"];
    sessionApi.acceptLastUserMessage(ids, "保留这一轮", "client-turn-real-id");
    mockGetChat.mockResolvedValue({
      messages: [],
      status: "running",
      chat_id: "chat-cached-client-id",
      session_id: "console:cached-client-id",
      pending_user_message: "保留这一轮",
      runtime_state: {
        status: "running",
        run_id: "run-cached-client-id",
        events_preview: [],
      },
    });

    const session = (await sessionApi.getSession(
      "chat-cached-client-id",
    )) as any;
    const requestData = session.messages[0]?.cards?.[0]?.data;

    expect(requestData?.metadata?.client_turn_id).toBe("client-turn-real-id");
    expect(requestData?.input?.[0]?.metadata?.client_turn_id).toBe(
      "client-turn-real-id",
    );
  });

  it("keeps the first prompt visible when a new session history loads before streaming starts", async () => {
    const createdIds: string[] = [];
    sessionApi.onSessionCreated = (sessionId) => {
      createdIds.push(sessionId);
    };
    mockEnsureTopicWorkspace.mockResolvedValue({
      chat: {
        id: "chat-real-new",
        session_id: "console:new-chat",
        name: "你好啊",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    });

    await sessionApi.createSession({ name: "你好啊", messages: [] });
    const localSessionId = createdIds[0];
    mockGetChat.mockResolvedValue({
      messages: [],
      status: "idle",
      chat_id: "chat-real-new",
      session_id: "console:new-chat",
      name: "你好啊",
    });

    const session = (await sessionApi.getSession(localSessionId)) as any;
    const firstCard = session.messages[0]?.cards?.[0];

    expect(firstCard?.code).toBe(LANTAI_REQUEST_CARD_CODE);
    expect(firstCard?.data?.input?.[0]?.content?.[0]?.text).toBe("你好啊");
  });

  it("persists a knowledge source when creating a source-bound session", async () => {
    const knowledgeSource = {
      path: ".wiki/raw/sources/budget.xlsx",
      filename: "budget.xlsx",
      kind: "office",
    };
    mockEnsureTopicWorkspace.mockResolvedValue({
      chat: {
        id: "chat-knowledge",
        session_id: "console:knowledge",
        name: "关于 budget.xlsx",
        user_id: "default",
        channel: "console",
        meta: { knowledge_source: knowledgeSource },
        status: "idle",
      },
    });

    const sessions = await sessionApi.createSession({
      name: "关于 budget.xlsx",
      messages: [],
      meta: { knowledge_source: knowledgeSource },
    });

    expect(mockEnsureTopicWorkspace).toHaveBeenCalledWith(
      expect.objectContaining({ knowledge_source: knowledgeSource }),
    );
    expect((sessions[0] as any).meta.knowledge_source).toEqual(knowledgeSource);
  });

  it("waits for a server-confirmed research association before exposing the session", async () => {
    let resolveWorkspace!: (value: unknown) => void;
    mockEnsureTopicWorkspace.mockReturnValue(new Promise((resolve) => { resolveWorkspace = resolve; }));
    const pending = sessionApi.createSession({ name: "Research", meta: { research_project_id: "project-a" } });
    expect(mockEnsureTopicWorkspace).toHaveBeenCalledWith(expect.objectContaining({ research_project_id: "project-a" }));
    expect(sessionApi.sessionList).toHaveLength(0);
    resolveWorkspace({ chat: { id: "research-chat", session_id: "research-session", meta: { research_project_id: "project-a" }, status: "idle" } });
    const sessions = await pending;
    expect(sessions[0].meta).toMatchObject({ research_project_id: "project-a" });
    expect(sessionApi.getRealIdForSession(sessions[0].id)).toBe("research-chat");
  });

  it("does not expose an unbound research session when preparation fails", async () => {
    mockEnsureTopicWorkspace.mockRejectedValue(new Error("project unavailable"));
    await expect(sessionApi.createSession({ meta: { research_project_id: "project-a" } })).rejects.toThrow("project unavailable");
    expect(sessionApi.sessionList).toHaveLength(0);
  });

  it("rejects a research response that did not persist the requested association", async () => {
    mockEnsureTopicWorkspace.mockResolvedValue({ chat: { id: "research-chat", session_id: "research-session", meta: {} } });
    await expect(sessionApi.createSession({ meta: { research_project_id: "project-a" } })).rejects.toThrow();
    expect(sessionApi.sessionList).toHaveLength(0);
  });

  it("opens a new local conversation without waiting for workspace I/O", async () => {
    let resolveWorkspace:
      | ((value: {
          chat: {
            id: string;
            session_id: string;
            name: string;
            user_id: string;
            channel: string;
            meta: Record<string, unknown>;
            status: string;
          };
        }) => void)
      | null = null;
    mockEnsureTopicWorkspace.mockReturnValue(
      new Promise((resolve) => {
        resolveWorkspace = resolve;
      }),
    );

    const created = await sessionApi.createSession({
      name: "New Chat",
      messages: [],
    });

    expect(created).toHaveLength(1);
    expect(created[0].id).toMatch(/^local:/);
    expect((created[0] as any).realId).toBeUndefined();

    resolveWorkspace?.({
      chat: {
        id: "chat-background-created",
        session_id: "console:background-created",
        name: "New Chat",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(sessionApi.getRealIdForSession(String(created[0].id))).toBe(
      "chat-background-created",
    );
  });

  it("assigns collision-safe identities to consecutive local conversations", async () => {
    mockEnsureTopicWorkspace.mockReturnValue(new Promise(() => undefined));

    const first = await sessionApi.createSession({
      name: "First local chat",
      messages: [],
    });
    const second = await sessionApi.createSession({
      name: "Second local chat",
      messages: [],
    });

    expect(first[0].id).toMatch(/^local:/);
    expect(second[0].id).toMatch(/^local:/);
    expect(second[0].id).not.toBe(first[0].id);
  });

  it("does not let an older workspace resolution replace the active conversation", async () => {
    let resolveFirstWorkspace!: (value: any) => void;
    mockEnsureTopicWorkspace
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirstWorkspace = resolve;
          }),
      )
      .mockResolvedValueOnce({
        chat: {
          id: "chat-second",
          session_id: "console:second",
          name: "Second",
          user_id: "default",
          channel: "console",
          meta: { topic_workspace_dir: "/tmp/second" },
          status: "idle",
        },
      });

    await sessionApi.createSession({ name: "First", messages: [] });
    await sessionApi.createSession({ name: "Second", messages: [] });
    await Promise.resolve();
    await Promise.resolve();
    expect(mockUpdateActiveTopicWorkspace).toHaveBeenCalledWith(
      "default",
      "console:second",
      expect.any(Object),
      "chat-second",
    );

    resolveFirstWorkspace({
      chat: {
        id: "chat-first",
        session_id: "console:first",
        name: "First",
        user_id: "default",
        channel: "console",
        meta: { topic_workspace_dir: "/tmp/first" },
        status: "idle",
      },
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(mockUpdateActiveTopicWorkspace).not.toHaveBeenCalledWith(
      "default",
      "console:first",
      expect.any(Object),
      "chat-first",
    );
  });

  it("drops stale pending user text once backend history is complete", async () => {
    (sessionApi as any).sessionList = [
      {
        id: "chat-complete",
        name: "New Chat",
        sessionId: "console:complete",
        userId: "default",
        channel: "console",
        messages: [],
        meta: {},
        pendingUserMessage: "已经落盘的输入",
      },
    ];
    sessionApi.setLastUserMessage(
      ["chat-complete", "console:complete"],
      "已经落盘的输入",
    );
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-complete",
          role: "user",
          type: "message",
          content: [{ type: "text", text: "已经落盘的输入" }],
        },
        {
          id: "assistant-complete",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "OK" }],
        },
      ],
      status: "idle",
      chat_id: "chat-complete",
      session_id: "console:complete",
    });

    const session = (await sessionApi.getSession("chat-complete")) as any;
    const userTexts = session.messages
      .filter((message: any) => message.role === "user")
      .map(
        (message: any) =>
          message.cards?.[0]?.data?.input?.[0]?.content?.[0]?.text,
      );

    expect(session.pendingUserMessage).toBeUndefined();
    expect(userTexts).toEqual(["已经落盘的输入"]);
  });

  it("keeps a cached prompt before a completed assistant-only history", async () => {
    sessionApi.setLastUserMessage(
      ["chat-missing-user", "console:missing-user"],
      "切换会话时不能丢的输入",
    );
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "assistant-only",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "这条回复已经完成" }],
        },
      ],
      status: "idle",
      chat_id: "chat-missing-user",
      session_id: "console:missing-user",
    });

    const session = (await sessionApi.getSession("chat-missing-user")) as any;
    const userTexts = session.messages
      .filter((message: any) => message.role === "user")
      .map(
        (message: any) =>
          message.cards?.[0]?.data?.input?.[0]?.content?.[0]?.text,
      );

    expect(session.messages.map((message: any) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
    expect(userTexts).toEqual(["切换会话时不能丢的输入"]);
    expect(session.pendingUserMessage).toBeUndefined();
  });

  it("restores every accepted user prompt after settings-style history rehydration", async () => {
    sessionApi.setLastUserMessage(
      ["chat-settings-return", "console:settings-return"],
      "第一条不会消失的输入",
    );
    sessionApi.setLastUserMessage(
      ["chat-settings-return", "console:settings-return"],
      "第二条不会消失的输入",
    );
    sessionApi.setLastUserMessage(
      ["chat-settings-return", "console:settings-return"],
      "第三条不会消失的输入",
    );
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "assistant-after-settings-return",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "历史助手回复仍然存在" }],
        },
      ],
      status: "idle",
      chat_id: "chat-settings-return",
      session_id: "console:settings-return",
    });

    const session = (await sessionApi.getSession(
      "chat-settings-return",
    )) as any;
    const userTexts = session.messages
      .filter((message: any) => message.role === "user")
      .map(
        (message: any) =>
          message.cards?.[0]?.data?.input?.[0]?.content?.[0]?.text,
      );

    expect(session.messages.map((message: any) => message.role)).toEqual([
      "user",
      "user",
      "user",
      "assistant",
    ]);
    expect(userTexts).toEqual([
      "第一条不会消失的输入",
      "第二条不会消失的输入",
      "第三条不会消失的输入",
    ]);
  });

  it("deduplicates one submitted client turn across aliases regardless of elapsed time", async () => {
    const now = vi.spyOn(Date, "now");
    now.mockReturnValueOnce(1_000);
    sessionApi.setLastUserMessage(
      ["local-slow-submit", "console:slow-submit"],
      "你仔细地读一读这个内容",
      "client-turn-slow-submit",
    );
    now.mockReturnValueOnce(61_000);
    sessionApi.setLastUserMessage(
      ["chat-slow-submit", "console:slow-submit", "local-slow-submit"],
      "你仔细地读一读这个内容",
      "client-turn-slow-submit",
    );
    now.mockRestore();

    mockGetChat.mockResolvedValue({
      messages: [],
      status: "idle",
      chat_id: "chat-slow-submit",
      session_id: "console:slow-submit",
    });

    const session = (await sessionApi.getSession("chat-slow-submit")) as any;
    const userTexts = session.messages
      .filter((message: any) => message.role === "user")
      .map(
        (message: any) =>
          message.cards?.[0]?.data?.input?.[0]?.content?.[0]?.text,
      );

    expect(userTexts).toEqual(["你仔细地读一读这个内容"]);
  });

  it("splits persisted structured assistant thinking and text into visible runtime outputs", async () => {
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-structured",
          role: "user",
          type: "message",
          content: [{ type: "text", text: "你好，沈行知" }],
        },
        {
          id: "assistant-structured",
          role: "assistant",
          content: [
            {
              type: "thinking",
              thinking: "用户只是打了个招呼，我应该简短回应。",
            },
            {
              type: "text",
              text: "你好。我在。你说。",
            },
          ],
        },
      ],
      status: "idle",
      chat_id: "chat-structured",
      session_id: "console:structured",
    });

    const session = (await sessionApi.getSession("chat-structured")) as any;
    const responseCard = session.messages.find(
      (message: any) => message.role === "assistant",
    )?.cards?.[0];
    const output = responseCard?.data?.output || [];

    expect(responseCard?.code).toBe(LANTAI_RESPONSE_CARD_CODE);
    expect(output).toHaveLength(2);
    expect(output[0].type).toBe(LantaiRuntimeMessageType.REASONING);
    expect(output[0].content?.[0]?.text).toBe(
      "用户只是打了个招呼，我应该简短回应。",
    );
    expect(output[1].type).toBe(LantaiRuntimeMessageType.MESSAGE);
    expect(output[1].content?.[0]?.text).toBe("你好。我在。你说。");
  });

  it("promotes persisted turn identity to response-card metadata", async () => {
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-turn-identity",
          role: "user",
          type: "message",
          content: [{ type: "text", text: "第二条" }],
        },
        {
          id: "assistant-turn-identity",
          role: "assistant",
          type: "message",
          metadata: {
            client_turn_id: "client-turn-two",
            native_runtime_turn_id: "runtime-turn-two",
          },
          content: [{ type: "text", text: "第二条回答" }],
        },
      ],
      status: "idle",
      chat_id: "chat-turn-identity",
      session_id: "console:turn-identity",
    });

    const session = (await sessionApi.getSession("chat-turn-identity")) as any;
    const responseData = session.messages.find(
      (message: any) => message.role === "assistant",
    )?.cards?.[0]?.data;

    expect(responseData?.metadata).toEqual(
      expect.objectContaining({
        client_turn_id: "client-turn-two",
        native_runtime_turn_id: "runtime-turn-two",
      }),
    );
  });

  it("promotes legacy runtime_turn_id so restored progress stays in its run", async () => {
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "assistant-runtime-turn-identity",
          role: "assistant",
          type: "reasoning",
          metadata: {
            client_turn_id: "client-runtime-turn",
            runtime_turn_id: "runtime-turn-legacy",
          },
          content: [{ type: "text", text: "正在核对交付内容。" }],
        },
      ],
      status: "idle",
      chat_id: "chat-runtime-turn-identity",
      session_id: "console:runtime-turn-identity",
    });

    const session = (await sessionApi.getSession(
      "chat-runtime-turn-identity",
    )) as any;
    const responseData = session.messages[0]?.cards?.[0]?.data;

    expect(responseData?.metadata).toEqual(
      expect.objectContaining({
        client_turn_id: "client-runtime-turn",
        runtime_turn_id: "runtime-turn-legacy",
      }),
    );
  });

  it("moves a late-persisted user prompt above runtime controls from its own turn", async () => {
    const clientTurnId = "user-1787437301571-neb8xdmj";
    const controlMetadata = (eventType: string) => ({
      original_id: `runtime-control:${eventType}`,
      metadata: {
        runtime_event_type: eventType,
        client_turn_id: clientTurnId,
        runtime_item_id: "call-collaboration",
      },
    });
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "collaboration-started",
          role: "assistant",
          type: "plugin_call",
          created_at: "2026-08-22T22:21:57Z",
          metadata: controlMetadata("tool.call.started"),
          content: [{ type: "data", data: { call_id: "call-collaboration" } }],
        },
        {
          id: "persisted-user",
          role: "user",
          type: "message",
          created_at: "2026-08-23 06:21:57.670",
          content: [{ type: "text", text: "莫言和余华打架谁会赢？" }],
        },
        {
          id: "collaboration-completed",
          role: "assistant",
          type: "plugin_call_output",
          created_at: "2026-08-22T22:22:24Z",
          metadata: controlMetadata("tool.call.completed"),
          content: [{ type: "data", data: { call_id: "call-collaboration" } }],
        },
        {
          id: "persisted-answer",
          role: "assistant",
          type: "message",
          created_at: "2026-08-23 06:22:46.814",
          metadata: { client_turn_id: clientTurnId },
          content: [{ type: "text", text: "综合结论。" }],
        },
      ],
      status: "idle",
      chat_id: "chat-late-user-anchor",
      session_id: "console:late-user-anchor",
    });

    const session = (await sessionApi.getSession(
      "chat-late-user-anchor",
    )) as any;

    expect(session.messages.map((message: any) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
    expect(
      session.messages[1].cards[0].data.output.map(
        (output: any) => output.type,
      ),
    ).toEqual(["plugin_call", "plugin_call_output", "message"]);
  });

  it("does not wait forever when a local session never resolves a backend realId", async () => {
    vi.useFakeTimers();
    try {
      const createdIds: string[] = [];
      sessionApi.onSessionCreated = (sessionId) => {
        createdIds.push(sessionId);
      };
      mockEnsureTopicWorkspace.mockRejectedValue(new Error("backend down"));

      await sessionApi.createSession({ name: "离线消息", messages: [] });
      const localSessionId = createdIds[0];
      const pending = sessionApi.getSession(localSessionId) as Promise<any>;

      await vi.advanceTimersByTimeAsync(4000);
      const session = await pending;

      expect(session.id).toBe(localSessionId);
      expect(session.sessionId).toBe(localSessionId);
      expect(session.messages).toEqual([]);
      expect(mockGetChat).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses backend channel metadata when opening a channel chat by URL id", async () => {
    mockGetChat.mockResolvedValue({
      messages: [],
      status: "idle",
      chat_id: "wechat-chat-1",
      session_id: "wechat:friend-1",
      name: "微信好友",
      user_id: "friend-1",
      channel: "wechat",
      meta: { source: "wechat" },
      created_at: "2026-05-18T09:00:00Z",
      updated_at: "2026-05-18T09:10:00Z",
      pinned: true,
    });

    sessionApi.activateSession("wechat-chat-1");
    const session = (await sessionApi.getSession("wechat-chat-1")) as any;

    expect(session.name).toBe("微信好友");
    expect(session.channel).toBe("wechat");
    expect(session.userId).toBe("friend-1");
    expect(session.sessionId).toBe("wechat:friend-1");
    expect(session.meta).toEqual({ source: "wechat" });
    expect((window as any).currentChannel).toBe("wechat");
    expect((window as any).currentUserId).toBe("friend-1");
    expect((window as any).currentSessionId).toBe("wechat:friend-1");
  });

  it("does not label history without a persisted timestamp as now", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-27T15:52:00Z"));
    try {
      mockGetChat.mockResolvedValue({
        messages: [
          {
            id: "assistant-without-time",
            role: "assistant",
            type: "message",
            content: [{ type: "text", text: "很早以前的回答" }],
          },
        ],
        status: "idle",
        chat_id: "chat-without-message-time",
        session_id: "console:without-message-time",
      });

      const session = (await sessionApi.getSession(
        "chat-without-message-time",
      )) as any;
      const assistantMessage = session.messages[0];
      const responseData = assistantMessage.cards?.find(
        (card: any) => card.code === LANTAI_RESPONSE_CARD_CODE,
      )?.data;

      expect(assistantMessage.createdAt).toBeNull();
      expect(responseData.created_at).toBeNull();
      expect(responseData.completed_at).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps distinct persisted times on historical assistant turns", async () => {
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-afternoon",
          role: "user",
          type: "message",
          created_at: "2026-07-27 15:08:55.550",
          content: [{ type: "text", text: "下午的问题" }],
        },
        {
          id: "assistant-afternoon",
          role: "assistant",
          type: "message",
          created_at: "2026-07-27 15:09:02.274",
          content: [{ type: "text", text: "下午的回答" }],
        },
        {
          id: "user-evening",
          role: "user",
          type: "message",
          created_at: "2026-07-27 22:23:09.709",
          content: [{ type: "text", text: "晚上的问题" }],
        },
        {
          id: "assistant-evening",
          role: "assistant",
          type: "message",
          created_at: "2026-07-27 22:23:11.242",
          content: [{ type: "text", text: "晚上的回答" }],
        },
      ],
      status: "idle",
      chat_id: "chat-with-history-times",
      session_id: "console:with-history-times",
    });

    const session = (await sessionApi.getSession(
      "chat-with-history-times",
    )) as any;
    const assistantMessages = session.messages.filter(
      (message: any) => message.role === "assistant",
    );

    expect(assistantMessages.map((message: any) => message.createdAt)).toEqual([
      "2026-07-27 15:09:02.274",
      "2026-07-27 22:23:11.242",
    ]);
  });

  it("reconciles a replayed ledger prefix into legacy history without appending a reload-time assistant card", async () => {
    const clientTurnId = "user-1787295661868-a35huk8c";
    const answerId = "msg_86c3aa04311b45338295dcf3971b3fc3";
    const answer = "查完了，先说结论：“蓝鲸汇”不是一个有唯一指向的词。";
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "restored-user-row",
          role: "user",
          type: "message",
          created_at: "2026-08-21 15:01:20.695",
          metadata: {
            original_id: "msg-user-blue-whale",
            metadata: { source: "runtime_schema" },
          },
          content: [{ type: "text", text: "那你知道什么是蓝鲸汇吗" }],
        },
        {
          id: "restored-reasoning-row",
          role: "assistant",
          type: "reasoning",
          created_at: "2026-08-21 15:01:54.175",
          metadata: {
            original_id: answerId,
            original_name: "assistant",
            metadata: { client_turn_id: clientTurnId },
          },
          content: [{ type: "text", text: "Search results are ambiguous." }],
        },
        {
          id: "restored-answer-row",
          role: "assistant",
          type: "message",
          created_at: "2026-08-21 15:01:54.175",
          metadata: {
            original_id: answerId,
            original_name: "assistant",
            metadata: { client_turn_id: clientTurnId },
          },
          content: [{ type: "text", text: answer }],
        },
      ],
      status: "idle",
      chat_id: "chat-blue-whale",
      session_id: "console:blue-whale",
    });

    const session = (await sessionApi.getSession("chat-blue-whale")) as any;
    const messages = buildNativeRuntimeStreamMessages(
      [
        {
          schema_version: 1,
          id: "evt-replayed-answer-prefix",
          seq: 76,
          type: "message.delta",
          status: "running",
          source: "assistant",
          payload: {
            client_turn_id: clientTurnId,
            content_type: "text",
            stream_message_type: "message",
            delta: "查",
            message: {
              id: `${answerId}:message`,
              role: "assistant",
              metadata: { client_turn_id: clientTurnId },
              content: [{ type: "text", text: "查" }],
            },
          },
          usage: null,
          created_at: "2026-08-21T07:01:48Z",
          turn_id: null,
          session_id: "console:blue-whale",
          item_id: `${answerId}:message`,
          presentation: null,
        },
      ],
      session.messages,
      { strictTurnIdentity: true },
    );
    const assistantMessages = messages.filter(
      (message: any) => message.role === "assistant",
    );
    const response = assistantMessages[0]?.cards?.find(
      (card: any) => card.code === LANTAI_RESPONSE_CARD_CODE,
    )?.data;

    expect(assistantMessages).toHaveLength(1);
    expect(response.metadata.client_turn_id).toBe(clientTurnId);
    expect(response.output.map((output: any) => output.id)).toEqual([
      `${answerId}:reasoning`,
      `${answerId}:message`,
    ]);
    expect(response.output[1].content[0].text).toBe(answer);
  });

  it("restores stable ids for a combined reasoning and answer history row", async () => {
    const clientTurnId = "client-turn-combined-history";
    const sourceMessageId = "msg-combined-history";
    const answer = "这是稳定恢复后的完整答案。";
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "reload-generated-api-id",
          role: "assistant",
          type: "message",
          created_at: "2026-08-21 16:00:00.000",
          metadata: {
            original_id: sourceMessageId,
            metadata: { client_turn_id: clientTurnId },
          },
          content: [
            { type: "thinking", text: "先核对历史。" },
            { type: "text", text: answer },
          ],
        },
      ],
      status: "idle",
      chat_id: "chat-combined-history",
      session_id: "console:combined-history",
    });

    const session = (await sessionApi.getSession(
      "chat-combined-history",
    )) as any;
    const output = session.messages[0].cards[0].data.output;
    expect(output.map((item: any) => item.id)).toEqual([
      `${sourceMessageId}:reasoning`,
      `${sourceMessageId}:message`,
    ]);

    const replayed = buildNativeRuntimeStreamMessages(
      [
        {
          schema_version: 1,
          id: "evt-combined-replay-prefix",
          seq: 7,
          type: "message.delta",
          status: "running",
          source: "assistant",
          payload: {
            client_turn_id: clientTurnId,
            content_type: "text",
            stream_message_type: "message",
            delta: "这",
            message: {
              id: `${sourceMessageId}:message`,
              role: "assistant",
              metadata: { client_turn_id: clientTurnId },
              content: [{ type: "text", text: "这" }],
            },
          },
          usage: null,
          created_at: "2026-08-21T08:00:00Z",
          turn_id: null,
          session_id: "console:combined-history",
          item_id: `${sourceMessageId}:message`,
          presentation: null,
        },
      ],
      session.messages,
      { strictTurnIdentity: true },
    );

    expect(replayed).toHaveLength(1);
    expect((replayed[0].cards[0].data as any).output).toHaveLength(2);
    expect((replayed[0].cards[0].data as any).output[1].content[0].text).toBe(
      answer,
    );
  });

  it("restores persisted tool cards and generated images from chat history", async () => {
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-image",
          role: "user",
          type: "message",
          created_at: "2026-07-28T09:00:00Z",
          content: [{ type: "text", text: "画一张图" }],
        },
        {
          id: "tool-image-call",
          role: "assistant",
          type: "plugin_call",
          created_at: "2026-07-28T09:00:01Z",
          content: [
            {
              type: "data",
              data: {
                call_id: "call-image",
                name: "generate_image",
                arguments: "{}",
              },
            },
          ],
        },
        {
          id: "tool-image-media",
          role: "assistant",
          type: "message",
          created_at: "2026-07-28T09:00:03Z",
          content: [
            {
              type: "image",
              image_url: "/media/generated.png",
            },
          ],
        },
        {
          id: "tool-image-result",
          role: "assistant",
          type: "plugin_call_output",
          created_at: "2026-07-28T09:00:03Z",
          content: [
            {
              type: "data",
              data: {
                call_id: "call-image",
                name: "generate_image",
                output: '[{"type":"text","text":"已生成 1 张图片。"}]',
              },
            },
          ],
        },
        {
          id: "assistant-image-final",
          role: "assistant",
          type: "message",
          created_at: "2026-07-28T09:00:04Z",
          content: [{ type: "text", text: "图片已经生成。" }],
        },
      ],
      status: "idle",
      chat_id: "chat-image-history",
      session_id: "console:image-history",
    });

    const session = (await sessionApi.getSession("chat-image-history")) as any;
    const response = session.messages[1].cards.find(
      (card: any) => card.code === LANTAI_RESPONSE_CARD_CODE,
    ).data;

    expect(response.output.map((item: any) => item.type)).toEqual([
      LantaiRuntimeMessageType.PLUGIN_CALL,
      LantaiRuntimeMessageType.MESSAGE,
      LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT,
      LantaiRuntimeMessageType.MESSAGE,
    ]);
    expect(response.output[1].content[0]).toMatchObject({
      type: "image",
      image_url: "/media/generated.png",
    });
  });

  it("restores a delivered video player from send_file_to_user history", async () => {
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-video",
          role: "user",
          type: "message",
          created_at: "2026-09-13T01:47:00Z",
          content: [{ type: "text", text: "生成一个竖版短片" }],
        },
        {
          id: "tool-video-call",
          role: "assistant",
          type: "plugin_call",
          created_at: "2026-09-13T01:47:01Z",
          content: [
            {
              type: "data",
              data: {
                call_id: "call-video",
                name: "send_file_to_user",
                arguments: "{}",
              },
            },
          ],
        },
        {
          id: "tool-video-result",
          role: "assistant",
          type: "plugin_call_output",
          created_at: "2026-09-13T01:47:03Z",
          content: [
            {
              type: "data",
              data: {
                call_id: "call-video",
                name: "send_file_to_user",
                output: [
                  {
                    type: "video",
                    source: {
                      type: "url",
                      url: "file:///tmp/topic/generated-video.mp4",
                    },
                  },
                  { type: "text", text: "File sent successfully." },
                ],
              },
            },
          ],
        },
        {
          id: "assistant-video-final",
          role: "assistant",
          type: "message",
          created_at: "2026-09-13T01:47:04Z",
          content: [{ type: "text", text: "视频已经生成。" }],
        },
      ],
      status: "idle",
      chat_id: "chat-video-history",
      session_id: "console:video-history",
    });

    const session = (await sessionApi.getSession("chat-video-history")) as any;
    const response = session.messages[1].cards.find(
      (card: any) => card.code === LANTAI_RESPONSE_CARD_CODE,
    ).data;
    const restoredVideos = response.output.flatMap((item: any) =>
      item.type === LantaiRuntimeMessageType.MESSAGE
        ? item.content.filter((content: any) => content.type === "video")
        : [],
    );

    expect(restoredVideos).toEqual([
      expect.objectContaining({
        type: "video",
        video_url: "file:///tmp/topic/generated-video.mp4",
      }),
    ]);
  });

  it("does not duplicate a delivered video already restored as media", async () => {
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-video-existing",
          role: "user",
          type: "message",
          content: [{ type: "text", text: "生成视频" }],
        },
        {
          id: "tool-video-media",
          role: "assistant",
          type: "message",
          content: [
            {
              type: "video",
              video_url: "/tmp/topic/generated-video.mp4",
            },
          ],
        },
        {
          id: "tool-video-result-existing",
          role: "assistant",
          type: "plugin_call_output",
          content: [
            {
              type: "data",
              data: {
                call_id: "call-video-existing",
                name: "send_file_to_user",
                output: JSON.stringify([
                  {
                    type: "video",
                    source: {
                      type: "url",
                      url: "/tmp/topic/generated-video.mp4",
                    },
                  },
                ]),
              },
            },
          ],
        },
      ],
      status: "idle",
      chat_id: "chat-video-existing",
      session_id: "console:video-existing",
    });

    const session = (await sessionApi.getSession("chat-video-existing")) as any;
    const response = session.messages[1].cards.find(
      (card: any) => card.code === LANTAI_RESPONSE_CARD_CODE,
    ).data;
    const restoredVideos = response.output.flatMap((item: any) =>
      item.type === LantaiRuntimeMessageType.MESSAGE
        ? item.content.filter((content: any) => content.type === "video")
        : [],
    );

    expect(restoredVideos).toHaveLength(1);
    expect(restoredVideos[0].video_url).toBe("/tmp/topic/generated-video.mp4");
  });

  it("restores a delivered PPT file from send_file_to_user history", async () => {
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-ppt",
          role: "user",
          type: "message",
          content: [{ type: "text", text: "把这个做成PPT。" }],
        },
        {
          id: "tool-ppt-result",
          role: "assistant",
          type: "plugin_call_output",
          metadata: { client_turn_id: "user-ppt" },
          content: [
            {
              type: "data",
              data: {
                call_id: "call-ppt-delivery",
                name: "send_file_to_user",
                output: [
                  {
                    type: "file",
                    source: {
                      type: "url",
                      url: "file:///tmp/topic/金山办公媒体采访回复要点.pptx",
                    },
                    filename: "金山办公媒体采访回复要点.pptx",
                  },
                  { type: "text", text: "File sent successfully." },
                ],
              },
            },
          ],
        },
        {
          id: "assistant-ppt-final",
          role: "assistant",
          type: "message",
          metadata: {
            client_turn_id: "user-ppt",
            canonical_terminal_reply: true,
          },
          content: [{ type: "text", text: "做好了，文件已发给你。" }],
        },
      ],
      status: "idle",
      chat_id: "chat-ppt-history",
      session_id: "console:ppt-history",
    });

    const session = (await sessionApi.getSession("chat-ppt-history")) as any;
    const response = session.messages[1].cards.find(
      (card: any) => card.code === LANTAI_RESPONSE_CARD_CODE,
    ).data;
    const restoredFiles = response.output.flatMap((item: any) =>
      item.type === LantaiRuntimeMessageType.MESSAGE
        ? item.content.filter((content: any) => content.type === "file")
        : [],
    );

    expect(restoredFiles).toEqual([
      expect.objectContaining({
        type: "file",
        file_name: "金山办公媒体采访回复要点.pptx",
        file_source_url: "file:///tmp/topic/金山办公媒体采访回复要点.pptx",
        file_url: "file:///tmp/topic/金山办公媒体采访回复要点.pptx",
      }),
    ]);
    expect(response.metadata).toMatchObject({
      canonical_history_timeline: true,
      canonical_terminal_reply: true,
      client_turn_id: "user-ppt",
    });
  });

  it("restores and deduplicates a PPT when history only persisted delivery call arguments", async () => {
    const filePath =
      "/Users/example/Library/Application Support/lantai-console/working/presentation.pptx";
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-ppt-call-only",
          role: "user",
          type: "message",
          metadata: {
            metadata: { client_turn_id: "turn-ppt-call-only" },
          },
          content: [{ type: "text", text: "把这个做成PPT吧" }],
        },
        ...["first", "retry"].map((suffix) => ({
          id: `tool-ppt-call-only-${suffix}`,
          role: "assistant",
          type: "plugin_call",
          metadata: {
            metadata: { client_turn_id: "turn-ppt-call-only" },
          },
          content: [
            {
              type: "data",
              data: {
                call_id: `call-ppt-delivery-${suffix}`,
                name: "send_file_to_user",
                arguments: JSON.stringify({
                  file_path: filePath,
                  title: "WPS数字身份认证PPT",
                }),
              },
            },
          ],
        })),
        {
          id: "assistant-ppt-call-only-final",
          role: "assistant",
          type: "message",
          metadata: {
            metadata: {
              client_turn_id: "turn-ppt-call-only",
              canonical_terminal_reply: true,
            },
          },
          content: [
            { type: "text", text: "PPT 出来了，已经发给你，共 10 页。" },
          ],
        },
      ],
      status: "idle",
      chat_id: "chat-ppt-call-only",
      session_id: "console:ppt-call-only",
    });

    const session = (await sessionApi.getSession("chat-ppt-call-only")) as any;
    const response = session.messages[1].cards.find(
      (card: any) => card.code === LANTAI_RESPONSE_CARD_CODE,
    ).data;
    const restoredContent = response.output.flatMap(
      (item: any) => item.content || [],
    );

    expect(
      restoredContent.filter((content: any) => content.type === "file"),
    ).toEqual([
      expect.objectContaining({
        type: "file",
        file_name: "WPS数字身份认证PPT.pptx",
        file_source_url: `file://${filePath}`,
      }),
    ]);
    expect(restoredContent).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: "text",
          text: "PPT 出来了，已经发给你，共 10 页。",
        }),
      ]),
    );
    expect(response.metadata).toMatchObject({
      client_turn_id: "turn-ppt-call-only",
      canonical_terminal_reply: true,
    });
  });

  it("does not restore a file from a persisted delivery call that failed", async () => {
    const filePath = "/tmp/topic/missing-presentation.pptx";
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-ppt-failed-delivery",
          role: "user",
          type: "message",
          content: [{ type: "text", text: "把这个做成PPT吧" }],
        },
        {
          id: "tool-ppt-failed-call",
          role: "assistant",
          type: "plugin_call",
          content: [
            {
              type: "data",
              data: {
                call_id: "call-ppt-failed-delivery",
                name: "send_file_to_user",
                arguments: JSON.stringify({ file_path: filePath }),
              },
            },
          ],
        },
        {
          id: "tool-ppt-failed-output",
          role: "assistant",
          type: "plugin_call_output",
          status: "failed",
          content: [
            {
              type: "data",
              data: {
                call_id: "call-ppt-failed-delivery",
                name: "send_file_to_user",
                error: "file not found",
                output: [{ type: "text", text: "file not found" }],
              },
            },
          ],
        },
        {
          id: "assistant-ppt-failed-final",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "这次文件没有发出。" }],
        },
      ],
      status: "idle",
      chat_id: "chat-ppt-failed-delivery",
      session_id: "console:ppt-failed-delivery",
    });

    const session = (await sessionApi.getSession(
      "chat-ppt-failed-delivery",
    )) as any;
    const response = session.messages[1].cards.find(
      (card: any) => card.code === LANTAI_RESPONSE_CARD_CODE,
    ).data;
    expect(
      response.output.flatMap((item: any) => item.content || []),
    ).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: "file",
          file_source_url: `file://${filePath}`,
        }),
      ]),
    );
  });

  it("keeps the completed PPT delivery snapshot when returning from the local editor", async () => {
    const userCard = {
      id: "user-editor-return",
      role: "user",
      cards: [
        {
          code: LANTAI_REQUEST_CARD_CODE,
          data: {
            client_turn_id: "turn-editor-return",
            input: [
              {
                role: "user",
                type: "message",
                content: [{ type: "text", text: "把这个做成PPT吧" }],
              },
            ],
          },
        },
      ],
    };
    const completedDelivery = {
      id: "assistant-editor-return",
      role: "assistant",
      cards: [
        {
          code: LANTAI_RESPONSE_CARD_CODE,
          data: {
            status: "completed",
            metadata: {
              client_turn_id: "turn-editor-return",
              canonical_terminal_reply: true,
            },
            output: [
              {
                id: "final-editor-return",
                type: "message",
                role: "assistant",
                status: "completed",
                content: [
                  {
                    type: "text",
                    text: "PPT 已完成，10 页逐页校验通过。",
                  },
                  {
                    type: "file",
                    file_name: "WPS数字身份认证PPT.pptx",
                    file_source_url:
                      "file:///tmp/topic/WPS数字身份认证PPT.pptx",
                    file_url: "file:///tmp/topic/WPS数字身份认证PPT.pptx",
                  },
                ],
              },
            ],
          },
        },
      ],
    };
    mockListChats.mockResolvedValue([
      {
        id: "chat-editor-return",
        name: "WPS数字身份认证PPT",
        session_id: "console:editor-return",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await sessionApi.getSessionList();
    await sessionApi.updateSession({
      id: "chat-editor-return",
      messages: [userCard, completedDelivery] as any,
      __preserveMessages: true,
    } as any);
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "user-editor-return",
          role: "user",
          type: "message",
          metadata: { client_turn_id: "turn-editor-return" },
          content: [{ type: "text", text: "把这个做成PPT吧" }],
        },
        {
          id: "assistant-history-lagging",
          role: "assistant",
          type: "message",
          metadata: {
            client_turn_id: "turn-editor-return",
            canonical_terminal_reply: true,
          },
          content: [{ type: "text", text: "生成过程结束。" }],
        },
      ],
      status: "idle",
      chat_id: "chat-editor-return",
      session_id: "console:editor-return",
    });

    const restored = (await sessionApi.getSession("chat-editor-return")) as any;
    const assistant = restored.messages.find(
      (message: any) => message.role === "assistant",
    );
    const output = assistant.cards[0].data.output;
    const content = output.flatMap((item: any) => item.content || []);

    expect(content).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: "text",
          text: "PPT 已完成，10 页逐页校验通过。",
        }),
        expect.objectContaining({
          type: "file",
          file_name: "WPS数字身份认证PPT.pptx",
          file_source_url: "file:///tmp/topic/WPS数字身份认证PPT.pptx",
        }),
      ]),
    );
    expect(content).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ text: "生成过程结束。" }),
      ]),
    );
  });

  it("preserves only the matching completed turn when queued messages were answered later", async () => {
    const userCard = (turnId: string, text: string) => ({
      id: `user-${turnId}`,
      role: "user",
      metadata: { client_turn_id: turnId },
      cards: [
        {
          code: LANTAI_REQUEST_CARD_CODE,
          data: {
            client_turn_id: turnId,
            input: [
              {
                role: "user",
                type: "message",
                metadata: { client_turn_id: turnId },
                content: [{ type: "text", text }],
              },
            ],
          },
        },
      ],
    });
    const assistantCard = (
      turnId: string,
      text: string,
      fileName?: string,
    ) => ({
      id: `assistant-${turnId}`,
      role: "assistant",
      cards: [
        {
          code: LANTAI_RESPONSE_CARD_CODE,
          data: {
            status: "completed",
            metadata: {
              client_turn_id: turnId,
              canonical_terminal_reply: true,
            },
            output: [
              {
                id: `output-${turnId}`,
                type: "message",
                role: "assistant",
                status: "completed",
                metadata: { client_turn_id: turnId },
                content: [
                  { type: "text", text },
                  ...(fileName
                    ? [
                        {
                          type: "file",
                          file_name: fileName,
                          file_source_url: `file:///tmp/topic/${fileName}`,
                          file_url: `file:///tmp/topic/${fileName}`,
                        },
                      ]
                    : []),
                ],
              },
            ],
          },
        },
      ],
    });
    const queuedSnapshot = [
      userCard("turn-one", "先整理一下"),
      userCard("turn-two", "再补充数据"),
      userCard("turn-three", "做成 PPT 吧"),
      assistantCard("turn-one", "第一轮已完成。"),
      assistantCard("turn-two", "第二轮已完成。"),
      assistantCard(
        "turn-three",
        "PPT 已完成，10 页逐页校验通过。",
        "灵犀创作者计划老板汇报.pptx",
      ),
    ];

    mockListChats.mockResolvedValue([
      {
        id: "chat-queued-editor-return",
        name: "灵犀创作者计划老板汇报PPT",
        session_id: "console:queued-editor-return",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await sessionApi.getSessionList();
    await sessionApi.updateSession({
      id: "chat-queued-editor-return",
      messages: queuedSnapshot as any,
      __preserveMessages: true,
    } as any);
    mockGetChat.mockResolvedValue({
      messages: [
        {
          id: "history-user-one",
          role: "user",
          type: "message",
          metadata: { client_turn_id: "turn-one" },
          content: [{ type: "text", text: "先整理一下" }],
        },
        {
          id: "history-assistant-one",
          role: "assistant",
          type: "message",
          metadata: { client_turn_id: "turn-one" },
          content: [{ type: "text", text: "第一轮已完成。" }],
        },
        {
          id: "history-user-two",
          role: "user",
          type: "message",
          metadata: { client_turn_id: "turn-two" },
          content: [{ type: "text", text: "再补充数据" }],
        },
        {
          id: "history-assistant-two",
          role: "assistant",
          type: "message",
          metadata: { client_turn_id: "turn-two" },
          content: [{ type: "text", text: "第二轮已完成。" }],
        },
        {
          id: "history-user-three",
          role: "user",
          type: "message",
          metadata: { client_turn_id: "turn-three" },
          content: [{ type: "text", text: "做成 PPT 吧" }],
        },
        {
          id: "history-assistant-three-lagging",
          role: "assistant",
          type: "message",
          metadata: {
            client_turn_id: "turn-three",
            canonical_terminal_reply: true,
          },
          content: [{ type: "text", text: "生成过程结束。" }],
        },
      ],
      status: "idle",
      chat_id: "chat-queued-editor-return",
      session_id: "console:queued-editor-return",
    });

    const restored = (await sessionApi.getSession(
      "chat-queued-editor-return",
    )) as any;
    const users = restored.messages.filter(
      (message: any) => message.role === "user",
    );
    const assistants = restored.messages.filter(
      (message: any) => message.role === "assistant",
    );
    const assistantText = assistants.map((message: any) =>
      message.cards[0].data.output
        .flatMap((output: any) => output.content || [])
        .filter((content: any) => content.type === "text")
        .map((content: any) => content.text)
        .join(""),
    );
    const finalContent = assistants[2].cards[0].data.output.flatMap(
      (output: any) => output.content || [],
    );

    expect(users).toHaveLength(3);
    expect(assistants).toHaveLength(3);
    expect(assistantText).toEqual([
      "第一轮已完成。",
      "第二轮已完成。",
      "PPT 已完成，10 页逐页校验通过。",
    ]);
    expect(finalContent).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: "file",
          file_name: "灵犀创作者计划老板汇报.pptx",
        }),
      ]),
    );
    expect(assistantText).not.toContain("生成过程结束。");
  });

  it("keeps turn_event_preview from backend on the loaded session", async () => {
    mockGetChat.mockResolvedValue({
      messages: [],
      status: "running",
      chat_id: "chat-preview-1",
      session_id: "console:preview-1",
      turn_event_preview: {
        workspace_dir: "/tmp/topic",
        summary_line: "本轮已开始",
        events: [
          {
            id: "evt-1",
            ts: "2026-05-21T10:00:00Z",
            kind: "chat.turn.started",
            status: "running",
            label: "开始运行",
            detail: "文本：继续处理",
          },
        ],
      },
    });

    const session = (await sessionApi.getSession("chat-preview-1")) as any;

    expect(session.turnEventPreview?.summary_line).toBe("本轮已开始");
    expect(session.turnEventPreview?.events?.[0]?.label).toBe("开始运行");
  });

  it("does not emit session selection callbacks for passive history loads", async () => {
    const onSessionSelected = vi.fn();
    sessionApi.onSessionSelected = onSessionSelected;
    mockGetChat.mockResolvedValue({
      messages: [],
      status: "idle",
      chat_id: "chat-passive-load",
      session_id: "console:passive-load",
      name: "被动加载",
    });

    await sessionApi.getSession("chat-passive-load");

    expect(onSessionSelected).not.toHaveBeenCalled();
    expect((window as any).currentSessionId).toBeUndefined();
    expect((window as any).currentChannel).toBeUndefined();
  });

  it("does not let a delayed channel history response replace a new Console selection", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-24T17:30:00Z"));
    try {
      let resolveOldHistory!: (value: any) => void;
      mockGetChat.mockReturnValue(
        new Promise((resolve) => {
          resolveOldHistory = resolve;
        }),
      );
      mockEnsureTopicWorkspace.mockReturnValue(new Promise(() => {}));

      sessionApi.selectSession("wechat-chat-old");
      const oldRequest = sessionApi.getSession("wechat-chat-old");

      const sessions = await sessionApi.createSession({
        name: "New Chat",
        messages: [],
      });
      const newConsoleId = sessions[0].id;
      expect((window as any).currentSessionId).toBe(newConsoleId);
      expect((window as any).currentChannel).toBe("console");

      resolveOldHistory({
        messages: [{ role: "assistant", content: "旧微信会话内容" }],
        status: "idle",
        chat_id: "wechat-chat-old",
        session_id: "wechat:friend-old",
        user_id: "friend-old",
        channel: "wechat",
        meta: { source: "wechat" },
      });
      await oldRequest;

      expect((window as any).currentSessionId).toBe(newConsoleId);
      expect((window as any).currentUserId).toBe("default");
      expect((window as any).currentChannel).toBe("console");
    } finally {
      vi.useRealTimers();
    }
  });

  it("emits session selection callbacks only for explicit user selection", async () => {
    const onSessionSelected = vi.fn();
    sessionApi.onSessionSelected = onSessionSelected;
    mockListChats.mockResolvedValue([
      {
        id: "chat-real-selected",
        name: "显式选择",
        session_id: "console:selected",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await sessionApi.getSessionList();

    sessionApi.selectSession("chat-real-selected");
    sessionApi.selectSession("chat-real-selected");

    expect(onSessionSelected).toHaveBeenCalledTimes(1);
    expect(onSessionSelected).toHaveBeenCalledWith("chat-real-selected", null);
  });

  it("resolves explicit selection through realId and backend sessionId aliases", () => {
    const onSessionSelected = vi.fn();
    sessionApi.onSessionSelected = onSessionSelected;
    Object.assign(sessionApi as any, {
      sessionList: [
        {
          id: "local-temp-session",
          realId: "chat-real-alias",
          sessionId: "console:alias",
          name: "别名会话",
          userId: "default",
          channel: "console",
          messages: [],
          meta: {},
        },
      ],
    });

    sessionApi.selectSession("console:alias");
    sessionApi.selectSession("chat-real-alias");

    expect(onSessionSelected).toHaveBeenCalledTimes(1);
    expect(onSessionSelected).toHaveBeenCalledWith(
      "local-temp-session",
      "chat-real-alias",
    );
  });

  it("does not replace a backend chat UUID with its console session alias after ensureTopicWorkspace", () => {
    Object.assign(sessionApi as any, {
      sessionList: [
        {
          id: "chat-real-existing",
          realId: "chat-real-existing",
          sessionId: "console:existing",
          name: "已打开会话",
          userId: "default",
          channel: "console",
          messages: [
            {
              id: "optimistic-user",
              role: "user",
              cards: [],
            },
            {
              id: "optimistic-assistant",
              role: "assistant",
              cards: [],
            },
          ],
          meta: {},
        },
      ],
    });

    sessionApi.resolveEnsuredSession(
      ["console:existing", "chat-real-existing"],
      "chat-real-existing",
      "console:existing",
    );

    const session = (sessionApi as any).sessionList[0];
    expect(session.id).toBe("chat-real-existing");
    expect(session.realId).toBe("chat-real-existing");
    expect(session.sessionId).toBe("console:existing");
  });

  it("moves the preferred chat to the front when preferredChatId is a realId alias", async () => {
    Object.assign(sessionApi as any, {
      sessionList: [
        {
          id: "local-temp-b",
          realId: "chat-real-b",
          sessionId: "console:b",
          name: "B",
          userId: "default",
          channel: "console",
          messages: [],
          meta: {},
        },
      ],
    });
    sessionApi.preferredChatId = "chat-real-b";
    mockListChats.mockResolvedValue([
      {
        id: "chat-real-b",
        name: "B",
        session_id: "console:b",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
      {
        id: "chat-real-a",
        name: "A",
        session_id: "console:a",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);

    const list = (await sessionApi.getSessionList()) as any[];

    expect(list[0].id).toBe("local-temp-b");
    expect(list[0].realId).toBe("chat-real-b");
  });

  it("preserves optimistic user cards when the session list refreshes", async () => {
    const userCard = {
      id: "optimistic-user-card",
      role: "user",
      cards: [
        {
          code: LANTAI_REQUEST_CARD_CODE,
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [{ type: "text", text: "需要一直可见" }],
              },
            ],
          },
        },
      ],
    };
    const assistantCard = {
      id: "optimistic-assistant-card",
      role: "assistant",
      cards: [],
    };
    const chatSpec = {
      id: "preserve-chat",
      name: "New Chat",
      session_id: "preserve-session",
      user_id: "default",
      channel: "console",
      meta: {},
      status: "idle",
    };

    mockListChats.mockResolvedValue([chatSpec]);
    await sessionApi.getSessionList();

    let sessions = await sessionApi.updateSession({
      id: "preserve-chat",
      messages: [userCard, assistantCard] as any,
    });
    expect(
      (sessions.find((s) => s.id === "preserve-chat") as any)?.messages,
    ).toEqual([userCard]);

    mockListChats.mockResolvedValue([chatSpec]);
    sessions = await sessionApi.getSessionList();
    expect(
      (sessions.find((s) => s.id === "preserve-chat") as any)?.messages,
    ).toEqual([userCard]);
  });

  it("does not copy optimistic messages between chats that share a session alias", async () => {
    const snakeCard = {
      id: "snake-user-card",
      role: "user",
      cards: [
        {
          code: LANTAI_REQUEST_CARD_CODE,
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [{ type: "text", text: "做一个贪吃蛇小游戏" }],
              },
            ],
          },
        },
      ],
    };
    const chats = [
      {
        id: "snake-chat",
        name: "做一个贪吃蛇小游戏",
        session_id: "console:shared-workspace",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "running",
      },
      {
        id: "token-chat",
        name: "这两个图，哪个30天消耗量更大",
        session_id: "console:shared-workspace",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ];

    Object.assign(sessionApi as any, {
      sessionList: [
        {
          id: "snake-chat",
          realId: "snake-chat",
          sessionId: "console:shared-workspace",
          messages: [snakeCard],
          status: "running",
        },
        {
          id: "token-chat",
          realId: "token-chat",
          sessionId: "console:shared-workspace",
          messages: [],
          status: "idle",
        },
      ],
    });
    mockListChats.mockResolvedValue(chats);

    const sessions = (await sessionApi.getSessionList({
      force: true,
    })) as any[];

    expect(sessions.find((item) => item.id === "snake-chat")?.messages).toEqual(
      [snakeCard],
    );
    expect(
      sessions.find((item) => item.id === "token-chat")?.messages || [],
    ).toEqual([]);
  });

  it("does not copy a whole attachment-bearing user card when only another independent chat remains in the list", async () => {
    const chatAUserCard = {
      id: "chat-a-user-card",
      role: "user",
      createdAt: "2026-09-18T21:09:00+08:00",
      cards: [
        {
          code: LANTAI_REQUEST_CARD_CODE,
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  { type: "text", text: "把这个做成PPT。" },
                  {
                    type: "file",
                    file_url: "/files/ONLY_CHAT_A_ATTACHMENT.pdf",
                    file_name: "ONLY_CHAT_A_ATTACHMENT.pdf",
                  },
                ],
              },
            ],
          },
        },
      ],
    };
    Object.assign(sessionApi as any, {
      sessionList: [
        {
          id: "chat-a",
          name: "把这个做成PPT",
          realId: "chat-a",
          sessionId: "console:reused-transport",
          messages: [chatAUserCard],
          status: "running",
        },
      ],
    });
    mockListChats.mockResolvedValue([
      {
        id: "chat-b",
        name: "把这个做成PPT",
        session_id: "console:reused-transport",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);

    const sessions = (await sessionApi.getSessionList({
      force: true,
    })) as any[];
    expect(sessions).toEqual([
      expect.objectContaining({
        id: "chat-b",
        name: "把这个做成PPT",
        messages: [],
      }),
    ]);
    expect(JSON.stringify(sessions)).not.toContain(
      "ONLY_CHAT_A_ATTACHMENT.pdf",
    );
    expect(JSON.stringify(sessions)).not.toContain("2026-09-18T21:09:00+08:00");

    mockGetChat.mockResolvedValue({
      chat_id: "chat-b",
      session_id: "console:reused-transport",
      name: "把这个做成PPT",
      status: "idle",
      created_at: "2026-09-19T21:09:00+08:00",
      messages: [
        {
          id: "chat-b-user-card",
          role: "user",
          type: "message",
          created_at: "2026-09-19T21:09:00+08:00",
          content: [
            { type: "text", text: "把这个做成PPT。" },
            {
              type: "file",
              file_url: "/files/ONLY_CHAT_B_ATTACHMENT.pdf",
              filename: "ONLY_CHAT_B_ATTACHMENT.pdf",
            },
          ],
        },
      ],
    });

    const chatB = await sessionApi.getSession("chat-b");
    const serializedChatB = JSON.stringify(chatB);
    expect(serializedChatB).toContain("ONLY_CHAT_B_ATTACHMENT.pdf");
    expect(serializedChatB).toContain("2026-09-19T21:09:00+08:00");
    expect(serializedChatB).not.toContain("ONLY_CHAT_A_ATTACHMENT.pdf");
    expect(serializedChatB).not.toContain("2026-09-18T21:09:00+08:00");
  });

  it("preserves assistant cards for native runtime stream updates when requested", async () => {
    const userCard = { id: "user-card", role: "user", cards: [] };
    const assistantCard = {
      id: "native-runtime-card",
      role: "assistant",
      cards: [
        {
          code: LANTAI_RESPONSE_CARD_CODE,
          data: { output: [] },
        },
      ],
    };
    mockListChats.mockResolvedValue([
      {
        id: "native-chat",
        name: "New Chat",
        session_id: "native-session",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await sessionApi.getSessionList();

    const sessions = await sessionApi.updateSession({
      id: "native-chat",
      messages: [userCard, assistantCard] as any,
      __preserveMessages: true,
    } as any);

    expect(
      (sessions.find((s) => s.id === "native-chat") as any)?.messages,
    ).toEqual([userCard, assistantCard]);
  });

  it("does not erase a just-submitted user card when a stale runtime patch arrives", async () => {
    const firstUser = { id: "user-1", role: "user", cards: [] };
    const firstAssistant = {
      id: "assistant-1",
      role: "assistant",
      cards: [{ code: LANTAI_RESPONSE_CARD_CODE, data: { output: [] } }],
    };
    const secondUser = { id: "user-2", role: "user", cards: [] };
    const secondAssistant = {
      id: "assistant-2",
      role: "assistant",
      cards: [{ code: LANTAI_RESPONSE_CARD_CODE, data: { output: [] } }],
    };
    mockListChats.mockResolvedValue([
      {
        id: "runtime-race-chat",
        name: "New Chat",
        session_id: "runtime-race-session",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "running",
      },
    ]);
    await sessionApi.getSessionList();
    await sessionApi.updateSession({
      id: "runtime-race-chat",
      messages: [firstUser, firstAssistant, secondUser] as any,
      __preserveMessages: true,
    } as any);

    const sessions = await sessionApi.updateSession({
      id: "runtime-race-chat",
      messages: [firstUser, firstAssistant, secondAssistant] as any,
      __preserveMessages: true,
    } as any);

    expect(
      (sessions.find((s) => s.id === "runtime-race-chat") as any)?.messages,
    ).toEqual([firstUser, firstAssistant, secondUser, secondAssistant]);
  });

  it("does not concatenate stale assistant cards with a current runtime snapshot", async () => {
    const userCard = { id: "user-card", role: "user", cards: [] };
    const staleAssistant = {
      id: "stale-assistant-card",
      role: "assistant",
      cards: [{ code: LANTAI_RESPONSE_CARD_CODE, data: { output: [] } }],
    };
    const currentAssistant = {
      id: "current-assistant-card",
      role: "assistant",
      cards: [{ code: LANTAI_RESPONSE_CARD_CODE, data: { output: [] } }],
    };
    mockListChats.mockResolvedValue([
      {
        id: "runtime-replacement-chat",
        name: "New Chat",
        session_id: "runtime-replacement-session",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await sessionApi.getSessionList();
    await sessionApi.updateSession({
      id: "runtime-replacement-chat",
      messages: [userCard, staleAssistant] as any,
      __preserveMessages: true,
    } as any);

    const sessions = await sessionApi.updateSession({
      id: "runtime-replacement-chat",
      messages: [userCard, currentAssistant] as any,
      __preserveMessages: true,
    } as any);

    expect(
      (sessions.find((s) => s.id === "runtime-replacement-chat") as any)
        ?.messages,
    ).toEqual([userCard, currentAssistant]);
  });

  it("reconciles a persisted user card whose backend id differs from its optimistic id", async () => {
    const requestCard = (id: string, text: string) => ({
      id,
      role: "user",
      // Real optimistic submissions and durable history carry this same key.
      // Different message ids alone cannot prove they are one submission.
      metadata: { client_turn_id: "runtime-id-reconciliation-turn" },
      cards: [
        {
          code: LANTAI_REQUEST_CARD_CODE,
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [{ type: "text", text, status: "created" }],
              },
            ],
          },
        },
      ],
    });
    const optimisticUser = requestCard("optimistic-user", "rapid queue first");
    const persistedUser = requestCard("backend-user", "rapid queue first");
    mockListChats.mockResolvedValue([
      {
        id: "runtime-id-reconciliation-chat",
        name: "New Chat",
        session_id: "runtime-id-reconciliation-session",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "running",
      },
    ]);
    await sessionApi.getSessionList();
    await sessionApi.updateSession({
      id: "runtime-id-reconciliation-chat",
      messages: [optimisticUser] as any,
      __preserveMessages: true,
    } as any);

    const sessions = await sessionApi.updateSession({
      id: "runtime-id-reconciliation-chat",
      messages: [persistedUser] as any,
      __preserveMessages: true,
    } as any);

    expect(
      (sessions.find((s) => s.id === "runtime-id-reconciliation-chat") as any)
        ?.messages,
    ).toEqual([persistedUser]);
  });

  it.each([
    ["text", { type: "text", text: "repeat submission" }],
    ["image", { type: "image", image_url: "file:///tmp/shared.png" }],
    ["file", { type: "file", file_url: "file:///tmp/report.docx" }],
    ["audio", { type: "audio", audio_url: "file:///tmp/shared.wav" }],
    ["video", { type: "video", video_url: "file:///tmp/shared.mp4" }],
  ])(
    "retains separate submissions with identical %s content",
    async (_, content) => {
      const userCard = (id: string, turnId: string) => ({
        id,
        role: "user",
        metadata: { client_turn_id: turnId },
        cards: [
          {
            code: LANTAI_REQUEST_CARD_CODE,
            data: {
              input: [
                {
                  role: "user",
                  type: "message",
                  content: [content],
                },
              ],
            },
          },
        ],
      });
      const first = userCard("message-a", "turn-a");
      const second = userCard("message-b", "turn-b");
      mockListChats.mockResolvedValue([
        {
          id: "repeated-submission-chat",
          name: "Chat",
          session_id: "repeated-submission-session",
          user_id: "default",
          channel: "console",
          meta: {},
          status: "running",
        },
      ]);
      await sessionApi.getSessionList();
      await sessionApi.updateSession({
        id: "repeated-submission-chat",
        messages: [first] as any,
        __preserveMessages: true,
      } as any);
      const sessions = await sessionApi.updateSession({
        id: "repeated-submission-chat",
        messages: [second] as any,
        __preserveMessages: true,
      } as any);
      expect(
        (sessions.find((s) => s.id === "repeated-submission-chat") as any)
          .messages,
      ).toEqual([first, second]);
      const replay = await sessionApi.updateSession({
        id: "repeated-submission-chat",
        messages: [first, second] as any,
        __preserveMessages: true,
      } as any);
      expect(
        (replay.find((s) => s.id === "repeated-submission-chat") as any)
          .messages,
      ).toEqual([first, second]);
    },
  );

  const seedIdentityChat = async (id: string, sessionId: string) => {
    mockListChats.mockResolvedValue([
      {
        id,
        name: "Chat",
        session_id: sessionId,
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await sessionApi.getSessionList();
  };

  for (const field of ["command_id", "client_turn_id"] as const) {
    for (const nested of [false, true]) {
      it.each([
        ["text", { type: "text", text: "repeat durable submission" }],
        ["image", { type: "image", image_url: "file:///synthetic/shared.png" }],
        ["file", { type: "file", file_url: "file:///synthetic/shared.docx" }],
      ])(
        `hydrates independent ${field} ${nested ? "nested" : "flat"} %s history and replays it once without message IDs`,
        async (_, content) => {
          await seedIdentityChat(
            "independent-identity-chat",
            "independent-identity-session",
          );
          const history = (identity: string) => ({
            chat_id: "independent-identity-chat",
            name: "Chat",
            session_id: "independent-identity-session",
            user_id: "default",
            channel: "console",
            meta: {},
            status: "idle",
            messages: [
              {
                role: "user",
                type: "message",
                metadata: nested
                  ? { metadata: { [field]: identity } }
                  : { [field]: identity },
                content: [content],
              },
            ],
          });
          mockGetChat.mockResolvedValue(history("identity-A"));
          const first = await sessionApi.getSession(
            "independent-identity-chat",
          );
          expect(first.messages![0].metadata?.[field]).toBe("identity-A");
          await sessionApi.updateSession({
            ...first,
            __preserveMessages: true,
          } as any);
          const replay = await sessionApi.getSession(
            "independent-identity-chat",
          );
          expect(
            replay.messages!.filter((message) => message.role === "user"),
          ).toHaveLength(1);

          mockGetChat.mockResolvedValue(history("identity-B"));
          const next = await sessionApi.getSession("independent-identity-chat");
          expect(
            next
              .messages!.filter((message) => message.role === "user")
              .map((message) => message.metadata?.[field]),
          ).toEqual(["identity-A", "identity-B"]);
          await sessionApi.updateSession({
            ...next,
            __preserveMessages: true,
          } as any);
          const nextReplay = await sessionApi.getSession(
            "independent-identity-chat",
          );
          expect(
            nextReplay.messages!.filter((message) => message.role === "user"),
          ).toHaveLength(2);
        },
      );

      it(`retains distinct ${field} ${nested ? "nested" : "flat"} submissions when persisted original IDs are reused`, async () => {
        await seedIdentityChat(
          "reused-original-identity-chat",
          "reused-original-identity-session",
        );
        const history = (identity: string) => ({
          chat_id: "reused-original-identity-chat",
          name: "Chat",
          session_id: "reused-original-identity-session",
          user_id: "default",
          channel: "console",
          meta: {},
          status: "idle",
          messages: [
            {
              role: "user",
              type: "message",
              metadata: nested
                ? { original_id: "same-row", metadata: { [field]: identity } }
                : { original_id: "same-row", [field]: identity },
              content: [{ type: "text", text: "same durable request" }],
            },
          ],
        });
        mockGetChat.mockResolvedValue(history("identity-A"));
        const first = await sessionApi.getSession(
          "reused-original-identity-chat",
        );
        await sessionApi.updateSession({
          ...first,
          __preserveMessages: true,
        } as any);
        mockGetChat.mockResolvedValue(history("identity-B"));
        const next = await sessionApi.getSession(
          "reused-original-identity-chat",
        );
        expect(
          next
            .messages!.filter((message) => message.role === "user")
            .map((message) => message.metadata?.[field]),
        ).toEqual(["identity-A", "identity-B"]);
      });
    }
  }

  it("retains distinct explicit turns with a reused row id through getSession", async () => {
    mockListChats.mockResolvedValue([
      {
        id: "reused-row-chat",
        name: "Chat",
        session_id: "reused-row-session",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await sessionApi.getSessionList();
    await sessionApi.updateSession({
      id: "reused-row-chat",
      __preserveMessages: true,
      messages: [
        {
          id: "reused-row",
          role: "user",
          metadata: { client_turn_id: "turn-A" },
          cards: [
            {
              code: LANTAI_REQUEST_CARD_CODE,
              data: {
                input: [
                  {
                    role: "user",
                    type: "message",
                    content: [{ type: "text", text: "same request" }],
                  },
                ],
              },
            },
          ],
        },
      ],
    } as any);
    mockGetChat.mockResolvedValue({
      chat_id: "reused-row-chat",
      name: "Chat",
      session_id: "reused-row-session",
      user_id: "default",
      channel: "console",
      meta: {},
      status: "idle",
      messages: [
        {
          id: "reused-row",
          role: "user",
          type: "message",
          metadata: { client_turn_id: "turn-B" },
          content: [{ type: "text", text: "same request" }],
        },
      ],
    });

    const hydrated = await sessionApi.getSession("reused-row-chat");

    expect(
      hydrated
        .messages!.filter((message) => message.role === "user")
        .map((message) => message.metadata?.client_turn_id),
    ).toEqual(["turn-A", "turn-B"]);
  });

  it("uses a top-level client turn before a reused row id during preserve updates", async () => {
    const card = (turnId: string) => ({
      id: "reused-row",
      role: "user",
      client_turn_id: turnId,
      cards: [
        {
          code: LANTAI_REQUEST_CARD_CODE,
          data: {
            input: [
              {
                role: "user",
                content: [{ type: "text", text: "same request" }],
              },
            ],
          },
        },
      ],
    });
    mockListChats.mockResolvedValue([
      {
        id: "top-level-turn-chat",
        name: "Chat",
        session_id: "top-level-turn-session",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await sessionApi.getSessionList();
    await sessionApi.updateSession({
      id: "top-level-turn-chat",
      messages: [card("turn-A")],
      __preserveMessages: true,
    } as any);
    const sessions = await sessionApi.updateSession({
      id: "top-level-turn-chat",
      messages: [card("turn-B")],
      __preserveMessages: true,
    } as any);

    expect(
      (
        sessions.find((session) => session.id === "top-level-turn-chat")!
          .messages as any[]
      ).map((message) => message.client_turn_id),
    ).toEqual(["turn-A", "turn-B"]);
  });

  it("merges preserved runtime messages after discovering a route backend chat", async () => {
    const userCard = { id: "user-card", role: "user", cards: [] };
    const assistantCard = {
      id: "native-runtime-card",
      role: "assistant",
      cards: [
        {
          code: LANTAI_RESPONSE_CARD_CODE,
          data: {
            output: [
              {
                id: "run:message",
                content: [{ type: "text", text: "救援回答" }],
              },
            ],
          },
        },
      ],
    };
    mockListChats.mockResolvedValue([
      {
        id: "route-backend-chat",
        name: "New Chat",
        session_id: "console:route-backend-chat",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);

    const sessions = await sessionApi.updateSession({
      id: "route-backend-chat",
      messages: [userCard, assistantCard] as any,
      __preserveMessages: true,
    } as any);

    expect(mockListChats).toHaveBeenCalledTimes(1);
    expect(
      (sessions.find((s) => s.id === "route-backend-chat") as any)?.messages,
    ).toEqual([userCard, assistantCard]);
  });

  it("does not discard complete history during a metadata-only runtime update", async () => {
    const userCard = { id: "user-card", role: "user", cards: [] };
    const assistantCard = {
      id: "assistant-card",
      role: "assistant",
      cards: [{ code: LANTAI_RESPONSE_CARD_CODE, data: { output: [] } }],
    };
    mockListChats.mockResolvedValue([
      {
        id: "metadata-chat",
        name: "New Chat",
        session_id: "metadata-session",
        user_id: "default",
        channel: "console",
        meta: {},
        status: "idle",
      },
    ]);
    await sessionApi.getSessionList();
    await sessionApi.updateSession({
      id: "metadata-chat",
      messages: [userCard, assistantCard] as any,
      __preserveMessages: true,
    } as any);

    const sessions = await sessionApi.updateSession({
      id: "metadata-chat",
      generating: true,
      status: "running",
    } as any);

    expect(
      (sessions.find((s) => s.id === "metadata-chat") as any)?.messages,
    ).toEqual([userCard, assistantCard]);
  });

  it("keeps complete active history across a session-list refresh", async () => {
    const userCard = { id: "user-card", role: "user", cards: [] };
    const assistantCard = {
      id: "assistant-card",
      role: "assistant",
      cards: [{ code: LANTAI_RESPONSE_CARD_CODE, data: { output: [] } }],
    };
    const chatSpec = {
      id: "refresh-chat",
      name: "New Chat",
      session_id: "refresh-session",
      user_id: "default",
      channel: "console",
      meta: {},
      status: "idle",
    };
    mockListChats.mockResolvedValue([chatSpec]);
    await sessionApi.getSessionList();
    await sessionApi.updateSession({
      id: "refresh-chat",
      messages: [userCard, assistantCard] as any,
      __preserveMessages: true,
    } as any);

    const sessions = await sessionApi.getSessionList();

    expect(
      (sessions.find((s) => s.id === "refresh-chat") as any)?.messages,
    ).toEqual([userCard, assistantCard]);
  });

  it("clears stale local running state when the backend reports a terminal session", async () => {
    mockListChats
      .mockResolvedValueOnce([
        {
          id: "terminal-chat",
          name: "New Chat",
          session_id: "terminal-session",
          user_id: "default",
          channel: "console",
          meta: {},
          status: "running",
        },
      ])
      .mockResolvedValueOnce([
        {
          id: "terminal-chat",
          name: "New Chat",
          session_id: "terminal-session",
          user_id: "default",
          channel: "console",
          meta: {},
          status: "idle",
        },
      ]);

    await sessionApi.getSessionList({ force: true });
    await sessionApi.updateSession({
      id: "terminal-chat",
      status: "running",
      generating: true,
      pendingUserMessage: "still running",
      runtimeState: { status: "running" },
      turnEventPreview: { status: "running" },
    } as any);

    const sessions = await sessionApi.getSessionList({ force: true });
    const terminal = sessions.find((session) => session.id === "terminal-chat");

    expect(terminal?.status).toBe("idle");
    expect(terminal?.generating).toBe(false);
    expect(terminal?.pendingUserMessage).toBeUndefined();
    expect(terminal?.runtimeState).toBeUndefined();
    expect(terminal?.turnEventPreview).toBeUndefined();
  });
});
