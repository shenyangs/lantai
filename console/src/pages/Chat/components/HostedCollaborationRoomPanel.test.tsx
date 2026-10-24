import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { CollaborationBatchSnapshot } from "../../../api/types/chat";
import HostedCollaborationRoomPanel from "./HostedCollaborationRoomPanel";

const runtimeControls = vi.hoisted(() => ({
  pauseRun: vi.fn().mockResolvedValue({}),
  resumeRun: vi.fn().mockResolvedValue({}),
  interruptRun: vi.fn().mockResolvedValue({}),
}));

vi.mock("../../../api/modules/runtime", () => ({
  runtimeApi: runtimeControls,
}));

const snapshot: CollaborationBatchSnapshot = {
  schema_version: 2,
  batch_id: "room-1",
  revision: 8,
  conversation_revision: 2,
  status: "running",
  phase: "verifying",
  participation_mode: "autonomous",
  protocol: "roundtable",
  current_round: 2,
  unattended: true,
  host: {
    agent_id: "default",
    display_name: "沈行知",
    role: "host_and_final_owner",
  },
  goal: "梳理下周发布计划",
  why_parallel: "发布节奏与风险需要先独立判断，再交叉复核",
  execution_scope: {
    root_run_id: "root-execution-1",
    parent_run_id: "run-parent",
    attempt_id: "run-parent:attempt-2",
    runtime_generation: 3,
    chat_id: "chat-parent",
    session_id: "session-parent",
    workspace_dir: "/tmp/workspace",
  },
  budget: {
    max_member_attempts: 4,
    max_wall_seconds: 900,
    reserved_host_seconds: 60,
    max_artifact_bytes: 1024,
    max_member_response_tokens: 2000,
  },
  budget_usage: {
    member_attempts_started: 2,
    verified_artifact_bytes: 64,
    elapsed_wall_seconds: 8,
  },
  quiet_summary: "正在处理 1 项冲突并验证交付。",
  acceptance_criteria: ["时间可执行", "风险已标明"],
  members: [
    {
      member_id: "member-1",
      agent_id: "Lantai_Coconut_Agent",
      display_name: "椰汁",
      role: "研究",
      label: "梳理竞品节奏",
      status: "completed",
      attempt: 1,
      duration_ms: 120,
      result: "竞品节奏支持周三发布，但需预留回滚窗口。",
      round_completed: 2,
      review_status: "completed",
    },
    {
      member_id: "member-2",
      agent_id: "Lantai_Longjing_Agent",
      display_name: "龙井",
      role: "复核",
      label: "检查风险",
      status: "waiting_resource",
      attempt: 1,
      duration_ms: 80,
      execution_task_id: "execution-task-risk",
      resource_claims: ["browser", "service:4173"],
      round_completed: 1,
      review_status: "running",
    },
  ],
  host_decisions: [
    {
      decision_id: "decision-1",
      phase: "verifying",
      safe_summary: "采用周三发布节奏",
      reason: "依赖已齐备",
      revision: 8,
    },
  ],
  conflicts: [
    {
      conflict_id: "conflict-1",
      safe_summary: "回滚窗口尚需确认",
      member_ids: ["member-2"],
      status: "open",
      resolution_summary: "",
    },
  ],
};

describe("HostedCollaborationRoomPanel", () => {
  it("shows a quiet autonomous room and lets the user take over", async () => {
    const user = userEvent.setup();
    const onTakeover = vi.fn();
    render(
      <MemoryRouter>
        <App>
          <HostedCollaborationRoomPanel
            callId="call-room-1"
            initialSnapshot={snapshot}
            onClose={vi.fn()}
            onTakeover={onTakeover}
          />
        </App>
      </MemoryRouter>,
    );

    expect(screen.getByText("沈行知主持")).toBeInTheDocument();
    expect(screen.getByText(/圆桌第 2\/2 轮/)).toBeInTheDocument();
    expect(screen.getAllByText("验证中").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/执行预算：尝试 2\/4/)).toBeInTheDocument();
    expect(screen.getByText("采用周三发布节奏")).toBeInTheDocument();
    expect(
      screen.getByText("竞品节奏支持周三发布，但需预留回滚窗口。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("无需参会 · 直接发新消息可调整方向"),
    ).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "协作阶段" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "暂停协作" }));
    expect(runtimeControls.pauseRun).toHaveBeenCalledWith("run-parent", {
      expected_attempt_id: "run-parent:attempt-2",
      chat_id: "chat-parent",
      session_id: "session-parent",
      workspace_dir: "/tmp/workspace",
    });
    expect(
      screen.getByRole("button", { name: "继续协作" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "停止本次协作" }));
    expect(runtimeControls.interruptRun).toHaveBeenCalledWith("run-parent", {
      expected_attempt_id: "run-parent:attempt-2",
      chat_id: "chat-parent",
      session_id: "session-parent",
      workspace_dir: "/tmp/workspace",
    });

    await user.click(screen.getByRole("button", { name: /^成员/ }));
    expect(screen.getByText("椰汁")).toBeInTheDocument();
    expect(screen.getByText("龙井")).toBeInTheDocument();
    expect(screen.getByText("等待独占资源")).toBeInTheDocument();
    expect(
      screen.getByText(/任务 execution-task-risk · 资源 browser、service:4173/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^结论与证据/ }));
    expect(screen.getByText("回滚窗口尚需确认")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "调整方向" }));
    expect(onTakeover).toHaveBeenCalledTimes(1);
  });

  it("shows a failed closed room as failed", () => {
    render(
      <MemoryRouter>
        <App>
          <HostedCollaborationRoomPanel
            callId="call-room-failed"
            initialSnapshot={{
              ...snapshot,
              batch_id: "batch-room-failed",
              status: "failed",
              phase: "closed",
              members: snapshot.members.map((member) => ({
                ...member,
                status: "failed",
              })),
            }}
            onClose={vi.fn()}
          />
        </App>
      </MemoryRouter>,
    );

    expect(screen.getAllByText("协作失败").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("已关房")).toBeNull();
    expect(screen.getByText("协作未形成结果 · 可追溯")).toBeInTheDocument();
  });
});
