import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RuntimeEventStreamBridge from "./RuntimeEventStreamBridge";
import { resetRuntimeEventSubscriptionRegistryForTests } from "../runtimeEventSubscriptionRegistry";
import {
  collaborationSnapshotForCall,
  resetCollaborationEventStoreForTests,
} from "../collaborationEventStore";

const {
  mockSubscribeChatEvents,
  mockGetChatEvents,
  mockGetSession,
  mockUpdateSession,
  mockSetSessions,
  mockUseChatAnywhereSessionsState,
  mockGetRealIdForSession,
  mockChooseActiveSessionMessages,
} = vi.hoisted(() => ({
  mockSubscribeChatEvents: vi.fn(),
  mockGetChatEvents: vi.fn(),
  mockGetSession: vi.fn(),
  mockUpdateSession: vi.fn(),
  mockSetSessions: vi.fn(),
  mockUseChatAnywhereSessionsState: vi.fn(),
  mockGetRealIdForSession: vi.fn(),
  mockChooseActiveSessionMessages: vi.fn(),
}));

vi.mock("../../../api/modules/chat", () => ({
  chatApi: {
    getChatEvents: mockGetChatEvents,
    subscribeChatEvents: mockSubscribeChatEvents,
  },
}));

vi.mock("../sessionApi", () => ({
  default: {
    getSession: mockGetSession,
    updateSession: mockUpdateSession,
    getRealIdForSession: mockGetRealIdForSession,
  },
}));

vi.mock("@/vendor/lantaiChat", () => ({
  useChatAnywhereSessionsState: mockUseChatAnywhereSessionsState,
  chooseActiveSessionMessages: mockChooseActiveSessionMessages,
}));

const baseSession = () => ({
  id: "chat-1",
  realId: "backend-chat-1",
  generating: true,
  status: "running",
  runtimeState: {
    status: "running",
    stream_epoch: "epoch-a",
    events_preview: [{ id: "old", seq: 4 }],
  },
  messages: [{ id: "user-1", role: "user", cards: [] }],
});

function messageEventResponse() {
  return {
    chat_id: "backend-chat-1",
    workspace_dir: "/tmp/topic-workspace",
    after_seq: 4,
    latest_seq: 5,
    events: [
      {
        id: "evt-answer",
        session_id: "console:chat-1",
        turn_id: "run-1",
        item_id: "answer-1",
        seq: 5,
        type: "message.delta",
        status: "running",
        source: "assistant",
        presentation: null,
        payload: {
          legacy_kind: "chat.message.delta",
          delta: "answer must stay in direct SSE",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            content: [{ type: "text", text: "answer must stay in direct SSE" }],
          },
        },
        usage: null,
        created_at: "2026-07-26T12:00:00Z",
      },
    ],
  };
}

function turnStartedResponse() {
  return {
    chat_id: "backend-chat-1",
    workspace_dir: "/tmp/topic-workspace",
    after_seq: 4,
    latest_seq: 5,
    events: [
      {
        id: "evt-turn-started",
        session_id: "console:chat-1",
        turn_id: "run-1",
        item_id: null,
        seq: 5,
        type: "turn.started",
        status: "running",
        source: "runtime",
        presentation: null,
        payload: { legacy_kind: "chat.turn.started" },
        usage: null,
        created_at: "2026-07-26T12:00:00Z",
      },
    ],
  };
}

function toolStartedResponse() {
  return {
    chat_id: "backend-chat-1",
    workspace_dir: "/tmp/topic-workspace",
    after_seq: 4,
    latest_seq: 5,
    events: [
      {
        id: "evt-tool-started",
        session_id: "console:chat-1",
        turn_id: "run-1",
        item_id: "call-1",
        seq: 5,
        type: "tool.call.started",
        status: "running",
        source: "tool",
        presentation: null,
        payload: {
          legacy_kind: "chat.tool_call.started",
          tool_name: "read_file",
          arguments: { path: "/tmp/topic-workspace/source.md" },
        },
        usage: null,
        created_at: "2026-07-26T12:00:00Z",
      },
    ],
  };
}

function terminalEventResponse() {
  return {
    chat_id: "backend-chat-1",
    workspace_dir: "/tmp/topic-workspace",
    after_seq: 5,
    latest_seq: 6,
    stream_epoch: "epoch-a",
    events: [
      {
        id: "evt-terminal",
        session_id: "console:chat-1",
        turn_id: "run-1",
        item_id: null,
        seq: 6,
        type: "turn.completed",
        status: "completed",
        source: "runtime",
        presentation: null,
        payload: { final_task_status: "completed" },
        usage: null,
        created_at: "2026-07-26T12:00:01Z",
      },
    ],
  };
}

