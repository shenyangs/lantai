import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { Modal } from "antd";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/common_setup";
import ChatHeaderTitle from "./index";

const { mockUseChatAnywhereSessionsState } = vi.hoisted(() => ({
  mockUseChatAnywhereSessionsState: vi.fn(),
}));

const {
  mockGetSessionList,
  mockRemoveSession,
  mockSelectSession,
  mockUpdateSession,
} = vi.hoisted(() => ({
  mockGetSessionList: vi.fn(),
  mockRemoveSession: vi.fn(),
  mockSelectSession: vi.fn(),
  mockUpdateSession: vi.fn(),
}));

const { mockRetryTurn, mockUpdateChat } = vi.hoisted(() => ({
  mockRetryTurn: vi.fn(),
  mockUpdateChat: vi.fn(),
}));

vi.mock("@/vendor/lantaiChat", () => ({
  useChatAnywhereSessionsState: mockUseChatAnywhereSessionsState,
}));

vi.mock("../../sessionApi", () => ({
  default: {
    getSessionList: mockGetSessionList,
    removeSession: mockRemoveSession,
    selectSession: mockSelectSession,
    updateSession: mockUpdateSession,
  },
}));

vi.mock("../../../../api/modules/chat", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../../../api/modules/chat")>();
  return {
    ...actual,
    chatApi: {
      ...actual.chatApi,
      retryTurn: mockRetryTurn,
      updateChat: mockUpdateChat,
    },
  };
});

