import { describe, expect, it } from "vitest";

import {
  normalizeImportedClientData,
  normalizeImportedClients,
} from "./mcpImport";

describe("interactive MCP import normalization", () => {
  it.each([
    ["enabled", { command: "node", enabled: true }],
    ["isActive", { command: "node", isActive: true }],
  ])("quarantines imported clients that request %s", (_label, input) => {
    expect(normalizeImportedClientData("unsafe-import", input)).toMatchObject({
      name: "unsafe-import",
      enabled: false,
    });
  });

  it.each([
    [
      "mcpServers wrapper",
      { mcpServers: { wrapped: { command: "node", enabled: true } } },
      "wrapped",
    ],
    [
      "keyed clients",
      { keyed: { command: "node", isActive: true } },
      "keyed",
    ],
    [
      "direct client",
      { key: "direct", command: "node", enabled: true, isActive: true },
      "direct",
    ],
  ])("quarantines activation in the %s format", (_label, input, key) => {
    const imported = normalizeImportedClients(input);

    expect(imported).toHaveLength(1);
    expect(imported[0]).toMatchObject({ key, data: { enabled: false } });
    expect(imported[0].data).not.toHaveProperty("isActive");
  });
});
