import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../../../../test/common_setup";
import { KdocsConnectionPanel } from "./KdocsConnectionPanel";

const apiMock = vi.hoisted(() => ({
  getKdocsConnectionStatus: vi.fn(),
  connectKdocs: vi.fn(),
  disconnectKdocs: vi.fn(),
}));

const translationMock = vi.hoisted(() => ({
  t: (key: string) => key,
}));

const messageMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/api", () => ({ api: apiMock }));

vi.mock("react-i18next", () => ({
  useTranslation: () => translationMock,
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: messageMock }),
}));

describe("KdocsConnectionPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.electronAPI = undefined;
    apiMock.getKdocsConnectionStatus.mockReset();
    apiMock.connectKdocs.mockReset();
    apiMock.disconnectKdocs.mockReset();
    apiMock.getKdocsConnectionStatus.mockResolvedValue({
      installed: false,
      authenticated: false,
      cli_version: "",
      token_url: "https://www.kdocs.cn/latest",
    });
    apiMock.connectKdocs.mockResolvedValue({
      installed: true,
      authenticated: true,
      cli_version: "2.5.7",
      token_url: "https://www.kdocs.cn/latest",
    });
  });

  it("binds the token, clears the secret input, and shows verified state", async () => {
    const user = userEvent.setup();
    const token = "private-kdocs-token-value";
    renderWithProviders(<KdocsConnectionPanel />);

    const input = await screen.findByLabelText("skills.kdocsTokenLabel");
    await user.type(input, token);
    await user.click(
      screen.getByRole("button", { name: "skills.kdocsConnect" }),
    );

    await waitFor(() =>
      expect(apiMock.connectKdocs).toHaveBeenCalledWith(token),
    );
    await waitFor(() => expect(input).toHaveValue(""));
    expect(
      await screen.findByText("skills.kdocsConnected"),
    ).toBeInTheDocument();
    expect(screen.queryByText(token)).not.toBeInTheDocument();
  });

  it("opens the token page through the desktop external-link bridge", async () => {
    const user = userEvent.setup();
    const openExternal = vi.fn().mockResolvedValue({ success: true });
    window.electronAPI = { openExternal };
    const windowOpen = vi.spyOn(window, "open").mockReturnValue(null);
    renderWithProviders(<KdocsConnectionPanel />);

    const label = await screen.findByText("skills.kdocsOpenTokenPage");
    await user.click(label.closest("button") as HTMLButtonElement);

    await waitFor(() =>
      expect(openExternal).toHaveBeenCalledWith("https://www.kdocs.cn/latest"),
    );
    expect(windowOpen).not.toHaveBeenCalled();
  });
});
