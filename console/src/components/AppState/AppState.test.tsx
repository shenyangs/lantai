import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Button } from "@/vendor/lantaiDesign";
import { AppState } from "./index";

describe("AppState", () => {
  it("announces loading without presenting it as an error", () => {
    render(
      <AppState tone="loading" title="加载中" description="正在准备当前页面" />,
    );

    const state = screen.getByRole("status");
    expect(state).toHaveAttribute("aria-busy", "true");
    expect(state).toHaveTextContent("正在准备当前页面");
  });

  it("uses an assertive alert for recoverable errors", () => {
    render(<AppState tone="error" title="工作台遇到问题" />);

    expect(screen.getByRole("alert")).toHaveAttribute("aria-live", "assertive");
  });

  it("preserves the explicit primary action instead of promoting the first button", () => {
    render(
      <AppState
        title="暂无安排"
        actions={
          <>
            <Button>继续工作</Button>
            <Button type="primary">创建提醒</Button>
          </>
        }
      />,
    );

    expect(screen.getByRole("button", { name: "继续工作" })).not.toHaveClass(
      "ant-btn-primary",
    );
    expect(screen.getByRole("button", { name: "创建提醒" })).toHaveClass(
      "ant-btn-primary",
    );

    const stylesheet = readFileSync(
      join(process.cwd(), "src/components/AppState/index.module.less"),
      "utf8",
    );
    expect(stylesheet).not.toContain("button:first-child");
  });
});
