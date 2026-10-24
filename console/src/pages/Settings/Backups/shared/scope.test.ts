import { describe, expect, it } from "vitest";
import { buildPreRestoreScope, buildScope, defaultCreateScope } from "./scope";

describe("backup scope presentation", () => {
  it("defaults to a sanitized export", () => {
    expect(defaultCreateScope(["a", "b"])).toEqual({
      backupMode: "partial",
      selectedAgents: ["a", "b"],
      globalConfig: true,
      includeSkillPool: true,
      includeSecrets: false,
    });
  });

  it("includes secrets only for an explicit disaster backup", () => {
    const full = buildScope("full", ["a"], true, true, false, ["a", "b"]);
    expect(full.scope).toEqual({
      include_agents: true,
      include_global_config: true,
      include_skill_pool: true,
      include_secrets: true,
      sanitized: false,
    });
    expect(full.agents).toEqual(["a", "b"]);

    const partial = buildScope("partial", ["a"], true, true, false, ["a", "b"]);
    expect(partial.scope).toEqual({
      include_agents: true,
      include_global_config: true,
      include_skill_pool: true,
      include_secrets: false,
      sanitized: true,
    });
    expect(partial.agents).toEqual(["a"]);
  });

  it("creates a complete local rollback backup before restore", () => {
    expect(buildPreRestoreScope(["a"]).scope).toEqual({
      include_agents: true,
      include_global_config: true,
      include_secrets: true,
      include_skill_pool: true,
      sanitized: false,
    });
  });
});
