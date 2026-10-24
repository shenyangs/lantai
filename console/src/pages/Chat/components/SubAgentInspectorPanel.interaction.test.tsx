import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RuntimeEventsResponse } from "../../../api/types/chat";
import {
  ingestCollaborationRuntimeEvents,
  resetCollaborationEventStoreForTests,
} from "../collaborationEventStore";
import type { SubAgentInspectionTarget } from "./SubAgentInspectorPanel";

const { cancelAgentTask, listRuns, getRunEvents, subscribe, navigate } =
  vi.hoisted(() => ({
    cancelAgentTask: vi.fn(),
    listRuns: vi.fn(),
    getRunEvents: vi.fn(),
    subscribe: vi.fn(),
    navigate: vi.fn(),
  }));

vi.mock("@/vendor/lantaiChat", () => ({
  useChatAnywhereSessionsState: () => ({
    sessions: [
      {
        id: "parent-chat",
        runtimeState: { run_id: "parent-run" },
      },
    ],
    currentSessionId: "parent-chat",
  }),
}));

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );
  return { ...actual, useNavigate: () => navigate };
});

vi.mock("../../../api/modules/runtime", () => ({
  runtimeApi: { cancelAgentTask, listRuns, getRunEvents },
}));

vi.mock("../runtimeEventSubscriptionRegistry", () => ({
  subscribeRuntimeEventSession: subscribe,
}));

import SubAgentInspectorPanel, {
  resultFromSubAgentEvents,
  semanticSubAgentProcessEvents,
} from "./SubAgentInspectorPanel";

const target: SubAgentInspectionTarget = {
  id: "batch-1:member-water",
  agentId: "Lantai_Water_Agent",
  agentName: "白水",
  role: "研究与证据",
  task: "核验关键事实",
  status: "running",
  result: "",
  toolName: "collaborate_with_agents",
  toolCallId: "call-1",
  memberId: "member-water",
  sessionId: "host:to:water:attempt-1",
  taskId: "child-task-water",
};

