import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MacUseTargetBanner, MacUseTargetDialog } from "./MacUsePicker";

const apiMocks = vi.hoisted(() => ({
  listWindows: vi.fn(),
}));

vi.mock("../../../api/modules/macUse", () => ({
  macUseApi: apiMocks,
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: { error: vi.fn(), warning: vi.fn(), success: vi.fn() },
  }),
}));

describe("MacUseTargetDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.listWindows.mockResolvedValue({
      ok: true,
      data: {
        windows: [
          {
            window_id: "42",
            process_id: 101,
            process_started_at: 1788700000.125,
            owner_name: "文本编辑",
            window_title: "fixture.txt",
            app_bundle_id: "com.apple.TextEdit",
            app_path: "/System/Applications/TextEdit.app",
            on_screen: true,
            bounds: { x: 10, y: 20, width: 800, height: 600 },
          },
        ],
      },
    });
    window.electronAPI = {
      isDesktop: true,
      platform: "darwin",
      apiBaseUrl: "",
      selectMacUseTarget: vi.fn().mockResolvedValue({
        ok: true,
        data: {
          selection_receipt:
            "macsel_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
      }),
    };
  });

  it("selects a concrete application and window identity", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onClose = vi.fn();
    render(
      <MacUseTargetDialog
        open
        value={null}
        onChange={onChange}
        onClose={onClose}
      />,
    );

    expect(
      await screen.findByRole("dialog", {
        name: "选择要操作的 Mac 应用（实验）",
      }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: /文本编辑/ }));

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith({
        app_bundle_id: "com.apple.TextEdit",
        window_id: "42",
        process_id: 101,
        process_started_at: 1788700000.125,
        window_title: "fixture.txt",
        owner_name: "文本编辑",
        selection_receipt:
          "macsel_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      }),
    );
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does not read or expose application windows before a task asks for them", () => {
    render(
      <MacUseTargetDialog
        open={false}
        value={null}
        onChange={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(apiMocks.listWindows).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("dialog", {
        name: "选择要操作的 Mac 应用（实验）",
      }),
    ).not.toBeInTheDocument();
  });

  it("shows the selected target without exposing process ids", async () => {
    const user = userEvent.setup();
    const onClear = vi.fn();
    render(
      <MacUseTargetBanner
        target={{
          app_bundle_id: "com.apple.TextEdit",
          window_id: "42",
          process_id: 101,
          process_started_at: 1788700000.125,
          window_title: "fixture.txt",
          owner_name: "文本编辑",
          selection_receipt:
            "macsel_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        }}
        onClear={onClear}
      />,
    );

    expect(screen.getByText("将操作：文本编辑")).toBeVisible();
    expect(screen.getByText("fixture.txt")).toBeVisible();
    expect(screen.queryByText("42")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "移除 Mac 应用目标" }));
    await waitFor(() => expect(onClear).toHaveBeenCalledOnce());
  });
});
