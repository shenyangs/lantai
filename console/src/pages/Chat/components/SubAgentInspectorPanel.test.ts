import { describe, expect, it } from "vitest";

import type { RuntimeEvent } from "../../../api/types/chat";
import type { RuntimeRunSummary } from "../../../api/types/runtime";
import {
  findSubAgentRun,
  resultFromSubAgentEvents,
  runtimeEventBelongsToChildRun,
  runtimeEventMatchesSubAgent,
  type SubAgentInspectionTarget,
} from "./SubAgentInspectorPanel";

const target: SubAgentInspectionTarget = {
  id: "batch-1:member-2",
  agentId: "Lantai_Water_Agent",
  agentName: "白水",
  role: "代码与测试",
  task: "做技术检查",
  status: "running",
  result: "",
  toolName: "collaborate_with_agents",
  toolCallId: "call-batch-1",
  memberId: "member-2",
};

function runtimeEvent(
  payload: Record<string, unknown>,
  overrides: Partial<RuntimeEvent> = {},
): RuntimeEvent {
  return {
    id: "event-1",
    seq: 1,
    type: "tool.call.delta",
    status: "running",
    source: "tool",
    payload,
    created_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

describe("SubAgentInspectorPanel helpers", () => {
  it("matches only the selected collaboration member", () => {
    const event = runtimeEvent({
      collaboration: {
        members: [
          {
            member_id: "member-1",
            agent_id: "Lantai_Mocha_Agent",
            display_name: "摩卡",
            status: "running",
          },
          {
            member_id: "member-2",
            agent_id: "Lantai_Water_Agent",
            display_name: "白水",
            status: "completed",
          },
        ],
      },
    });

    expect(runtimeEventMatchesSubAgent(event, target)).toBe(true);
    expect(
      runtimeEventMatchesSubAgent(event, {
        ...target,
        memberId: "member-3",
        agentId: "Lantai_Matcha_Agent",
        agentName: "抹茶",
      }),
    ).toBe(false);
  });

  it("extracts the selected member result from a shared batch output", () => {
    const event = runtimeEvent(
      {
        output: JSON.stringify({
          results: [
            {
              member_id: "member-1",
              agent_id: "Lantai_Mocha_Agent",
              result: "视觉检查完成",
            },
            {
              member_id: "member-2",
              agent_id: "Lantai_Water_Agent",
              result: "测试全部通过",
            },
          ],
        }),
      },
      { type: "tool.call.completed", status: "completed" },
    );

    expect(resultFromSubAgentEvents([event], target)).toBe("测试全部通过");
  });

  it("resolves an independent child run by its returned session id", () => {
    const runs = [
      {
        id: "child-run-1",
        session_id: "default:to:Lantai_Water_Agent:abc",
      },
    ] as RuntimeRunSummary[];

    expect(
      findSubAgentRun(
        runs,
        {
          ...target,
          memberId: undefined,
          sessionId: "default:to:Lantai_Water_Agent:abc",
        },
        "parent-run",
        [],
      )?.id,
    ).toBe("child-run-1");
  });

  it("prefers the authoritative room session over same-name legacy runs", () => {
    const runs = [
      {
        id: "stale-run",
        session_id: "old-session",
        title: "白水",
      },
      {
        id: "live-run",
        session_id: "host:to:water:attempt-2",
        title: "白水",
      },
    ] as RuntimeRunSummary[];

    expect(
      findSubAgentRun(
        runs,
        { ...target, sessionId: "host:to:water:attempt-2" },
        "parent-run",
        [],
      )?.id,
    ).toBe("live-run");
  });

  it("isolates live events when switching between child sessions", () => {
    const selected = runtimeEvent(
      { run_id: "child-run-2" },
      { session_id: "child-session-2", turn_id: "child-run-2" },
    );
    const stale = runtimeEvent(
      { run_id: "child-run-1" },
      { session_id: "child-session-1", turn_id: "child-run-1" },
    );

    expect(
      runtimeEventBelongsToChildRun(selected, "child-run-2", "child-session-2"),
    ).toBe(true);
    expect(
      runtimeEventBelongsToChildRun(stale, "child-run-2", "child-session-2"),
    ).toBe(false);
  });
});