function completedMessageTurnResponse() {
  return {
    chat_id: "backend-chat-1",
    workspace_dir: "/tmp/topic-workspace",
    after_seq: 4,
    latest_seq: 6,
    stream_epoch: "epoch-a",
    events: [
      {
        id: "evt-answer-completed",
        session_id: "console:chat-1",
        turn_id: "run-1",
        item_id: "answer-1",
        seq: 5,
        type: "message.completed",
        status: "completed",
        source: "assistant",
        presentation: null,
        payload: {
          legacy_kind: "chat.message.completed",
          client_turn_id: "client-turn-1",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            metadata: { client_turn_id: "client-turn-1" },
            content: [{ type: "text", text: "completed answer" }],
          },
        },
        usage: null,
        created_at: "2026-07-26T12:00:00Z",
      },
      {
        ...terminalEventResponse().events[0],
        seq: 6,
        payload: {
          final_task_status: "completed",
          client_turn_id: "client-turn-1",
        },
      },
    ],
  };
}

describe("RuntimeEventStreamBridge", () => {
  beforeEach(() => {
    vi.useRealTimers();
    resetRuntimeEventSubscriptionRegistryForTests();
    resetCollaborationEventStoreForTests();
    vi.clearAllMocks();
    mockGetRealIdForSession.mockReturnValue(null);
    mockChooseActiveSessionMessages.mockImplementation(
      (current, incoming) => incoming ?? current,
    );
    mockGetSession.mockResolvedValue({
      ...baseSession(),
      generating: false,
      status: "idle",
      messages: [
        { id: "user-1", role: "user", cards: [] },
        { id: "assistant-1", role: "assistant", cards: [] },
      ],
    });
    mockUpdateSession.mockResolvedValue([{ id: "chat-1" }]);
    mockSubscribeChatEvents.mockReturnValue(vi.fn());
    mockGetChatEvents.mockResolvedValue({
      chat_id: "backend-chat-1",
      after_seq: 0,
      latest_seq: 0,
      events: [],
    });
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [baseSession()],
    });
  });

  it("subscribes after the latest control-plane preview sequence", () => {
    render(<RuntimeEventStreamBridge refreshKey={1} />);

    expect(mockSubscribeChatEvents).toHaveBeenCalledWith(
      "backend-chat-1",
      expect.any(Function),
      expect.objectContaining({ afterSeq: 4, streamEpoch: "epoch-a" }),
    );
  });

  it("hydrates terminal collaboration snapshots before tailing from a saved cursor", async () => {
    mockGetChatEvents.mockResolvedValue({
      chat_id: "backend-chat-1",
      after_seq: 0,
      latest_seq: 9,
      events: [
        {
          id: "collaboration-terminal",
          seq: 9,
          type: "tool.call.completed",
          status: "completed",
          source: "tool",
          item_id: "call-batch",
          created_at: "2026-07-29T00:00:00Z",
          payload: {
            collaboration: {
              batch_id: "batch-1",
              revision: 9,
              status: "partial",
              members: [
                {
                  member_id: "member-1",
                  agent_id: "Lantai_Cola_Agent",
                  display_name: "可乐",
                  role: "文案",
                  label: "Copy",
                  status: "cancelled",
                  attempt: 1,
                  duration_ms: 1200,
                },
              ],
            },
          },
        },
      ],
    });

    render(<RuntimeEventStreamBridge refreshKey={1} />);

    await waitFor(() =>
      expect(collaborationSnapshotForCall("call-batch")).toMatchObject({
        revision: 9,
        status: "partial",
        members: [expect.objectContaining({ status: "cancelled" })],
      }),
    );
    expect(mockGetChatEvents).toHaveBeenCalledWith("backend-chat-1", 0, {
      eventTypes: [
        "tool.call.started",
        "tool.call.delta",
        "tool.call.completed",
      ],
    });
  });

  it("uses the route chat id with a fresh cursor when no session has resolved", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: null,
      setSessions: mockSetSessions,
      sessions: [],
    });

    render(<RuntimeEventStreamBridge chatId="route-chat-id" />);

    expect(mockSubscribeChatEvents).toHaveBeenCalledWith(
      "route-chat-id",
      expect.any(Function),
      expect.objectContaining({ afterSeq: 0 }),
    );
  });

  it("does not reconcile an idle conversation on a permanent timer", async () => {
    vi.useFakeTimers();
    const session = {
      ...baseSession(),
      generating: false,
      status: "idle",
      runtimeState: {
        ...baseSession().runtimeState,
        status: "completed",
      },
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });

    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    await act(async () => Promise.resolve());
    expect(mockGetChatEvents).toHaveBeenCalledOnce();

    await act(async () => {
      vi.advanceTimersByTime(24_000);
      await Promise.resolve();
    });
    expect(mockGetChatEvents).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });

  it("treats a durable terminal state as authoritative over stale local activity flags", async () => {
    vi.useFakeTimers();
    const session = {
      ...baseSession(),
      generating: true,
      status: "running",
      __primaryStreamActive: true,
      runtimeState: {
        ...baseSession().runtimeState,
        status: "completed",
      },
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });

    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    await act(async () => Promise.resolve());

    await act(async () => {
      vi.advanceTimersByTime(24_000);
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
    });

    expect(mockGetChatEvents).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });

  it("mirrors live runtime text locally while persisting metadata only", async () => {
    const onUpdate = vi.fn();
    const session = baseSession();
    const originalMessages = session.messages;
    render(
      <RuntimeEventStreamBridge chatId="backend-chat-1" onUpdate={onUpdate} />,
    );
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    onRuntimeUpdate(messageEventResponse());

    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalledOnce());
    expect(onUpdate).toHaveBeenCalledWith(messageEventResponse());
    const persisted = mockUpdateSession.mock.calls[0]?.[0] as Record<
      string,
      unknown
    >;
    expect(persisted).toMatchObject({
      id: "chat-1",
      generating: true,
      status: "running",
      __preserveMessages: false,
    });
    expect(persisted.messages).toBeUndefined();

    const updateSessions = mockSetSessions.mock.calls[0]?.[0] as (
      sessions: ReturnType<typeof baseSession>[],
    ) => ReturnType<typeof baseSession>[];
    const next = updateSessions([session]);
    expect(next[0]?.messages).not.toBe(originalMessages);
    expect(next[0]?.messages).toHaveLength(2);
    expect(JSON.stringify(next[0]?.messages)).toContain(
      "answer must stay in direct SSE",
    );
    expect(JSON.stringify(next[0]?.messages)).toContain(
      "native_runtime_stream",
    );
  });

  it("commits ledger state while keeping mirrored text silent during the primary stream", async () => {
    const onUpdate = vi.fn();
    const session = {
      ...baseSession(),
      __primaryStreamActive: true,
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    render(
      <RuntimeEventStreamBridge chatId="backend-chat-1" onUpdate={onUpdate} />,
    );
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    onRuntimeUpdate(messageEventResponse());

    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalledOnce());
    expect(onUpdate).toHaveBeenCalledWith(messageEventResponse());
    expect(mockSetSessions).toHaveBeenCalledOnce();
    const updateSessions = mockSetSessions.mock.calls[0]?.[0] as (
      sessions: ReturnType<typeof baseSession>[],
    ) => ReturnType<typeof baseSession>[];
    const next = updateSessions([session]);
    expect(next[0]?.messages).toBe(session.messages);
    expect(next[0]?.runtimeState?.stream_latest_seq).toBe(5);
  });

  it("applies canonical state even when an optional presentation callback throws", async () => {
    const presentationError = new Error("presentation failed");
    const onUpdate = vi.fn(() => {
      throw presentationError;
    });
    render(
      <RuntimeEventStreamBridge chatId="backend-chat-1" onUpdate={onUpdate} />,
    );
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    expect(() => onRuntimeUpdate(turnStartedResponse())).not.toThrow();

    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalledOnce());
    expect(onUpdate).toHaveBeenCalledWith(turnStartedResponse());
  });

  it("closes the matching direct response when message and turn completion arrive while the primary stream is active", async () => {
    const session = {
      ...baseSession(),
      __primaryStreamActive: true,
      runtimeState: {
        ...baseSession().runtimeState,
        run_id: "run-1",
        client_turn_id: "client-turn-1",
      },
      messages: [
        {
          id: "user-1",
          role: "user",
          metadata: { client_turn_id: "client-turn-1" },
          cards: [],
        },
        {
          id: "assistant-1",
          role: "assistant",
          msgStatus: "loading",
          cards: [
            {
              code: "LantaiRuntimeResponseCard",
              data: {
                id: "response-1",
                status: "in_progress",
                metadata: {
                  client_turn_id: "client-turn-1",
                  run_id: "run-1",
                },
                output: [
                  {
                    id: "answer-1",
                    object: "message",
                    status: "in_progress",
                    type: "message",
                    role: "assistant",
                    content: [
                      {
                        object: "content",
                        status: "in_progress",
                        type: "text",
                        index: 0,
                        text: "completed answer",
                      },
                    ],
                    metadata: {
                      native_runtime_stream: false,
                      client_turn_id: "client-turn-1",
                      run_id: "run-1",
                    },
                  },
                ],
              },
            },
          ],
        },
      ],
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    onRuntimeUpdate(completedMessageTurnResponse());

    await waitFor(() => expect(mockSetSessions).toHaveBeenCalledOnce());
    const updateSessions = mockSetSessions.mock.calls[0]?.[0] as (
      sessions: (typeof session)[],
    ) => (typeof session)[];
    const next = updateSessions([session]);
    const assistant = next[0]?.messages[1] as any;
    const response = assistant.cards[0].data;
    const output = response.output[0];

    expect(next[0]?.runtimeState).toMatchObject({ status: "completed" });
    expect(response.status).toBe("completed");
    expect(output.status).toBe("completed");
    expect(output.content[0].status).toBe("completed");
    expect(output.metadata.native_runtime_stream).toBe(true);
    expect(assistant.msgStatus).toBe("finished");
  });

  it("merges a tool frame into the latest live transcript instead of replacing new narration with a stale snapshot", async () => {
    const staleSession = {
      ...baseSession(),
      __primaryStreamActive: true,
    };
    const liveNarration = {
      id: "assistant-live-narration",
      role: "assistant",
      cards: [],
    };
    const latestSession = {
      ...staleSession,
      messages: [...staleSession.messages, liveNarration],
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [staleSession],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    onRuntimeUpdate(toolStartedResponse());

    await waitFor(() => expect(mockSetSessions).toHaveBeenCalledOnce());
    const updateSessions = mockSetSessions.mock.calls[0]?.[0] as (
      sessions: (typeof latestSession)[],
    ) => (typeof latestSession)[];
    const next = updateSessions([latestSession]);

    expect(next[0]?.messages?.map((message) => message.id)).toContain(
      "assistant-live-narration",
    );
    expect(JSON.stringify(next[0]?.messages)).toContain("read_file");
  });

  it("never creates a transcript placeholder from a control-plane turn event", async () => {
    const session = baseSession();
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    onRuntimeUpdate(turnStartedResponse());

    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalledOnce());
    const persisted = mockUpdateSession.mock.calls[0]?.[0] as Record<
      string,
      unknown
    >;
    expect(persisted.messages).toBeUndefined();

    const updateSessions = mockSetSessions.mock.calls[0]?.[0] as (
      sessions: ReturnType<typeof baseSession>[],
    ) => ReturnType<typeof baseSession>[];
    const next = updateSessions([session]);
    expect(next[0]?.messages).toBe(session.messages);
    expect(next[0]?.messages).toHaveLength(1);
  });

  it("does not add a control-plane placeholder while the direct chat stream owns the turn", async () => {
    const session = {
      ...baseSession(),
      __primaryStreamActive: true,
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });

    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    onRuntimeUpdate(turnStartedResponse());

    await waitFor(() => expect(mockSetSessions).toHaveBeenCalledOnce());
    const updateSessions = mockSetSessions.mock.calls[0]?.[0] as (
      sessions: ReturnType<typeof baseSession>[],
    ) => ReturnType<typeof baseSession>[];
    const next = updateSessions([session]);

    expect(next[0]?.messages).toBe(session.messages);
    expect(next[0]?.messages).toHaveLength(1);
    expect(mockUpdateSession.mock.calls[0]?.[0]?.messages).toBeUndefined();
  });

  it("keeps an active turn running when the control-plane transport disconnects", async () => {
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const options = mockSubscribeChatEvents.mock.calls[0]?.[2];

    options.onTransportStatus("reconnecting", {
      message: "network interrupted",
    });

    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalledOnce());
    const persisted = mockUpdateSession.mock.calls[0]?.[0] as Record<
      string,
      any
    >;
    expect(persisted).toMatchObject({
      id: "chat-1",
      generating: true,
      status: "running",
      __preserveMessages: false,
      runtimeState: expect.objectContaining({
        transport_status: "reconnecting",
        transport_error: "network interrupted",
      }),
    });
    expect(persisted.messages).toBeUndefined();
  });

  it("clears a reconnecting marker when transport reopens before the next event", async () => {
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const options = mockSubscribeChatEvents.mock.calls[0]?.[2];
    options.onTransportStatus("reconnecting", {
      message: "network interrupted",
    });
    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalledOnce());
    options.onTransportStatus("connected");
    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalledTimes(2));
    const restored = mockUpdateSession.mock.calls[1]?.[0];
    expect(restored.runtimeState).toMatchObject({
      status: "running",
      transport_status: "connected",
    });
    expect(restored.runtimeState.transport_error).toBeUndefined();
    expect(restored.runtimeState.summary).not.toBe("network interrupted");
    expect(restored.messages).toBeUndefined();
  });

  it("does not revive a completed turn when transport reconnects", async () => {
    const session = baseSession();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [
        {
          ...session,
          generating: false,
          status: "idle",
          runtimeState: { ...session.runtimeState, status: "completed" },
        },
      ],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const options = mockSubscribeChatEvents.mock.calls[0]?.[2];
    options.onTransportStatus("connected");
    await act(async () => Promise.resolve());
    expect(mockUpdateSession).not.toHaveBeenCalled();
  });

  it("does not race durable hydration against an active primary chat stream", async () => {
    const session = {
      ...baseSession(),
      __primaryStreamActive: true,
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    onRuntimeUpdate(terminalEventResponse());

    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalledOnce());
    await new Promise((resolve) => window.setTimeout(resolve, 400));
    expect(mockGetSession).not.toHaveBeenCalled();
    expect(mockSetSessions).toHaveBeenCalledOnce();
  });

  it("does not hydrate or settle a stale logical-turn terminal on a shared run", async () => {
    vi.useFakeTimers();
    const session = {
      ...baseSession(),
      runtimeState: {
        ...baseSession().runtimeState,
        status: "running",
        run_id: "run-shared",
        client_turn_id: "client-turn-old",
        events_preview: [
          {
            id: "evt-old-progress",
            seq: 5,
            ts: "2026-07-26T12:00:20Z",
            type: "message.delta",
            kind: "chat.message.delta",
            status: "running",
            label: "旧轮进度",
            detail: "",
            run_id: "run-shared",
            turn_id: "run-shared",
            client_turn_id: "client-turn-old",
          },
        ],
      },
      messages: [
        {
          id: "user-new",
          role: "user",
          createdAt: "2026-07-26T12:01:00Z",
          metadata: { client_turn_id: "client-turn-new" },
          cards: [],
        },
      ],
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];
    const staleTerminal = {
      ...terminalEventResponse(),
      events: [
        {
          ...terminalEventResponse().events[0],
          id: "evt-old-terminal",
          turn_id: "run-shared",
          type: "turn.failed",
          status: "failed",
          created_at: "2026-07-26T12:00:30Z",
          payload: { final_task_status: "failed" },
        },
      ],
    };

    act(() => onRuntimeUpdate(staleTerminal));
    await act(async () => Promise.resolve());

    expect(mockUpdateSession).toHaveBeenCalledOnce();
    expect(mockUpdateSession.mock.calls[0]?.[0]).toMatchObject({
      generating: true,
      status: "running",
      runtimeState: expect.objectContaining({
        status: "running",
        run_id: "run-shared",
        client_turn_id: "client-turn-new",
      }),
    });

    await act(async () => {
      vi.advanceTimersByTime(1_500);
      await Promise.resolve();
    });
    expect(mockGetSession).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it("cancels accepted-terminal hydration when a new foreground turn starts", async () => {
    vi.useFakeTimers();
    const session = {
      ...baseSession(),
      __primaryStreamActive: false,
      runtimeState: {
        ...baseSession().runtimeState,
        run_id: "run-1",
      },
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    act(() => onRuntimeUpdate(terminalEventResponse()));
    act(() =>
      onRuntimeUpdate({
        ...turnStartedResponse(),
        latest_seq: 7,
        events: [
          {
            ...turnStartedResponse().events[0],
            id: "evt-next-turn-started",
            seq: 7,
            turn_id: "run-2",
            payload: {
              legacy_kind: "chat.turn.started",
              client_turn_id: "client-turn-2",
            },
          },
        ],
      }),
    );
    await act(async () => Promise.resolve());

    await act(async () => {
      vi.advanceTimersByTime(1_500);
      await Promise.resolve();
    });
    expect(mockGetSession).not.toHaveBeenCalled();
    expect(mockUpdateSession).toHaveBeenLastCalledWith(
      expect.objectContaining({
        generating: true,
        status: "running",
        runtimeState: expect.objectContaining({
          status: "running",
          run_id: "run-2",
          client_turn_id: "client-turn-2",
        }),
      }),
    );
    vi.useRealTimers();
  });

  it("cancels terminal hydration when a new user arrives before runtime events", async () => {
    vi.useFakeTimers();
    const terminalSession = {
      ...baseSession(),
      __primaryStreamActive: true,
      runtimeState: {
        ...baseSession().runtimeState,
        run_id: "run-1",
      },
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [terminalSession],
    });
    const view = render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    act(() => onRuntimeUpdate(terminalEventResponse()));
    const nextUserSession = {
      ...terminalSession,
      runtimeState: {
        ...terminalSession.runtimeState,
        status: "completed",
      },
      messages: [
        ...terminalSession.messages,
        {
          id: "user-2",
          role: "user",
          metadata: { client_turn_id: "client-turn-2" },
          cards: [],
        },
      ],
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [nextUserSession],
    });
    view.rerender(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    await act(async () => Promise.resolve());

    await act(async () => {
      vi.advanceTimersByTime(1_500);
      await Promise.resolve();
    });

    expect(mockGetSession).not.toHaveBeenCalled();
    expect(mockSetSessions).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });

  it("discards in-flight terminal hydration when a new user arrives", async () => {
    vi.useFakeTimers();
    let resolveHydration:
      | ((value: ReturnType<typeof baseSession>) => void)
      | null = null;
    mockGetSession.mockReturnValue(
      new Promise((resolve) => {
        resolveHydration = resolve;
      }),
    );
    const terminalSession = {
      ...baseSession(),
      __primaryStreamActive: false,
      runtimeState: {
        ...baseSession().runtimeState,
        run_id: "run-1",
      },
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [terminalSession],
    });
    const view = render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    act(() => onRuntimeUpdate(terminalEventResponse()));
    await act(async () => {
      vi.advanceTimersByTime(1_200);
      await Promise.resolve();
    });
    expect(mockGetSession).toHaveBeenCalledWith("chat-1");

    const nextUserSession = {
      ...terminalSession,
      runtimeState: {
        ...terminalSession.runtimeState,
        status: "completed",
      },
      messages: [
        ...terminalSession.messages,
        {
          id: "user-2",
          role: "user",
          metadata: { client_turn_id: "client-turn-2" },
          cards: [],
        },
      ],
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [nextUserSession],
    });
    view.rerender(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    await act(async () => Promise.resolve());

    await act(async () => {
      resolveHydration?.({
        ...baseSession(),
        generating: false,
        status: "idle",
        messages: [
          { id: "user-1", role: "user", cards: [] },
          { id: "assistant-stale", role: "assistant", cards: [] },
        ],
      });
      await Promise.resolve();
    });

    expect(mockSetSessions).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });

  it("hydrates a just-settled non-canonical stream to restore files and tools", async () => {
    const session = {
      ...baseSession(),
      __primaryStreamActive: false,
      __primaryStreamSettledAt: Date.now(),
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    onRuntimeUpdate(terminalEventResponse());

    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalledOnce());
    await waitFor(() => expect(mockGetSession).toHaveBeenCalledWith("chat-1"), {
      timeout: 2_000,
    });
    expect(mockSetSessions).toHaveBeenCalledTimes(2);
  });

  it("hydrates a completed but non-canonical live transcript to restore tool chronology", async () => {
    const session = {
      ...baseSession(),
      __primaryStreamActive: false,
      messages: [
        { id: "user-1", role: "user", cards: [] },
        {
          id: "assistant-1",
          role: "assistant",
          cards: [
            {
              code: "ResponseCard",
              data: {
                id: "response-1",
                status: "completed",
                output: [
                  {
                    id: "answer-1",
                    type: "message",
                    content: [{ type: "text", text: "already completed" }],
                  },
                ],
              },
            },
          ],
        },
      ],
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    onRuntimeUpdate(terminalEventResponse());

    await waitFor(() => expect(mockUpdateSession).toHaveBeenCalledOnce());
    await waitFor(() => expect(mockGetSession).toHaveBeenCalledWith("chat-1"), {
      timeout: 2_000,
    });
  });

  it("reconciles terminal hydration without deleting visible interim narration", async () => {
    const session = {
      ...baseSession(),
      __primaryStreamActive: false,
      messages: [
        { id: "user-1", role: "user", cards: [] },
        { id: "assistant-live-interim", role: "assistant", cards: [] },
      ],
    };
    mockChooseActiveSessionMessages.mockImplementation((current) => current);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    onRuntimeUpdate(terminalEventResponse());

    await waitFor(() => expect(mockGetSession).toHaveBeenCalledWith("chat-1"), {
      timeout: 2_000,
    });
    await waitFor(() => expect(mockSetSessions).toHaveBeenCalledTimes(2));
    const terminalUpdateSessions = mockSetSessions.mock.calls[0]?.[0] as (
      sessions: ReturnType<typeof baseSession>[],
    ) => ReturnType<typeof baseSession>[];
    const updateSessions = mockSetSessions.mock.calls[1]?.[0] as (
      sessions: ReturnType<typeof baseSession>[],
    ) => ReturnType<typeof baseSession>[];
    const next = updateSessions(terminalUpdateSessions([session]));
    expect(mockChooseActiveSessionMessages).toHaveBeenCalledWith(
      session.messages,
      expect.any(Array),
    );
    expect(next[0]?.messages?.map((message) => message.id)).toEqual([
      "user-1",
      "assistant-live-interim",
    ]);
    expect(next[0]?.generating).toBe(false);
    expect(mockUpdateSession).toHaveBeenCalledOnce();
    expect(mockUpdateSession.mock.calls[0]?.[0]?.messages).toBeUndefined();
  });

  it("preserves narration added before a terminal hydration updater commits", async () => {
    vi.useFakeTimers();
    const session = {
      ...baseSession(),
      __primaryStreamActive: false,
      messages: [
        { id: "user-1", role: "user", cards: [] },
        { id: "assistant-before-hydration", role: "assistant", cards: [] },
      ],
    };
    mockChooseActiveSessionMessages.mockImplementation((current, incoming) => {
      const currentIds = new Set(
        current.map((message: { id?: string }) => message.id),
      );
      return [
        ...current,
        ...(incoming || []).filter(
          (message: { id?: string }) => !currentIds.has(message.id),
        ),
      ];
    });
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    act(() => onRuntimeUpdate(terminalEventResponse()));
    await act(async () => {
      vi.advanceTimersByTime(1_200);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockSetSessions).toHaveBeenCalledTimes(2);
    const terminalUpdater = mockSetSessions.mock.calls[0]?.[0] as (
      sessions: any[],
    ) => any[];
    const hydratedUpdater = mockSetSessions.mock.calls[1]?.[0] as (
      sessions: any[],
    ) => any[];
    const afterTerminal = terminalUpdater([session]);
    const liveNarration = {
      id: "assistant-during-hydration-commit",
      role: "assistant",
      cards: [],
    };
    const liveSession = {
      ...afterTerminal[0],
      messages: [...afterTerminal[0].messages, liveNarration],
    };

    const afterHydration = hydratedUpdater([liveSession]);

    expect(
      afterHydration[0].messages.map((message: any) => message.id),
    ).toEqual(
      expect.arrayContaining([
        "assistant-before-hydration",
        "assistant-during-hydration-commit",
        "assistant-1",
      ]),
    );
    vi.useRealTimers();
  });

  it("keeps authoritative terminal state when durable hydration is stale", async () => {
    vi.useFakeTimers();
    const session = {
      ...baseSession(),
      __primaryStreamActive: false,
      runtimeState: {
        ...baseSession().runtimeState,
        run_id: "run-1",
        client_turn_id: "client-turn-1",
      },
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    mockGetSession.mockResolvedValue({
      ...baseSession(),
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        run_id: "run-1",
        client_turn_id: "client-turn-1",
        outcome_status: "unknown",
        delivery_status: "pending",
      },
      turnEventPreview: { summary_line: "陈旧运行快照", events: [] },
      messages: [
        { id: "user-1", role: "user", cards: [] },
        { id: "assistant-durable", role: "assistant", cards: [] },
      ],
    });
    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];
    const terminalResponse = terminalEventResponse();
    terminalResponse.events[0].payload = {
      client_turn_id: "client-turn-1",
      final_task_status: "completed",
      outcome_status: "delivered",
      delivery_status: "verified",
    };

    act(() => onRuntimeUpdate(terminalResponse));
    await act(async () => {
      vi.advanceTimersByTime(1_200);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockSetSessions).toHaveBeenCalledTimes(2);
    const terminalUpdater = mockSetSessions.mock.calls[0]?.[0] as (
      sessions: any[],
    ) => any[];
    const hydratedUpdater = mockSetSessions.mock.calls[1]?.[0] as (
      sessions: any[],
    ) => any[];
    const afterTerminal = terminalUpdater([session]);
    const afterHydration = hydratedUpdater(afterTerminal);

    expect(afterHydration[0]).toMatchObject({
      generating: false,
      status: "idle",
      runtimeState: {
        status: "completed",
        run_id: "run-1",
        client_turn_id: "client-turn-1",
        outcome_status: "delivered",
        delivery_status: "verified",
      },
    });
    expect(afterHydration[0].turnEventPreview?.summary_line).not.toBe(
      "陈旧运行快照",
    );
    vi.useRealTimers();
  });

  it("discards in-flight terminal hydration when the same run resumes", async () => {
    vi.useFakeTimers();
    let resolveHydration:
      | ((value: ReturnType<typeof baseSession>) => void)
      | null = null;
    mockGetSession.mockReturnValue(
      new Promise((resolve) => {
        resolveHydration = resolve;
      }),
    );
    const session = {
      ...baseSession(),
      __primaryStreamActive: false,
      runtimeState: {
        ...baseSession().runtimeState,
        run_id: "run-1",
        client_turn_id: "client-turn-1",
      },
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [session],
    });
    const view = render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];
    const terminalResponse = terminalEventResponse();
    terminalResponse.events[0].payload = {
      client_turn_id: "client-turn-1",
      final_task_status: "completed",
    };

    act(() => onRuntimeUpdate(terminalResponse));
    await act(async () => {
      vi.advanceTimersByTime(1_200);
      await Promise.resolve();
    });
    expect(mockGetSession).toHaveBeenCalledWith("chat-1");

    const resumedSession = {
      ...session,
      generating: true,
      status: "running",
      runtimeState: {
        ...session.runtimeState,
        status: "running",
      },
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [resumedSession],
    });
    view.rerender(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    await act(async () => Promise.resolve());

    await act(async () => {
      resolveHydration?.({
        ...baseSession(),
        generating: false,
        status: "idle",
      });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockSetSessions).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });

  it("applies an in-flight terminal hydration after the primary stream settles", async () => {
    vi.useFakeTimers();
    let resolveHydration:
      | ((value: ReturnType<typeof baseSession>) => void)
      | null = null;
    mockGetSession.mockReturnValue(
      new Promise((resolve) => {
        resolveHydration = resolve;
      }),
    );
    const stalledSession = {
      ...baseSession(),
      __primaryStreamActive: false,
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [stalledSession],
    });
    const view = render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    const onRuntimeUpdate = mockSubscribeChatEvents.mock.calls[0]?.[1];

    act(() => onRuntimeUpdate(terminalEventResponse()));
    await act(async () => {
      vi.advanceTimersByTime(1_200);
      await Promise.resolve();
    });
    expect(mockGetSession).toHaveBeenCalledWith("chat-1");

    const settledSession = {
      ...stalledSession,
      generating: false,
      status: "idle",
      runtimeState: {
        ...stalledSession.runtimeState,
        status: "completed",
        run_id: "run-1",
      },
      __primaryStreamActive: false,
      __primaryStreamSettledAt: Date.now(),
      messages: [
        { id: "user-1", role: "user", cards: [] },
        {
          id: "assistant-1",
          role: "assistant",
          cards: [
            {
              code: "ResponseCard",
              data: {
                id: "response-1",
                status: "completed",
                output: [
                  {
                    id: "answer-1",
                    type: "message",
                    content: [{ type: "text", text: "live answer" }],
                  },
                ],
              },
            },
          ],
        },
      ],
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      setSessions: mockSetSessions,
      sessions: [settledSession],
    });
    view.rerender(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    await act(async () => Promise.resolve());

    await act(async () => {
      resolveHydration?.({
        ...baseSession(),
        generating: false,
        status: "idle",
        messages: [
          { id: "user-1", role: "user", cards: [] },
          { id: "assistant-stale", role: "assistant", cards: [] },
        ],
      });
      await Promise.resolve();
    });

    expect(mockSetSessions).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("reconciles and backfills a completed turn on window focus without switching chats", async () => {
    mockGetChatEvents
      .mockResolvedValueOnce({
        chat_id: "backend-chat-1",
        after_seq: 0,
        latest_seq: 0,
        events: [],
      })
      .mockResolvedValueOnce(terminalEventResponse());

    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    await waitFor(() => expect(mockGetChatEvents).toHaveBeenCalledOnce());

    act(() => window.dispatchEvent(new Event("focus")));

    await waitFor(() => expect(mockGetChatEvents).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(mockGetSession).toHaveBeenCalledWith("chat-1"), {
      timeout: 2_000,
    });
    await waitFor(() => expect(mockSetSessions).toHaveBeenCalledTimes(2));
  });

  it("bounds a hung recovery read, allows a later recovery, and ignores the late response", async () => {
    vi.useFakeTimers();
    let resolveHungRecovery:
      | ((value: ReturnType<typeof turnStartedResponse>) => void)
      | null = null;
    let recoverySignal: AbortSignal | undefined;
    mockGetChatEvents
      .mockResolvedValueOnce({
        chat_id: "backend-chat-1",
        after_seq: 0,
        latest_seq: 0,
        events: [],
      })
      .mockImplementationOnce((_chatId, _cursor, options) => {
        recoverySignal = options?.signal;
        return new Promise((resolve) => {
          resolveHungRecovery = resolve;
        });
      })
      .mockResolvedValueOnce(terminalEventResponse());

    render(<RuntimeEventStreamBridge chatId="backend-chat-1" />);
    await act(async () => Promise.resolve());

    act(() => window.dispatchEvent(new Event("focus")));
    await act(async () => Promise.resolve());
    expect(mockGetChatEvents).toHaveBeenCalledTimes(2);

    await act(async () => {
      vi.advanceTimersByTime(5_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(recoverySignal?.aborted).toBe(true);

    act(() => window.dispatchEvent(new Event("focus")));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockGetChatEvents).toHaveBeenCalledTimes(3);
    expect(mockUpdateSession).toHaveBeenCalledOnce();

    await act(async () => {
      resolveHungRecovery?.(turnStartedResponse());
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockUpdateSession).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });

  it("rebuilds an empty resync response from the durable session snapshot", async () => {
    const onUpdate = vi.fn();
    const snapshotFileMessage = {
      id: "assistant-ppt-file",
      role: "assistant",
      cards: [
        {
          code: "FileCard",
          data: { name: "答辩汇报.pptx", path: "/workspace/答辩汇报.pptx" },
        },
      ],
    };
    mockGetChatEvents
      .mockResolvedValueOnce({
        chat_id: "backend-chat-1",
        after_seq: 0,
        latest_seq: 0,
        events: [],
      })
      .mockResolvedValueOnce({
        chat_id: "backend-chat-1",
        stream_epoch: "epoch-b",
        cursor: "epoch-b:0",
        after_seq: 4,
        latest_seq: 0,
        resync_required: true,
        resync_reason: "stream_epoch_mismatch",
        events: [],
      });
    mockGetSession.mockResolvedValue({
      ...baseSession(),
      generating: false,
      status: "idle",
      runtimeState: {
        status: "completed",
        run_id: "run-1",
        client_turn_id: "client-turn-1",
        outcome_status: "delivered",
        delivery_status: "verified",
        stream_epoch: "epoch-b",
        stream_latest_seq: 0,
        events_preview: [],
      },
      turnEventPreview: { summary_line: "PPT 已交付", events: [] },
      messages: [baseSession().messages[0], snapshotFileMessage],
    });

    render(
      <RuntimeEventStreamBridge chatId="backend-chat-1" onUpdate={onUpdate} />,
    );
    await waitFor(() => expect(mockGetChatEvents).toHaveBeenCalledOnce());

    act(() => window.dispatchEvent(new Event("focus")));

    await waitFor(() => expect(mockGetSession).toHaveBeenCalledWith("chat-1"));
    await waitFor(() => expect(mockSetSessions).toHaveBeenCalledOnce());
    const updateSessions = mockSetSessions.mock.calls[0]?.[0] as (
      sessions: ReturnType<typeof baseSession>[],
    ) => ReturnType<typeof baseSession>[];
    const next = updateSessions([baseSession()]);
    expect(next[0]).toMatchObject({
      generating: false,
      status: "idle",
      runtimeState: {
        status: "completed",
        stream_epoch: "epoch-b",
        stream_cursor: "epoch-b:0",
      },
    });
    expect(next[0]?.messages?.map((message) => message.id)).toContain(
      "assistant-ppt-file",
    );
    expect(onUpdate).toHaveBeenCalledWith(
      expect.objectContaining({ resync_required: true, events: [] }),
    );
  });
});
