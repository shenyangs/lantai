import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../../test/common_setup";
import { ChannelCard } from "./ChannelCard";

describe("ChannelCard", () => {
  it("exposes its single navigation action and status to keyboard users", () => {
    const onClick = vi.fn();
    renderWithProviders(
      <ChannelCard
        channelKey="dingtalk"
        config={{ enabled: true, bot_prefix: "小澜" }}
        onClick={onClick}
      />,
    );

    const card = screen.getByRole("button", { name: /打开渠道设置$/ });
    expect(card).toHaveAttribute("tabindex", "0");
    expect(card).toHaveAccessibleName(/enabled|已启用/);

    fireEvent.keyDown(card, { key: "Enter" });
    fireEvent.keyDown(card, { key: " " });
    expect(onClick).toHaveBeenCalledTimes(2);
  });
});
