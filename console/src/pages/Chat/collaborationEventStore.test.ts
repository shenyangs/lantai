import { describe, expect, it } from "vitest";
import type { RuntimeEvent } from "../../api/types/chat";
import {
  collaborationParentChatTarget,
  collaborationSnapshotForCall,
  collaborationRuntimeControlTarget,
  ingestCollaborationRuntimeEvents,
  latestCollaborationPreparation,
  resetCollaborationEventStoreForTests,
} from "./collaborationEventStore";

function event(
  id: string,
  revision: number,
  memberStatus: string,
): RuntimeEvent {
  return {
    id,
    seq: revision,
    type: revision === 3 ? "tool.call.completed" : "tool.call.delta",
    status: revision === 3 ? "completed" : "running",
    source: "tool",
    item_id: "call-batch",
    created_at: "2026-07-29T00:00:00Z",
    payload: {
      collaboration: {
        schema_version: 2,
        batch_id: "batch-1",
        revision,
        conversation_revision: revision,
        status: revision === 3 ? "completed" : "running",
        phase: revision === 3 ? "closed" : "working",
        participation_mode: "autonomous",
        protocol: "roundtable",
        host: {
          agent_id: "default",
          display_name: "沈行知",
          role: "host_and_final_owner",
        },
        goal: "形成统一方案",
        why_parallel: "先独立判断，再交叉复核",
        acceptance_criteria: ["指出分歧"],
        execution_scope: {
          root_run_id: "root-execution-1",
          parent_run_id: "run-parent",
          parent_attempt_id: "run-parent:attempt-2",
          parent_effect_id: "run-parent:tool:call-batch",
          attempt_id: "run-parent:attempt-2",
          runtime_generation: 4,
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
          member_attempts_started: 1,
          verified_artifact_bytes: 64,
          elapsed_wall_seconds: 3,
        },
        quiet_summary: "沈行知正在主持协作。",
        unattended: true,
        members: [
          {
            member_id: "member-1",
            agent_id: "Lantai_Matcha_Agent",
            display_name: "抹茶",
            role: "写作",
            label: "起草主线",
            status: memberStatus,
            attempt: 1,
            duration_ms: 10,
            session_id: "default:to:Lantai_Matcha_Agent:attempt-1",
            task_id: "task-matcha-1",
            execution_task_id: "execution-task-matcha",
            run_id: "subagent-matcha-1",
            chat_id: "child-chat-matcha-1",
            execution_mode: "scoped_execute",
            permission_profile: "delegated_workspace",
            workspace_scope: "/tmp/workspace/member-1",
            allowed_tools: ["browser_search"],
            resource_claims: ["browser"],
            resource_leases: [
              {
                resource: "browser",
                owner_id: "batch-1:member-1",
                generation: 2,
                acquired_at_epoch_seconds: 12,
                expires_at_epoch_seconds: 42,
              },
            ],
            result:
              memberStatus === "completed" ? "已验收的公开成果" : undefined,
            round_completed: memberStatus === "completed" ? 2 : 1,
            review_status:
              memberStatus === "completed" ? "completed" : "running",
          },
        ],
      },
    },
  };
}

describe("collaborationEventStore", () => {
  it("deduplicates by batch revision and never rolls state backward", () => {
    resetCollaborationEventStoreForTests();

    ingestCollaborationRuntimeEvents([
      event("event-2", 2, "completed"),
      event("event-2-duplicate", 2, "running"),
      event("event-1-late", 1, "queued"),
    ]);

    const snapshot = collaborationSnapshotForCall("call-batch");
    expect(snapshot?.revision).toBe(2);
    expect(snapshot?.phase).toBe("working");
    expect(snapshot?.host?.display_name).toBe("沈行知");
    expect(snapshot?.unattended).toBe(true);
    expect(snapshot?.protocol).toBe("roundtable");
    expect(snapshot?.why_parallel).toBe("先独立判断，再交叉复核");
    expect(snapshot?.members[0].status).toBe("completed");
    expect(snapshot?.members[0]).toMatchObject({
      session_id: "default:to:Lantai_Matcha_Agent:attempt-1",
      task_id: "task-matcha-1",
      execution_task_id: "execution-task-matcha",
      run_id: "subagent-matcha-1",
      chat_id: "child-chat-matcha-1",
      result: "已验收的公开成果",
      round_completed: 2,
      review_status: "completed",
      execution_mode: "scoped_execute",
      permission_profile: "delegated_workspace",
      workspace_scope: "/tmp/workspace/member-1",
      allowed_tools: ["browser_search"],
      resource_claims: ["browser"],
    });
    expect(snapshot?.execution_scope).toMatchObject({
      root_run_id: "root-execution-1",
      parent_effect_id: "run-parent:tool:call-batch",
    });
    expect(snapshot?.budget_usage).toEqual({
      member_attempts_started: 1,
      verified_artifact_bytes: 64,
      elapsed_wall_seconds: 3,
      started_at_epoch_seconds: undefined,
    });
    expect(collaborationRuntimeControlTarget(snapshot)).toEqual({
      runId: "run-parent",
      request: {
        expected_attempt_id: "run-parent:attempt-2",
        chat_id: "chat-parent",
        session_id: "session-parent",
        workspace_dir: "/tmp/workspace",
      },
    });
    expect(collaborationParentChatTarget(snapshot)).toBe("chat-parent");
  });

  it("projects the collaboration acknowledgement before the first tool frame", () => {
    expect(
      latestCollaborationPreparation([
        {
          id: "event-preparing",
          ts: "2026-08-23T00:00:00Z",
          kind: "system",
          status: "running",
          label: "正在组建协作室",
          detail: "正在确定成员分工与验收条件",
          payload: {
            kind: "collaboration_preparing",
            trace_title: "正在组建协作室",
            trace_detail: "沈行知正在选择成员与分工",
          },
        },
      ]),
    ).toEqual({
      phase: "preparing",
      title: "协作请求已接收",
      detail: "沈行知正在选择成员与分工",
    });
  });

  it("projects a bounded setup retry instead of an invented closed room", () => {
    expect(
      latestCollaborationPreparation([
        {
          id: "event-preparing",
          ts: "2026-08-23T00:00:00Z",
          kind: "system",
          status: "running",
          label: "正在组建协作室",
          payload: { kind: "collaboration_preparing" },
        },
        {
          id: "event-retrying",
          ts: "2026-08-23T00:00:01Z",
          kind: "system",
          status: "running",
          label: "正在修正协作配置",
          detail: "沈行知正在重新选择成员与分工",
          payload: {
            kind: "required_capability_retrying",
            trace_title: "正在修正协作配置",
            trace_detail: "沈行知正在重新选择成员与分工",
          },
        },
      ]),
    ).toEqual({
      phase: "staffing",
      title: "正在修正协作配置",
      detail: "沈行知正在重新选择成员与分工",
    });
  });
});
