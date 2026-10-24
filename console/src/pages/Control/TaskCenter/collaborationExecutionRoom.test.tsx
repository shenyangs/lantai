import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { describe, expect, it, vi } from "vitest";

import type { CollaborationBatchSnapshot } from "../../../api/types/chat";
import type { RuntimeRunSummary } from "../../../api/types/runtime";
import CollaborationRoom from "./CollaborationRoom";

describe("execution collaboration room", () => {
  it("shows each member's scoped tools, workspace, and accepted artifacts", async () => {
    const user = userEvent.setup();
    const parentRun = {
      id: "run-parent",
      display_id: "run-parent",
      title: "生成发布材料",
      status: "running",
    } as RuntimeRunSummary;
    const hostedSnapshot = {
      schema_version: 2,
      batch_id: "exec-room",
      revision: 4,
      conversation_revision: 4,
      status: "running",
      phase: "working",
      participation_mode: "autonomous",
      protocol: "parallel_consultation",
      current_round: 1,
      unattended: true,
      host: {
        agent_id: "default",
        display_name: "沈行知",
        role: "host_and_final_owner",
      },
      goal: "生成发布材料",
      why_parallel: "研究和编辑按依赖执行",
      quiet_summary: "正在验收执行成员产物。",
      acceptance_criteria: ["产物可核验"],
      execution_scope: {
        root_run_id: "run-root",
        parent_run_id: "run-parent",
        parent_attempt_id: "attempt-parent",
        parent_effect_id: "effect-parent",
      },
      budget: {
        max_member_attempts: 2,
        max_wall_seconds: 1800,
        reserved_host_seconds: 30,
        max_artifact_bytes: 1024,
        max_member_response_tokens: 8192,
      },
      budget_usage: {
        member_attempts_started: 1,
        verified_artifact_bytes: 128,
        elapsed_wall_seconds: 3,
      },
      members: [
        {
          member_id: "member-1",
          agent_id: "research",
          display_name: "研究员",
          role: "研究",
          label: "检索并写入材料",
          status: "completed",
          attempt: 1,
          duration_ms: 120,
          execution_mode: "scoped_execute",
          permission_profile: "delegated_workspace",
          workspace_scope: ".lantai/execution-teams/room/member-1",
          execution_task_id: "exec-room:member-1",
          allowed_tools: ["web_search", "write_file"],
          resource_claims: ["browser", "service:4173"],
          resource_leases: [
            {
              resource: "browser",
              owner_id: "exec-room:member-1:attempt-1",
              generation: 2,
              acquired_at_epoch_seconds: 100,
              expires_at_epoch_seconds: 200,
            },
          ],
          artifacts: [
            {
              path: "outputs/research.md",
              media_type: "text/markdown",
              size_bytes: 128,
              sha256: "a".repeat(64),
            },
          ],
        },
      ],
      host_decisions: [],
      conflicts: [],
    } as CollaborationBatchSnapshot;

    render(
      <App>
        <CollaborationRoom
          parentRun={parentRun}
          participants={[]}
          hostedSnapshot={hostedSnapshot}
          availableRuns={[]}
          onOpenRun={vi.fn()}
          onShowBoard={vi.fn()}
        />
      </App>,
    );

    await user.click(screen.getByRole("button", { name: "成员" }));

    expect(screen.getByText("尝试 1/2")).toBeInTheDocument();
    expect(screen.getByText("权限：delegated_workspace")).toBeInTheDocument();
    expect(
      screen.getByText("工作区：.lantai/execution-teams/room/member-1"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("工具：web_search、write_file"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("执行任务：exec-room:member-1"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("独占资源：browser、service:4173"),
    ).toBeInTheDocument();
    expect(screen.getByText("当前租约：browser#2")).toBeInTheDocument();
    expect(screen.getByText("已验收产物：1 个")).toBeInTheDocument();
    expect(
      screen.getByText("隔离执行成员·由主持人统一验收交付"),
    ).toBeInTheDocument();
  });
});
