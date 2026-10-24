import { beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { RuntimeEventsResponse } from "../../api/types/chat";

const { mockSubscribeChatEvents } = vi.hoisted(() => ({
  mockSubscribeChatEvents: vi.fn(),
}));

vi.mock("../../api/modules/chat", () => ({
  chatApi: {
    subscribeChatEvents: mockSubscribeChatEvents,
  },
}));

import {
  resetRuntimeEventSubscriptionRegistryForTests,
  subscribeRuntimeEventSession,
} from "./runtimeEventSubscriptionRegistry";

function response(seq: number): RuntimeEventsResponse {
  return {
    chat_id: "chat-1",
    stream_epoch: "epoch-a",
    cursor: `epoch-a:${seq}`,
    after_seq: seq - 1,
    latest_seq: seq,
    events: [],
  };
}

function p0Incident(incidentId: string) {
  const library = JSON.parse(
    readFileSync(
      resolve(
        process.cwd(),
        "../tests/fixtures/runtime/incidents/p0_incident_library_v1.json",
      ),
      "utf8",
    ),
  ) as {
    schema_version: number;
    incidents: Array<{
      incident_id: string;
      input: Record<string, string | number>;
      expected: Record<string, unknown>;
    }>;
  };
  expect(library.schema_version).toBe(1);
  return library.incidents.find((item) => item.incident_id === incidentId)!;
}

describe("runtimeEventSubscriptionRegistry", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    resetRuntimeEventSubscriptionRegistryForTests();
    mockSubscribeChatEvents.mockReturnValue(vi.fn());
  });

  it("shares one physical stream between subscribers of the same chat", () => {
    const first = vi.fn();
    const second = vi.fn();
    const releaseFirst = subscribeRuntimeEventSession("chat-1", first, {
      afterSeq: 3,
      streamEpoch: "epoch-a",
    });
    const releaseSecond = subscribeRuntimeEventSession("chat-1", second, {
      afterSeq: 3,
      streamEpoch: "epoch-a",
    });

    expect(mockSubscribeChatEvents).toHaveBeenCalledTimes(1);
    const onUpdate = mockSubscribeChatEvents.mock.calls[0][1];
    onUpdate(response(4));
    expect(first).toHaveBeenCalledOnce();
    expect(second).toHaveBeenCalledOnce();

    releaseFirst();
    onUpdate(response(5));
    expect(first).toHaveBeenCalledOnce();
    expect(second).toHaveBeenCalledTimes(2);
    releaseSecond();
  });

  it("keeps the transport alive across a React cleanup/remount window", () => {
    const close = vi.fn();
    mockSubscribeChatEvents.mockReturnValue(close);
    const release = subscribeRuntimeEventSession("chat-1", vi.fn(), {
      afterSeq: 0,
    });
    release();

    vi.advanceTimersByTime(100);
    const releaseRemount = subscribeRuntimeEventSession("chat-1", vi.fn(), {
      afterSeq: 0,
    });
    vi.advanceTimersByTime(500);

    expect(mockSubscribeChatEvents).toHaveBeenCalledTimes(1);
    expect(close).not.toHaveBeenCalled();
    releaseRemount();
    vi.advanceTimersByTime(500);
    expect(close).toHaveBeenCalledOnce();
  });

  it("resumes from the registry cursor after a route detour outlives the close grace", () => {
    const close = vi.fn();
    mockSubscribeChatEvents.mockReturnValue(close);
    const release = subscribeRuntimeEventSession("chat-1", vi.fn(), {
      afterSeq: 0,
    });
    const onUpdate = mockSubscribeChatEvents.mock.calls[0][1];
    onUpdate(response(37));

    release();
    vi.advanceTimersByTime(500);
    expect(close).toHaveBeenCalledOnce();

    const releaseRemount = subscribeRuntimeEventSession("chat-1", vi.fn(), {
      afterSeq: 0,
    });

    expect(mockSubscribeChatEvents).toHaveBeenCalledTimes(2);
    expect(mockSubscribeChatEvents.mock.calls[1][2]).toEqual(
      expect.objectContaining({
        afterSeq: 37,
        streamEpoch: "epoch-a",
      }),
    );
    releaseRemount();
  });

  it("commits the applied cursor before notifying isolated observers", () => {
    const applyToTurnStore = vi.fn(() => true);
    const observerError = new Error("broken presentation subscriber");
    const reportObserverError = vi.fn();
    const healthyObserver = vi.fn();
    const releaseApplier = subscribeRuntimeEventSession(
      "chat-1",
      applyToTurnStore,
      { afterSeq: 0, role: "state-applier" },
    );
    const releaseBrokenObserver = subscribeRuntimeEventSession(
      "chat-1",
      () => {
        throw observerError;
      },
      { role: "observer", onError: reportObserverError },
    );
    const releaseHealthyObserver = subscribeRuntimeEventSession(
      "chat-1",
      healthyObserver,
      { role: "observer" },
    );
    const onUpdate = mockSubscribeChatEvents.mock.calls[0][1];

    expect(() => onUpdate(response(9))).not.toThrow();
    expect(applyToTurnStore).toHaveBeenCalledBefore(healthyObserver);
    expect(reportObserverError).toHaveBeenCalledWith(observerError);
    expect(healthyObserver).toHaveBeenCalledWith(response(9));

    releaseApplier();
    releaseBrokenObserver();
    releaseHealthyObserver();
    vi.advanceTimersByTime(500);
    const releaseRemount = subscribeRuntimeEventSession("chat-1", vi.fn(), {
      afterSeq: 0,
    });
    expect(mockSubscribeChatEvents.mock.calls[1][2]).toEqual(
      expect.objectContaining({ afterSeq: 9, streamEpoch: "epoch-a" }),
    );
    releaseRemount();
  });

  it("does not checkpoint a response rejected by the central state applier", () => {
    const applyError = new Error("TurnStore commit failed");
    const release = subscribeRuntimeEventSession(
      "chat-1",
      () => {
        throw applyError;
      },
      {
        afterSeq: 3,
        streamEpoch: "epoch-a",
        role: "state-applier",
      },
    );
    const onUpdate = mockSubscribeChatEvents.mock.calls[0][1];

    expect(() => onUpdate(response(9))).toThrow(applyError);
    release();
    vi.advanceTimersByTime(500);

    const releaseRemount = subscribeRuntimeEventSession("chat-1", vi.fn(), {
      afterSeq: 3,
      streamEpoch: "epoch-a",
    });
    expect(mockSubscribeChatEvents.mock.calls[1][2]).toEqual(
      expect.objectContaining({ afterSeq: 3, streamEpoch: "epoch-a" }),
    );
    releaseRemount();
  });

  it("does not apply a checkpoint from a different stream epoch", () => {
    const release = subscribeRuntimeEventSession("chat-1", vi.fn(), {
      afterSeq: 0,
      streamEpoch: "epoch-a",
    });
    const onUpdate = mockSubscribeChatEvents.mock.calls[0][1];
    onUpdate(response(37));
    release();
    vi.advanceTimersByTime(500);

    const releaseNewLedger = subscribeRuntimeEventSession("chat-1", vi.fn(), {
      afterSeq: 2,
      streamEpoch: "epoch-b",
    });

    expect(mockSubscribeChatEvents.mock.calls[1][2]).toEqual(
      expect.objectContaining({
        afterSeq: 2,
        streamEpoch: "epoch-b",
      }),
    );
    releaseNewLedger();
  });

  it("does not share transports between different chats", () => {
    const releaseFirst = subscribeRuntimeEventSession("chat-1", vi.fn());
    const releaseSecond = subscribeRuntimeEventSession("chat-2", vi.fn());

    expect(mockSubscribeChatEvents).toHaveBeenCalledTimes(2);
    expect(mockSubscribeChatEvents.mock.calls[0][0]).toBe("chat-1");
    expect(mockSubscribeChatEvents.mock.calls[1][0]).toBe("chat-2");
    releaseFirst();
    releaseSecond();
  });

  it("does not share the same chat id across different agent workspaces", () => {
    const releaseFirst = subscribeRuntimeEventSession("chat-1", vi.fn(), {
      agentId: "Lantai_Water_Agent",
    });
    const releaseSecond = subscribeRuntimeEventSession("chat-1", vi.fn(), {
      agentId: "Lantai_Mocha_Agent",
    });

    expect(mockSubscribeChatEvents).toHaveBeenCalledTimes(2);
    expect(mockSubscribeChatEvents.mock.calls[0][2]).toEqual(
      expect.objectContaining({ agentId: "Lantai_Water_Agent" }),
    );
    expect(mockSubscribeChatEvents.mock.calls[1][2]).toEqual(
      expect.objectContaining({ agentId: "Lantai_Mocha_Agent" }),
    );
    releaseFirst();
    releaseSecond();
  });

  it("drops a transport response that belongs to another chat", () => {
    const listener = vi.fn();
    const release = subscribeRuntimeEventSession("chat-1", listener);
    const onUpdate = mockSubscribeChatEvents.mock.calls[0][1];

    onUpdate({ ...response(1), chat_id: "chat-2" });
    expect(listener).not.toHaveBeenCalled();

    onUpdate(response(2));
    expect(listener).toHaveBeenCalledOnce();
    expect(listener).toHaveBeenCalledWith(response(2));
    release();
  });

  it("replays the versioned P0 cross-talk incident without foreign delivery", () => {
    const incident = p0Incident("chat-cross-talk-v1");
    const fixture = incident.input;
    const expected = incident.expected as {
      delivered_chat_ids: string[];
      dropped_foreign_responses: number;
    };
    const listener = vi.fn();
    const release = subscribeRuntimeEventSession(
      String(fixture.subscribed_chat_id),
      listener,
    );
    const onUpdate = mockSubscribeChatEvents.mock.calls[0][1];

    onUpdate({
      ...response(Number(fixture.foreign_seq)),
      chat_id: String(fixture.foreign_chat_id),
    });
    onUpdate({
      ...response(Number(fixture.owned_seq)),
      chat_id: String(fixture.subscribed_chat_id),
    });

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener.mock.calls.map(([payload]) => payload.chat_id)).toEqual(
      expected.delivered_chat_ids,
    );
    expect(2 - listener.mock.calls.length).toBe(
      expected.dropped_foreign_responses,
    );
    release();
  });

  it("keeps 10,000 interleaved transport responses isolated by chat", () => {
    const first = vi.fn();
    const second = vi.fn();
    const releaseFirst = subscribeRuntimeEventSession("chat-1", first);
    const releaseSecond = subscribeRuntimeEventSession("chat-2", second);
    const updateFirst = mockSubscribeChatEvents.mock.calls[0][1];
    const updateSecond = mockSubscribeChatEvents.mock.calls[1][1];

    for (let seq = 1; seq <= 5_000; seq += 1) {
      const firstResponse = response(seq);
      const secondResponse = { ...response(seq), chat_id: "chat-2" };
      updateFirst(secondResponse);
      updateSecond(firstResponse);
      updateFirst(firstResponse);
      updateSecond(secondResponse);
    }

    expect(first).toHaveBeenCalledTimes(5_000);
    expect(second).toHaveBeenCalledTimes(5_000);
    expect(
      first.mock.calls.every(([payload]) => payload.chat_id === "chat-1"),
    ).toBe(true);
    expect(
      second.mock.calls.every(([payload]) => payload.chat_id === "chat-2"),
    ).toBe(true);
    releaseFirst();
    releaseSecond();
  });
});
