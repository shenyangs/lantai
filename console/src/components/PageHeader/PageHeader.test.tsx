import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, type MemoryRouterProps } from "react-router-dom";
import type { ReactElement } from "react";
import { PageHeader } from "./index";

function renderPageHeader(node: ReactElement, routerProps?: MemoryRouterProps) {
  return render(<MemoryRouter {...routerProps}>{node}</MemoryRouter>);
}

describe("PageHeader", () => {
  it("renders current breadcrumb", () => {
    renderPageHeader(<PageHeader current="Settings" />);
    expect(
      screen.getByRole("heading", { level: 1, name: "Settings" }),
    ).toBeInTheDocument();
  });

  it("renders parent and current with separator", () => {
    renderPageHeader(<PageHeader parent="Home" current="Profile" />);
    expect(screen.getByText("Home")).toBeInTheDocument();
    expect(screen.getByText("Profile")).toBeInTheDocument();
    expect(screen.getByText("/")).toBeInTheDocument();
  });

  it("renders items prop directly", () => {
    renderPageHeader(
      <PageHeader items={[{ title: "A" }, { title: "B" }, { title: "C" }]} />,
    );
    expect(screen.getByText("A")).toBeInTheDocument();
    expect(screen.getByText("B")).toBeInTheDocument();
    expect(screen.getByText("C")).toBeInTheDocument();
  });

  it("renders center slot", () => {
    renderPageHeader(<PageHeader center={<span>Center Content</span>} />);
    expect(screen.getByText("Center Content")).toBeInTheDocument();
  });

  it("renders extra slot", () => {
    renderPageHeader(<PageHeader extra={<button>Action</button>} />);
    expect(screen.getByRole("button", { name: "Action" })).toBeInTheDocument();
  });

  it("renders afterBreadcrumb slot", () => {
    renderPageHeader(
      <PageHeader current="Page" afterBreadcrumb={<span>tag</span>} />,
    );
    expect(screen.getByText("tag")).toBeInTheDocument();
  });

  it("renders subRow slot", () => {
    renderPageHeader(<PageHeader subRow={<div>Sub Row Content</div>} />);
    expect(screen.getByText("Sub Row Content")).toBeInTheDocument();
  });

  it("renders description", () => {
    renderPageHeader(<PageHeader description="Page description" />);
    expect(screen.getByText("Page description")).toBeInTheDocument();
  });

  it("applies className prop", () => {
    const { container } = renderPageHeader(
      <PageHeader className="custom-class" />,
    );
    expect(container.firstChild).toHaveClass("custom-class");
  });

  it("renders nothing for empty items array", () => {
    const { container } = renderPageHeader(<PageHeader items={[]} />);
    expect(container.querySelector(".breadcrumbSeparator")).toBeNull();
  });

  it("skips empty parent/current", () => {
    renderPageHeader(<PageHeader parent="" current="" />);
    expect(screen.queryByText("/")).not.toBeInTheDocument();
  });

  it("shows settings back button on permissions page", () => {
    renderPageHeader(<PageHeader parent="设置" current="权限" />, {
      initialEntries: ["/permissions"],
    });
    expect(
      screen.getByRole("button", { name: "返回设置中心" }),
    ).toBeInTheDocument();
  });

  it("keeps the top-level memory page out of settings navigation", () => {
    renderPageHeader(<PageHeader parent="工作区" current="记忆" />, {
      initialEntries: ["/memory"],
    });
    expect(
      screen.queryByRole("button", { name: "返回设置中心" }),
    ).not.toBeInTheDocument();
  });
});
