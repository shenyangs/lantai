import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import LantaiResponseReasoning from "./lantaiChatResponseReasoning";

describe("reasoning disclosure", () => {
  it("keeps completed reasoning collapsed without losing the full text", () => {
    const ending = "这是必须能够查看的最后一段。";
    const source = `${"重复但仍属于完整记录。".repeat(120)}${ending}`;
    const { container } = render(
      React.createElement(LantaiResponseReasoning, {
        data: { status: "completed", content: source },
      }),
    );
    const disclosure = container.querySelector("details");

    expect(disclosure).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("思考"));

    expect(disclosure).toHaveAttribute("open");
    expect(disclosure).toHaveTextContent(ending);
    expect(disclosure).not.toHaveTextContent("已自动压缩");
  });

  it("keeps live provider reasoning private until the user expands it", () => {
    const { container, rerender } = render(
      React.createElement(LantaiResponseReasoning, {
        data: { status: "in_progress", content: "正在检查第一步" },
      }),
    );
    const disclosure = container.querySelector("details");
    const summary = container.querySelector("summary");

    expect(disclosure).not.toHaveAttribute("open");
    expect(disclosure).toHaveAttribute("data-view-state", "live-collapsed");
    expect(summary).toHaveTextContent("正在思考");
    expect(summary).toHaveTextContent("展开");

    fireEvent.click(summary!);

    expect(disclosure).toHaveAttribute("open");
    expect(disclosure).toHaveAttribute("data-view-state", "expanded");
    expect(summary).toHaveTextContent("收起");

    rerender(
      React.createElement(LantaiResponseReasoning, {
        data: { status: "completed", content: "正在检查第一步，已经完成" },
      }),
    );

    expect(disclosure).not.toHaveAttribute("open");
    expect(disclosure).toHaveAttribute(
      "data-view-state",
      "completed-collapsed",
    );
    expect(summary).toHaveTextContent("展开");
  });

  it("keeps updated live reasoning available after explicit expansion", () => {
    const { container, rerender } = render(
      React.createElement(LantaiResponseReasoning, {
        data: { status: "in_progress", content: "第一步" },
      }),
    );
    const disclosure = container.querySelector("details");
    expect(disclosure).not.toHaveAttribute("open");

    rerender(
      React.createElement(LantaiResponseReasoning, {
        data: { status: "in_progress", content: "第一步\n第二步" },
      }),
    );
    expect(disclosure).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("思考"));
    expect(disclosure).toHaveAttribute("open");
    expect(
      container.querySelector(".lantai-reasoning-content")?.textContent,
    ).toBe("第一步\n第二步");
  });

  it("restores the controlled state when Chromium mutates native open", () => {
    const { container } = render(
      React.createElement(LantaiResponseReasoning, {
        data: { status: "in_progress", content: "仍应默认折叠" },
      }),
    );
    const disclosure = container.querySelector("details")!;

    disclosure.open = true;
    fireEvent(disclosure, new Event("toggle"));

    expect(disclosure).not.toHaveAttribute("open");
    expect(disclosure).toHaveAttribute("data-view-state", "live-collapsed");
    expect(disclosure.querySelector("summary")).toHaveTextContent("展开");
  });
});

describe("reasoning fidelity", () => {
  it("renders runtime reasoning verbatim without redaction or replacement", () => {
    const source =
      "先调用 read_skill，读取 skills/puer/SKILL.md，并核对 skill_name。";
    const { container } = render(
      React.createElement(LantaiResponseReasoning, {
        data: {
          status: "completed",
          content: source,
          metadata: { skill_exposure: "subagent_internal" },
        },
      }),
    );

    fireEvent.click(screen.getByText("思考"));
    expect(
      container.querySelector(".lantai-reasoning-content"),
    ).toHaveTextContent(source);
  });
});
