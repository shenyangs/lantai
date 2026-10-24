import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../test/common_setup";
import type { WorkMethod } from "../../../api/types/workMethod";
import CapabilityMenuButton from "./CapabilityMenuButton";

const {
  getKdocsConnectionStatus,
  getWpsNoteConnectionStatus,
  listMCPClients,
  listOfficialMCPConnectors,
  toggleMCPClient,
  listSkillIndex,
} = vi.hoisted(() => ({
  getKdocsConnectionStatus: vi.fn(),
  getWpsNoteConnectionStatus: vi.fn(),
  listMCPClients: vi.fn(),
  listOfficialMCPConnectors: vi.fn(),
  toggleMCPClient: vi.fn(),
  listSkillIndex: vi.fn(),
}));

vi.mock("../../../api/modules/mcp", () => ({
  mcpApi: {
    listMCPClients,
    listOfficialMCPConnectors,
    toggleMCPClient,
  },
}));

vi.mock("../../../api/modules/skill", () => ({
  skillApi: {
    getKdocsConnectionStatus,
    getWpsNoteConnectionStatus,
    listSkillIndex,
  },
}));

const methods: WorkMethod[] = [
  {
    id: "standard",
    name: "标准",
    summary: "自然回答，只在需要时使用工具。",
    instructions: [],
    scope: [],
    acceptance: [],
    max_no_progress_iterations: 3,
    built_in: true,
  },
  {
    id: "deep_research",
    name: "深度研究",
    summary: "先收集证据，再形成可核验结论。",
    instructions: [],
    scope: [],
    acceptance: [],
    max_no_progress_iterations: 3,
    built_in: true,
  },
  {
    id: "sustained_execution",
    name: "持续执行",
    summary: "建立目标并持续推进到完成。",
    instructions: [],
    scope: [],
    acceptance: [],
    max_no_progress_iterations: 3,
    built_in: true,
  },
];

const connector = {
  key: "mail",
  name: "QQ 邮箱",
  description: "读取和发送邮件",
  enabled: true,
  transport: "streamable_http" as const,
  url: "https://mcp.example/mail",
  headers: {},
  command: "",
  args: [],
  env: {},
  cwd: "",
  oauth: {
    enabled: false,
    authorization_url: "",
    token_url: "",
    client_id: "",
    client_secret: "",
    scopes: [],
    redirect_uri: "",
    access_token: "",
    refresh_token: "",
    expires_at: null,
    token_type: "Bearer",
    extra_authorize_params: {},
    extra_token_params: {},
  },
  allowed_tools: [],
};

function renderMenuProps(overrides: Record<string, unknown> = {}) {
  return {
    selectedAgent: "agent-1",
    workMethods: methods,
    workMethodId: "standard",
    onWorkMethodChange: vi.fn(),
    skillsEnabled: true,
    selectedSkillName: null,
    onPickSkill: vi.fn(),
    onClearSkill: vi.fn(),
    onManageSkills: vi.fn(),
    planEnabled: false,
    planDraftMode: false,
    onEnsurePlanEnabled: vi.fn().mockResolvedValue(undefined),
    onPlanDraftModeChange: vi.fn(),
    goalDraftMode: false,
    onGoalDraftModeChange: vi.fn(),
    onManageConnectors: vi.fn(),
    ...overrides,
  };
}

function renderMenu(overrides: Record<string, unknown> = {}) {
  const props = renderMenuProps(overrides);
  renderWithProviders(<CapabilityMenuButton {...props} />);
  return props;
}

