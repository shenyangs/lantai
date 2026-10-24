import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { codingApi } from "../../api/modules/coding";
import type { CodeTaskRun } from "../../api/types";
import { renderWithProviders } from "../../test/common_setup";
import TerminalPage from "./index";

const runningTask: CodeTaskRun = {
  task_id: "task-1",
  workspace_dir: "/tmp/lantai-project",
  command: "pytest -q",
  status: "running",
  started_at: "2026-07-31T12:00:00Z",
  ended_at: null,
  returncode: null,
  stdout_tail: "first chunk\n",
  stderr_tail: "",
  output_seq: 1,
};

describe("TerminalPage", () => {
  beforeEach(() => {
    vi.spyOn(codingApi, "getWorkbenchState").mockResolvedValue({
      workspace_dir: "/tmp/lantai-project",
    } as never);
    vi.spyOn(codingApi, "listTasks").mockResolvedValue({
      tasks: [runningTask],
    });
    vi.spyOn(codingApi, "stopTask").mockResolvedValue({
      ...runningTask,
      status: "stopping",
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows output from a command that is still running", async () => {
    renderWithProviders(<TerminalPage />, { initialEntries: ["/terminal"] });

    expect(screen.getByText("工作区命令")).toBeInTheDocument();
    expect(
      screen.getByText(
        /\u666e\u901a\u4f7f\u7528\u4e0d\u9700\u8981\u8fdb\u5165\u8fd9\u91cc/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "返回设置中心" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("first chunk")).toBeInTheDocument();
    expect(screen.getAllByText("运行中").length).toBeGreaterThan(0);
    expect(screen.getByText("/tmp/lantai-project")).toBeInTheDocument();
  });

  it("starts a guarded command and selects the queued task", async () => {
    const queuedTask: CodeTaskRun = {
      ...runningTask,
      task_id: "task-2",
      command: "npm test",
      status: "queued",
      stdout_tail: "",
      output_seq: 0,
    };
    const startTask = vi
      .spyOn(codingApi, "startTask")
      .mockImplementation(async () => {
        vi.mocked(codingApi.listTasks).mockResolvedValue({
          tasks: [queuedTask, runningTask],
        });
        return queuedTask;
      });
    const user = userEvent.setup();
    renderWithProviders(<TerminalPage />, { initialEntries: ["/terminal"] });

    await screen.findByText("first chunk");
    await user.type(screen.getByPlaceholderText(/npm test/), "npm test");
    await user.click(screen.getByRole("button", { name: /运行命令/ }));

    await waitFor(() =>
      expect(startTask).toHaveBeenCalledWith({
        command: "npm test",
        confirmed: true,
        timeout_seconds: 300,
      }),
    );
    expect((await screen.findAllByText("npm test")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("排队中").length).toBeGreaterThan(0);
  });
});
