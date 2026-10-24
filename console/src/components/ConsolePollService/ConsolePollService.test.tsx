import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConsolePollService from "./index";

const { mockAcknowledgePushMessage, mockGetPushMessages, mockSetApprovals } =
  vi.hoisted(() => ({
    mockAcknowledgePushMessage: vi.fn(),
    mockGetPushMessages: vi.fn(),
    mockSetApprovals: vi.fn(),
  }));

vi.mock("../../api/modules/console", () => ({
  consoleApi: {
    acknowledgePushMessage: mockAcknowledgePushMessage,
    getPushMessages: mockGetPushMessages,
  },
}));

vi.mock("../../contexts/ApprovalContext", () => ({
  useApprovalContext: () => ({ setApprovals: mockSetApprovals }),
}));

const emptyResponse = { messages: [], pending_approvals: [] };

describe("ConsolePollService", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    mockAcknowledgePushMessage.mockResolvedValue({ acknowledged: true });
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    Object.assign(window, {
      electronAPI: {
        notifySystemBehavior: vi.fn().mockResolvedValue(undefined),
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("queues every message instead of marking truncated items as seen", async () => {
    mockGetPushMessages.mockResolvedValue({
      messages: Array.from({ length: 5 }, (_, index) => ({
        id: `message-${index + 1}`,
        text: `Message ${index + 1}`,
      })),
      pending_approvals: [],
    });

    render(<ConsolePollService />);
    await act(async () => Promise.resolve());
    expect(screen.getByText("Message 1")).toBeInTheDocument();
    expect(screen.getByText("Message 2")).toBeInTheDocument();
    expect(screen.queryByText("Message 3")).not.toBeInTheDocument();

    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(screen.getByText("Message 3")).toBeInTheDocument();
    expect(screen.getByText("Message 4")).toBeInTheDocument();

    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(screen.getByText("Message 5")).toBeInTheDocument();
  });

  it("abandons a hung poll after its deadline and resumes later polls", async () => {
    mockGetPushMessages
      .mockImplementationOnce(() => new Promise(() => undefined))
      .mockImplementationOnce(() => new Promise(() => undefined))
      .mockResolvedValue(emptyResponse);

    render(<ConsolePollService />);
    await act(async () => Promise.resolve());
    expect(mockGetPushMessages).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(16_999));
    expect(mockGetPushMessages).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(mockGetPushMessages).toHaveBeenCalledTimes(2);

    await act(async () => vi.advanceTimersByTimeAsync(12_000));
    expect(screen.getByText("通知更新暂停，正在自动重试")).toBeInTheDocument();

    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(mockGetPushMessages).toHaveBeenCalledTimes(3);
    expect(
      screen.queryByText("通知更新暂停，正在自动重试"),
    ).not.toBeInTheDocument();
  });

  it("does not spend a notification's visible lifetime while hidden", async () => {
    mockGetPushMessages.mockResolvedValue({
      messages: [{ id: "message-1", text: "Still here" }],
      pending_approvals: [],
    });

    render(<ConsolePollService />);
    await act(async () => Promise.resolve());
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });

    await act(async () => vi.advanceTimersByTimeAsync(20_000));
    expect(screen.getByText("Still here")).toBeInTheDocument();

    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(screen.queryByText("Still here")).not.toBeInTheDocument();
  });

  it("keeps sourced sticky notifications until acknowledgement succeeds", async () => {
    mockGetPushMessages.mockResolvedValue({
      messages: [
        {
          id: "important-1",
          text: "Delivery failed",
          sticky: true,
          occurred_at: 1_725_000_000,
          session_id: "session/1",
          source_kind: "cron_job",
          source_id: "job-1",
          source_name: "Daily review",
        },
      ],
      pending_approvals: [],
    });

    render(<ConsolePollService />);
    await act(async () => Promise.resolve());
    expect(screen.getByText("需确认")).toBeInTheDocument();
    expect(screen.getByText("Daily review")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看定时任务" })).toHaveAttribute(
      "href",
      "#/cron-jobs",
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认并关闭" }));
      await Promise.resolve();
    });

    expect(mockAcknowledgePushMessage).toHaveBeenCalledWith("important-1");
    expect(screen.queryByText("Delivery failed")).not.toBeInTheDocument();
  });

  it("keeps a sticky notification visible when acknowledgement fails", async () => {
    mockAcknowledgePushMessage.mockRejectedValueOnce(new Error("offline"));
    mockGetPushMessages.mockResolvedValue({
      messages: [{ id: "important-2", text: "Please verify", sticky: true }],
      pending_approvals: [],
    });

    render(<ConsolePollService />);
    await act(async () => Promise.resolve());
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认并关闭" }));
      await Promise.resolve();
    });

    expect(screen.getByText("Please verify")).toBeInTheDocument();
    expect(
      screen.getByText("未能确认，这条消息仍保留。请稍后重试。"),
    ).toBeInTheDocument();
  });
});