describe("SubAgentInspectorPanel interaction", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetCollaborationEventStoreForTests();
    listRuns.mockResolvedValue({
      total: 1,
      items: [
        {
          id: "child-run-water",
          attempt_id: "child-attempt-water",
          chat_id: "child-chat-water",
          session_id: target.sessionId,
          status: "running",
        },
      ],
    });
    getRunEvents.mockResolvedValue({
      run_id: "child-run-water",
      attempt_id: "child-attempt-water",
      events: [],
    });
    subscribe.mockReturnValue(vi.fn());
  });

  it("does not replace an accepted result with the last delta from a truncated event window", () => {
    const acceptedResult =
      "这次任务本质是用户决策辅助，需要把当班长的利弊摆清楚。";
    const completedTarget = {
      ...target,
      status: "completed",
      result: acceptedResult,
    };

    expect(
      resultFromSubAgentEvents(
        [
          {
            id: "truncated-message-delta",
            seq: 400,
            type: "message.delta",
            status: "running",
            source: "assistant",
            session_id: target.sessionId,
            turn_id: "child-run-water",
            created_at: "2026-08-30T14:08:51Z",
            payload: {
              run_id: "child-run-water",
              message: {
                content: [{ type: "text", text: "当" }],
              },
              delta: "",
            },
          },
        ],
        completedTarget,
      ),
    ).toBe(acceptedResult);
  });

  it("keeps token fragments out of the process log and retains only public progress", () => {
    const semantic = semanticSubAgentProcessEvents([
      {
        id: "answer-delta-1",
        seq: 1,
        type: "message.delta",
        status: "running",
        source: "assistant",
        created_at: "2026-08-30T14:08:40Z",
        payload: {
          message: { content: [{ type: "text", text: "当" }] },
          delta: "当",
        },
      },
      {
        id: "private-thinking-delta",
        seq: 2,
        type: "message.delta",
        status: "running",
        source: "assistant",
        created_at: "2026-08-30T14:08:41Z",
        payload: {
          message: { content: [{ type: "thinking", text: "私密推理" }] },
          delta: "私密推理",
          content_type: "thinking",
        },
      },
      {
        id: "public-progress-1",
        seq: 3,
        type: "message.delta",
        status: "running",
        source: "assistant",
        created_at: "2026-08-30T14:08:42Z",
        payload: {
          message: {
            content: [{ type: "thinking", text: "正在梳理选择标准。" }],
          },
          delta: "正在梳理选择标准。",
          content_type: "thinking",
          public_progress: { id: "progress-1", revision: 1 },
        },
      },
      {
        id: "answer-completed",
        seq: 4,
        type: "message.completed",
        status: "completed",
        source: "assistant",
        turn_id: "child-run-water",
        created_at: "2026-08-30T14:08:43Z",
        payload: {
          message: { content: [{ type: "text", text: "完整公开结果" }] },
        },
      },
      {
        id: "turn-completed",
        seq: 5,
        type: "turn.completed",
        status: "completed",
        source: "runtime",
        turn_id: "child-run-water",
        created_at: "2026-08-30T14:08:44Z",
        payload: {},
      },
    ]);

    expect(semantic).toHaveLength(2);
    expect(semantic.map((event) => event.type)).toEqual([
      "reasoning.progress",
      "message.completed",
    ]);
    expect(semantic[0].payload).toEqual(
      expect.objectContaining({
        trace_title: "思考",
        trace_detail: "正在梳理选择标准。",
      }),
    );
    expect(semantic[1].payload).toEqual(
      expect.objectContaining({
        trace_title: "结果已形成",
        trace_detail: "完整内容见结果页",
      }),
    );
    expect(JSON.stringify(semantic)).not.toContain("私密推理");
    expect(JSON.stringify(semantic)).not.toContain('"text":"当"');
    expect(JSON.stringify(semantic)).not.toContain("完整公开结果");
  });

  it("accepts only a completed message as a direct child result", () => {
    expect(
      resultFromSubAgentEvents(
        [
          {
            id: "completed-message",
            seq: 5,
            type: "message.completed",
            status: "completed",
            source: "assistant",
            session_id: target.sessionId,
            turn_id: "child-run-water",
            created_at: "2026-08-30T14:08:55Z",
            payload: {
              message: {
                content: [{ type: "text", text: "完整公开结果" }],
              },
            },
          },
        ],
        target,
      ),
    ).toBe("完整公开结果");
  });

  it("streams only the selected child's public record and result", async () => {
    const onStop = vi.fn();
    render(
      <SubAgentInspectorPanel
        target={target}
        onClose={vi.fn()}
        onOpenArtifacts={vi.fn()}
        onTakeover={vi.fn()}
        onStop={onStop}
      />,
    );

    expect(
      screen.getByRole("complementary", { name: /白水的协作详情/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("核验关键事实")).toBeInTheDocument();

    await waitFor(() => {
      expect(listRuns).toHaveBeenCalledWith(
        expect.objectContaining({
          agentId: "Lantai_Water_Agent",
          sessionId: target.sessionId,
        }),
      );
      expect(subscribe).toHaveBeenCalledWith(
        "child-chat-water",
        expect.any(Function),
        expect.objectContaining({ agentId: "Lantai_Water_Agent" }),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "停止该协作者" }));
    expect(onStop).toHaveBeenCalledTimes(1);

    const onUpdate = subscribe.mock.calls[0][1] as (
      response: RuntimeEventsResponse,
    ) => void;
    act(() => {
      onUpdate({
        chat_id: "child-chat-water",
        after_seq: 0,
        latest_seq: 2,
        events: [
          {
            id: "child-message-1",
            seq: 2,
            type: "message.completed",
            status: "completed",
            source: "assistant",
            session_id: target.sessionId,
            turn_id: "child-run-water",
            created_at: "2026-08-23T12:00:00Z",
            payload: {
              run_id: "child-run-water",
              message: {
                content: [
                  { type: "thinking", text: "不应展示的私密推理" },
                  { type: "text", text: "公开核验结论" },
                ],
              },
            },
          },
        ],
      });
    });

    expect(
      screen.getByText(/展示公开思考、工作摘要与工具状态/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /结果/ }));
    expect(await screen.findByText("公开核验结论")).toBeInTheDocument();
    expect(screen.queryByText("不应展示的私密推理")).not.toBeInTheDocument();
  });

  it("does not report a closed live connection while the child stream anchor is still being discovered", async () => {
    const discoveringTarget = { ...target, id: `${target.id}:discovering` };
    listRuns.mockResolvedValue({ total: 0, items: [] });
    getRunEvents.mockResolvedValue({
      run_id: "parent-run",
      attempt_id: "parent-attempt",
      events: [
        {
          id: "room-running",
          seq: 1,
          type: "tool.call.delta",
          status: "running",
          source: "tool",
          created_at: "2026-08-24T11:49:00Z",
          payload: {
            collaboration: {
              members: [
                {
                  member_id: discoveringTarget.memberId,
                  agent_id: discoveringTarget.agentId,
                  display_name: discoveringTarget.agentName,
                  session_id: discoveringTarget.sessionId,
                  status: "running",
                },
              ],
            },
          },
        },
      ],
    });

    render(
      <SubAgentInspectorPanel
        target={discoveringTarget}
        onClose={vi.fn()}
        onOpenArtifacts={vi.fn()}
      />,
    );

    expect(
      await screen.findByText("正在定位该协作者的实时记录"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/实时连接已关闭/)).not.toBeInTheDocument();
    expect(subscribe).not.toHaveBeenCalled();
  });

  it("shows a completed member's accepted public result before the whole room closes", async () => {
    listRuns.mockResolvedValue({ total: 0, items: [] });
    getRunEvents.mockResolvedValue({
      run_id: "parent-run",
      attempt_id: "parent-attempt",
      events: [
        {
          id: "member-completed",
          seq: 2,
          type: "tool.call.delta",
          status: "running",
          source: "tool",
          created_at: "2026-08-24T11:49:05Z",
          payload: {
            collaboration: {
              members: [
                {
                  member_id: target.memberId,
                  agent_id: target.agentId,
                  display_name: target.agentName,
                  session_id: target.sessionId,
                  status: "completed",
                  result: "公开验收结果：健康与安全审核通过。",
                },
              ],
            },
          },
        },
      ],
    });

    render(
      <SubAgentInspectorPanel
        target={target}
        onClose={vi.fn()}
        onOpenArtifacts={vi.fn()}
      />,
    );

    expect((await screen.findAllByText("已完成")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /结果/ }));
    expect(
      await screen.findByText("公开验收结果：健康与安全审核通过。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("结果还在准备中")).not.toBeInTheDocument();
  });

  it("updates an already-open inspector as the room publishes child identity and result", async () => {
    const discoveringTarget = {
      ...target,
      id: `${target.id}:live-store`,
      sessionId: undefined,
    };
    listRuns.mockResolvedValue({ total: 0, items: [] });
    getRunEvents.mockResolvedValue({
      run_id: "parent-run",
      attempt_id: "parent-attempt",
      events: [],
    });

    render(
      <SubAgentInspectorPanel
        target={discoveringTarget}
        onClose={vi.fn()}
        onOpenArtifacts={vi.fn()}
      />,
    );

    expect(
      await screen.findByText("正在定位该协作者的实时记录"),
    ).toBeInTheDocument();

    act(() => {
      ingestCollaborationRuntimeEvents([
        {
          id: "room-live-result",
          item_id: "call-1",
          seq: 3,
          type: "tool.call.delta",
          status: "running",
          source: "tool",
          created_at: "2026-08-24T11:49:10Z",
          payload: {
            collaboration: {
              schema_version: 3,
              batch_id: "batch-1",
              revision: 3,
              status: "running",
              phase: "working",
              members: [
                {
                  member_id: target.memberId,
                  agent_id: target.agentId,
                  display_name: target.agentName,
                  status: "completed",
                  session_id: "host:to:water:attempt-1",
                  run_id: "child-run-water",
                  chat_id: "child-chat-water",
                  result: "实时回填的公开验收结果。",
                },
              ],
            },
          },
        },
      ]);
    });

    expect((await screen.findAllByText("已完成")).length).toBeGreaterThan(0);
    await waitFor(() => {
      expect(subscribe).toHaveBeenCalledWith(
        "child-chat-water",
        expect.any(Function),
        expect.objectContaining({ agentId: target.agentId }),
      );
    });
    fireEvent.click(screen.getByRole("button", { name: /结果/ }));
    expect(
      await screen.findByText("实时回填的公开验收结果。"),
    ).toBeInTheDocument();
  });
});
