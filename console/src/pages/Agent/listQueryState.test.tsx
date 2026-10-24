import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAgentStore } from "../../stores/agentStore";
import { useMCP } from "./MCP/useMCP";
import { useTools } from "./Tools/useTools";

const {
  listTools,
  listMCPClients,
  setToolEnabled,
  updateMCPClient,
  message,
  translate,
} = vi.hoisted(() => ({
  listTools: vi.fn(),
  listMCPClients: vi.fn(),
  setToolEnabled: vi.fn(),
  updateMCPClient: vi.fn(),
  message: {
    error: vi.fn(),
    success: vi.fn(),
    info: vi.fn(),
  },
  translate: (key: string) => key,
}));

vi.mock("../../api", () => ({
  default: {
    listTools,
    listMCPClients,
    setToolEnabled,
    updateMCPClient,
  },
}));

vi.mock("../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: translate }),
}));

describe("capability list query state", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    useAgentStore.setState({
      selectedAgent: "default",
      agents: [],
      lastChatIdByAgent: {},
    });
    listTools.mockReset();
    listMCPClients.mockReset();
    setToolEnabled.mockReset();
    updateMCPClient.mockReset();
    message.error.mockReset();
    message.success.mockReset();
    message.info.mockReset();
  });

  it("keeps a Tools load failure distinct from an empty list and preserves stale data", async () => {
    const savedTool = {
      name: "saved-tool",
      display_name: "已加载工具",
      enabled: false,
      locked: false,
      async_execution: false,
    };
    listTools
      .mockRejectedValueOnce(new Error("工具服务暂时不可用"))
      .mockResolvedValueOnce([savedTool])
      .mockRejectedValueOnce(new Error("刷新失败"));

    const { result } = renderHook(() => useTools());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.loadError).toBe("工具服务暂时不可用");
    expect(result.current.tools).toEqual([]);

    await act(async () => {
      await result.current.loadTools();
    });
    expect(result.current.loadError).toBe("");
    expect(result.current.tools).toEqual([savedTool]);

    await act(async () => {
      await result.current.loadTools();
    });
    expect(result.current.loadError).toBe("刷新失败");
    expect(result.current.tools).toEqual([savedTool]);
  });

  it("keeps an MCP load failure distinct from an empty client list", async () => {
    listMCPClients.mockRejectedValueOnce(new Error("MCP 列表读取失败"));

    const { result } = renderHook(() => useMCP());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.loadError).toBe("MCP 列表读取失败");
    expect(result.current.clients).toEqual([]);
  });

  it("sends an explicit tool state with its revision and keeps per-item batch results", async () => {
    const alpha = {
      name: "alpha",
      display_name: "Alpha",
      revision: 3,
      enabled: false,
      locked: false,
      async_execution: false,
    };
    const beta = {
      name: "beta",
      display_name: "Beta",
      revision: 8,
      enabled: false,
      locked: false,
      async_execution: false,
    };
    listTools.mockResolvedValueOnce([alpha, beta]);
    setToolEnabled
      .mockResolvedValueOnce({ ...alpha, enabled: true, revision: 4 })
      .mockRejectedValueOnce(new Error("conflict"));

    const { result } = renderHook(() => useTools());
    await waitFor(() => expect(result.current.tools).toHaveLength(2));

    await act(async () => {
      await result.current.setEnabledForTools(result.current.tools, true);
    });

    expect(setToolEnabled).toHaveBeenNthCalledWith(1, "alpha", true, 3);
    expect(setToolEnabled).toHaveBeenNthCalledWith(2, "beta", true, 8);
    expect(result.current.tools).toEqual([
      { ...alpha, enabled: true, revision: 4 },
      beta,
    ]);
    expect(message.error).toHaveBeenCalledWith("已更新 1 项，1 项未完成：Beta");
  });

  it("keeps an MCP client pending until an explicit versioned state write completes", async () => {
    const client = {
      key: "demo",
      revision: 7,
      name: "Demo",
      description: "",
      enabled: false,
      transport: "stdio" as const,
      url: "",
      headers: {},
      command: "demo",
      args: [],
      env: {},
      cwd: "",
      oauth: {},
      allowed_tools: [],
    };
    let resolveUpdate!: (value: typeof client) => void;
    listMCPClients.mockResolvedValueOnce([client]);
    updateMCPClient.mockImplementationOnce(
      () =>
        new Promise<typeof client>((resolve) => {
          resolveUpdate = resolve;
        }),
    );

    const { result } = renderHook(() => useMCP());
    await waitFor(() => expect(result.current.clients).toHaveLength(1));

    let updatePromise!: Promise<void>;
    act(() => {
      updatePromise = result.current.toggleEnabled(client, true);
    });
    await waitFor(() =>
      expect(result.current.pendingClientKeys.has("demo")).toBe(true),
    );
    expect(updateMCPClient).toHaveBeenCalledWith("demo", {
      enabled: true,
      expected_revision: 7,
    });
    expect(result.current.clients[0].enabled).toBe(false);

    await act(async () => {
      resolveUpdate({ ...client, enabled: true, revision: 8 });
      await updatePromise;
    });

    expect(result.current.pendingClientKeys.has("demo")).toBe(false);
    expect(result.current.clients[0]).toMatchObject({
      enabled: true,
      revision: 8,
    });
  });
});
