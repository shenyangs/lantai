import { describe, expect, it, vi } from "vitest";
import { agentsApi } from "./agents";

vi.mock("../request", () => ({ request: vi.fn() }));

import { request } from "../request";

describe("agentsApi", () => {
  it("commits an Electron-staged workspace through the recovery endpoint", async () => {
    vi.mocked(request).mockResolvedValue({ success: true });

    await agentsApi.restoreWorkspace("default", {
      expected_source_dir: "/external/default",
      staged_workspace_dir: "/managed/workspace-imports/default-test",
    });

    expect(request).toHaveBeenCalledWith(
      "/agents/default/restore-workspace",
      {
        method: "POST",
        body: JSON.stringify({
          expected_source_dir: "/external/default",
          staged_workspace_dir: "/managed/workspace-imports/default-test",
        }),
      },
    );
  });

  it("deletes agents through the agent endpoint and accepts lifecycle reports", async () => {
    vi.mocked(request).mockResolvedValue({
      success: true,
      agent_id: "research",
      workspace_lifecycle: {
        agent_id: "research",
        workspace_dir: "/tmp/research",
        workspace_exists: true,
        workspace_deleted: false,
        artifact_archive: { kind: "artifact_lifecycle_cascade" },
        artifact_delete_dry_run: { dry_run: true },
        runtime_archives: [],
      },
    });

    const result = await agentsApi.deleteAgent("research");

    expect(request).toHaveBeenCalledWith("/agents/research", {
      method: "DELETE",
    });
    expect(result.workspace_lifecycle?.workspace_deleted).toBe(false);
  });
});
