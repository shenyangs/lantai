import { describe, it, expect, vi, beforeEach } from "vitest";
import { buildAuthHeaders, encodeHeaderValue } from "./authHeaders";

vi.mock("./config", () => ({
  getApiToken: vi.fn(() => ""),
}));

import { getApiToken } from "./config";

describe("buildAuthHeaders", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    vi.mocked(getApiToken).mockReturnValue("");
  });

  it("does not include unicode workspace paths in HTTP headers", () => {
    sessionStorage.setItem(
      "lantai:active-topic-workspace:v1",
      JSON.stringify({
        effectiveWorkspaceDir:
          "/Users/example/Downloads/codex/lantai_1/项目主文件夹/lantai3",
      }),
    );

    const headers = buildAuthHeaders();

    expect(headers["X-Lantai-Workspace-Dir"]).toBeUndefined();
    expect(() => new Headers(headers)).not.toThrow();
  });

  it("sends only the opaque active workspace grant as project authority", () => {
    sessionStorage.setItem(
      "lantai-agent-storage",
      JSON.stringify({ state: { selectedAgent: "default" } }),
    );
    sessionStorage.setItem(
      "lantai:active-topic-workspace:v1",
      JSON.stringify({
        agentId: "default",
        effectiveWorkspaceDir: "/Users/example/项目",
        workspaceGrantId: "wsg_safe-token_123",
      }),
    );

    const headers = buildAuthHeaders();

    expect(headers["X-Workspace-Grant-Id"]).toBe("wsg_safe-token_123");
    expect(headers["X-Lantai-Workspace-Dir"]).toBeUndefined();
  });

  it("never sends a workspace grant owned by another selected agent", () => {
    sessionStorage.setItem(
      "lantai-agent-storage",
      JSON.stringify({ state: { selectedAgent: "agent-two" } }),
    );
    sessionStorage.setItem(
      "lantai:active-topic-workspace:v1",
      JSON.stringify({
        agentId: "agent-one",
        workspaceGrantId: "wsg_agent-one-only",
      }),
    );

    const headers = buildAuthHeaders();

    expect(headers["X-Agent-Id"]).toBe("agent-two");
    expect(headers["X-Workspace-Grant-Id"]).toBeUndefined();
  });

  it("encodes unicode agent IDs before they become header values", () => {
    sessionStorage.setItem(
      "lantai-agent-storage",
      JSON.stringify({ state: { selectedAgent: "沈行知" } }),
    );

    const headers = buildAuthHeaders();

    expect(headers["X-Agent-Id"]).toBe(encodeHeaderValue("沈行知"));
    expect(() => new Headers(headers)).not.toThrow();
  });

  it("keeps ascii authorization tokens intact", () => {
    vi.mocked(getApiToken).mockReturnValue("token-123");

    expect(buildAuthHeaders().Authorization).toBe("Bearer token-123");
  });
});
