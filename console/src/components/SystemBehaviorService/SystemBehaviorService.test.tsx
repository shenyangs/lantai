import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SystemBehaviorService from "./index";

const { mockListChatActivity, mockListChats, mockGetSettings } = vi.hoisted(
  () => ({
    mockListChatActivity: vi.fn(),
    mockListChats: vi.fn(),
    mockGetSettings: vi.fn(),
  }),
);

vi.mock("../../api/modules/chat", () => ({
  chatApi: {
    listChatActivity: mockListChatActivity,
    listChats: mockListChats,
  },
}));

vi.mock("../../api/modules/settings", () => ({
  settingsApi: {
    getSystemBehaviorSettings: mockGetSettings,
  },
}));

describe("SystemBehaviorService", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListChatActivity.mockResolvedValue([
      { id: "chat-1", name: "Long task", status: "running" },
    ]);
    mockGetSettings.mockResolvedValue({});
    Object.assign(window, {
      electronAPI: {
        setTaskActivity: vi.fn().mockResolvedValue(undefined),
        notifySystemBehavior: vi.fn().mockResolvedValue(undefined),
        updateSystemBehavior: vi.fn().mockResolvedValue(undefined),
      },
    });
  });

  it("polls only lightweight activity instead of the full chat list", async () => {
    render(<SystemBehaviorService />);

    await waitFor(() => expect(mockListChatActivity).toHaveBeenCalledOnce());
    expect(mockListChats).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(window.electronAPI?.setTaskActivity).toHaveBeenCalledWith({
        sourceId: "chat:chat-1",
        running: true,
      }),
    );

    await act(async () => undefined);
  });

  it("keeps Cron active while notifying foreground completion exactly once", async () => {
    vi.useFakeTimers();
    const cron = {
      id: "chat-1",
      name: "Scheduled report",
      status: "running",
      kind: "cron",
    };
    mockListChatActivity
      .mockResolvedValueOnce([
        { id: "chat-1", name: "Foreground", status: "running" },
        cron,
      ])
      .mockResolvedValueOnce([cron])
      .mockResolvedValueOnce([]);
    const view = render(<SystemBehaviorService />);
    try {
      await act(async () => undefined);
      expect(window.electronAPI?.setTaskActivity).toHaveBeenCalledWith({
        sourceId: "cron:chat-1",
        running: true,
      });
      await act(async () => vi.advanceTimersByTimeAsync(5000));
      expect(window.electronAPI?.notifySystemBehavior).toHaveBeenCalledTimes(1);
      expect(window.electronAPI?.notifySystemBehavior).toHaveBeenCalledWith(
        expect.objectContaining({ type: "turn_completed", body: "Foreground" }),
      );
      expect(window.electronAPI?.setTaskActivity).not.toHaveBeenCalledWith({
        sourceId: "cron:chat-1",
        running: false,
      });
      await act(async () => vi.advanceTimersByTimeAsync(5000));
      expect(window.electronAPI?.setTaskActivity).toHaveBeenCalledWith({
        sourceId: "cron:chat-1",
        running: false,
      });
      expect(window.electronAPI?.notifySystemBehavior).toHaveBeenCalledTimes(1);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
  it("retains activity on poll failure and releases Cron ownership on unmount", async () => {
    vi.useFakeTimers();
    mockListChatActivity
      .mockResolvedValueOnce([
        { id: "scheduled", name: "Cron", status: "running", kind: "cron" },
      ])
      .mockRejectedValueOnce(new Error("temporary disconnect"));
    const view = render(<SystemBehaviorService />);
    try {
      await act(async () => undefined);
      await act(async () => vi.advanceTimersByTimeAsync(5000));
      expect(window.electronAPI?.setTaskActivity).not.toHaveBeenCalledWith({
        sourceId: "cron:scheduled",
        running: false,
      });
      expect(window.electronAPI?.notifySystemBehavior).not.toHaveBeenCalled();
      view.unmount();
      expect(window.electronAPI?.setTaskActivity).toHaveBeenCalledWith({
        sourceId: "cron:scheduled",
        running: false,
      });
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
});
