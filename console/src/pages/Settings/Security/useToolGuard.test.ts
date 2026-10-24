import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ToolGuardConfig } from "../../../api/modules/security";
import { useToolGuard } from "./useToolGuard";

const { getBuiltinRules, getToolGuard, listPermissionProfiles } = vi.hoisted(
  () => ({
    getBuiltinRules: vi.fn(),
    getToolGuard: vi.fn(),
    listPermissionProfiles: vi.fn(),
  }),
);

vi.mock("../../../api", () => ({
  default: {
    getBuiltinRules,
    getToolGuard,
    listPermissionProfiles,
  },
}));

const baseConfig: ToolGuardConfig = {
  enabled: true,
  permission_mode: "quiet",
  default_workspace_dir: "",
  guarded_tools: null,
  denied_tools: [],
  custom_rules: [],
  command_rules: [],
  disabled_rules: [],
  auto_denied_rules: [],
  remembered_approvals: [],
  shell_evasion_checks: {},
};

describe("useToolGuard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getToolGuard.mockResolvedValue(baseConfig);
    getBuiltinRules.mockResolvedValue([]);
    listPermissionProfiles.mockResolvedValue([{ id: "full" }]);
  });

  it("applies the saved backend mode to the visible status state", async () => {
    const { result } = renderHook(() => useToolGuard());
    await waitFor(() => expect(result.current.loading).toBe(false));

    act(() => {
      result.current.applyConfig({
        ...baseConfig,
        permission_mode: "full_auto",
      });
    });

    expect(result.current.config?.permission_mode).toBe("full_auto");
    expect(result.current.buildSaveBody().permission_mode).toBe("full_auto");
  });
});
