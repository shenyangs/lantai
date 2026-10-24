import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/common_setup";
import ModelSelector, { formatContextPolicyNotice } from "./index";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock("@/api/modules/provider", () => ({
  providerApi: {
    listProviders: vi.fn(),
    getActiveModels: vi.fn(),
    setActiveLlm: vi.fn(),
    discoverModels: vi.fn(),
  },
}));

vi.mock("@/api/modules/agents", () => ({
  agentsApi: {
    restoreWorkspace: vi.fn(),
    listAgents: vi.fn(),
  },
}));

vi.mock("@/stores/agentStore", () => {
  const useAgentStore = vi.fn(() => ({ selectedAgent: "default" }));
  useAgentStore.getState = vi.fn(() => ({ selectedAgent: "default" }));
  return { useAgentStore };
});

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

import { providerApi } from "@/api/modules/provider";
import { agentsApi } from "@/api/modules/agents";
import { useAgentStore } from "@/stores/agentStore";

const mockProvider = {
  id: "openai",
  name: "OpenAI",
  api_key: "sk-xxx",
  api_key_prefix: "",
  chat_model: "OpenAIChatModel",
  require_api_key: true,
  base_url: "",
  is_custom: false,
  is_local: false,
  support_model_discovery: false,
  support_connection_check: false,
  freeze_url: false,
  generate_kwargs: {},
  models: [
    {
      id: "gpt-4",
      name: "GPT-4",
      supports_multimodal: false,
      supports_image: false,
      supports_video: false,
      generate_kwargs: {},
    },
    {
      id: "gpt-3.5-turbo",
      name: "GPT-3.5 Turbo",
      supports_multimodal: false,
      supports_image: false,
      supports_video: false,
      generate_kwargs: {},
    },
  ],
  extra_models: [],
};

const mockActiveModels = {
  active_llm: { provider_id: "openai", model: "gpt-4" },
};

const mockLocalProvider = {
  ...mockProvider,
  id: "ollama",
  name: "Ollama",
  api_key: "",
  require_api_key: false,
  base_url: "http://127.0.0.1:11434",
  is_local: true,
  support_model_discovery: true,
  models: [],
};

function setupDefaultMocks() {
  vi.mocked(providerApi.listProviders).mockResolvedValue([mockProvider]);
  vi.mocked(providerApi.getActiveModels).mockResolvedValue(mockActiveModels);
  vi.mocked(providerApi.setActiveLlm).mockResolvedValue({});
  vi.mocked(providerApi.discoverModels).mockResolvedValue({
    success: false,
    message: "",
    models: [],
    added_count: 0,
  });
}

