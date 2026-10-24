import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { webSearchApi } from "@/api";
import { WebSearchSection } from "./WebSearchSection";

vi.mock("@/api", () => ({
  webSearchApi: {
    getConfig: vi.fn(),
    updateConfig: vi.fn(),
  },
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: { success: vi.fn(), error: vi.fn() },
  }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

describe("WebSearchSection", () => {
  beforeEach(() => {
    vi.mocked(webSearchApi.getConfig).mockReset();
    vi.mocked(webSearchApi.updateConfig).mockReset();
    vi.mocked(webSearchApi.getConfig).mockResolvedValue({
      configured: false,
      enabled: false,
      provider: "bing_rss",
      search_count: 0,
      fetch_count: 0,
      migrated_from_quick_search: false,
      credential_error: false,
      last_used_at: "",
    });
    vi.mocked(webSearchApi.updateConfig).mockResolvedValue({
      configured: true,
      enabled: true,
      provider: "deepseek",
      search_count: 0,
      fetch_count: 0,
      migrated_from_quick_search: false,
      credential_error: false,
      last_used_at: "",
    });
  });

  it("saves its own key for the unified tools", async () => {
    render(<WebSearchSection />);
    await waitFor(() => expect(webSearchApi.getConfig).toHaveBeenCalledOnce());

    fireEvent.change(
      screen.getByPlaceholderText("models.webSearchKeyPlaceholder"),
      { target: { value: "sk-separate-search" } },
    );
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(
      screen.getByRole("button", { name: /models\.webSearchSave/ }),
    );

    await waitFor(() =>
      expect(webSearchApi.updateConfig).toHaveBeenCalledWith({
        api_key: "sk-separate-search",
        enabled: true,
      }),
    );
  });

  it("requires a separate key before saving an enabled provider", async () => {
    render(<WebSearchSection />);
    await waitFor(() => expect(screen.getByRole("switch")).toBeEnabled());
    fireEvent.click(screen.getByRole("switch"));
    expect(
      screen.getByRole("button", { name: /models\.webSearchSave/ }),
    ).toBeDisabled();
    expect(webSearchApi.updateConfig).not.toHaveBeenCalled();
  });

  it("disables with blank input while preserving the saved key", async () => {
    const saved = {
      configured: true,
      enabled: true,
      provider: "deepseek" as const,
      search_count: 3,
      fetch_count: 2,
      migrated_from_quick_search: true,
      credential_error: false,
      last_used_at: "",
    };
    vi.mocked(webSearchApi.getConfig).mockResolvedValue(saved);
    vi.mocked(webSearchApi.updateConfig).mockResolvedValue({
      ...saved,
      enabled: false,
      provider: "bing_rss",
    });
    render(<WebSearchSection />);
    await waitFor(() => expect(screen.getByRole("switch")).toBeChecked());
    expect(screen.getByText("models.webSearchMigrated")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(
      screen.getByRole("button", { name: /models\.webSearchSave/ }),
    );
    await waitFor(() =>
      expect(webSearchApi.updateConfig).toHaveBeenCalledWith({
        api_key: "",
        enabled: false,
      }),
    );
    expect(screen.getByLabelText("models.webSearchKey")).toHaveValue("");
  });

  it("blocks overwriting config when loading fails", async () => {
    vi.mocked(webSearchApi.getConfig).mockRejectedValue(
      new Error("unavailable"),
    );
    render(<WebSearchSection />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "models.webSearchLoadFailed",
    );
    expect(
      screen.queryByText("models.webSearchNotConfigured"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("switch")).toBeDisabled();
    expect(screen.getByLabelText("models.webSearchKey")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /models\.webSearchSave/ }),
    ).toBeDisabled();
    expect(webSearchApi.updateConfig).not.toHaveBeenCalled();
  });
});
