import { describe, expect, it } from "vitest";
import {
  buildSessionRows,
  formatSessionTime,
  getSessionPreview,
  getSessionStatus,
  getSessionTitle,
  isBlankSession,
  matchesSessionFilter,
} from "./presentation";

const now = new Date("2026-07-30T10:30:00+08:00");

describe("chat session presentation", () => {
  it("groups pinned and recent sessions without duplicating pinned rows", () => {
    const rows = buildSessionRows(
      [
        {
          id: "today",
          updatedAt: "2026-07-30T09:00:00+08:00",
        },
        {
          id: "pinned",
          pinned: true,
          updatedAt: "2026-07-29T09:00:00+08:00",
        },
        {
          id: "yesterday",
          updatedAt: "2026-07-29T08:00:00+08:00",
        },
      ],
      now,
    );

    expect(
      rows.filter((row) => row.kind === "group").map((row) => row.label),
    ).toEqual(["置顶", "今天", "昨天"]);
    expect(
      rows.filter((row) => row.kind === "session").map((row) => row.session.id),
    ).toEqual(["pinned", "today", "yesterday"]);
  });

  it("prefers a visible result summary and normalizes markdown", () => {
    expect(
      getSessionPreview({
        id: "one",
        meta: {
          last_result_summary: "## 完成\n- 已修复 **三个** 问题",
          last_turn_text: "请修复",
        },
      }),
    ).toBe("完成 已修复 三个 问题");
  });

  it("removes markdown from generated session titles", () => {
    expect(getSessionTitle({ id: "one", name: "**山窗听雨**" })).toBe(
      "山窗听雨",
    );
  });

  it("maps live, failed and completed runs to clear status labels", () => {
    expect(getSessionStatus({ id: "live", generating: true })).toEqual({
      label: "运行中",
      tone: "running",
    });
    expect(
      getSessionStatus({
        id: "failed",
        meta: { last_turn_status: "failed" },
      }),
    ).toEqual({ label: "执行失败", tone: "danger" });
    expect(
      getSessionStatus({
        id: "done",
        meta: {
          last_result_summary: "完成",
          last_turn_outcome_status: "delivered",
        },
      }),
    ).toEqual({ label: "任务成功", tone: "success" });
    expect(
      getSessionStatus({
        id: "legacy-done",
        meta: {
          last_result_summary: "已交付报告",
          last_turn_status: "finished",
        },
      }),
    ).toEqual({ label: "回复结束", tone: "neutral" });
    expect(
      getSessionStatus({
        id: "needs-input",
        meta: { last_turn_outcome_status: "needs_input" },
      }),
    ).toEqual({ label: "待你处理", tone: "warning" });
    expect(
      getSessionStatus({
        id: "partial",
        meta: { last_turn_outcome_status: "incomplete" },
      }),
    ).toEqual({ label: "部分完成", tone: "warning" });
  });

  it("does not equate any assistant summary with completed work", () => {
    expect(
      getSessionStatus({
        id: "chat-replied",
        meta: { last_result_summary: "我回答了这个问题" },
      }),
    ).toEqual({ label: "最近对话", tone: "neutral" });
  });

  it("hides only genuinely unstarted default chats", () => {
    expect(isBlankSession({ id: "blank", name: "New Chat" })).toBe(true);
    expect(
      isBlankSession({
        id: "started",
        name: "New Chat",
        meta: { last_turn_text: "已经发送" },
      }),
    ).toBe(false);
    expect(isBlankSession({ id: "named", name: "手动命名" })).toBe(false);
  });

  it("filters active, attention, completed, and archived sessions", () => {
    expect(
      matchesSessionFilter({ id: "running", status: "running" }, "active"),
    ).toBe(true);
    expect(
      matchesSessionFilter(
        { id: "blocked", meta: { last_turn_outcome_status: "blocked" } },
        "attention",
      ),
    ).toBe(true);
    expect(
      matchesSessionFilter(
        {
          id: "completed",
          meta: { last_turn_outcome_status: "delivered" },
        },
        "completed",
      ),
    ).toBe(true);
    expect(
      matchesSessionFilter({ id: "archived", archived: true }, "archived"),
    ).toBe(true);
    expect(
      matchesSessionFilter({ id: "archived", archived: true }, "all"),
    ).toBe(false);
  });

  it("keeps collapsed date groups visible while omitting their sessions", () => {
    const rows = buildSessionRows(
      [
        {
          id: "today",
          updatedAt: "2026-07-30T09:00:00+08:00",
        },
        {
          id: "older",
          updatedAt: "2026-07-20T09:00:00+08:00",
        },
      ],
      now,
      new Set(["earlier"]),
    );

    expect(rows.map((row) => row.key)).toEqual([
      "group:today",
      "session:today",
      "group:earlier",
    ]);
    expect(rows.at(-1)).toMatchObject({
      kind: "group",
      collapsed: true,
      count: 1,
    });
  });

  it("uses compact time labels", () => {
    expect(
      formatSessionTime(
        { id: "today", updatedAt: "2026-07-30T09:07:00+08:00" },
        now,
        "Asia/Shanghai",
      ),
    ).toBe("09:07");
    expect(
      formatSessionTime(
        { id: "older", updatedAt: "2026-07-20T09:07:00+08:00" },
        now,
      ),
    ).toBe("07-20");
  });
});