describe("CapabilityMenuButton", () => {
  beforeEach(() => {
    listMCPClients.mockReset();
    listOfficialMCPConnectors.mockReset();
    toggleMCPClient.mockReset();
    listSkillIndex.mockReset();
    getKdocsConnectionStatus.mockReset();
    getWpsNoteConnectionStatus.mockReset();
    listMCPClients.mockResolvedValue([connector]);
    listOfficialMCPConnectors.mockResolvedValue([
      {
        key: "notion",
        name: "Notion",
        description: "搜索、读取并更新 Notion 工作区内容。",
        url: "https://mcp.notion.com/mcp",
        docs_url:
          "https://developers.notion.com/guides/mcp/get-started-with-mcp",
        official: true,
        configured: false,
      },
      {
        key: "linear",
        name: "Linear",
        description: "读取和管理 Linear 项目。",
        url: "https://mcp.linear.app/mcp",
        docs_url: "https://linear.app/docs/mcp",
        official: true,
        configured: false,
      },
      {
        key: "figma",
        name: "Figma",
        description: "读取 Figma 设计上下文。",
        url: "https://mcp.figma.com/mcp",
        docs_url: "https://help.figma.com/figma-mcp",
        official: true,
        configured: false,
      },
    ]);
    getKdocsConnectionStatus.mockResolvedValue({
      installed: true,
      authenticated: true,
      cli_version: "2.6.7",
      token_url: "",
    });
    getWpsNoteConnectionStatus.mockResolvedValue({
      installed: true,
      connected: false,
      cli_version: "",
      tool_count: 0,
      install_method: "wps_note_desktop",
      repository_url: "https://github.com/wpsnote/wpsnote-skills",
    });
    toggleMCPClient.mockResolvedValue({ ...connector, enabled: false });
    listSkillIndex.mockResolvedValue([
      {
        name: "文案润色",
        description: "优化中文表达",
        enabled: true,
        source: "builtin",
        channels: ["console"],
        tags: [],
      },
    ]);
  });

  it("keeps the default capability trigger icon-only and reveals real active choices", () => {
    const { rerender } = renderWithProviders(
      <CapabilityMenuButton {...renderMenuProps()} />,
    );
    const defaultTrigger = screen.getByRole("button", { name: "能力菜单" });
    expect(defaultTrigger.textContent).toBe("");
    expect(defaultTrigger).toHaveAttribute("data-chat-capability-trigger", "1");
    expect(defaultTrigger).toHaveAttribute("data-active-choice", "false");

    rerender(
      <CapabilityMenuButton
        {...renderMenuProps({ workMethodId: "deep_research" })}
      />,
    );
    const activeTrigger = screen.getByRole("button", { name: "能力菜单" });
    expect(activeTrigger).toHaveTextContent("深度研究");
    expect(activeTrigger).toHaveAttribute("data-active-choice", "true");
  });

  it("puts work methods, skills, connectors, plan mode, and goal mode in one menu", async () => {
    const user = userEvent.setup();
    renderMenu();

    await user.click(screen.getByRole("button", { name: "能力菜单" }));

    expect(
      screen.getByRole("menuitem", { name: /工作法：标准/ }),
    ).toBeVisible();
    expect(screen.getByRole("menuitem", { name: /技能/ })).toBeVisible();
    expect(screen.getByRole("menuitem", { name: /连接器/ })).toBeVisible();
    expect(
      screen.getByRole("menuitemcheckbox", { name: /计划模式/ }),
    ).toBeVisible();
    expect(
      screen.getByRole("menuitemcheckbox", { name: /目标模式/ }),
    ).toBeVisible();
  });

  it("uses the same compact icon scale as the composer controls", async () => {
    const user = userEvent.setup();
    renderMenu();

    await user.click(screen.getByRole("button", { name: "能力菜单" }));

    const methodItem = screen.getByRole("menuitem", { name: /工作法：标准/ });
    const icons = methodItem.querySelectorAll("svg");
    expect(icons[0]).toHaveAttribute("width", "17");
    expect(icons[1]).toHaveAttribute("width", "15");
  });

  it("renders the popover outside the clipped composer hierarchy", async () => {
    const user = userEvent.setup();
    renderMenu();

    await user.click(screen.getByRole("button", { name: "能力菜单" }));

    expect(screen.getByTestId("capability-menu-popover").parentElement).toBe(
      document.body,
    );
  });

  it("selects a real work method from the secondary panel", async () => {
    const user = userEvent.setup();
    const props = renderMenu();

    await user.click(screen.getByRole("button", { name: "能力菜单" }));
    await user.click(screen.getByRole("menuitem", { name: /工作法：标准/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /深度研究/ }));

    expect(props.onWorkMethodChange).toHaveBeenCalledWith("deep_research");
  });

  it("loads and selects an enabled chat skill", async () => {
    const user = userEvent.setup();
    const props = renderMenu();

    await user.click(screen.getByRole("button", { name: "能力菜单" }));
    await user.click(screen.getByRole("menuitem", { name: /^技能/ }));
    await user.click(
      await screen.findByRole("menuitemradio", { name: /文案润色/ }),
    );

    expect(listSkillIndex).toHaveBeenCalledWith("agent-1");
    expect(props.onPickSkill).toHaveBeenCalledWith("文案润色");
  });

  it("enables plan mode on demand", async () => {
    const user = userEvent.setup();
    const props = renderMenu();

    await user.click(screen.getByRole("button", { name: "能力菜单" }));
    await user.click(
      screen.getByRole("menuitemcheckbox", { name: /计划模式/ }),
    );

    expect(props.onEnsurePlanEnabled).toHaveBeenCalledTimes(1);
    expect(props.onPlanDraftModeChange).toHaveBeenCalledWith(true);
    expect(props.onGoalDraftModeChange).toHaveBeenCalledWith(false);
  });

  it("enables goal mode as a planned sustained-execution turn", async () => {
    const user = userEvent.setup();
    const props = renderMenu();

    await user.click(screen.getByRole("button", { name: "能力菜单" }));
    await user.click(
      screen.getByRole("menuitemcheckbox", { name: /目标模式/ }),
    );

    expect(props.onEnsurePlanEnabled).toHaveBeenCalledTimes(1);
    expect(props.onWorkMethodChange).toHaveBeenCalledWith(
      "sustained_execution",
    );
    expect(props.onPlanDraftModeChange).toHaveBeenCalledWith(false);
    expect(props.onGoalDraftModeChange).toHaveBeenCalledWith(true);
  });

  it("lists and toggles configured connectors", async () => {
    const user = userEvent.setup();
    renderMenu({ planEnabled: true });

    await user.click(screen.getByRole("button", { name: "能力菜单" }));
    await user.click(screen.getByRole("menuitem", { name: /连接器/ }));
    const connectorSwitch = await screen.findByRole("switch", {
      name: "QQ 邮箱",
    });
    await user.click(connectorSwitch);

    expect(toggleMCPClient).toHaveBeenCalledWith("mail");
    await waitFor(() =>
      expect(
        screen.queryByRole("switch", { name: "QQ 邮箱" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("shows official connectors and hides disabled custom MCP clients", async () => {
    const user = userEvent.setup();
    listMCPClients.mockResolvedValue([
      {
        ...connector,
        key: "tavily_mcp",
        name: "tavily_mcp",
        enabled: false,
      },
    ]);
    renderMenu();

    await user.click(screen.getByRole("button", { name: "能力菜单" }));
    await user.click(screen.getByRole("menuitem", { name: /连接器/ }));

    expect(await screen.findByText("金山文档")).toBeInTheDocument();
    expect(screen.getByText("WPS 笔记")).toBeInTheDocument();
    expect(screen.getByText("Notion")).toBeInTheDocument();
    expect(screen.getByText("Linear")).toBeInTheDocument();
    expect(screen.getByText("Figma")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "金山文档" }).tagName).toBe("svg");
    expect(screen.getByRole("img", { name: "WPS 笔记" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Notion" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Linear" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Figma" })).toBeInTheDocument();
    expect(screen.queryByText("tavily_mcp")).not.toBeInTheDocument();
    expect(listOfficialMCPConnectors).toHaveBeenCalledTimes(1);
    expect(getKdocsConnectionStatus).toHaveBeenCalledTimes(1);
    expect(getWpsNoteConnectionStatus).toHaveBeenCalledTimes(1);
  });

  it("always shows Kingsoft Docs even when its live status is unavailable", async () => {
    const user = userEvent.setup();
    getKdocsConnectionStatus.mockRejectedValue(new Error("status unavailable"));
    renderMenu();

    await user.click(screen.getByRole("button", { name: "能力菜单" }));
    await user.click(screen.getByRole("menuitem", { name: /连接器/ }));

    expect(await screen.findByText("金山文档")).toBeInTheDocument();
  });
});