function clearTestStorage() {
  try {
    window.localStorage?.clear();
  } catch {
    // Some Vitest environments run without localStorage support.
  }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("ModelSelector", () => {
  beforeEach(() => {
    clearTestStorage();
    setupDefaultMocks();
    vi.mocked(useAgentStore).mockReturnValue({
      selectedAgent: "default",
      agents: [],
      setAgents: vi.fn(),
    } as ReturnType<typeof useAgentStore>);
    vi.mocked(useAgentStore.getState).mockReturnValue({
      selectedAgent: "default",
    } as ReturnType<typeof useAgentStore.getState>);
  });

  afterEach(() => {
    clearTestStorage();
    vi.clearAllMocks();
  });

  it("displays current active model name on trigger button after loading", async () => {
    renderWithProviders(<ModelSelector />);
    expect(await screen.findByText("GPT-4")).toBeInTheDocument();
  });

  it("exposes model selection as a keyboard-operable expanded control", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);

    const trigger = await screen.findByRole("button", {
      name: "选择模型，当前 GPT-4",
    });
    expect(trigger).toHaveAttribute("aria-haspopup", "dialog");
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await user.tab();
    expect(trigger).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("OpenAI")).toBeInTheDocument();
  });

  it("distinguishes model-service failure from an empty configuration and retries", async () => {
    vi.mocked(providerApi.listProviders).mockRejectedValue(
      new Error("service unavailable"),
    );
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);

    const trigger = await screen.findByRole("button", {
      name: /选择模型，当前/,
    });
    await user.click(trigger);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "模型列表加载失败，请检查模型服务后重试。",
    );
    expect(
      screen.queryByText("modelSelector.noConfiguredModels"),
    ).not.toBeInTheDocument();

    vi.mocked(providerApi.listProviders).mockResolvedValue([mockProvider]);
    await user.click(screen.getByRole("button", { name: "重新加载" }));

    expect(await screen.findByText("OpenAI")).toBeInTheDocument();
  });

  it("explains available input and unknown-window fallback after a model switch", () => {
    expect(
      formatContextPolicyNotice("Small", {
        mode: "smart",
        hard_window: 65_536,
        usable_input_tokens: 55_296,
        target_input_tokens: 44_236,
        output_reserve: 8_192,
        safety_margin: 2_048,
        used_unknown_fallback: false,
        status: "unverified",
        source: "provider:context_window",
        switch_action: "adapted",
      }),
    ).toContain("本轮可用输入 44.2K");
    expect(
      formatContextPolicyNotice("Custom", {
        mode: "smart",
        hard_window: 131_072,
        usable_input_tokens: 107_480,
        target_input_tokens: 85_984,
        output_reserve: 19_660,
        safety_margin: 3_932,
        used_unknown_fallback: true,
        status: "unverified",
        source: "provider",
        switch_action: "adapted",
      }),
    ).toContain("模型窗口待验证");
  });

  it("restores a protected workspace through Electron before model selection", async () => {
    const setAgents = vi.fn();
    vi.mocked(useAgentStore).mockReturnValue({
      selectedAgent: "default",
      agents: [
        {
          id: "default",
          name: "沈行知",
          description: "",
          workspace_dir: "/Users/test/Downloads/workspace",
          enabled: true,
          workspace_access_required: true,
        },
      ],
      setAgents,
    } as ReturnType<typeof useAgentStore>);
    const restoreWorkspace = vi.fn().mockResolvedValue({
      ok: true,
      stagedPath: "/managed/workspace-imports/default-test",
      sourcePath: "/Users/test/Downloads/workspace",
    });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: { restoreWorkspace },
    });
    vi.mocked(agentsApi.restoreWorkspace).mockResolvedValue({
      success: true,
      agent_id: "default",
      workspace_dir: "/managed/workspaces/default",
      source_preserved: true,
    });
    vi.mocked(agentsApi.listAgents).mockResolvedValue({ agents: [] });
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);

    await user.click(await screen.findByText("恢复工作区"));
    await user.click(await screen.findByText("选择并恢复"));

    await waitFor(() => {
      expect(restoreWorkspace).toHaveBeenCalledWith({
        agentId: "default",
        expectedPath: "/Users/test/Downloads/workspace",
      });
      expect(agentsApi.restoreWorkspace).toHaveBeenCalledWith("default", {
        expected_source_dir: "/Users/test/Downloads/workspace",
        staged_workspace_dir: "/managed/workspace-imports/default-test",
      });
      expect(setAgents).toHaveBeenCalledWith([]);
    });
  });

  it("displays i18n key when there is no active model", async () => {
    vi.mocked(providerApi.getActiveModels).mockResolvedValue({
      active_llm: undefined,
    });
    renderWithProviders(<ModelSelector />);
    expect(
      await screen.findByText("modelSelector.selectModel"),
    ).toBeInTheDocument();
  });

  it("does not display a stale active model outside the eligible list", async () => {
    // provider has no api_key configured, so it is excluded from eligible list
    vi.mocked(providerApi.listProviders).mockResolvedValue([
      { ...mockProvider, api_key: "" },
    ]);
    renderWithProviders(<ModelSelector />);
    expect(
      await screen.findByText("modelSelector.selectModel"),
    ).toBeInTheDocument();
    expect(screen.queryByText("GPT-4")).not.toBeInTheDocument();
  });

  it("hides local discovery providers until they have selectable models", async () => {
    vi.mocked(providerApi.listProviders).mockResolvedValue([
      mockProvider,
      mockLocalProvider,
    ]);
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await screen.findByText("GPT-4");

    await user.click(screen.getByText("GPT-4"));

    expect(await screen.findByText("OpenAI")).toBeInTheDocument();
    expect(screen.queryByText("Ollama")).not.toBeInTheDocument();
    expect(screen.queryByText("正在探测本地模型...")).not.toBeInTheDocument();
    expect(screen.queryByText("暂未发现本地模型")).not.toBeInTheDocument();
    await waitFor(() => expect(providerApi.discoverModels).toHaveBeenCalled());
    await waitFor(() =>
      expect(providerApi.listProviders).toHaveBeenCalledTimes(3),
    );
  });

  it("shows local discovery providers once model rows exist", async () => {
    vi.mocked(providerApi.listProviders).mockResolvedValue([
      mockProvider,
      {
        ...mockLocalProvider,
        models: [
          {
            id: "qwen3:8b",
            name: "qwen3:8b",
            supports_multimodal: false,
            supports_image: false,
            supports_video: false,
            generate_kwargs: {},
          },
        ],
      },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await screen.findByText("GPT-4");

    await user.click(screen.getByText("GPT-4"));

    expect(await screen.findByText("Ollama")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ollama，1 个模型" }));
    expect((await screen.findAllByText("qwen3:8b")).length).toBeGreaterThan(0);
    await waitFor(() => expect(providerApi.discoverModels).toHaveBeenCalled());
    await waitFor(() =>
      expect(providerApi.listProviders).toHaveBeenCalledTimes(3),
    );
  });

  it("calls listProviders and getActiveModels on mount", async () => {
    renderWithProviders(<ModelSelector />);
    await screen.findByText("GPT-4");
    expect(providerApi.listProviders).toHaveBeenCalledOnce();
    expect(providerApi.getActiveModels).toHaveBeenCalledWith({
      scope: "effective",
      agent_id: "default",
    });
  });

  it("clicking trigger button opens dropdown and shows provider list", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await screen.findByText("GPT-4");

    await user.click(screen.getByText("GPT-4"));

    expect(await screen.findByText("OpenAI")).toBeInTheDocument();
  });

  it("shows scope plus current and recent models before provider browsing", async () => {
    window.localStorage.setItem(
      "lantai:model-selector:recent:default",
      JSON.stringify([{ providerId: "openai", modelId: "gpt-3.5-turbo" }]),
    );
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await user.click(await screen.findByText("GPT-4"));

    expect(screen.getByText("当前智能体 · default")).toBeInTheDocument();
    expect(screen.getByText(/历史回复保留原模型/)).toBeInTheDocument();
    expect(screen.getByText("快速选择")).toBeInTheDocument();
    expect(screen.getByText("当前")).toBeInTheDocument();
    expect(screen.getByText("最近")).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", {
        name: "选择 GPT-3.5 Turbo，提供商 OpenAI",
      }),
    ).toHaveLength(2);
  });

  it("collapses a provider with many models by default and lets the user expand it", async () => {
    vi.mocked(providerApi.listProviders).mockResolvedValue([
      {
        ...mockProvider,
        models: [
          ...mockProvider.models,
          ...Array.from({ length: 4 }, (_, index) => ({
            ...mockProvider.models[0],
            id: `gpt-extra-${index + 1}`,
            name: `GPT Extra ${index + 1}`,
          })),
        ],
      },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await screen.findByText("GPT-4");

    await user.click(screen.getByText("GPT-4"));

    const providerToggle = await screen.findByRole("button", {
      name: "OpenAI，6 个模型",
    });
    expect(providerToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("GPT-3.5 Turbo")).not.toBeInTheDocument();

    await user.click(providerToggle);

    expect(providerToggle).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("GPT-3.5 Turbo")).toBeInTheDocument();

    await user.keyboard("{Enter}");
    expect(providerToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("GPT-3.5 Turbo")).not.toBeInTheDocument();
  });

  it("temporarily expands collapsed providers while searching", async () => {
    vi.mocked(providerApi.listProviders).mockResolvedValue([
      {
        ...mockProvider,
        models: [
          ...mockProvider.models,
          ...Array.from({ length: 4 }, (_, index) => ({
            ...mockProvider.models[0],
            id: `gpt-extra-${index + 1}`,
            name: `GPT Extra ${index + 1}`,
          })),
        ],
      },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await screen.findByText("GPT-4");
    await user.click(screen.getByText("GPT-4"));

    const providerToggle = await screen.findByRole("button", {
      name: "OpenAI，6 个模型",
    });
    expect(providerToggle).toHaveAttribute("aria-expanded", "false");

    const search = screen.getByPlaceholderText("搜索模型或供应商...");
    await user.type(search, "Turbo");

    expect(providerToggle).toHaveAttribute("aria-expanded", "true");
    expect(providerToggle).toBeDisabled();
    expect(await screen.findByText("GPT-3.5 Turbo")).toBeInTheDocument();

    await user.clear(search);

    expect(providerToggle).toHaveAttribute("aria-expanded", "false");
    expect(providerToggle).toBeEnabled();
    expect(screen.queryByText("GPT-3.5 Turbo")).not.toBeInTheDocument();
  });

  it("keeps runtime routing diagnostics out of the model picker", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await screen.findByText("GPT-4");

    await user.click(screen.getByText("GPT-4"));

    expect(screen.queryByText("云端模型")).not.toBeInTheDocument();
    expect(screen.queryByText("本地优先")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/当前选择会作为本轮默认模型/),
    ).not.toBeInTheDocument();
  });

  it("clicking a model calls setActiveLlm with correct parameters", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await screen.findByText("GPT-4");

    await user.click(screen.getByText("GPT-4"));
    const gpt35 = await screen.findByText("GPT-3.5 Turbo");
    await user.click(gpt35);

    expect(providerApi.setActiveLlm).toHaveBeenCalledWith({
      provider_id: "openai",
      model: "gpt-3.5-turbo",
      scope: "agent",
      agent_id: "default",
    });
  });

  it("clicking the already active model does not call setActiveLlm", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await screen.findByText("GPT-4");

    await user.click(screen.getByText("GPT-4"));
    const gpt4Items = await screen.findAllByText("GPT-4");
    await user.click(gpt4Items[gpt4Items.length - 1]);

    expect(providerApi.setActiveLlm).not.toHaveBeenCalled();
  });

  it("dropdown shows empty state when no providers are available", async () => {
    vi.mocked(providerApi.listProviders).mockResolvedValue([]);
    vi.mocked(providerApi.getActiveModels).mockResolvedValue({
      active_llm: undefined,
    });
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await screen.findByText("modelSelector.selectModel");

    await user.click(screen.getByText("modelSelector.selectModel"));

    expect(
      await screen.findByText("modelSelector.noConfiguredModels"),
    ).toBeInTheDocument();
  });

  it("still displays original active model after setActiveLlm failure", async () => {
    vi.mocked(providerApi.setActiveLlm).mockRejectedValue(
      new Error("API error"),
    );
    const user = userEvent.setup();
    renderWithProviders(<ModelSelector />);
    await screen.findByText("GPT-4");

    await user.click(screen.getByText("GPT-4"));
    const gpt35 = await screen.findByText("GPT-3.5 Turbo");
    await user.click(gpt35);

    // GPT-4 may appear in two places when dropdown is still open (trigger + dropdown item)
    await waitFor(() => {
      expect(screen.getAllByText("GPT-4").length).toBeGreaterThanOrEqual(1);
    });
  });
});
