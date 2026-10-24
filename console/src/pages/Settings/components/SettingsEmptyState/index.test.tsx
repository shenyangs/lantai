import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SettingsEmptyState } from ".";

describe("SettingsEmptyState", () => {
  it("presents a concise title and optional next step", () => {
    render(
      <SettingsEmptyState
        title="暂无统计数据"
        description="调整时间范围后重试。"
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("暂无统计数据");
    expect(screen.getByRole("status")).toHaveTextContent(
      "调整时间范围后重试。",
    );
  });
});
