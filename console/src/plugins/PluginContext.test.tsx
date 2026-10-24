import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getState, loadAllPlugins } = vi.hoisted(() => ({
  getState: vi.fn(),
  loadAllPlugins: vi.fn(),
}));

vi.mock("../api/modules/customization", () => ({
  customizationApi: { getState },
}));

vi.mock("./usePluginLoader", () => ({ loadAllPlugins }));

import { PluginProvider, usePlugins } from "./PluginContext";
import { pluginSystem } from "./hostExternals";

function Probe() {
  const value = usePlugins();
  return <span>{value.loading ? "loading" : "ready"}</span>;
}

describe("PluginProvider recovery boundary", () => {
  beforeEach(() => {
    pluginSystem.clear();
    getState.mockReset();
    loadAllPlugins.mockReset();
    loadAllPlugins.mockResolvedValue({ loaded: 0, failed: [] });
  });

  it("does not execute frontend plugins in safe recovery mode", async () => {
    getState.mockResolvedValue({ mode: "safe" });

    render(<PluginProvider><Probe /></PluginProvider>);

    await screen.findByText("ready");
    expect(loadAllPlugins).not.toHaveBeenCalled();
  });

  it("loads plugins normally outside safe recovery mode", async () => {
    getState.mockResolvedValue({ mode: "normal" });

    render(<PluginProvider><Probe /></PluginProvider>);

    await waitFor(() => expect(loadAllPlugins).toHaveBeenCalledOnce());
  });
});
