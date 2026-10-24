import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../../../test/common_setup";
import SecurityPage from "./index";

const { mockNavigate, mockSave, mockReset } = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
  mockSave: vi.fn(),
  mockReset: vi.fn(),
}));

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock("./useSecurityPage", () => ({
  useSecurityPage: () => ({
    activeTab: "toolGuard",
    setActiveTab: vi.fn(),
    form: undefined,
    config: { permission_mode: "quiet", enabled: true },
    enabled: true,
    setEnabled: vi.fn(),
    toolOptions: [],
    saving: false,
    handleSave: mockSave,
    handleReset: mockReset,
    mergedRules: [],
    builtinRules: [],
    customRules: [],
    commandRules: [],
    setCommandRules: vi.fn(),
    showFullAutoMode: false,
    rememberedApprovals: [],
    rememberedApprovalsLoading: false,
    handleRefreshRememberedApprovals: vi.fn(),
    handleDeleteRememberedApproval: vi.fn(),
    handleClearRememberedApprovals: vi.fn(),
    toggleRule: vi.fn(),
    toggleAutoDeny: vi.fn(),
    deleteCustomRule: vi.fn(),
    openAddRule: vi.fn(),
    openEditRule: vi.fn(),
    shellEvasionChecks: {},
    toggleShellEvasionCheck: vi.fn(),
    editModal: false,
    setEditModal: vi.fn(),
    editingRule: null,
    editForm: {},
    handleEditSave: vi.fn(),
    previewRule: null,
    setPreviewRule: vi.fn(),
    fileGuardHandlers: null,
    onFileGuardHandlersReady: vi.fn(),
    allowNoAuthHostsHandlers: null,
    onAllowNoAuthHostsHandlersReady: vi.fn(),
    loading: false,
    error: null,
    fetchAll: vi.fn(),
  }),
}));

vi.mock("./components", () => ({
  ToolGuardTab: ({ onOpenPermissions }: { onOpenPermissions: () => void }) => (
    <section>
      <p>权限模式由统一权限页管理</p>
      <button onClick={onOpenPermissions}>打开权限设置</button>
    </section>
  ),
  RuleModal: () => null,
  PreviewModal: () => null,
  SkillScannerSection: () => null,
  FileGuardSection: () => null,
  AllowNoAuthHostsTab: () => null,
}));

describe("SecurityPage", () => {
  it("routes permission mode to its owner while keeping rule persistence here", () => {
    renderWithProviders(<SecurityPage />, { initialEntries: ["/security"] });

    expect(screen.getByText("权限模式由统一权限页管理")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "打开权限设置" }));
    expect(mockNavigate).toHaveBeenCalledWith("/permissions");
    expect(screen.queryByRole("button", { name: "保存" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "保存检测规则" }));
    expect(mockSave).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "取消规则修改" }));
    expect(mockReset).toHaveBeenCalledTimes(1);
  });
});
