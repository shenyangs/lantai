import type { RuntimeRunSummary } from "../../../api/types/runtime";

export type RuntimeBoardColumnId =
  | "pending"
  | "active"
  | "review"
  | "done"
  | "blocked";

export interface RuntimeBoardColumn {
  id: RuntimeBoardColumnId;
  label: string;
  description: string;
}

export const RUNTIME_BOARD_COLUMNS: RuntimeBoardColumn[] = [
  { id: "pending", label: "待开始", description: "排队、认领或尚未启动" },
  { id: "active", label: "进行中", description: "运行时正在推进" },
  { id: "review", label: "待确认", description: "等待审批或人工确认" },
  { id: "done", label: "已完成", description: "运行已成功结束" },
  { id: "blocked", label: "需处理", description: "失败、中断、暂停或卡住" },
];

export function runtimeRunBoardColumn(
  run: RuntimeRunSummary,
): RuntimeBoardColumnId {
  const authority = String(run.authority_status || "")
    .trim()
    .toLowerCase();

  if (run.pending_approval || authority === "waiting_approval") {
    return "review";
  }
  if (run.status === "completed" || authority === "succeeded") {
    return "done";
  }
  if (
    ["failed", "interrupted", "stalled"].includes(run.status) ||
    ["failed", "cancelled", "stalled", "paused", "stop_requested"].includes(
      authority,
    )
  ) {
    return "blocked";
  }
  if (run.status === "running" || authority === "running") {
    return "active";
  }
  return "pending";
}

export function groupRuntimeRunsByBoardColumn(
  runs: RuntimeRunSummary[],
): Record<RuntimeBoardColumnId, RuntimeRunSummary[]> {
  const grouped: Record<RuntimeBoardColumnId, RuntimeRunSummary[]> = {
    pending: [],
    active: [],
    review: [],
    done: [],
    blocked: [],
  };
  for (const run of runs) {
    grouped[runtimeRunBoardColumn(run)].push(run);
  }
  return grouped;
}
