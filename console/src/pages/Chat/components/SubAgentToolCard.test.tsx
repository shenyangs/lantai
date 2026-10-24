import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SubAgentToolCard, {
  BUILTIN_TEAM_AGENT_IDS,
  isSubAgentToolName,
} from "./SubAgentToolCard";
import type { AgentSummary } from "../../../api/types/agents";
import {
  ingestCollaborationRuntimeEvents,
  resetCollaborationEventStoreForTests,
} from "../collaborationEventStore";

const agents: AgentSummary[] = [
  {
    id: "Lantai_Mocha_Agent",
    name: "摩卡",
    description: "视觉设计、前端、页面",
    workspace_dir: "/tmp/mocha",
    enabled: true,
  },
  {
    id: "Lantai_Water_Agent",
    name: "白水",
    description: "代码、脚本、自动化",
    workspace_dir: "/tmp/water",
    enabled: true,
  },
];

describe("SubAgentToolCard", () => {
  beforeEach(() => {
    resetCollaborationEventStoreForTests();
  });

  it("shows every real batch member on the first frame without private prompts", () => {
    render(
      <SubAgentToolCard
        agents={agents}
        data={{
          status: "in_progress",
          content: [
            {
              data: {
                call_id: "call-batch-first-frame",
                name: "collaborate_with_agents",
                arguments: {
                  goal: "在多条回应路线中形成统一判断",
                  why_parallel: "先独立判断，再交叉复核关键分歧",
                  protocol: "roundtable",
                  tasks: [
                    {
                      to_agent: "Lantai_Matcha_Agent",
                      label: "起草主线",
                    },
                    {
                      to_agent: "Lantai_Cola_Agent",
                      label: "打磨标题",
                    },
                    {
                      to_agent: "Lantai_IcedTea_Agent",
                      label: "判断情绪",
                    },
                    {
                      to_agent: "Lantai_Oolong_Agent",
                      label: "检查风险",
                    },
                  ],
                },
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("协作 · 圆桌研判")).toBeInTheDocument();
    expect(screen.getByText("这个问题准备怎么回答？")).toBeInTheDocument();
    expect(
      screen.getByText("在多条回应路线中形成统一判断"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("先独立判断，再交叉复核关键分歧"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("已选 4 位协作者 · 直接发新消息可调整方向"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "协作阶段" })).toBeNull();
    for (const name of ["抹茶", "可乐", "冰茶", "乌龙"]) {
      expect(screen.getAllByText(name).length).toBeGreaterThanOrEqual(1);
    }
    for (const label of ["起草主线", "打磨标题", "判断情绪", "检查风险"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.queryByText(/私有/)).not.toBeInTheDocument();
  });

  it("does not invent a closed room from arguments after preflight failure", () => {
    const { container } = render(
      <SubAgentToolCard
        agents={agents}
        data={{
          status: "failed",
          content: [
            {
              data: {
                call_id: "call-preflight-failed",
                name: "collaborate_with_agents",
                arguments: {
                  tasks: [
                    { to_agent: "longjing", label: "军事史" },
                    { to_agent: "coconut", label: "人物对比" },
                  ],
                },
                error: "Agent is unavailable",
              },
            },
          ],
        }}
      />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("button", { name: "查看协作室" })).toBeNull();
    expect(screen.queryByText("沈行知已完成协作")).toBeNull();
  });

  it("labels a real failed room as failed instead of completed", () => {
    ingestCollaborationRuntimeEvents([
      {
        id: "event-real-room-failed",
        seq: 3,
        type: "tool.call.completed",
        status: "failed",
        source: "tool",
        item_id: "call-real-room-failed",
        created_at: "2026-08-23T00:00:00Z",
        payload: {
          collaboration: {
            batch_id: "batch-real-room-failed",
            revision: 3,
            status: "failed",
            phase: "closed",
            members: [
              {
                member_id: "member-1",
                agent_id: "Lantai_Mocha_Agent",
                display_name: "摩卡",
                status: "failed",
                attempt: 1,
                duration_ms: 40,
              },
              {
                member_id: "member-2",
                agent_id: "Lantai_Water_Agent",
                display_name: "白水",
                status: "failed",
                attempt: 1,
                duration_ms: 42,
              },
            ],
          },
        },
      },
    ]);

    render(
      <SubAgentToolCard
        agents={agents}
        data={{
          status: "failed",
          content: [
            {
              data: {
                call_id: "call-real-room-failed",
                name: "collaborate_with_agents",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("协作未形成可验收结果")).toBeInTheDocument();
    expect(screen.queryByText("沈行知已完成协作")).toBeNull();
    expect(screen.getByText("协作失败")).toBeInTheDocument();
  });

  it("updates members in place and keeps results in the collaboration room", () => {
    const tasks = [
      { to_agent: "Lantai_Matcha_Agent", label: "起草主线" },
      { to_agent: "Lantai_Cola_Agent", label: "打磨标题" },
    ];
    const { rerender } = render(
      <SubAgentToolCard
        data={{
          status: "in_progress",
          content: [
            {
              data: {
                call_id: "call-batch-live",
                name: "collaborate_with_agents",
                arguments: { tasks },
              },
            },
          ],
        }}
      />,
    );

    act(() => {
      ingestCollaborationRuntimeEvents([
        {
          id: "event-live-2",
          seq: 2,
          type: "tool.call.delta",
          status: "running",
          source: "tool",
          item_id: "call-batch-live",
          created_at: "2026-07-29T00:00:00Z",
          payload: {
            collaboration: {
              batch_id: "batch-live",
              revision: 2,
              status: "running",
              members: [
                {
                  member_id: "member-1",
                  agent_id: "Lantai_Matcha_Agent",
                  display_name: "抹茶",
                  role: "写作",
                  label: "起草主线",
                  status: "completed",
                  attempt: 1,
                  duration_ms: 120,
                },
                {
                  member_id: "member-2",
                  agent_id: "Lantai_Cola_Agent",
                  display_name: "可乐",
                  role: "文案",
                  label: "打磨标题",
                  status: "running",
                  attempt: 1,
                  duration_ms: 80,
                },
              ],
            },
          },
        },
      ]);
    });

    const rows = screen.getAllByRole("button", {
      name: /抹茶|可乐/,
    });
    expect(rows[0]).toHaveAccessibleName("抹茶：结果已回收");
    expect(rows[1]).toHaveAccessibleName("可乐：工作中");

    rerender(
      <SubAgentToolCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                call_id: "call-batch-live",
                name: "collaborate_with_agents",
                arguments: { tasks },
              },
            },
            {
              data: {
                output: [
                  {
                    type: "text",
                    text: JSON.stringify({
                      batch_status: "completed",
                      results: [
                        {
                          member_id: "member-1",
                          agent_id: "Lantai_Matcha_Agent",
                          status: "completed",
                          result: "抹茶的完整结果",
                        },
                        {
                          member_id: "member-2",
                          agent_id: "Lantai_Cola_Agent",
                          status: "completed",
                          result: "可乐的完整结果",
                        },
                      ],
                    }),
                  },
                ],
              },
            },
          ],
        }}
      />,
    );
    act(() => {
      ingestCollaborationRuntimeEvents([
        {
          id: "event-live-3",
          seq: 3,
          type: "tool.call.completed",
          status: "completed",
          source: "tool",
          item_id: "call-batch-live",
          created_at: "2026-07-29T00:00:01Z",
          payload: {
            collaboration: {
              batch_id: "batch-live",
              revision: 3,
              status: "completed",
              members: [
                {
                  member_id: "member-1",
                  agent_id: "Lantai_Matcha_Agent",
                  display_name: "抹茶",
                  role: "写作",
                  label: "起草主线",
                  status: "completed",
                  attempt: 1,
                  duration_ms: 120,
                },
                {
                  member_id: "member-2",
                  agent_id: "Lantai_Cola_Agent",
                  display_name: "可乐",
                  role: "文案",
                  label: "打磨标题",
                  status: "completed",
                  attempt: 1,
                  duration_ms: 180,
                },
              ],
            },
          },
        },
      ]);
    });

    expect(screen.queryByText("抹茶的完整结果")).not.toBeInTheDocument();
    expect(screen.queryByText("可乐的完整结果")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "抹茶：结果已回收" }),
    ).toBeDisabled();
    expect(screen.queryByText("可乐的完整结果")).not.toBeInTheDocument();
  });

  it("opens the whole hosted room from the quiet batch header", async () => {
    const user = userEvent.setup();
    const onInspect = vi.fn();
    render(
      <SubAgentToolCard
        agents={agents}
        onInspect={onInspect}
        data={{
          status: "in_progress",
          content: [
            {
              data: {
                call_id: "call-hosted-room",
                name: "collaborate_with_agents",
                arguments: {
                  goal: "形成发布计划",
                  tasks: [
                    { to_agent: "Lantai_Mocha_Agent", label: "产品检查" },
                    { to_agent: "Lantai_Water_Agent", label: "工程检查" },
                  ],
                },
              },
            },
          ],
        }}
      />,
    );

    await user.click(screen.getByRole("button", { name: "查看协作室" }));
    expect(onInspect).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "room",
        agentName: "沈行知",
        task: "形成发布计划",
        toolCallId: "call-hosted-room",
        roomSnapshot: expect.objectContaining({
          batch_id: "call-hosted-room",
          members: expect.any(Array),
        }),
      }),
    );
  });

  it("opens a completed hosted member with its result and child identity", async () => {
    const user = userEvent.setup();
    const onInspect = vi.fn();
    act(() => {
      ingestCollaborationRuntimeEvents([
        {
          id: "event-member-result",
          seq: 2,
          type: "tool.call.delta",
          status: "running",
          source: "tool",
          item_id: "call-member-result",
          created_at: "2026-08-24T11:49:00Z",
          payload: {
            collaboration: {
              schema_version: 3,
              batch_id: "batch-member-result",
              revision: 2,
              status: "running",
              phase: "working",
              members: [
                {
                  member_id: "member-1",
                  agent_id: "Lantai_Matcha_Agent",
                  display_name: "抹茶",
                  role: "写作",
                  label: "整合结果",
                  status: "completed",
                  attempt: 1,
                  duration_ms: 120,
                  session_id: "default:to:Lantai_Matcha_Agent:member-result",
                  task_id: "task-member-result",
                  result: "抹茶已验收的公开结果",
                },
              ],
            },
          },
        },
      ]);
    });

    render(
      <SubAgentToolCard
        agents={agents}
        onInspect={onInspect}
        data={{
          status: "in_progress",
          content: [
            {
              data: {
                call_id: "call-member-result",
                name: "collaborate_with_agents",
                arguments: {
                  tasks: [
                    {
                      to_agent: "Lantai_Matcha_Agent",
                      label: "整合结果",
                    },
                  ],
                },
              },
            },
          ],
        }}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "查看抹茶的协作详情" }),
    );
    expect(onInspect).toHaveBeenCalledWith(
      expect.objectContaining({
        memberId: "member-1",
        sessionId: "default:to:Lantai_Matcha_Agent:member-result",
        result: "抹茶已验收的公开结果",
      }),
    );
  });

  it("renders a concrete team agent instead of the generic chat tool name", async () => {
    const user = userEvent.setup();
    render(
      <SubAgentToolCard
        agents={agents}
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "chat_with_agent",
                arguments: {
                  to_agent: "Lantai_Mocha_Agent",
                  text: "请设计一个带图片素材的精美网页。",
                },
              },
            },
            {
              data: {
                output: [
                  {
                    type: "text",
                    text: "[SESSION: default:to:Lantai_Mocha_Agent:abc]\n\n# 设计思路\n图片 URL 可用，开始设计页面。",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getAllByText("摩卡").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("视觉设计、前端、页面")).toBeInTheDocument();
    expect(screen.getByText(/即时协作/)).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.getAllByText(/请设计一个带图片素材/).length).toBeGreaterThan(
      0,
    );

    expect(screen.queryByText("设计思路")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /展开协作任务/ }));

    expect(screen.getByText("任务")).toBeInTheDocument();
    expect(screen.getByText("当前状态")).toBeInTheDocument();
    expect(screen.getByText("协作者已返回结果")).toBeInTheDocument();
    expect(screen.getByText("设计思路")).toBeInTheDocument();
    expect(screen.getByText(/图片 URL 可用/)).toBeInTheDocument();
    expect(screen.queryByText(/\[SESSION:/)).not.toBeInTheDocument();
    expect(screen.queryByText("chat_with_agent")).not.toBeInTheDocument();
  });

  it("opens the right-side inspection target instead of expanding inline", async () => {
    const user = userEvent.setup();
    const onInspect = vi.fn();
    render(
      <SubAgentToolCard
        agents={agents}
        onInspect={onInspect}
        data={{
          status: "completed",
          content: [
            {
              data: {
                call_id: "call-inspect-mocha",
                name: "chat_with_agent",
                arguments: {
                  to_agent: "Lantai_Mocha_Agent",
                  text: "检查这个页面的交互层级。",
                },
              },
            },
            {
              data: {
                output:
                  "[SESSION: default:to:Lantai_Mocha_Agent:inspect]\n\n交互层级检查完成。",
              },
            },
          ],
        }}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "查看摩卡的协作详情" }),
    );

    expect(onInspect).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "call-inspect-mocha",
        agentId: "Lantai_Mocha_Agent",
        agentName: "摩卡",
        sessionId: "default:to:Lantai_Mocha_Agent:inspect",
        result: "交互层级检查完成。",
      }),
    );
    expect(screen.queryByText("当前状态")).not.toBeInTheDocument();
  });

  it("collapses the built-in roster into one quiet team summary", () => {
    const roster = BUILTIN_TEAM_AGENT_IDS.map((id) => ({
      id,
      name: id.replace(/^Lantai_/, "").replace(/_Agent$/, ""),
      description: "团队成员",
      enabled: true,
    }));

    render(
      <SubAgentToolCard
        agents={[]}
        data={{
          status: "completed",
          content: [
            { data: { name: "list_agents", arguments: {} } },
            {
              data: {
                output: [
                  { type: "text", text: JSON.stringify({ agents: roster }) },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("协作团队已就绪")).toBeInTheDocument();
    expect(screen.getByText("23 位专业协作者可按需调用")).toBeInTheDocument();
    expect(screen.queryByText("Americano")).not.toBeInTheDocument();
    expect(screen.queryByText("Water")).not.toBeInTheDocument();
  });

  it("treats direct team-agent tool names as sub-agent render targets", () => {
    expect(BUILTIN_TEAM_AGENT_IDS).toHaveLength(23);
    expect(isSubAgentToolName("Lantai_Water_Agent", agents)).toBe(true);
    expect(isSubAgentToolName("Water", agents)).toBe(true);
    expect(isSubAgentToolName("Lantai_Puer_Agent", agents)).toBe(true);
    expect(isSubAgentToolName("black_coffee", agents)).toBe(true);
    expect(isSubAgentToolName("memory_search", agents)).toBe(false);
  });

  it("keeps a sub-agent's private Skill implementation out of the card", async () => {
    const user = userEvent.setup();
    render(
      <SubAgentToolCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "chat_with_agent",
                arguments: {
                  to_agent: "Lantai_Puer_Agent",
                  text: "请先 read_skill，再读取 skills/puer/SKILL.md 后判断估值。",
                },
              },
            },
            {
              data: {
                output: [
                  {
                    type: "text",
                    text: "我已用 puer Skill 完成分析，skill_name=puer。",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getAllByText("普洱").length).toBeGreaterThanOrEqual(1);
    expect(
      screen.queryByText(/read_skill|SKILL\.md|\bSkill\b|skill_name/i),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /展开协作任务/ }));
    expect(
      screen.queryByText(/read_skill|SKILL\.md|\bSkill\b|skill_name/i),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText(/专业方法/).length).toBeGreaterThanOrEqual(1);
  });

  it("does not duplicate background polling events inside the conversation", () => {
    const { container } = render(
      <SubAgentToolCard
        agents={agents}
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "check_agent_task",
                arguments: { task_id: "task_1234567890abcdef" },
              },
            },
            {
              data: {
                output: [
                  {
                    type: "text",
                    text: "[TASK_ID: task_1234567890abcdef]\n[STATUS: running]\n\nTask is still running...",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText(/task_12345/)).not.toBeInTheDocument();
  });

  it("shows submitted work as a compact delegated job without raw ids", async () => {
    const user = userEvent.setup();
    render(
      <SubAgentToolCard
        agents={agents}
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "submit_to_agent",
                arguments: {
                  to_agent: "Lantai_Water_Agent",
                  text: "请在后台检查构建失败原因。",
                },
              },
            },
            {
              data: {
                output: [
                  {
                    type: "text",
                    text: "[TASK_ID: build-task-1]\n[SESSION: console:to:Lantai_Water_Agent:abc]\n\nTask submitted successfully.\nCheck status with: check_agent_task(task_id='build-task-1')",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getAllByText("白水").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/异步协作/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("已派发").length).toBeGreaterThanOrEqual(1);
    expect(
      screen.queryByText("任务已派发，等待协作者处理"),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /展开协作任务/ }));
    expect(screen.getByText("任务已派发，等待协作者处理")).toBeInTheDocument();
    expect(screen.queryByText(/\[TASK_ID:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Check status with/)).not.toBeInTheDocument();
    expect(screen.queryByText("build-task-1")).not.toBeInTheDocument();
  });
});
