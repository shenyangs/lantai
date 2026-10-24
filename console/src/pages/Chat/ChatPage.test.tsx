/**
 * Chat/index.tsx behavior tests
 *
 * Strategy (following the openclaw chat.test.ts pattern):
 * - Mock LantaiRuntimeWebUI as a spy component that captures the options prop
 * - Directly invoke callbacks like options.api.fetch and
 *   options.sender.attachments.customRequest to test ChatPage logic
 *   without depending on a real WebSocket runtime
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  act,
  fireEvent,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation, useNavigate } from "react-router-dom";
import { renderWithProviders } from "@/test/common_setup";
import ChatPage, {
  DEFAULT_EMPTY_PROMPT_SUGGESTIONS,
  normalizeEmptyPromptSuggestions,
} from "./index";
import { permissionPresetStorageKey } from "./permissionMode";
import sessionApi from "./sessionApi";
import { DEFAULT_WORK_METHODS } from "./workMethod";
import {
  consumeModelSetupDraft,
  persistModelSetupDraft,
  persistModelSetupDraftSnapshot,
} from "@/utils/modelSetupFlow";
import type { KdocsDocumentItem } from "@/api/types/kdocs";
import {
  documentToKdocsReference,
  loadKdocsReferences,
} from "./kdocsReferences";
import {
  artifactWorkbenchStorageKey,
  loadArtifactWorkbenchState,
  saveArtifactWorkbenchState,
} from "./components/artifactWorkbenchState";

// ---------------------------------------------------------------------------
// Capture LantaiRuntimeWebUI options
// ---------------------------------------------------------------------------
let capturedOptions: any = null;
let capturedDroppedFiles: File[] = [];
let artifactEditSelectionPage = 3;

const {
  mockListProviders,
  mockGetActiveModels,
  mockGetReasoningCapability,
  mockListPermissionProfiles,
  mockUploadFile,
  mockFilePreviewUrl,
  mockGetApiUrl,
  mockGetChat,
  mockGetChatEvents,
  mockDraftCapabilityFromTask,
  mockStreamCollaborationDraft,
  mockListTaskRuns,
  mockGetToday,
  mockGetDailyBriefHome,
  mockGetBriefDraft,
  mockSelectedAgent,
  mockSetSelectedAgent,
  mockSetLastChatId,
  mockGetLastChatId,
  mockStopChat,
  mockInterruptRun,
  mockEnsureTopicWorkspace,
  mockApprovals,
  mockSetApprovals,
  mockSendApprovalCommand,
  mockPreviewSessionPermission,
  mockUpdateSessionPermission,
  mockModalConfirm,
  mockSaveQueryToWiki,
  mockGetPlanConfig,
  mockUpdatePlanConfig,
  mockGetAgentRunningConfig,
  mockReadKdocsDocumentSafely,
  mockRuntimeSetLoading,
  mockRuntimeGetLoading,
  mockListMacUseWindows,
  mockWelcomeGreetingInput,
} = vi.hoisted(() => ({
  mockListProviders: vi.fn(),
  mockGetActiveModels: vi.fn(),
  mockGetReasoningCapability: vi.fn(),
  mockListPermissionProfiles: vi.fn(),
  mockUploadFile: vi.fn(),
  mockFilePreviewUrl: vi.fn((f: string) => `/preview/${f}`),
  mockGetApiUrl: vi.fn((p: string) => `/api${p}`),
  mockGetChat: vi.fn(),
  mockGetChatEvents: vi.fn(),
  mockDraftCapabilityFromTask: vi.fn(),
  mockStreamCollaborationDraft: vi.fn(),
  mockListTaskRuns: vi.fn(),
  mockGetToday: vi.fn(),
  mockGetDailyBriefHome: vi.fn(),
  mockGetBriefDraft: vi.fn(),
  mockSelectedAgent: vi.fn(() => "default"),
  mockSetSelectedAgent: vi.fn(),
  mockSetLastChatId: vi.fn(),
  mockGetLastChatId: vi.fn(),
  mockStopChat: vi.fn(),
  mockInterruptRun: vi.fn(),
  mockEnsureTopicWorkspace: vi.fn(),
  mockApprovals: [] as any[],
  mockSetApprovals: vi.fn(),
  mockSendApprovalCommand: vi.fn(),
  mockPreviewSessionPermission: vi.fn(),
  mockUpdateSessionPermission: vi.fn(),
  mockModalConfirm: vi.fn(),
  mockSaveQueryToWiki: vi.fn(),
  mockGetPlanConfig: vi.fn(),
  mockUpdatePlanConfig: vi.fn(),
  mockGetAgentRunningConfig: vi.fn(),
  mockReadKdocsDocumentSafely: vi.fn(),
  mockRuntimeSetLoading: vi.fn(),
  mockRuntimeGetLoading: vi.fn(() => true),
  mockListMacUseWindows: vi.fn(),
  mockWelcomeGreetingInput: { current: null as any },
}));

vi.mock("../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: {
      success: vi.fn(),
      error: vi.fn(),
      warning: vi.fn(),
      info: vi.fn(),
    },
    modal: {
      confirm: mockModalConfirm,
    },
  }),
}));

vi.mock("../../contexts/ApprovalContext", () => ({
  useApprovalContext: () => ({
    approvals: mockApprovals,
    setApprovals: mockSetApprovals,
  }),
}));

vi.mock("../../plugins/PluginContext", () => ({
  usePlugins: () => ({
    plugins: [],
    registerPlugin: vi.fn(),
    toolRenderConfig: {},
  }),
  PluginContext: { Provider: ({ children }: any) => children },
}));

vi.mock("./components/ChatSessionInitializer", () => ({
  default: () => null,
}));

vi.mock("./components/ChatArtifactPanel", () => ({
  default: (props: any) =>
    props.open ? (
      <aside data-testid="chat-artifact-panel">
        <button type="button" aria-label="收起产物面板" onClick={props.onClose}>
          close
        </button>
        <button
          type="button"
          aria-label="准备第 3 页修改"
          onClick={() =>
            props.onPrepareArtifactEdit({
              title: "季度复盘",
              target: {
                schema_version: 1,
                chat_id: props.chatId || "",
                asset_id: "asset-deck",
                work_id: "work-deck-v1",
                edit_path: "/tmp/lantai/topic/季度复盘.pptx",
                base_revision: {
                  version: 1,
                  digest: "sha256:deck-v1",
                },
                selection: { kind: "page", page: artifactEditSelectionPage },
              },
            })
          }
        >
          prepare
        </button>
      </aside>
    ) : null,
}));

vi.mock("@/vendor/lantaiChat", () => ({
  Bubble: {
    Spin: () => <div data-testid="bubble-spin" />,
  },
  DefaultCards: {
    Audios: ({ data }: any) => (
      <div data-testid="audio-card">{JSON.stringify(data)}</div>
    ),
    Files: ({ data }: any) => (
      <div data-testid="file-card">{JSON.stringify(data)}</div>
    ),
    Images: ({ data }: any) => (
      <div data-testid="image-card">{JSON.stringify(data)}</div>
    ),
    Videos: ({ data }: any) => (
      <div data-testid="video-card">{JSON.stringify(data)}</div>
    ),
  },
  Markdown: ({ content }: any) => <div>{content}</div>,
  // render rightHeader so child components appear in the DOM
  LantaiRuntimeWebUI: vi.fn((props: any) => {
    capturedOptions = props.options;
    return (
      <div data-testid="chat-ui">
        {props.options?.theme?.rightHeader}
        <div className="lantai-chat-anywhere-input">
          {props.options?.sender?.context}
          {props.options?.sender?.prefix}
          <textarea aria-label="chat input" />
          <input
            data-testid="drop-file-input"
            type="file"
            multiple
            onChange={(event) => {
              capturedDroppedFiles = Array.from(
                (event.currentTarget as HTMLInputElement).files ?? [],
              );
            }}
          />
        </div>
      </div>
    );
  }),
  useChatAnywhereSessionsState: vi.fn(() => ({
    sessions: [],
    currentSessionId: null,
    setCurrentSessionId: vi.fn(),
    setSessions: vi.fn(),
  })),
  useChatAnywhereSessions: vi.fn(() => ({ createSession: vi.fn() })),
  useChatAnywhereInput: vi.fn(() => ({
    setLoading: mockRuntimeSetLoading,
    getLoading: mockRuntimeGetLoading,
  })),
}));

vi.mock("@/api/modules/provider", () => ({
  providerApi: {
    listProviders: mockListProviders,
    getActiveModels: mockGetActiveModels,
    getReasoningCapability: mockGetReasoningCapability,
  },
}));

vi.mock("@/api/modules/tools", () => ({
  toolsApi: {
    listPermissionProfiles: mockListPermissionProfiles,
  },
}));

vi.mock("@/api/modules/agent", () => ({
  agentApi: {
    getAgentRunningConfig: mockGetAgentRunningConfig,
  },
}));

vi.mock("@/api/modules/chat", () => ({
  chatUploadResponseToAttachmentSuccess: (response: any) => ({
    url: mockFilePreviewUrl(response.url),
    canonicalPath: response.url,
    storedName: response.stored_name,
    digest: response.digest,
    immutableVersion: response.version,
    uploadReceipt: response.upload_receipt,
  }),
  chatApi: {
    uploadFile: mockUploadFile,
    filePreviewUrl: mockFilePreviewUrl,
    getChat: mockGetChat,
    getChatContextStats: vi.fn(async () => ({
      total_messages: 0,
      compressed_summary_tokens: 0,
      messages_tokens: 0,
      estimated_tokens: 0,
      max_input_length: 10000,
      context_usage_ratio: 0,
    })),
    getChatEvents: mockGetChatEvents,
    ensureTopicWorkspace: mockEnsureTopicWorkspace,
    subscribeChatEvents: vi.fn(() => vi.fn()),
    stopChat: mockStopChat,
  },
  sessionApi: {
    getRealIdForSession: vi.fn(() => null),
    setLastUserMessage: vi.fn(),
    stageLastUserMessage: vi.fn(),
    acceptLastUserMessage: vi.fn(),
    discardPendingUserMessage: vi.fn(),
    saveDraftText: vi.fn(),
    getSessionList: vi.fn(() => Promise.resolve([])),
  },
}));

vi.mock("@/api/modules/runtime", () => ({
  runtimeApi: {
    interruptRun: mockInterruptRun,
  },
}));

vi.mock("@/api/modules/macUse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/modules/macUse")>();
  return {
    ...actual,
    macUseApi: {
      ...actual.macUseApi,
      listWindows: mockListMacUseWindows,
    },
  };
});

vi.mock("@/api/modules/capability", () => ({
  capabilityApi: {
    draftCapabilityFromTask: mockDraftCapabilityFromTask,
    streamCollaborationDraft: mockStreamCollaborationDraft,
    listTaskRuns: mockListTaskRuns,
  },
}));

vi.mock("@/api/modules/time", () => ({
  timeApi: {
    getToday: mockGetToday,
  },
}));

vi.mock("@/api/modules/dailyBrief", () => ({
  dailyBriefApi: {
    getHomeSnapshot: mockGetDailyBriefHome,
    getDiscussionDraft: mockGetBriefDraft,
  },
}));

vi.mock("./kdocsRead", () => ({
  readKdocsDocumentSafely: mockReadKdocsDocumentSafely,
}));

vi.mock("@/api/modules/commands", () => ({
  commandsApi: {
    sendApprovalCommand: mockSendApprovalCommand,
    previewSessionPermission: mockPreviewSessionPermission,
    updateSessionPermission: mockUpdateSessionPermission,
  },
}));

vi.mock("@/api/modules/workspace", () => ({
  workspaceApi: {
    saveQueryToWiki: mockSaveQueryToWiki,
  },
}));

vi.mock("@/api/modules/plan", () => ({
  planApi: {
    getPlanConfig: mockGetPlanConfig,
    updatePlanConfig: mockUpdatePlanConfig,
    getCurrentPlan: vi.fn().mockResolvedValue(null),
  },
  subscribePlanUpdates: vi.fn(() => vi.fn()),
}));

vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  return {
    ...actual,
    // Modal: do not render when open=false, avoids CSS animation leaving content in the DOM
    Modal: ({
      open,
      children,
    }: {
      open: boolean;
      children: React.ReactNode;
    }) => (open ? <div data-testid="modal">{children}</div> : null),
  };
});
vi.mock("@/api/config", () => ({
  getApiUrl: mockGetApiUrl,
  getApiToken: vi.fn(() => ""),
}));

vi.mock("@/stores/agentStore", () => ({
  useAgentStore: vi.fn(() => ({
    selectedAgent: mockSelectedAgent(),
    setSelectedAgent: mockSetSelectedAgent,
    setLastChatId: mockSetLastChatId,
    getLastChatId: mockGetLastChatId,
  })),
}));

vi.mock("@/contexts/ThemeContext", () => ({
  useTheme: vi.fn(() => ({ isDark: false })),
}));

vi.mock("./welcomeGreetings/useWelcomeGreeting", () => ({
  useWelcomeGreeting: (input: any) => {
    mockWelcomeGreetingInput.current = input;
    return {
      id: input.enabled ? "fixed.test" : "greeting.localized.default",
      title: input.enabled ? "一条安全的动态招呼" : input.fallbackTitle,
      subtitle: input.enabled ? "只属于这个新入口。" : input.fallbackSubtitle,
      source: input.enabled ? "fixed" : "neutral",
    };
  },
}));

vi.mock("./sessionApi", () => ({
  default: {
    onSessionIdResolved: null,
    onSessionRemoved: null,
    onSessionSelected: null,
    onSessionCreated: null,
    getRealIdForSession: vi.fn(() => null),
    setLastUserMessage: vi.fn(),
    stageLastUserMessage: vi.fn(),
    acceptLastUserMessage: vi.fn(),
    discardPendingUserMessage: vi.fn(),
    saveDraftText: vi.fn(),
    rememberSessionAlias: vi.fn(),
    resolveEnsuredSession: vi.fn(),
  },
}));

vi.mock("./OptionsPanel/defaultConfig", () => ({
  default: { theme: { leftHeader: {} }, api: {} },
  getDefaultConfig: vi.fn(() => ({
    theme: { leftHeader: {} },
    welcome: {},
    sender: {},
  })),
}));

vi.mock("./ModelSelector", () => ({
  default: () => <div data-testid="model-selector" />,
}));

vi.mock("./components/ChatActionGroup", () => ({
  default: () => <div data-testid="action-group" />,
}));

vi.mock("./components/ChatHeaderTitle", () => ({
  default: () => <div data-testid="header-title" />,
}));

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
const mockActiveModel = {
  active_llm: { provider_id: "openai", model: "gpt-4" },
};
let currentMockActiveModel = mockActiveModel;
const mockProviders = [
  {
    id: "openai",
    name: "OpenAI",
    models: [
      {
        id: "gpt-4",
        name: "GPT-4",
        supports_multimodal: true,
        supports_image: true,
        supports_video: false,
      },
    ],
    extra_models: [],
  },
];

function latestConsoleChatBody(): any {
  const calls = ((globalThis.fetch as any)?.mock?.calls || []) as Array<
    [unknown, { body?: string } | undefined]
  >;
  const call = [...calls]
    .reverse()
    .find(
      ([url, options]) =>
        String(url).endsWith("/console/chat") && options?.body,
    );
  expect(call).toBeTruthy();
  return JSON.parse(call?.[1]?.body || "{}");
}

function mockConsoleChatTransport(
  handler: (url: unknown, options: any) => Promise<Response>,
) {
  return vi.fn((url, options) =>
    String(url).endsWith("/console/chat")
      ? handler(url, options)
      : Promise.resolve(
          new Response("{}", {
            headers: { "Content-Type": "application/json" },
          }),
        ),
  );
}

function kdocsComposerProps(): {
  onAddDocument: (document: KdocsDocumentItem) => void;
  onRemoveReference: (key: string) => void;
} {
  return capturedOptions.sender.context.props.children[1].props;
}

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location-probe">
      {location.pathname}
      {location.search}
    </div>
  );
}

function SameModelRouteRefreshButton() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/chat/chat-refresh")}>
      refresh same model route
    </button>
  );
}

function ChatSwitchButtons() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate("/chat/chat-2")}>
        switch to chat 2
      </button>
      <button type="button" onClick={() => navigate("/chat/chat-1")}>
        switch to chat 1
      </button>
    </>
  );
}

// ---------------------------------------------------------------------------
// tests
// ---------------------------------------------------------------------------
describe("ChatPage", () => {
  it("keeps retired image-generation prompts out of new chat suggestions", () => {
    expect(DEFAULT_EMPTY_PROMPT_SUGGESTIONS).toHaveLength(6);
    expect(
      normalizeEmptyPromptSuggestions([
        ...DEFAULT_EMPTY_PROMPT_SUGGESTIONS,
        { label: "生成一张图", prompt: "帮我生成一张图：" },
        { label: "Generate an image", prompt: "Help me generate an image:" },
      ]),
    ).toEqual(DEFAULT_EMPTY_PROMPT_SUGGESTIONS);
  });

  beforeEach(() => {
    window.sessionStorage.clear();
    capturedOptions = null;
    capturedDroppedFiles = [];
    artifactEditSelectionPage = 3;
    mockSelectedAgent.mockReturnValue("default");
    currentMockActiveModel = mockActiveModel;
    mockListProviders.mockResolvedValue(mockProviders);
    mockGetActiveModels.mockImplementation(async () => currentMockActiveModel);
    mockGetReasoningCapability.mockResolvedValue({
      supported: false,
      mode: "",
      fixed: false,
      default_enabled: false,
      note: "",
    });
    mockListPermissionProfiles.mockResolvedValue([
      {
        id: "workspace",
        name: "工作区",
        description: "工作区权限",
        examples: [],
      },
      {
        id: "full",
        name: "完全访问",
        description: "完全访问权限",
        examples: [],
      },
    ]);
    mockUploadFile.mockResolvedValue({
      url: "/Users/example/.lantai/media/55aa55aa55aa55aa55aa55aa55aa55aa_uploaded.png",
      file_name: "uploaded.png",
      stored_name: "55aa55aa55aa55aa55aa55aa55aa55aa_uploaded.png",
      size: 8,
      digest: "sha256:55aa55aa55aa55aa55aa55aa55aa55aa",
      version: 1,
      upload_receipt: "upload-receipt:v1:chat-page",
    });
    mockGetChat.mockResolvedValue({
      chat_id: "chat-1",
      messages: [],
      meta: {
        last_turn_run_id: "run-1",
      },
    });
    mockGetChatEvents.mockResolvedValue({
      chat_id: "chat-1",
      after_seq: 0,
      latest_seq: 0,
      events: [],
    });
    mockDraftCapabilityFromTask.mockResolvedValue({
      capability: { id: "cap-1" },
    });
    mockStreamCollaborationDraft.mockResolvedValue({
      capability: { id: "cap-1" },
      source_run: { id: "run-1" },
      evidence: [],
      warnings: [],
      suggested_skills: [],
      primary_asset: "skill",
      scope: {
        chat_id: "chat-1",
        anchor_run_id: "run-1",
        message_count: 2,
        turn_count: 1,
        tool_call_count: 0,
        truncated: false,
        redacted_count: 0,
      },
    });
    mockListTaskRuns.mockResolvedValue([]);
    mockGetToday.mockResolvedValue({
      date: "2026-08-01",
      timezone: "Asia/Shanghai",
      generated_at: "2026-08-01T00:00:00Z",
      items: [],
      counts: { reminder: 0, task: 0, automation: 0 },
    });
    mockGetDailyBriefHome.mockResolvedValue({
      state: "not_configured",
      visible: true,
      card: {
        title: "澜台简报",
        status_line: "选择你想关注的内容",
        hint: "点击后进入设置，不会自动联网",
        route: "/daily-brief",
        edition_id: null,
        edition_revision: null,
        edition_date: null,
        item_count: null,
        has_update: false,
        is_stale: false,
      },
    });
    mockStopChat.mockResolvedValue({ stopped: true });
    mockInterruptRun.mockResolvedValue({ stopped: true });
    mockEnsureTopicWorkspace.mockImplementation(async () => ({
      chat: {
        id: "chat-1",
        session_id: "console:default",
        user_id: "default",
        channel: "console",
        name: "新会话",
        created_at: null,
        updated_at: null,
        meta: {},
      },
      topic_workspace_dir: "/tmp/lantai/topic",
      effective_workspace_dir: "/tmp/lantai/topic",
      manual_workspace_dir: null,
    }));
    mockSendApprovalCommand.mockResolvedValue({ success: true, message: "ok" });
    mockPreviewSessionPermission.mockResolvedValue({
      queue_revision: "queue-v1",
      requests: [],
    });
    mockUpdateSessionPermission.mockResolvedValue({
      success: true,
      permission_mode: "full_auto",
      permission_profile: "full",
      resolved_request_ids: [],
    });
    mockModalConfirm.mockReset();
    mockGetPlanConfig.mockResolvedValue({ enabled: false });
    mockUpdatePlanConfig.mockResolvedValue({ enabled: true });
    mockGetAgentRunningConfig.mockResolvedValue({ max_input_length: 10_000 });
    mockReadKdocsDocumentSafely.mockImplementation(
      async (document: KdocsDocumentItem) => ({
        ...documentToKdocsReference(document),
        read_status: "read",
        read_snapshot: {
          version: document.version,
          content_hash: document.content_hash,
          read_at: "2026-09-20T00:00:00.000Z",
        },
        content_snapshot: {
          text: "文档正文",
          format: "markdown",
          truncated: false,
        },
      }),
    );
    mockSaveQueryToWiki.mockResolvedValue({ path: "wiki/周会纪要.md" });
    mockListMacUseWindows.mockResolvedValue({
      ok: true,
      data: { windows: [] },
    });
    mockSetLastChatId.mockReset();
    mockGetLastChatId.mockReset();
    mockWelcomeGreetingInput.current = null;
    mockApprovals.length = 0;
    (sessionApi.getRealIdForSession as any).mockReset();
    (sessionApi.getRealIdForSession as any).mockReturnValue(null);
    (window as any).currentSessionId = undefined;
    (window as any).currentUserId = undefined;
    (window as any).currentChannel = undefined;
    window.electronAPI = undefined;
    window.sessionStorage.clear();
  });

  afterEach(() => vi.clearAllMocks());

  // ── basic rendering ───────────────────────────────────────────────────────

  it("opens cached history on the bare chat route and leaves new-chat creation to the explicit command", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    expect(await screen.findByTestId("chat-ui")).toBeInTheDocument();
    expect(capturedOptions.session.initialSessionStrategy).toBe("first");
    expect(capturedOptions.session.initialSessionId).toBeUndefined();
    expect(mockWelcomeGreetingInput.current.enabled).toBe(false);
  });

  it("shows home shortcuts as one icon-led group and keeps their draft action", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    const chatUi = await screen.findByTestId("chat-ui");
    const welcome = document.createElement("div");
    welcome.className = "lantai-chat-anywhere-message-list-welcome";
    chatUi.prepend(welcome);

    const shortcuts = await screen.findByRole("group", {
      name: "新会话推荐词",
    });
    const buttons = Array.from(shortcuts.querySelectorAll("button"));
    expect(buttons.slice(0, 4).map((button) => button.textContent)).toEqual([
      "分析一份文档",
      "制作可编辑演示",
      "研究并附来源",
      "让浏览器帮我",
    ]);
    expect(buttons.every((button) => button.querySelector("svg"))).toBe(true);

    await user.click(buttons[0]);
    expect(screen.getByRole("textbox", { name: "chat input" })).toHaveValue(
      "读取我接下来附上的文档，提炼关键结论、风险和下一步：",
    );
    expect(screen.getByRole("textbox", { name: "chat input" })).toHaveFocus();
  });

  it("does not expose or probe the removed desktop speech input", async () => {
    const getAppleSpeechStatus = vi.fn().mockResolvedValue({
      ok: true,
      supported: true,
      available: true,
      supports_on_device: true,
      authorization: "authorized",
    });
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      getAppleSpeechStatus,
      transcribeAppleSpeech: vi.fn(),
    };

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    expect(getAppleSpeechStatus).not.toHaveBeenCalled();
    expect(capturedOptions.sender.beforeAttachments).toBeNull();
    expect(capturedOptions.sender.allowSpeech).toBe(false);
  });

  it("does not expose a permanent Mac app operation entry in the composer", async () => {
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
    };

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    expect(
      screen.queryByRole("button", { name: "让澜台操作这个应用" }),
    ).not.toBeInTheDocument();
  });

  it("offers target selection contextually after the model inspects Mac app control", async () => {
    const user = userEvent.setup();
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
    };

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const MacInspectRenderer =
      capturedOptions.customToolRenderConfig.mac_inspect;
    render(
      <MacInspectRenderer
        data={{
          status: "success",
          content: [
            { data: { arguments: {} } },
            {
              data: {
                output: JSON.stringify({
                  content: [
                    {
                      type: "text",
                      text: JSON.stringify({
                        ok: true,
                        action: "status",
                        effect_status: "not_applicable",
                        message: "Mac 应用操作组件可用",
                      }),
                    },
                  ],
                }),
              },
            },
          ],
        }}
      />,
    );

    await user.click(screen.getByRole("button", { name: "选择要操作的应用" }));

    expect(await screen.findByTestId("modal")).toBeInTheDocument();
    expect(mockListMacUseWindows).toHaveBeenCalledOnce();
  });

  it("offers a direct continuation for recent unfinished work on the new-chat page", async () => {
    mockListTaskRuns.mockResolvedValue([
      {
        id: "run-unwanted",
        agent_id: "default",
        source: "chat",
        chat_id: "chat-unwanted",
        title: "New Chat",
        user_intent: "New Chat",
        status: "running",
        started_at: "2026-08-01T01:33:00Z",
        artifacts: [],
      },
    ]);

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    await waitFor(() => expect(mockListTaskRuns).toHaveBeenCalled());
  });

  it("mounts a published daily brief without losing the original work continuation", async () => {
    mockListTaskRuns.mockResolvedValue([
      {
        id: "run-brief-continuation",
        agent_id: "default",
        source: "chat",
        chat_id: "chat-brief-continuation",
        title: "继续原工作",
        user_intent: "保留首页接续责任",
        status: "running",
        started_at: "2026-09-20T07:00:00Z",
        artifacts: [],
      },
    ]);
    mockGetDailyBriefHome.mockResolvedValue({
      state: "ready",
      visible: true,
      card: {
        title: "澜台简报",
        status_line: "2026-09-20",
        hint: "有新内容可阅读",
        route: "/daily-brief",
        edition_id: "edition-2026-09-20",
        edition_revision: 1,
        edition_date: "2026-09-20",
        item_count: 1,
        has_update: true,
        is_stale: false,
      },
    });

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });

    const chatUi = await screen.findByTestId("chat-ui");
    const welcome = document.createElement("div");
    welcome.className = "lantai-chat-anywhere-message-list-welcome";
    chatUi.prepend(welcome);

    await waitFor(() => {
      expect(mockGetDailyBriefHome).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId("chat-stage")).toHaveAttribute(
        "data-empty-conversation",
        "true",
      );
    });

    expect(
      await screen.findByRole("region", { name: "接续与发现" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "打开澜台简报：2026-09-20" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "继续原工作" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("这个变化与当前的 Runtime 工作有关。"),
    ).not.toBeInTheDocument();
  });

  it("restores the composer draft after opening the brief workbench and returning", async () => {
    const user = userEvent.setup();
    mockGetDailyBriefHome.mockResolvedValue({
      state: "ready",
      visible: true,
      card: {
        title: "澜台简报",
        status_line: "2026-09-20",
        hint: "有新内容可阅读",
        route: "/daily-brief",
        edition_id: "edition-2026-09-20",
        edition_revision: 1,
        edition_date: "2026-09-20",
        item_count: 1,
        has_update: true,
        is_stale: false,
      },
    });

    const first = renderWithProviders(
      <>
        <ChatPage />
        <LocationProbe />
      </>,
      { initialEntries: ["/chat"] },
    );
    const chatUi = await screen.findByTestId("chat-ui");
    const welcome = document.createElement("div");
    welcome.className = "lantai-chat-anywhere-message-list-welcome";
    chatUi.prepend(welcome);
    const textarea = screen.getByLabelText("chat input");
    await user.type(textarea, "不要丢掉这段尚未发送的草稿");

    const briefEntry = await screen.findByRole("button", {
      name: "打开澜台简报：2026-09-20",
    });
    await user.click(briefEntry);
    expect(screen.getByTestId("location-probe")).toHaveTextContent(
      "/daily-brief",
    );
    first.unmount();

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await waitFor(() =>
      expect(screen.getByLabelText("chat input")).toHaveValue(
        "不要丢掉这段尚未发送的草稿",
      ),
    );
  });

  it("keeps an explicitly routed conversation instead of creating a new one", async () => {
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/history-chat"],
    });
    expect(await screen.findByTestId("chat-ui")).toBeInTheDocument();
    expect(capturedOptions.session.initialSessionStrategy).toBe("first");
    expect(capturedOptions.session.initialSessionId).toBe("history-chat");
  });

  it("imports a brief into the existing composer only after a click and preserves unsent text", async () => {
    const user = userEvent.setup();
    mockGetBriefDraft.mockResolvedValue({
      composer_text: "简报讨论\n来源：https://example.com/primary",
    });
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/history-chat?briefDraft=draft-1"],
    });
    await screen.findByTestId("chat-ui");
    const textarea = screen.getByLabelText("chat input");
    await user.type(textarea, "原有未发送草稿");
    renderWithProviders(capturedOptions.sender.context);
    const insert = await screen.findByRole("button", { name: "插入到输入框" });
    expect(textarea).toHaveValue("原有未发送草稿");
    await user.click(insert);
    await waitFor(() =>
      expect(textarea).toHaveValue(
        "原有未发送草稿\n\n简报讨论\n来源：https://example.com/primary",
      ),
    );
    expect(capturedOptions.session.initialSessionId).toBe("history-chat");
  });

  it("remembers the active route so returning to chat restores this conversation", async () => {
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-retained"],
    });

    await screen.findByTestId("chat-ui");

    expect(mockSetLastChatId).toHaveBeenCalledWith("default", "chat-retained");
  });

  it("moves artifact view preferences when a temporary session resolves to its canonical chat", async () => {
    const temporaryChatId = "temp-1720000000002";
    const canonicalChatId = "chat-canonical-artifact";
    window.localStorage.removeItem(
      artifactWorkbenchStorageKey(temporaryChatId),
    );
    window.localStorage.removeItem(
      artifactWorkbenchStorageKey(canonicalChatId),
    );
    saveArtifactWorkbenchState(temporaryChatId, {
      openPaths: ["季度复盘.pptx"],
      activePath: "季度复盘.pptx",
      expanded: [],
      view: "preview",
      selectedWorkId: "work-deck-v2",
      selectedPageByWorkId: { "work-deck-v2": 3 },
    });

    renderWithProviders(
      <>
        <ChatPage />
        <LocationProbe />
      </>,
      { initialEntries: [`/chat/${temporaryChatId}`] },
    );
    await screen.findByTestId("chat-ui");

    act(() => {
      sessionApi.onSessionIdResolved?.(temporaryChatId, canonicalChatId);
    });

    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        `/chat/${canonicalChatId}`,
      ),
    );
    expect(loadArtifactWorkbenchState(canonicalChatId)).toEqual({
      openPaths: ["季度复盘.pptx"],
      activePath: "季度复盘.pptx",
      expanded: [],
      view: "preview",
      selectedWorkId: "work-deck-v2",
      selectedPageByWorkId: { "work-deck-v2": 3 },
    });
    expect(
      window.localStorage.getItem(artifactWorkbenchStorageKey(temporaryChatId)),
    ).toBeNull();
  });

  it("moves directly to the conversation explicitly selected by the user", async () => {
    renderWithProviders(
      <>
        <ChatPage />
        <LocationProbe />
      </>,
      { initialEntries: ["/chat/chat-current"] },
    );
    await screen.findByTestId("chat-ui");

    act(() => {
      sessionApi.onSessionSelected?.("chat-next", null);
    });

    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/chat/chat-next",
      ),
    );
    expect(mockSetLastChatId).toHaveBeenCalledWith("default", "chat-next");
  });

  it("settles an in-flight KDocs read into its origin without contaminating the next conversation", async () => {
    let finishRead:
      | ((value: ReturnType<typeof documentToKdocsReference>) => void)
      | undefined;
    const document: KdocsDocumentItem = {
      key: "drive-a:file-a",
      drive_id: "drive-a",
      file_id: "file-a",
      parent_id: "0",
      name: "经营周报.docx",
      type: "docx",
      suffix: "docx",
      is_folder: false,
      size: 128,
      version: "7",
      content_hash: "hash-7",
      link_id: "",
      original_url: "https://www.kdocs.cn/l/example",
      location: "我的云文档 / 周报",
      owner: null,
      modifier: null,
      shared: false,
      permissions: { can_read: true },
    };
    mockReadKdocsDocumentSafely.mockReturnValueOnce(
      new Promise((resolve) => {
        finishRead = resolve;
      }),
    );

    renderWithProviders(
      <>
        <ChatPage />
        <LocationProbe />
      </>,
      { initialEntries: ["/chat/chat-origin"] },
    );
    await screen.findByTestId("chat-ui");

    act(() => kdocsComposerProps().onAddDocument(document));
    await waitFor(() =>
      expect(
        capturedOptions.api.getRequestBizParams().meta.kdocs_references[0]
          .read_status,
      ).toBe("reading"),
    );

    act(() => sessionApi.onSessionSelected?.("chat-next", null));
    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/chat/chat-next",
      ),
    );
    await waitFor(() =>
      expect(capturedOptions.api.getRequestBizParams()).toEqual({}),
    );

    await act(async () => {
      finishRead?.({
        ...documentToKdocsReference(document),
        read_status: "read",
      });
      await Promise.resolve();
    });

    expect(capturedOptions.api.getRequestBizParams()).toEqual({});
    expect(loadKdocsReferences("default", "chat-origin")).toEqual([
      expect.objectContaining({
        file_id: "file-a",
        read_status: "read",
      }),
    ]);
  });

  it("does not resurrect a KDocs reference removed while its read is in flight", async () => {
    let finishRead:
      | ((value: ReturnType<typeof documentToKdocsReference>) => void)
      | undefined;
    const document: KdocsDocumentItem = {
      key: "drive-a:file-remove",
      drive_id: "drive-a",
      file_id: "file-remove",
      parent_id: "0",
      name: "待删除文档.docx",
      type: "docx",
      suffix: "docx",
      is_folder: false,
      size: 64,
      version: "1",
      content_hash: "hash-remove",
      link_id: "",
      original_url: "",
      location: "我的云文档",
      owner: null,
      modifier: null,
      shared: false,
      permissions: { can_read: true },
    };
    mockReadKdocsDocumentSafely.mockReturnValueOnce(
      new Promise((resolve) => {
        finishRead = resolve;
      }),
    );

    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-remove"],
    });
    await screen.findByTestId("chat-ui");
    act(() => kdocsComposerProps().onAddDocument(document));
    await waitFor(() =>
      expect(
        capturedOptions.api.getRequestBizParams().meta.kdocs_references,
      ).toHaveLength(1),
    );

    const referenceKey = documentToKdocsReference(document).key;
    act(() => kdocsComposerProps().onRemoveReference(referenceKey));
    await waitFor(() =>
      expect(capturedOptions.api.getRequestBizParams()).toEqual({}),
    );
    await act(async () => {
      finishRead?.({
        ...documentToKdocsReference(document),
        read_status: "read",
      });
      await Promise.resolve();
    });

    expect(capturedOptions.api.getRequestBizParams()).toEqual({});
    expect(loadKdocsReferences("default", "chat-remove")).toEqual([]);
  });

  it("clears the submitted KDocs references after the chat request is accepted", async () => {
    const document: KdocsDocumentItem = {
      key: "drive-a:file-sent",
      drive_id: "drive-a",
      file_id: "file-sent",
      parent_id: "0",
      name: "已发送文档.docx",
      type: "docx",
      suffix: "docx",
      is_folder: false,
      size: 96,
      version: "3",
      content_hash: "hash-sent",
      link_id: "",
      original_url: "https://www.kdocs.cn/l/sent",
      location: "我的云文档 / 待办",
      owner: null,
      modifier: null,
      shared: false,
      permissions: { can_read: true },
    };
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);

    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-kdocs-success"],
    });
    await screen.findByTestId("chat-ui");
    act(() => kdocsComposerProps().onAddDocument(document));
    await waitFor(() =>
      expect(
        capturedOptions.api.getRequestBizParams().meta.kdocs_references[0]
          .read_status,
      ).toBe("read"),
    );
    const bizParams = capturedOptions.api.getRequestBizParams();

    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "分析这份文档" }],
      biz_params: bizParams,
      signal: undefined,
    });

    expect(latestConsoleChatBody().meta.kdocs_references).toHaveLength(1);
    expect(capturedOptions.api.getRequestBizParams()).toEqual({});
    expect(loadKdocsReferences("default", "chat-kdocs-success")).toEqual([]);
  });

  it("preserves KDocs references when the chat request fails", async () => {
    const document: KdocsDocumentItem = {
      key: "drive-a:file-retry",
      drive_id: "drive-a",
      file_id: "file-retry",
      parent_id: "0",
      name: "需要重试.docx",
      type: "docx",
      suffix: "docx",
      is_folder: false,
      size: 96,
      version: "4",
      content_hash: "hash-retry",
      link_id: "",
      original_url: "https://www.kdocs.cn/l/retry",
      location: "我的云文档 / 待办",
      owner: null,
      modifier: null,
      shared: false,
      permissions: { can_read: true },
    };
    global.fetch = vi.fn().mockRejectedValue(new Error("网络暂不可用"));

    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-kdocs-failure"],
    });
    await screen.findByTestId("chat-ui");
    act(() => kdocsComposerProps().onAddDocument(document));
    await waitFor(() =>
      expect(
        capturedOptions.api.getRequestBizParams().meta.kdocs_references[0]
          .read_status,
      ).toBe("read"),
    );
    const bizParams = capturedOptions.api.getRequestBizParams();

    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "分析这份文档" }],
      biz_params: bizParams,
      signal: undefined,
    });

    expect(
      capturedOptions.api.getRequestBizParams().meta.kdocs_references,
    ).toHaveLength(1);
    expect(loadKdocsReferences("default", "chat-kdocs-failure")).toHaveLength(
      1,
    );
  });

  it("keeps a delayed submission owned by its original route after navigation", async () => {
    let resolveActiveModels!: (value: typeof mockActiveModel) => void;
    mockGetActiveModels.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveActiveModels = resolve;
      }),
    );
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
    } as Response);
    renderWithProviders(
      <>
        <ChatPage />
        <LocationProbe />
      </>,
      { initialEntries: ["/chat/chat-original"] },
    );
    await screen.findByTestId("chat-ui");

    const submission = capturedOptions.api.fetch({
      input: [
        {
          role: "user",
          content: "原会话任务",
          session: {
            session_id: "console:original",
            user_id: "default",
            channel: "console",
          },
        },
      ],
      signal: undefined,
    });
    await waitFor(() => expect(mockGetActiveModels).toHaveBeenCalled());

    act(() => {
      sessionApi.onSessionSelected?.("chat-next", null);
    });
    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/chat/chat-next",
      ),
    );
    resolveActiveModels(mockActiveModel);
    await submission;

    expect(mockEnsureTopicWorkspace).toHaveBeenCalledWith(
      expect.objectContaining({ chat_id: "chat-original" }),
    );
    expect(latestConsoleChatBody()).toEqual(
      expect.objectContaining({ chat_id: "chat-1" }),
    );
    expect(sessionApi.rememberSessionAlias).not.toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
      expect.anything(),
      "chat-next",
    );
  });

  it("gives an unsent new conversation a restorable route immediately", async () => {
    renderWithProviders(
      <>
        <ChatPage />
        <LocationProbe />
      </>,
      { initialEntries: ["/chat/chat-current"] },
    );
    await screen.findByTestId("chat-ui");
    const initialEntryId = mockWelcomeGreetingInput.current.entryId;
    expect(mockWelcomeGreetingInput.current.enabled).toBe(false);

    act(() => {
      sessionApi.onSessionCreated?.("1785216000000");
    });

    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/chat/1785216000000",
      ),
    );
    await waitFor(() =>
      expect(mockWelcomeGreetingInput.current.enabled).toBe(true),
    );
    const newChatEntryId = mockWelcomeGreetingInput.current.entryId;
    expect(newChatEntryId).not.toBe(initialEntryId);
    expect(mockSetLastChatId).toHaveBeenCalledWith("default", "1785216000000");

    act(() => {
      sessionApi.onSessionIdResolved?.("1785216000000", "chat-canonical");
    });
    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/chat/chat-canonical",
      ),
    );
    expect(mockWelcomeGreetingInput.current.enabled).toBe(true);
    expect(mockWelcomeGreetingInput.current.entryId).toBe(newChatEntryId);

    act(() => {
      sessionApi.onSessionSelected?.("chat-history", null);
    });
    await waitFor(() =>
      expect(mockWelcomeGreetingInput.current.enabled).toBe(false),
    );
  });

  it("retires the welcome after the first accepted user turn", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-current"],
    });
    await screen.findByTestId("chat-ui");

    act(() => {
      sessionApi.onSessionCreated?.("local-session-1");
    });
    await waitFor(() =>
      expect(mockWelcomeGreetingInput.current.enabled).toBe(true),
    );

    await act(async () => {
      await capturedOptions.api.fetch({
        input: [
          {
            role: "user",
            content: "开始一段真正的新对话",
            session: {
              session_id: "local-session-1",
              user_id: "default",
              channel: "console",
            },
          },
        ],
        signal: undefined,
      });
    });

    expect(mockWelcomeGreetingInput.current.enabled).toBe(false);

    act(() => {
      sessionApi.onSessionSelected?.("local-session-1", null);
    });
    expect(mockWelcomeGreetingInput.current.enabled).toBe(false);
  });

  it("renders the current chat header controls", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");
    expect(screen.getByTestId("header-title")).toBeInTheDocument();
    const artifactButton = screen.getByRole("button", {
      name: "查看任务产物",
    });
    expect(artifactButton).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "沉淀能力" }),
    ).not.toBeInTheDocument();
    expect(capturedOptions.actions.list[2].icon.props.title).toBe(
      "将此前协作沉淀为 Skill",
    );
    expect(
      capturedOptions.actions.list.some(
        (action: { icon?: { props?: { title?: string } } }) =>
          action.icon?.props?.title === "查看本轮产物",
      ),
    ).toBe(false);
  });

  it("opens task artifacts beside the current conversation without changing routes", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <>
        <ChatPage />
        <LocationProbe />
      </>,
      { initialEntries: ["/chat/chat-1"] },
    );
    await screen.findByTestId("chat-ui");

    await user.click(screen.getByRole("button", { name: "查看任务产物" }));

    expect(screen.getByTestId("chat-artifact-panel")).toBeInTheDocument();
    expect(screen.getByTestId("location-probe")).toHaveTextContent(
      "/chat/chat-1",
    );
    expect(
      screen.getByRole("button", { name: "收起任务产物" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "收起产物面板" }));
    expect(screen.queryByTestId("chat-artifact-panel")).not.toBeInTheDocument();
  });

  it("prepares a page-bound artifact edit without sending and submits it exactly once", async () => {
    const user = userEvent.setup();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
    } as Response);

    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-1"],
    });
    await screen.findByTestId("chat-ui");

    await user.click(screen.getByRole("button", { name: "查看任务产物" }));
    await user.click(screen.getByRole("button", { name: "准备第 3 页修改" }));

    expect(screen.queryByTestId("chat-artifact-panel")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "chat input" })).toHaveValue(
        "请按选中的目标修改这个成果。",
      ),
    );
    expect(
      screen.getByRole("group", { name: "成果修改目标" }),
    ).toHaveTextContent("季度复盘");
    expect(
      screen.getByRole("group", { name: "成果修改目标" }),
    ).toHaveTextContent("V1 · 第 3 页");
    expect(
      (global.fetch as any).mock.calls.filter(([url]: [unknown]) =>
        String(url).endsWith("/console/chat"),
      ),
    ).toHaveLength(0);

    const bizParams = capturedOptions.api.getRequestBizParams();
    expect(bizParams.meta.artifact_edit_target).toEqual({
      schema_version: 1,
      chat_id: "chat-1",
      asset_id: "asset-deck",
      work_id: "work-deck-v1",
      edit_path: "/tmp/lantai/topic/季度复盘.pptx",
      base_revision: {
        version: 1,
        digest: "sha256:deck-v1",
      },
      selection: { kind: "page", page: 3 },
    });

    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "把第三页改得更清楚" }],
        biz_params: bizParams,
        signal: undefined,
      });
    });

    const consoleChatCalls = (global.fetch as any).mock.calls.filter(
      ([url, options]: [unknown, { method?: string }]) =>
        String(url).endsWith("/console/chat") && options?.method === "POST",
    );
    expect(consoleChatCalls).toHaveLength(1);
    expect(latestConsoleChatBody().meta.artifact_edit_target).toEqual(
      bizParams.meta.artifact_edit_target,
    );
    await waitFor(() =>
      expect(capturedOptions.api.getRequestBizParams()).toEqual({}),
    );
    expect(
      screen.queryByRole("group", { name: "成果修改目标" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the page-bound artifact edit target when submission fails", async () => {
    const user = userEvent.setup();
    global.fetch = vi.fn().mockRejectedValue(new Error("网络暂不可用"));

    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-1"],
    });
    await screen.findByTestId("chat-ui");
    await user.click(screen.getByRole("button", { name: "查看任务产物" }));
    await user.click(screen.getByRole("button", { name: "准备第 3 页修改" }));

    const bizParams = capturedOptions.api.getRequestBizParams();
    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "把第三页改得更清楚" }],
        biz_params: bizParams,
        signal: undefined,
      });
    });

    expect(
      capturedOptions.api.getRequestBizParams().meta.artifact_edit_target,
    ).toEqual(bizParams.meta.artifact_edit_target);
    expect(
      screen.getByRole("group", { name: "成果修改目标" }),
    ).toHaveTextContent("V1 · 第 3 页");
  });

  it("keeps an unsent artifact target scoped to its chat while switching chats", async () => {
    const user = userEvent.setup();
    mockGetChat.mockImplementation(async (chatId: string) => ({
      chat_id: chatId,
      messages: [],
      meta: {},
    }));

    renderWithProviders(
      <>
        <ChatPage />
        <ChatSwitchButtons />
      </>,
      { initialEntries: ["/chat/chat-1"] },
    );
    await screen.findByTestId("chat-ui");
    await user.click(screen.getByRole("button", { name: "查看任务产物" }));
    await user.click(screen.getByRole("button", { name: "准备第 3 页修改" }));
    expect(
      screen.getByRole("group", { name: "成果修改目标" }),
    ).toHaveTextContent("季度复盘");

    await user.click(screen.getByRole("button", { name: "switch to chat 2" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("group", { name: "成果修改目标" }),
      ).not.toBeInTheDocument(),
    );
    expect(capturedOptions.api.getRequestBizParams()).toEqual({});

    await user.click(screen.getByRole("button", { name: "switch to chat 1" }));
    expect(
      await screen.findByRole("group", { name: "成果修改目标" }),
    ).toHaveTextContent("V1 · 第 3 页");
    expect(
      capturedOptions.api.getRequestBizParams().meta.artifact_edit_target
        .chat_id,
    ).toBe("chat-1");
  });

  it("restores the unsent artifact target after the chat page unmounts and respects explicit removal", async () => {
    const user = userEvent.setup();
    global.fetch = vi.fn().mockResolvedValue(new Response(""));
    const first = renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-1"],
    });
    await screen.findByTestId("chat-ui");
    await user.click(screen.getByRole("button", { name: "查看任务产物" }));
    await user.click(screen.getByRole("button", { name: "准备第 3 页修改" }));
    const target =
      capturedOptions.api.getRequestBizParams().meta.artifact_edit_target;
    first.unmount();

    const returned = renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-1"],
    });
    expect(
      await screen.findByRole("group", { name: "成果修改目标" }),
    ).toHaveTextContent("V1 · 第 3 页");
    expect(
      capturedOptions.api.getRequestBizParams().meta.artifact_edit_target,
    ).toEqual(target);
    expect(
      (global.fetch as any).mock.calls.filter(([url]: [unknown]) =>
        String(url).endsWith("/console/chat"),
      ),
    ).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "移除成果修改目标" }));
    returned.unmount();
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
    await screen.findByTestId("chat-ui");
    expect(capturedOptions.api.getRequestBizParams()).toEqual({});
  });

  it("isolates artifact drafts between agents sharing a chat route", async () => {
    const user = userEvent.setup();
    mockGetLastChatId.mockReturnValue("chat-1");
    const view = renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-1"],
    });
    await screen.findByTestId("chat-ui");
    await user.click(screen.getByRole("button", { name: "查看任务产物" }));
    await user.click(screen.getByRole("button", { name: "准备第 3 页修改" }));
    mockSelectedAgent.mockReturnValue("secondary");
    view.rerender(<ChatPage />);
    await waitFor(() =>
      expect(capturedOptions.api.getRequestBizParams()).toEqual({}),
    );
    expect(
      screen.queryByRole("group", { name: "成果修改目标" }),
    ).not.toBeInTheDocument();
    mockSelectedAgent.mockReturnValue("default");
    view.rerender(<ChatPage />);
    expect(
      await screen.findByRole("group", { name: "成果修改目标" }),
    ).toHaveTextContent("V1 · 第 3 页");
  });

  it.each([3, 4])(
    "keeps a newly prepared page %i target when an older submission is accepted",
    async (nextPage) => {
      const user = userEvent.setup();
      let accept!: (response: Response) => void;
      global.fetch = mockConsoleChatTransport(
        () =>
          new Promise<Response>((resolve) => {
            accept = resolve;
          }),
      );
      const first = renderWithProviders(<ChatPage />, {
        initialEntries: ["/chat/chat-1"],
      });
      await screen.findByTestId("chat-ui");
      await user.click(screen.getByRole("button", { name: "查看任务产物" }));
      await user.click(screen.getByRole("button", { name: "准备第 3 页修改" }));
      const sending = capturedOptions.api.fetch({
        input: [{ role: "user", content: "修改先前目标" }],
        biz_params: capturedOptions.api.getRequestBizParams(),
      });
      await waitFor(() => expect(accept).toBeTypeOf("function"));
      await user.click(
        screen.getByRole("button", { name: "移除成果修改目标" }),
      );
      artifactEditSelectionPage = nextPage;
      await user.click(screen.getByRole("button", { name: "查看任务产物" }));
      await user.click(screen.getByRole("button", { name: "准备第 3 页修改" }));
      await act(async () => {
        accept(new Response(""));
        await sending;
      });
      expect(
        screen.getByRole("group", { name: "成果修改目标" }),
      ).toHaveTextContent(`第 ${nextPage} 页`);
      first.unmount();
      renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
      expect(
        await screen.findByRole("group", { name: "成果修改目标" }),
      ).toHaveTextContent(`第 ${nextPage} 页`);
    },
  );

  it("clears an accepted artifact draft in the returned chat when its earlier page was unmounted", async () => {
    const user = userEvent.setup();
    let accept!: (response: Response) => void;
    global.fetch = mockConsoleChatTransport(
      vi
        .fn()
        .mockImplementationOnce(
          () =>
            new Promise<Response>((resolve) => {
              accept = resolve;
            }),
        )
        .mockResolvedValue(new Response("")),
    );
    const first = renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-1"],
    });
    await screen.findByTestId("chat-ui");
    await user.click(screen.getByRole("button", { name: "查看任务产物" }));
    await user.click(screen.getByRole("button", { name: "准备第 3 页修改" }));
    const sending = capturedOptions.api.fetch({
      input: [{ role: "user", content: "修改第三页" }],
      biz_params: capturedOptions.api.getRequestBizParams(),
    });
    await waitFor(() => expect(accept).toBeTypeOf("function"));
    first.unmount();
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
    await screen.findByRole("group", { name: "成果修改目标" });
    await act(async () => {
      accept(new Response(""));
      await sending;
    });
    expect(
      screen.queryByRole("group", { name: "成果修改目标" }),
    ).not.toBeInTheDocument();
    expect(capturedOptions.api.getRequestBizParams()).toEqual({});
  });

  it("requires confirmation before saving a response to Wiki", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const response = {
      output: [
        {
          role: "assistant",
          content: "# 周会纪要\n需要跟进三项任务。",
        },
      ],
    };
    await act(() =>
      capturedOptions.actions.list[1].onClick({
        data: response,
      }),
    );

    expect(mockModalConfirm).toHaveBeenCalledTimes(1);
    expect(mockSaveQueryToWiki).not.toHaveBeenCalled();

    const confirmOptions = mockModalConfirm.mock.calls[0][0];
    expect(confirmOptions).toEqual(
      expect.objectContaining({
        title: "workspace.saveToWikiConfirmTitle",
        content: "workspace.saveToWikiConfirmDesc",
        okText: "workspace.confirmSaveToWiki",
        cancelText: "common.cancel",
        autoFocusButton: "cancel",
        centered: true,
      }),
    );

    await act(async () => {
      await confirmOptions.onOk();
    });
    expect(mockSaveQueryToWiki).toHaveBeenCalledWith(
      "周会纪要",
      "# 周会纪要\n需要跟进三项任务。",
    );
  });

  it("copies only the final answer from a response that also contains thinking", async () => {
    const clipboardWrite = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await act(() =>
      capturedOptions.actions.list[0].onClick({
        data: {
          output: [
            {
              type: "reasoning",
              role: "assistant",
              content: [{ type: "text", text: "不应进入剪贴板的 Thinking" }],
            },
            {
              type: "message",
              role: "assistant",
              content: [{ type: "text", text: "只复制这段最终回答" }],
            },
          ],
        },
      }),
    );

    expect(clipboardWrite).toHaveBeenCalledOnce();
    expect(clipboardWrite).toHaveBeenCalledWith("只复制这段最终回答");
  });

  it("distills the chat prefix anchored to the clicked response", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
    await screen.findByTestId("chat-ui");

    await act(() =>
      capturedOptions.actions.list[2].onClick({
        data: {
          id: "response-1",
          metadata: { native_runtime_turn_id: "run-1" },
          output: [],
        },
      }),
    );

    await waitFor(() =>
      expect(mockStreamCollaborationDraft).toHaveBeenCalledWith(
        {
          chat_id: "chat-1",
          anchor_run_id: "run-1",
          anchor_response_id: "response-1",
        },
        expect.objectContaining({
          signal: expect.any(AbortSignal),
          onEvent: expect.any(Function),
        }),
      ),
    );
  });

  it("hides internal memory tools while preserving generic fallback rendering", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const MemoryRenderer = capturedOptions.customToolRenderConfig.memory_search;
    const { container } = render(<MemoryRenderer data={{}} />);
    expect(container).toBeEmptyDOMElement();

    const GenericRenderer =
      capturedOptions.customToolRenderConfig.some_unregistered_tool;
    render(
      <GenericRenderer
        data={{
          status: "completed",
          content: [{ data: { name: "some_unregistered_tool" } }],
        }}
      />,
    );
    expect(screen.getByText("some_unregistered_tool")).toBeInTheDocument();
  });

  it("routes generate_image events to the dedicated in-chat image card", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const ImageRenderer = capturedOptions.customToolRenderConfig.generate_image;
    const { container, rerender } = render(
      <ImageRenderer
        data={{
          status: "running",
          content: [
            {
              data: {
                name: "generate_image",
                arguments: { prompt: "月光下的山谷" },
              },
            },
          ],
        }}
      />,
    );
    const card = container.querySelector("[data-image-generation-card='1']");

    expect(card).toBeInTheDocument();
    expect(card).toHaveAttribute("data-chat-tool-call", "1");

    rerender(
      <ImageRenderer
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "generate_image",
                arguments: { prompt: "月光下的山谷" },
              },
            },
            {
              data: {
                output: [
                  {
                    type: "image",
                    image_url: "https://images.example/moonlit-valley.png",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(container.querySelector("[data-image-generation-card='1']")).toBe(
      card,
    );
    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      "https://images.example/moonlit-valley.png",
    );
  });

  // ── customFetch: model not configured → show modal ────────────────────────

  it("customFetch returns 400 and shows modal when model is not configured", async () => {
    mockGetActiveModels.mockResolvedValue({ active_llm: undefined });
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    // directly invoke capturedOptions.api.fetch (openclaw pattern)
    const response = await capturedOptions.api.fetch({
      input: [],
      signal: undefined,
    });
    expect(response.status).toBe(400);
    const body = await response.clone().json();
    expect(body.object).toBe("response");
    expect(body.status).toBe("failed");
    expect(body.type).toBe("response.failed");
    expect(
      await screen.findByText("modelConfig.promptTitle"),
    ).toBeInTheDocument();
  });

  it("shows model config modal when provider API throws", async () => {
    mockGetActiveModels.mockRejectedValue(new Error("network"));
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const response = await capturedOptions.api.fetch({
      input: [],
      signal: undefined,
    });
    expect(response.status).toBe(400);
    const body = await response.clone().json();
    expect(body.object).toBe("response");
    expect(body.status).toBe("failed");
    expect(body.type).toBe("response.failed");
    expect(
      await screen.findByText("modelConfig.promptTitle"),
    ).toBeInTheDocument();
  });

  // ── modal interaction ─────────────────────────────────────────────────────

  it("clicking Skip button closes the modal", async () => {
    mockGetActiveModels.mockResolvedValue({ active_llm: undefined });
    const user = userEvent.setup();
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await capturedOptions.api.fetch({ input: [], signal: undefined });
    await screen.findByText("modelConfig.promptTitle");

    await user.click(screen.getByText("modelConfig.skipButton"));
    // antd Modal has animations; wait for DOM removal
    await waitFor(
      () =>
        expect(
          screen.queryByText("modelConfig.skipButton"),
        ).not.toBeInTheDocument(),
      { timeout: 3000 },
    );
  });

  it("routes the recommended choice through Watcha and preserves the submitted draft", async () => {
    mockGetActiveModels.mockResolvedValue({ active_llm: undefined });
    const user = userEvent.setup();
    renderWithProviders(
      <>
        <ChatPage />
        <LocationProbe />
      </>,
      { initialEntries: ["/chat/thread-setup"] },
    );
    await screen.findByTestId("chat-ui");

    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "继续这条消息" }],
        signal: undefined,
      });
    });

    expect(
      await screen.findByText("modelConfig.watchaChoice"),
    ).toBeInTheDocument();
    expect(
      JSON.parse(
        window.sessionStorage.getItem(
          "lantai:model-setup-draft:/chat/thread-setup",
        ) || "{}",
      ),
    ).toMatchObject({
      version: 2,
      returnTo: "/chat/thread-setup",
      composerText: "继续这条消息",
      submittedInput: [{ role: "user", content: "继续这条消息" }],
    });
    await user.click(screen.getByText("modelConfig.watchaChoice"));
    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/account?setup=watcha&returnTo=%2Fchat%2Fthread-setup",
      ),
    );
  });

  it("restores a setup draft into an empty chat composer", async () => {
    persistModelSetupDraft("/chat/thread-returned", "从配置页回来的草稿");
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/thread-returned"],
    });

    expect(await screen.findByLabelText("chat input")).toHaveValue(
      "从配置页回来的草稿",
    );
    expect(consumeModelSetupDraft("/chat/thread-returned")).toBe("");
  });

  it("restores attachments after model setup and submits them only on the user's next send", async () => {
    persistModelSetupDraftSnapshot("/chat/thread-returned", {
      composerText: "请审阅附件",
      submittedInput: [
        {
          role: "user",
          content: [
            { type: "text", text: "请审阅附件" },
            {
              type: "file",
              filename: "brief.pdf",
              file_url: "http://localhost/files/preview/brief.pdf",
            },
          ],
        },
      ],
    });
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/thread-returned"],
    });

    expect(await screen.findByLabelText("chat input")).toHaveValue(
      "请审阅附件",
    );
    expect(global.fetch).not.toHaveBeenCalledWith(
      "/api/console/chat",
      expect.anything(),
    );

    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "请审阅修订后的附件" }],
        signal: undefined,
      });
    });

    const requestBody = latestConsoleChatBody();
    expect(requestBody.input[0].content).toEqual([
      { type: "text", text: "请审阅修订后的附件" },
      {
        type: "file",
        filename: "brief.pdf",
        file_url: "/brief.pdf",
      },
    ]);
  });

  it("keeps restored attachment metadata across a chat page unmount until an accepted send", async () => {
    const attachment = {
      type: "file",
      filename: "brief.pdf",
      file_url: "/brief.pdf",
      stored_name: "brief-v1.pdf",
      digest: "sha256:brief",
      version: 1,
      upload_receipt: "upload-receipt:v1:brief",
      role: "reference",
      role_confirmed: true,
    };
    persistModelSetupDraftSnapshot("/chat/chat-1", {
      composerText: "请审阅附件",
      submittedInput: [
        {
          role: "user",
          content: [{ type: "text", text: "请审阅附件" }, attachment],
        },
      ],
      taskContext: { agentId: "default" },
    });
    global.fetch = vi.fn().mockResolvedValue(new Response(""));
    const first = renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-1"],
    });
    await screen.findByDisplayValue("请审阅附件");
    first.unmount();
    const returned = renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-1"],
    });
    await screen.findByTestId("chat-ui");
    expect(
      (global.fetch as any).mock.calls.filter(([url]: [unknown]) =>
        String(url).endsWith("/console/chat"),
      ),
    ).toHaveLength(0);
    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "审阅修订稿" }],
      });
    });
    expect(latestConsoleChatBody().input[0].content).toEqual([
      { type: "text", text: "审阅修订稿" },
      attachment,
    ]);
    returned.unmount();
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
    await screen.findByTestId("chat-ui");
    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "下一条消息" }],
      });
    });
    expect(latestConsoleChatBody().input[0].content).toBe("下一条消息");
  });

  it.each(["network", "http"])(
    "retains restored attachments after a %s failure for explicit retry",
    async (failureKind) => {
      const attachment = {
        type: "file",
        filename: "brief.pdf",
        file_url: "/brief.pdf",
      };
      persistModelSetupDraftSnapshot("/chat/chat-1", {
        composerText: "请审阅附件",
        submittedInput: [{ role: "user", content: [attachment] }],
      });
      const fetchMock = vi.fn();
      if (failureKind === "network")
        fetchMock.mockRejectedValueOnce(new Error("暂时断网"));
      else
        fetchMock.mockResolvedValueOnce(
          new Response('{"detail":"暂不可用"}', {
            status: 503,
            headers: { "Content-Type": "application/json" },
          }),
        );
      global.fetch = mockConsoleChatTransport(
        fetchMock.mockResolvedValue(new Response("")),
      );
      renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
      await screen.findByDisplayValue("请审阅附件");
      await act(async () => {
        await capturedOptions.api.fetch({
          input: [{ role: "user", content: "首次发送" }],
        });
      });
      expect(latestConsoleChatBody().input[0].content).toEqual([
        { type: "text", text: "首次发送" },
        attachment,
      ]);
      await act(async () => {
        await capturedOptions.api.fetch({
          input: [{ role: "user", content: "重试发送" }],
        });
      });
      expect(latestConsoleChatBody().input[0].content).toEqual([
        { type: "text", text: "重试发送" },
        attachment,
      ]);
    },
  );

  it("isolates restored attachments while switching chats and agents", async () => {
    const user = userEvent.setup();
    mockGetLastChatId.mockReturnValue("chat-1");
    const attachment = {
      type: "file",
      filename: "brief.pdf",
      file_url: "/brief.pdf",
    };
    persistModelSetupDraftSnapshot("/chat/chat-1", {
      composerText: "请审阅附件",
      submittedInput: [{ role: "user", content: [attachment] }],
      taskContext: { agentId: "default" },
    });
    global.fetch = vi.fn().mockResolvedValue(new Response(""));
    const ui = () => (
      <>
        <ChatPage />
        <ChatSwitchButtons />
      </>
    );
    const view = renderWithProviders(ui(), {
      initialEntries: ["/chat/chat-1"],
    });
    await screen.findByDisplayValue("请审阅附件");
    await user.click(screen.getByRole("button", { name: "switch to chat 2" }));
    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "另一个会话" }],
      });
    });
    expect(latestConsoleChatBody().input[0].content).toBe("另一个会话");
    await user.click(screen.getByRole("button", { name: "switch to chat 1" }));
    mockSelectedAgent.mockReturnValue("secondary");
    view.rerender(ui());
    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "另一个 Agent" }],
      });
    });
    expect(latestConsoleChatBody().input[0].content).toBe("另一个 Agent");
    mockSelectedAgent.mockReturnValue("default");
    view.rerender(ui());
    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "返回原会话" }],
      });
    });
    expect(latestConsoleChatBody().input[0].content).toEqual([
      { type: "text", text: "返回原会话" },
      attachment,
    ]);
  });

  it("does not consume a model setup draft owned by another agent", async () => {
    mockGetLastChatId.mockReturnValue("chat-1");
    persistModelSetupDraftSnapshot("/chat/chat-1", {
      composerText: "default 的草稿",
      submittedInput: [
        {
          role: "user",
          content: [
            { type: "file", filename: "private.pdf", file_url: "/private.pdf" },
          ],
        },
      ],
      taskContext: { agentId: "default" },
    });
    mockSelectedAgent.mockReturnValue("secondary");
    global.fetch = vi.fn().mockResolvedValue(new Response(""));
    const view = renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-1"],
    });
    await screen.findByTestId("chat-ui");
    expect(screen.getByLabelText("chat input")).toHaveValue("");
    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "secondary 的消息" }],
      });
    });
    expect(latestConsoleChatBody().input[0].content).toBe("secondary 的消息");
    mockSelectedAgent.mockReturnValue("default");
    view.rerender(<ChatPage />);
    await waitFor(() => {
      expect(sessionApi.saveDraftText).toHaveBeenCalledWith(
        "chat-1",
        "default 的草稿",
      );
    });
    expect(consumeModelSetupDraft("/chat/chat-1")).toBe("");
    await act(async () => {
      await capturedOptions.api.fetch({
        input: [{ role: "user", content: "default 确认发送" }],
      });
    });
    expect(latestConsoleChatBody().input[0].content).toEqual([
      { type: "text", text: "default 确认发送" },
      { type: "file", filename: "private.pdf", file_url: "/private.pdf" },
    ]);
  });

  it.each([false, true])(
    "handles late attachment acceptance after remount without deleting a newer draft (%s)",
    async (replaceDraft) => {
      const original = {
        type: "file",
        filename: "old.pdf",
        file_url: "/old.pdf",
      };
      const replacement = {
        type: "file",
        filename: "new.pdf",
        file_url: "/new.pdf",
      };
      persistModelSetupDraftSnapshot("/chat/chat-1", {
        composerText: "旧草稿",
        submittedInput: [{ role: "user", content: [original] }],
      });
      let accept!: (response: Response) => void;
      global.fetch = mockConsoleChatTransport(
        vi
          .fn()
          .mockImplementationOnce(
            () =>
              new Promise<Response>((resolve) => {
                accept = resolve;
              }),
          )
          .mockResolvedValue(new Response("")),
      );
      const first = renderWithProviders(<ChatPage />, {
        initialEntries: ["/chat/chat-1"],
      });
      await screen.findByDisplayValue("旧草稿");
      const sending = capturedOptions.api.fetch({
        input: [{ role: "user", content: "发送旧稿" }],
      });
      await waitFor(() => expect(accept).toBeTypeOf("function"));
      first.unmount();
      if (replaceDraft)
        persistModelSetupDraftSnapshot("/chat/chat-1", {
          composerText: "新草稿",
          submittedInput: [{ role: "user", content: [replacement] }],
        });
      renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
      await screen.findByTestId("chat-ui");
      if (replaceDraft) await screen.findByDisplayValue("新草稿");
      await act(async () => {
        accept(new Response(""));
        await sending;
      });
      await act(async () => {
        await capturedOptions.api.fetch({
          input: [{ role: "user", content: "返回后发送" }],
        });
      });
      expect(latestConsoleChatBody().input[0].content).toEqual(
        replaceDraft
          ? [{ type: "text", text: "返回后发送" }, replacement]
          : "返回后发送",
      );
    },
  );

  // ── customFetch: normal send ──────────────────────────────────────────────

  it("customFetch calls /api/console/chat when model is configured", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "hello" }],
      signal: undefined,
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/console/chat",
      expect.objectContaining({ method: "POST" }),
    );
    const chatFetchCall = (global.fetch as any).mock.calls.find(
      ([url]: [string]) => url === "/api/console/chat",
    );
    const requestBody = JSON.parse(chatFetchCall[1].body);
    expect(requestBody.meta.client_turn_id).toMatch(/^client-turn-/);
    expect(requestBody.permission_mode).toBe("quiet");
    expect(requestBody.permission_profile).toBe("workspace");
  });

  it("submits identical user text as two distinct commands", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const input = [{ role: "user", content: "保持原文，只调整排版" }];
    await capturedOptions.api.fetch({ input, signal: undefined });
    await capturedOptions.api.fetch({ input, signal: undefined });

    const bodies = (global.fetch as any).mock.calls
      .filter(
        ([url, options]: [unknown, { method?: string }]) =>
          String(url).endsWith("/console/chat") && options?.method === "POST",
      )
      .map(([, options]: [unknown, { body: string }]) =>
        JSON.parse(options.body),
      );
    expect(bodies).toHaveLength(2);
    expect(bodies[0].input).toEqual(bodies[1].input);
    expect(bodies[0].meta.client_turn_id).not.toBe(
      bodies[1].meta.client_turn_id,
    );
  });

  it("keeps running control input isolated from new-turn setup and loading", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-control"],
    });
    await screen.findByTestId("chat-ui");
    mockGetActiveModels.mockClear();
    mockEnsureTopicWorkspace.mockClear();
    mockRuntimeSetLoading.mockClear();
    vi.mocked(sessionApi.stageLastUserMessage).mockClear();
    vi.mocked(sessionApi.acceptLastUserMessage).mockClear();

    await capturedOptions.api.fetch({
      input: [
        {
          role: "user",
          content: [{ type: "text", text: "立即改为先核对证据" }],
          session: {
            session_id: "console:active-run",
            user_id: "default",
            channel: "console",
          },
        },
      ],
      biz_params: {
        operation: "steer",
        deep_thinking: { enabled: true, effort: "high" },
        meta: {
          client_turn_id: "control-command-1",
          control_intent: "redirect",
        },
      },
      signal: undefined,
    });

    expect(mockGetActiveModels).not.toHaveBeenCalled();
    expect(mockEnsureTopicWorkspace).not.toHaveBeenCalled();
    expect(mockRuntimeSetLoading).not.toHaveBeenCalled();
    expect(sessionApi.stageLastUserMessage).not.toHaveBeenCalled();
    expect(sessionApi.acceptLastUserMessage).not.toHaveBeenCalled();
    expect(latestConsoleChatBody()).toMatchObject({
      input: [
        {
          role: "user",
          content: [{ type: "text", text: "立即改为先核对证据" }],
        },
      ],
      session_id: "console:active-run",
      chat_id: "chat-control",
      operation: "steer",
      meta: {
        client_turn_id: "control-command-1",
        control_intent: "redirect",
      },
    });
    const requestBody = latestConsoleChatBody();
    expect(requestBody).not.toHaveProperty("deep_thinking");
    expect(requestBody).not.toHaveProperty("permission_mode");
    expect(requestBody).not.toHaveProperty("permission_profile");
    expect(requestBody.meta).not.toHaveProperty("work_method_id");
  });

  it("does not clear a healthy primary run when control admission fails", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      statusText: "Conflict",
      text: vi.fn(async () =>
        JSON.stringify({
          detail: {
            code: "target_ended",
            message: "The current run has reached its terminal boundary.",
          },
        }),
      ),
    } as unknown as Response);
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-control"],
    });
    await screen.findByTestId("chat-ui");
    mockRuntimeSetLoading.mockClear();

    const response = await capturedOptions.api.fetch({
      input: [
        {
          role: "user",
          content: [{ type: "text", text: "调整方向" }],
          session: { session_id: "console:active-run" },
        },
      ],
      biz_params: {
        operation: "steer",
        meta: {
          client_turn_id: "control-command-conflict",
          control_intent: "redirect",
        },
      },
      signal: undefined,
    });

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toMatchObject({
      error: {
        code: "target_ended",
        message: "The current run has reached its terminal boundary.",
      },
    });
    expect(mockRuntimeSetLoading).not.toHaveBeenCalled();
  });

  it("sends the permission preset selected in the composer", async () => {
    const user = userEvent.setup();
    mockModalConfirm.mockImplementationOnce(
      ({ onOk }: { onOk?: () => void }) => {
        onOk?.();
        return { destroy: vi.fn(), update: vi.fn() };
      },
    );
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "权限模式：仅风险操作询问",
        }),
      ).toBeInTheDocument(),
    );

    await user.click(
      screen.getByRole("button", {
        name: "权限模式：仅风险操作询问",
      }),
    );
    await user.click(screen.getByRole("menuitemradio", { name: /完全访问/ }));

    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "hello" }],
      signal: undefined,
    });

    const requestBody = latestConsoleChatBody();
    expect(requestBody.permission_mode).toBe("full_auto");
    expect(requestBody.permission_profile).toBe("full");
  });

  it("applies a permission preset to the active root session immediately", async () => {
    const user = userEvent.setup();
    mockModalConfirm.mockImplementationOnce(
      ({ onOk }: { onOk?: () => void }) => {
        onOk?.();
        return { destroy: vi.fn(), update: vi.fn() };
      },
    );
    (window as any).currentSessionId = "root-session";
    (window as any).currentUserId = "owner-user";
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
    await screen.findByTestId("chat-ui");

    await user.click(
      screen.getByRole("button", {
        name: "权限模式：仅风险操作询问",
      }),
    );
    await user.click(screen.getByRole("menuitemradio", { name: /完全访问/ }));

    await waitFor(() =>
      expect(mockPreviewSessionPermission).toHaveBeenCalledWith({
        sessionId: "root-session",
        userId: "owner-user",
      }),
    );
    await waitFor(() =>
      expect(mockUpdateSessionPermission).toHaveBeenCalledWith({
        sessionId: "root-session",
        userId: "owner-user",
        permissionMode: "full_auto",
        permissionProfile: "full",
        expectedQueueRevision: "queue-v1",
      }),
    );
  });

  it("does not apply full access until the user makes a real confirmation decision", async () => {
    const user = userEvent.setup();
    (window as any).currentSessionId = "root-session";
    (window as any).currentUserId = "owner-user";
    mockPreviewSessionPermission.mockResolvedValueOnce({
      queue_revision: "queue-v2",
      requests: [
        {
          request_id: "approval-1",
          tool_name: "execute_shell_command",
          severity: "high",
        },
      ],
    });
    mockModalConfirm.mockImplementationOnce(
      ({ onCancel }: { onCancel?: () => void }) => {
        onCancel?.();
        return { destroy: vi.fn(), update: vi.fn() };
      },
    );
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
    await screen.findByTestId("chat-ui");

    await user.click(
      screen.getByRole("button", {
        name: "权限模式：仅风险操作询问",
      }),
    );
    await user.click(screen.getByRole("menuitemradio", { name: /完全访问/ }));

    await waitFor(() => expect(mockModalConfirm).toHaveBeenCalledTimes(1));
    expect(mockUpdateSessionPermission).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", {
        name: "权限模式：仅风险操作询问",
      }),
    ).toBeInTheDocument();
  });

  it("keeps a restored full-access preset while the optional capability probe is pending", async () => {
    let resolvePermissionProfiles:
      | ((profiles: Array<{ id: string }>) => void)
      | undefined;
    mockListPermissionProfiles.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePermissionProfiles = resolve;
      }),
    );
    window.sessionStorage.setItem(
      permissionPresetStorageKey("default", "chat-1"),
      "full",
    );
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);

    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-1"],
    });
    await screen.findByTestId("chat-ui");
    await act(async () => {
      await Promise.resolve();
    });

    expect(
      screen.getByRole("button", {
        name: "权限模式：完全访问",
      }),
    ).toBeInTheDocument();
    expect(
      window.sessionStorage.getItem(
        permissionPresetStorageKey("default", "chat-1"),
      ),
    ).toBe("full");

    await act(async () => {
      resolvePermissionProfiles?.([{ id: "workspace" }, { id: "full" }]);
      await Promise.resolve();
    });

    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "first turn" }],
      signal: undefined,
    });
    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "second turn" }],
      signal: undefined,
    });

    const chatBodies = (
      (globalThis.fetch as any).mock.calls as Array<
        [unknown, { body?: string } | undefined]
      >
    )
      .filter(
        ([url, options]) =>
          String(url).endsWith("/console/chat") && options?.body,
      )
      .map(([, options]) => JSON.parse(options?.body || "{}"));
    expect(chatBodies).toHaveLength(2);
    expect(chatBodies).toEqual([
      expect.objectContaining({
        permission_mode: "full_auto",
        permission_profile: "full",
      }),
      expect.objectContaining({
        permission_mode: "full_auto",
        permission_profile: "full",
      }),
    ]);
  });

  it("binds a direct route to its existing backend chat before session hydration", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
    await screen.findByTestId("chat-ui");

    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "hello" }],
      signal: undefined,
    });

    expect(mockEnsureTopicWorkspace).toHaveBeenCalledWith(
      expect.objectContaining({
        chat_id: "chat-1",
      }),
    );
  });

  it("customFetch prefers the submitted session over stale window globals", async () => {
    (window as any).currentSessionId = "console:stale-session";
    (window as any).currentUserId = "stale-user";
    (window as any).currentChannel = "stale-channel";
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    mockEnsureTopicWorkspace.mockImplementationOnce(async (payload: any) => ({
      chat: {
        id: "submitted-chat",
        session_id: payload.session_id,
        user_id: payload.user_id,
        channel: payload.channel,
        name: "submitted chat",
        created_at: null,
        updated_at: null,
        meta: {},
      },
      topic_workspace_dir: "/tmp/lantai/submitted-topic",
      effective_workspace_dir: "/tmp/lantai/submitted-topic",
      manual_workspace_dir: null,
    }));
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await act(() =>
      capturedOptions.api.fetch({
        input: [
          {
            role: "user",
            content: "hello",
            session: {
              session_id: "console:submitted-session",
              user_id: "submitted-user",
              channel: "console",
            },
          },
        ],
        signal: undefined,
      }),
    );

    const chatFetchCall = (global.fetch as any).mock.calls.find(
      ([url]: [string]) => url === "/api/console/chat",
    );
    const requestBody = JSON.parse(chatFetchCall[1].body);
    expect(requestBody.session_id).toBe("console:submitted-session");
    expect(requestBody.user_id).toBe("submitted-user");
    expect(requestBody.channel).toBe("console");
  });

  it("customFetch preserves an existing client_turn_id in request meta", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "hello" }],
      biz_params: {
        meta: {
          client_turn_id: "client-turn-existing",
          source: "test",
        },
      },
      signal: undefined,
    });

    const chatFetchCall = (global.fetch as any).mock.calls.find(
      ([url]: [string]) => url === "/api/console/chat",
    );
    const requestBody = JSON.parse(chatFetchCall[1].body);
    expect(requestBody.meta).toEqual({
      client_turn_id: "client-turn-existing",
      source: "test",
      work_method_id: "standard",
    });
  });

  it("sends directly to the primary model without a work-method preflight", async () => {
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/work-methods") {
        return new Response(JSON.stringify(DEFAULT_WORK_METHODS), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    expect(
      await screen.findByRole("button", { name: "能力菜单" }),
    ).toBeInTheDocument();

    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "比较三份资料后给出判断" }],
      signal: undefined,
    });

    const requestBody = latestConsoleChatBody();
    expect(requestBody.meta.work_method_id).toBe("standard");
    expect(requestBody.meta).not.toHaveProperty("work_method_selection");
    expect(global.fetch).not.toHaveBeenCalledWith(
      expect.stringContaining("/work-methods/auto/"),
      expect.anything(),
    );
  });

  it("migrates a saved automatic choice and does not expose the old mode", async () => {
    const storageKey = "lantai:work-method:v1:default:draft";
    localStorage.setItem(storageKey, "auto");
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/work-methods") {
        return new Response(JSON.stringify(DEFAULT_WORK_METHODS), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    try {
      renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
      expect(
        await screen.findByRole("button", { name: "能力菜单" }),
      ).toBeInTheDocument();

      await userEvent
        .setup()
        .click(screen.getByRole("button", { name: "能力菜单" }));
      await userEvent
        .setup()
        .click(screen.getByRole("menuitem", { name: "工作法：标准" }));
      expect(
        screen.queryByRole("menuitemradio", { name: /自动/ }),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole("menuitemradio", { name: /标准/ }),
      ).toHaveAttribute("aria-checked", "true");
    } finally {
      localStorage.removeItem(storageKey);
    }
  });

  it("customFetch converts backend HTTP errors into failed chat payloads", async () => {
    vi.mocked(sessionApi.acceptLastUserMessage).mockClear();
    vi.mocked(sessionApi.discardPendingUserMessage).mockClear();
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
      text: vi.fn(async () =>
        JSON.stringify({ detail: "backend unavailable" }),
      ),
    } as unknown as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const response = await act(() =>
      capturedOptions.api.fetch({
        input: [{ role: "user", content: "hello" }],
        signal: undefined,
      }),
    );

    expect(response.status).toBe(503);
    const body = await response.clone().json();
    expect(body.object).toBe("response");
    expect(body.status).toBe("failed");
    expect(body.type).toBe("response.failed");
    expect(body.error.message).toBe("backend unavailable");
    expect(sessionApi.acceptLastUserMessage).not.toHaveBeenCalled();
    expect(sessionApi.discardPendingUserMessage).toHaveBeenCalledWith(
      expect.any(Array),
    );
  });

  it("customFetch converts backend text errors into failed chat payloads", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      text: vi.fn(async () => "backend gateway failed"),
    } as unknown as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const response = await act(() =>
      capturedOptions.api.fetch({
        input: [{ role: "user", content: "hello" }],
        signal: undefined,
      }),
    );

    expect(response.status).toBe(502);
    const body = await response.clone().json();
    expect(body.status).toBe("failed");
    expect(body.error.message).toBe("backend gateway failed");
  });

  it("customFetch falls back to status text when an HTTP error body is unreadable", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
      text: vi.fn(async () => {
        throw new Error("body unavailable");
      }),
    } as unknown as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const response = await act(() =>
      capturedOptions.api.fetch({
        input: [{ role: "user", content: "hello" }],
        signal: undefined,
      }),
    );

    expect(response.status).toBe(503);
    const body = await response.clone().json();
    expect(body.status).toBe("failed");
    expect(body.error.message).toBe("Service Unavailable");
  });

  it("resolves a freshly ensured topic workspace before posting the first chat turn", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await capturedOptions.api.fetch({
      input: [
        {
          role: "user",
          type: "message",
          content: [{ type: "text", text: "新会话第一条消息" }],
          session: {
            session_id: "local-session-1",
            user_id: "default",
            channel: "console",
          },
        },
      ],
      signal: undefined,
    });

    expect(sessionApi.resolveEnsuredSession).toHaveBeenCalledWith(
      expect.arrayContaining(["local-session-1"]),
      "chat-1",
      "console:default",
    );
    expect(sessionApi.rememberSessionAlias).toHaveBeenCalledWith(
      "local-session-1",
      "chat-1",
      "console:default",
      undefined,
    );
    expect(sessionApi.rememberSessionAlias).toHaveBeenCalledWith(
      "console:default",
      "chat-1",
      "console:default",
      undefined,
    );
    expect(sessionApi.stageLastUserMessage).toHaveBeenCalledWith(
      expect.arrayContaining(["chat-1", "console:default", "local-session-1"]),
      "新会话第一条消息",
    );
    expect(sessionApi.acceptLastUserMessage).toHaveBeenCalledWith(
      expect.arrayContaining(["chat-1", "console:default", "local-session-1"]),
      "新会话第一条消息",
      expect.any(String),
    );

    const body = latestConsoleChatBody();
    expect(body.session_id).toBe("console:default");
    expect(body.input[0].content).toEqual([
      { type: "text", text: "新会话第一条消息" },
    ]);
  });

  it("cancels a running chat with the resolved backend chat id", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");
    (sessionApi.getRealIdForSession as any).mockReturnValueOnce("chat-real-1");

    capturedOptions.api.cancel({ session_id: "local-session-1" });

    await waitFor(() =>
      expect(mockStopChat).toHaveBeenCalledWith("chat-real-1"),
    );
  });

  it("cancels the current direct route when the chat UI reports a stale local session", async () => {
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-route-1"],
    });
    await screen.findByTestId("chat-ui");

    capturedOptions.api.cancel({ session_id: "console:stale-local-session" });

    await waitFor(() =>
      expect(mockStopChat).toHaveBeenCalledWith("chat-route-1"),
    );
  });

  it("does not send another chat's turn id as a stop fence", async () => {
    mockGetChatEvents.mockResolvedValue({
      chat_id: "chat-other",
      after_seq: 0,
      latest_seq: 1,
      events: [
        {
          id: "event-other",
          turn_id: "turn-other",
          type: "turn.started",
          status: "running",
          source: "assistant",
          payload: {},
          created_at: "2026-07-24T00:00:00Z",
        },
      ],
    });
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-current"],
    });
    await screen.findByTestId("chat-ui");
    await waitFor(() => expect(mockGetChatEvents).toHaveBeenCalled());

    capturedOptions.api.cancel({ session_id: "console:stale-local-session" });

    await waitFor(() =>
      expect(mockStopChat).toHaveBeenCalledWith("chat-current"),
    );
  });

  it("stops the active chat through its authoritative runtime run", async () => {
    mockGetChatEvents.mockResolvedValue({
      chat_id: "chat-current",
      after_seq: 0,
      latest_seq: 1,
      events: [
        {
          id: "event-current",
          turn_id: "turn-current",
          type: "turn.started",
          status: "running",
          source: "assistant",
          payload: {},
          created_at: "2026-07-24T00:00:00Z",
        },
      ],
    });
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-current"],
    });
    await screen.findByTestId("chat-ui");
    await waitFor(() => expect(mockGetChatEvents).toHaveBeenCalled());

    mockStopChat.mockResolvedValue({ stopped: false });
    const result = await capturedOptions.api.cancel({
      session_id: "console:stale-local-session",
    });

    expect(mockInterruptRun).toHaveBeenCalledWith("turn-current");
    expect(mockStopChat).not.toHaveBeenCalled();
    expect(result).toMatchObject({ stopped: true });
  });

  it("stops the foreground root run when the latest event belongs to a child", async () => {
    mockGetChatEvents.mockResolvedValue({
      chat_id: "chat-current",
      after_seq: 0,
      latest_seq: 2,
      events: [
        {
          id: "event-root",
          turn_id: "run-root",
          type: "turn.started",
          status: "running",
          source: "assistant",
          payload: {},
          created_at: "2026-07-24T00:00:00Z",
        },
        {
          id: "event-child",
          turn_id: "run-child",
          type: "tool.call.started",
          status: "running",
          source: "tool",
          payload: { parent_run_id: "run-root" },
          created_at: "2026-07-24T00:00:01Z",
        },
      ],
    });
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-current"],
    });
    await screen.findByTestId("chat-ui");
    await waitFor(() => expect(mockGetChatEvents).toHaveBeenCalled());

    await capturedOptions.api.cancel({
      session_id: "console:stale-local-session",
    });

    expect(mockInterruptRun).toHaveBeenCalledWith("run-root");
    expect(mockInterruptRun).not.toHaveBeenCalledWith("run-child");
    expect(mockStopChat).not.toHaveBeenCalled();
  });

  it("reports success when stopping the desktop takeover is the only active run", async () => {
    const stopMacUse = vi.fn().mockResolvedValue({
      ok: true,
      data: { session: { was_active: true } },
    });
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      stopMacUse,
    };
    mockStopChat.mockResolvedValue({ stopped: false });
    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat/chat-current"],
    });
    await screen.findByTestId("chat-ui");

    const result = await capturedOptions.api.cancel({
      session_id: "console:stale-local-session",
    });

    expect(stopMacUse).toHaveBeenCalledWith("chat-current");
    expect(result).toMatchObject({ stopped: true });
  });

  it("reconnect posts its submitted identity instead of stale window globals", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    (window as any).currentSessionId = "console:current-user";
    (window as any).currentUserId = "current-user";
    (window as any).currentChannel = "console";

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await capturedOptions.api.reconnect({
      session_id: "fallback-session",
      user_id: "submitted-user",
      channel: "submitted-channel",
      signal: undefined,
    });

    const body = latestConsoleChatBody();
    expect(body).toMatchObject({
      reconnect: true,
      session_id: "fallback-session",
      user_id: "submitted-user",
      channel: "submitted-channel",
    });
  });

  it("shows an aliased ToolGuard approval and submits its recorded root session", async () => {
    const user = userEvent.setup();
    (window as any).currentSessionId = "chat-1";
    (window as any).currentUserId = "current-user";
    (sessionApi.getRealIdForSession as any).mockImplementation(
      (value: string) => (value === "console:root-session-1" ? "chat-1" : null),
    );
    mockApprovals.push({
      request_id: "approval-1",
      session_id: "sub-session-1",
      root_session_id: "console:root-session-1",
      user_id: "approval-owner",
      agent_id: "default",
      tool_name: "shell",
      severity: "high",
      findings_count: 1,
      findings_summary: "需要确认命令执行",
      tool_params: { command: "echo ok" },
      created_at: Math.floor(Date.now() / 1000),
      timeout_seconds: 60,
      policy_decision: { matched_rule_id: "shell-command" },
      approval_options: ["allow_once", "allow_session"],
      rememberable: true,
      permission_scope: "session",
      permission_group_key: null,
      source_run_id: "run-1",
      source_agent_name: "default",
    });

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });

    await screen.findByRole("button", { name: /允许一次/ });
    await user.click(screen.getByRole("button", { name: /允许一次/ }));

    await waitFor(() =>
      expect(mockSendApprovalCommand).toHaveBeenCalledWith(
        "approve",
        "approval-1",
        "console:root-session-1",
        undefined,
        "once",
        "approval-owner",
      ),
    );
  });

  it.each([
    {
      action: "approve" as const,
      buttonName: /允许一次/,
      expectedMessage: /审批结果尚未确认.*不要重复提交/,
    },
    {
      action: "deny" as const,
      buttonName: /Deny/,
      expectedMessage: /拒绝结果尚未确认.*不要重复提交/,
    },
  ])(
    "keeps an approval visible when the $action result is unknown",
    async ({ action, buttonName, expectedMessage }) => {
      const user = userEvent.setup();
      (window as any).currentSessionId = "root-session";
      (window as any).currentUserId = "owner-user";
      mockApprovals.push({
        request_id: "approval-unknown",
        session_id: "root-session",
        root_session_id: "root-session",
        user_id: "owner-user",
        agent_id: "default",
        tool_name: "execute_shell_command",
        severity: "medium",
        findings_count: 1,
        findings_summary: "需要确认命令执行",
        tool_params: { command: "npm install" },
        created_at: Math.floor(Date.now() / 1000),
        timeout_seconds: null,
        policy_decision: { matched_rule_id: "mode.default" },
        approval_options: ["allow_once", "deny"],
        session_rememberable: false,
        rememberable: false,
        permission_scope: "once",
        permission_group_key: null,
      });
      mockSendApprovalCommand.mockRejectedValueOnce(
        new TypeError("fetch failed after submission"),
      );

      renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
      await user.click(await screen.findByRole("button", { name: buttonName }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        expectedMessage,
      );
      expect(
        screen.getByRole("button", { name: /允许一次/ }),
      ).toBeInTheDocument();
      expect(mockSendApprovalCommand).toHaveBeenCalledWith(
        action,
        "approval-unknown",
        "root-session",
        undefined,
        action === "approve" ? "once" : undefined,
        "owner-user",
      );
    },
  );

  it("submits one session approval and lets the backend drain the group", async () => {
    const user = userEvent.setup();
    (window as any).currentSessionId = "root-session";
    (window as any).currentUserId = "owner-user";
    for (const requestId of ["approval-1", "approval-2"]) {
      mockApprovals.push({
        request_id: requestId,
        session_id: "root-session",
        root_session_id: "root-session",
        user_id: "owner-user",
        agent_id: "default",
        tool_name: "execute_shell_command",
        severity: "medium",
        findings_count: 1,
        findings_summary: "需要确认命令执行",
        tool_params: { command: "npm install" },
        created_at: Math.floor(Date.now() / 1000),
        timeout_seconds: null,
        policy_decision: { matched_rule_id: "mode.default" },
        approval_options: ["allow_once", "allow_session"],
        session_rememberable: true,
        rememberable: true,
        permission_scope: "session",
        permission_group_key: "same-group",
      });
    }

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
    await user.click(
      await screen.findByRole("button", { name: /本次会话允许/ }),
    );

    await waitFor(() =>
      expect(mockSendApprovalCommand).toHaveBeenCalledTimes(1),
    );
    expect(mockSendApprovalCommand).toHaveBeenCalledWith(
      "approve",
      "approval-1",
      "root-session",
      undefined,
      "session",
      "owner-user",
    );
  });

  it("queries a control input with separate transport and owner identities", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({
        command_id: "command-1",
        execution_status: "applied",
      }),
    } as unknown as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await expect(
      capturedOptions.api.getControlInputStatus({
        session_id: "console:shared-transport",
        chat_id: "chat-owner-1",
        command_id: "command-1",
      }),
    ).resolves.toEqual({
      command_id: "command-1",
      execution_status: "applied",
    });
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/console/chat/control-input?session_id=console%3Ashared-transport&command_id=command-1&chat_id=chat-owner-1",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("cancels a control input through the dedicated endpoint", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({
        command_id: "command-1",
        status: "cancelled",
      }),
    } as unknown as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await expect(
      capturedOptions.api.cancelControlInput({
        session_id: "console:shared-transport",
        chat_id: "chat-owner-1",
        command_id: "command-1",
      }),
    ).resolves.toEqual({
      command_id: "command-1",
      status: "cancelled",
    });
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/console/chat/control-input/cancel?session_id=console%3Ashared-transport&command_id=command-1&chat_id=chat-owner-1",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("preserves the server disposition when cancellation loses the claim race", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: vi.fn().mockResolvedValue({
        detail: {
          error: "control_input_not_cancellable",
          execution_status: "bound",
        },
      }),
    } as unknown as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await expect(
      capturedOptions.api.cancelControlInput({
        session_id: "session-1",
        command_id: "command-1",
      }),
    ).rejects.toMatchObject({
      message: "HTTP 409",
      detail: {
        error: "control_input_not_cancellable",
        execution_status: "bound",
      },
    });
  });

  it("customFetch strips the invisible marker from attachment-only messages", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const cases = [
      {
        type: "image",
        image_url: "http://localhost/files/preview/uploaded.png",
      },
      {
        type: "file",
        filename: "doc.pdf",
        file_url: "http://localhost/files/preview/doc.pdf",
      },
      {
        type: "video",
        video_url: "http://localhost/files/preview/clip.mp4",
      },
      {
        type: "audio",
        data: "http://localhost/files/preview/voice.m4a",
      },
    ];

    for (const part of cases) {
      await capturedOptions.api.fetch({
        input: [
          {
            role: "user",
            content: [{ type: "text", text: "\u200B" }, part],
          },
        ],
        signal: undefined,
      });
    }

    const calls = ((globalThis.fetch as any)?.mock?.calls || []) as Array<
      [unknown, { body?: string } | undefined]
    >;
    const bodies = calls
      .filter(
        ([url, options]) =>
          String(url).endsWith("/console/chat") && Boolean(options?.body),
      )
      .slice(-cases.length)
      .map(([, options]) => JSON.parse(options?.body || "{}"));

    expect(bodies.map((body) => body.input[0].content)).toEqual([
      [{ type: "image", image_url: "/uploaded.png" }],
      [{ type: "file", filename: "doc.pdf", file_url: "/doc.pdf" }],
      [{ type: "video", video_url: "/clip.mp4" }],
      [{ type: "audio", data: "/voice.m4a" }],
    ]);
  });

  it("defaults deep thinking on for a supported active model", async () => {
    mockGetReasoningCapability.mockResolvedValue({
      supported: true,
      mode: "openai_reasoning_effort",
      fixed: false,
      default_enabled: false,
      note: "",
    });
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await waitFor(() =>
      expect(
        screen.getByLabelText("chat.deepThinking.label"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByLabelText("chat.deepThinking.label")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(capturedOptions.api.getRequestBizParams()).toEqual({
      deep_thinking: { enabled: true, effort: "high" },
    });

    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "hard problem" }],
      signal: undefined,
    });

    const body = latestConsoleChatBody();
    expect(body.deep_thinking).toEqual({ enabled: true, effort: "high" });
  });

  it("keeps an unavailable deep-thinking entry visible without enabling it", async () => {
    mockGetReasoningCapability.mockResolvedValue({
      supported: false,
      mode: "none",
      fixed: false,
      default_enabled: false,
      note: "",
    });

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const deepThinkingButton = await screen.findByLabelText(
      "chat.deepThinking.label",
    );
    await waitFor(() => expect(deepThinkingButton).toBeDisabled());
    expect(deepThinkingButton).toHaveAttribute("aria-pressed", "false");
    expect(capturedOptions.api.getRequestBizParams()).toEqual({});
  });

  it("keeps a pre-capability submit closure aligned with the committed visible deep-thinking state", async () => {
    let finishCapability: ((value: any) => void) | undefined;
    mockGetReasoningCapability.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishCapability = resolve;
        }),
    );
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    const fetchCapturedBeforeCapability = capturedOptions.api.fetch;
    const getterCapturedBeforeCapability =
      capturedOptions.api.getRequestBizParams;

    await waitFor(() =>
      expect(mockGetReasoningCapability).toHaveBeenCalledTimes(1),
    );

    act(() => {
      finishCapability?.({
        supported: true,
        mode: "openai_reasoning_effort",
        fixed: false,
        default_enabled: false,
        note: "",
      });
    });

    const deepThinkingButton = await screen.findByLabelText(
      "chat.deepThinking.label",
    );
    await waitFor(() =>
      expect(deepThinkingButton).toHaveAttribute("aria-pressed", "true"),
    );

    expect(getterCapturedBeforeCapability()).toEqual({
      deep_thinking: { enabled: true, effort: "high" },
    });
    await fetchCapturedBeforeCapability({
      input: [{ role: "user", content: "submit from an older render" }],
      signal: undefined,
    });
    expect(latestConsoleChatBody().deep_thinking).toEqual({
      enabled: true,
      effort: "high",
    });
  });

  it("keeps the visible deep-thinking choice during a same-model capability refresh", async () => {
    mockGetReasoningCapability.mockResolvedValue({
      supported: true,
      mode: "openai_reasoning_effort",
      fixed: false,
      default_enabled: false,
      note: "",
    });
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);

    renderWithProviders(
      <>
        <ChatPage />
        <SameModelRouteRefreshButton />
      </>,
      { initialEntries: ["/chat"] },
    );
    await screen.findByTestId("chat-ui");

    const deepThinkingButton = await screen.findByLabelText(
      "chat.deepThinking.label",
    );
    await waitFor(() =>
      expect(deepThinkingButton).toHaveAttribute("aria-pressed", "true"),
    );
    const callsBeforeRefresh = mockGetReasoningCapability.mock.calls.length;

    let finishRefresh: ((value: any) => void) | undefined;
    mockGetReasoningCapability.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishRefresh = resolve;
        }),
    );
    fireEvent.click(screen.getByText("refresh same model route"));
    await waitFor(() =>
      expect(mockGetReasoningCapability.mock.calls.length).toBeGreaterThan(
        callsBeforeRefresh,
      ),
    );

    // The current model and the user-visible choice have not changed while
    // the refresh is in flight.  The request must not silently drop that
    // choice and fall back to the provider's server-side default.
    expect(deepThinkingButton).toHaveAttribute("aria-pressed", "true");
    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "hard problem during refresh" }],
      signal: undefined,
    });

    expect(latestConsoleChatBody().deep_thinking).toEqual({
      enabled: true,
      effort: "high",
    });
    finishRefresh?.({
      supported: true,
      mode: "openai_reasoning_effort",
      fixed: false,
      default_enabled: false,
      note: "",
    });
  });

  it("keeps the visible deep-thinking choice aligned with transport while a model switch resolves", async () => {
    mockGetReasoningCapability.mockResolvedValue({
      supported: true,
      mode: "openai_reasoning_effort",
      fixed: false,
      default_enabled: false,
      note: "",
    });
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);

    renderWithProviders(<ChatPage />, {
      initialEntries: ["/chat"],
    });
    await screen.findByTestId("chat-ui");
    const deepThinkingButton = await screen.findByLabelText(
      "chat.deepThinking.label",
    );
    await waitFor(() =>
      expect(deepThinkingButton).toHaveAttribute("aria-pressed", "true"),
    );
    const callsBeforeSwitch = mockGetReasoningCapability.mock.calls.length;

    let finishSwitch: ((value: any) => void) | undefined;
    mockGetReasoningCapability.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishSwitch = resolve;
        }),
    );
    act(() => {
      window.dispatchEvent(new CustomEvent("model-switched"));
    });
    await waitFor(() =>
      expect(mockGetReasoningCapability.mock.calls.length).toBeGreaterThan(
        callsBeforeSwitch,
      ),
    );

    expect(deepThinkingButton).toHaveAttribute("aria-pressed", "true");
    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "first message during model switch" }],
      signal: undefined,
    });
    expect(latestConsoleChatBody().deep_thinking).toEqual({
      enabled: true,
      effort: "high",
    });

    finishSwitch?.({
      supported: true,
      mode: "openai_reasoning_effort",
      fixed: false,
      default_enabled: false,
      note: "",
    });
  });

  it("keeps a manual choice for the same model but enables thinking after switching models", async () => {
    mockGetReasoningCapability.mockResolvedValue({
      supported: true,
      mode: "openai_reasoning_effort",
      fixed: false,
      default_enabled: false,
      note: "",
    });

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const deepThinkingButton = await screen.findByLabelText(
      "chat.deepThinking.label",
    );
    await waitFor(() =>
      expect(deepThinkingButton).toHaveAttribute("aria-pressed", "true"),
    );

    fireEvent.click(deepThinkingButton);
    expect(deepThinkingButton).toHaveAttribute("aria-pressed", "false");
    expect(capturedOptions.api.getRequestBizParams()).toEqual({
      deep_thinking: { enabled: false, effort: "high" },
    });
    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "manual thinking choice" }],
      biz_params: capturedOptions.api.getRequestBizParams(),
      signal: undefined,
    });
    expect(latestConsoleChatBody().deep_thinking).toEqual({
      enabled: false,
      effort: "high",
    });

    act(() => {
      window.dispatchEvent(new CustomEvent("model-switched"));
    });
    await waitFor(() =>
      expect(mockGetReasoningCapability).toHaveBeenLastCalledWith(
        "openai",
        "gpt-4",
      ),
    );
    expect(deepThinkingButton).toHaveAttribute("aria-pressed", "false");

    currentMockActiveModel = {
      active_llm: { provider_id: "openai", model: "gpt-5" },
    };
    act(() => {
      window.dispatchEvent(new CustomEvent("model-switched"));
    });

    await waitFor(() =>
      expect(mockGetReasoningCapability).toHaveBeenLastCalledWith(
        "openai",
        "gpt-5",
      ),
    );
    await waitFor(() =>
      expect(deepThinkingButton).toHaveAttribute("aria-pressed", "true"),
    );
  });

  it("places the unified capability menu before permission and deep thinking", async () => {
    mockGetReasoningCapability.mockResolvedValue({
      supported: true,
      mode: "openai_reasoning_effort",
      fixed: false,
      default_enabled: false,
      note: "",
    });

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const deepThinkingButton = await screen.findByLabelText(
      "chat.deepThinking.label",
    );
    const capabilityButton = screen.getByRole("button", { name: "能力菜单" });
    const permissionButton = screen.getByRole("button", {
      name: /权限模式/,
    });

    expect(
      capabilityButton.compareDocumentPosition(permissionButton) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(
      permissionButton.compareDocumentPosition(deepThinkingButton) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it("turns goal mode into a tracked sustained-execution turn", async () => {
    const user = userEvent.setup();
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/plan/config")) {
        return new Response(JSON.stringify({ enabled: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.endsWith("/work-methods")) {
        return new Response(JSON.stringify(DEFAULT_WORK_METHODS), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await user.click(screen.getByRole("button", { name: "能力菜单" }));
    await user.click(
      screen.getByRole("menuitemcheckbox", { name: /目标模式/ }),
    );

    const input = screen.getByRole("textbox", { name: "chat input" });
    fireEvent.change(input, { target: { value: "把这次重构推进到验收" } });
    await act(async () => {
      await capturedOptions.sender.beforeSubmit();
    });

    expect(input).toHaveValue("/plan 把这次重构推进到验收");
    await capturedOptions.api.fetch({
      input: [{ role: "user", content: (input as HTMLTextAreaElement).value }],
      signal: undefined,
    });
    expect(latestConsoleChatBody().meta.work_method_id).toBe(
      "sustained_execution",
    );
  });

  it("does not force deep thinking off before capability discovery finishes", async () => {
    mockGetReasoningCapability.mockImplementation(
      () => new Promise<never>(() => undefined),
    );
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200 } as Response);
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    await capturedOptions.api.fetch({
      input: [{ role: "user", content: "first message" }],
      signal: undefined,
    });

    expect(capturedOptions.api.getRequestBizParams()).toEqual({});
    expect(latestConsoleChatBody()).not.toHaveProperty("deep_thinking");
  });

  // ── handleFileUpload ──────────────────────────────────────────────────────

  it("enables multiple attachments for file selection and drops", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    expect(capturedOptions.sender.attachments.multiple).toBe(true);
  });

  it("calls onError and skips upload when file exceeds 150MB", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const bigFile = new File(["x"], "big.bin", {
      type: "application/octet-stream",
    });
    Object.defineProperty(bigFile, "size", {
      value: 150 * 1024 * 1024 + 1,
    });
    const onError = vi.fn();
    const onSuccess = vi.fn();

    await capturedOptions.sender.attachments.customRequest({
      file: bigFile,
      onSuccess,
      onError,
    });

    expect(onError).toHaveBeenCalledOnce();
    expect(mockUploadFile).not.toHaveBeenCalled();
  });

  it("uploads successfully and calls onSuccess when file is within size limit", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const smallFile = new File(["content"], "img.png", { type: "image/png" });
    const onSuccess = vi.fn();
    const onError = vi.fn();
    const onProgress = vi.fn();

    await capturedOptions.sender.attachments.customRequest({
      file: smallFile,
      onSuccess,
      onError,
      onProgress,
    });

    expect(mockUploadFile).toHaveBeenCalledWith(
      smallFile,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(onSuccess).toHaveBeenCalledWith({
      url: "/preview//Users/example/.lantai/media/55aa55aa55aa55aa55aa55aa55aa55aa_uploaded.png",
      canonicalPath:
        "/Users/example/.lantai/media/55aa55aa55aa55aa55aa55aa55aa55aa_uploaded.png",
      storedName: "55aa55aa55aa55aa55aa55aa55aa55aa_uploaded.png",
      digest: "sha256:55aa55aa55aa55aa55aa55aa55aa55aa",
      immutableVersion: 1,
      uploadReceipt: "upload-receipt:v1:chat-page",
    });
    expect(onError).not.toHaveBeenCalled();
    expect(onProgress.mock.calls).toEqual([[{ percent: 100 }]]);
    expect(screen.getByLabelText("chat input")).toHaveValue("\u200B");
  });

  it("accepts a file exactly at the 150MB attachment limit", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");

    const boundaryFile = new File(["x"], "boundary.bin", {
      type: "application/octet-stream",
    });
    Object.defineProperty(boundaryFile, "size", {
      value: 150 * 1024 * 1024,
    });
    const onSuccess = vi.fn();
    const onError = vi.fn();

    await capturedOptions.sender.attachments.customRequest({
      file: boundaryFile,
      onSuccess,
      onError,
    });

    expect(mockUploadFile).toHaveBeenCalledWith(
      boundaryFile,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(onError).not.toHaveBeenCalled();
  });

  it("dispatches dropped files to the chat upload input", async () => {
    const originalDataTransfer = globalThis.DataTransfer;
    class TestDataTransfer {
      files: File[] = [];
      items = {
        add: (file: File) => {
          this.files.push(file);
        },
      };
    }
    Object.defineProperty(globalThis, "DataTransfer", {
      value: TestDataTransfer,
      configurable: true,
    });

    try {
      renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
      await screen.findByTestId("chat-ui");

      const dropped = new File(["content"], "dropped.png", {
        type: "image/png",
      });
      fireEvent.drop(screen.getByTestId("chat-stage"), {
        dataTransfer: {
          types: ["Files"],
          files: [dropped],
        },
      });

      expect(capturedDroppedFiles).toEqual([dropped]);
    } finally {
      Object.defineProperty(globalThis, "DataTransfer", {
        value: originalDataTransfer,
        configurable: true,
      });
    }
  });

  it("adds a DOCX dropped from Finder to the chat attachments", async () => {
    const originalDataTransfer = globalThis.DataTransfer;
    class TestDataTransfer {
      files: File[] = [];
      items = {
        add: (file: File) => {
          this.files.push(file);
        },
      };
    }
    Object.defineProperty(globalThis, "DataTransfer", {
      value: TestDataTransfer,
      configurable: true,
    });
    const openDocxEditor = vi.fn().mockResolvedValue({ success: true });
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      getPathForFile: vi.fn(() => "/tmp/from-finder.docx"),
      openDocxEditor,
    };
    try {
      renderWithProviders(<ChatPage />, { initialEntries: ["/chat/chat-1"] });
      await screen.findByTestId("chat-ui");

      const dropped = new File(["docx"], "from-finder.docx", {
        type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      });
      fireEvent.drop(screen.getByTestId("chat-stage"), {
        dataTransfer: { types: ["Files"], files: [dropped] },
      });

      expect(capturedDroppedFiles).toEqual([dropped]);
      expect(openDocxEditor).not.toHaveBeenCalled();
    } finally {
      Object.defineProperty(globalThis, "DataTransfer", {
        value: originalDataTransfer,
        configurable: true,
      });
    }
  });

  // ── multimodal caps ───────────────────────────────────────────────────────

  it("calls providerApi on mount to fetch multimodal capabilities", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");
    await waitFor(() => expect(mockGetActiveModels).toHaveBeenCalled());
    expect(mockListProviders).toHaveBeenCalled();
  });

  it("model-switched event triggers re-fetch of multimodal capabilities", async () => {
    renderWithProviders(<ChatPage />, { initialEntries: ["/chat"] });
    await screen.findByTestId("chat-ui");
    // wait for initial mount calls to settle
    await waitFor(() => expect(mockGetActiveModels).toHaveBeenCalled());
    const callsBefore = mockGetActiveModels.mock.calls.length;

    act(() => {
      window.dispatchEvent(new CustomEvent("model-switched"));
    });

    await waitFor(() =>
      expect(mockGetActiveModels.mock.calls.length).toBeGreaterThan(
        callsBefore,
      ),
    );
  });
});
