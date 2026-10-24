import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import Sidebar from "./Sidebar";
import { SIDEBAR_VISIBILITY_STORAGE_KEY } from "./sidebarPreferences";

const {
  mockGetLastChatId,
  mockListChannels,
  mockListChats,
  mockNavigate,
  mockGetTaskBoard,
  mockGetEffectiveState,
  mockGetDailyBriefHomeSnapshot,
  mockAgentState,
} = vi.hoisted(() => ({
  mockGetLastChatId: vi.fn(),
  mockListChannels: vi.fn(),
  mockListChats: vi.fn(),
  mockNavigate: vi.fn(),
  mockGetTaskBoard: vi.fn(() => new Promise(() => {})),
  mockGetEffectiveState: vi.fn(() => new Promise(() => {})),
  mockGetDailyBriefHomeSnapshot: vi.fn(() => new Promise(() => {})),
  mockAgentState: { selectedAgent: "default" },
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      key === "channels.channelNames.wechat"
        ? "微信"
        : options?.defaultValue || key,
    i18n: { language: "zh", resolvedLanguage: "zh" },
  }),
}));

vi.mock("../plugins/PluginContext", () => ({
  usePlugins: () => ({ pluginRoutes: [] }),
}));

vi.mock("../stores/agentStore", () => ({
  useAgentStore: () => ({
    selectedAgent: mockAgentState.selectedAgent,
    getLastChatId: mockGetLastChatId,
  }),
}));

vi.mock("../api/modules/channel", () => ({
  channelApi: {
    listChannels: mockListChannels,
  },
}));

vi.mock("../api/modules/chat", () => ({
  chatApi: {
    listChats: mockListChats,
  },
}));

vi.mock("../api/modules/reminder", () => ({
  reminderApi: {
    getTodayBrief: vi.fn(() => new Promise(() => {})),
  },
}));

vi.mock("../api/modules/dailyBrief", () => ({
  dailyBriefApi: {
    getHomeSnapshot: mockGetDailyBriefHomeSnapshot,
  },
}));

vi.mock("../api/modules/runtime", () => ({
  runtimeApi: {
    getTaskBoard: mockGetTaskBoard,
  },
}));

vi.mock("../api/modules/customization", () => ({
  CUSTOMIZATION_CHANGED_EVENT: "lantai:customization-changed",
  customizationApi: {
    getEffectiveState: mockGetEffectiveState,
  },
}));

vi.mock("../pages/Chat/components/ChatSearchPanel", () => ({
  default: () => null,
}));

