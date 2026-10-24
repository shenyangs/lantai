import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import WorkMethodProjectionCard from "./WorkMethodProjectionCard";

describe("WorkMethodProjectionCard", () => {
  it("renders an evidence-centered deep research projection", () => {
    render(
      <WorkMethodProjectionCard
        methodId="deep_research"
        generating
        events={[
          {
            id: "start",
            seq: 1,
            type: "turn.started",
            status: "running",
            payload: { phase: "framing" },
          },
          {
            id: "tool",
            seq: 2,
            type: "tool.started",
            status: "running",
            payload: {
              phase: "collecting",
              trace_detail: "正在交叉核对两个独立来源",
            },
          },
        ]}
      />,
    );

    expect(screen.getByText("深度研究")).toBeInTheDocument();
    expect(screen.getByText("立题")).toBeInTheDocument();
    expect(screen.getByText("取证")).toBeInTheDocument();
    expect(screen.getByText("正在交叉核对两个独立来源")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("shows sustained execution retries without inventing a percentage", () => {
    render(
      <WorkMethodProjectionCard
        methodId="sustained_execution"
        generating
        events={[
          {
            id: "retry",
            seq: 2,
            type: "turn.continue_requested",
            status: "info",
            payload: { mode: "retry" },
          },
          {
            id: "tool",
            seq: 3,
            type: "tool.started",
            status: "running",
            payload: { phase: "executing", trace_title: "重新执行验证" },
          },
        ]}
      />,
    );

    expect(screen.getByText("持续执行")).toBeInTheDocument();
    expect(screen.getByText("已开启 1 次新尝试")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });
});
