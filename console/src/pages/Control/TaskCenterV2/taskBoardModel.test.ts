import { describe, expect, it } from "vitest";

import type { RuntimeTaskBoardItem } from "../../../api/types/runtime";
import {
  TASK_LANES,
  groupTaskBoardItems,
  taskBoardPollInterval,
  taskDeliveryLabel,
  taskSourceLabel,
  taskStatusLabel,
  taskStatusDescription,
} from "./taskBoardModel";

function item(
  id: string,
  lane: RuntimeTaskBoardItem["lane"],
): RuntimeTaskBoardItem {
  return {
    id,
    display_id: id,
    title: id,
    summary: "",
    source: "chat",
    lane,
    outcome_status: lane === "done" ? "delivered" : "unknown",
    authority_status: "queued",
    transport_status: "unknown",
    delivery_status: "pending",
    is_history: false,
    pending_approval: false,
    ambiguous_effect: false,
    budget: {},
    usage: {
      input_tokens: 0,
      output_tokens: 0,
      cache_tokens: 0,
      context_tokens: 0,
      cost_points: 0,
      tool_calls: 0,
      artifact_bytes: 0,
    },
    recovery: {},
    can_start: lane === "pending",
    can_interrupt: lane === "active",
    can_retry: false,
    can_resume: false,
    can_open_chat: true,
    event_count: 0,
    last_event_summary: "",
    debug_refs: {},
  };
}

describe("taskBoardModel", () => {
  it("groups authoritative items into exactly five lanes", () => {
    const grouped = groupTaskBoardItems([
      item("a", "pending"),
      item("b", "active"),
      item("c", "needs_user"),
      item("d", "done"),
      item("e", "needs_action"),
    ]);

    expect(Object.keys(grouped)).toEqual([
      "pending",
      "active",
      "needs_user",
      "done",
      "needs_action",
    ]);
    expect(TASK_LANES.find((lane) => lane.id === "done")?.label).toBe("已交付");
    expect(grouped.needs_user[0].id).toBe("c");
    expect(taskStatusLabel(grouped.done[0])).toBe("已交付");
    expect(taskStatusLabel(grouped.needs_user[0])).toBe("等你决定");
    expect(taskStatusLabel(grouped.needs_action[0])).toBe("需要处理");
    expect(taskStatusDescription(grouped.needs_user[0])).toContain("你的");
    expect(taskStatusDescription(grouped.needs_action[0])).toContain(
      "进一步处理",
    );
    expect(
      taskDeliveryLabel({ ...grouped.done[0], delivery_status: "verified" }),
    ).toBe("结果已验证");
  });

  it("does not describe pauses or stop requests as failures", () => {
    const action = item("action", "needs_action");
    expect(taskStatusLabel({ ...action, authority_status: "paused" })).toBe(
      "已暂停",
    );
    expect(
      taskStatusDescription({ ...action, authority_status: "paused" }),
    ).toContain("可以恢复运行");
    expect(
      taskStatusLabel({ ...action, authority_status: "stop_requested" }),
    ).toBe("正在停止");
    expect(
      taskStatusDescription({
        ...action,
        authority_status: "stop_requested",
      }),
    ).toContain("停止请求已经提交");
    expect(
      taskStatusLabel({
        ...action,
        authority_status: "succeeded",
        delivery_status: "partial",
      }),
    ).toBe("部分完成");
  });

  it("polls active work faster and hidden pages slowly", () => {
    expect(taskBoardPollInterval([item("a", "active")], false)).toBe(2_500);
    expect(taskBoardPollInterval([item("a", "done")], false)).toBe(15_000);
    expect(taskBoardPollInterval([item("a", "active")], true)).toBe(30_000);
  });

  it("uses the same name for scheduled execution across task views", () => {
    expect(taskSourceLabel("cron")).toBe("定时任务");
  });
});
