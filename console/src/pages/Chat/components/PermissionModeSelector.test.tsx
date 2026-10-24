import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import PermissionModeSelector from "./PermissionModeSelector";

describe("PermissionModeSelector comprehensibility", () => {
  it("changes the current conversation preset and closes the menu", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <PermissionModeSelector
        value="risk"
        onChange={onChange}
        onOpenCustom={vi.fn()}
      />,
    );

    const trigger = screen.getByRole("button", { name: /权限模式/ });
    await user.click(trigger);
    await user.click(screen.getByRole("menuitemradio", { name: /请求权限/ }));

    expect(onChange).toHaveBeenCalledWith("request");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("opens defaults separately and hides complete access when unavailable", async () => {
    const user = userEvent.setup();
    const onOpenCustom = vi.fn();
    render(
      <PermissionModeSelector
        value="risk"
        allowFullAccess={false}
        onChange={vi.fn()}
        onOpenCustom={onOpenCustom}
      />,
    );

    await user.click(screen.getByRole("button", { name: /权限模式/ }));
    expect(
      screen.queryByRole("menuitemradio", { name: /完全访问/ }),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("menuitem", { name: /设置后台任务的默认权限/ }),
    );
    expect(onOpenCustom).toHaveBeenCalledTimes(1);
  });

  it("explains that a chat choice is local and cannot bypass hard limits", async () => {
    const user = userEvent.setup();
    render(
      <PermissionModeSelector
        value="risk"
        onChange={vi.fn()}
        onOpenCustom={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /权限模式/ }));

    expect(screen.getByText("只影响当前对话")).toBeInTheDocument();
    expect(
      screen.getByText(/其他对话和后台任务的默认权限不会改变/),
    ).toBeInTheDocument();
    expect(screen.getByText(/系统硬性限制始终有效/)).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: /设置后台任务的默认权限/ }),
    ).toBeInTheDocument();
  });
});
