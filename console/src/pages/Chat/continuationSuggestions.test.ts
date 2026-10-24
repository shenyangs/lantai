import { describe, expect, it } from "vitest";
import type { TaskRun, TodayTimeline } from "../../api/types";
import { selectContinuationSuggestions } from "./continuationSuggestions";

const runs: TaskRun[] = [
  {
    id: "run-1",
    agent_id: "default",
    source: "chat",
    chat_id: "chat-1",
    title: "完善发布说明",
    user_intent: "继续完善发布说明",
    status: "blocked",
    started_at: "2026-08-01T08:00:00Z",
    steps: [],
    artifacts: [
      {
        id: "artifact-1",
        type: "markdown",
        title: "发布说明草稿",
        path: "/workspace/release.md",
        created_at: "2026-08-01T08:30:00Z",
      },
    ],
  },
];

const today: TodayTimeline = {
  date: "2026-08-01",
  timezone: "Asia/Shanghai",
  generated_at: "2026-08-01T09:00:00Z",
  counts: { reminder: 1, task: 0, automation: 0 },
  items: [
    {
      id: "reminder-1",
      kind: "reminder",
      source: "lantai",
      source_label: "提醒",
      title: "发布复盘",
      summary: "讨论发布结果",
      start_at: "2026-08-01T10:00:00Z",
      end_at: "2026-08-01T11:00:00Z",
      all_day: false,
      blocking: false,
      status: "scheduled",
      action_path: "/today",
      metadata: {},
    },
  ],
};

describe("selectContinuationSuggestions", () => {
  it("projects at most one run, today item and artifact with source paths", () => {
    const suggestions = selectContinuationSuggestions(runs, today, {
      now: new Date("2026-08-01T09:00:00Z"),
    });

    expect(suggestions.map((item) => item.kind)).toEqual([
      "run",
      "today",
      "artifact",
    ]);
    expect(suggestions[0]).toMatchObject({
      title: "完善发布说明",
      sourceLabel: "未完成会话",
      actionPath: "/chat/chat-1",
    });
    expect(suggestions[1].actionPath).toBe("/today");
    expect(suggestions[2]).toMatchObject({
      title: "发布说明草稿",
      actionPath: "/chat/chat-1",
    });
  });

  it("fails soft and never returns more than three suggestions", () => {
    expect(selectContinuationSuggestions([], null)).toEqual([]);
    expect(selectContinuationSuggestions(runs, today)).toHaveLength(3);
  });
});
