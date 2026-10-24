import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/common_setup";
import type { AgentSummary } from "@/api/types/agents";
import BackupScopeForm, { type ScopeFormValue } from "./BackupScopeForm";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("./AgentMultiSelect", () => ({
  default: () => null,
}));

describe("BackupScopeForm", () => {
  it("expands an earlier subset to every agent when full backup is selected", () => {
    const onChange = vi.fn();
    const value: ScopeFormValue = {
      backupMode: "partial",
      selectedAgents: ["default"],
      globalConfig: false,
      includeSkillPool: false,
      includeSecrets: false,
    };
    const agents = [{ id: "default" }, { id: "research" }] as AgentSummary[];

    renderWithProviders(
      <BackupScopeForm value={value} onChange={onChange} agents={agents} />,
    );
    fireEvent.click(screen.getByRole("radio", { name: /backup\.fullBackup/ }));

    expect(onChange).toHaveBeenCalledWith({
      backupMode: "full",
      selectedAgents: ["default", "research"],
      globalConfig: true,
      includeSkillPool: true,
      includeSecrets: true,
    });
  });
});
