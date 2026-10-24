import { Form } from "antd";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ToolGuardTab } from "./ToolGuardTab";

function TestSubject() {
  const [form] = Form.useForm();
  return (
    <ToolGuardTab
      form={form}
      config={{
        enabled: false,
        permission_mode: "quiet",
        default_workspace_dir: "",
        guarded_tools: [],
        denied_tools: [],
        custom_rules: [],
        disabled_rules: [],
        auto_denied_rules: [],
        remembered_approvals: [],
        shell_evasion_checks: {},
      }}
      enabled={false}
      setEnabled={vi.fn()}
      toolOptions={[]}
      commandRules={[]}
      setCommandRules={vi.fn()}
      showFullAutoMode
      mergedRules={[]}
      toggleRule={vi.fn()}
      toggleAutoDeny={vi.fn()}
      onPreviewRule={vi.fn()}
      onEditRule={vi.fn()}
      onDeleteRule={vi.fn()}
      openAddRule={vi.fn()}
      rememberedApprovals={[]}
      onRefreshRememberedApprovals={vi.fn()}
      onDeleteRememberedApproval={vi.fn()}
      onClearRememberedApprovals={vi.fn()}
      shellEvasionChecks={{}}
      toggleShellEvasionCheck={vi.fn()}
    />
  );
}

describe("ToolGuardTab", () => {
  it("keeps independent policy controls editable when additional scanning is off", () => {
    render(<TestSubject />);

    expect(screen.getByText("附加风险扫描")).toBeInTheDocument();
    expect(
      screen.getByRole("textbox", { name: "备用工作文件夹" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("radio", { name: /安全操作自动完成/ }),
    ).toBeEnabled();
    const commandRuleButton = screen.getByRole("button", {
      name: /添加规则$/,
    });
    expect(commandRuleButton).toBeEnabled();
    expect(
      screen.getByRole("button", { name: /security\.rules\.add$/ }),
    ).toBeDisabled();
  });
});
