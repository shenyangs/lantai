import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../../test/common_setup";
import type { RememberedApproval } from "../../../../api/modules/security";
import { RememberedApprovalsTable } from "./RememberedApprovalsTable";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) =>
      ({
        "security.rememberedApprovals.tool": "Tool",
        "security.rememberedApprovals.rules": "Rules",
        "security.rememberedApprovals.risk": "Risk",
        "security.rememberedApprovals.workspace": "Workspace",
        "security.rememberedApprovals.command": "Command",
        "security.rememberedApprovals.updatedAt": "Updated",
        "security.rememberedApprovals.actions": "Actions",
        "security.rememberedApprovals.delete": "Delete",
        "security.rememberedApprovals.refresh": "Refresh",
        "security.rememberedApprovals.clearAll": "Clear",
        "security.rememberedApprovals.empty": "No remembered approvals",
        "security.rememberedApprovals.deleteConfirm": "Delete approval?",
        "security.rememberedApprovals.clearConfirm": "Clear approvals?",
        "common.delete": "Delete",
        "common.cancel": "Cancel",
      })[key] ?? key,
  }),
}));

const approval: RememberedApproval = {
  id: "approval-1",
  enabled: true,
  scope: "workspace",
  workspace_dir: "/tmp/lantai/workspace",
  tool_name: "execute_shell_command",
  permission_profile: "workspace",
  matched_rule_ids: ["shell.install_dependency"],
  command_head: "npm",
  risk_level: "medium",
  approval_copy: "Install dependencies",
  policy_decision: {},
  source_agent_id: "default",
  created_at: "2026-05-01T00:00:00+00:00",
  updated_at: "2026-05-02T00:00:00+00:00",
};

describe("RememberedApprovalsTable", () => {
  it("renders remembered approval details", () => {
    const { container } = renderWithProviders(
      <RememberedApprovalsTable
        approvals={[approval]}
        enabled
        onRefresh={vi.fn()}
        onDelete={vi.fn()}
        onClearAll={vi.fn()}
      />,
    );

    expect(screen.getByText("execute_shell_command")).toBeInTheDocument();
    expect(screen.getByText("shell.install_dependency")).toBeInTheDocument();
    expect(screen.getByText("MEDIUM")).toBeInTheDocument();
    expect(screen.getByText("workspace")).toBeInTheDocument();
    expect(screen.getByText("npm")).toBeInTheDocument();

    const buttonIcons = Array.from(container.querySelectorAll("button svg"));
    expect(buttonIcons).not.toHaveLength(0);
    buttonIcons.forEach((icon) => {
      expect(icon).not.toHaveAttribute("width", "small");
      expect(icon).not.toHaveAttribute("height", "small");
    });
  });

  it("confirms before clearing all remembered approvals", async () => {
    const user = userEvent.setup();
    const onClearAll = vi.fn();
    renderWithProviders(
      <RememberedApprovalsTable
        approvals={[approval]}
        enabled
        onRefresh={vi.fn()}
        onDelete={vi.fn()}
        onClearAll={onClearAll}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Clear/ }));
    await user.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() => expect(onClearAll).toHaveBeenCalledTimes(1));
  });
});
