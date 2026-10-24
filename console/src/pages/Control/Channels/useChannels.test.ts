import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAgentStore } from "../../../stores/agentStore";
import { useChannels } from "./useChannels";

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const { listChannels, listChannelTypes, listChannelDescriptors } = vi.hoisted(
  () => ({
    listChannels: vi.fn(),
    listChannelTypes: vi.fn(),
    listChannelDescriptors: vi.fn(),
  }),
);

vi.mock("../../../api", () => ({
  default: {
    listChannels,
    listChannelTypes,
    listChannelDescriptors,
  },
}));

describe("useChannels state ownership", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    useAgentStore.setState({
      selectedAgent: "default",
      agents: [],
      lastChatIdByAgent: {},
    });
    listChannels.mockReset();
    listChannelTypes.mockReset();
    listChannelDescriptors.mockReset();
    listChannelDescriptors.mockResolvedValue([]);
  });

  it("does not let the previous agent's late response overwrite the current agent", async () => {
    const firstChannels = deferred<Record<string, unknown>>();
    const firstTypes = deferred<string[]>();
    const secondChannels = deferred<Record<string, unknown>>();
    const secondTypes = deferred<string[]>();
    listChannels
      .mockReturnValueOnce(firstChannels.promise)
      .mockReturnValueOnce(secondChannels.promise);
    listChannelTypes
      .mockReturnValueOnce(firstTypes.promise)
      .mockReturnValueOnce(secondTypes.promise);

    const { result } = renderHook(() => useChannels());
    await waitFor(() => expect(listChannels).toHaveBeenCalledTimes(1));

    act(() => {
      useAgentStore.getState().setSelectedAgent("agent-two");
    });
    await waitFor(() => expect(listChannels).toHaveBeenCalledTimes(2));

    await act(async () => {
      secondChannels.resolve({ telegram: { enabled: true } });
      secondTypes.resolve(["telegram"]);
      await secondChannels.promise;
    });
    await waitFor(() =>
      expect(result.current.orderedKeys).toEqual(["telegram"]),
    );

    await act(async () => {
      firstChannels.resolve({ discord: { enabled: true } });
      firstTypes.resolve(["discord"]);
      await firstChannels.promise;
    });

    expect(result.current.orderedKeys).toEqual(["telegram"]);
    expect(result.current.channels).not.toHaveProperty("discord");
  });
});
