import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PoolSkillSpec } from "../../../../api/types";
import { PoolSkillCard } from "./PoolSkillCard";
import { PoolSkillListItem } from "./PoolSkillListItem";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) =>
      ({
        "skills.lastUpdated": "更新时间",
        "skills.tags": "标签",
        "skills.skillDescription": "描述",
      })[key] ?? key,
  }),
}));

vi.mock("@/vendor/lantaiDesign", () => ({
  Button: ({
    children,
    danger: _danger,
    ...props
  }: ButtonHTMLAttributes<HTMLButtonElement> & {
    children?: ReactNode;
    danger?: boolean;
  }) => <button {...props}>{children}</button>,
  Card: ({
    children,
    hoverable: _hoverable,
    ...props
  }: HTMLAttributes<HTMLDivElement> & {
    children?: ReactNode;
    hoverable?: boolean;
  }) => <div {...props}>{children}</div>,
  Checkbox: ({
    checked,
    ...props
  }: ButtonHTMLAttributes<HTMLInputElement> & { checked?: boolean }) => (
    <input type="checkbox" checked={checked} readOnly {...props} />
  ),
  Tooltip: ({ children }: { children?: ReactNode }) => <>{children}</>,
}));

vi.mock("../../../Agent/Skills/components", () => ({
  getSkillVisual: () => null,
}));

const skill: PoolSkillSpec = {
  name: "agent-designer",
  description: "设计和创建智能体",
  content: "# Agent Designer",
  source: "builtin",
  protected: true,
  sync_status: "synced",
  tags: ["智能体"],
  last_updated: "2026-08-22T00:00:00Z",
};

const handlers = {
  onToggleSelect: vi.fn(),
  onEdit: vi.fn(),
  onBroadcast: vi.fn(),
  onDelete: vi.fn(),
};

describe("skill pool presentation", () => {
  it("does not show filesystem update time on cards", () => {
    render(
      <PoolSkillCard
        skill={skill}
        isSelected={false}
        batchModeEnabled={false}
        {...handlers}
      />,
    );

    expect(screen.queryByText(/更新时间/)).not.toBeInTheDocument();
    expect(screen.getByText("agent-designer")).toBeInTheDocument();
  });

  it("does not show filesystem update time in list view", () => {
    render(
      <PoolSkillListItem
        skill={skill}
        isSelected={false}
        batchModeEnabled={false}
        {...handlers}
      />,
    );

    expect(screen.queryByText(/更新时间/)).not.toBeInTheDocument();
    expect(screen.getByText("agent-designer")).toBeInTheDocument();
  });
});
