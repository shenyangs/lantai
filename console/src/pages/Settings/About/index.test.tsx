import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../../test/common_setup";
import AboutPage from "./index";

vi.mock("../../../api/modules/root", () => ({
  rootApi: { getVersion: vi.fn().mockResolvedValue({ version: "2.0.26" }) },
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("preview")));
});

afterEach(() => vi.unstubAllGlobals());

describe("AboutPage", () => {
  it("uses the current page name as its only level-one heading", () => {
    renderWithProviders(<AboutPage />, {
      initialEntries: ["/settings/about"],
    });

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(
      screen.getByRole("heading", { level: 1, name: "关于澜台" }),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { level: 2, name: "澜台" }),
    ).toBeVisible();
  });

  it("does not expose telemetry or diagnostic controls", () => {
    renderWithProviders(<AboutPage />, {
      initialEntries: ["/settings/about"],
    });

    expect(screen.queryByText("隐私与诊断")).not.toBeInTheDocument();
    expect(screen.queryByText("帮助澜台改进")).not.toBeInTheDocument();
    expect(screen.queryByText("导出诊断报告")).not.toBeInTheDocument();
  });

  it("opens the developer GitHub profile in the desktop system browser", async () => {
    const user = userEvent.setup();
    const openExternal = vi.fn().mockResolvedValue({ success: true });
    window.electronAPI = {
      isDesktop: true,
      platform: "darwin",
      apiBaseUrl: "",
      getAppVersion: vi.fn().mockResolvedValue({
        ok: true,
        version: "2.0.26",
      }),
      openExternal,
    };

    renderWithProviders(<AboutPage />, {
      initialEntries: ["/settings/about"],
    });

    const githubLink = await screen.findByRole("link", {
      name: /GitHub/,
    });
    expect(githubLink).toHaveAttribute("href", "https://github.com/shenyangs");

    await user.click(githubLink);

    expect(openExternal).toHaveBeenCalledWith("https://github.com/shenyangs");
  });

  it("shows the embedded build lineage", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            version: "2.1.7",
            gitSha: "1234567890abcdef1234567890abcdef12345678",
            shortGitSha: "1234567890ab",
            sourceBranch: "main",
            buildTime: "2026-08-31T13:00:00Z",
            canonicalMain: true,
          }),
      }),
    );

    renderWithProviders(<AboutPage />, {
      initialEntries: ["/settings/about"],
    });

    expect(await screen.findByText("构建身份")).toBeInTheDocument();
    expect(screen.getByText("1234567890ab")).toBeInTheDocument();
    expect(screen.getByText("已验证 main")).toBeInTheDocument();
  });
});
