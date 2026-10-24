import { describe, expect, it } from "vitest";

import type { RuntimeEvent } from "../../../api/types/runtime";
import {
  deriveCollaborationParticipants,
  deriveHostedCollaborationProjection,
} from "./collaborationProjection";

function event(
  seq: number,
  status: string,
  payload: Record<string, unknown>,
): RuntimeEvent {
  return {
    id: `event-${seq}`,
    seq,
    type: status === "completed" ? "item.completed" : "item.started",
    status,
    source: "agent",
    turn_id: "run-child",
    payload,
    created_at: `2026-07-27T10:0${seq}:00Z`,
  };
}

describe("collaboration room projection", () => {
  it("merges a child run event sequence into one latest participant card", () => {
    const participants = deriveCollaborationParticipants(
      [
        event(1, "running", {
          phase: "subtask",
          parent_run_id: "run-parent",
          agent_name: "白水",
          agent_role: "代码与自动化",
          delegation_reason: "并行检查实现",
          current_tool: "read_file",
          sub_run: { run_id: "run-child", status: "running" },
        }),
        event(2, "completed", {
          phase: "subtask",
          parent_run_id: "run-parent",
          agent_name: "白水",
          result_summary: "已完成实现检查",
          sub_run: { run_id: "run-child", status: "completed" },
        }),
      ],
      "run-parent",
    );

    expect(participants).toHaveLength(1);
    expect(participants[0]).toMatchObject({
      runId: "run-child",
      agentName: "白水",
      role: "代码与自动化",
      status: "completed",
      resultSummary: "已完成实现检查",
      eventCount: 2,
    });
  });

  it("ignores ordinary tool events and does not mistake the parent for a child", () => {
    const participants = deriveCollaborationParticipants(
      [
        event(1, "running", {
          phase: "tool",
          current_tool: "read_file",
        }),
        {
          ...event(2, "completed", {
            phase: "subtask",
            parent_run_id: "run-parent",
            agent_name: "审校",
            sub_run: { status: "completed" },
          }),
          turn_id: "run-parent",
        },
      ],
      "run-parent",
    );

    expect(participants).toHaveLength(1);
    expect(participants[0].runId).toBeNull();
    expect(participants[0].agentName).toBe("审校");
  });

  it("rebuilds the authoritative hosted room from the latest runtime revision", () => {
    const events = [1, 3, 2].map((revision) => ({
      ...event(revision, revision === 3 ? "completed" : "running", {
        collaboration: {
          schema_version: 2,
          batch_id: "room-1",
          revision,
          conversation_revision: revision,
          status: revision === 3 ? "completed" : "running",
          phase: revision === 3 ? "closed" : "working",
          host: {
            agent_id: "default",
            display_name: "沈行知",
            role: "host_and_final_owner",
          },
          members: [
            {
              member_id: "member-1",
              agent_id: "Lantai_Water_Agent",
              display_name: "白水",
              role: "工程",
              label: "检查实现",
              status: revision === 3 ? "completed" : "running",
              attempt: 1,
              duration_ms: 20,
            },
          ],
          closure:
            revision === 3
              ? {
                  outcome: "ready_for_synthesis",
                  synthesis_ready: true,
                  deliverable_ready: false,
                  user_action_required: false,
                  completed_members: 1,
                  failed_members: 0,
                  cancelled_members: 0,
                  open_conflicts: 0,
                  acceptance_met: ["完成检查"],
                  acceptance_unmet: [],
                  summary: "已完成并关房",
                }
              : null,
        },
      }),
      type: revision === 3 ? "tool.call.completed" : "tool.call.delta",
    }));

    const room = deriveHostedCollaborationProjection(events);
    expect(room?.revision).toBe(3);
    expect(room?.phase).toBe("closed");
    expect(room?.closure?.outcome).toBe("ready_for_synthesis");
    expect(room?.closure?.synthesis_ready).toBe(true);
    expect(room?.closure?.deliverable_ready).toBe(false);
    expect(room?.members[0].display_name).toBe("白水");
  });
});
