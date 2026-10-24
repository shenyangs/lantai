import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { App } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";

import QuquResponseCard from "./QuquResponseCard";
import { clearUtilityArtifactClassificationCache } from "./utilityArtifactClassification";
import { chatApi, utilityIntelligenceApi } from "@/api";
import { ApiRequestError } from "@/api/request";
import {
  LantaiRuntimeContentType,
  LantaiRuntimeMessageType,
} from "@/vendor/lantaiChatTypes";
import { ChatAnywhereOptionsProvider } from "@/vendor/lantaiChat";
import ToolCallCard from "./ToolCallCard";
import {
  digestQuquSourceMessage,
  resetQuquContinuationStoreForTests,
  upsertQuquContinuation,
} from "../ququContinuationStore";

const chatSessionState = vi.hoisted(() => ({
  current: {
    sessions: [] as Array<Record<string, unknown>>,
    currentSessionId: "",
    setSessions: vi.fn(),
    setCurrentSessionId: vi.fn(),
  },
}));

vi.mock("@/vendor/lantaiChat", async () => {
  const actual = await vi.importActual<typeof import("@/vendor/lantaiChat")>(
    "@/vendor/lantaiChat",
  );
  return {
    ...actual,
    useChatAnywhereSessionsState: () => chatSessionState.current,
  };
});

vi.mock("@/api", () => ({
  chatApi: {
    continueQuqu: vi.fn(),
    getAnalysisChart: vi.fn(),
  },
  utilityIntelligenceApi: {
    classifyUtilityArtifacts: vi.fn(),
  },
}));

vi.mock("echarts/core", () => ({
  use: vi.fn(),
  init: vi.fn(() => ({
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
    getDataURL: vi.fn(() => "data:image/png;base64,dGVzdA=="),
  })),
}));

const classifyUtilityArtifacts = vi.mocked(
  utilityIntelligenceApi.classifyUtilityArtifacts,
);
const continueQuqu = vi.mocked(chatApi.continueQuqu);

const responseData = {
  id: "response-1",
  output: [
    {
      id: "reasoning-1",
      type: LantaiRuntimeMessageType.REASONING,
      status: "completed",
      content: [
        {
          type: LantaiRuntimeContentType.TEXT,
          text: "hidden reasoning text",
        },
      ],
    },
    {
      id: "tool-1",
      type: LantaiRuntimeMessageType.PLUGIN_CALL,
      status: "completed",
      content: [
        {
          data: {
            name: "memory_search",
          },
        },
      ],
    },
    {
      id: "message-1",
      type: LantaiRuntimeMessageType.MESSAGE,
      status: "completed",
      content: [
        {
          type: LantaiRuntimeContentType.TEXT,
          text: "visible final answer",
        },
      ],
    },
  ],
};

