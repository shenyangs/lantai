import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Modal } from "antd";
import { ApprovalCard } from "./ApprovalCard";

const baseProps = {
  requestId: "approval-1",
  toolName: "execute_shell_command",
  severity: "high",
  findingsCount: 1,
  findingsSummary: "安装依赖需要确认",
  toolParams: { command: "npm install lodash" },
  createdAt: Date.now() / 1000,
  timeoutSeconds: 300,
  rootSessionId: "root-session",
  onApprove: vi.fn(),
  onDeny: vi.fn(),
};

describe("ApprovalCard", () => {
  afterEach(() => {
    Modal.destroyAll();
  });
  it("shows an open-ended waiting state when approval has no timeout", () => {
    render(<ApprovalCard {...baseProps} timeoutSeconds={null} />);

    expect(screen.getByText("等待你的决定")).toBeInTheDocument();
    expect(screen.queryByLabelText(/剩余 .* 秒/)).toBeNull();
    expect(screen.getByRole("button", { name: /允许一次/ })).not.toBeDisabled();
  });

  it("explains and hides remembered scopes when policy is not rememberable", () => {
    render(
      <ApprovalCard
        {...baseProps}
        rememberable={false}
        sessionRememberable={false}
        policyDecision={{ workspace_escape: true }}
        approvalOptions={["allow_once", "deny", "view_rules"]}
      />,
    );

    expect(screen.queryByRole("button", { name: /本次会话允许/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /始终允许/ })).toBeNull();
    expect(screen.getByText(/只能逐次确认/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /允许一次/ })).not.toBeDisabled();
  });

  it("sends the selected approval scope", async () => {
    const onApprove = vi.fn().mockResolvedValue(undefined);
    render(
      <ApprovalCard
        {...baseProps}
        rememberable
        sessionRememberable
        approvalOptions={[
          "allow_once",
          "allow_session",
          "allow_workspace",
          "deny",
        ]}
        onApprove={onApprove}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /始终允许/ }));
    fireEvent.click(
      await screen.findByRole("button", { name: "确认始终允许" }),
    );

    await waitFor(() =>
      expect(onApprove).toHaveBeenCalledWith("approval-1", "workspace"),
    );
  });

  it("offers current-session approval for unknown tools without persisting it", async () => {
    const onApprove = vi.fn().mockResolvedValue(undefined);
    render(
      <ApprovalCard
        {...baseProps}
        toolName="generate_image"
        rememberable={false}
        sessionRememberable
        approvalOptions={["allow_once", "allow_session", "deny"]}
        onApprove={onApprove}
      />,
    );

    expect(screen.queryByRole("button", { name: /始终允许/ })).toBeNull();
    expect(
      screen.getByRole("button", { name: /本次会话允许/ }),
    ).not.toBeDisabled();
    expect(screen.getByText(/不会永久保存/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /本次会话允许/ }));

    await waitFor(() =>
      expect(onApprove).toHaveBeenCalledWith("approval-1", "session"),
    );
  });

  it("shows sub-agent source when the approval comes from a child task", () => {
    render(
      <ApprovalCard
        {...baseProps}
        sourceAgentName="Lantai_Water_Agent"
        sourceRunId="run-child-123456"
      />,
    );

    expect(screen.getByText(/Lantai_Water_Agent/)).toBeInTheDocument();
    expect(screen.getByText(/run-chil/)).toBeInTheDocument();
  });

  it("shows permission mode and command policy details", () => {
    render(
      <ApprovalCard
        {...baseProps}
        policyDecision={{
          permission_mode: "quiet",
          command_category: "shell.install_dependency",
          command_head: "npm",
          decision_source: "builtin_prompt",
          effective_rule_id: "shell.install_dependency",
        }}
      />,
    );

    expect(screen.getByText(/模式 quiet/)).toBeInTheDocument();
    expect(screen.getByText(/命令 npm/)).toBeInTheDocument();
  });

  it("routes approval shortcuts only to the active card", async () => {
    const inactiveApprove = vi.fn().mockResolvedValue(undefined);
    const activeApprove = vi.fn().mockResolvedValue(undefined);
    const activeDeny = vi.fn().mockResolvedValue(undefined);
    render(
      <>
        <ApprovalCard
          {...baseProps}
          requestId="approval-inactive"
          rememberable
          sessionRememberable
          approvalOptions={["allow_once", "allow_session", "deny"]}
          onApprove={inactiveApprove}
        />
        <ApprovalCard
          {...baseProps}
          requestId="approval-active"
          rememberable
          sessionRememberable
          approvalOptions={["allow_once", "allow_session", "deny"]}
          keyboardActive
          onApprove={activeApprove}
          onDeny={activeDeny}
        />
      </>,
    );

    fireEvent.keyDown(window, { key: "Enter" });
    await waitFor(() =>
      expect(activeApprove).toHaveBeenCalledWith("approval-active", "once"),
    );
    expect(inactiveApprove).not.toHaveBeenCalled();

    activeApprove.mockClear();
    fireEvent.keyDown(window, { key: "Enter", metaKey: true });
    await waitFor(() =>
      expect(activeApprove).toHaveBeenCalledWith("approval-active", "session"),
    );

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(activeDeny).toHaveBeenCalledWith("approval-active"),
    );
  });

  it("does not intercept approval shortcuts while editing text", () => {
    const onApprove = vi.fn().mockResolvedValue(undefined);
    render(
      <>
        <textarea aria-label="draft" />
        <ApprovalCard {...baseProps} keyboardActive onApprove={onApprove} />
      </>,
    );

    const textarea = screen.getByRole("textbox", { name: "draft" });
    textarea.focus();
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(onApprove).not.toHaveBeenCalled();
  });

  it("blocks both pointer and keyboard decisions after the request expires", async () => {
    const onApprove = vi.fn().mockResolvedValue(undefined);
    const onDeny = vi.fn().mockResolvedValue(undefined);
    render(
      <ApprovalCard
        {...baseProps}
        createdAt={Date.now() / 1000 - 10}
        timeoutSeconds={1}
        keyboardActive
        onApprove={onApprove}
        onDeny={onDeny}
      />,
    );

    await waitFor(() =>
      expect(screen.getByLabelText("剩余 0 秒")).toBeInTheDocument(),
    );
    const approveButton = screen.getByRole("button", { name: /允许一次/ });
    const denyButton = screen.getByRole("button", { name: /Deny/ });
    expect(approveButton).toBeDisabled();
    expect(denyButton).toBeDisabled();

    fireEvent.click(approveButton);
    fireEvent.click(denyButton);
    fireEvent.keyDown(window, { key: "Enter" });
    fireEvent.keyDown(window, { key: "Escape" });

    expect(onApprove).not.toHaveBeenCalled();
    expect(onDeny).not.toHaveBeenCalled();
  });

  it("uses the authoritative policy risk for both label and color", () => {
    render(
      <ApprovalCard
        {...baseProps}
        severity="low"
        policyDecision={{ risk_level: "high" }}
      />,
    );

    const riskTag = screen.getByText("高风险").closest(".ant-tag");
    expect(riskTag).toHaveClass("ant-tag-error");
    expect(riskTag).toHaveAttribute("data-severity", "high");
  });

  it("does not claim a decision was unapplied when the response is unknown", async () => {
    const onApprove = vi.fn().mockRejectedValue(new TypeError("fetch failed"));
    render(<ApprovalCard {...baseProps} onApprove={onApprove} />);

    fireEvent.click(screen.getByRole("button", { name: /允许一次/ }));

    expect(
      await screen.findByText(/结果尚未确认.*不要重复提交/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/授权未生效/)).toBeNull();
  });

  it("keeps a pending approval visibly loading and prevents a repeat decision", async () => {
    const onApprove = vi.fn(() => new Promise<void>(() => {}));
    render(<ApprovalCard {...baseProps} onApprove={onApprove} />);

    const approveButton = screen.getByRole("button", { name: /允许一次/ });
    fireEvent.click(approveButton);

    await waitFor(() => expect(approveButton).toHaveClass("ant-btn-loading"));
    expect(approveButton).toBeDisabled();
    fireEvent.click(approveButton);
    expect(onApprove).toHaveBeenCalledTimes(1);
  });

  it("keeps cancelling the whole task separate, discoverable, and confirmed", async () => {
    const onCancel = vi.fn();
    render(<ApprovalCard {...baseProps} onCancel={onCancel} />);

    fireEvent.click(screen.getByRole("button", { name: "取消整个任务" }));
    expect(onCancel).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "取消任务" }));
    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1));
  });
});
