import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { utilityIntelligenceApi } from "@/api";
import { UtilityIntelligenceSection } from "./UtilityIntelligenceSection";

vi.mock("@/api", () => ({
  utilityIntelligenceApi: {
    getUtilityIntelligenceConfig: vi.fn(),
    getBackgroundSemanticStatus: vi.fn(),
    updateUtilityIntelligenceConfig: vi.fn(),
  },
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: {
      success: vi.fn(),
      error: vi.fn(),
    },
  }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const getConfig = vi.mocked(
  utilityIntelligenceApi.getUtilityIntelligenceConfig,
);
const updateConfig = vi.mocked(
  utilityIntelligenceApi.updateUtilityIntelligenceConfig,
);
const getStatus = vi.mocked(utilityIntelligenceApi.getBackgroundSemanticStatus);

const providers = [
  {
    id: "provider-1",
    name: "Provider One",
    api_key_prefix: "",
    chat_model: "model-1",
    models: [
      {
        id: "model-1",
        name: "Model One",
        supports_multimodal: false,
        supports_image: false,
        supports_video: false,
        generate_kwargs: {},
      },
    ],
    extra_models: [],
    is_custom: false,
    is_local: false,
    support_model_discovery: true,
    support_connection_check: true,
    freeze_url: false,
    require_api_key: true,
    api_key: "configured",
    base_url: "https://example.com/v1",
    generate_kwargs: {},
  },
];

describe("UtilityIntelligenceSection", () => {
  beforeEach(() => {
    getConfig.mockReset();
    updateConfig.mockReset();
    getStatus.mockReset();
    getConfig.mockResolvedValue({
      mode: "follow_active",
      background_processing_enabled: true,
      model: { provider_id: "", model: "" },
      timeout_seconds: 12,
    });
    getStatus.mockResolvedValue({
      background_processing_enabled: true,
      routing_mode: "follow_active",
      queue: {
        agent_id: "default",
        worker_running: true,
        pending: 0,
        running: 0,
        completed: 3,
        failed: 0,
        last_error: "",
        last_failed_at: null,
        last_completed_at: 1,
      },
    });
    updateConfig.mockImplementation(async (config) => config);
  });

  it("loads independently from the active chat model controls", async () => {
    render(<UtilityIntelligenceSection providers={providers} />);

    await waitFor(() => expect(getConfig).toHaveBeenCalledOnce());
    expect(screen.getByText("models.utilityModel")).toBeInTheDocument();
    expect(
      screen.getByText("models.utilityFollowDescription"),
    ).toBeInTheDocument();
    expect(screen.queryByText("models.selectProvider")).not.toBeInTheDocument();
  });

  it("can disable background processing without changing model routing", async () => {
    render(<UtilityIntelligenceSection providers={providers} />);
    await waitFor(() => expect(getConfig).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("switch"));
    expect(screen.getByText("尚未保存")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "取消修改后台智能" }),
    ).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /models\.save$/ }));

    await waitFor(() =>
      expect(updateConfig).toHaveBeenCalledWith({
        mode: "follow_active",
        background_processing_enabled: false,
        model: { provider_id: "", model: "" },
        timeout_seconds: 12,
      }),
    );
  });

  it("can discard a staged switch change without saving it", async () => {
    render(<UtilityIntelligenceSection providers={providers} />);
    await waitFor(() => expect(getConfig).toHaveBeenCalledOnce());

    const toggle = screen.getByRole("switch");
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "取消修改后台智能" }));

    expect(toggle).toBeChecked();
    expect(updateConfig).not.toHaveBeenCalled();
    expect(screen.getByText("已保存")).toBeInTheDocument();
  });
});