describe("QuquResponseCard process summaries", () => {
  it("keeps the real assistant identity visible during a silent 15-second provider wait, then removes the stopped receipt", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-02T06:00:00Z"));
    const session = {
      id: "chat-first-feedback",
      generating: true,
      status: "running",
      runtimeState: { status: "completed", run_id: "previous-run" },
    };
    chatSessionState.current = {
      sessions: [session],
      currentSessionId: session.id,
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };
    const card = () => (
      <ChatAnywhereOptionsProvider
        options={{ welcome: { avatar: "/lantai.png", nick: "沈行知" } }}
      >
        <QuquResponseCard
          data={{
            id: "first-feedback-response",
            status: "in_progress",
            output: [],
            metadata: { optimistic_pending: true },
          }}
          isLast
          progressInComposer
        />
      </ChatAnywhereOptionsProvider>
    );
    try {
      const { container, rerender, unmount } = render(card());
      const avatar = container.querySelector(
        '[data-lantai-response-avatar="1"]',
      );
      expect(avatar).toHaveTextContent("沈行知");
      act(() => vi.advanceTimersByTime(15_000));
      expect(container.querySelector('[data-lantai-response-avatar="1"]')).toBe(
        avatar,
      );
      expect(screen.queryByRole("status")).toBeNull();
      expect(screen.queryByText(/正在思考/)).not.toBeInTheDocument();
      expect(container.querySelector("[data-chat-thinking]")).toBeNull();

      session.generating = false;
      session.status = "idle";
      rerender(card());
      expect(
        container.querySelector('[data-lantai-response-avatar="1"]'),
      ).toBeNull();
      unmount();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    {
      name: "active acknowledged stream",
      active: true,
      generating: true,
      status: "running",
      runId: "receipt-run",
      isLast: true,
      visible: true,
    },
    {
      name: "stopped stream",
      active: false,
      generating: false,
      status: "running",
      runId: "receipt-run",
      isLast: true,
      visible: false,
    },
    {
      name: "restored receipt without local stream",
      active: false,
      generating: true,
      status: "running",
      runId: "receipt-run",
      isLast: true,
      visible: false,
    },
    {
      name: "completed run",
      active: true,
      generating: true,
      status: "completed",
      runId: "receipt-run",
      isLast: true,
      visible: false,
    },
    {
      name: "different run",
      active: true,
      generating: true,
      status: "running",
      runId: "next-run",
      isLast: true,
      visible: false,
    },
    {
      name: "older response",
      active: true,
      generating: true,
      status: "running",
      runId: "receipt-run",
      isLast: false,
      visible: false,
    },
  ])(
    "keeps acknowledged identity scoped to its $name after a long wait",
    ({ active, generating, status, runId, isLast, visible }) => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-10-08T06:00:00Z"));
      chatSessionState.current = {
        sessions: [
          {
            id: "acknowledged-chat",
            generating,
            __primaryStreamActive: active,
            runtimeState: { status, run_id: runId },
          },
        ],
        currentSessionId: "acknowledged-chat",
        setSessions: vi.fn(),
        setCurrentSessionId: vi.fn(),
      };
      try {
        const { container, unmount } = render(
          <ChatAnywhereOptionsProvider
            options={{ welcome: { avatar: "/lantai.png", nick: "沈行知" } }}
          >
            <QuquResponseCard
              data={{
                id: "acknowledged-response",
                status: "in_progress",
                output: [],
                metadata: { pending_receipt_run_id: "receipt-run" },
              }}
              isLast={isLast}
              progressInComposer
            />
          </ChatAnywhereOptionsProvider>,
        );
        expect(
          container.querySelectorAll('[data-lantai-response-avatar="1"]'),
        ).toHaveLength(visible ? 1 : 0);
        act(() => vi.advanceTimersByTime(45_000));
        expect(
          container.querySelectorAll('[data-lantai-response-avatar="1"]'),
        ).toHaveLength(visible ? 1 : 0);
        expect(screen.queryByRole("status")).toBeNull();
        expect(screen.queryByText(/正在思考/)).not.toBeInTheDocument();
        unmount();
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it.each([
    {
      name: "unverified response",
      metadata: {},
      runtimeStatus: "",
      isLast: true,
    },
    {
      name: "older response",
      metadata: { optimistic_pending: true },
      runtimeStatus: "running",
      isLast: false,
    },
    {
      name: "different active run",
      metadata: { run_id: "old-run" },
      runtimeStatus: "running",
      isLast: true,
    },
    {
      name: "terminal run",
      metadata: { run_id: "current-run" },
      runtimeStatus: "completed",
      isLast: true,
    },
  ])(
    "does not revive an assistant identity for a $name",
    ({ metadata, runtimeStatus, isLast }) => {
      chatSessionState.current = {
        sessions: [
          {
            id: "chat-stale-identity",
            generating: true,
            runtimeState: { status: runtimeStatus, run_id: "current-run" },
          },
        ],
        currentSessionId: "chat-stale-identity",
        setSessions: vi.fn(),
        setCurrentSessionId: vi.fn(),
      };
      const { container } = render(
        <ChatAnywhereOptionsProvider
          options={{ welcome: { avatar: "/lantai.png", nick: "沈行知" } }}
        >
          <QuquResponseCard
            data={{
              id: "stale-identity",
              status: "in_progress",
              output: [],
              metadata,
            }}
            isLast={isLast}
            progressInComposer
          />
        </ChatAnywhereOptionsProvider>,
      );
      expect(
        container.querySelector('[data-lantai-response-avatar="1"]'),
      ).toBeNull();
      expect(screen.queryByRole("status")).toBeNull();
    },
  );

  it("leaves no duplicate pending signal when the composer owns progress", () => {
    chatSessionState.current = {
      sessions: [
        {
          id: "compact-chat",
          generating: true,
          runtimeState: {
            status: "running",
            run_id: "compact-run",
          },
        },
      ],
      currentSessionId: "compact-chat",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "compact-response",
          status: "in_progress",
          output: [],
          metadata: { run_id: "compact-run" },
        }}
        progressInComposer
      />,
    );
    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).toBeNull();
    expect(
      container.querySelector(".lantai-pending-assistant-copy"),
    ).toBeNull();
    expect(
      container.querySelector(".lantai-pending-assistant-elapsed"),
    ).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(classifyUtilityArtifacts).not.toHaveBeenCalled();
  });

  beforeEach(async () => {
    chatSessionState.current = {
      sessions: [],
      currentSessionId: "",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };
    clearUtilityArtifactClassificationCache();
    resetQuquContinuationStoreForTests();
    continueQuqu.mockReset();
    continueQuqu.mockResolvedValue({
      continuation_id: "ququ-continuation:one",
      request_id: "request-one",
      agent_id: "default",
      workspace_scope_id: "workspace-one",
      chat_id: "chat-ququ-actions",
      session_id: "console:default",
      user_id: "default",
      channel: "console",
      source_message_id: "message-ququ-actions",
      source_message_digest: await digestQuquSourceMessage(
        "正文。\n\n~>[echo] 你这个“算了”，一点都不像算了。",
      ),
      source_ququ_id: "source-ququ-one",
      ququ_ordinal: 0,
      placement: "echo",
      text: "因为有时候安慰也像另一种催促。你只是该理直气壮地歇一会儿。",
      status: "completed",
      created_at: "2026-09-20T01:00:00Z",
      updated_at: "2026-09-20T01:00:01Z",
      persisted: true,
      tools_used: false,
    });
    classifyUtilityArtifacts.mockReset();
    classifyUtilityArtifacts.mockResolvedValue({
      status: "classified",
      model: "utility/test-model",
      artifacts: [],
    });
  });

  it("exposes an explicit pending status before the first response content", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-pending",
          status: "in_progress",
          output: [],
          metadata: { optimistic_pending: true },
        }}
        showThinking
        showToolCalls
      />,
    );

    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).toBeInTheDocument();
  });

  it("does not invent a starting task for an unverified empty response", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-unverified-empty",
          status: "in_progress",
          output: [],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("正在启动任务")).not.toBeInTheDocument();
  });

  it("does not leave an empty pending spinner after the session is terminal", () => {
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-terminal",
          generating: false,
          status: "idle",
          runtimeState: { status: "completed", run_id: "run-terminal" },
        },
      ],
      currentSessionId: "chat-terminal",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };

    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-stale-pending",
          status: "in_progress",
          output: [],
          metadata: { run_id: "run-terminal" },
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).not.toBeInTheDocument();
  });

  it("keeps a new turn pending while the previous run is still terminal", () => {
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-next-turn",
          generating: false,
          status: "idle",
          runtimeState: { status: "completed", run_id: "run-previous" },
        },
      ],
      currentSessionId: "chat-next-turn",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };

    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-next-turn",
          status: "in_progress",
          output: [],
          metadata: { run_id: "run-next" },
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).toBeInTheDocument();
  });

  it("shows an optimistic pending receipt after restarting an old terminal conversation", () => {
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-restored-terminal",
          generating: true,
          status: "running",
          runtimeState: { status: "completed", run_id: "run-before-restart" },
        },
      ],
      currentSessionId: "chat-restored-terminal",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };

    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-after-restart",
          status: "in_progress",
          output: [],
          metadata: {
            client_turn_id: "user-after-restart",
            optimistic_pending: true,
          },
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.getByText("消息已发出")).toBeInTheDocument();
    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).toBeInTheDocument();
  });

  it("hides an optimistic receipt once the session is no longer generating", () => {
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-finished-optimistic",
          generating: false,
          status: "idle",
          runtimeState: { status: "completed", run_id: "run-finished" },
        },
      ],
      currentSessionId: "chat-finished-optimistic",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };

    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-finished-optimistic",
          status: "in_progress",
          output: [],
          metadata: {
            client_turn_id: "user-finished",
            optimistic_pending: true,
          },
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("正在启动任务")).not.toBeInTheDocument();
  });

  it("never renders the generic pending state on an older assistant card", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-old-empty",
          status: "in_progress",
          output: [],
        }}
        isLast={false}
        showThinking
        showToolCalls
      />,
    );

    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).not.toBeInTheDocument();
  });

  it("hides a long wait unless the backend verifies the current run", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-28T00:00:40Z"));
    try {
      const { container } = render(
        <QuquResponseCard
          data={{
            id: "response-unverified-long-wait",
            status: "in_progress",
            created_at: Math.floor(Date.now() / 1000) - 40,
            output: [],
            metadata: { run_id: "run-unverified" },
          }}
          isLast
          showThinking
          showToolCalls
        />,
      );

      expect(
        container.querySelector('[data-lantai-assistant-pending="1"]'),
      ).not.toBeInTheDocument();
      expect(screen.queryByText("任务仍在进行")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a long wait only for the matching backend run", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-28T00:00:40Z"));
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-running",
          runtimeState: { status: "running", run_id: "run-current" },
        },
      ],
      currentSessionId: "chat-running",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };
    try {
      render(
        <QuquResponseCard
          data={{
            id: "response-current-long-wait",
            status: "in_progress",
            created_at: Math.floor(Date.now() / 1000) - 40,
            output: [],
            metadata: { run_id: "run-current" },
          }}
          isLast
          showThinking
          showToolCalls
        />,
      );

      expect(screen.getByText("暂时没有新进展")).toBeInTheDocument();
      expect(
        screen.getByText("还没收到新进展，请查看过程确认状态"),
      ).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("hides pending state when the backend is running a different run", () => {
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-other-run",
          runtimeState: { status: "running", run_id: "run-current" },
        },
      ],
      currentSessionId: "chat-other-run",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };

    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-stale-run",
          status: "in_progress",
          output: [],
          metadata: { run_id: "run-old" },
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).not.toBeInTheDocument();
  });

  it("does not project a running session onto an unidentified empty card", () => {
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-running-unidentified",
          generating: true,
          status: "running",
          runtimeState: { status: "running", run_id: "run-current" },
        },
      ],
      currentSessionId: "chat-running-unidentified",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };

    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-unidentified-running",
          status: "in_progress",
          output: [],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).not.toBeInTheDocument();
  });

  it("replaces the generic pending state with the selected work method projection", () => {
    render(
      <QuquResponseCard
        data={{
          id: "response-research",
          status: "in_progress",
          output: [],
          metadata: {
            work_method_id: "deep_research",
            runtime_projection_events: [
              {
                id: "started",
                seq: 1,
                type: "turn.started",
                status: "running",
                payload: { phase: "framing" },
              },
            ],
          },
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(
      screen.getByTestId("work-method-projection-deep_research"),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId("lantai-assistant-pending"),
    ).not.toBeInTheDocument();
  });

  it("keeps the pending status visible when the stream only sends a heartbeat", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-heartbeat-only",
          status: "in_progress",
          created_at: Math.floor(Date.now() / 1000),
          output: [
            {
              id: "heartbeat-1",
              type: LantaiRuntimeMessageType.HEARTBEAT,
              status: "in_progress",
              content: [],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(
      container.querySelector('[data-lantai-assistant-pending="1"]'),
    ).toBeInTheDocument();
    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.getByText("还没收到回应")).toBeInTheDocument();
  });

  it("replaces the generic wait with a hosted collaboration acknowledgement", () => {
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-collaboration",
          runtimeState: {
            status: "running",
            run_id: "run-collaboration",
            events_preview: [
              {
                id: "event-preparing",
                ts: "2026-08-23T00:00:00Z",
                kind: "system",
                status: "running",
                label: "正在组建协作室",
                detail: "沈行知正在选择成员与分工",
                payload: {
                  kind: "collaboration_preparing",
                  trace_detail: "沈行知正在选择成员与分工",
                },
              },
            ],
          },
        },
      ],
      currentSessionId: "chat-collaboration",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };

    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-collaboration-pending",
          status: "in_progress",
          output: [],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(screen.getByText("协作请求已接收")).toBeInTheDocument();
    expect(screen.getByText("沈行知正在选择成员与分工")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "协作阶段" })).toBeInTheDocument();
    expect(
      container.querySelector('[data-lantai-collaboration-pending="1"]'),
    ).toBeInTheDocument();
    expect(screen.queryByText("正在等待首个更新")).not.toBeInTheDocument();
  });

  it("uses the real chat options context in controlled visual review", () => {
    const { container } = render(
      <ChatAnywhereOptionsProvider
        options={{
          customToolRenderConfig: { guanlan_read: ToolCallCard },
          welcome: { avatar: "/lantai.png", nick: "沈行知" },
        }}
      >
        <QuquResponseCard
          data={{
            id: "response-context-review",
            status: "completed",
            output: [
              {
                id: "tool-context-review",
                type: LantaiRuntimeMessageType.PLUGIN_CALL,
                status: "completed",
                content: [
                  {
                    data: {
                      name: "guanlan_read",
                      arguments: { url: "https://example.com" },
                    },
                  },
                  { data: { output: "已核对来源。" } },
                ],
              },
            ],
          }}
          isLast
          showThinking
          showToolCalls
        />
      </ChatAnywhereOptionsProvider>,
    );

    expect(screen.getByText("工具执行已结束")).toBeInTheDocument();
    expect(screen.getByText("沈行知")).toBeInTheDocument();
    const group = container.querySelector('[data-lantai-tool-group="1"]');
    expect(group).toHaveAttribute("data-status", "completed");
    expect(group).toHaveTextContent("工具执行已完成");
    expect(group).toHaveTextContent("读取与检索");
    expect(group).not.toHaveTextContent("guanlan_read");
    expect(
      container.querySelector(".lantai-tool-card"),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /展开执行过程/ }));
    expect(screen.getByText("执行轨迹")).toBeInTheDocument();
  });

  it("presents a user stop as a neutral recoverable status", () => {
    const continueGeneration = vi.fn();
    const retryGeneration = vi.fn();
    const { container } = render(
      <ChatAnywhereOptionsProvider
        options={{
          recovery: { continueGeneration, retryGeneration },
        }}
      >
        <QuquResponseCard
          data={{
            id: "response-stopped",
            status: "failed",
            output: [],
            error: {
              message: "已停止生成",
              code: "user_stopped",
              user_stopped: true,
            },
          }}
          showThinking
          showToolCalls
        />
      </ChatAnywhereOptionsProvider>,
    );

    expect(screen.getByText("已生成的内容已保留")).toBeInTheDocument();
    expect(
      container.querySelector('.lantai-response-status[data-tone="stopped"]'),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "准备继续" }));
    fireEvent.click(screen.getByRole("button", { name: "准备重新执行" }));
    expect(continueGeneration).toHaveBeenCalledTimes(1);
    expect(retryGeneration).toHaveBeenCalledTimes(1);
  });

  it("keeps technical failure details collapsed behind a retry action", () => {
    const retryGeneration = vi.fn();
    const { container } = render(
      <ChatAnywhereOptionsProvider options={{ recovery: { retryGeneration } }}>
        <QuquResponseCard
          data={{
            id: "response-technical-error",
            status: "failed",
            output: [],
            error: {
              message:
                "HTTP 502: upstream_exception { trace_id: request-1234567890 }",
            },
          }}
          showThinking
          showToolCalls
        />
      </ChatAnywhereOptionsProvider>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("这次没能完成");
    expect(screen.getByText("查看原因")).toBeInTheDocument();
    expect(
      container.querySelector('.lantai-response-status[data-tone="error"]'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "准备重新执行" }));
    expect(retryGeneration).toHaveBeenCalledTimes(1);
  });

  it("shows factual elapsed stages while waiting for the first visible update", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-28T00:00:00Z"));

    try {
      const { container } = render(
        <QuquResponseCard
          data={{
            id: "response-elapsed-pending",
            status: "in_progress",
            created_at: Math.floor(Date.now() / 1000),
            output: [],
            metadata: { optimistic_pending: true },
          }}
          isLast
          showThinking
          showToolCalls
        />,
      );

      const pending = container.querySelector(
        '[data-lantai-assistant-pending="1"]',
      );
      expect(pending).toHaveAttribute("data-stage", "waiting");
      expect(screen.getByText("等待回应")).toBeInTheDocument();
      expect(screen.getByText("消息已发出")).toBeInTheDocument();
      expect(screen.getByText("刚刚")).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(12_000);
      });

      expect(pending).toHaveAttribute("data-stage", "waiting");
      expect(screen.getByText("等待回应")).toBeInTheDocument();
      expect(screen.getByText("12 秒")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders a compact process summary inside the latest assistant turn", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-inline-process",
          status: "in_progress",
          created_at: Math.floor(Date.now() / 1000) - 5,
          output: [
            {
              id: "reasoning-inline",
              type: LantaiRuntimeMessageType.REASONING,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "先确认文件结构。",
                },
              ],
            },
            {
              id: "tool-inline",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "in_progress",
              content: [
                {
                  data: {
                    call_id: "write-inline",
                    name: "write_file",
                    arguments: { path: "index.html" },
                  },
                },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    const summary = container.querySelector('[data-lantai-inline-process="1"]');
    expect(summary).toHaveAttribute("data-status", "running");
    expect(summary).toHaveTextContent("正在编辑文件");
    expect(summary).toHaveTextContent("1 次工具调用");
    expect(summary).toHaveTextContent("1 段思考");
  });

  it("does not call a tools-only terminal response completed", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-tools-only-ended",
          status: "completed",
          output: [
            {
              id: "tool-only",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "completed",
              content: [
                {
                  data: {
                    name: "execute_shell_command",
                  },
                },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    const summary = container.querySelector('[data-lantai-inline-process="1"]');
    expect(summary).toHaveAttribute("data-status", "incomplete");
    expect(summary).toHaveTextContent("工具执行已结束");
    expect(summary).toHaveTextContent("尚未看到完整回复");
    expect(summary).not.toHaveTextContent("本轮执行完成");
  });

  it("keeps a terminal tools-only handoff visible when a trailing placeholder is last", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-tools-only-not-last",
          status: "completed",
          output: [
            {
              id: "tool-only-not-last",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "completed",
              content: [{ data: { name: "create_presentation" } }],
            },
          ],
        }}
        isLast={false}
        showThinking
        showToolCalls
      />,
    );

    const summary = container.querySelector('[data-lantai-inline-process="1"]');
    expect(summary).toHaveAttribute("data-status", "incomplete");
    expect(summary).toHaveTextContent("工具执行已结束");
    expect(summary).toHaveTextContent("尚未看到完整回复");
  });

  it("shows an explicit recovery state when a completed last response is empty", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-completed-empty",
          status: "completed",
          output: [],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    const summary = container.querySelector('[data-lantai-inline-process="1"]');
    expect(summary).toHaveAttribute("data-status", "incomplete");
    expect(summary).toHaveTextContent("未收到最终回复");
    expect(summary).toHaveTextContent("可先核实已有结果，再决定是否重新执行");
  });

  it("groups consecutive completed tools into one expandable chronological step", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-consecutive-tools",
          status: "completed",
          output: [
            {
              id: "tool-read",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "completed",
              content: [{ data: { name: "read_file" } }],
            },
            {
              id: "tool-write",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "completed",
              content: [{ data: { name: "write_file" } }],
            },
            {
              id: "message-after-tools",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "已生成文件。",
                },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    const groups = container.querySelectorAll('[data-lantai-tool-group="1"]');
    expect(groups).toHaveLength(1);
    expect(groups[0]).toHaveTextContent("2 次调用");
    expect(groups[0]).toHaveTextContent("读取与检索、编辑文件");
    expect(
      container.querySelectorAll('[data-chat-tool-call="1"]'),
    ).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: /展开执行过程/ }));
    expect(
      container.querySelectorAll('[data-chat-tool-call="1"]'),
    ).toHaveLength(2);
  });

  it("settles completed tool groups while the turn continues reasoning", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-completed-tools-still-reasoning",
          status: "in_progress",
          output: [
            {
              id: "tool-read-completed",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "completed",
              content: [{ data: { name: "read_file" } }],
            },
            {
              id: "reasoning-after-completed-tool",
              type: LantaiRuntimeMessageType.REASONING,
              status: "in_progress",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "继续整理读取到的内容。",
                },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    const group = container.querySelector('[data-lantai-tool-group="1"]');
    expect(group).toHaveAttribute("data-status", "completed");
    expect(group).toHaveTextContent("工具执行已完成");
    expect(group).not.toHaveAttribute("aria-busy");
    expect(group?.querySelector(".lantai-tool-group-spin")).toBeNull();
  });

  it("keeps recovered tool failures visible after an answer", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-recovered-tool-failure",
          status: "completed",
          output: [
            {
              id: "tool-command-failed",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "failed",
              content: [
                {
                  data: {
                    name: "execute_shell_command",
                    error: "ModuleNotFoundError: No module named 'pptx'",
                  },
                },
              ],
            },
            {
              id: "tool-render-recovered",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "completed",
              content: [{ data: { name: "run_skill_script" } }],
            },
            {
              id: "message-after-recovery",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "PPT 已生成并通过检查。",
                },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    const processSummary = container.querySelector(
      '[data-lantai-inline-process="1"]',
    );
    expect(processSummary).toHaveAttribute("data-status", "attention");
    expect(processSummary).toHaveTextContent("回复已生成，仍有步骤未完成");

    const group = container.querySelector('[data-lantai-tool-group="1"]');
    expect(group).toHaveAttribute("data-status", "attention");
    expect(group).toHaveTextContent("有一步未完成");
    expect(group).toHaveTextContent("详情已保留");
    expect(
      screen.getByRole("button", { name: /展开执行过程/ }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(
      container.querySelectorAll('[data-chat-tool-call="1"]'),
    ).toHaveLength(0);
  });

  it("keeps an intermediate tool failure neutral while the turn is still recovering", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-recovering-tool-failure",
          status: "in_progress",
          output: [
            {
              id: "tool-policy-blocked",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "failed",
              content: [
                {
                  data: {
                    name: "execute_shell_command",
                    error: "禁止直接写入系统敏感路径。",
                  },
                },
              ],
            },
            {
              id: "reasoning-after-block",
              type: LantaiRuntimeMessageType.REASONING,
              status: "in_progress",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "改用工作区内的安全路径。",
                },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    const group = container.querySelector('[data-lantai-tool-group="1"]');
    expect(group).toHaveAttribute("data-status", "running");
    expect(group).toHaveTextContent("正在尝试其他方式");
    expect(group).not.toHaveTextContent("有一步未完成");
    expect(group).not.toHaveTextContent("详情已保留");
    expect(group).toHaveAttribute("aria-busy", "true");
  });

  it("still surfaces an unrecovered tool failure when the turn ends without an answer", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-terminal-tool-failure",
          status: "failed",
          output: [
            {
              id: "tool-terminal-failure",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "failed",
              content: [
                {
                  data: {
                    name: "execute_shell_command",
                    error: "Command failed with exit code 1.",
                  },
                },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    const group = container.querySelector('[data-lantai-tool-group="1"]');
    expect(group).toHaveAttribute("data-status", "attention");
    expect(group).toHaveTextContent("有一步未完成");
  });

  it("keeps trailing tool failures visible after the assistant answer", () => {
    const ququ = "这轮到这里就好。";
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-trailing-tool-failure",
          status: "completed",
          output: [
            {
              id: "message-before-trailing-tool",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `版本信息已经确认。\n\n~>[echo] ${ququ}`,
                },
              ],
            },
            {
              id: "tool-failed-after-answer",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "failed",
              content: [
                {
                  data: {
                    name: "execute_shell_command",
                    error: "follow-up inspection failed",
                  },
                },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    expect(screen.getByText("版本信息已经确认。")).toBeInTheDocument();
    expect(screen.getByText(ququ)).toBeInTheDocument();
    const group = container.querySelector('[data-lantai-tool-group="1"]');
    expect(group).toHaveAttribute("data-status", "attention");
    expect(group).toHaveTextContent("有一步未完成");
    expect(group).toHaveTextContent("详情已保留");
  });

  it("keeps prominent action and artifact tools visible outside collapsed groups", () => {
    const ImageRenderer = () => (
      <div data-chat-tool-call="1" data-prominent-image-tool="1">
        图片产物
      </div>
    );
    const { container } = render(
      <ChatAnywhereOptionsProvider
        options={{ customToolRenderConfig: { generate_image: ImageRenderer } }}
      >
        <QuquResponseCard
          data={{
            id: "response-prominent-tool",
            status: "completed",
            output: [
              {
                id: "tool-image",
                type: LantaiRuntimeMessageType.PLUGIN_CALL,
                status: "completed",
                content: [{ data: { name: "generate_image" } }],
              },
            ],
          }}
          isLast
          showThinking
          showToolCalls
        />
      </ChatAnywhereOptionsProvider>,
    );

    expect(
      container.querySelector('[data-prominent-image-tool="1"]'),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-lantai-tool-group="1"]'),
    ).not.toBeInTheDocument();
  });

  it("keeps a completed generated video visible after the final answer", () => {
    const VideoRenderer = () => (
      <div data-chat-tool-call="1" data-prominent-video-tool="1">
        视频产物
      </div>
    );
    const { container } = render(
      <ChatAnywhereOptionsProvider
        options={{ customToolRenderConfig: { generate_video: VideoRenderer } }}
      >
        <QuquResponseCard
          data={{
            id: "response-video-delivery",
            status: "completed",
            output: [
              {
                id: "tool-video",
                type: LantaiRuntimeMessageType.PLUGIN_CALL,
                status: "completed",
                content: [
                  { data: { name: "generate_video" } },
                  {
                    data: {
                      output: [
                        {
                          type: "video",
                          source: {
                            type: "url",
                            url: "file:///tmp/lantai/generated-video.mp4",
                          },
                        },
                      ],
                    },
                  },
                ],
              },
              {
                id: "message-video-delivery",
                type: LantaiRuntimeMessageType.MESSAGE,
                status: "completed",
                content: [
                  {
                    type: LantaiRuntimeContentType.TEXT,
                    text: "视频已经生成完成。",
                  },
                ],
              },
            ],
          }}
          isLast
          showThinking
          showToolCalls
        />
      </ChatAnywhereOptionsProvider>,
    );

    expect(
      container.querySelector('[data-prominent-video-tool="1"]'),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-lantai-tool-group="1"]'),
    ).not.toBeInTheDocument();
  });

  it("filters thinking and tool messages when display settings disable them", () => {
    const { container } = render(
      <QuquResponseCard
        data={responseData}
        showThinking={false}
        showToolCalls={false}
      />,
    );

    expect(screen.getByText("visible final answer")).toBeInTheDocument();
    expect(screen.queryByText("思考")).not.toBeInTheDocument();
    expect(screen.queryByText("hidden reasoning text")).not.toBeInTheDocument();
    expect(screen.queryByText("memory_search")).not.toBeInTheDocument();
    expect(
      container.querySelector('[data-lantai-assistant-answer="1"]'),
    ).toHaveTextContent("visible final answer");
  });

  it("preserves source order when an assistant message arrives before a later tool", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-answer-before-later-tools",
          status: "in_progress",
          output: [
            {
              id: "message-progress",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "我先查一下",
                },
              ],
            },
            {
              id: "message-deliverable",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "这是用户要看的最终结果",
                },
              ],
            },
            {
              id: "tool-after-deliverable",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "completed",
              content: [{ data: { name: "guanlan_read" } }],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    const progress = screen.getByText("我先查一下");
    const deliverable = screen.getByText("这是用户要看的最终结果");
    const toolGroup = container.querySelector(
      '[data-lantai-tool-group="1"]',
    ) as HTMLElement;
    expect(
      progress.compareDocumentPosition(deliverable) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      deliverable.compareDocumentPosition(toolGroup) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(toolGroup).toHaveTextContent("读取与检索");
    expect(
      container.querySelectorAll('[data-lantai-assistant-answer="1"]'),
    ).toHaveLength(2);
    expect(
      container.querySelector('[data-lantai-assistant-answer="1"]'),
    ).toHaveClass("lantai-assistant-answer");
  });

  it("keeps a compact tool group between the primary answer and trailing Ququ echo", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-answer-tools-echo",
          status: "completed",
          output: [
            {
              id: "message-deliverable-before-tool",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "这是用户要看的主体答案",
                },
              ],
            },
            {
              id: "tool-after-primary-answer",
              type: LantaiRuntimeMessageType.PLUGIN_CALL,
              status: "completed",
              content: [{ data: { name: "web_search" } }],
            },
            {
              id: "ququ-after-tool",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "~>[echo] 这次差点被顺序骗了。",
                },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls
      />,
    );

    const answer = screen.getByText("这是用户要看的主体答案");
    const echo = screen.getByText("这次差点被顺序骗了。");
    const toolGroup = container.querySelector(
      '[data-lantai-tool-group="1"]',
    ) as HTMLElement;
    expect(
      answer.compareDocumentPosition(toolGroup) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      toolGroup.compareDocumentPosition(echo) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(toolGroup).toHaveTextContent("搜索与检查");
  });

  it("silently omits unavailable thinking from persisted history", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-no-thinking",
          status: "completed",
          output: [
            {
              id: "message-no-thinking",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "直接回答",
                },
              ],
              metadata: {
                metadata: {
                  reasoning_status: {
                    requested: true,
                    returned: false,
                  },
                },
              },
            },
          ],
        }}
        showThinking
        showToolCalls={false}
      />,
    );

    expect(
      container.querySelector('[data-lantai-reasoning-unavailable="1"]'),
    ).not.toBeInTheDocument();
    expect(screen.getByText("直接回答")).toBeInTheDocument();
    expect(
      screen.queryByText("已向模型请求思考过程，但模型本轮未返回可展示内容。"),
    ).not.toBeInTheDocument();
  });

  it("keeps completed thinking collapsed without replacing its full content", () => {
    const { container } = render(
      <QuquResponseCard
        data={responseData}
        isLast
        showThinking
        showToolCalls={false}
      />,
    );

    const reasoning = container.querySelector(".lantai-reasoning");
    expect(reasoning).toBeInTheDocument();
    expect(reasoning).not.toHaveAttribute("open");
    expect(reasoning).toHaveTextContent("hidden reasoning text");
  });

  it("renders the complete in-progress reasoning stream", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-thinking-only",
          status: "in_progress",
          output: [
            {
              id: "thinking-1",
              type: LantaiRuntimeMessageType.REASONING,
              status: "in_progress",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "先想一下",
                  status: "in_progress",
                },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls={false}
      />,
    );

    const thinking = container.querySelector('[data-chat-thinking="1"]');
    expect(thinking).toBeInTheDocument();
    expect(thinking).toHaveAttribute("data-status", "in_progress");
    expect(thinking).not.toHaveAttribute("open");
    expect(screen.getByText("先想一下")).toBeInTheDocument();
    fireEvent.click(screen.getByText("思考"));
    expect(thinking).toHaveAttribute("open");
    expect(
      container.querySelector('[data-lantai-inline-process="1"]'),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("visible final answer")).not.toBeInTheDocument();
  });

  it("keeps adjacent thinking blocks complete when they have different sources", () => {
    const repeated =
      "用户突然要求给新生儿起名。这是一个很有情感意义的任务。用户突然要求给新生儿起名。这是一个很有情感意义的任务。";
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-repeated-thinking",
          status: "in_progress",
          output: [
            {
              id: "reasoning-a",
              type: LantaiRuntimeMessageType.REASONING,
              status: "in_progress",
              content: [
                { type: LantaiRuntimeContentType.TEXT, text: repeated },
              ],
            },
            {
              id: "reasoning-b",
              type: LantaiRuntimeMessageType.REASONING,
              status: "in_progress",
              content: [
                { type: LantaiRuntimeContentType.TEXT, text: repeated },
              ],
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls={false}
      />,
    );

    expect(screen.getAllByText("思考")).toHaveLength(2);
    const reasoningBlocks = container.querySelectorAll(
      ".lantai-reasoning-content",
    );
    expect(reasoningBlocks).toHaveLength(2);
    expect(reasoningBlocks[0]).toHaveTextContent(repeated);
    expect(reasoningBlocks[1]).toHaveTextContent(repeated);
    expect(screen.queryByText(/已自动压缩/)).not.toBeInTheDocument();
  });

  it("merges same-source streaming fragments without losing their text", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-same-source-thinking",
          status: "in_progress",
          output: [
            {
              id: "reasoning-fragment-a",
              type: LantaiRuntimeMessageType.REASONING,
              status: "in_progress",
              content: [
                { type: LantaiRuntimeContentType.TEXT, text: "先核对原文。" },
              ],
              metadata: { source_message_id: "provider-reasoning-1" },
            },
            {
              id: "reasoning-fragment-b",
              type: LantaiRuntimeMessageType.REASONING,
              status: "in_progress",
              content: [
                { type: LantaiRuntimeContentType.TEXT, text: "再核对日期。" },
              ],
              metadata: { source_message_id: "provider-reasoning-1" },
            },
          ],
        }}
        isLast
        showThinking
        showToolCalls={false}
      />,
    );

    expect(screen.getAllByText("思考")).toHaveLength(1);
    expect(
      container.querySelector(".lantai-reasoning-content"),
    ).toHaveTextContent("先核对原文。再核对日期。");
  });

  it("keeps the last visible response during a transient empty streaming patch", () => {
    const { rerender } = render(
      <QuquResponseCard
        data={{ ...responseData, status: "in_progress" }}
        showThinking
        showToolCalls
      />,
    );

    expect(screen.getByText("visible final answer")).toBeInTheDocument();

    rerender(
      <QuquResponseCard
        data={{ id: "response-1", status: "in_progress", output: [] }}
        showThinking
        showToolCalls
      />,
    );

    expect(screen.getByText("visible final answer")).toBeInTheDocument();
  });

  it("does not reuse cached streaming output for a different response", () => {
    const { rerender } = render(
      <QuquResponseCard
        data={{ ...responseData, status: "in_progress" }}
        showThinking
        showToolCalls
      />,
    );

    expect(screen.getByText("visible final answer")).toBeInTheDocument();

    rerender(
      <QuquResponseCard
        data={{ id: "response-2", status: "in_progress", output: [] }}
        showThinking
        showToolCalls
      />,
    );

    expect(screen.queryByText("visible final answer")).not.toBeInTheDocument();
  });

  it("renders a response error once when it is also present in output", () => {
    render(
      <QuquResponseCard
        data={{
          id: "response-error",
          status: "failed",
          error: { message: "请求失败" },
          output: [
            {
              id: "error-1",
              type: LantaiRuntimeMessageType.ERROR,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "请求失败",
                },
              ],
            },
          ],
        }}
        showThinking={false}
        showToolCalls={false}
      />,
    );

    expect(screen.getAllByText("请求失败")).toHaveLength(1);
  });

  it("renders persisted text output even when the message type is missing", () => {
    render(
      <QuquResponseCard
        data={{
          id: "response-missing-type",
          status: "completed",
          output: [
            {
              id: "message-missing-type",
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "你好。我在。你说。",
                },
              ],
            },
          ],
        }}
        showThinking
        showToolCalls
      />,
    );

    expect(screen.getByText("你好。我在。你说。")).toBeInTheDocument();
  });

  it("keeps a legacy unmarked ququ after the ordinary answer", () => {
    const answer = "这是用户应当先看到的正文。";
    const ququ = "这句悄悄话应当成为正文的余韵。";
    render(
      <QuquResponseCard
        data={{
          id: "response-leading-ququ",
          status: "completed",
          output: [
            {
              id: "message-leading-ququ",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `~>${ququ}\n\n${answer}`,
                },
              ],
            },
          ],
        }}
      />,
    );

    const answerNode = screen.getByText(answer);
    const ququNode = screen.getByText(ququ);
    expect(
      answerNode.compareDocumentPosition(ququNode) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("places a semantic lead ququ before the ordinary answer", () => {
    const answer = "这是辩题的完整拆解。";
    const ququ = "好，这题挺狠的，我先锁定义，再给正反打法。";
    render(
      <QuquResponseCard
        data={{
          id: "response-semantic-lead-ququ",
          status: "completed",
          output: [
            {
              id: "message-semantic-lead-ququ",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `~>[lead] ${ququ}\n\n${answer}`,
                },
              ],
            },
          ],
        }}
      />,
    );

    const answerNode = screen.getByText(answer);
    const ququNode = screen.getByText(ququ);
    expect(
      ququNode.compareDocumentPosition(answerNode) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("shows lead while streaming but waits for a stable source before continuation", async () => {
    const card = (status: "in_progress" | "completed") => (
      <QuquResponseCard
        data={{
          id: "streaming-response",
          status,
          output: [
            {
              id: "streaming-message",
              type: LantaiRuntimeMessageType.MESSAGE,
              status,
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  status,
                  text: "~>[lead] 我先松了一口气。\n\n正文正在继续。",
                },
              ],
            },
          ],
        }}
      />
    );
    const { rerender } = render(card("in_progress"));
    expect(screen.getByText("我先松了一口气。")).toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(screen.getByRole("button", { name: "再说一点" })).toBeDisabled();
    rerender(card("completed"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "再说一点" })).toBeEnabled(),
    );
  });

  it("expands a ququ in place without creating a user turn or exposing feedback labels", async () => {
    const ququ = "你这个“算了”，一点都不像算了。";
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-ququ-actions",
          realId: "chat-ququ-actions",
          sessionId: "console:default",
          userId: "default",
          channel: "console",
        },
      ],
      currentSessionId: "chat-ququ-actions",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };
    const card = (
      <QuquResponseCard
        data={{
          id: "response-ququ-actions",
          status: "completed",
          input: [
            {
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "最近其实有点累，但不太想承认。",
                },
              ],
            },
          ],
          output: [
            {
              id: "message-ququ-actions",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `正文。\n\n~>[echo] ${ququ}`,
                },
              ],
            },
          ],
        }}
      />
    );
    const rendered = render(
      <>
        <div className="lantai-sender-test-shell">
          <textarea aria-label="消息输入框" />
        </div>
        {card}
      </>,
    );

    const composer = screen.getByRole("textbox", { name: "消息输入框" });
    expect(screen.queryByRole("button", { name: "接着说" })).toBeNull();
    expect(screen.queryByRole("button", { name: "说中了" })).toBeNull();
    expect(screen.queryByRole("button", { name: "没说中" })).toBeNull();

    const continueButton = screen.getByRole("button", { name: "再说一点" });
    await waitFor(() => expect(continueButton).toBeEnabled());
    fireEvent.click(continueButton);

    await waitFor(() => {
      expect(continueQuqu).toHaveBeenCalledWith(expect.objectContaining({
        request_id: expect.any(String),
        chat_id: "chat-ququ-actions",
        session_id: "console:default",
        user_id: "default",
        channel: "console",
        source_message_id: "message-ququ-actions",
        source_message_digest: expect.stringMatching(/^[0-9a-f]{64}$/),
        ququ_ordinal: 0,
        placement: "echo",
        response_id: "response-ququ-actions",
        ququ_text: ququ,
        user_text: "最近其实有点累，但不太想承认。",
        answer_text: "正文。",
        retry_failed: false,
      }));
    });
    expect(
      await screen.findByText(
        "因为有时候安慰也像另一种催促。你只是该理直气壮地歇一会儿。",
      ),
    ).toBeInTheDocument();
    expect(composer).toHaveValue("");
    expect(screen.queryByRole("button", { name: "再说一点" })).toBeNull();
    expect(screen.queryByRole("button", { name: "更多蛐蛐操作" })).toBeNull();

    rendered.unmount();
    render(card);
    expect(
      await screen.findByText(
        "因为有时候安慰也像另一种催促。你只是该理直气壮地歇一会儿。",
      ),
    ).toBeInTheDocument();
    expect(continueQuqu).toHaveBeenCalledTimes(1);
  });

  it("shows the source conflict instead of asking for a futile retry", async () => {
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-conflict",
          realId: "chat-conflict",
          sessionId: "console:conflict",
          userId: "default",
          channel: "console",
        },
      ],
      currentSessionId: "chat-conflict",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };
    continueQuqu.mockRejectedValueOnce(
      new ApiRequestError("原回答版本已变化，请在当前回答上重新操作", {
        httpStatus: 409,
        kind: "http",
        recoveryAction: "inspect-error",
      }),
    );
    render(
      <QuquResponseCard
        data={{
          id: "response-conflict",
          status: "completed",
          output: [
            {
              id: "message-conflict",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "正文。\n\n~>[echo] 这里有句蛐蛐。",
                },
              ],
            },
          ],
        }}
      />,
    );
    const button = screen.getByRole("button", { name: "再说一点" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(
      await screen.findByText("原回答版本已变化，请在当前回答上重新操作"),
    ).toBeInTheDocument();
    expect(screen.queryByText("这句一时没接上，再试一次")).toBeNull();
  });

  it.each([
    ["ququ_generation_failed", true],
    ["ququ_generation_pending", false],
  ] as const)(
    "recovers %s using the correct retry intent",
    async (code, retryFailed) => {
      chatSessionState.current = {
        sessions: [
          {
            id: "chat-conflict",
            realId: "chat-conflict",
            sessionId: "console:conflict",
            userId: "default",
            channel: "console",
          },
        ],
        currentSessionId: "chat-conflict",
        setSessions: vi.fn(),
        setCurrentSessionId: vi.fn(),
      };
      continueQuqu.mockRejectedValueOnce(
        new ApiRequestError("续念状态提示", {
          code,
          httpStatus: 409,
          kind: "http",
          recoveryAction: "inspect-error",
        }),
      );
      render(
        <QuquResponseCard
          data={{
            id: "response-conflict",
            status: "completed",
            output: [
              {
                id: "message-conflict",
                type: LantaiRuntimeMessageType.MESSAGE,
                status: "completed",
                content: [
                  {
                    type: LantaiRuntimeContentType.TEXT,
                    text: "正文。\n\n~>[echo] 这里有句蛐蛐。",
                  },
                ],
              },
            ],
          }}
        />,
      );
      const button = screen.getByRole("button", { name: "再说一点" });
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);
      await waitFor(() => expect(continueQuqu).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(button).toBeEnabled());
      continueQuqu.mockRejectedValueOnce(new Error("temporary failure"));
      fireEvent.click(button);
      await waitFor(() => expect(continueQuqu).toHaveBeenCalledTimes(2));
      expect(continueQuqu.mock.calls[1][0]).toEqual(
        expect.objectContaining({
          retry_failed: retryFailed,
          request_id: continueQuqu.mock.calls[0][0].request_id,
        }),
      );
    },
  );

  it("does not attach an old continuation to a regenerated answer that reused the message id", async () => {
    const ququ = "同一句蛐蛐也必须跟着回答版本走。";
    const oldSource = `旧正文。\n\n~>[echo] ${ququ}`;
    const newSource = `重生成后的新正文。\n\n~>[echo] ${ququ}`;
    chatSessionState.current = {
      sessions: [
        {
          id: "chat-versioned",
          realId: "chat-versioned",
          sessionId: "console:versioned",
          userId: "default",
          channel: "console",
        },
      ],
      currentSessionId: "chat-versioned",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };
    const record = {
      request_id: "request-versioned",
      agent_id: "default",
      workspace_scope_id: "workspace-versioned",
      chat_id: "chat-versioned",
      session_id: "console:versioned",
      user_id: "default",
      channel: "console",
      source_message_id: "shared-version-message",
      source_ququ_id: "source-versioned",
      ququ_ordinal: 0,
      placement: "echo" as const,
      status: "completed" as const,
      created_at: "2026-09-20T01:00:00Z",
      persisted: true,
      tools_used: false,
    };
    upsertQuquContinuation({
      ...record,
      continuation_id: "ququ-continuation:old-version",
      source_message_digest: await digestQuquSourceMessage(oldSource),
      text: "只属于旧回答版本的续念。",
      updated_at: "2026-09-20T01:00:01Z",
    });
    upsertQuquContinuation({
      ...record,
      continuation_id: "ququ-continuation:new-version",
      source_message_digest: await digestQuquSourceMessage(newSource),
      text: "只属于新回答版本的续念。",
      updated_at: "2026-09-20T01:00:02Z",
    });
    const card = (source: string) => (
      <QuquResponseCard
        data={{
          id: "response-versioned",
          status: "completed",
          output: [
            {
              id: "shared-version-message",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: source,
                },
              ],
            },
          ],
        }}
      />
    );

    const oldRender = render(card(oldSource));
    expect(
      await screen.findByText("只属于旧回答版本的续念。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("只属于新回答版本的续念。")).toBeNull();
    oldRender.unmount();

    render(card(newSource));
    expect(
      await screen.findByText("只属于新回答版本的续念。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("只属于旧回答版本的续念。")).toBeNull();
  });

  it("keeps ququ actions limited to the in-place continuation", () => {
    const ququ = "你这个“算了”，一点都不像算了。";
    render(
      <QuquResponseCard
        data={{
          id: "response-ququ-feedback",
          status: "completed",
          output: [
            {
              id: "message-ququ-feedback",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `正文。\n\n~>[echo] ${ququ}`,
                },
              ],
            },
          ],
        }}
      />,
    );

    expect(
      screen.getByRole("button", { name: "再说一点" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "更多蛐蛐操作" })).toBeNull();
    expect(screen.queryByText("说中了")).toBeNull();
    expect(screen.queryByText("没说中")).toBeNull();
  });

  it("keeps a late continuation on its original chat after navigation", async () => {
    const ququ = "同一句也不能串到别的会话。";
    const makeCard = (responseId: string, messageId: string) => (
      <QuquResponseCard
        data={{
          id: responseId,
          status: "completed",
          output: [
            {
              id: messageId,
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `正文。\n\n~>[echo] ${ququ}`,
                },
              ],
            },
          ],
        }}
      />
    );
    const session = (chatId: string) => ({
      id: chatId,
      realId: chatId,
      sessionId: `console:${chatId}`,
      userId: "default",
      channel: "console",
    });
    let resolveContinuation!: (value: Awaited<ReturnType<typeof chatApi.continueQuqu>>) => void;
    continueQuqu.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveContinuation = resolve;
      }),
    );
    chatSessionState.current = {
      sessions: [session("chat-a"), session("chat-b")],
      currentSessionId: "chat-a",
      setSessions: vi.fn(),
      setCurrentSessionId: vi.fn(),
    };

    const original = render(makeCard("response-a", "shared-message"));
    const continueButton = screen.getByRole("button", { name: "再说一点" });
    await waitFor(() => expect(continueButton).toBeEnabled());
    fireEvent.click(continueButton);
    await waitFor(() => expect(continueQuqu).toHaveBeenCalledTimes(1));
    original.unmount();

    chatSessionState.current = {
      ...chatSessionState.current,
      currentSessionId: "chat-b",
    };
    const other = render(makeCard("response-b", "shared-message"));
    await act(async () => {
      resolveContinuation({
        continuation_id: "ququ-continuation:late-a",
        request_id: "request-late-a",
        agent_id: "default",
        workspace_scope_id: "workspace-one",
        chat_id: "chat-a",
        session_id: "console:chat-a",
        user_id: "default",
        channel: "console",
        source_message_id: "shared-message",
        source_message_digest: await digestQuquSourceMessage(
          `正文。\n\n~>[echo] ${ququ}`,
        ),
        source_ququ_id: "source-ququ-late-a",
        ququ_ordinal: 0,
        placement: "echo",
        text: "只属于原回答的迟到续念。",
        status: "completed",
        created_at: "2026-09-20T01:00:00Z",
        updated_at: "2026-09-20T01:00:01Z",
        persisted: true,
        tools_used: false,
      });
      await Promise.resolve();
    });
    expect(screen.queryByText("只属于原回答的迟到续念。")).toBeNull();

    other.unmount();
    chatSessionState.current = {
      ...chatSessionState.current,
      currentSessionId: "chat-a",
    };
    render(makeCard("response-a", "shared-message"));
    expect(
      await screen.findByText("只属于原回答的迟到续念。"),
    ).toBeInTheDocument();
    expect(continueQuqu).toHaveBeenCalledTimes(1);
  });

  it("places an echo ququ after every artifact in the assistant answer", () => {
    const echo = "正文和附件都交付完了，这句话才该落下来。";
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-echo-after-file",
          status: "completed",
          output: [
            {
              id: "message-echo-after-file",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `正文里说明了附件内容。\n\n~>[echo] ${echo}`,
                },
                {
                  type: LantaiRuntimeContentType.FILE,
                  file_url: "https://example.com/result.pdf",
                  file_name: "result.pdf",
                },
              ],
            },
          ],
        }}
      />,
    );

    const fileArtifact = container.querySelector(
      '[data-lantai-artifact="file"]',
    );
    const echoNode = screen.getByText(echo);
    expect(fileArtifact).toBeInTheDocument();
    expect(
      fileArtifact!.compareDocumentPosition(echoNode) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("renders fenced code as a distinct copyable code artifact", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-code",
          status: "completed",
          output: [
            {
              id: "message-code",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "```typescript\nconst answer = 42;\n```",
                },
              ],
            },
          ],
        }}
      />,
    );

    expect(
      container.querySelector('[data-lantai-artifact="code"]'),
    ).toBeInTheDocument();
    expect(screen.getByText("typescript")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "复制代码" }),
    ).toBeInTheDocument();
  });

  it("renders completed echarts JSON as a safe interactive artifact", async () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-chart",
          status: "completed",
          output: [
            {
              id: "message-chart",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text:
                    "```echarts\n" +
                    JSON.stringify({
                      title: "季度收入",
                      option: {
                        xAxis: { type: "category", data: ["Q1", "Q2"] },
                        yAxis: { type: "value" },
                        series: [{ type: "bar", data: [120, 168] }],
                      },
                    }) +
                    "\n```",
                },
              ],
            },
          ],
        }}
      />,
    );

    // ECharts is intentionally lazy-loaded. Under a full parallel suite its
    // transform can take longer than Testing Library's 1s default.
    await waitFor(
      () =>
        expect(
          container.querySelector('[data-lantai-artifact="echarts"]'),
        ).toBeInTheDocument(),
      { timeout: 3_000 },
    );
    expect(screen.getByText("季度收入")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "复制图表配置" }),
    ).toBeInTheDocument();
    await waitFor(
      () =>
        expect(
          screen.getByRole("button", { name: "下载图表 PNG" }),
        ).toBeEnabled(),
      { timeout: 3_000 },
    );
  });

  it("renders an analysis result reference inside the ordinary chat answer", async () => {
    const analysisId = "a".repeat(64);
    window.location.hash = "/chat/chart-owner";
    vi.mocked(chatApi.getAnalysisChart).mockResolvedValue({
      analysis_id: analysisId,
      dataset_id: "b".repeat(64),
      source_name: "sales.csv",
      source_sha256: "c".repeat(64),
      source_row_count: 2,
      sheet_name: null,
      analysis_plan: { group_by: ["region"] },
      calculated_at: "2026-09-27T00:00:00Z",
      source_status: "current",
      semantic_review: "pending",
      warnings: [],
      x_column: "region",
      y_column: "revenue",
      points: [{ label: "East", value: "10.20" }],
    });
    const { container } = render(
      <QuquResponseCard data={{
        id: "response-analysis-chart",
        status: "completed",
        output: [{
          id: "message-analysis-chart",
          type: LantaiRuntimeMessageType.MESSAGE,
          status: "completed",
          content: [{ type: LantaiRuntimeContentType.TEXT, text:
            "```analysis-chart\n" + JSON.stringify({
              analysis_id: analysisId,
              x_column: "region",
              y_column: "revenue",
              type: "bar",
            }) + "\n```",
          }],
        }],
      }} />,
    );
    await waitFor(() => expect(
      container.querySelector('[data-lantai-artifact="analysis-chart"]'),
    ).toBeInTheDocument(), { timeout: 3_000 });
    expect(screen.getByText("10.20")).toBeInTheDocument();
    expect(chatApi.getAnalysisChart).toHaveBeenCalledWith(
      "chart-owner", analysisId, "region", "revenue", expect.anything(),
    );
  });

  it("renders markdown tables as copyable and downloadable table artifacts", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-table",
          status: "completed",
          output: [
            {
              id: "message-table",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "| 名称 | 状态 |\n| --- | --- |\n| Runtime | 稳定 |",
                },
              ],
            },
          ],
        }}
      />,
    );

    expect(
      container.querySelector('[data-lantai-artifact="table"]'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "复制表格" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "下载 CSV" }),
    ).toBeInTheDocument();
  });

  it("renders document-like markdown in the shared framed artifact shell", () => {
    const content = `# Runtime 整改说明

${"这是文档正文，用于说明当前状态和实施方法。".repeat(12)}

## 验收标准

${"每一项改动都应经过测试，并保留清楚的验证证据。".repeat(8)}`;
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-document",
          status: "completed",
          output: [
            {
              id: "message-document",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: content,
                },
              ],
            },
          ],
        }}
      />,
    );

    const documentArtifact = container.querySelector(
      '[data-lantai-artifact="document"]',
    );
    expect(documentArtifact).toBeInTheDocument();
    expect(documentArtifact).toHaveClass(
      "lantai-artifact",
      "lantai-document-output",
    );
    expect(
      screen.getByRole("button", { name: "复制文档" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "下载 Markdown" }),
    ).toBeInTheDocument();
  });

  it("keeps ququ outside a document artifact and out of copied content", async () => {
    const ququ = "这句话得留在产物边界外面。";
    const documentContent = `# 总体判断

这是给用户的正式文档正文。

## 核心优势

这一部分也属于正式文档。`;
    const clipboardDescriptor = Object.getOwnPropertyDescriptor(
      navigator,
      "clipboard",
    );
    const secureContextDescriptor = Object.getOwnPropertyDescriptor(
      window,
      "isSecureContext",
    );
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });

    try {
      const { container } = render(
        <App>
          <QuquResponseCard
            data={{
              id: "response-document-with-ququ",
              status: "completed",
              output: [
                {
                  id: "message-document-with-ququ",
                  type: LantaiRuntimeMessageType.MESSAGE,
                  status: "completed",
                  content: [
                    {
                      type: LantaiRuntimeContentType.TEXT,
                      text: `~>${ququ}\n\n${documentContent}`,
                    },
                  ],
                },
              ],
            }}
          />
        </App>,
      );

      const ququNode = screen.getByText(ququ);
      const documentArtifact = container.querySelector(
        '[data-lantai-artifact="document"]',
      );
      expect(documentArtifact).toBeInTheDocument();
      expect(documentArtifact).not.toContainElement(ququNode);
      expect(documentArtifact).not.toHaveTextContent(ququ);
      expect(
        documentArtifact!.compareDocumentPosition(ququNode) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();

      fireEvent.click(screen.getByRole("button", { name: "复制文档" }));

      await waitFor(() =>
        expect(writeText).toHaveBeenCalledWith(documentContent),
      );
    } finally {
      if (clipboardDescriptor) {
        Object.defineProperty(navigator, "clipboard", clipboardDescriptor);
      } else {
        Reflect.deleteProperty(navigator, "clipboard");
      }
      if (secureContextDescriptor) {
        Object.defineProperty(
          window,
          "isSecureContext",
          secureContextDescriptor,
        );
      } else {
        Reflect.deleteProperty(window, "isSecureContext");
      }
    }
  });

  it("routes lead and echo ququ around a document artifact", () => {
    const lead = "这份方案先锁边界，再展开细节。";
    const echo = "边界说清楚了，后面的事才不会越做越乱。";
    const documentContent = `# 实施方案

这是正式交付给用户的文档正文。

## 验收标准

每项改动都必须经过测试和构建验证。`;
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-document-semantic-ququ",
          status: "completed",
          output: [
            {
              id: "message-document-semantic-ququ",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `~>[lead] ${lead}\n\n${documentContent}\n\n~>[echo] ${echo}`,
                },
              ],
            },
          ],
        }}
      />,
    );

    const documentArtifact = container.querySelector(
      '[data-lantai-artifact="document"]',
    );
    const leadNode = screen.getByText(lead);
    const echoNode = screen.getByText(echo);
    expect(documentArtifact).toBeInTheDocument();
    expect(documentArtifact).not.toContainElement(leadNode);
    expect(documentArtifact).not.toContainElement(echoNode);
    expect(
      leadNode.compareDocumentPosition(documentArtifact!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      documentArtifact!.compareDocumentPosition(echoNode) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("treats a concise explicitly titled checklist as a document", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-checklist",
          status: "completed",
          output: [
            {
              id: "message-checklist",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `# 上线检查清单

- 确认生产环境配置已经完成并通过复核
- 运行核心对话与权限流程的自动化测试
- 核对安装包版本、签名状态和产物哈希
- 完成回滚演练并记录负责人和恢复时间`,
                },
              ],
            },
          ],
        }}
      />,
    );

    expect(
      container.querySelector('[data-lantai-artifact="document"]'),
    ).toBeInTheDocument();
  });

  it("uses document semantics for a short explicitly categorized document", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-short-minutes",
          status: "completed",
          output: [
            {
              id: "message-short-minutes",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "# 会议纪要\n\n已确认。",
                },
              ],
            },
          ],
        }}
      />,
    );

    expect(
      container.querySelector('[data-lantai-artifact="document"]'),
    ).toBeInTheDocument();
  });

  it("renders runtime files with type, open, and download actions", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-file",
          status: "completed",
          output: [
            {
              id: "message-file",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.FILE,
                  file_url: "https://example.com/report.xlsx",
                  file_name: "可靠性报告.xlsx",
                  file_size: 4096,
                },
              ],
            },
          ],
        }}
      />,
    );

    expect(
      container.querySelector('[data-lantai-artifact="file"]'),
    ).toBeInTheDocument();
    expect(screen.getByText("可靠性报告.xlsx")).toBeInTheDocument();
    expect(screen.getByText("XLSX · 4.0 KB")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开文件" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载文件" })).toBeInTheDocument();
  });

  it("keeps a preserved file and surfaces a later partial failure", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-partial-file",
          status: "failed",
          output: [
            {
              id: "message-file",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.FILE,
                  file_url: "https://example.com/resume.pdf",
                  file_name: "简历.pdf",
                },
              ],
            },
            {
              id: "error-after-file",
              type: LantaiRuntimeMessageType.ERROR,
              status: "completed",
              metadata: { partial_success: true },
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "部分完成：已生成的产物已保留。后续步骤失败：PDF 校验脚本失败",
                },
              ],
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("简历.pdf")).toBeInTheDocument();
    expect(screen.getByText(/PDF 校验脚本失败/)).toBeInTheDocument();
    expect(
      container.querySelector('.lantai-response-status[data-tone="warning"]'),
    ).toBeInTheDocument();
  });

  it.each(["code", "error_code"])(
    "localizes a completion failure from the %s runtime protocol without inventing a file",
    (codeField) => {
      const rawGateError =
        "A durable artifact was requested, but no successful finalize_deliverable evidence exists.";
      const { container } = render(
        <QuquResponseCard
          data={{
            id: "response-missing-ppt",
            status: "failed",
            error: {
              [codeField]: "completion_gate_failed",
              error_type: "RuntimeError",
              message: rawGateError,
            },
            output: [],
          }}
        />,
      );

      expect(screen.getByText("交付校验未完成")).toBeInTheDocument();
      expect(screen.getByText(/没有确认到可用文件/)).toBeInTheDocument();
      expect(screen.queryByText(rawGateError)).not.toBeInTheDocument();
      expect(screen.queryByText("文件已生成并保留")).not.toBeInTheDocument();
      expect(
        container.querySelector('[data-lantai-artifact="file"]'),
      ).not.toBeInTheDocument();
    },
  );

  it.each(["code", "error_code"])(
    "preserves a delivered PPT and replaces %s gate internals with a quality warning",
    (codeField) => {
      const rawGateError =
        "A durable artifact was requested, but no successful finalize_deliverable evidence exists.";
      const { container } = render(
        <QuquResponseCard
          data={{
            id: "response-delivered-ppt-warning",
            status: "failed",
            error: {
              [codeField]: "completion_gate_failed",
              message: rawGateError,
            },
            output: [
              {
                id: "message-delivered-ppt",
                type: LantaiRuntimeMessageType.MESSAGE,
                status: "completed",
                content: [
                  {
                    type: LantaiRuntimeContentType.FILE,
                    file_url: "file:///workspace/presentation.pptx",
                    file_name: "presentation.pptx",
                  },
                ],
              },
            ],
          }}
        />,
      );

      expect(screen.getByText("presentation.pptx")).toBeInTheDocument();
      expect(screen.getByText("文件已生成并保留")).toBeInTheDocument();
      expect(screen.getByText(/质量校验尚未全部通过/)).toBeInTheDocument();
      expect(screen.queryByText(rawGateError)).not.toBeInTheDocument();
      expect(screen.queryByText("这次没能完成")).not.toBeInTheDocument();
      expect(
        container.querySelector('.lantai-response-status[data-tone="warning"]'),
      ).toBeInTheDocument();
    },
  );

  it("keeps ordinary explanatory lists in the normal response surface", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-ordinary-list",
          status: "completed",
          output: [
            {
              id: "message-ordinary-list",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `可以从下面几个方向考虑：

- 先确认目标
- 再收集材料
- 最后执行验证`,
                },
              ],
            },
          ],
        }}
      />,
    );

    expect(
      container.querySelector('[data-lantai-artifact="text"]'),
    ).not.toBeInTheDocument();
  });

  it("upgrades a completed model-classified poem without touching its text", async () => {
    vi.useFakeTimers();
    try {
      const sourceText = `《夜航》

江灯摇碎一船星，
风送潮声入梦听。
雾尽长天月满汀。`;
      classifyUtilityArtifacts.mockResolvedValue({
        status: "classified",
        model: "utility/test-model",
        artifacts: [
          {
            kind: "creative",
            title: "夜航",
            source_text: sourceText,
            start: 0,
            end: sourceText.length,
          },
        ],
      });

      const { container } = render(
        <QuquResponseCard
          isLast
          data={{
            id: "response-classified-poem",
            status: "completed",
            output: [
              {
                id: "message-classified-poem",
                type: LantaiRuntimeMessageType.MESSAGE,
                status: "completed",
                content: [
                  { type: LantaiRuntimeContentType.TEXT, text: sourceText },
                ],
              },
            ],
          }}
        />,
      );

      expect(classifyUtilityArtifacts).not.toHaveBeenCalled();
      expect(
        container.querySelector('[data-lantai-artifact="text"]'),
      ).not.toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1200);
      });

      const artifact = container.querySelector(
        '[data-lantai-artifact="text"][data-lantai-artifact-kind="creative"]',
      );
      expect(artifact).toBeInTheDocument();
      expect(artifact).toHaveTextContent("江灯摇碎一船星");
      expect(
        screen.getByRole("button", { name: "复制作品" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "下载文本" }),
      ).toBeInTheDocument();
      expect(classifyUtilityArtifacts).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("adds one content-specific note after a completed research report", async () => {
    vi.useFakeTimers();
    try {
      const report = `# 竞品研究报告

## 结论

根据官方定价页和近期实测，A 方案当前更适合小团队。`;
      const note = "定价和套餐范围会变动，发布前应重新核对报告中的小团队价格。";
      classifyUtilityArtifacts.mockResolvedValue({
        status: "classified",
        model: "utility/test-model",
        artifacts: [],
        advisory: { label: "注意", body: note },
      });

      const { container } = render(
        <QuquResponseCard
          isLast
          data={{
            id: "response-research-advisory",
            status: "completed",
            output: [
              {
                id: "message-research-advisory",
                type: LantaiRuntimeMessageType.MESSAGE,
                status: "completed",
                content: [
                  { type: LantaiRuntimeContentType.TEXT, text: report },
                ],
              },
            ],
          }}
        />,
      );

      expect(
        container.querySelector('[data-lantai-research-advisory="1"]'),
      ).not.toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1200);
      });

      const advisory = container.querySelector(
        '[data-lantai-research-advisory="1"]',
      );
      expect(advisory).toBeInTheDocument();
      expect(advisory).toHaveTextContent("注意");
      expect(advisory).toHaveTextContent(note);
      expect(classifyUtilityArtifacts).toHaveBeenCalledTimes(1);
      expect(
        container.querySelector('[data-lantai-artifact="document"]'),
      ).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("runs the auxiliary presentation analysis at most once per response", async () => {
    vi.useFakeTimers();
    try {
      const note = "样本只覆盖现有三家供应商，不能外推到全部市场。";
      classifyUtilityArtifacts.mockResolvedValue({
        status: "classified",
        model: "utility/test-model",
        artifacts: [],
        advisory: { label: "提示", body: note },
      });

      const { container } = render(
        <QuquResponseCard
          isLast
          data={{
            id: "response-single-utility-analysis",
            status: "completed",
            output: [
              {
                id: "message-progress-copy",
                type: LantaiRuntimeMessageType.MESSAGE,
                status: "completed",
                content: [
                  {
                    type: LantaiRuntimeContentType.TEXT,
                    text: "我先核对了官方资料。",
                  },
                ],
              },
              {
                id: "message-final-report",
                type: LantaiRuntimeMessageType.MESSAGE,
                status: "completed",
                content: [
                  {
                    type: LantaiRuntimeContentType.TEXT,
                    text: "# 供应商研究报告\n\n## 结论\n\n三家供应商各有优势。",
                  },
                  {
                    type: LantaiRuntimeContentType.TEXT,
                    text: "## 建议\n\n先小范围试用 A 方案。",
                  },
                ],
              },
            ],
          }}
        />,
      );

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1200);
      });

      expect(classifyUtilityArtifacts).toHaveBeenCalledTimes(1);
      expect(
        container.querySelectorAll('[data-lantai-research-advisory="1"]'),
      ).toHaveLength(1);
      expect(screen.getByText(note)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps ququ out of automatic semantic work classification and content", async () => {
    vi.useFakeTimers();
    try {
      const lead = "这几行先别急着解释。";
      const echo = "写完以后，风才真的停下来。";
      const sourceText = `《夜航》

江灯摇碎一船星，
风送潮声入梦听。`;
      classifyUtilityArtifacts.mockResolvedValue({
        status: "classified",
        model: "utility/test-model",
        artifacts: [
          {
            kind: "creative",
            title: "夜航",
            source_text: sourceText,
            start: 0,
            end: sourceText.length,
          },
        ],
      });

      const { container } = render(
        <QuquResponseCard
          isLast
          data={{
            id: "response-classified-poem-with-ququ",
            status: "completed",
            output: [
              {
                id: "message-classified-poem-with-ququ",
                type: LantaiRuntimeMessageType.MESSAGE,
                status: "completed",
                content: [
                  {
                    type: LantaiRuntimeContentType.TEXT,
                    text: `~>[lead] ${lead}\n\n${sourceText}\n\n~>[echo] ${echo}`,
                  },
                ],
              },
            ],
          }}
        />,
      );

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1200);
      });

      const artifact = container.querySelector(
        '[data-lantai-artifact="text"][data-lantai-artifact-kind="creative"]',
      );
      expect(classifyUtilityArtifacts).toHaveBeenCalledTimes(1);
      expect(classifyUtilityArtifacts.mock.calls[0][0]).toBe(sourceText);
      expect(artifact).toBeInTheDocument();
      expect(artifact).toHaveTextContent("江灯摇碎一船星");
      expect(artifact).not.toHaveTextContent(lead);
      expect(artifact).not.toHaveTextContent(echo);
      expect(screen.getByText(lead)).toBeInTheDocument();
      expect(screen.getByText(echo)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels the hidden classifier when a new turn supersedes the response", async () => {
    vi.useFakeTimers();
    try {
      const text =
        "这是一段足够长的候选文本，用来验证用户继续对话时，后台作品识别不会抢占下一轮模型请求。".repeat(
          3,
        );
      const data = {
        id: "response-classifier-cancelled",
        status: "completed",
        output: [
          {
            id: "message-classifier-cancelled",
            type: LantaiRuntimeMessageType.MESSAGE,
            status: "completed",
            content: [{ type: LantaiRuntimeContentType.TEXT, text }],
          },
        ],
      };
      const { rerender } = render(<QuquResponseCard isLast data={data} />);

      rerender(<QuquResponseCard isLast={false} data={data} />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1500);
      });

      expect(classifyUtilityArtifacts).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders context compaction as a compact system action", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-compact-action",
          status: "completed",
          output: [
            {
              id: "message-compact-action",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              metadata: {
                source: "legacy",
                metadata: {
                  system_command: "compact",
                  system_action: {
                    kind: "context_compaction",
                    status: "completed",
                    title: "对话已压缩",
                    detail: "后续对话会继续使用这份上下文摘要。",
                    message_count: 9,
                    before_tokens: 747,
                    after_tokens: 163,
                    saved_percent: 78,
                    summary:
                      "# 当前目标\n\n保留任务目标。\n\n# 已知问题\n\n（无）",
                  },
                },
              },
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "对话已压缩：9 条消息，747 → 163 Token。",
                },
              ],
            },
          ],
        }}
      />,
    );

    const action = container.querySelector(
      '[data-lantai-system-action="context_compaction"]',
    );
    expect(action).toBeInTheDocument();
    expect(action).toHaveTextContent("对话已压缩");
    expect(action).toHaveTextContent("9 条消息");
    expect(action).toHaveTextContent("747 → 163 Token");
    expect(
      container.querySelector('[data-lantai-artifact="document"]'),
    ).not.toBeInTheDocument();
    expect(
      container.querySelector('[data-lantai-response-avatar="1"]'),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("保留任务目标。")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "查看摘要" }));

    expect(screen.getByText("保留任务目标。")).toBeInTheDocument();
    expect(screen.queryByText("已知问题")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起摘要" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("renders degraded automatic compaction as a visible warning", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-degraded-compaction",
          status: "completed",
          output: [
            {
              id: "message-degraded-compaction",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              metadata: {
                system_action: {
                  kind: "context_compaction",
                  status: "degraded",
                  title: "上下文已降级整理",
                  detail:
                    "已保留最近 5 条消息继续回答，较早内容未进入本轮上下文。",
                  technical_reason: "utility model unavailable",
                },
              },
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "上下文已降级整理。",
                },
              ],
            },
          ],
        }}
      />,
    );

    const action = container.querySelector(
      '[data-lantai-system-action="context_compaction"]',
    );
    expect(action).toHaveAttribute("data-status", "degraded");
    expect(action).toHaveTextContent("上下文已降级整理");
    expect(action).toHaveTextContent("较早内容未进入本轮上下文");
    expect(screen.getByText("技术详情")).toBeInTheDocument();
  });

  it("renders collaboration distillation as a visible assistant workflow", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-distillation",
          status: "in_progress",
          output: [
            {
              id: "message-distillation",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "in_progress",
              metadata: {
                system_action: {
                  kind: "collaboration_distillation",
                  status: "running",
                  show_avatar: true,
                  title: "沈行知正在沉淀这段协作",
                  detail: "正在提炼用户纠正。",
                  steps: [
                    {
                      id: "collect",
                      title: "读取会话范围",
                      status: "completed",
                    },
                    {
                      id: "distill",
                      title: "提炼纠正与稳定方法",
                      status: "running",
                      detail: "已经过 3 秒。",
                    },
                  ],
                },
              },
              content: [{ type: LantaiRuntimeContentType.TEXT, text: "" }],
            },
          ],
        }}
      />,
    );

    expect(
      container.querySelector(
        '[data-lantai-system-action="collaboration_distillation"]',
      ),
    ).toHaveAttribute("data-status", "running");
    expect(screen.getByText("沈行知正在沉淀这段协作")).toBeInTheDocument();
    expect(screen.getByText("读取会话范围")).toBeInTheDocument();
    expect(screen.getByText("提炼纠正与稳定方法")).toBeInTheDocument();
    expect(screen.getByText("已经过 3 秒。")).toBeInTheDocument();
  });

  it("keeps other system command output out of document classification", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-history-command",
          status: "completed",
          output: [
            {
              id: "message-history-command",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              metadata: {
                source: "legacy",
                metadata: {
                  system_command: "history",
                },
              },
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: "# 第一段\n\n内容一\n\n# 第二段\n\n内容二",
                },
              ],
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("内容一")).toBeInTheDocument();
    expect(screen.getByText("内容二")).toBeInTheDocument();
    expect(
      container.querySelector('[data-lantai-artifact="document"]'),
    ).not.toBeInTheDocument();
  });

  it("upgrades persisted legacy compact output to the system action surface", () => {
    const { container } = render(
      <QuquResponseCard
        data={{
          id: "response-legacy-compact",
          status: "completed",
          output: [
            {
              id: "message-legacy-compact",
              type: LantaiRuntimeMessageType.MESSAGE,
              status: "completed",
              content: [
                {
                  type: LantaiRuntimeContentType.TEXT,
                  text: `✅ **Compact Complete!**

- Messages compacted: 9
- Tokens: 747/262.1k(0%) -> 163/262.1k(0%)
**Compressed Summary:**
# 当前目标

保留用户目标。

# 工作环境

（无）
- Summary task started in background`,
                },
              ],
            },
          ],
        }}
      />,
    );

    expect(
      container.querySelector(
        '[data-lantai-system-action="context_compaction"]',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Compact Complete!")).not.toBeInTheDocument();
    expect(
      container.querySelector('[data-lantai-artifact="document"]'),
    ).not.toBeInTheDocument();
  });
});