describe("Sidebar chat navigation", () => {
  beforeEach(() => {
    window.localStorage.clear();
    mockNavigate.mockReset();
    mockGetLastChatId.mockReset();
    mockAgentState.selectedAgent = "default";
    mockGetTaskBoard.mockReset();
    mockGetTaskBoard.mockImplementation(() => new Promise(() => {}));
    mockGetEffectiveState.mockReset();
    mockGetEffectiveState.mockImplementation(() => new Promise(() => {}));
    mockGetDailyBriefHomeSnapshot.mockReset();
    mockGetDailyBriefHomeSnapshot.mockImplementation(
      () => new Promise(() => {}),
    );
    mockListChannels.mockReset();
    mockListChats.mockReset();
    mockListChannels.mockImplementation(() => new Promise(() => {}));
    mockListChats.mockImplementation(() => new Promise(() => {}));
  });

  it("does not expose the retired presentation studio entry", () => {
    render(<Sidebar selectedKey="chat" />);
    expect(screen.queryByText("演示文稿")).not.toBeInTheDocument();
  });

  it("returns to the selected agent's last conversation", () => {
    mockGetLastChatId.mockReturnValue("chat-retained");

    render(<Sidebar selectedKey="settings" />);

    fireEvent.click(screen.getByRole("button", { name: "聊天" }));

    expect(mockNavigate).toHaveBeenCalledWith("/chat/chat-retained");
  });

  it("does not expose the developer command runner as primary navigation", () => {
    render(<Sidebar selectedKey="settings" />);

    expect(
      screen.queryByRole("button", {
        name: /\u7ec8\u7aef AI|\u5de5\u4f5c\u533a\u547d\u4ee4/,
      }),
    ).not.toBeInTheDocument();
  });

  it("keeps Works as a first-class primary destination", () => {
    render(<Sidebar selectedKey="settings" />);

    fireEvent.click(screen.getByRole("button", { name: "作品" }));

    expect(mockNavigate).toHaveBeenCalledWith("/works");
  });

  it("opens the daily brief workbench without loading brief content", () => {
    render(<Sidebar selectedKey="settings" />);

    fireEvent.click(screen.getByRole("button", { name: "简报" }));

    expect(mockNavigate).toHaveBeenCalledWith("/daily-brief");
    expect(mockGetDailyBriefHomeSnapshot).not.toHaveBeenCalled();
  });

  it("marks the daily brief sidebar entry as the current page", () => {
    render(<Sidebar selectedKey="daily-brief" />);

    expect(screen.getByRole("button", { name: "简报" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("button", { name: "聊天" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("keeps channel navigation and channel expansion as separate actions", async () => {
    mockListChannels.mockResolvedValue({
      wechat: { enabled: true },
    });
    mockListChats.mockResolvedValue([
      {
        id: "wechat-chat",
        channel: "wechat",
        created_at: "2026-09-07T00:00:00Z",
        updated_at: "2026-09-07T01:00:00Z",
      },
    ]);

    render(<Sidebar selectedKey="chat" />);

    fireEvent.click(screen.getByRole("button", { name: "渠道" }));
    expect(mockNavigate).toHaveBeenCalledWith("/channels");

    mockNavigate.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "展开渠道最近会话" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /微信/ })).toBeVisible(),
    );
    expect(mockNavigate).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "管理渠道" })).toBeVisible();
  });

  it("shows the established default navigation directly without a More disclosure", () => {
    render(<Sidebar selectedKey="chat" />);

    const defaultItems = [
      "聊天",
      "作品",
      "简报",
      "渠道",
      "今日安排",
      "定时任务",
      "知识库",
      "记忆",
      "智能体",
      "技能",
      "工具",
      "设置",
    ];
    defaultItems.forEach((name) => {
      expect(screen.getByRole("button", { name })).toBeVisible();
    });

    expect(screen.queryByRole("button", { name: "更多" })).toBeNull();
    expect(screen.queryByRole("button", { name: "任务" })).toBeNull();
    expect(screen.queryByRole("button", { name: "浏览器" })).toBeNull();
    expect(screen.queryByRole("button", { name: "能力中心" })).toBeNull();
    ["控制", "工作区", "能力", "系统"].forEach((title) => {
      expect(screen.getByText(title)).toBeVisible();
    });
  });

  it("does not expose tasks when legacy factory customization mentions them", async () => {
    mockGetEffectiveState.mockResolvedValue({
      values: { "navigation.sidebar.tasks.visible": false },
      sources: {
        "navigation.sidebar.tasks.visible": { layer: "factory" },
      },
      ignored: {},
      warnings: [],
    });

    render(<Sidebar selectedKey="chat" />);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "任务" })).toBeNull();
    });
  });

  it("keeps a profile visibility overlay after manual sidebar preferences change", async () => {
    mockGetEffectiveState.mockResolvedValue({
      values: { "navigation.sidebar.tasks.visible": false },
      sources: {
        "navigation.sidebar.tasks.visible": { layer: "profile" },
      },
      ignored: {},
      warnings: [],
    });

    render(<Sidebar selectedKey="chat" />);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "任务" })).toBeNull();
    });

    window.dispatchEvent(new Event("lantai:sidebar-preferences-changed"));

    expect(screen.queryByRole("button", { name: "任务" })).toBeNull();
  });

  it("ignores an older customization response that resolves after the latest one", async () => {
    type EffectiveState = {
      values: Record<string, unknown>;
      sources: Record<string, { layer: string }>;
      ignored: Record<string, string>;
      warnings: string[];
    };
    let resolveOlder: (value: EffectiveState) => void = () => {};
    let resolveLatest: (value: EffectiveState) => void = () => {};
    mockGetEffectiveState
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOlder = resolve;
        }),
      )
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveLatest = resolve;
        }),
      );

    render(<Sidebar selectedKey="chat" />);
    window.dispatchEvent(new Event("lantai:customization-changed"));

    await act(async () => {
      resolveLatest({
        values: { "navigation.sidebar.tasks.visible": false },
        sources: {
          "navigation.sidebar.tasks.visible": { layer: "profile" },
        },
        ignored: {},
        warnings: [],
      });
      await Promise.resolve();
    });
    expect(screen.queryByRole("button", { name: "任务" })).toBeNull();

    await act(async () => {
      resolveOlder({
        values: { "navigation.sidebar.tasks.visible": false },
        sources: {
          "navigation.sidebar.tasks.visible": { layer: "factory" },
        },
        ignored: {},
        warnings: [],
      });
      await Promise.resolve();
    });
    expect(screen.queryByRole("button", { name: "任务" })).toBeNull();
  });

  it("does not surface internal tasks when they need attention", async () => {
    window.localStorage.setItem(
      SIDEBAR_VISIBILITY_STORAGE_KEY,
      JSON.stringify(["chat", "works", "settings"]),
    );
    mockGetTaskBoard.mockResolvedValue({
      counts: { needs_user: 1, needs_action: 1 },
    });

    render(<Sidebar selectedKey="chat" />);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /任务/ })).toBeNull();
    });
    expect(
      JSON.parse(
        window.localStorage.getItem("lantai:sidebar-visible-nav:v6") || "[]",
      ),
    ).not.toContain("task-center");
  });

  it("does not poll the internal task board for public navigation", async () => {
    window.localStorage.setItem(
      SIDEBAR_VISIBILITY_STORAGE_KEY,
      JSON.stringify(["chat", "works", "settings"]),
    );
    render(<Sidebar selectedKey="chat" />);

    await waitFor(() => expect(mockGetEffectiveState).toHaveBeenCalled());
    expect(mockGetTaskBoard).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /任务/ })).toBeNull();
  });
});

it("keeps the selected row visible when navigation height shrinks", () => {
  let resize: (() => void) | undefined;
  const disconnect = vi.fn();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        resize = callback;
      }
      observe() {}
      disconnect = disconnect;
    },
  );
  try {
    const { unmount } = render(<Sidebar selectedKey="settings" />);
    const nav = screen.getByRole("navigation", { name: "澜台导航" });
    const selected = nav.querySelector<HTMLElement>('[aria-current="page"]')!;
    vi.spyOn(nav, "getBoundingClientRect").mockReturnValue({
      top: 100,
      bottom: 600,
      height: 500,
    } as DOMRect);
    vi.spyOn(selected, "getBoundingClientRect").mockReturnValue({
      top: 580,
      bottom: 620,
    } as DOMRect);
    act(() => resize?.());
    expect(nav.scrollTop).toBe(28);
    unmount();
    expect(disconnect).toHaveBeenCalled();
  } finally {
    vi.unstubAllGlobals();
  }
});
