import { beforeEach, describe, expect, it } from "vitest";

import {
  beginPluginRegistration,
  endPluginRegistration,
  installHostExternals,
  pluginSystem,
} from "./hostExternals";

describe("controlled extension host", () => {
  beforeEach(() => {
    pluginSystem.clear();
    installHostExternals();
  });

  it("binds declared UI permissions to one plugin and removes registrations", () => {
    beginPluginRegistration("route-plugin", ["ui:route"]);
    window.Lantai.extension?.registerRoutes([
      {
        path: "/plugin/route-plugin",
        label: "扩展页",
        component: () => null,
      },
    ]);
    endPluginRegistration("route-plugin");

    expect(pluginSystem.getRoutes()).toHaveLength(1);
    pluginSystem.remove("route-plugin");
    expect(pluginSystem.getRoutes()).toEqual([]);
  });

  it("denies undeclared capabilities and exposes no credential helper", () => {
    beginPluginRegistration("route-only", ["ui:route"]);

    expect(() =>
      window.Lantai.extension?.registerToolRender({
        forbidden_tool: () => null,
      }),
    ).toThrow(/ui:tool-renderer/);
    expect("getApiToken" in window.Lantai.host).toBe(false);
    expect("modules" in window.Lantai).toBe(false);

    endPluginRegistration("route-only");
  });

  it("allows only declared API methods on whitelisted host paths", async () => {
    beginPluginRegistration("reader", ["api:read"]);

    await expect(
      window.Lantai.extension?.fetchApi("/auth/session"),
    ).rejects.toThrow(/not allowed/);
    await expect(
      window.Lantai.extension?.fetchApi("/work-methods", { method: "POST" }),
    ).rejects.toThrow(/api:write/);

    endPluginRegistration("reader");
  });
});
