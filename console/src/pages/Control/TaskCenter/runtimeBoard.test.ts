import { describe, expect, it } from "vitest";

import type { RuntimeRunSummary } from "../../../api/types/runtime";
import {
  RUNTIME_BOARD_COLUMNS,
  groupRuntimeRunsByBoardColumn,
  runtimeRunBoardColumn,
} from "./runtimeBoard";

function run(
  status: RuntimeRunSummary["status"],
  overrides: Partial<RuntimeRunSummary> = {},
): RuntimeRunSummary {
  return {
    id: `run-${status}`,
    display_id: `run-${status}`,
    source: "chat",
    title: status,
    summary: "",
    status,
    can_interrupt: false,
    can_retry: false,
    usage: {
      input_tokens: 0,
      output_tokens: 0,
      cache_tokens: 0,
      context_tokens: 0,
      cost_points: 0,
      tool_calls: 0,
      artifact_bytes: 0,
    },
    last_event_summary: "",
    event_count: 0,
    debug_refs: {},
    ...overrides,
  };
}

describe("runtime task board projection", () => {
  it("maps authoritative runtime states without creating editable task state", () => {
    expect(runtimeRunBoardColumn(run("idle"))).toBe("pending");
    expect(runtimeRunBoardColumn(run("running"))).toBe("active");
    expect(
      runtimeRunBoardColumn(
        run("running", {
          pending_approval: true,
          authority_status: "waiting_approval",
        }),
      ),
    ).toBe("review");
    expect(runtimeRunBoardColumn(run("completed"))).toBe("done");
    expect(runtimeRunBoardColumn(run("stalled"))).toBe("blocked");
  });

  it("keeps all five board columns in a stable product order", () => {
    const grouped = groupRuntimeRunsByBoardColumn([
      run("completed"),
      run("running"),
    ]);

    expect(RUNTIME_BOARD_COLUMNS.map((column) => column.id)).toEqual([
      "pending",
      "active",
      "review",
      "done",
      "blocked",
    ]);
    expect(grouped.active).toHaveLength(1);
    expect(grouped.done).toHaveLength(1);
    expect(grouped.pending).toEqual([]);
  });
});