describe("ChatHeaderTitle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetSessionList.mockResolvedValue([]);
    mockRemoveSession.mockResolvedValue([]);
    mockUpdateChat.mockResolvedValue(undefined);
    mockRetryTurn.mockResolvedValue({ queued: false, text: "" });
    mockUpdateSession.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("displays the current session name", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [{ id: "sess-1", name: "My Chat" }],
      currentSessionId: "sess-1",
    });
    renderWithProviders(<ChatHeaderTitle />);
    expect(screen.getByText("My Chat")).toBeInTheDocument();
  });

  it("renders generated Markdown titles as plain text", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [{ id: "sess-1", name: "**山窗听雨**" }],
      currentSessionId: "sess-1",
    });
    renderWithProviders(<ChatHeaderTitle />);
    expect(screen.getByText("山窗听雨")).toBeInTheDocument();
    expect(screen.queryByText("**山窗听雨**")).not.toBeInTheDocument();
  });

  it('displays "新会话" when session name is empty', () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [{ id: "sess-1", name: "" }],
      currentSessionId: "sess-1",
    });
    renderWithProviders(<ChatHeaderTitle />);
    expect(screen.getByText("新会话")).toBeInTheDocument();
  });

  it('displays "新会话" when no matching session exists', () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [],
      currentSessionId: null,
    });
    renderWithProviders(<ChatHeaderTitle />);
    expect(screen.getByText("新会话")).toBeInTheDocument();
  });

  it("displays the correct session name after switching currentSessionId", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [
        { id: "sess-1", name: "Chat A" },
        { id: "sess-2", name: "Chat B" },
      ],
      currentSessionId: "sess-2",
    });
    renderWithProviders(<ChatHeaderTitle />);
    expect(screen.getByText("Chat B")).toBeInTheDocument();
    expect(screen.queryByText("Chat A")).not.toBeInTheDocument();
  });

  it("skips a no-op list refresh without re-selecting the active session", async () => {
    const setCurrentSessionId = vi.fn();
    const setSessions = vi.fn();
    const sessions = [
      {
        id: "sess-1",
        name: "My Chat",
        updatedAt: "2026-05-31T01:00:00Z",
      },
    ];
    mockGetSessionList.mockResolvedValue(sessions);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions,
      currentSessionId: "sess-1",
      setCurrentSessionId,
      setSessions,
    });

    renderWithProviders(<ChatHeaderTitle />);
    await userEvent.click(screen.getByRole("button", { name: /My Chat/ }));

    await waitFor(() => expect(mockGetSessionList).toHaveBeenCalledTimes(1));
    expect(setSessions).not.toHaveBeenCalled();
    expect(setCurrentSessionId).not.toHaveBeenCalled();
    expect(mockSelectSession).not.toHaveBeenCalled();
  });

  it("keeps the cached list visible without exposing background refresh", async () => {
    const setSessions = vi.fn();
    let resolveList: (value: unknown) => void = () => {};
    const refreshPromise = new Promise((resolve) => {
      resolveList = resolve;
    });
    const sessions = [
      {
        id: "sess-1",
        name: "My Chat",
        updatedAt: "2026-05-31T01:00:00Z",
      },
      {
        id: "sess-2",
        name: "Another Chat",
        updatedAt: "2026-05-31T00:00:00Z",
      },
    ];
    mockGetSessionList.mockReturnValue(refreshPromise);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions,
      currentSessionId: "sess-1",
      setCurrentSessionId: vi.fn(),
      setSessions,
    });

    renderWithProviders(<ChatHeaderTitle />);
    await userEvent.click(screen.getByRole("button", { name: /My Chat/ }));

    expect(screen.queryByText("刷新中")).not.toBeInTheDocument();
    expect(screen.getByText("Another Chat")).toBeInTheDocument();

    resolveList(sessions);
    await waitFor(() => expect(mockGetSessionList).toHaveBeenCalledTimes(1));
    expect(setSessions).not.toHaveBeenCalled();
  });

  it("labels the title dropdown as an isolated conversation switcher", async () => {
    const sessions = [
      { id: "current", name: "当前新对话" },
      { id: "older", name: "过往对话" },
    ];
    mockGetSessionList.mockResolvedValue(sessions);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions,
      currentSessionId: "current",
      setCurrentSessionId: vi.fn(),
      setSessions: vi.fn(),
    });

    renderWithProviders(<ChatHeaderTitle />);
    await userEvent.click(screen.getByTestId("chat-session-switcher"));

    expect(screen.getByRole("note")).toHaveTextContent(
      "切换会话每个会话的消息与任务上下文彼此独立",
    );
  });

  it("shows the settled result preview and completed status mark", async () => {
    const session = {
      id: "sess-complete",
      name: "会话列表性能优化",
      updatedAt: "2026-08-09T12:00:00+08:00",
      meta: {
        last_result_summary: "已移除闲置会话的事件账本扫描",
        last_turn_outcome_status: "delivered",
      },
    };
    mockGetSessionList.mockResolvedValue([session]);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [session],
      currentSessionId: session.id,
      setCurrentSessionId: vi.fn(),
      setSessions: vi.fn(),
    });

    renderWithProviders(<ChatHeaderTitle />);
    await userEvent.click(screen.getByTestId("chat-session-switcher"));

    expect(
      screen.getByText("已移除闲置会话的事件账本扫描"),
    ).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "任务成功" })).toBeInTheDocument();
  });

  it("hides unstarted default chats and can reveal archived sessions", async () => {
    const sessions = [
      { id: "blank", name: "New Chat" },
      {
        id: "active",
        name: "活跃会话",
        updatedAt: "2026-08-09T12:00:00+08:00",
      },
      {
        id: "archived",
        name: "已归档会话",
        archived: true,
        updatedAt: "2026-08-08T12:00:00+08:00",
      },
    ];
    mockGetSessionList.mockResolvedValue(sessions);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions,
      currentSessionId: "active",
      setCurrentSessionId: vi.fn(),
      setSessions: vi.fn(),
    });

    renderWithProviders(<ChatHeaderTitle />);
    await userEvent.click(screen.getByTestId("chat-session-switcher"));

    expect(screen.queryByText("New Chat")).not.toBeInTheDocument();
    expect(screen.queryByText("已归档会话")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "已归档" }));
    const archivedList = screen.getByRole("listbox");
    expect(within(archivedList).getByText("已归档会话")).toBeInTheDocument();
    expect(
      within(archivedList).queryByText("活跃会话"),
    ).not.toBeInTheDocument();
  });

  it("searches titles and result summaries", async () => {
    const sessions = [
      {
        id: "one",
        name: "登录页重构",
        meta: { last_result_summary: "已完成移动端适配" },
      },
      { id: "two", name: "数据导出" },
    ];
    mockGetSessionList.mockResolvedValue(sessions);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions,
      currentSessionId: "one",
      setCurrentSessionId: vi.fn(),
      setSessions: vi.fn(),
    });

    renderWithProviders(<ChatHeaderTitle />);
    await userEvent.click(screen.getByTestId("chat-session-switcher"));
    await userEvent.type(
      screen.getByRole("textbox", { name: "搜索会话" }),
      "移动端",
    );

    const resultList = screen.getByRole("listbox");
    expect(within(resultList).getByText("登录页重构")).toBeInTheDocument();
    expect(within(resultList).queryByText("数据导出")).not.toBeInTheDocument();
  });

  it("archives sessions through row actions", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const setSessions = vi.fn();
    const session = {
      id: "sess-1",
      name: "My Chat",
      updatedAt: "2026-05-31T01:00:00Z",
    };
    mockGetSessionList.mockResolvedValue([session]);
    mockUpdateSession.mockImplementation(async (patch) => [
      { ...session, ...patch },
    ]);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [session],
      currentSessionId: session.id,
      setCurrentSessionId: vi.fn(),
      setSessions,
    });

    renderWithProviders(<ChatHeaderTitle />);
    await user.click(screen.getByTestId("chat-session-switcher"));
    await user.click(screen.getByRole("button", { name: "会话操作：My Chat" }));
    await user.click(await screen.findByRole("menuitem", { name: "归档" }));

    await waitFor(() =>
      expect(mockUpdateChat).toHaveBeenCalledWith("sess-1", {
        archived: true,
        pinned: false,
      }),
    );
    expect(setSessions).toHaveBeenCalled();
  });

  it("pins sessions and copies a stable deep link from row actions", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const clipboardWrite = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue(undefined);
    const session = {
      id: "sess-actions",
      name: "可操作会话",
      updatedAt: "2026-08-09T12:00:00+08:00",
    };
    mockGetSessionList.mockResolvedValue([session]);
    mockUpdateSession.mockResolvedValue([{ ...session, pinned: true }]);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [session],
      currentSessionId: session.id,
      setCurrentSessionId: vi.fn(),
      setSessions: vi.fn(),
    });

    renderWithProviders(<ChatHeaderTitle />);
    await user.click(screen.getByTestId("chat-session-switcher"));
    await user.click(
      screen.getByRole("button", { name: "会话操作：可操作会话" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "置顶" }));

    await waitFor(() =>
      expect(mockUpdateChat).toHaveBeenCalledWith("sess-actions", {
        pinned: true,
      }),
    );

    await user.click(
      screen.getByRole("button", { name: "会话操作：可操作会话" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "复制会话链接" }),
    );
    expect(clipboardWrite).toHaveBeenCalledWith(
      expect.stringMatching(/#\/chat\/sess-actions$/),
    );
  });

  it("supports arrow-key navigation and group collapse", async () => {
    const user = userEvent.setup();
    const sessions = [
      {
        id: "today",
        name: "今日会话",
        updatedAt: new Date().toISOString(),
      },
      {
        id: "older",
        name: "更早会话",
        updatedAt: "2025-01-01T00:00:00Z",
      },
    ];
    mockGetSessionList.mockResolvedValue(sessions);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions,
      currentSessionId: "today",
      setCurrentSessionId: vi.fn(),
      setSessions: vi.fn(),
    });

    renderWithProviders(<ChatHeaderTitle />);
    await user.click(screen.getByTestId("chat-session-switcher"));
    const todayOption = screen.getByRole("option", { name: /今日会话/ });
    todayOption.focus();
    await user.keyboard("{ArrowDown}{ArrowUp}{ArrowLeft}");

    const todayGroup = screen.getByRole("button", { name: /今天/ });
    expect(todayGroup).toHaveAttribute("aria-expanded", "false");
    todayGroup.focus();
    await user.keyboard("{ArrowRight}");
    expect(todayGroup).toHaveAttribute("aria-expanded", "true");
  });

  it("prepares a failed turn for safe user-confirmed retry", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const session = {
      id: "sess-retry",
      name: "失败任务",
      meta: {
        last_turn_text: "请重新生成报告",
        last_turn_run_id: "run-retry",
        last_turn_status: "failed",
      },
    };
    mockRetryTurn.mockResolvedValue({
      queued: false,
      text: "请重新生成报告",
    });
    mockGetSessionList.mockResolvedValue([session]);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [session],
      currentSessionId: session.id,
      setCurrentSessionId: vi.fn(),
      setSessions: vi.fn(),
    });

    renderWithProviders(<ChatHeaderTitle />);
    await user.click(screen.getByTestId("chat-session-switcher"));
    await user.click(screen.getByRole("button", { name: "重试：失败任务" }));

    await waitFor(() => {
      expect(mockRetryTurn).toHaveBeenCalledWith("sess-retry", "run-retry");
      expect(mockSelectSession).toHaveBeenCalledWith("sess-retry");
    });
    expect(window.sessionStorage.getItem("lantai:retry-draft:sess-retry")).toBe(
      "请重新生成报告",
    );
  });

  it("renders long histories progressively", async () => {
    const sessions = Array.from({ length: 101 }, (_, index) => ({
      id: `session-${index}`,
      name: `会话 ${index}`,
      updatedAt: new Date(Date.UTC(2026, 7, 9, 12, 0, index)).toISOString(),
    }));
    mockGetSessionList.mockResolvedValue(sessions);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions,
      currentSessionId: "session-100",
      setCurrentSessionId: vi.fn(),
      setSessions: vi.fn(),
    });

    renderWithProviders(<ChatHeaderTitle />);
    await userEvent.click(screen.getByTestId("chat-session-switcher"));

    expect(screen.getAllByRole("option")).toHaveLength(80);
    await userEvent.click(screen.getByRole("button", { name: /显示更多/ }));
    expect(screen.getAllByRole("option")).toHaveLength(101);
  });

  it("marks session selection as user-initiated before switching sessions", async () => {
    const setCurrentSessionId = vi.fn();
    const sessions = [
      {
        id: "sess-1",
        name: "My Chat",
        updatedAt: "2026-05-31T01:00:00Z",
      },
      {
        id: "sess-2",
        name: "Another Chat",
        updatedAt: "2026-05-31T00:00:00Z",
      },
    ];
    mockGetSessionList.mockResolvedValue(sessions);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions,
      currentSessionId: "sess-1",
      setCurrentSessionId,
      setSessions: vi.fn(),
    });

    renderWithProviders(<ChatHeaderTitle />);
    await userEvent.click(screen.getByRole("button", { name: /My Chat/ }));
    await userEvent.click(screen.getByText("Another Chat"));

    expect(mockSelectSession).toHaveBeenCalledWith("sess-2");
    expect(setCurrentSessionId).toHaveBeenCalledWith("sess-2");
  });

  it("renames a session inline from its secondary actions menu", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const setSessions = vi.fn();
    const sessions = [
      {
        id: "sess-1",
        name: "My Chat",
        updatedAt: "2026-05-31T01:00:00Z",
      },
    ];
    const renamedSessions = [
      {
        ...sessions[0],
        name: "Renamed Chat",
      },
    ];
    mockGetSessionList.mockResolvedValue(sessions);
    mockUpdateSession.mockResolvedValue(renamedSessions);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions,
      currentSessionId: "sess-1",
      setCurrentSessionId: vi.fn(),
      setSessions,
    });

    renderWithProviders(<ChatHeaderTitle />);
    await user.click(screen.getByTestId("chat-session-switcher"));
    await user.click(
      await screen.findByRole("button", { name: "会话操作：My Chat" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "重命名" }));

    const input = await screen.findByRole("textbox", { name: "会话名称" });
    await user.clear(input);
    await user.type(input, "Renamed Chat");
    await user.keyboard("{Enter}");

    await waitFor(() => {
      expect(mockUpdateChat).toHaveBeenCalledWith("sess-1", {
        name: "Renamed Chat",
      });
      expect(mockUpdateSession).toHaveBeenCalledWith({
        id: "sess-1",
        name: "Renamed Chat",
      });
    });
    expect(mockUpdateChat).toHaveBeenCalledTimes(1);
    expect(mockUpdateSession).toHaveBeenCalledTimes(1);
    expect(setSessions).toHaveBeenCalledWith(renamedSessions);
  });

  it("requires confirmation and safely switches after deleting the active session", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const setCurrentSessionId = vi.fn();
    const setSessions = vi.fn();
    const sessions = [
      {
        id: "sess-1",
        name: "My Chat",
        updatedAt: "2026-05-31T01:00:00Z",
      },
      {
        id: "sess-2",
        name: "Next Chat",
        updatedAt: "2026-05-31T00:00:00Z",
      },
    ];
    mockGetSessionList.mockResolvedValue(sessions);
    mockRemoveSession.mockResolvedValue([sessions[1]]);
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions,
      currentSessionId: "sess-1",
      setCurrentSessionId,
      setSessions,
    });
    const confirm = vi
      .spyOn(Modal, "confirm")
      .mockImplementation(() => ({ destroy: vi.fn(), update: vi.fn() }) as any);

    renderWithProviders(<ChatHeaderTitle />);
    await user.click(screen.getByTestId("chat-session-switcher"));
    await user.click(
      await screen.findByRole("button", { name: "会话操作：My Chat" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "删除" }));

    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "删除“My Chat”？",
        okButtonProps: { danger: true },
      }),
    );
    const config = confirm.mock.calls[0]?.[0];
    await config?.onOk?.();

    await waitFor(() => {
      expect(mockRemoveSession).toHaveBeenCalledWith(sessions[0]);
      expect(setSessions).toHaveBeenCalledWith([sessions[1]]);
      expect(setCurrentSessionId).toHaveBeenCalledWith("sess-2");
      expect(mockSelectSession).toHaveBeenCalledWith("sess-2");
    });
  });

  it("runs an accepted delete action only once while it is pending", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    let resolveDelete: (value: unknown[]) => void = () => {};
    mockRemoveSession.mockReturnValue(
      new Promise((resolve) => {
        resolveDelete = resolve;
      }),
    );
    const session = {
      id: "sess-1",
      name: "My Chat",
      updatedAt: "2026-05-31T01:00:00Z",
    };
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [session],
      currentSessionId: "sess-1",
      setCurrentSessionId: vi.fn(),
      setSessions: vi.fn(),
    });
    const confirm = vi
      .spyOn(Modal, "confirm")
      .mockImplementation(() => ({ destroy: vi.fn(), update: vi.fn() }) as any);

    renderWithProviders(<ChatHeaderTitle />);
    await user.click(screen.getByTestId("chat-session-switcher"));
    await user.click(
      await screen.findByRole("button", { name: "会话操作：My Chat" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "删除" }));

    const onOk = confirm.mock.calls[0]?.[0]?.onOk;
    const first = onOk?.();
    const second = onOk?.();
    expect(mockRemoveSession).toHaveBeenCalledTimes(1);

    resolveDelete([]);
    await Promise.all([first, second]);
  });
});
