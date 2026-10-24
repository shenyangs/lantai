import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "../../../../../api";
import type { ProviderInfo } from "../../../../../api/types";
import { LocalProviderCard } from "./LocalProviderCard";

vi.mock("../../../../../api", () => ({
  default: {
    testProviderConnection: vi.fn(),
    discoverModels: vi.fn(),
  },
}));

vi.mock("../modals/ProviderConfigModal", () => ({
  ProviderConfigModal: () => null,
}));

vi.mock("../modals/ModelManageModal", () => ({
  ModelManageModal: () => null,
}));

const successMessage = vi.fn();
const errorMessage = vi.fn();

vi.mock("../../../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: {
      success: successMessage,
      error: errorMessage,
      info: vi.fn(),
    },
  }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const provider: ProviderInfo = {
  id: "ollama",
  name: "Ollama",
  api_key_prefix: "",
  chat_model: "OpenAIChatModel",
  models: [],
  extra_models: [
    {
      id: "qwen3:8b",
      name: "qwen3:8b",
      supports_multimodal: false,
      supports_image: false,
      supports_video: false,
      supports_tool_use: true,
      generate_kwargs: {},
    },
  ],
  is_custom: false,
  is_local: true,
  support_model_discovery: true,
  support_connection_check: true,
  freeze_url: false,
  require_api_key: false,
  api_key: "",
  base_url: "http://localhost:11434",
  generate_kwargs: {},
};

describe("LocalProviderCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.testProviderConnection).mockResolvedValue({
      success: true,
      message: "Ollama is running with 1 local model(s).",
    });
    vi.mocked(api.discoverModels).mockResolvedValue({
      success: true,
      message: "",
      models: provider.extra_models,
      added_count: 1,
    });
  });

  it("checks the real local runtime instead of inferring health from saved models", async () => {
    render(
      <LocalProviderCard
        provider={provider}
        activeModels={null}
        onSaved={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(api.testProviderConnection).toHaveBeenCalledWith("ollama"),
    );
    expect(screen.getByText("models.localRuntimeOnline")).toBeInTheDocument();
    expect(screen.getByText("models.localAgentReadyCount")).toBeInTheDocument();
    expect(screen.queryByText("API Key:")).not.toBeInTheDocument();
  });

  it("syncs native models and refreshes provider state", async () => {
    const onSaved = vi.fn().mockResolvedValue(undefined);
    render(
      <LocalProviderCard
        provider={provider}
        activeModels={null}
        onSaved={onSaved}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: /models\.syncLocalModels/ }),
    );

    await waitFor(() =>
      expect(api.discoverModels).toHaveBeenCalledWith(
        "ollama",
        undefined,
        true,
      ),
    );
    expect(onSaved).toHaveBeenCalledOnce();
    expect(successMessage).toHaveBeenCalled();
  });

  it("automatically discovers models on the first healthy empty connection", async () => {
    const onSaved = vi.fn().mockResolvedValue(undefined);
    render(
      <LocalProviderCard
        provider={{ ...provider, extra_models: [] }}
        activeModels={null}
        onSaved={onSaved}
      />,
    );

    await waitFor(() =>
      expect(api.discoverModels).toHaveBeenCalledWith(
        "ollama",
        undefined,
        true,
      ),
    );
    expect(onSaved).toHaveBeenCalledOnce();
  });

  it("shows an offline state when the runtime cannot be reached", async () => {
    vi.mocked(api.testProviderConnection).mockResolvedValue({
      success: false,
      message: "Start Ollama with ollama serve.",
    });

    render(
      <LocalProviderCard
        provider={provider}
        activeModels={null}
        onSaved={vi.fn()}
      />,
    );

    expect(
      await screen.findByText("models.localRuntimeOffline"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("models.localRuntimeStartHint"),
    ).toBeInTheDocument();
  });
});
