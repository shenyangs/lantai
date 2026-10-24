import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  BEVERAGE_AGENT_AVATARS,
  BeverageAgentAvatar,
  getBeverageAgentAvatar,
} from "./index";

describe("BeverageAgentAvatar", () => {
  it("gives every built-in beverage agent a constrained illustration spec", () => {
    expect(Object.keys(BEVERAGE_AGENT_AVATARS)).toHaveLength(24);
    expect(getBeverageAgentAvatar("Lantai_ColdBrew_Agent")).toMatchObject({
      name: "冷萃",
      mark: "flask",
    });
    expect(getBeverageAgentAvatar("Lantai_Cola_Agent")).toMatchObject({
      name: "可乐",
      vessel: "can",
      mark: "quote",
    });
  });

  it("renders a labeled line-art avatar and keeps a generic fallback", () => {
    const { rerender } = render(
      <BeverageAgentAvatar agentId="Lantai_ClearTea_Agent" />,
    );
    expect(screen.getByRole("img", { name: "清茶头像" })).toHaveAttribute(
      "data-agent-avatar",
      "Lantai_ClearTea_Agent",
    );

    rerender(<BeverageAgentAvatar agentId="custom-agent" />);
    expect(screen.getByRole("img", { name: "智能体头像" })).toBeInTheDocument();
  });
});
