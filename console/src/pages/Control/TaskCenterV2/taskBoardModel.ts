import type {
  RuntimeTaskBoardItem,
  RuntimeTaskLane,
} from "../../../api/types/runtime";

export interface TaskLaneDefinition {
  id: RuntimeTaskLane;
  label: string;
  description: string;
  marker: string;
}

export const TASK_LANES: TaskLaneDefinition[] = [
  {
    id: "pending",
    label: "待开始",
    description: "已保存，尚未占用执行资源",
    marker: "queue",
  },
  {
    id: "active",
    label: "进行中",
    description: "智能体正在推进",
    marker: "live",
  },
  {
    id: "needs_user",
    label: "等你决定",
    description: "需要你的审批、选择或补充信息",
    marker: "input",
  },
  {
    id: "done",
    label: "已交付",
    description: "已结束且交付经过验证",
    marker: "done",
  },
  {
    id: "needs_action",
    label: "需要处理",
    description: "暂停、停止中、失败或交付待确认",
    marker: "attention",
  },
];

export function groupTaskBoardItems(
  items: RuntimeTaskBoardItem[],
): Record<RuntimeTaskLane, RuntimeTaskBoardItem[]> {
  const grouped: Record<RuntimeTaskLane, RuntimeTaskBoardItem[]> = {
    pending: [],
    active: [],
    needs_user: [],
    done: [],
    needs_action: [],
  };
  items.forEach((item) => grouped[item.lane].push(item));
  return grouped;
}

export function taskBoardPollInterval(
  items: RuntimeTaskBoardItem[],
  hidden: boolean,
): number {
  if (hidden) return 30_000;
  return items.some((item) => ["active", "needs_user"].includes(item.lane))
    ? 2_500
    : 15_000;
}

export function taskStatusLabel(item: RuntimeTaskBoardItem): string {
  const authorityStatus = String(item.authority_status || "").toLowerCase();
  if (item.lane === "needs_action") {
    if (authorityStatus === "paused") return "已暂停";
    if (authorityStatus === "stop_requested") return "正在停止";
    if (authorityStatus === "cancelled") return "已停止";
    if (authorityStatus === "failed") return "执行失败";
    if (authorityStatus === "stalled") return "执行停滞";
    if (authorityStatus === "succeeded") {
      if (item.delivery_status === "partial") return "部分完成";
      if (item.delivery_status === "not_delivered") return "尚未交付";
      return "交付待确认";
    }
    return "需要处理";
  }
  const labels: Record<RuntimeTaskLane, string> = {
    pending: "待开始",
    active: "进行中",
    needs_user: "等你决定",
    done: "已交付",
    needs_action: "需要处理",
  };
  return labels[item.lane];
}

export function taskStatusDescription(item: RuntimeTaskBoardItem): string {
  const authorityStatus = String(item.authority_status || "").toLowerCase();
  if (item.lane === "needs_action") {
    if (authorityStatus === "paused") {
      return "任务已经暂停，可以恢复运行或结束任务。";
    }
    if (authorityStatus === "stop_requested") {
      return "停止请求已经提交，正在等待运行安全结束。";
    }
    if (authorityStatus === "cancelled") {
      return "任务已经停止，已有结果仍会保留。";
    }
    if (authorityStatus === "failed") {
      return "任务执行失败，需要查看原因后再决定是否重试。";
    }
    if (authorityStatus === "stalled") {
      return "任务长时间没有进展，可以从检查点继续或人工接管。";
    }
    if (authorityStatus === "succeeded") {
      return item.delivery_status === "partial"
        ? "任务已经结束，但只形成了部分结果。"
        : "任务已经结束，但交付结果还没有通过验证。";
    }
  }
  const descriptions: Record<RuntimeTaskLane, string> = {
    pending: "任务已经保存，尚未开始执行。",
    active: "智能体正在推进，新的进展会继续更新。",
    needs_user: "系统正在等你的审批、选择或补充信息。",
    done: "任务已经结束，交付结果经过验证。",
    needs_action: "当前状态需要进一步处理，打开详情可查看下一步。",
  };
  return descriptions[item.lane];
}

export function taskDeliveryLabel(item: RuntimeTaskBoardItem): string {
  const value = String(item.delivery_status || "").toLowerCase();
  if (value === "verified") return "结果已验证";
  if (value === "partial") return "已有部分结果";
  if (value === "not_delivered") return "尚未交付结果";
  return "结果待确认";
}

export function taskSourceLabel(source: string): string {
  return (
    {
      chat: "聊天",
      mission: "任务",
      browser: "浏览器",
      capability: "能力",
      pipeline: "流水线",
      cron: "定时任务",
      reminder: "提醒",
    }[source] || source
  );
}
