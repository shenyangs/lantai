import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { CapabilityCard, CapabilityPage } from "./index";
import styles from "./index.module.less";

describe("CapabilityPage detail navigation", () => {
  it("keeps top-level tools out of the settings detail hierarchy", () => {
    render(
      <MemoryRouter initialEntries={["/tools"]}>
        <CapabilityPage title="系统工具" description="管理系统工具">
          <div>工具列表</div>
        </CapabilityPage>
      </MemoryRouter>,
    );

    expect(
      screen.queryByRole("button", { name: "返回设置中心" }),
    ).not.toBeInTheDocument();
  });

  it("returns from an about detail page to the settings center", () => {
    render(
      <MemoryRouter initialEntries={["/about"]}>
        <Routes>
          <Route
            path="/about"
            element={
              <CapabilityPage title="关于澜台" description="查看版本信息">
                <div>版本信息</div>
              </CapabilityPage>
            }
          />
          <Route path="/settings" element={<div>设置中心</div>} />
        </Routes>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "返回设置中心" }));

    expect(screen.getByText("设置中心")).toBeInTheDocument();
  });

  it("does not add a settings back button to a top-level capability page", () => {
    render(
      <MemoryRouter initialEntries={["/skills"]}>
        <CapabilityPage title="技能" description="管理技能">
          <div>技能列表</div>
        </CapabilityPage>
      </MemoryRouter>,
    );

    expect(
      screen.queryByRole("button", { name: "返回设置中心" }),
    ).not.toBeInTheDocument();
  });

  it("supports a compact page header without requiring description copy", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/tools"]}>
        <CapabilityPage title="系统工具" compact>
          <div>工具列表</div>
        </CapabilityPage>
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("heading", { name: "系统工具" }),
    ).toBeInTheDocument();
    expect(container.querySelector("main")).toHaveClass(styles.pageCompact);
  });

  it("uses the explicit product section in the breadcrumb", () => {
    render(
      <MemoryRouter initialEntries={["/memory"]}>
        <CapabilityPage section="工作区" title="记忆">
          <div>记忆列表</div>
        </CapabilityPage>
      </MemoryRouter>,
    );

    expect(screen.getByText("工作区")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "记忆" })).toBeInTheDocument();
  });
});

describe("CapabilityCard interaction semantics", () => {
  it("keeps a card with local controls passive", () => {
    render(
      <CapabilityCard
        icon={<span>图标</span>}
        title="技能"
        footer={<button type="button">编辑</button>}
      />,
    );

    expect(screen.getByText("技能").closest("article")).not.toHaveAttribute(
      "role",
    );
    expect(screen.getByRole("button", { name: "编辑" })).toBeInTheDocument();
  });

  it("labels a single-action clickable card and supports the keyboard", () => {
    const onOpen = vi.fn();
    render(
      <CapabilityCard
        icon={<span>图标</span>}
        title="详情"
        onClick={onOpen}
        interactiveLabel="打开详情"
      />,
    );

    const card = screen.getByRole("button", { name: "打开详情" });
    fireEvent.keyDown(card, { key: "Enter" });
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
