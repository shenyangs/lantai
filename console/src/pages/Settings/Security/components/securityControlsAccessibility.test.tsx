import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/common_setup";
import type { MergedRule } from "../useToolGuard";
import { RuleTable } from "./RuleTable";
import { ShellEvasionSection } from "./ShellEvasionSection";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) =>
      ({
        "common.enable": "启用",
        "common.disable": "禁用",
        "security.rules.enable": "启用",
        "security.rules.disable": "禁用",
        "security.rules.edit": "编辑",
        "security.rules.delete": "删除",
        "security.rules.preview": "预览",
        "security.rules.autoDenyEnable": "启用自动拒绝",
        "security.rules.autoDenyDisable": "关闭自动拒绝",
        "security.shellEvasion.checks.command_substitution.name": "命令替换",
      })[key] ?? key,
  }),
}));

vi.mock("@/contexts/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

const baseRule = {
  tools: [],
  params: [],
  category: "command_injection",
  severity: "HIGH",
  patterns: [],
  exclude_patterns: [],
  description: "fixture",
  remediation: "",
  disabled: false,
  autoDeny: false,
} satisfies Omit<MergedRule, "id" | "source">;

describe("Security control accessible names", () => {
  it("names every rule switch and icon button with its rule ID", () => {
    const rules: MergedRule[] = [
      { ...baseRule, id: "BUILTIN_RULE", source: "builtin" },
      { ...baseRule, id: "CUSTOM_RULE", source: "custom" },
    ];

    renderWithProviders(
      <RuleTable
        rules={rules}
        enabled
        onToggleRule={vi.fn()}
        onToggleAutoDeny={vi.fn()}
        onPreviewRule={vi.fn()}
        onEditRule={vi.fn()}
        onDeleteRule={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("switch", {
        name: "BUILTIN_RULE：启用自动拒绝",
      }),
    ).toBeVisible();
    expect(
      screen.getByRole("switch", { name: "BUILTIN_RULE：禁用" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "预览：BUILTIN_RULE" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "编辑：CUSTOM_RULE" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "删除：CUSTOM_RULE" }),
    ).toBeVisible();
  });

  it("names shell checks by the setting they change", () => {
    renderWithProviders(
      <ShellEvasionSection
        checks={{ command_substitution: true }}
        onToggle={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("switch", { name: "命令替换：禁用" }),
    ).toBeVisible();
  });
});
