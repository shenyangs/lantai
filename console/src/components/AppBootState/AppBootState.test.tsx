import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AppBootState } from "./index";

describe("AppBootState", () => {
  it("announces that the workspace is being prepared", () => {
    render(<AppBootState />);

    const state = screen.getByRole("status");
    expect(state).toHaveAttribute("aria-busy", "true");
    expect(state).toHaveTextContent("澜台");
    expect(state).toHaveTextContent("正在准备工作台");
  });
});
