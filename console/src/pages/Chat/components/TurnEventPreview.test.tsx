import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { buildNativeRuntimeStreamMessages } from "../runtimeEventStream";
import { getRuntimeStallState } from "../runtimeStall";
import TurnEventPreview, {
  buildExecutionStages,
  buildPresencePresentation,
  buildProcessTrace,
  buildWorkbenchOverview,
  buildWorkPhaseSummary,
  latestAuthoredPresenceText,
  publicProgressHistory,
} from "./TurnEventPreview";

const {
  defaultSessionState,
  mockContinueRun,
  mockInterruptRun,
  mockInspectPreview,
  mockMessageWarning,
  mockUpdateSession,
  mockUseChatAnywhereSessionsState,
} = vi.hoisted(() => ({
  defaultSessionState: () => ({
    currentSessionId: "chat-1",
    sessions: [
      {
        id: "chat-1",
        turnEventPreview: {
          workspace_dir: "/tmp/topic-workspace",
          summary_line: "本轮已开始",
          events: [
            {
              id: "evt-1",
              ts: "2026-05-21T10:00:00Z",
              kind: "chat.turn.started",
              status: "running",
              label: "开始运行",
              detail: "文本：请继续整理方案",
            },
          ],
        },
      },
    ],
  }),
  mockContinueRun: vi.fn(),
  mockInterruptRun: vi.fn(),
  mockInspectPreview: vi.fn(),
  mockMessageWarning: vi.fn(),
  mockUpdateSession: vi.fn(),
  mockUseChatAnywhereSessionsState: vi.fn(),
}));

mockUseChatAnywhereSessionsState.mockImplementation(defaultSessionState);

vi.mock("../../../api/modules/runtime", () => ({
  runtimeApi: {
    interruptRun: mockInterruptRun,
    retryRun: vi.fn(),
    continueRun: mockContinueRun,
    inspectPreview: mockInspectPreview,
  },
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: {
      success: vi.fn(),
      warning: mockMessageWarning,
      error: vi.fn(),
    },
  }),
}));

vi.mock("../sessionApi", () => ({
  default: {
    updateSession: mockUpdateSession,
    setLastUserMessage: vi.fn(),
    saveDraftText: vi.fn(),
  },
}));

const mockNavigate = vi.fn();

vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

vi.mock("@/vendor/lantaiChat", () => ({
  useChatAnywhereSessionsState: mockUseChatAnywhereSessionsState,
}));

