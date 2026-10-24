import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DEFAULT_WORK_METHODS, readWorkMethodId } from "../workMethod";
import WorkMethodSelector from "./WorkMethodSelector";

describe("WorkMethodSelector", () => {
  it("keeps the offline fallback free of mode-specific resource quotas", () => {
    expect(DEFAULT_WORK_METHODS).toMatchObject([
      {
        id: "standard",
        summary: "默认对话方式。自然回答，只在用户需要行动时调用工具。",
        max_no_progress_iterations: 3,
      },
      { id: "deep_research", max_no_progress_iterations: 3 },
      { id: "research_delivery", max_no_progress_iterations: 3 },
      { id: "sustained_execution", max_no_progress_iterations: 3 },
      { id: "collaboration", max_no_progress_iterations: 3 },
    ]);
    expect(
      DEFAULT_WORK_METHODS.every(
        (method) =>
          method.max_steps == null &&
          method.max_tool_calls == null &&
          method.max_wall_seconds == null,
      ),
    ).toBe(true);
  });

  it("migrates the retired automatic classifier to the standard loop", () => {
    localStorage.setItem("lantai:work-method:v1:agent:chat", "auto");
    expect(readWorkMethodId("agent", "chat")).toBe("standard");
    localStorage.removeItem("lantai:work-method:v1:agent:chat");
  });
  it("shows the active method and selects another trusted method", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <WorkMethodSelector
        methods={DEFAULT_WORK_METHODS}
        value="standard"
        onChange={onChange}
      />,
    );

    const trigger = screen.getByRole("button", { name: "工作法：标准" });
    expect(trigger).toHaveAttribute("data-chat-work-method-trigger", "1");

    await user.click(trigger);
    await user.click(screen.getByRole("menuitemradio", { name: /深度研究/ }));

    expect(onChange).toHaveBeenCalledWith("deep_research");
  });
});
