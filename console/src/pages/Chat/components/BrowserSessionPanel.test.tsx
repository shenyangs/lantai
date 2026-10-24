import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  BrowserTaskRun,
  BrowserWorkbenchConfig,
} from "../../../api/types";
import BrowserSessionPanel from "./BrowserSessionPanel";

const { apiMocks, appMessage } = vi.hoisted(() => ({
  apiMocks: {
    getTask: vi.fn(),
    listTaskEvents: vi.fn(),
    getTaskArtifacts: vi.fn(),
    getConfig: vi.fn(),
    getBrowserSkillStatus: vi.fn(),
    pauseTask: vi.fn(),
    requestTaskTakeover: vi.fn(),
    resumeTask: vi.fn(),
    cancelTask: vi.fn(),
    confirmTask: vi.fn(),
    subscribeTaskEvents: vi.fn(() => vi.fn()),
  },
  appMessage: { error: vi.fn(), warning: vi.fn(), success: vi.fn() },
}));

vi.mock("../../../api/modules/browserSession", () => ({
  browserWorkbenchApi: apiMocks,
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: appMessage }),
}));

const task: BrowserTaskRun = {
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

const electronConfig: BrowserWorkbenchConfig = {
  agent_control_enabled: true,
  default_profile_id: "default",
  require_confirmation_for_write: true,
  enable_scheduled_browser_tasks: false,
  primary_runtime: "electron_bridge",
  optional_engines_enabled: true,
};

function desktopState(owner: "agent" | "user" = "agent") {
  return {
    ok: true,
    active_tab_id: "tab-4",
    profile_id: "default",
    tabs: [
      {
        id: "tab-4",
        profile_id: "default",
        title: "豆瓣电影",
        url: "https://www.douban.com/",
        loading: false,
        can_go_back: true,
        can_go_forward: false,
        task_group_id: task.run_id,
      },
    ],
    surface: {
      mode: owner === "user" ? "sidecar_interactive" : "sidecar_preview",
      visible: true,
      control_owner: owner,
      run_id: task.run_id,
      bounds: { x: 700, y: 128, width: 680, height: 620 },
    },
  } as BrowserWorkbenchState;
}

const target = {
  runId: task.run_id,
  profileId: "default",
  url: "https://www.douban.com/",
  instruction: task.instruction,
  task,
};

describe("BrowserSessionPanel", () => {
  let desktop: {
    mountSidecar: ReturnType<typeof vi.fn>;
    layoutSidecar: ReturnType<typeof vi.fn>;
    setSidecarControl: ReturnType<typeof vi.fn>;
    captureSidecarPreview: ReturnType<typeof vi.fn>;
    unmountSidecar: ReturnType<typeof vi.fn>;
    popoutSidecar: ReturnType<typeof vi.fn>;
    focusExternalBrowser: ReturnType<typeof vi.fn>;
    action: ReturnType<typeof vi.fn>;
    onState: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1440,
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 700,
      y: 128,
      top: 128,
      right: 1380,
      bottom: 748,
      left: 700,
      width: 680,
      height: 620,
      toJSON: () => ({}),
    });
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
        unobserve() {}
      },
    );
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn((query: string) => ({
        matches: query.includes("1100") && window.innerWidth <= 1100,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });

    apiMocks.getTask.mockResolvedValue(task);
    apiMocks.listTaskEvents.mockResolvedValue([
      {
        id: "event-1",
        ts: "2026-08-31T08:00:01Z",
        run_id: task.run_id,
        type: "action_completed",
        status: "running_background",
        message: "snapshot completed",
        action: "snapshot",
        observation_id: "observation-1",
        metadata: {
          observation: { runtime: "electron_bridge" },
        },
      },
    ]);
    apiMocks.getTaskArtifacts.mockResolvedValue({
      run_id: task.run_id,
      replay_run_id: task.run_id,
      event_count: 1,
      task_events: [],
      replay_events: [],
      screenshots: ["/tmp/browser-shot.png"],
      observation_summaries: [{ title: "豆瓣电影" }],
      network: [],
      console: [],
    });
    apiMocks.getConfig.mockResolvedValue(electronConfig);
    apiMocks.getBrowserSkillStatus.mockResolvedValue({
      ok: true,
      installed: true,
      connected: true,
      ready: true,
      status: "available",
      runtime: "browser_skill",
      message: "connected",
      browsers: [{ instance_id: "chrome", browser_name: "Google Chrome" }],
      sessions: [],
      repository_url: "https://github.com/Tencent/BrowserSkill",
      upstream_commit: "test",
    });
    apiMocks.pauseTask.mockResolvedValue({ ...task, status: "paused" });
    apiMocks.requestTaskTakeover.mockResolvedValue({
      ready: true,
      run: { ...task, status: "paused" },
      error_code: "",
      message: "ready",
    });
    apiMocks.resumeTask.mockResolvedValue(task);
    apiMocks.cancelTask.mockResolvedValue({ ...task, status: "cancelled" });
    apiMocks.subscribeTaskEvents.mockReturnValue(vi.fn());

    desktop = {
      mountSidecar: vi.fn(async () => desktopState()),
      layoutSidecar: vi.fn(async () => desktopState()),
      setSidecarControl: vi.fn(async (owner) => desktopState(owner)),
      captureSidecarPreview: vi.fn(async () => ({
        ok: true,
        data_url: "data:image/png;base64,cG5n",
        page_id: "tab-4",
        url: "https://www.douban.com/",
      })),
      unmountSidecar: vi.fn(async () => desktopState()),
      popoutSidecar: vi.fn(async () => desktopState()),
      focusExternalBrowser: vi.fn(async () => ({ ok: true })),
      action: vi.fn(async () => desktopState()),
      onState: vi.fn(() => vi.fn()),
    };
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: { isDesktop: true, browserWorkbench: desktop },
    });
  });

  it("shows the live page beside chat and hands control over only after pausing", async () => {
    const user = userEvent.setup();
    const { unmount } = render(
      <BrowserSessionPanel target={target} onClose={vi.fn()} />,
    );

    expect(
      screen.getByRole("complementary", { name: "浏览器会话" }),
    ).toBeVisible();
    const separator = screen.getByRole("separator", {
      name: "调整聊天与浏览器宽度",
    });
    expect(separator).toBeVisible();
    const initialWidth = Number(separator.getAttribute("aria-valuenow"));
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(Number(separator.getAttribute("aria-valuenow"))).toBeGreaterThan(
      initialWidth,
    );
    await waitFor(() => expect(desktop.mountSidecar).toHaveBeenCalledOnce());
    await waitFor(() =>
      expect(screen.getByAltText("www.douban.com 当前页面预览")).toBeVisible(),
    );
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新网页" })).toBeDisabled();
    expect(screen.getByRole("tab", { name: "豆瓣电影" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "我来操作" }));
    expect(apiMocks.requestTaskTakeover).toHaveBeenCalledWith(task.run_id);
    expect(desktop.setSidecarControl).toHaveBeenCalledWith("user");
    expect(await screen.findByText("你正在操作，澜台已暂停")).toBeVisible();
    expect(screen.getByRole("button", { name: "刷新网页" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "交还给澜台" }));
    expect(desktop.setSidecarControl).toHaveBeenLastCalledWith("agent");
    expect(apiMocks.resumeTask).toHaveBeenCalledWith(task.run_id);

    unmount();
    expect(desktop.unmountSidecar).toHaveBeenCalled();
  });

  it("shows concise keyframes and expands evidence without hiding task controls", async () => {
    const user = userEvent.setup();
    const completedEvent = {
      id: "event-keyframe",
      ts: "2026-08-31T08:00:02Z",
      run_id: task.run_id,
      type: "agent_action_completed",
      status: "running_background",
      message: "snapshot completed",
      action: "snapshot",
      observation_id: "observation-keyframe",
      metadata: {
        screenshot_path: "/tmp/browser-step.png",
        observation: {
          runtime: "electron_bridge",
          url: "https://movie.douban.com/chart",
          screenshot_path: "/tmp/browser-step.png",
        },
      },
    };
    apiMocks.listTaskEvents.mockResolvedValue([
      completedEvent,
      { ...completedEvent, id: "event-keyframe-duplicate" },
      {
        id: "event-confirmation",
        ts: "2026-08-31T08:00:03Z",
        run_id: task.run_id,
        type: "agent_waiting_for_confirmation",
        status: "waiting_for_user_confirmation",
        message: "confirmation required",
        action: "click",
        observation_id: "",
        metadata: {},
      },
    ]);

    render(<BrowserSessionPanel target={target} onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: /查看过程/ }));

    const timeline = screen.getByRole("list", { name: "浏览器关键步骤" });
    expect(within(timeline).getAllByText("读取页面完成")).toHaveLength(1);
    expect(within(timeline).getByText("页面：movie.douban.com")).toBeVisible();
    expect(within(timeline).getByText("需要你确认下一步")).toBeVisible();

    const keyframe = within(timeline).getByRole("button", {
      name: "查看读取页面完成的页面画面",
    });
    expect(keyframe.querySelector("img")).toHaveAttribute(
      "src",
      expect.stringContaining("/api/files/preview/tmp/browser-step.png"),
    );
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();

    await user.click(keyframe);
    expect(keyframe).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByAltText("movie.douban.com 在读取页面完成时的页面画面"),
    ).toBeVisible();
    expect(
      screen.getByRole("region", { name: /实时预览：www\.douban\.com/ }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "收起页面画面" }));
    expect(keyframe).toHaveAttribute("aria-pressed", "false");
  });

  it("limits a long process history to the latest 20 key steps", async () => {
    const user = userEvent.setup();
    apiMocks.listTaskEvents.mockResolvedValue(
      Array.from({ length: 25 }, (_, index) => ({
        id: `event-${index + 1}`,
        ts: `2026-08-31T08:00:${String(index + 1).padStart(2, "0")}Z`,
        run_id: task.run_id,
        type: "action_completed",
        status: "running_background",
        message: "action completed",
        action: index % 2 ? "navigate" : "snapshot",
        observation_id: `observation-${index + 1}`,
        metadata: {},
      })),
    );

    render(<BrowserSessionPanel target={target} onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: /查看过程/ }));

    expect(
      within(screen.getByRole("list", { name: "浏览器关键步骤" })).getAllByRole(
        "listitem",
      ),
    ).toHaveLength(20);
  });

  it("keeps confirmation and Stop available at the same time", async () => {
    const confirmationTask: BrowserTaskRun = {
      ...task,
      status: "waiting_for_user_confirmation",
      current_action: "click",
      pending_confirmation: {
        action: "click",
        profile_id: "default",
        url: "https://www.douban.com/",
        selector: "a[data-testid='details']",
        access: "write",
        reason: "打开电影详情",
      },
    };
    apiMocks.getTask.mockResolvedValue(confirmationTask);

    render(
      <BrowserSessionPanel
        target={{ ...target, task: confirmationTask }}
        onClose={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("region", { name: "浏览器操作确认" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "允许一次" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "拒绝" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  });

  it.each([
    ["paused", false],
    ["manual_takeover_required", false],
    ["running_background", true],
  ] as const)("keeps Stop available for %s state", async (status, takeOver) => {
    const user = userEvent.setup();
    const stateTask: BrowserTaskRun = { ...task, status };
    apiMocks.getTask.mockResolvedValue(stateTask);

    render(
      <BrowserSessionPanel
        target={{ ...target, task: stateTask }}
        onClose={vi.fn()}
      />,
    );
    if (takeOver) {
      await user.click(await screen.findByRole("button", { name: "我来操作" }));
      expect(await screen.findByText("你正在操作，澜台已暂停")).toBeVisible();
    }

    expect(await screen.findByRole("button", { name: "停止" })).toBeEnabled();
  });

  it("uses a screenshot preview and focuses Chrome for BrowserSkill takeover", async () => {
    const user = userEvent.setup();
    apiMocks.getConfig.mockResolvedValue({
      ...electronConfig,
      primary_runtime: "browser_skill",
    });
    apiMocks.listTaskEvents.mockResolvedValue([
      {
        id: "event-browser-skill",
        ts: "2026-08-31T08:00:01Z",
        run_id: task.run_id,
        type: "action_completed",
        status: "running_background",
        message: "observed",
        action: "snapshot",
        observation_id: "observation-browser-skill",
        metadata: { observation: { runtime: "browser_skill" } },
      },
    ]);

    render(<BrowserSessionPanel target={target} onClose={vi.fn()} />);

    expect(
      await screen.findByRole("button", { name: "切换到 Google Chrome" }),
    ).toBeVisible();
    expect(desktop.mountSidecar).not.toHaveBeenCalled();
    expect(screen.getByAltText("www.douban.com 当前页面预览")).toHaveAttribute(
      "src",
      expect.stringContaining("/api/files/preview/tmp/browser-shot.png"),
    );
    expect(screen.getByText("最新截图")).toBeVisible();
    expect(
      screen.getByRole("region", { name: "最新截图：www.douban.com" }),
    ).toBeVisible();

    await user.click(
      screen.getByRole("button", { name: "切换到 Google Chrome" }),
    );
    expect(apiMocks.requestTaskTakeover).toHaveBeenCalledWith(task.run_id);
    expect(desktop.focusExternalBrowser).toHaveBeenCalledWith("Google Chrome");
    expect(await screen.findByText("你正在操作，澜台已暂停")).toBeVisible();
    expect(
      screen.getByText(
        /请在 Google Chrome 的 BrowserSkill Agent Window 中操作/,
      ),
    ).toBeVisible();
  });

  it("closing during takeover returns control and hides the sidecar without stopping", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<BrowserSessionPanel target={target} onClose={onClose} />);
    await waitFor(() => expect(desktop.mountSidecar).toHaveBeenCalledOnce());
    await user.click(screen.getByRole("button", { name: "我来操作" }));

    await user.click(screen.getByRole("button", { name: "收起浏览器" }));

    expect(desktop.setSidecarControl).toHaveBeenLastCalledWith("agent");
    expect(apiMocks.resumeTask).toHaveBeenCalledWith(task.run_id);
    expect(apiMocks.cancelTask).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does not expose the live page while an Agent action is still settling", async () => {
    const user = userEvent.setup();
    apiMocks.requestTaskTakeover.mockResolvedValue({
      ready: false,
      run: { ...task, status: "paused" },
      error_code: "browser_takeover_action_still_running",
      message: "当前动作仍在结束中",
    });
    render(<BrowserSessionPanel target={target} onClose={vi.fn()} />);
    await waitFor(() => expect(desktop.mountSidecar).toHaveBeenCalledOnce());

    await user.click(screen.getByRole("button", { name: "我来操作" }));

    expect(desktop.setSidecarControl).not.toHaveBeenCalledWith("user");
    expect(appMessage.error).toHaveBeenCalledWith("当前动作仍在结束中");
  });

  it("switches user-controlled tabs with standard arrow-key navigation", async () => {
    const user = userEvent.setup();
    const tabs = [
      ...desktopState().tabs,
      {
        id: "tab-5",
        profile_id: "default",
        title: "电影榜单",
        url: "https://movie.douban.com/chart",
        loading: false,
        can_go_back: false,
        can_go_forward: false,
        task_group_id: task.run_id,
      },
    ];
    desktop.mountSidecar.mockResolvedValue({ ...desktopState(), tabs });
    desktop.setSidecarControl.mockImplementation(async (owner) => ({
      ...desktopState(owner),
      tabs,
    }));
    desktop.action.mockImplementation(async () => ({
      ...desktopState("user"),
      active_tab_id: "tab-5",
      tabs,
    }));

    render(<BrowserSessionPanel target={target} onClose={vi.fn()} />);
    await waitFor(() => expect(desktop.mountSidecar).toHaveBeenCalledOnce());
    await user.click(screen.getByRole("button", { name: "我来操作" }));

    const firstTab = screen.getByRole("tab", { name: "豆瓣电影" });
    firstTab.focus();
    fireEvent.keyDown(firstTab, { key: "ArrowRight" });

    expect(desktop.action).toHaveBeenCalledWith({
      action: "select_tab",
      page_id: "tab-5",
      profile_id: "default",
    });
    expect(screen.getByRole("tab", { name: "电影榜单" })).toHaveFocus();
  });

  it("treats a narrow sidecar as a focus-managed overlay", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 900,
    });
    const previous = document.createElement("button");
    previous.textContent = "打开浏览器";
    const chatStage = document.createElement("main");
    chatStage.dataset.testid = "chat-stage";
    document.body.append(previous, chatStage);
    previous.focus();
    const onClose = vi.fn();
    const rendered = render(
      <BrowserSessionPanel target={target} onClose={onClose} />,
    );

    const dialog = await screen.findByRole("dialog", { name: "浏览器会话" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "收起浏览器" })).toHaveFocus(),
    );
    expect(chatStage.inert).toBe(true);
    expect(chatStage).toHaveAttribute("aria-hidden", "true");
    expect(
      screen.queryByRole("separator", { name: "调整聊天与浏览器宽度" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "放大浏览器区域" })).toBeNull();

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
    rendered.rerender(<BrowserSessionPanel target={null} onClose={onClose} />);
    await waitFor(() => expect(previous).toHaveFocus());
    expect(chatStage.inert).toBe(false);
    expect(chatStage).not.toHaveAttribute("aria-hidden");
    previous.remove();
    chatStage.remove();
  });
});
