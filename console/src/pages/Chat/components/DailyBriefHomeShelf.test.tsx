import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/common_setup";
import type { DailyBriefHomeSnapshot } from "@/api/types/dailyBrief";
import type { ContinuationSuggestion } from "../continuationSuggestions";
import DailyBriefHomeShelf from "./DailyBriefHomeShelf";

const continuationItems: ContinuationSuggestion[] = [
  {
    id: "run:one",
    kind: "run",
    title: "继续 Runtime 工作",
    summary: "返回原任务",
    sourceLabel: "未完成会话",
    timestamp: "2026-09-20T07:00:00Z",
    actionPath: "/chat/chat-one",
  },
  {
    id: "today:one",
    kind: "today",
    title: "今日评审",
    summary: "下午三点",
    sourceLabel: "今日",
    timestamp: "2026-09-20T07:00:00Z",
    actionPath: "/today",
  },
  {
    id: "artifact:one",
    kind: "artifact",
    title: "最近产物",
    summary: "上一版方案",
    sourceLabel: "最近产物",
    timestamp: "2026-09-20T06:00:00Z",
    actionPath: "/chat/chat-two",
  },
];

const readySnapshot: DailyBriefHomeSnapshot = {
  state: "ready",
  visible: true,
  card: {
    title: "澜台简报",
    status_line: "2026-09-20",
    hint: "有新内容可阅读",
    route: "/daily-brief",
    edition_id: "edition-2026-09-20",
    edition_revision: 1,
    edition_date: "2026-09-20",
    item_count: 2,
    has_update: true,
    is_stale: false,
  },
};

describe("DailyBriefHomeShelf", () => {
  it("keeps the original continuation shelf after the invitation is hidden", () => {
    renderWithProviders(
      <DailyBriefHomeShelf
        snapshot={{
          state: "hidden",
          visible: false,
          card: {
            title: "澜台简报",
            status_line: "已隐藏",
            hint: "",
            route: "/daily-brief",
            edition_id: null,
            edition_revision: null,
            edition_date: null,
            item_count: null,
            has_update: false,
            is_stale: false,
          },
        }}
        continuationItems={continuationItems}
        onOpenBrief={vi.fn()}
        onOpenContinuation={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("region", { name: "继续上次工作" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "澜台简报" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("开启澜台简报")).not.toBeInTheDocument();
  });

  it("shows the brief plus one quiet continuation without loading brief content", async () => {
    const user = userEvent.setup();
    const onOpenBrief = vi.fn();
    const onOpenContinuation = vi.fn();
    renderWithProviders(
      <DailyBriefHomeShelf
        snapshot={readySnapshot}
        continuationItems={continuationItems}
        onOpenBrief={onOpenBrief}
        onOpenContinuation={onOpenContinuation}
      />,
    );

    expect(
      screen.getByRole("region", { name: "接续与发现" }),
    ).toBeInTheDocument();
    expect(screen.getByText("澜台简报")).toBeInTheDocument();
    expect(screen.getByText("2026-09-20")).toBeInTheDocument();
    expect(screen.queryByText("事实")).not.toBeInTheDocument();
    expect(screen.queryByText("趋势与反证")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(2);
    expect(screen.queryByText("今日评审")).not.toBeInTheDocument();
    expect(screen.queryByText("最近产物")).not.toBeInTheDocument();
    expect(screen.queryByText("继续上次工作")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "打开澜台简报：2026-09-20" }),
    );
    expect(onOpenBrief).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "继续 Runtime 工作" }));

    expect(onOpenContinuation).toHaveBeenCalledWith(continuationItems[0]);
  });

  it("keeps the not-configured state at the same entry-card scale", () => {
    renderWithProviders(
      <DailyBriefHomeShelf
        snapshot={{
          state: "not_configured",
          visible: true,
          card: {
            title: "澜台简报",
            status_line: "选择你想关注的内容",
            hint: "点击后进入设置，不会自动联网",
            route: "/daily-brief",
            edition_id: null,
            edition_revision: null,
            edition_date: null,
            item_count: null,
            has_update: false,
            is_stale: false,
          },
        }}
        continuationItems={continuationItems}
        onOpenBrief={vi.fn()}
        onOpenContinuation={vi.fn()}
      />,
    );

    expect(screen.getAllByRole("button")).toHaveLength(2);
    expect(screen.getByText("选择你想关注的内容")).toBeInTheDocument();
  });

  it.each([
    ["generating", "正在生成", "上一期 2026-09-19 仍可阅读"],
    ["no_update", "2026-09-20 已检查", "没有值得新增的发现"],
    ["partial", "2026-09-20 部分完成", "部分来源未能完成查证"],
    ["failed", "本次未完成", "未完成更新，点击查看原因"],
    ["paused", "已暂停", "点击查看当前设置"],
    ["stale", "上一期 2026-09-19", "当前没有新一期"],
  ] as const)(
    "keeps the %s state as metadata-only entry content",
    (state, statusLine, hint) => {
      const { container } = renderWithProviders(
        <DailyBriefHomeShelf
          snapshot={{
            state,
            visible: true,
            card: {
              title: "澜台简报",
              status_line: statusLine,
              hint,
              route: "/daily-brief",
              edition_id: state === "failed" ? null : "edition-existing",
              edition_revision: state === "failed" ? null : 1,
              edition_date: state === "failed" ? null : "2026-09-19",
              item_count: null,
              has_update: state === "partial",
              is_stale: state === "failed" || state === "stale",
            },
          }}
          continuationItems={[]}
          onOpenBrief={vi.fn()}
          onOpenContinuation={vi.fn()}
        />,
      );

      expect(screen.getAllByRole("button")).toHaveLength(1);
      expect(screen.getByText(statusLine)).toBeInTheDocument();
      expect(screen.getByText(hint)).toBeInTheDocument();
      expect(screen.queryByText("趋势与反证")).not.toBeInTheDocument();
      expect(container.querySelectorAll("article")).toHaveLength(0);
    },
  );
});
