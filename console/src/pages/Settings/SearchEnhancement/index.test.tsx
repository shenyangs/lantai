import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../test/common_setup";
import SearchEnhancementPage from "./index";
import { useSearchEnhancement } from "./useSearchEnhancement";

vi.mock("./useSearchEnhancement", () => ({
  useSearchEnhancement: vi.fn(),
}));

const mockUseSearchEnhancement = vi.mocked(useSearchEnhancement);

describe("SearchEnhancementPage", () => {
  const setEnabled = vi.fn(async () => undefined);
  const refresh = vi.fn(async () => undefined);

  beforeEach(() => {
    setEnabled.mockClear();
    refresh.mockClear();
    mockUseSearchEnhancement.mockReturnValue({
      guanlan: {
        client_key: "guanlan",
        name: "Guanlan",
        enabled: false,
        configured: false,
        description: "中文互联网搜索增强",
        package_available: true,
        package_version: "0.10.10",
        bundled_version: "0.10.10",
        package_status: "ready",
        package_message: "",
        capability_status: "disabled",
        connected: false,
        runtime_attempt_count: 0,
        runtime_error_code: null,
        runtime_source: "bundled",
        runtime_message: "",
        runtime_updated_at: null,
        package_update_supported: false,
        package_updated: false,
        package_previous_version: null,
        package_restart_required: false,
        package_candidate_version: null,
        package_metadata_source: "unavailable",
        package_metadata_degraded: false,
        package_update_available: false,
        repository_url: "https://github.com/shenyangs/GuanLan",
        license: "MIT License",
        tools: [
          { name: "guanlan_search", description: "Search" },
          { name: "guanlan_read", description: "Read" },
          { name: "guanlan_hotnews", description: "Hot news" },
          { name: "guanlan_research", description: "Research" },
        ],
      },
      loading: false,
      mutating: false,
      setEnabled,
      refresh,
    });
  });

  it("keeps Guanlan as a single product-level toggle", () => {
    renderWithProviders(<SearchEnhancementPage />, {
      initialEntries: ["/search-enhancement"],
    });

    expect(screen.getByText("searchEnhancement.title")).toBeVisible();
    expect(screen.getByText("mcp.guanlanName")).toBeVisible();

    const toggle = screen.getByRole("switch", {
      name: "searchEnhancement.guanlanToggle",
    });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(setEnabled).toHaveBeenCalledWith(true);
  });

  it("exposes Guanlan-specific runtime, capability, and project management", () => {
    renderWithProviders(<SearchEnhancementPage />, {
      initialEntries: ["/search-enhancement"],
    });

    expect(screen.getByText("searchEnhancement.runtimeTitle")).toBeVisible();
    expect(screen.getByText("searchEnhancement.capabilitiesTitle")).toBeVisible();
    expect(screen.getByText("searchEnhancement.boundariesTitle")).toBeVisible();
    expect(screen.getByText("mcp.guanlanToolSearch")).toBeVisible();
    expect(screen.getByText("mcp.guanlanToolRead")).toBeVisible();
    expect(screen.getByText("mcp.guanlanToolHotnews")).toBeVisible();
    expect(screen.getByText("mcp.guanlanToolResearch")).toBeVisible();
    expect(screen.getByRole("link", { name: "mcp.guanlanRepo" })).toHaveAttribute(
      "href",
      "https://github.com/shenyangs/GuanLan",
    );
  });

  it("refreshes Guanlan diagnostics without changing its enabled state", () => {
    renderWithProviders(<SearchEnhancementPage />, {
      initialEntries: ["/search-enhancement"],
    });

    fireEvent.click(
      screen.getByRole("button", { name: "searchEnhancement.refreshStatus" }),
    );
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(setEnabled).not.toHaveBeenCalled();
  });
});
