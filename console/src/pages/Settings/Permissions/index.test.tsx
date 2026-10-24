import { Form } from "antd";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import PermissionsPage from "./index";

const guardState = vi.hoisted(() => ({ enabled: true }));

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );
  return { ...actual, useNavigate: () => vi.fn() };
});

vi.mock("../Security/useSecurityPage", () => ({
  useSecurityPage: () => {
    const [form] = Form.useForm();
    return {
      form,
      config: {
        enabled: guardState.enabled,
        permission_mode: "quiet",
        default_workspace_dir: "",
        guarded_tools: [],
        denied_tools: [],
      },
      enabled: guardState.enabled,
      setEnabled: vi.fn(),
      toolOptions: [],
      saving: false,
      handleSave: vi.fn(),
      handleReset: vi.fn(),
      commandRules: [],
      setCommandRules: vi.fn(),
      showFullAutoMode: true,
      rememberedApprovals: [],
      rememberedApprovalsLoading: false,
      handleRefreshRememberedApprovals: vi.fn(),
      handleDeleteRememberedApproval: vi.fn(),
      handleClearRememberedApprovals: vi.fn(),
      loading: false,
      error: null,
      fetchAll: vi.fn(),
    };
  },
}));

describe("PermissionsPage comprehensibility", () => {
  afterEach(() => {
    guardState.enabled = true;
  });

  it("separates global defaults, chat overrides and hard limits", () => {
    render(
      <MemoryRouter>
        <PermissionsPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("后台与未指定场景的默认权限")).toBeInTheDocument();
    expect(
      screen.getByText(/聊天输入框里的权限由每个对话单独保存/),
    ).toBeInTheDocument();
    expect(screen.getByText(/系统硬性限制始终有效/)).toBeInTheDocument();
    expect(screen.getByText("附加风险扫描")).toBeInTheDocument();
    expect(
      screen.getByText(/关闭后仍保留系统硬性限制和当前执行模式/),
    ).toBeInTheDocument();
    expect(screen.getByText("备用工作文件夹")).toBeInTheDocument();
    expect(
      screen.getByText(/不会改变已经绑定工作区的对话/),
    ).toBeInTheDocument();
  });

  it("keeps effective defaults and command rules editable when only scanning is off", () => {
    guardState.enabled = false;
    render(
      <MemoryRouter>
        <PermissionsPage />
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("radio", { name: /安全操作自动完成/ }),
    ).toBeEnabled();
    expect(
      screen.getByRole("textbox", { name: "备用工作文件夹" }),
    ).toBeEnabled();
    expect(screen.getByText("附加风险扫描已关闭")).toBeInTheDocument();
  });
});
