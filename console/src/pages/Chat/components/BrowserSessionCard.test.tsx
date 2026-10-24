import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { BrowserTaskRun } from "../../../api/types";
import BrowserSessionCard from "./BrowserSessionCard";

const apiMocks = vi.hoisted(() => ({
  getTask: vi.fn(),
  pauseTask: vi.fn(),
  resumeTask: vi.fn(),
  cancelTask: vi.fn(),
  confirmTask: vi.fn(),
  subscribeTaskEvents: vi.fn(() => vi.fn()),
}));

vi.mock("../../../api/modules/browserSession", () => ({
  browserWorkbenchApi: apiMocks,
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: { error: vi.fn(), warning: vi.fn(), success: vi.fn() },
  }),
}));

const runningTask: BrowserTaskRun = {
  run_id: "browser-run-42",
  profile_id: "default",
  target_domains: ["www.douban.com"],
  access: "read",
  mode: "background_observable",
  instruction: "比较最近上映电影的评分和影评",
  status: "running_background",
  created_at: "2026-08-31T08:00:00Z",
  updated_at: "2026-08-31T08:00:01Z",
  current_action: "snapshot",
  result: {},
  error_code: "",
  replay_run_id: "browser-run-42",
  planned_action_count: 8,
  completed_action_count: 3,
  requires_foreground_confirmation: false,
  pending_confirmation: null,
  metadata: {},
};

function toolData(task: BrowserTaskRun = runningTask) {
  return {
    status: "completed",
    content: [
      {
        data: {
          name: "browser_task",
          arguments: {
            operation: "create",
            url: "https://www.douban.com/",
            instruction: task.instruction,
          },
        },
      },
      {
        data: {
          output: [
            {
              type: "text",
              text: JSON.stringify({ ok: true, task }),
            },
          ],
        },
      },
    ],
  };
}

describe("BrowserSessionCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
    apiMocks.getTask.mockResolvedValue(runningTask);
    apiMocks.pauseTask.mockResolvedValue({
      ...runningTask,
      status: "paused",
    });
    apiMocks.resumeTask.mockResolvedValue(runningTask);
    apiMocks.cancelTask.mockResolvedValue({
      ...runningTask,
      status: "cancelled",
    });
    apiMocks.confirmTask.mockResolvedValue(runningTask);
    apiMocks.subscribeTaskEvents.mockReturnValue(vi.fn());
  });

  it("shows a human browser session instead of raw tool output", async () => {
    const inspect = vi.fn();
    const user = userEvent.setup();
    render(<BrowserSessionCard data={toolData()} onInspect={inspect} />);

    expect(screen.getByText("www.douban.com")).toBeInTheDocument();
    expect(
      screen.getByText("比较最近上映电影的评分和影评"),
    ).toBeInTheDocument();
    expect(screen.getByText("浏览中")).toBeInTheDocument();
    expect(screen.getByText("读取页面")).toBeInTheDocument();
    expect(screen.queryByText("browser-run-42")).not.toBeInTheDocument();
    expect(screen.queryByText("browser_task")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "查看网页" }));
    expect(inspect).toHaveBeenCalledWith(
      expect.objectContaining({
        runId: "browser-run-42",
        profileId: "default",
        url: "https://www.douban.com/",
      }),
    );

    await user.click(screen.getByRole("button", { name: "详情" }));
    expect(screen.getByText("browser-run-42")).toBeInTheDocument();
  });

  it("opens a new active browser task beside the chat only once", async () => {
    const inspect = vi.fn();
    const { rerender } = render(
      <BrowserSessionCard data={toolData()} onInspect={inspect} autoOpen />,
    );

    await waitFor(() => expect(inspect).toHaveBeenCalledOnce());
    rerender(
      <BrowserSessionCard data={toolData()} onInspect={inspect} autoOpen />,
    );
    expect(inspect).toHaveBeenCalledOnce();
  });

  it("controls an active browser task from the chat card", async () => {
    const user = userEvent.setup();
    render(<BrowserSessionCard data={toolData()} />);

    await user.click(screen.getByRole("button", { name: "暂停" }));

    await waitFor(() => {
      expect(apiMocks.pauseTask).toHaveBeenCalledWith("browser-run-42");
    });
    expect(screen.getByText("已暂停")).toBeInTheDocument();
  });

  it("keeps consequential browser actions inside the conversation", async () => {
    const user = userEvent.setup();
    const confirmationTask: BrowserTaskRun = {
      ...runningTask,
      status: "waiting_for_user_confirmation",
      pending_confirmation: {
        action: "click",
        url: "https://www.douban.com/",
      },
    };
    apiMocks.getTask.mockResolvedValue(confirmationTask);
    render(<BrowserSessionCard data={toolData(confirmationTask)} />);

    expect(screen.getByText("需要你的确认")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "允许一次" }));

    expect(apiMocks.confirmTask).toHaveBeenCalledWith("browser-run-42", {
      approved: true,
      reason: "用户在主聊天中允许本次浏览器操作",
    });
  });
});