describe("TurnEventPreview", () => {
  it("keeps the compact line free of ticking elapsed metadata", () => {
    render(<TurnEventPreview variant="presence" />);
    const rail = screen.getByRole("button", { name: /本轮工作过程/ });
    expect(rail).not.toHaveTextContent("已运行");
    expect(rail).toHaveTextContent("查看过程");
  });

  it("preserves long public wording including its final qualification", () => {
    const text = "正在核对资料。".repeat(30) + "尚未确认，请勿据此提交。";
    expect(
      buildPresencePresentation({
        status: "running",
        stageTitle: "工具调用",
        authoredText: text,
        stall: { level: "healthy" } as any,
      })?.text,
    ).toBe(text);
  });

  it("shows the local send receipt before timestamps or runtime identity arrive", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "local-send",
      sessions: [
        {
          id: "local-send",
          generating: true,
          messages: [
            { id: "local-user", role: "user", cards: [] },
            {
              id: "local-assistant",
              role: "assistant",
              cards: [
                {
                  code: "LantaiRuntimeResponseCard",
                  data: {
                    status: "in_progress",
                    output: [],
                    metadata: { optimistic_pending: true },
                  },
                },
              ],
            },
          ],
        },
      ],
    } as any);
    render(<TurnEventPreview variant="presence" />);
    expect(
      screen.getByRole("button", { name: /本轮工作过程/ }),
    ).toHaveTextContent("正在发送");
  });

  it("does not invent a completion percentage from an open-ended task", () => {
    render(<TurnEventPreview />);
    openTraceDrawer();
    expect(screen.queryByRole("progressbar", { name: "本轮进度" })).toBeNull();
    expect(screen.getAllByText("0 项已完成").length).toBeGreaterThan(0);
  });

  it("describes first-output waiting without claiming semantic understanding", () => {
    expect(
      buildPresencePresentation({
        status: "running",
        stageTitle: "开始运行",
        stageLabel: "理解中",
        stall: { level: "healthy", waitingForFirstOutput: true } as any,
      })?.stage,
    ).toBe("等待回应");
  });

  it("does not revive tool-bound prose when a resync no longer confirms its tool", () => {
    const now = new Date().toISOString();
    const events = [
      {
        id: "tool-start",
        seq: 1,
        ts: now,
        run_id: "run-expiry",
        kind: "chat.tool_call.started",
        status: "running",
        label: "工具调用",
        detail: "read_file",
        payload: { id: "call-expiry", tool_name: "read_file" },
      },
    ];
    const messages = buildNativeRuntimeStreamMessages([
      {
        id: "progress-expiry",
        seq: 2,
        created_at: now,
        type: "message.completed",
        status: "completed",
        source: "assistant",
        session_id: "console:expiry",
        turn_id: "run-expiry",
        item_id: "progress-expiry",
        payload: {
          content_type: "thinking",
          stream_message_type: "reasoning",
          public_progress: {
            schema_version: 2,
            id: "progress-expiry",
            revision: 1,
            source: "runtime_facts",
            text_origin: "assistant_public_text",
            tool_call_ids: ["call-expiry"],
          },
          message: {
            id: "progress-expiry",
            role: "assistant",
            content: [
              {
                type: "thinking",
                text: "找到路线了，我再看看黄山的登山时间。",
              },
            ],
          },
        },
      },
    ] as any);
    const session = {
      id: "chat-expiry",
      messages,
      runtimeState: {
        status: "running",
        run_id: "run-expiry",
        started_at: now,
        updated_at: now,
        transport_status: "connected",
        events_preview: events,
      },
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: session.id,
      sessions: [session],
    } as any);
    const { rerender } = render(<TurnEventPreview variant="presence" />);
    expect(
      screen.getByRole("button", { name: /本轮工作过程/ }),
    ).toHaveTextContent("找到路线了");
    session.runtimeState = { ...session.runtimeState, events_preview: [] };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: session.id,
      sessions: [{ ...session }],
    } as any);
    rerender(<TurnEventPreview variant="presence" />);
    expect(
      screen.getByRole("button", { name: /本轮工作过程/ }),
    ).not.toHaveTextContent("找到路线了");
  });

  afterEach(() => {
    mockContinueRun.mockReset();
    mockInterruptRun.mockReset();
    mockUpdateSession.mockReset();
    mockUseChatAnywhereSessionsState.mockReset();
    mockUseChatAnywhereSessionsState.mockImplementation(defaultSessionState);
  });

  function openTraceDrawer() {
    fireEvent.click(screen.getByRole("button", { name: /打开本轮执行/ }));
  }

  it("summarizes delivered files for ordinary users while preserving technical evidence", () => {
    const overview = buildWorkbenchOverview({
      status: "completed",
      outcomeStatus: "delivered",
      deliveryStatus: "verified",
      summary: "本轮已完成",
      stages: [
        {
          id: "file",
          time: "10:00",
          timestampMs: 1,
          phase: "file",
          title: "工具结果",
          status: "completed",
          detail: "send_file_to_user",
          debug: {
            kind: "chat.tool_call.finished",
            payload: { tool_name: "send_file_to_user" },
          },
        },
      ],
    });

    expect(overview).toMatchObject({
      label: "本轮结果",
      headline: "任务成功，成果已交付",
      nextStep: "在当前聊天中打开或下载交付内容。",
      results: [
        {
          title: "交付文件",
          detail: "文件已发送到当前聊天，可以直接打开或下载。",
        },
      ],
    });
  });

  it("uses the real tool event shape and never reports a failed file send as delivered", () => {
    const sendEvent = {
      id: "send-file",
      ts: "2026-09-07T01:00:00Z",
      kind: "chat.tool_call.finished",
      status: "failed",
      label: "工具结果",
      detail: "send_file_to_user",
      payload: {
        phase: "tool",
        tool_name: "send_file_to_user",
      },
    };
    const failedTrace = buildProcessTrace({
      summary_line: "交付失败",
      events: [sendEvent],
    });
    expect(failedTrace.steps[0].phase).toBe("file");
    expect(
      buildWorkbenchOverview({
        status: "failed",
        outcomeStatus: "blocked",
        stages: buildExecutionStages(failedTrace.steps, "failed"),
      }).results,
    ).toEqual([]);
    const recoveredStages = buildExecutionStages(
      failedTrace.steps,
      "completed",
    );
    expect(recoveredStages[0].status).toBe("recovered");
    expect(
      buildWorkbenchOverview({
        status: "completed",
        outcomeStatus: "delivered",
        stages: recoveredStages,
      }).results,
    ).toEqual([]);

    const completedTrace = buildProcessTrace({
      summary_line: "交付完成",
      events: [
        {
          ...sendEvent,
          id: "send-file-completed",
          status: "completed",
          detail: "文件已发送",
        },
      ],
    });
    expect(
      buildWorkbenchOverview({
        status: "completed",
        outcomeStatus: "delivered",
        stages: buildExecutionStages(completedTrace.steps, "completed"),
      }).results,
    ).toEqual([
      {
        title: "交付文件",
        detail: "文件已发送到当前聊天，可以直接打开或下载。",
      },
    ]);
  });

  it("keeps unmerged public events for the technical evidence count", () => {
    const lifecycle = buildProcessTrace({
      summary_line: "读取资料",
      events: [
        {
          id: "read-started",
          ts: "2026-09-07T01:00:00Z",
          kind: "chat.tool_call.started",
          status: "running",
          label: "工具调用",
          detail: "read_file",
          payload: { phase: "tool", tool_name: "read_file", id: "call-1" },
        },
        {
          id: "read-finished",
          ts: "2026-09-07T01:00:01Z",
          kind: "chat.tool_call.finished",
          status: "completed",
          label: "工具结果",
          detail: "/tmp/reference.md",
          payload: { phase: "tool", tool_name: "read_file", id: "call-1" },
        },
      ],
    });

    expect(lifecycle.steps).toHaveLength(1);
    expect(lifecycle.rawSteps).toHaveLength(2);
    expect(lifecycle.rawSteps.map((step) => step.debug.id)).toEqual([
      "read-started",
      "read-finished",
    ]);
  });

  it("does not count a recovered failed send as a delivered file", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-recovered-send",
      sessions: [
        {
          id: "chat-recovered-send",
          runtimeState: {
            status: "completed",
            outcome_status: "delivered",
            run_id: "run-recovered-send",
            summary: "回答已经完成",
            events_preview: [
              {
                id: "send-failed-before-answer",
                ts: "2026-09-07T01:00:00Z",
                run_id: "run-recovered-send",
                kind: "chat.tool_call.finished",
                status: "failed",
                label: "工具结果",
                detail: "send_file_to_user",
                payload: {
                  phase: "tool",
                  tool_name: "send_file_to_user",
                },
              },
              {
                id: "answer-completed-after-send",
                ts: "2026-09-07T01:00:01Z",
                run_id: "run-recovered-send",
                kind: "chat.final_answer",
                status: "completed",
                label: "最终回复",
                detail: "回答已经完成",
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);
    openTraceDrawer();

    expect(screen.queryByText("1 项交付")).toBeNull();
    expect(
      screen.getByText(
        "这次文件发送没有成功；本轮后来已恢复，请以聊天中的实际附件为准。",
      ),
    ).toBeInTheDocument();
  });

  it("makes failures and unknown outcomes explicit without inventing completion", () => {
    expect(
      buildWorkbenchOverview({
        status: "failed",
        outcomeStatus: "blocked",
        summary: "运行失败",
        stages: [],
      }),
    ).toMatchObject({
      label: "需要处理",
      headline: "这轮还没有完成",
      nextStep: "查看问题说明，再选择继续处理或重新尝试。",
    });
    expect(
      buildWorkbenchOverview({
        status: "finished",
        outcomeStatus: "unknown",
        summary: "本轮已结束",
        stages: [],
      }).headline,
    ).toBe("回复已结束");
  });

  it("keeps the running overview neutral for a custom agent identity", () => {
    expect(
      buildWorkbenchOverview({
        status: "running",
        outcomeStatus: "in_progress",
        stages: [],
      }).headline,
    ).toBe("正在完成这轮工作");
  });

  it("does not present an internal file write as a delivered result", () => {
    const overview = buildWorkbenchOverview({
      status: "completed",
      outcomeStatus: "delivered",
      stages: [
        {
          id: "write-internal-file",
          time: "10:00",
          timestampMs: 1,
          phase: "file",
          title: "工具结果",
          status: "completed",
          detail: "/tmp/internal-draft.md",
          debug: {
            kind: "chat.tool_call.finished",
            payload: { phase: "tool", tool_name: "write_file" },
          },
        },
      ],
    });

    expect(overview.results).toEqual([]);
    expect(overview.headline).toBe("任务成功，成果已交付");
    expect(overview.nextStep).toBe("结果已显示在当前聊天回复中。");
  });

  it("does not call a mixed pending phase completed", () => {
    const summary = buildWorkPhaseSummary([
      { phase: "test", status: "completed" },
      { phase: "build", status: "queued" },
    ] as any);

    expect(summary.find((phase) => phase.id === "verify")).toEqual(
      expect.objectContaining({
        count: 1,
        total: 2,
        status: "pending",
      }),
    );
  });

  it.each([false, true])(
    "uses assistant-authored public progress despite a stale sending marker (%s)",
    (optimisticPending) => {
      const now = new Date().toISOString();
      mockUseChatAnywhereSessionsState.mockReturnValue({
        currentSessionId: "chat-presence",
        sessions: [
          {
            id: "chat-presence",
            generating: true,
            messages: [
              {
                id: "assistant-presence",
                role: "assistant",
                msgStatus: "loading",
                cards: [
                  {
                    code: "LantaiRuntimeResponseCard",
                    data: {
                      status: "in_progress",
                      metadata: {
                        run_id: "run-presence",
                        optimistic_pending: optimisticPending,
                      },
                      output: [
                        {
                          id: "progress-1",
                          type: "reasoning",
                          status: "in_progress",
                          metadata: {
                            run_id: "run-presence",
                            public_projection: "runtime_progress_v2",
                            public_progress_id: "progress-1",
                            public_progress_revision: 1,
                            source: "runtime_facts",
                            private_reasoning_exposed: false,
                            text_origin: "assistant_public_text",
                            tool_call_ids: ["call-read"],
                          },
                          content: [
                            {
                              type: "thinking",
                              text: "我把材料读完了，正在把冲突的口径逐项对齐。",
                            },
                          ],
                        },
                      ],
                    },
                  },
                ],
              },
            ],
            runtimeState: {
              status: "running",
              run_id: "run-presence",
              started_at: now,
              updated_at: now,
              transport_status: "connected",
              events_preview: [
                {
                  id: "evt-presence",
                  ts: now,
                  kind: "chat.tool_call.started",
                  status: "running",
                  label: "工具调用",
                  detail: "read_file",
                  payload: { id: "call-read", tool_name: "read_file" },
                },
              ],
            },
          },
        ],
      } as any);

      render(<TurnEventPreview variant="presence" />);

      const presence = screen.getByRole("button", {
        name: /打开沈行知的本轮工作过程/,
      });
      expect(presence).toHaveAttribute("data-authored", "1");
      expect(presence).toHaveAttribute("aria-describedby");
      expect(within(presence).queryByText("沈行知")).toBeNull();
      expect(presence).toHaveAccessibleName("打开沈行知的本轮工作过程");
      expect(within(presence).queryByText("沈")).toBeNull();
      expect(within(presence).queryByText("正在读取文件")).toBeNull();
      expect(within(presence).queryByText("刚刚更新")).toBeNull();
      expect(
        within(presence).getByText(
          "我把材料读完了，正在把冲突的口径逐项对齐。",
        ),
      ).toBeInTheDocument();
      expect(within(presence).getByText("查看过程")).toBeInTheDocument();
      expect(presence.querySelector("img")).toBeNull();
      expect(screen.getByRole("status")).toHaveTextContent(
        "沈行知：我把材料读完了，正在把冲突的口径逐项对齐。；正在读取文件",
      );

      fireEvent.click(presence);
      expect(screen.getByText("沈行知 · 本轮工作")).toBeInTheDocument();
    },
  );

  it.each([
    "running",
    "stopping",
    "needs_input",
    "failed",
    "interrupted",
    "incomplete",
    "blocked",
    "stalled",
    "idle",
  ])("does not duplicate %s status inside the message flow", (status) => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-inline-presence",
      sessions: [
        {
          id: "chat-inline-presence",
          runtimeState: {
            status,
            run_id: "run-inline-presence",
            events_preview: [],
          },
        },
      ],
    } as any);
    render(
      <>
        <TurnEventPreview variant="inline" />
        <TurnEventPreview variant="presence" />
      </>,
    );
    expect(screen.queryByTestId("inline-turn-progress")).toBeNull();
    if (status !== "idle") {
      expect(
        screen.getByRole("button", { name: /打开沈行知的本轮工作过程/ }),
      ).toBeInTheDocument();
    }
  });

  it.each(["completed", "finished"])(
    "keeps the %s process accessible in the message after the turn ends",
    (status) => {
      mockUseChatAnywhereSessionsState.mockReturnValue({
        currentSessionId: "chat-inline-presence",
        sessions: [
          {
            id: "chat-inline-presence",
            runtimeState: {
              status,
              run_id: "run-inline-presence",
              events_preview: [],
            },
          },
        ],
      } as any);
      render(
        <>
          <TurnEventPreview variant="inline" />
          <TurnEventPreview variant="presence" />
        </>,
      );
      const inline = screen.getByTestId("inline-turn-progress");
      expect(inline).toHaveAttribute("aria-busy", "false");
      expect(inline).not.toHaveTextContent(/正在|开始运行/);
      expect(
        screen.queryByRole("button", { name: /打开沈行知的本轮工作过程/ }),
      ).toBeNull();
      fireEvent.click(inline);
      expect(screen.getByText("沈行知 · 本轮工作")).toBeInTheDocument();
    },
  );

  it("keeps an open process drawer visible while its entry moves after completion", () => {
    const session = {
      id: "chat-drawer-transition",
      runtimeState: {
        status: "running",
        run_id: "run-drawer-transition",
        events_preview: [],
      },
    };
    const update = () =>
      mockUseChatAnywhereSessionsState.mockReturnValue({
        currentSessionId: session.id,
        sessions: [{ ...session }],
      } as any);
    const surfaces = (
      <>
        <TurnEventPreview variant="presence" />
        <TurnEventPreview variant="inline" />
      </>
    );
    update();
    const { rerender } = render(surfaces);
    fireEvent.click(
      screen.getByRole("button", { name: /打开沈行知的本轮工作过程/ }),
    );
    expect(screen.getByText("沈行知 · 本轮工作")).toBeInTheDocument();
    session.runtimeState = { ...session.runtimeState, status: "completed" };
    update();
    rerender(
      <>
        <TurnEventPreview variant="presence" />
        <TurnEventPreview variant="inline" />
      </>,
    );
    expect(screen.getByText("沈行知 · 本轮工作")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /打开沈行知的本轮工作过程/ }),
    ).toBeNull();
    expect(screen.getByTestId("inline-turn-progress")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭工作台" }));
    fireEvent.click(screen.getByTestId("inline-turn-progress"));
    expect(screen.getByText("沈行知 · 本轮工作")).toBeInTheDocument();
  });

  it("keeps matching runtime progress authoritative over an early terminal response", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-terminal-response",
      sessions: [
        {
          id: "chat-terminal-response",
          messages: [
            {
              id: "user-terminal-response",
              role: "user",
              createdAt: now,
              metadata: { client_turn_id: "turn-terminal-response" },
              cards: [
                {
                  code: "LantaiRuntimeRequestCard",
                  data: {
                    client_turn_id: "turn-terminal-response",
                    input: [
                      {
                        role: "user",
                        type: "message",
                        content: [{ type: "text", text: "做成 PPT 吧" }],
                      },
                    ],
                  },
                },
              ],
            },
            {
              id: "assistant-terminal-response",
              role: "assistant",
              cards: [
                {
                  code: "LantaiRuntimeResponseCard",
                  data: {
                    status: "completed",
                    metadata: {
                      client_turn_id: "turn-terminal-response",
                    },
                    output: [
                      {
                        role: "assistant",
                        type: "message",
                        status: "completed",
                        metadata: {
                          client_turn_id: "turn-terminal-response",
                        },
                        content: [{ type: "text", text: "PPT 已完成" }],
                      },
                    ],
                  },
                },
              ],
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "run-terminal-response",
            client_turn_id: "turn-terminal-response",
            started_at: now,
            updated_at: now,
            transport_status: "connected",
            events_preview: [
              {
                id: "evt-terminal-response-stale",
                ts: now,
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "create_presentation",
              },
            ],
          },
        },
      ],
    } as any);

    render(
      <>
        <TurnEventPreview variant="inline" />
        <TurnEventPreview variant="presence" />
      </>,
    );

    expect(screen.queryByTestId("inline-turn-progress")).toBeNull();
    expect(
      screen.getByRole("button", { name: /打开沈行知的本轮工作过程/ }),
    ).toHaveAttribute("aria-busy", "true");
  });

  it("does not let the initial receiving fallback replace meaningful progress", () => {
    const now = new Date().toISOString();
    const receivingEvent = {
      id: "evt-receiving",
      ts: now,
      kind: "chat.turn.started",
      status: "running",
      label: "开始运行",
      detail: "文本：请继续整理方案",
    };
    const runningState = {
      status: "running",
      run_id: "run-stable-presence",
      started_at: now,
      updated_at: now,
      transport_status: "connected",
      events_preview: [receivingEvent],
    };

    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-stable-presence",
      sessions: [
        {
          id: "chat-stable-presence",
          messages: [
            {
              id: "assistant-progress",
              role: "assistant",
              cards: [
                {
                  code: "LantaiRuntimeResponseCard",
                  data: {
                    status: "in_progress",
                    output: [
                      {
                        type: "reasoning",
                        status: "in_progress",
                        metadata: {
                          run_id: "run-stable-presence",
                          public_projection: "runtime_progress_v2",
                          public_progress_id: "progress-stable",
                          public_progress_revision: 1,
                          source: "runtime_facts",
                          private_reasoning_exposed: false,
                          text_origin: "assistant_public_text",
                        },
                        content: [
                          {
                            type: "text",
                            text: "正在核对演示文稿的版式和内容。",
                          },
                        ],
                      },
                    ],
                  },
                },
              ],
            },
          ],
          runtimeState: runningState,
        },
      ],
    } as any);

    const { rerender } = render(<TurnEventPreview variant="presence" />);
    expect(
      screen.getByText("正在核对演示文稿的版式和内容。"),
    ).toBeInTheDocument();

    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-stable-presence",
      sessions: [
        {
          id: "chat-stable-presence",
          messages: [],
          runtimeState: runningState,
        },
      ],
    } as any);
    rerender(<TurnEventPreview variant="presence" />);

    expect(
      screen.getByText("正在核对演示文稿的版式和内容。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("正在收到消息")).toBeNull();

    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-stable-presence",
      sessions: [
        {
          id: "chat-stable-presence",
          messages: [],
          runtimeState: {
            ...runningState,
            run_id: "run-next-presence",
          },
        },
      ],
    } as any);
    rerender(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.queryByText("刚开始")).toBeNull();
    expect(screen.queryByText("请继续整理方案")).toBeNull();
  });

  it("keeps the last substantive structured stage through a same-run resync", () => {
    const now = new Date().toISOString();
    const turnStarted = {
      id: "evt-structural-start",
      seq: 1,
      ts: now,
      run_id: "run-structural-stable",
      client_turn_id: "client-structural-stable",
      kind: "chat.turn.started",
      type: "turn.started",
      status: "running",
      label: "开始运行",
      detail: "消息已送达",
    };
    const sessionState = (events: any[], status = "running") => ({
      currentSessionId: "chat-structural-stable",
      sessions: [
        {
          id: "chat-structural-stable",
          messages: [
            {
              id: "client-structural-stable",
              role: "user",
              metadata: { client_turn_id: "client-structural-stable" },
            },
          ],
          runtimeState: {
            status,
            run_id: "run-structural-stable",
            started_at: now,
            updated_at: now,
            transport_status: "connected",
            events_preview: events,
          },
        },
      ],
    });

    mockUseChatAnywhereSessionsState.mockReturnValue(
      sessionState([
        turnStarted,
        {
          id: "evt-structural-tool",
          seq: 2,
          ts: now,
          run_id: "run-structural-stable",
          client_turn_id: "client-structural-stable",
          kind: "chat.tool_call.started",
          type: "tool.call.started",
          status: "running",
          label: "工具调用",
          detail: "read_file",
          payload: { tool_name: "read_file" },
        },
      ]),
    );
    const { rerender } = render(<TurnEventPreview variant="presence" />);
    expect(screen.getByText("正在读取文件")).toBeInTheDocument();

    mockUseChatAnywhereSessionsState.mockReturnValue(
      sessionState([turnStarted]),
    );
    rerender(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("正在读取文件")).toBeInTheDocument();
    expect(screen.queryByText("等待回应")).toBeNull();

    mockUseChatAnywhereSessionsState.mockReturnValue(
      sessionState(
        [
          turnStarted,
          {
            id: "evt-structural-failed",
            seq: 3,
            ts: now,
            run_id: "run-structural-stable",
            type: "turn.failed",
            kind: "chat.turn.failed",
            status: "failed",
            label: "本轮失败",
            detail: "模型连接中断",
          },
        ],
        "failed",
      ),
    );
    rerender(<TurnEventPreview variant="presence" />);

    expect(screen.getByRole("status")).toHaveTextContent("需要处理");
    expect(screen.queryByText("正在读取文件")).toBeNull();
  });

  it("shows a safe runtime-authored trace detail as flexible process copy", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-structured-detail",
      sessions: [
        {
          id: "chat-structured-detail",
          runtimeState: {
            status: "running",
            run_id: "run-structured-detail",
            started_at: now,
            updated_at: now,
            events_preview: [
              {
                id: "evt-structured-detail",
                seq: 2,
                ts: now,
                run_id: "run-structured-detail",
                kind: "chat.tool_call.started",
                type: "tool.call.started",
                status: "running",
                label: "专业协作",
                detail: "collaboration",
                phase: "subtask",
                payload: {
                  agent_name: "普洱",
                  trace_title: "正在组建协作室",
                  trace_detail: "沈行知正在选择成员与分工",
                },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("沈行知正在选择成员与分工")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("正在子任务：普洱");
  });

  it("advances cumulative public-progress text without regressing to a shorter stream prefix", () => {
    const now = new Date().toISOString();
    const sessionState = (text: string, revision = 4) => ({
      currentSessionId: "chat-streaming-progress",
      sessions: [
        {
          id: "chat-streaming-progress",
          messages: [
            {
              id: "assistant-streaming-progress",
              role: "assistant",
              cards: [
                {
                  code: "LantaiRuntimeResponseCard",
                  data: {
                    status: "in_progress",
                    metadata: { run_id: "run-streaming-progress" },
                    output: [
                      {
                        type: "reasoning",
                        status: "in_progress",
                        metadata: {
                          run_id: "run-streaming-progress",
                          public_projection: "runtime_progress_v2",
                          public_progress_id: "progress-streaming",
                          public_progress_revision: revision,
                          source: "runtime_facts",
                          private_reasoning_exposed: false,
                          text_origin: "assistant_public_text",
                        },
                        content: [{ type: "thinking", text }],
                      },
                    ],
                  },
                },
              ],
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "run-streaming-progress",
            started_at: now,
            updated_at: now,
            transport_status: "connected",
            events_preview: [],
          },
        },
      ],
    });

    mockUseChatAnywhereSessionsState.mockReturnValue(
      sessionState("我先把材料"),
    );
    const { rerender } = render(<TurnEventPreview variant="presence" />);
    expect(screen.getByText("我先把材料")).toBeInTheDocument();

    mockUseChatAnywhereSessionsState.mockReturnValue(
      sessionState("我先把材料读完，再逐项核对冲突口径。"),
    );
    rerender(<TurnEventPreview variant="presence" />);
    expect(
      screen.getByText("我先把材料读完，再逐项核对冲突口径。"),
    ).toBeInTheDocument();

    mockUseChatAnywhereSessionsState.mockReturnValue(
      sessionState("我先把材料读完"),
    );
    rerender(<TurnEventPreview variant="presence" />);
    expect(
      screen.getByText("我先把材料读完，再逐项核对冲突口径。"),
    ).toBeInTheDocument();
    mockUseChatAnywhereSessionsState.mockReturnValue(
      sessionState("旧版本不应覆盖", 3),
    );
    rerender(<TurnEventPreview variant="presence" />);
    expect(screen.queryByText("旧版本不应覆盖")).toBeNull();
    const answering = sessionState("我先把材料读完，再逐项核对冲突口径。");
    (answering.sessions[0].messages[0].cards[0].data.output as any[]).push({
      type: "message",
      metadata: { run_id: "run-streaming-progress" },
      content: [{ type: "text", text: "核对结果如下" }],
    });
    mockUseChatAnywhereSessionsState.mockReturnValue(answering);
    rerender(<TurnEventPreview variant="presence" />);
    expect(
      screen.queryByText("我先把材料读完，再逐项核对冲突口径。"),
    ).toBeNull();
    expect(
      publicProgressHistory(
        answering.sessions[0].messages as any,
        "run-streaming-progress",
      )[0].text,
    ).toBe("我先把材料读完，再逐项核对冲突口径。");
  });

  it("never presents the user's request as Shen Xingzhi's own process copy", () => {
    render(<TurnEventPreview variant="presence" />);

    expect(
      screen.getByRole("button", { name: "打开沈行知的本轮工作过程" }),
    ).toBeInTheDocument();
    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.queryByText("刚开始")).toBeNull();
    expect(screen.queryByText("请继续整理方案")).toBeNull();
    expect(screen.queryByText("刚刚有进展")).toBeNull();
  });

  it("keeps a factual presence rail visible before the first scoped event arrives", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-awaiting-event",
      sessions: [
        {
          id: "chat-awaiting-event",
          messages: [
            {
              id: "client-awaiting-event",
              role: "user",
              metadata: { client_turn_id: "client-awaiting-event" },
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "run-awaiting-event",
            started_at: now,
            updated_at: now,
            transport_status: "connected",
            events_preview: [],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(
      screen.getByRole("button", { name: /打开沈行知的本轮工作过程/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "打开沈行知的本轮工作过程" }),
    ).toBeInTheDocument();
    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.queryByText("刚开始")).toBeNull();
    expect(screen.queryByText("client-awaiting-event")).toBeNull();
  });

  it("anchors a new client turn to its own receipt instead of an older same-run event", () => {
    const nowMs = Date.now();
    const old = new Date(nowMs - 10 * 60_000).toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-new-client-turn",
      sessions: [
        {
          id: "chat-new-client-turn",
          messages: [
            {
              id: "client-old",
              role: "user",
              createdAt: nowMs - 10 * 60_000,
              metadata: { client_turn_id: "client-old" },
            },
            {
              id: "client-new",
              role: "user",
              createdAt: nowMs,
              metadata: { client_turn_id: "client-new" },
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "shared-run",
            started_at: old,
            updated_at: old,
            transport_status: "connected",
            events_preview: [
              {
                id: "evt-old-tool",
                seq: 4,
                ts: old,
                run_id: "shared-run",
                client_turn_id: "client-old",
                type: "tool.call.started",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "read_file",
                payload: { tool_name: "read_file" },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.queryByText("刚开始")).toBeNull();
    expect(screen.queryByText("正在读取文件")).toBeNull();
    expect(screen.queryByText("正在重连")).toBeNull();
    expect(screen.queryByText(/10m前有进展/)).toBeNull();
  });

  it("does not guess causal client identity from a vendor user-message id", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-vendor-message-id",
      sessions: [
        {
          id: "chat-vendor-message-id",
          messages: [
            {
              id: "vendor-rendering-row-id",
              role: "user",
              createdAt: now,
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "run-vendor-message-id",
            started_at: now,
            updated_at: now,
            transport_status: "connected",
            events_preview: [
              {
                id: "evt-canonical-client-tool",
                seq: 2,
                ts: now,
                run_id: "run-vendor-message-id",
                client_turn_id: "canonical-client-turn",
                type: "tool.call.started",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "read_file",
                payload: { tool_name: "read_file" },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("正在读取文件")).toBeInTheDocument();
    expect(screen.queryByText("vendor-rendering-row-id")).toBeNull();
  });

  it("never promotes unmarked provider reasoning into the presence line", () => {
    const messages = [
      {
        id: "assistant-private-reasoning",
        role: "assistant",
        cards: [
          {
            code: "LantaiRuntimeResponseCard",
            data: {
              status: "in_progress",
              output: [
                {
                  type: "reasoning",
                  status: "in_progress",
                  content: [
                    { type: "thinking", text: "私有推理不能出现在这里" },
                  ],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    expect(latestAuthoredPresenceText(messages)).toBe("");

    messages[0].cards[0].data.output[0].content[0].metadata = {
      public_projection: "runtime_progress_v2",
      public_progress_id: "progress-1",
    };
    expect(latestAuthoredPresenceText(messages)).toBe("");

    messages[0].cards[0].data.output[0].content[0].metadata = {
      ...messages[0].cards[0].data.output[0].content[0].metadata,
      public_progress_revision: 1,
      source: "runtime_facts",
      private_reasoning_exposed: false,
      text_origin: "assistant_public_text",
    };
    expect(latestAuthoredPresenceText(messages)).toBe("私有推理不能出现在这里");
  });

  it("extracts only canonical public-progress blocks from mixed reasoning", () => {
    const messages = [
      {
        id: "assistant-mixed-reasoning",
        role: "assistant",
        cards: [
          {
            code: "LantaiRuntimeResponseCard",
            data: {
              status: "in_progress",
              metadata: {
                run_id: "run-mixed",
                client_turn_id: "client-mixed",
              },
              output: [
                {
                  type: "reasoning",
                  status: "in_progress",
                  content: [
                    { type: "thinking", text: "这段是未标记的私有推理。" },
                    {
                      type: "thinking",
                      text: "资料已读完，正在核对两个冲突口径。",
                      metadata: {
                        run_id: "run-mixed",
                        client_turn_id: "client-mixed",
                        public_projection: "runtime_progress_v2",
                        public_progress_id: "progress-mixed",
                        public_progress_revision: 2,
                        source: "runtime_facts",
                        private_reasoning_exposed: false,
                        text_origin: "assistant_public_text",
                      },
                    },
                  ],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    expect(
      latestAuthoredPresenceText(messages, "run-mixed", "client-mixed"),
    ).toBe("资料已读完，正在核对两个冲突口径。");
    expect(
      latestAuthoredPresenceText(messages, "run-mixed", "client-mixed"),
    ).not.toContain("私有推理");
  });

  it("rejects an output-level progress marker that wraps mixed text blocks", () => {
    const messages = [
      {
        id: "assistant-output-marker-mixed",
        role: "assistant",
        cards: [
          {
            code: "LantaiRuntimeResponseCard",
            data: {
              status: "in_progress",
              output: [
                {
                  type: "reasoning",
                  status: "in_progress",
                  metadata: {
                    run_id: "run-output-mixed",
                    public_projection: "runtime_progress_v2",
                    public_progress_id: "progress-output-mixed",
                    public_progress_revision: 1,
                    source: "runtime_facts",
                    private_reasoning_exposed: false,
                    text_origin: "assistant_public_text",
                  },
                  content: [
                    { type: "thinking", text: "公开句子。" },
                    { type: "thinking", text: "不应被夹带的推理。" },
                  ],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    expect(latestAuthoredPresenceText(messages, "run-output-mixed")).toBe("");
  });

  it("accepts the direct SSE public-progress envelope without exposing ordinary reasoning", () => {
    const messages = [
      {
        id: "assistant-direct-progress",
        role: "assistant",
        cards: [
          {
            code: "LantaiRuntimeResponseCard",
            data: {
              status: "in_progress",
              output: [
                {
                  type: "reasoning",
                  status: "in_progress",
                  metadata: {
                    run_id: "run-direct",
                    client_turn_id: "client-direct",
                    public_progress: {
                      schema_version: 2,
                      id: "progress-direct",
                      revision: 3,
                      source: "runtime_facts",
                      text_origin: "assistant_public_text",
                    },
                  },
                  content: [
                    {
                      type: "thinking",
                      text: "搜索已完成，正在交叉验证来源。",
                    },
                  ],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    expect(
      latestAuthoredPresenceText(messages, "run-direct", "client-direct"),
    ).toBe("搜索已完成，正在交叉验证来源。");

    messages[0].cards[0].data.output[0].metadata.public_progress.source =
      "provider";
    expect(
      latestAuthoredPresenceText(messages, "run-direct", "client-direct"),
    ).toBe("");

    messages[0].cards[0].data.output[0].metadata.public_progress.source =
      "runtime_facts";
    messages[0].cards[0].data.output[0].metadata.public_progress.id = 42;
    expect(
      latestAuthoredPresenceText(messages, "run-direct", "client-direct"),
    ).toBe("");

    messages[0].cards[0].data.output[0].metadata.public_progress.id =
      "progress-direct";
    messages[0].cards[0].data.output[0].metadata.public_progress.source = [
      "runtime_facts",
    ];
    expect(
      latestAuthoredPresenceText(messages, "run-direct", "client-direct"),
    ).toBe("");
  });

  it("carries an explicit durable runtime progress event into the compact presence selector", () => {
    const text = "资料已经找齐，正在核验最后两个日期。";
    const persistedProgressEvents = [
      {
        id: "evt-public-progress-integration",
        seq: 7,
        created_at: "2026-09-04T12:00:00Z",
        type: "message.completed",
        status: "completed",
        source: "assistant",
        session_id: "console:progress-integration",
        turn_id: "run-progress-integration",
        item_id: "provider-progress-integration",
        payload: {
          run_id: "run-progress-integration",
          client_turn_id: "client-progress-integration",
          source_message_id: "provider-progress-integration",
          content_type: "thinking",
          stream_message_type: "reasoning",
          text,
          public_progress: {
            schema_version: 2,
            id: "public-progress:integration:1",
            revision: 1,
            source: "runtime_facts",
            text_origin: "assistant_public_text",
            tool_call_ids: ["call-progress-integration"],
          },
          message: {
            id: "provider-progress-integration",
            role: "assistant",
            metadata: {
              client_turn_id: "client-progress-integration",
            },
            content: [{ type: "thinking", text }],
          },
        },
      },
    ] as any;
    const messages = buildNativeRuntimeStreamMessages(persistedProgressEvents);

    expect(
      latestAuthoredPresenceText(
        messages as any,
        "run-progress-integration",
        "client-progress-integration",
      ),
    ).toBe(text);

    const runtimeState = (toolCallId: string) => ({
      status: "running",
      run_id: "run-progress-integration",
      started_at: "2026-09-04T12:00:00Z",
      updated_at: "2026-09-04T12:00:01Z",
      transport_status: "connected",
      events_preview: [
        {
          id: `event-${toolCallId}`,
          seq: 8,
          ts: "2026-09-04T12:00:01Z",
          run_id: "run-progress-integration",
          client_turn_id: "client-progress-integration",
          type: "tool.call.started",
          kind: "chat.tool_call.started",
          status: "running",
          label: "工具调用",
          detail: "read_file",
          payload: { id: toolCallId, tool_name: "read_file" },
        },
      ],
    });
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-progress-integration",
      sessions: [
        {
          id: "chat-progress-integration",
          messages,
          runtimeState: runtimeState("call-progress-integration"),
        },
      ],
    } as any);
    const { rerender } = render(
      <>
        <TurnEventPreview variant="presence" />
        <TurnEventPreview variant="inline" />
      </>,
    );
    expect(screen.getByText(text)).toBeInTheDocument();

    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-progress-integration",
      sessions: [
        {
          id: "chat-progress-integration",
          messages,
          runtimeState: runtimeState("call-next-step"),
        },
      ],
    } as any);
    rerender(
      <>
        <TurnEventPreview variant="presence" />
        <TurnEventPreview variant="inline" />
      </>,
    );

    expect(screen.queryByText(text)).toBeNull();
    expect(screen.getByText("正在读取文件")).toBeInTheDocument();

    const completedRuntimeState = {
      status: "completed",
      outcome_status: "delivered",
      run_id: "run-progress-integration",
      started_at: "2026-09-04T12:00:00Z",
      updated_at: "2026-09-04T12:00:02Z",
      events_preview: [
        {
          id: "event-turn-completed",
          seq: 12,
          ts: "2026-09-04T12:00:02Z",
          run_id: "run-progress-integration",
          client_turn_id: "client-progress-integration",
          type: "turn.completed",
          kind: "chat.turn.settled",
          status: "completed",
          label: "本轮完成",
          detail: "回复已生成",
          payload: { outcome_status: "delivered" },
        },
      ],
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-progress-integration",
      sessions: [
        {
          id: "chat-progress-integration",
          messages,
          runtimeState: completedRuntimeState,
        },
      ],
    } as any);
    rerender(
      <>
        <TurnEventPreview variant="presence" />
        <TurnEventPreview variant="inline" />
      </>,
    );
    expect(screen.queryByText(text)).toBeNull();
    expect(screen.getByText("已完成")).toBeInTheDocument();

    const hydratedMessages = buildNativeRuntimeStreamMessages(
      persistedProgressEvents,
    );
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-progress-integration",
      sessions: [
        {
          id: "chat-progress-integration",
          messages: hydratedMessages,
          runtimeState: completedRuntimeState,
        },
      ],
    } as any);
    rerender(
      <>
        <TurnEventPreview variant="presence" />
        <TurnEventPreview variant="inline" />
      </>,
    );
    expect(screen.queryByText(text)).toBeNull();
    expect(screen.getByText("已完成")).toBeInTheDocument();
  });

  it("rejects otherwise canonical progress without the current run identity", () => {
    const messages = [
      {
        id: "assistant-unscoped-progress",
        role: "assistant",
        cards: [
          {
            code: "LantaiRuntimeResponseCard",
            data: {
              status: "in_progress",
              output: [
                {
                  type: "reasoning",
                  content: [
                    {
                      type: "thinking",
                      text: "没有身份的进展不能串入当前轮。",
                      metadata: {
                        public_projection: "runtime_progress_v2",
                        public_progress_id: "progress-unscoped",
                        public_progress_revision: 1,
                        source: "runtime_facts",
                        private_reasoning_exposed: false,
                        text_origin: "assistant_public_text",
                      },
                    },
                  ],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    expect(latestAuthoredPresenceText(messages, "run-current")).toBe("");
  });

  it("does not treat an ordinary completed answer as process narration", () => {
    const messages = [
      {
        id: "assistant-final-answer",
        role: "assistant",
        cards: [
          {
            code: "LantaiRuntimeResponseCard",
            data: {
              status: "in_progress",
              metadata: { run_id: "run-answer" },
              output: [
                {
                  type: "message",
                  status: "completed",
                  content: [
                    { type: "text", text: "这是最终回答，不是过程旁白。" },
                  ],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    expect(latestAuthoredPresenceText(messages, "run-answer")).toBe("");
  });

  it("filters public progress to the current runtime run", () => {
    const publicProgress = (runId: string, text: string) => ({
      id: `assistant-${runId}`,
      role: "assistant",
      cards: [
        {
          code: "LantaiRuntimeResponseCard",
          data: {
            status: "in_progress",
            metadata: { run_id: runId },
            output: [
              {
                type: "reasoning",
                status: "in_progress",
                metadata: {
                  run_id: runId,
                  public_projection: "runtime_progress_v2",
                  public_progress_id: `progress-${runId}`,
                  public_progress_revision: 1,
                  source: "runtime_facts",
                  private_reasoning_exposed: false,
                  text_origin: "assistant_public_text",
                },
                content: [{ type: "thinking", text }],
              },
            ],
          },
        },
      ],
    });
    const messages = [
      publicProgress("run-current", "这是当前轮进展。"),
      publicProgress("run-old", "这是上一轮残留。"),
    ] as any;

    expect(latestAuthoredPresenceText(messages, "run-current")).toBe(
      "这是当前轮进展。",
    );
  });

  it("clears stable public copy when a new client turn starts in the same run", () => {
    const now = new Date().toISOString();
    const user = (id: string, text: string) => ({
      id,
      role: "user",
      metadata: { client_turn_id: id },
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            client_turn_id: id,
            input: [{ role: "user", content: [{ type: "text", text }] }],
          },
        },
      ],
    });
    const progress = {
      id: "assistant-turn-one",
      role: "assistant",
      cards: [
        {
          code: "LantaiRuntimeResponseCard",
          data: {
            status: "in_progress",
            metadata: {
              run_id: "shared-run",
              client_turn_id: "client-turn-one",
            },
            output: [
              {
                type: "reasoning",
                status: "in_progress",
                content: [
                  {
                    type: "thinking",
                    text: "第一件事正在整理。",
                    metadata: {
                      run_id: "shared-run",
                      client_turn_id: "client-turn-one",
                      public_projection: "runtime_progress_v2",
                      public_progress_id: "progress-turn-one",
                      public_progress_revision: 1,
                      source: "runtime_facts",
                      private_reasoning_exposed: false,
                      text_origin: "assistant_public_text",
                    },
                  },
                ],
              },
            ],
          },
        },
      ],
    };
    const stateFor = (clientTurnId: string) => ({
      status: "running",
      run_id: "shared-run",
      started_at: now,
      updated_at: now,
      transport_status: "connected",
      events_preview: [
        {
          id: `turn-start-${clientTurnId}`,
          ts: now,
          run_id: "shared-run",
          client_turn_id: clientTurnId,
          type: "turn.started",
          kind: "turn.started",
          status: "running",
          label: "开始运行",
          detail: "消息已送达",
        },
      ],
    });

    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-shared-run",
      sessions: [
        {
          id: "chat-shared-run",
          messages: [user("client-turn-one", "先整理第一件事"), progress],
          runtimeState: stateFor("client-turn-one"),
        },
      ],
    } as any);

    const { rerender } = render(<TurnEventPreview variant="presence" />);
    expect(screen.getByText("第一件事正在整理。")).toBeInTheDocument();

    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-shared-run",
      sessions: [
        {
          id: "chat-shared-run",
          messages: [
            user("client-turn-one", "先整理第一件事"),
            progress,
            user("client-turn-two", "再处理第二件事"),
          ],
          runtimeState: stateFor("client-turn-two"),
        },
      ],
    } as any);
    rerender(<TurnEventPreview variant="presence" />);

    expect(screen.queryByText("第一件事正在整理。")).toBeNull();
    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.queryByText("再处理第二件事")).toBeNull();
  });

  it("does not carry an old presence into a new unscoped user turn", () => {
    const nowMs = Date.now();
    const oldTime = new Date(nowMs - 10_000).toISOString();
    const newUserTime = new Date(nowMs).toISOString();
    const oldProgress = {
      id: "assistant-unscoped-boundary",
      role: "assistant",
      cards: [
        {
          code: "LantaiRuntimeResponseCard",
          data: {
            status: "in_progress",
            metadata: {
              run_id: "shared-unscoped-run",
              client_turn_id: "client-old-unscoped",
            },
            output: [
              {
                type: "reasoning",
                status: "in_progress",
                content: [
                  {
                    type: "thinking",
                    text: "上一件事还在核对旧材料。",
                    metadata: {
                      run_id: "shared-unscoped-run",
                      client_turn_id: "client-old-unscoped",
                      public_projection: "runtime_progress_v2",
                      public_progress_id: "progress-old-unscoped",
                      public_progress_revision: 1,
                      source: "runtime_facts",
                      private_reasoning_exposed: false,
                      text_origin: "assistant_public_text",
                    },
                  },
                ],
              },
            ],
          },
        },
      ],
    };
    const oldRuntimeState = {
      status: "running",
      run_id: "shared-unscoped-run",
      started_at: oldTime,
      updated_at: oldTime,
      transport_status: "connected",
      events_preview: [
        {
          id: "old-unscoped-start",
          seq: 1,
          ts: oldTime,
          run_id: "shared-unscoped-run",
          client_turn_id: "client-old-unscoped",
          type: "turn.started",
          kind: "chat.turn.started",
          status: "running",
          label: "开始运行",
          detail: "旧消息已送达",
        },
        {
          id: "old-unscoped-tool",
          seq: 2,
          ts: new Date(nowMs - 9_000).toISOString(),
          run_id: "shared-unscoped-run",
          client_turn_id: "client-old-unscoped",
          type: "tool.call.started",
          kind: "chat.tool_call.started",
          status: "running",
          label: "工具调用",
          detail: "read_file",
          payload: { tool_name: "read_file" },
        },
      ],
    };

    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-unscoped-boundary",
      sessions: [
        {
          id: "chat-unscoped-boundary",
          messages: [
            {
              id: "client-old-unscoped",
              role: "user",
              createdAt: oldTime,
              metadata: { client_turn_id: "client-old-unscoped" },
            },
            oldProgress,
          ],
          runtimeState: oldRuntimeState,
        },
      ],
    } as any);

    const { rerender } = render(<TurnEventPreview variant="presence" />);
    expect(screen.getByText("上一件事还在核对旧材料。")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("正在读取文件");

    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-unscoped-boundary",
      sessions: [
        {
          id: "chat-unscoped-boundary",
          messages: [
            {
              id: "client-old-unscoped",
              role: "user",
              createdAt: oldTime,
              metadata: { client_turn_id: "client-old-unscoped" },
            },
            oldProgress,
            {
              id: "render-only-new-message-id",
              role: "user",
              createdAt: newUserTime,
            },
          ],
          runtimeState: oldRuntimeState,
        },
      ],
    } as any);
    rerender(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.queryByText("刚开始")).toBeNull();
    expect(screen.queryByText("上一件事还在核对旧材料。")).toBeNull();
    expect(screen.queryByText("正在读取文件")).toBeNull();
    expect(screen.queryByText("render-only-new-message-id")).toBeNull();

    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-unscoped-boundary",
      sessions: [
        {
          id: "chat-unscoped-boundary",
          messages: [
            {
              id: "client-old-unscoped",
              role: "user",
              createdAt: oldTime,
              metadata: { client_turn_id: "client-old-unscoped" },
            },
            oldProgress,
            {
              id: "render-only-new-message-id",
              role: "user",
              createdAt: newUserTime,
            },
          ],
          runtimeState: {
            ...oldRuntimeState,
            updated_at: new Date(nowMs + 1_000).toISOString(),
            // The start row may have rolled out of the bounded preview before
            // React receives the snapshot. A differently scoped direct event
            // after the user boundary is enough to identify the new turn.
            events_preview: [
              {
                id: "new-unscoped-tool",
                seq: 40,
                ts: new Date(nowMs + 1_000).toISOString(),
                run_id: "shared-unscoped-run",
                client_turn_id: "client-new-unscoped",
                type: "tool.call.started",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "read_file",
                payload: { tool_name: "read_file" },
              },
            ],
          },
        },
      ],
    } as any);
    rerender(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("正在读取文件")).toBeInTheDocument();
    expect(screen.queryByText("上一件事还在核对旧材料。")).toBeNull();
  });

  it("shows a fresh optimistic user turn before its first runtime event arrives", () => {
    const nowMs = Date.now();
    const oldTime = new Date(nowMs - 10_000).toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-optimistic-boundary",
      sessions: [
        {
          id: "chat-optimistic-boundary",
          messages: [
            {
              id: "user-new-optimistic",
              role: "user",
              createdAt: nowMs,
              metadata: {
                client_turn_id: "user-new-optimistic",
                client_turn_id_origin: "optimistic_submission",
              },
            },
          ],
          runtimeState: {
            status: "completed",
            outcome_status: "delivered",
            delivery_status: "verified",
            run_id: "run-old-optimistic",
            client_turn_id: "user-old-optimistic",
            started_at: oldTime,
            updated_at: oldTime,
            events_preview: [
              {
                id: "old-optimistic-terminal",
                seq: 9,
                ts: oldTime,
                run_id: "run-old-optimistic",
                client_turn_id: "user-old-optimistic",
                type: "turn.completed",
                kind: "chat.turn.settled",
                status: "completed",
                label: "旧轮完成",
                detail: "旧回复已生成",
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.queryByText("刚开始")).toBeNull();
    expect(screen.queryByText("已就绪")).toBeNull();
    expect(screen.queryByText("本轮已完成")).toBeNull();
    expect(screen.queryByText("旧轮完成")).toBeNull();
  });

  it("filters structured stages to the current runtime run", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-current-run",
      sessions: [
        {
          id: "chat-current-run",
          runtimeState: {
            status: "running",
            run_id: "run-current",
            started_at: now,
            updated_at: now,
            events_preview: [
              {
                id: "evt-current",
                seq: 1,
                ts: now,
                run_id: "run-current",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "read_file",
                payload: { tool_name: "read_file" },
              },
              {
                id: "evt-old",
                seq: 99,
                ts: now,
                run_id: "run-old",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "view_image",
                payload: { tool_name: "view_image" },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("正在读取文件")).toBeInTheDocument();
    expect(screen.queryByText("正在查看图片")).toBeNull();
  });

  it("keeps child-run stages that explicitly belong to the current parent run", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-parent-run",
      sessions: [
        {
          id: "chat-parent-run",
          runtimeState: {
            status: "running",
            run_id: "run-parent",
            started_at: now,
            updated_at: now,
            events_preview: [
              {
                id: "evt-child",
                seq: 2,
                ts: now,
                run_id: "run-child",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "read_file",
                payload: {
                  parent_run_id: "run-parent",
                  tool_name: "read_file",
                },
              },
              {
                id: "evt-unrelated",
                seq: 3,
                ts: now,
                run_id: "run-unrelated",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "view_image",
                payload: { tool_name: "view_image" },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("正在读取文件")).toBeInTheDocument();
    expect(screen.queryByText("正在查看图片")).toBeNull();
  });

  it("lets a terminal failure event override a lagging running snapshot", () => {
    const nowMs = Date.now();
    const now = new Date(nowMs).toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-terminal-race",
      sessions: [
        {
          id: "chat-terminal-race",
          messages: [
            {
              id: "client-terminal-race",
              role: "user",
              createdAt: nowMs - 1_000,
              metadata: { client_turn_id: "client-terminal-race" },
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "run-terminal-race",
            started_at: now,
            updated_at: now,
            events_preview: [
              {
                id: "evt-client-scoped-start",
                seq: 1,
                ts: now,
                run_id: "run-terminal-race",
                client_turn_id: "client-terminal-race",
                type: "turn.started",
                kind: "chat.turn.started",
                status: "running",
                label: "开始运行",
                detail: "消息已送达",
              },
              {
                id: "evt-terminal-race",
                seq: 9,
                ts: now,
                run_id: "run-terminal-race",
                type: "turn.failed",
                kind: "chat.turn.failed",
                status: "failed",
                label: "本轮失败",
                detail: "模型连接中断",
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    const presence = screen.getByRole("button", {
      name: /打开沈行知的本轮工作过程/,
    });
    expect(presence).toHaveAttribute("data-tone", "warning");
    expect(screen.getByRole("status")).toHaveTextContent("需要处理");
    expect(within(presence).getByText("这次没能完成")).toBeInTheDocument();
    expect(
      within(presence).getByText("查看原因和重试方式"),
    ).toBeInTheDocument();
    expect(within(presence).queryByText("模型连接中断")).toBeNull();
    expect(screen.queryByText("等待回应")).toBeNull();

    fireEvent.click(presence);
    expect(screen.getByText(/模型连接中断/)).toBeInTheDocument();
  });

  it("ignores an old unscoped terminal when the same run accepts a newer client turn", () => {
    const nowMs = Date.now();
    const old = new Date(nowMs - 60_000).toISOString();
    const delayedOldTerminal = new Date(nowMs + 250).toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-reused-run-terminal",
      sessions: [
        {
          id: "chat-reused-run-terminal",
          messages: [
            {
              id: "client-old",
              role: "user",
              createdAt: nowMs - 60_000,
              metadata: { client_turn_id: "client-old" },
            },
            {
              id: "client-new",
              role: "user",
              createdAt: nowMs,
              metadata: { client_turn_id: "client-new" },
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "run-reused-by-inbox",
            started_at: old,
            updated_at: old,
            transport_status: "connected",
            events_preview: [
              {
                id: "evt-old-client-start",
                seq: 1,
                ts: old,
                run_id: "run-reused-by-inbox",
                client_turn_id: "client-old",
                type: "turn.started",
                kind: "chat.turn.started",
                status: "running",
                label: "开始运行",
                detail: "旧消息已送达",
              },
              {
                id: "evt-old-client-terminal",
                seq: 9,
                // The prior logical turn can settle after the next queued
                // user message has already been accepted. Timestamp-only
                // filtering must not mistake it for the new client turn.
                ts: delayedOldTerminal,
                run_id: "run-reused-by-inbox",
                type: "turn.failed",
                kind: "chat.turn.failed",
                status: "failed",
                label: "本轮失败",
                detail: "旧消息的模型连接中断",
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.queryByText("刚开始")).toBeNull();
    expect(screen.queryByText("需要处理")).toBeNull();
    expect(screen.queryByText("这里遇到了问题，本轮还没有完成。")).toBeNull();
  });

  it("does not let a child-run terminal event settle its active parent", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-child-terminal",
      sessions: [
        {
          id: "chat-child-terminal",
          messages: [
            {
              id: "client-parent",
              role: "user",
              metadata: { client_turn_id: "client-parent" },
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "run-parent",
            started_at: now,
            updated_at: now,
            transport_status: "connected",
            events_preview: [
              {
                id: "evt-parent-start",
                seq: 1,
                ts: now,
                run_id: "run-parent",
                client_turn_id: "client-parent",
                type: "turn.started",
                kind: "chat.turn.started",
                status: "running",
                label: "开始运行",
                detail: "消息已送达",
              },
              {
                id: "evt-child-completed",
                seq: 2,
                ts: now,
                run_id: "run-child",
                type: "turn.completed",
                kind: "chat.turn.settled",
                status: "completed",
                label: "子任务结束",
                detail: "子任务已经完成",
                payload: { parent_run_id: "run-parent" },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.queryByText("本轮已完成")).toBeNull();
  });

  it("ignores child failure fallback while the parent snapshot is idle", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-child-idle-fallback",
      sessions: [
        {
          id: "chat-child-idle-fallback",
          runtimeState: {
            status: "idle",
            run_id: "run-parent",
            started_at: now,
            updated_at: now,
            events_preview: [
              {
                id: "evt-parent-running",
                seq: 1,
                ts: now,
                run_id: "run-parent",
                type: "turn.started",
                kind: "chat.turn.started",
                status: "running",
                label: "开始运行",
                detail: "消息已送达",
              },
              {
                id: "evt-child-failed",
                seq: 2,
                ts: now,
                run_id: "run-child",
                type: "turn.failed",
                kind: "chat.turn.failed",
                status: "failed",
                label: "子任务失败",
                detail: "子任务失败",
                payload: { parent_run_id: "run-parent" },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(screen.getAllByText("处理中").length).toBeGreaterThan(0);
    expect(screen.queryByText("需要处理")).toBeNull();
  });

  it("ignores an unscoped child terminal outcome when resolving its parent run", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-unscoped-child-terminal",
      sessions: [
        {
          id: "chat-unscoped-child-terminal",
          runtimeState: {
            status: "running",
            outcome_status: "unknown",
            run_id: "run-parent",
            started_at: now,
            updated_at: now,
            transport_status: "connected",
            events_preview: [
              {
                id: "evt-parent-unscoped-start",
                seq: 1,
                ts: now,
                run_id: "run-parent",
                type: "turn.started",
                kind: "chat.turn.started",
                status: "running",
                label: "开始运行",
                detail: "消息已送达",
              },
              {
                id: "evt-child-unscoped-completed",
                seq: 2,
                ts: now,
                run_id: "run-child",
                type: "turn.completed",
                kind: "chat.turn.settled",
                status: "completed",
                label: "子任务结束",
                detail: "子任务还缺少输入",
                payload: {
                  parent_run_id: "run-parent",
                  outcome_status: "blocked",
                },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    expect(screen.getByText("等待回应")).toBeInTheDocument();
    expect(screen.queryByText("暂时受阻")).toBeNull();
    expect(screen.queryByText("已就绪")).toBeNull();
  });

  it("leaves one compact completed state after a delivered turn", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-ready",
      sessions: [
        {
          id: "chat-ready",
          runtimeState: {
            status: "completed",
            outcome_status: "delivered",
            run_id: "run-ready",
            started_at: now,
            updated_at: now,
            events_preview: [
              {
                id: "evt-ready",
                seq: 3,
                ts: now,
                run_id: "run-ready",
                type: "turn.completed",
                kind: "chat.turn.settled",
                status: "completed",
                label: "本轮完成",
                detail: "回复已生成",
                payload: { outcome_status: "delivered" },
              },
            ],
          },
        },
      ],
    } as any);

    render(
      <>
        <TurnEventPreview variant="presence" />
        <TurnEventPreview variant="inline" />
      </>,
    );

    const presence = screen.getByRole("button", {
      name: /查看沈行知的本轮工作过程/,
    });
    expect(within(presence).getByText("已完成")).toBeInTheDocument();
    expect(within(presence).getByText("查看过程")).toBeInTheDocument();
    expect(presence).toHaveAccessibleName("查看沈行知的本轮工作过程");
    expect(within(presence).queryByText("已就绪")).toBeNull();
    expect(within(presence).queryByText("本轮已完成")).toBeNull();
    expect(presence.querySelector("img")).toBeNull();
  });

  it("retires public progress when a later work step starts", () => {
    const now = new Date().toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-settled-progress",
      sessions: [
        {
          id: "chat-settled-progress",
          messages: [
            {
              id: "assistant-settled-progress",
              role: "assistant",
              cards: [
                {
                  code: "LantaiRuntimeResponseCard",
                  data: {
                    status: "in_progress",
                    metadata: { run_id: "run-settled-progress" },
                    output: [
                      {
                        type: "reasoning",
                        status: "completed",
                        content: [
                          {
                            type: "thinking",
                            text: "文件已经生成，我正在打开预览。",
                            metadata: {
                              run_id: "run-settled-progress",
                              public_projection: "runtime_progress_v2",
                              public_progress_id: "progress-settled",
                              public_progress_revision: 1,
                              source: "runtime_facts",
                              private_reasoning_exposed: false,
                              text_origin: "assistant_public_text",
                              tool_call_ids: ["preview-1"],
                            },
                          },
                        ],
                      },
                    ],
                  },
                },
              ],
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "run-settled-progress",
            started_at: now,
            updated_at: now,
            transport_status: "connected",
            events_preview: [
              {
                id: "tool-started",
                seq: 1,
                ts: now,
                run_id: "run-settled-progress",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "open_preview",
                payload: { id: "preview-1", tool_name: "open_preview" },
              },
              {
                id: "tool-finished",
                seq: 2,
                ts: now,
                run_id: "run-settled-progress",
                kind: "chat.tool_call.finished",
                status: "completed",
                label: "工具结果",
                detail: "open_preview",
                payload: { id: "preview-1", tool_name: "open_preview" },
              },
              {
                id: "later-tool-started",
                seq: 3,
                ts: now,
                run_id: "run-settled-progress",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "inspect_preview",
                payload: { id: "preview-2", tool_name: "inspect_preview" },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    const presence = screen.getByRole("button", {
      name: /打开沈行知的本轮工作过程/,
    });
    expect(
      within(presence).queryByText("文件已经生成，我正在打开预览。"),
    ).toBeNull();
    expect(within(presence).getByText("正在执行工具")).toBeInTheDocument();
  });

  it("retires public progress immediately after the real three-event tool lifecycle completes", () => {
    const now = new Date().toISOString();
    const messages = buildNativeRuntimeStreamMessages([
      {
        id: "progress-event-three-stage",
        seq: 2,
        created_at: now,
        type: "message.completed",
        status: "completed",
        source: "assistant",
        session_id: "console:three-stage",
        turn_id: "run-three-stage",
        item_id: "progress-three-stage",
        payload: {
          client_turn_id: "client-three-stage",
          content_type: "thinking",
          stream_message_type: "reasoning",
          public_progress: {
            schema_version: 2,
            id: "progress-three-stage",
            revision: 1,
            source: "runtime_facts",
            text_origin: "assistant_public_text",
            tool_call_ids: ["call-three-stage"],
          },
          message: {
            id: "progress-three-stage",
            role: "assistant",
            content: [
              {
                type: "thinking",
                text: "文件找到了，我正在读取。",
              },
            ],
          },
        },
      },
    ] as any);
    const lifecycleEvents = [
      {
        id: "call-started",
        seq: 3,
        ts: now,
        run_id: "run-three-stage",
        client_turn_id: "client-three-stage",
        kind: "chat.tool_call.started",
        type: "tool.call.started",
        status: "running",
        label: "工具调用",
        detail: "read_file",
        payload: { id: "call-three-stage", tool_name: "read_file" },
      },
      {
        id: "tool-started",
        seq: 4,
        ts: now,
        run_id: "run-three-stage",
        client_turn_id: "client-three-stage",
        kind: "chat.tool.started",
        type: "tool.started",
        status: "running",
        label: "开始执行",
        detail: "read_file",
        payload: { id: "call-three-stage", tool_name: "read_file" },
      },
      {
        id: "call-completed",
        seq: 5,
        ts: now,
        run_id: "run-three-stage",
        client_turn_id: "client-three-stage",
        kind: "chat.tool_call.completed",
        type: "tool.call.completed",
        status: "completed",
        label: "工具结果",
        detail: "read_file",
        payload: { id: "call-three-stage", tool_name: "read_file" },
      },
    ];
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-three-stage",
      sessions: [
        {
          id: "chat-three-stage",
          messages,
          runtimeState: {
            status: "running",
            run_id: "run-three-stage",
            started_at: now,
            updated_at: now,
            transport_status: "connected",
            events_preview: lifecycleEvents,
          },
        },
      ],
    } as any);

    render(<TurnEventPreview variant="presence" />);

    const presence = screen.getByRole("button", {
      name: /打开沈行知的本轮工作过程/,
    });
    expect(within(presence).queryByText("文件找到了，我正在读取。")).toBeNull();
    expect(within(presence).getByText("处理中")).toBeInTheDocument();
    expect(
      buildExecutionStages(
        buildProcessTrace({
          summary_line: "处理中",
          events: lifecycleEvents,
        } as any).steps,
        "running",
      ).filter((step) => step.status === "running"),
    ).toHaveLength(0);
  });

  it("settles an old request stage once a later tool stage has completed", () => {
    const trace = buildProcessTrace({
      summary_line: "处理中",
      events: [
        {
          id: "turn-start",
          seq: 1,
          ts: "2026-09-04T12:00:00Z",
          kind: "chat.turn.started",
          status: "running",
          label: "开始运行",
          detail: "文本：没啥事",
        },
        {
          id: "tool-start",
          seq: 2,
          ts: "2026-09-04T12:00:01Z",
          kind: "chat.tool_call.started",
          status: "running",
          label: "工具调用",
          detail: "read_file",
          payload: { id: "tool-1", tool_name: "read_file" },
        },
        {
          id: "tool-finish",
          seq: 3,
          ts: "2026-09-04T12:00:02Z",
          kind: "chat.tool_call.finished",
          status: "completed",
          label: "工具结果",
          detail: "read_file",
          payload: { id: "tool-1", tool_name: "read_file" },
        },
      ],
    });

    const stages = buildExecutionStages(trace.steps, "running");

    expect(stages.map((stage) => [stage.title, stage.status])).toEqual([
      ["收到消息", "completed"],
      ["读取文件", "completed"],
    ]);
  });

  it("keeps a terminal failure truthful but calm instead of surfacing an alarm", () => {
    const presentation = buildPresencePresentation({
      status: "failed",
      stageTitle: "生成文件",
      authoredText: "我马上就做好了。",
      stall: { level: "idle" } as any,
    });

    expect(presentation).toEqual({
      stage: "需要处理",
      text: "这次没能完成",
      meta: "查看原因和重试方式",
      tone: "warning",
      authored: false,
    });
  });

  it.each(["", "connected", "connecting"])(
    "does not invent reconnection or failure from an old %s connection marker",
    (transportStatus) => {
      const now = Date.parse("2026-09-05T10:00:00Z");
      for (const age of [82_000, 360_000]) {
        const stall = getRuntimeStallState(
          {
            status: "running",
            transport_status: transportStatus,
            updated_at: new Date(now - age).toISOString(),
            events_preview: [
              {
                id: "model-thinking",
                seq: 2,
                ts: new Date(now - age).toISOString(),
                kind: "chat.message.started",
                status: "running",
                label: "正在思考",
                detail: "",
              },
            ],
          },
          null,
          now,
        );
        const presentation = buildPresencePresentation({
          status: "running",
          stageTitle: "正在思考",
          stall,
        });
        expect(presentation?.stage).toBe("正在思考");
        expect(presentation?.text).toBe("");
        expect(presentation?.tone).toBe("quiet");
        expect(presentation?.meta).not.toMatch(/重连|连接正常|现场|前有进展/);
      }
    },
  );

  it.each(["reconnecting", "closed"])(
    "keeps a confirmed %s status in quiet metadata without replacing authored progress",
    (transportStatus) => {
      const stall = getRuntimeStallState({
        status: "running",
        transport_status: transportStatus,
        updated_at: new Date().toISOString(),
        events_preview: [],
      });
      const presentation = buildPresencePresentation({
        status: "running",
        stageTitle: "整理材料",
        authoredText: "资料读完了，接着核对两个冲突口径。",
        stall,
      });
      expect(presentation).toMatchObject({
        text: "资料读完了，接着核对两个冲突口径。",
        meta:
          transportStatus === "reconnecting"
            ? "正在恢复进度同步"
            : "进度同步暂时中断 · 查看详情",
        tone: "quiet",
        authored: true,
      });
      expect(presentation?.stage).not.toMatch(/重连|需要处理/);
    },
  );

  it("keeps transport heartbeat separate from business progress when authored progress goes quiet", () => {
    const now = Date.parse("2026-09-09T00:19:37Z");
    const stall = getRuntimeStallState(
      {
        status: "running",
        transport_status: "connected",
        started_at: "2026-09-09T00:08:38Z",
        updated_at: "2026-09-09T00:19:30Z",
        events_preview: [
          {
            id: "model-last-delta",
            seq: 100,
            ts: "2026-09-09T00:14:37Z",
            kind: "chat.message.delta",
            status: "running",
            label: "生成回复",
            detail: "回退",
          },
          {
            id: "transport-heartbeat",
            seq: 101,
            ts: "2026-09-09T00:19:30Z",
            kind: "chat.system.event",
            type: "runtime.progress",
            status: "running",
            label: "运行心跳",
            detail: "Runtime is active.",
            payload: { structured_keepalive: true },
          },
        ],
      },
      null,
      now,
    );

    const presentation = buildPresencePresentation({
      status: "running",
      stageTitle: "正在生成回复",
      stageLabel: "思考中",
      authoredText: "回退路径已确认，现在写生成脚本。",
      stall,
    });

    expect(stall.level).toBe("stalled");
    expect(stall.transportAlive).toBe(true);
    expect(presentation).toMatchObject({
      text: "回退路径已确认，现在写生成脚本。",
      meta: "连接正常 · 5m 没有新进展",
      tone: "quiet",
      authored: true,
    });
  });

  it("keeps the presence rail calm during long thinking with sparse runtime events", () => {
    const old = new Date(Date.now() - 82_000).toISOString();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-long-thinking",
      sessions: [
        {
          id: "chat-long-thinking",
          generating: true,
          runtimeState: {
            status: "running",
            run_id: "run-long-thinking",
            transport_status: "connected",
            started_at: old,
            updated_at: old,
            events_preview: [
              {
                id: "thinking-started",
                seq: 3,
                ts: old,
                run_id: "run-long-thinking",
                kind: "chat.message.started",
                type: "message.started",
                status: "running",
                label: "正在思考",
                detail: "",
                payload: { stream_message_type: "reasoning" },
              },
            ],
          },
        },
      ],
    } as any);
    render(<TurnEventPreview variant="presence" />);
    const rail = screen.getByRole("button", {
      name: /打开沈行知的本轮工作过程/,
    });
    expect(rail).toHaveAttribute("aria-busy", "true");
    expect(rail).toHaveAttribute("data-tone", "quiet");
    expect(within(rail).getByText("思考中")).toBeInTheDocument();
    expect(rail.querySelector("strong")).toBeNull();
    expect(rail.textContent).not.toMatch(
      /重连|连接正常|现场|前有进展|需要处理/,
    );
  });

  it("leaves a truthful compact completion row after a successful turn", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-completed-presence",
      sessions: [
        {
          id: "chat-completed-presence",
          runtimeState: {
            status: "completed",
            outcome_status: "delivered",
            events_preview: [
              {
                id: "evt-completed-presence",
                ts: new Date().toISOString(),
                kind: "chat.turn.settled",
                type: "turn.completed",
                status: "finished",
                label: "本轮结束",
                detail: "运行已结束",
              },
            ],
          },
        },
      ],
    } as any);

    render(
      <>
        <TurnEventPreview variant="presence" />
        <TurnEventPreview variant="inline" />
      </>,
    );

    const presence = screen.getByRole("button", {
      name: /查看沈行知的本轮工作过程/,
    });
    expect(presence).toBeInTheDocument();
    expect(within(presence).getByText("已完成")).toBeInTheDocument();
    expect(within(presence).queryByText("已就绪")).toBeNull();
    expect(within(presence).queryByText("本轮已完成")).toBeNull();

    fireEvent.click(presence);
    expect(screen.getByText("1 项已完成")).toBeInTheDocument();
    expect(screen.queryByText("0 项已完成")).toBeNull();
  });

  it("keeps reasoning telemetry in the thinking stage before answer output", () => {
    const trace = buildProcessTrace({
      summary_line: "正在思考",
      events: [
        {
          id: "evt-reasoning-delta",
          seq: 2,
          ts: "2026-09-04T12:00:01Z",
          run_id: "run-reasoning-stage",
          kind: "chat.message.delta",
          type: "message.delta",
          status: "running",
          label: "思考片段",
          detail: "",
          payload: { content_type: "thinking" },
        },
      ],
    });

    const stages = buildExecutionStages(trace.steps, "running");

    expect(stages).toHaveLength(1);
    expect(stages[0]).toMatchObject({
      phase: "model",
      title: "梳理任务思路",
      status: "running",
      detail: "正在理解并梳理任务",
    });
    expect(stages[0].phase).not.toBe("result");
  });

  it("describes quiet pre-response telemetry honestly", () => {
    const quietStep = buildProcessTrace({
      summary_line: "处理中",
      events: [
        {
          id: "evt-routing",
          ts: "2026-08-09T08:10:00Z",
          kind: "chat.model.routing_decision",
          status: "running",
          label: "模型路由",
          detail: "standard",
        },
      ],
    }).steps;

    const stages = buildExecutionStages(quietStep, "running");

    expect(stages).toHaveLength(1);
    expect(stages[0].title).toBe("等待模型响应");
    expect(stages[0].detail).toContain("等待首个回复或工具动作");
  });

  it("keeps a completed execution drawer truthful when transcript events arrive late", () => {
    const stages = buildExecutionStages([], "completed");

    expect(stages).toHaveLength(1);
    expect(stages[0]).toMatchObject({
      phase: "result",
      title: "完成本轮",
      status: "completed",
      detail: "回复已生成，执行详情仍在同步",
    });
  });

  it("turns generic tool events into user-readable work stages", () => {
    const trace = buildProcessTrace({
      summary_line: "处理中",
      events: [
        {
          id: "evt-read-file",
          ts: "2026-08-28T12:30:56Z",
          kind: "chat.tool_call.finished",
          status: "completed",
          label: "工具结果",
          detail: "read_file",
          payload: { tool_name: "read_file" },
        },
        {
          id: "evt-view-image",
          ts: "2026-08-28T12:35:43Z",
          kind: "chat.tool_call.started",
          status: "running",
          label: "工具调用",
          detail: "view_image",
          payload: { tool_name: "view_image" },
        },
      ],
    });

    expect(trace.steps.map((step) => step.title)).toEqual([
      "读取文件",
      "查看图片",
    ]);
  });

  it("omits private implementation events from the user execution trace", () => {
    const trace = buildProcessTrace({
      summary_line: "处理中",
      events: [
        {
          id: "evt-private-skill",
          ts: "2026-05-21T10:00:00Z",
          kind: "chat.tool_call.started",
          status: "running",
          label: "工具调用",
          detail: "read_skill",
          payload: {
            tool_name: "read_skill",
            presentation: {
              visibility: "hidden",
              surface: "debug",
            },
          },
        },
        {
          id: "evt-agent",
          ts: "2026-05-21T10:00:01Z",
          kind: "chat.tool_call.started",
          status: "running",
          label: "专业协作",
          detail: "普洱正在核对估值",
          phase: "subtask",
          payload: { agent_name: "普洱" },
        },
      ],
    });

    expect(trace.steps).toHaveLength(1);
    expect(trace.steps[0].title).toBe("子任务：普洱");
    expect(JSON.stringify(trace)).not.toContain("read_skill");
  });

  it("merges one tool start and result into one lifecycle row", () => {
    const trace = buildProcessTrace({
      summary_line: "工具失败",
      events: [
        {
          id: "evt-tool-start",
          ts: "2026-08-09T08:10:00Z",
          kind: "chat.tool_call.started",
          type: "tool.call.started",
          status: "running",
          label: "工具调用",
          detail: "execute_shell_command",
          payload: { id: "call-1", tool_name: "execute_shell_command" },
        },
        {
          id: "evt-tool-result",
          ts: "2026-08-09T08:10:01Z",
          kind: "chat.tool_call.finished",
          type: "tool.call.completed",
          status: "failed",
          label: "工具结果",
          detail: "policy engine failed",
          payload: { id: "call-1", tool_name: "execute_shell_command" },
        },
      ],
    });

    expect(trace.steps).toHaveLength(1);
    expect(trace.steps[0].status).toBe("failed");
    expect(trace.steps[0].detail).toContain("policy engine failed");
  });

  it("projects nested presentation recovery metadata into the execution trace", () => {
    const trace = buildProcessTrace({
      summary_line: "PPT 文件交付中断",
      events: [
        {
          id: "evt-ppt-delivery-failed",
          ts: "2026-09-27T08:10:01Z",
          kind: "chat.tool_call.finished",
          type: "tool.call.completed",
          status: "failed",
          label: "工具结果",
          detail: "create_presentation",
          payload: {
            id: "call-ppt-1",
            tool_name: "create_presentation",
            metadata: {
              error_code: "presentation_delivery_file_error",
              job_id: "ppt-job-visible-recovery",
              publish_ready: false,
              requires_publish: false,
              requires_human: false,
              recovery_actions: ["retry_file_stage"],
            },
          },
        },
      ],
    });

    expect(trace.steps).toHaveLength(1);
    expect(trace.steps[0].diagnosis).toMatchObject({
      category: "presentation_delivery",
      summary: "PPT 文件交付尚未完成",
      job_id: "ppt-job-visible-recovery",
      recovery_action: "retry_file_stage",
    });
  });

  it("projects intermediate failures as recovered after a successful turn", () => {
    const trace = buildProcessTrace({
      summary_line: "已完成",
      events: [
        {
          id: "evt-tool-failed",
          ts: "2026-08-31T14:00:00Z",
          kind: "chat.tool_call.finished",
          type: "tool.call.completed",
          status: "failed",
          label: "工具结果",
          detail: "ModuleNotFoundError: No module named 'pptx'",
          payload: {
            tool_name: "execute_shell_command",
            failure_diagnosis: {
              category: "dependency",
              summary: "依赖或导入缺失",
              next_action: "补齐依赖后重试。",
            },
          },
        },
        {
          id: "evt-file-sent",
          ts: "2026-08-31T14:01:00Z",
          kind: "chat.tool_call.finished",
          type: "tool.call.completed",
          status: "completed",
          label: "工具结果",
          detail: "send_file_to_user",
          payload: { tool_name: "send_file_to_user", ok: true },
        },
        {
          id: "evt-turn-completed",
          ts: "2026-08-31T14:01:01Z",
          kind: "chat.turn.settled",
          type: "turn.completed",
          status: "finished",
          label: "本轮结束",
          detail: "已交付",
        },
      ],
    });

    const stages = buildExecutionStages(trace.steps, "completed");
    const recovered = stages.find((step) => step.id === "evt-tool-failed");

    expect(recovered?.status).toBe("recovered");
    expect(recovered?.diagnosis).toBeNull();
    expect(recovered?.detail).toContain("已由后续步骤处理");
    expect(recovered?.debug.recovered_failure).toBeTruthy();
  });

  it("renders a compact execution button and opens a side drawer on click", () => {
    render(<TurnEventPreview />);

    expect(
      screen.getByRole("button", {
        name: "打开本轮执行，运行中，1 个阶段",
      }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("execution-status-orb")).toHaveAttribute(
      "data-status",
      "running",
    );
    expect(screen.getByTestId("execution-status-orb")).toHaveAttribute(
      "data-spinning",
      "1",
    );
    expect(screen.queryByText("收到消息")).not.toBeInTheDocument();

    openTraceDrawer();

    expect(screen.getByText("沈行知 · 本轮工作")).toBeInTheDocument();
    expect(
      screen.queryByRole("progressbar", { name: "本轮进度" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("本轮已知过程")).toBeInTheDocument();
    expect(screen.getByText("收到消息")).toBeInTheDocument();
    expect(screen.getAllByText("请继续整理方案")).toHaveLength(1);
    expect(screen.getByText("topic-workspace")).toBeInTheDocument();
    expect(screen.getByText("技术详情").closest("details")).not.toHaveAttribute(
      "open",
    );
  });

  it("lets users close an expanded execution drawer", async () => {
    render(<TurnEventPreview />);

    openTraceDrawer();
    expect(screen.getByText("收到消息")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "关闭工作台" }));

    await waitFor(() => {
      expect(screen.queryByText("收到消息")).not.toBeInTheDocument();
    });
  });

  it("lets users close the execution drawer by clicking outside", async () => {
    const { container } = render(<TurnEventPreview />);

    openTraceDrawer();
    expect(screen.getByText("收到消息")).toBeInTheDocument();

    const mask = container.ownerDocument.querySelector(".ant-drawer-mask");
    expect(mask).toBeInTheDocument();
    fireEvent.click(mask as Element);

    await waitFor(() => {
      expect(screen.queryByText("收到消息")).not.toBeInTheDocument();
    });
  });

  it("collapses reply telemetry into one understandable completed stage", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      sessions: [
        {
          id: "chat-1",
          runtimeState: {
            status: "completed",
            outcome_status: "delivered",
            run_id: "run-1",
            summary: "本轮结束",
            events_preview: [
              {
                id: "evt-delta-1",
                ts: "2026-05-21T10:00:00Z",
                kind: "chat.message.delta",
                type: "message.delta",
                status: "running",
                label: "回复片段",
                detail: "玩",
              },
              {
                id: "evt-delta-2",
                ts: "2026-05-21T10:00:01Z",
                kind: "chat.message.delta",
                type: "message.delta",
                status: "running",
                label: "回复片段",
                detail: "。",
              },
              {
                id: "evt-usage",
                ts: "2026-05-21T10:00:02Z",
                kind: "chat.system.event",
                type: "item.completed",
                status: "info",
                label: "系统事件",
                detail: "model usage",
              },
              {
                id: "evt-message-done",
                ts: "2026-05-21T10:00:03Z",
                kind: "chat.message.completed",
                type: "message.completed",
                status: "completed",
                label: "回复完成",
                detail: "文件已生成",
              },
              {
                id: "evt-final",
                ts: "2026-05-21T10:00:04Z",
                kind: "chat.final_answer",
                type: "item.completed",
                status: "completed",
                label: "最终回复",
                detail: "本轮回复完成",
              },
              {
                id: "evt-turn-done",
                ts: "2026-05-21T10:00:05Z",
                kind: "chat.turn.settled",
                type: "turn.completed",
                status: "finished",
                label: "本轮结束",
                detail: "运行已结束",
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);
    expect(
      screen.getByRole("button", {
        name: "打开本轮执行，任务成功，1 个阶段",
      }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("execution-status-orb")).toHaveAttribute(
      "data-status",
      "completed",
    );
    expect(screen.getByTestId("execution-status-orb")).toHaveAttribute(
      "data-spinning",
      "0",
    );

    openTraceDrawer();
    expect(screen.getByText("任务成功，成果已交付")).toBeInTheDocument();
    const stageList = screen.getByTestId("execution-stages");
    expect(within(stageList).getAllByText("生成回复")).toHaveLength(1);
    expect(within(stageList).getByText("回复已生成")).toBeInTheDocument();
    expect(within(stageList).getByText("已完成")).toBeInTheDocument();
    expect(within(stageList).queryByText("running")).not.toBeInTheDocument();
    expect(screen.getByText("6 条原始事件")).toBeInTheDocument();
  });

  it("shows a neutral ended state when delivery outcome is unknown", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-unknown-outcome",
      sessions: [
        {
          id: "chat-unknown-outcome",
          runtimeState: {
            status: "completed",
            run_id: "run-unknown-outcome",
            summary: "本轮结束",
            events_preview: [
              {
                id: "evt-unknown-outcome",
                ts: "2026-08-01T03:56:08Z",
                kind: "chat.turn.settled",
                type: "turn.completed",
                status: "finished",
                label: "本轮结束",
                detail: "运行已结束",
                payload: { final_task_status: "finished" },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);
    expect(
      screen.getByRole("button", {
        name: "打开本轮执行，回复结束，1 个阶段",
      }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("execution-status-orb")).toHaveAttribute(
      "data-status",
      "finished",
    );

    openTraceDrawer();
    expect(screen.getByText("回复已结束")).toBeInTheDocument();
    expect(screen.queryByText("本轮已完成")).not.toBeInTheDocument();
  });

  it("uses event delivery evidence over an unknown compatibility default", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-event-outcome",
      sessions: [
        {
          id: "chat-event-outcome",
          runtimeState: {
            status: "completed",
            outcome_status: "unknown",
            run_id: "run-event-outcome",
            summary: "结果已交付",
            events_preview: [
              {
                id: "evt-event-outcome",
                ts: "2026-08-01T03:56:08Z",
                kind: "chat.turn.settled",
                type: "turn.completed",
                status: "finished",
                label: "本轮结束",
                detail: "结果已交付",
                payload: { outcome_status: "delivered" },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);

    expect(screen.getByTestId("execution-status-orb")).toHaveAttribute(
      "data-status",
      "completed",
    );
  });

  it("offers a result-focused continuation when the run ended incomplete", async () => {
    document.body.innerHTML = '<div class="sender"><textarea></textarea></div>';
    mockContinueRun.mockResolvedValue({
      reconnected: false,
      run_id: "run-incomplete",
      chat_id: "chat-incomplete",
      session_id: "console:chat-incomplete",
      mode: "takeover",
      reason: "takeover_requires_frontend_resubmit",
      latest_seq: 8,
      text: "上一轮运行已结束，但回答可能不完整。请根据用户原始请求继续。",
    });
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-incomplete",
      sessions: [
        {
          id: "chat-incomplete",
          sessionId: "console:chat-incomplete",
          messages: [
            {
              id: "user-incomplete",
              role: "user",
              metadata: { client_turn_id: "turn-incomplete" },
              cards: [
                {
                  code: "LantaiRuntimeRequestCard",
                  data: {
                    client_turn_id: "turn-incomplete",
                    input: [
                      {
                        role: "user",
                        type: "message",
                        content: [{ type: "text", text: "把内容做成 PPT" }],
                      },
                    ],
                  },
                },
              ],
            },
            {
              id: "assistant-incomplete",
              role: "assistant",
              cards: [
                {
                  code: "LantaiRuntimeResponseCard",
                  data: {
                    status: "completed",
                    metadata: { client_turn_id: "turn-incomplete" },
                    output: [
                      {
                        role: "assistant",
                        type: "message",
                        status: "completed",
                        metadata: { client_turn_id: "turn-incomplete" },
                        content: [{ type: "text", text: "文件正在发送" }],
                      },
                    ],
                  },
                },
              ],
            },
          ],
          runtimeState: {
            status: "completed",
            outcome_status: "incomplete",
            run_id: "run-incomplete",
            client_turn_id: "turn-incomplete",
            summary: "本轮运行已结束",
            can_stop: false,
            can_reconnect: false,
            workspace_dir: "/tmp/topic-workspace",
            events_preview: [
              {
                id: "evt-incomplete",
                seq: 8,
                ts: "2026-08-01T03:56:08Z",
                kind: "chat.turn.settled",
                type: "turn.completed",
                status: "finished",
                label: "本轮结束",
                detail: "运行已结束",
                payload: { outcome_status: "incomplete" },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);
    expect(screen.getByTestId("execution-status-orb")).toHaveAttribute(
      "data-status",
      "incomplete",
    );
    openTraceDrawer();
    expect(screen.getByText("任务部分完成")).toBeInTheDocument();
    expect(
      screen.getByText(/本轮已结束，但回答可能不完整/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "继续回答" }));

    await waitFor(() => {
      expect(mockContinueRun).toHaveBeenCalledWith(
        "run-incomplete",
        expect.objectContaining({
          chat_id: "chat-incomplete",
          after_seq: 8,
        }),
      );
    });
    expect(
      (document.querySelector(".sender textarea") as HTMLTextAreaElement).value,
    ).toContain("根据用户原始请求继续");
  });

  it("prefers runtimeState when it exists and shows runtime actions", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      sessions: [
        {
          id: "chat-1",
          sessionId: "console:chat-1",
          runtimeState: {
            status: "running",
            run_id: "run-1",
            summary: "本轮运行中",
            can_stop: true,
            can_reconnect: true,
            events_preview: [
              {
                id: "evt-runtime-1",
                ts: "2026-05-21T10:00:00Z",
                kind: "chat.turn.started",
                status: "running",
                label: "开始运行",
                detail: "文本：继续执行",
              },
            ],
          },
          turnEventPreview: {
            workspace_dir: "/tmp/legacy",
            summary_line: "旧摘要",
            events: [],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);

    expect(
      screen.getByRole("button", {
        name: "打开本轮执行，运行中，1 个阶段",
      }),
    ).toBeInTheDocument();
    openTraceDrawer();
    expect(screen.getAllByText("运行中").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "停止" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("技术详情"));
    expect(
      screen.queryByRole("button", { name: "完整记录" }),
    ).not.toBeInTheDocument();
  });

  it("keeps a successful stop request pending until a terminal event arrives", async () => {
    mockInterruptRun.mockResolvedValue({ stopped: true, run_id: "run-stop" });
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-stop",
      sessions: [
        {
          id: "chat-stop",
          runtimeState: {
            status: "running",
            run_id: "run-stop",
            can_stop: true,
            can_reconnect: true,
            events_preview: [
              {
                id: "evt-start",
                ts: "2026-05-21T10:00:00Z",
                kind: "chat.turn.started",
                status: "running",
                label: "开始运行",
                detail: "",
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);
    openTraceDrawer();
    fireEvent.click(screen.getByRole("button", { name: "停止" }));

    await waitFor(() =>
      expect(mockInterruptRun).toHaveBeenCalledWith("run-stop"),
    );
    const update = mockUpdateSession.mock.calls.at(-1)?.[0];
    expect(update).toEqual(
      expect.objectContaining({
        generating: true,
        runtimeState: expect.objectContaining({
          status: "stopping",
          can_stop: false,
        }),
      }),
    );
    expect(update.runtimeState.events_preview.at(-1)).toEqual(
      expect.objectContaining({
        kind: "chat.turn.stop_requested",
        status: "info",
      }),
    );
  });

  it("keeps the call-chain entry when runtime events exist without a summary", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      sessions: [
        {
          id: "chat-1",
          sessionId: "console:chat-1",
          runtimeState: {
            status: "completed",
            outcome_status: "delivered",
            run_id: "run-1",
            summary: "",
            can_stop: false,
            can_reconnect: false,
            events_preview: [
              {
                id: "evt-runtime-1",
                ts: "2026-05-21T10:00:00Z",
                kind: "chat.turn.started",
                status: "completed",
                label: "模型调用",
                detail: "MiniMax-M3",
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);

    expect(
      screen.getByRole("button", {
        name: "打开本轮执行，任务成功，1 个阶段",
      }),
    ).toBeInTheDocument();
    openTraceDrawer();
    expect(screen.getByText("收到消息")).toBeInTheDocument();
    expect(screen.getByText("MiniMax-M3")).toBeInTheDocument();
  });

  it("renders code-focused phases for vibe coding turns", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      sessions: [
        {
          id: "chat-1",
          turnEventPreview: {
            workspace_dir: "/tmp/topic-workspace",
            summary_line: "代码任务运行中",
            events: [
              {
                id: "evt-test",
                ts: "2026-05-21T10:00:00Z",
                kind: "chat.tool_call.finished",
                status: "completed",
                label: "工具结果",
                detail: "pnpm test -- --runInBand",
                payload: {
                  phase: "test",
                  trace_title: "运行测试：pnpm",
                  command: "pnpm test -- --runInBand",
                },
              },
              {
                id: "evt-build",
                ts: "2026-05-21T10:01:00Z",
                kind: "chat.tool_call.finished",
                status: "completed",
                label: "工具结果",
                detail: "npm run build",
                payload: {
                  phase: "build",
                  trace_title: "构建项目：npm",
                  command: "npm run build",
                },
              },
              {
                id: "evt-preview",
                ts: "2026-05-21T10:02:00Z",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "npm run dev",
                payload: {
                  phase: "preview",
                  trace_title: "启动预览：npm",
                  command: "npm run dev",
                },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);

    openTraceDrawer();
    expect(screen.getByText("本轮已知过程")).toBeInTheDocument();
    const steps = within(screen.getByTestId("execution-stages"));
    expect(steps.getByText("验证结果")).toBeInTheDocument();
    expect(steps.getByText("构建产物")).toBeInTheDocument();
    expect(steps.getByText("查看预览")).toBeInTheDocument();
    expect(steps.queryByText("npm run build")).not.toBeInTheDocument();
  });

  it("renders checkpoint and subtask phases for agent coding workflows", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      sessions: [
        {
          id: "chat-1",
          turnEventPreview: {
            workspace_dir: "/tmp/topic-workspace",
            summary_line: "代码任务运行中",
            events: [
              {
                id: "evt-subtask",
                ts: "2026-05-21T10:00:00Z",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "collaborate_with_agents",
                payload: {
                  phase: "subtask",
                  agent_name: "frontend-reviewer",
                  agent_role: "检查前端预览",
                  delegation_reason: "并行确认页面状态",
                },
              },
              {
                id: "evt-checkpoint",
                ts: "2026-05-21T10:01:00Z",
                kind: "chat.system.event",
                status: "completed",
                label: "检查点",
                detail: "创建 checkpoint before preview fix",
                payload: {
                  phase: "checkpoint",
                  trace_title: "创建检查点",
                  trace_detail: "preview-fix-before",
                },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);

    openTraceDrawer();
    expect(screen.getByText("本轮已知过程")).toBeInTheDocument();
    const steps = within(screen.getByTestId("execution-stages"));
    expect(steps.getByText("子任务：frontend-reviewer")).toBeInTheDocument();
    expect(steps.getByText("创建检查点")).toBeInTheDocument();
    expect(steps.getByText("preview-fix-before")).toBeInTheDocument();
  });

  it("shows backend-marked stalled turns with a continuation action", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-21T10:03:00Z"));
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      sessions: [
        {
          id: "chat-1",
          sessionId: "console:chat-1",
          runtimeState: {
            status: "stalled",
            run_id: "run-1",
            summary: "本轮可能卡住",
            can_stop: false,
            can_reconnect: false,
            workspace_dir: "/tmp/topic-workspace",
            events_preview: [
              {
                id: "evt-stalled",
                seq: 3,
                ts: "2026-05-21T10:01:00Z",
                kind: "chat.system.event",
                type: "turn.stalled",
                status: "stalled",
                label: "系统事件",
                detail: "后端没有活动任务",
                payload: {
                  phase: "failure",
                  trace_title: "本轮可能卡住",
                  failure_diagnosis: {
                    label: "#3 本轮可能卡住",
                    category: "timeout",
                    summary: "执行超时或运行卡住",
                    next_action:
                      "先确认进程是否还活着和最后输出点；若无活动任务，从最后已完成步骤继续。",
                    evidence: "后端没有活动任务",
                  },
                },
              },
            ],
          },
        },
      ],
    } as any);

    try {
      render(<TurnEventPreview />);

      openTraceDrawer();
      expect(screen.getAllByText("已卡住").length).toBeGreaterThan(0);
      expect(screen.getAllByText("本轮可能卡住").length).toBeGreaterThan(0);
      expect(
        screen.getByRole("button", { name: "继续处理" }),
      ).toBeInTheDocument();

      expect(screen.getAllByText("执行超时或运行卡住").length).toBeGreaterThan(
        0,
      );
      expect(
        screen.getAllByText(/先确认进程是否还活着/).length,
      ).toBeGreaterThan(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("fills a repair prompt from runtime failure diagnosis", () => {
    document.body.innerHTML = '<div class="sender"><textarea></textarea></div>';
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      sessions: [
        {
          id: "chat-1",
          sessionId: "console:chat-1",
          runtimeState: {
            status: "failed",
            run_id: "run-preview",
            summary: "预览错误：Vite",
            can_stop: false,
            can_reconnect: false,
            workspace_dir: "/tmp/topic-workspace",
            events_preview: [
              {
                id: "evt-preview-fail",
                seq: 4,
                ts: "2026-05-21T10:02:00Z",
                kind: "chat.tool_call.finished",
                type: "tool.failed",
                status: "failed",
                label: "工具结果",
                detail: "net::ERR_CONNECTION_REFUSED localhost:5173",
                payload: {
                  phase: "preview",
                  trace_title: "预览错误：Vite",
                  trace_detail: "net::ERR_CONNECTION_REFUSED localhost:5173",
                  failure_diagnosis: {
                    label: "#4 预览错误：Vite",
                    category: "preview",
                    summary: "前端预览或浏览器验证失败",
                    next_action: "先确认 dev server 端口和页面控制台首个错误。",
                    evidence: "ERR_CONNECTION_REFUSED",
                  },
                },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);
    expect(screen.getByTestId("execution-status-orb")).toHaveAttribute(
      "data-status",
      "failed",
    );
    expect(screen.getByTestId("execution-status-orb")).toHaveAttribute(
      "data-spinning",
      "0",
    );
    openTraceDrawer();
    fireEvent.click(screen.getAllByRole("button", { name: "生成修复提示" })[0]);

    const textarea = document.querySelector(
      ".sender textarea",
    ) as HTMLTextAreaElement;
    expect(textarea.value).toContain("前端预览失败了");
    expect(textarea.value).toContain("run_id: run-preview");
    expect(textarea.value).toContain("浏览器控制台首个错误");
  });

  it("fills a same-job PPT delivery recovery prompt from tool metadata", () => {
    document.body.innerHTML = '<div class="sender"><textarea></textarea></div>';
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-ppt-recovery",
      sessions: [
        {
          id: "chat-ppt-recovery",
          sessionId: "console:chat-ppt-recovery",
          runtimeState: {
            status: "failed",
            run_id: "run-ppt-delivery",
            summary: "PPT 文件交付中断",
            can_stop: false,
            can_reconnect: false,
            workspace_dir: "/tmp/topic-workspace",
            events_preview: [
              {
                id: "evt-ppt-delivery-fail",
                seq: 5,
                ts: "2026-09-27T10:02:00Z",
                kind: "chat.tool_call.finished",
                type: "tool.call.completed",
                status: "failed",
                label: "工具结果",
                detail: "create_presentation",
                payload: {
                  id: "call-ppt-delivery",
                  tool_name: "create_presentation",
                  metadata: {
                    error_code: "presentation_delivery_file_error",
                    job_id: "ppt-job-ui-resume",
                    publish_ready: false,
                    requires_publish: false,
                    requires_human: false,
                    recovery_actions: ["retry_file_stage"],
                  },
                },
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);
    openTraceDrawer();
    expect(screen.getAllByText("PPT 文件交付尚未完成").length).toBeGreaterThan(
      0,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "生成修复提示" })[0]);

    const textarea = document.querySelector(
      ".sender textarea",
    ) as HTMLTextAreaElement;
    expect(textarea.value).toContain("presentation_job_id: ppt-job-ui-resume");
    expect(textarea.value).toContain(
      "presentation_recovery_action: retry_file_stage",
    );
    expect(textarea.value).toContain("不要新建作品");
    expect(textarea.value).toContain("不要重新生成已验真的页面");
  });

  it("shows elapsed time without opening details, freezes on the terminal event across hydration and isolates sessions", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-08T10:02:00Z"));
    const session = {
      id: "chat-1",
      runtimeState: {
        status: "running",
        run_id: "run-timer",
        started_at: "2026-09-08T10:00:00Z",
        updated_at: "2026-09-08T10:01:50Z",
        events_preview: [
          {
            id: "start",
            ts: "2026-09-08T10:00:00Z",
            kind: "chat.turn.started",
            status: "running",
            label: "开始",
          },
        ],
      },
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: session.id,
      sessions: [session],
    } as any);
    try {
      const { rerender } = render(
        <>
          <TurnEventPreview variant="presence" />
          <TurnEventPreview variant="inline" />
        </>,
      );
      expect(screen.queryByText("已运行 2m 0s")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: /本轮工作过程/ }));
      expect(screen.getByText("已运行 2m 0s")).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(1000));
      expect(screen.getByText("已运行 2m 1s")).toBeInTheDocument();
      session.runtimeState = {
        ...session.runtimeState,
        status: "completed",
        updated_at: "2026-09-08T10:02:01Z",
        events_preview: [
          ...session.runtimeState.events_preview,
          {
            id: "end",
            ts: "2026-09-08T10:02:01Z",
            kind: "chat.turn.settled",
            status: "completed",
            label: "完成",
          },
        ],
      };
      mockUseChatAnywhereSessionsState.mockReturnValue({
        currentSessionId: session.id,
        sessions: [{ ...session }],
      } as any);
      rerender(
        <>
          <TurnEventPreview variant="presence" />
          <TurnEventPreview variant="inline" />
        </>,
      );
      expect(screen.getAllByText("用时 2m 1s").length).toBeGreaterThan(0);
      act(() => vi.advanceTimersByTime(60_000));
      expect(screen.getAllByText("用时 2m 1s").length).toBeGreaterThan(0);
      session.runtimeState = {
        ...session.runtimeState,
        // History hydration is allowed to refresh snapshot metadata. It must
        // not make a completed task look as if it kept running for hours.
        updated_at: "2026-09-08T12:32:21Z",
      };
      mockUseChatAnywhereSessionsState.mockReturnValue({
        currentSessionId: session.id,
        sessions: [{ ...session }],
      } as any);
      rerender(
        <>
          <TurnEventPreview variant="presence" />
          <TurnEventPreview variant="inline" />
        </>,
      );
      expect(screen.getAllByText("用时 2m 1s").length).toBeGreaterThan(0);
      mockUseChatAnywhereSessionsState.mockReturnValue({
        currentSessionId: "chat-2",
        sessions: [session, { id: "chat-2" }],
      } as any);
      rerender(
        <>
          <TurnEventPreview variant="presence" />
          <TurnEventPreview variant="inline" />
        </>,
      );
      expect(screen.queryByText("用时 2m 1s")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reassures the user when a long model response is still connected", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-21T10:04:30Z"));
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      sessions: [
        {
          id: "chat-1",
          sessionId: "console:chat-1",
          runtimeState: {
            status: "running",
            run_id: "run-1",
            transport_status: "connected",
            updated_at: "2026-05-21T10:04:20Z",
            can_stop: true,
            can_reconnect: true,
            workspace_dir: "/tmp/topic-workspace",
            events_preview: [
              {
                id: "evt-message-started",
                seq: 21,
                ts: "2026-05-21T10:01:00Z",
                kind: "chat.message.started",
                status: "running",
                label: "生成回复",
                detail: "",
              },
            ],
          },
        },
      ],
    } as any);

    try {
      render(<TurnEventPreview />);
      openTraceDrawer();

      expect(screen.getByText("暂无新进展")).toBeInTheDocument();
      expect(screen.getByText("已运行 3m 30s")).toBeInTheDocument();
      expect(screen.getByText("暂时没有新进展")).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "继续处理" }),
      ).not.toBeInTheDocument();
      expect(screen.queryByText(/可能卡住/)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a connected-but-no-progress state beside the composer during a quiet request", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-09T00:19:37Z"));
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-live-heartbeat",
      sessions: [
        {
          id: "chat-live-heartbeat",
          messages: [
            {
              id: "assistant-live-heartbeat",
              role: "assistant",
              cards: [
                {
                  code: "LantaiRuntimeResponseCard",
                  data: {
                    status: "in_progress",
                    metadata: { run_id: "run-live-heartbeat" },
                    output: [
                      {
                        id: "progress-live-heartbeat",
                        type: "reasoning",
                        status: "completed",
                        metadata: {
                          run_id: "run-live-heartbeat",
                          public_projection: "runtime_progress_v2",
                          public_progress_id: "progress-live-heartbeat",
                          public_progress_revision: 1,
                          source: "runtime_facts",
                          private_reasoning_exposed: false,
                          text_origin: "assistant_public_text",
                        },
                        content: [
                          {
                            type: "thinking",
                            text: "回退路径已确认，现在写生成脚本。",
                          },
                        ],
                      },
                    ],
                  },
                },
              ],
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "run-live-heartbeat",
            transport_status: "connected",
            started_at: "2026-09-09T00:08:38Z",
            updated_at: "2026-09-09T00:19:30Z",
            events_preview: [
              {
                id: "message-live-heartbeat",
                seq: 100,
                ts: "2026-09-09T00:14:37Z",
                kind: "chat.message.delta",
                status: "running",
                label: "生成回复",
                detail: "回退",
              },
            ],
          },
        },
      ],
    } as any);

    try {
      render(<TurnEventPreview variant="presence" />);
      const presence = screen.getByRole("button", {
        name: /打开沈行知的本轮工作过程/,
      });
      expect(
        within(presence).getByText("连接正常 · 5m 没有新进展"),
      ).toBeInTheDocument();
      expect(presence).toHaveAttribute("data-tone", "quiet");
      expect(presence).toHaveAttribute("aria-busy", "true");
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not let queued guidance replace the active run presence with understanding", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-09T00:22:54Z"));
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-queued-guidance-presence",
      sessions: [
        {
          id: "chat-queued-guidance-presence",
          messages: [
            {
              id: "user-active-turn",
              role: "user",
              createdAt: Date.parse("2026-09-09T00:08:38Z"),
              metadata: { client_turn_id: "client-active-turn" },
            },
            {
              id: "assistant-active-progress",
              role: "assistant",
              cards: [
                {
                  code: "LantaiRuntimeResponseCard",
                  data: {
                    status: "in_progress",
                    metadata: {
                      run_id: "run-active-turn",
                      client_turn_id: "client-active-turn",
                    },
                    output: [
                      {
                        id: "progress-active-turn",
                        type: "reasoning",
                        status: "completed",
                        metadata: {
                          run_id: "run-active-turn",
                          client_turn_id: "client-active-turn",
                          public_projection: "runtime_progress_v2",
                          public_progress_id: "progress-active-turn",
                          public_progress_revision: 1,
                          source: "runtime_facts",
                          private_reasoning_exposed: false,
                          text_origin: "assistant_public_text",
                        },
                        content: [
                          {
                            type: "thinking",
                            text: "只需要修第 11 页底部重叠。",
                          },
                        ],
                      },
                    ],
                  },
                },
              ],
            },
            {
              id: "user-queued-guidance",
              role: "user",
              createdAt: Date.parse("2026-09-09T00:22:46Z"),
              metadata: { client_turn_id: "client-queued-guidance" },
              cards: [
                {
                  code: "LantaiRuntimeUserCard",
                  data: {
                    runtime_queue_status: "failed",
                    input: [
                      {
                        content: [{ type: "text", text: "搞不出来就别搞了" }],
                      },
                    ],
                  },
                },
              ],
            },
          ],
          runtimeState: {
            status: "running",
            run_id: "run-active-turn",
            client_turn_id: "client-active-turn",
            transport_status: "connected",
            started_at: "2026-09-09T00:08:38Z",
            updated_at: "2026-09-09T00:22:50Z",
            events_preview: [
              {
                id: "active-view-image",
                seq: 184,
                ts: "2026-09-09T00:22:42Z",
                run_id: "run-active-turn",
                client_turn_id: "client-active-turn",
                kind: "chat.tool_call.finished",
                status: "completed",
                label: "工具结果",
                detail: "slide-15.jpg",
                payload: { tool_name: "view_image" },
              },
            ],
          },
        },
      ],
    } as any);

    try {
      render(<TurnEventPreview variant="presence" />);
      const presence = screen.getByRole("button", {
        name: /打开沈行知的本轮工作过程/,
      });
      expect(
        within(presence).getByText("只需要修第 11 页底部重叠。"),
      ).toBeInTheDocument();
      expect(within(presence).queryByText("等待回应")).toBeNull();
      expect(within(presence).queryByText("已运行 14m 16s")).toBeNull();
      fireEvent.click(presence);
      expect(screen.getByText("已运行 14m 16s")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces a stalled running turn and fills a continuation prompt", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-21T10:07:00Z"));
    document.body.innerHTML = '<div class="sender"><textarea /></div>';
    mockContinueRun.mockResolvedValue({
      reconnected: false,
      run_id: "run-1",
      chat_id: "chat-1",
      session_id: "console:chat-1",
      mode: "takeover",
      reason: "takeover_requires_frontend_resubmit",
      latest_seq: 12,
      text: [
        "上一轮任务似乎中途停住了。请从当前状态继续，不要重复已经完成的步骤。",
        "- run_id: run-1",
        "- 停住阶段: 构建项目：npm",
        "- 最后事件详情: npm run build",
      ].join("\n"),
    });
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      sessions: [
        {
          id: "chat-1",
          sessionId: "console:chat-1",
          runtimeState: {
            status: "running",
            run_id: "run-1",
            summary: "构建项目：npm",
            can_stop: false,
            can_reconnect: true,
            workspace_dir: "/tmp/topic-workspace",
            events_preview: [
              {
                id: "evt-build",
                ts: "2026-05-21T10:01:00Z",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "npm run build",
                payload: {
                  phase: "build",
                  trace_title: "构建项目：npm",
                  trace_detail: "npm run build",
                },
              },
            ],
          },
        },
      ],
    } as any);

    try {
      render(<TurnEventPreview />);

      expect(
        screen.getByRole("button", {
          name: "打开本轮执行，运行中，1 个阶段",
        }),
      ).toBeInTheDocument();
      vi.useRealTimers();

      openTraceDrawer();
      expect(screen.getByText("这轮还没有完成")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent("可能卡住");
      fireEvent.click(screen.getByRole("button", { name: "继续处理" }));

      const textarea = document.querySelector(
        ".sender textarea",
      ) as HTMLTextAreaElement;
      await waitFor(() => {
        expect(mockContinueRun).toHaveBeenCalledWith("run-1", {
          chat_id: "chat-1",
          session_id: "console:chat-1",
          after_seq: 0,
          stage_label: "构建项目：npm",
          workspace_dir: "/tmp/topic-workspace",
        });
        expect(textarea.value).toContain("上一轮任务似乎中途停住了");
      });
      expect(textarea.value).toContain("- run_id: run-1");
      expect(textarea.value).toContain("- 停住阶段: 构建项目：npm");
      expect(textarea.value).toContain("- 最后事件详情: npm run build");

      expect(screen.getByRole("alert")).toHaveTextContent(
        "最后停在「构建项目：npm」",
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not draft a continuation across an unresolved tool effect", async () => {
    document.body.innerHTML = '<div class="sender"><textarea></textarea></div>';
    mockContinueRun.mockResolvedValue({
      reconnected: false,
      run_id: "run-1",
      chat_id: "chat-1",
      session_id: "console:chat-1",
      mode: "takeover",
      reason: "ambiguous_effect_requires_resolution",
      latest_seq: 12,
      blocking_effect_ids: ["effect:call-publish"],
      text: null,
    });
    mockUseChatAnywhereSessionsState.mockReturnValue({
      currentSessionId: "chat-1",
      sessions: [
        {
          id: "chat-1",
          sessionId: "console:chat-1",
          runtimeState: {
            status: "stalled",
            run_id: "run-1",
            summary: "发布版本",
            can_stop: false,
            can_reconnect: false,
            workspace_dir: "/tmp/topic-workspace",
            events_preview: [
              {
                id: "evt-publish",
                seq: 12,
                ts: "2026-05-21T10:01:00Z",
                kind: "chat.tool_call.started",
                status: "running",
                label: "工具调用",
                detail: "publish_release",
              },
            ],
          },
        },
      ],
    } as any);

    render(<TurnEventPreview />);
    openTraceDrawer();
    fireEvent.click(screen.getByRole("button", { name: "继续处理" }));

    await waitFor(() => {
      expect(mockMessageWarning).toHaveBeenCalledWith(
        "工具调用的执行状态尚未确认，请先确认该副作用的实际结果后再续跑。",
      );
    });
    expect(
      (document.querySelector(".sender textarea") as HTMLTextAreaElement).value,
    ).toBe("");
  });
});
