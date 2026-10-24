import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MacUseToolCard, { isMacUseToolName } from "./MacUseToolCard";

function toolData(payload: Record<string, unknown>) {
  return {
    status: "success",
    content: [
      { data: { arguments: {} } },
      {
        data: {
          output: JSON.stringify({
            content: [{ type: "text", text: JSON.stringify(payload) }],
          }),
        },
      },
    ],
  };
}

beforeEach(() => {
  window.electronAPI = {
    isDesktop: true,
    platform: "darwin",
    apiBaseUrl: "",
    openPath: vi.fn(),
    stopMacUse: vi.fn(),
  };
});

describe("MacUseToolCard", () => {
  it("keeps ambiguous execution visibly distinct from success", () => {
    render(
      <MacUseToolCard
        data={toolData({
          ok: true,
          action: "press_element",
          dispatch_status: "sent",
          effect_status: "ambiguous",
          message:
            "The control accepted the press; verify the application state.",
        })}
      />,
    );

    expect(screen.getByText("待核验")).toBeVisible();
    expect(screen.queryByText("已核验")).not.toBeInTheDocument();
  });

  it("opens screenshot evidence from a verified observation", async () => {
    const user = userEvent.setup();
    render(
      <MacUseToolCard
        data={toolData({
          ok: true,
          action: "observe_window",
          effect_status: "not_applicable",
          data: { screenshot_path: "/tmp/mac-use-shot.png" },
        })}
      />,
    );

    await user.click(screen.getByRole("button", { name: "查看证据" }));
    expect(window.electronAPI?.openPath).toHaveBeenCalledWith(
      "/tmp/mac-use-shot.png",
    );
  });

  it("offers target selection only after the model inspects Mac app control", async () => {
    const user = userEvent.setup();
    const onRequestTarget = vi.fn();
    render(
      <MacUseToolCard
        data={toolData({
          ok: true,
          action: "status",
          effect_status: "not_applicable",
          message: "Mac 应用操作组件可用",
        })}
        onRequestTarget={onRequestTarget}
      />,
    );

    expect(screen.getByText("检查应用操作能力")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "选择要操作的应用" }));
    expect(onRequestTarget).toHaveBeenCalledOnce();
  });

  it("recognizes only the five Mac Use tools", () => {
    expect(isMacUseToolName("mac_act")).toBe(true);
    expect(isMacUseToolName("browser_use")).toBe(false);
  });
});
