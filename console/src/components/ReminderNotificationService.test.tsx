import { act, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/common_setup";
import { useAgentStore } from "@/stores/agentStore";
import ReminderNotificationService from "./ReminderNotificationService";

const { claimNative, resolveNative } = vi.hoisted(() => ({
  claimNative: vi.fn(), resolveNative: vi.fn(),
}));
vi.mock("@/api/modules/reminder", () => ({ reminderApi: { claimNative, resolveNative } }));
vi.mock("@/api/authHeaders", () => ({
  buildAuthHeaders: () => ({ Authorization: "Bearer synthetic" }),
  encodeHeaderValue: encodeURIComponent,
}));

function installDesktop(notify = vi.fn()) {
  Object.defineProperty(window, "electronAPI", { configurable: true, value: {
    notifySystemBehavior: notify, onReminderOpen: vi.fn(() => vi.fn()),
  } });
  return notify;
}
const payload = { run_id: "r", attempt_id: "r:a", reminder_id: "m",
  agent_id: "a", receipt_token: "synthetic", caller_id: "", title: "Synthetic", body: "" };

describe("ReminderNotificationService", () => {
  beforeEach(() => {
    vi.restoreAllMocks(); vi.clearAllMocks();
    useAgentStore.setState({ selectedAgent: "a" });
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    Object.defineProperty(window, "electronAPI", { configurable: true, value: undefined });
    claimNative.mockImplementation(async (caller: string) => ({ ...payload, caller_id: caller }));
    resolveNative.mockResolvedValue({ ok: true });
  });
  it("never claims without the desktop adapter", async () => {
    renderWithProviders(<ReminderNotificationService />);
    await Promise.resolve();
    expect(claimNative).not.toHaveBeenCalled();
  });
  it("never claims while focused", async () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    installDesktop(); renderWithProviders(<ReminderNotificationService />);
    await Promise.resolve(); expect(claimNative).not.toHaveBeenCalled();
  });
  it("settles only the full original show receipt", async () => {
    const notify = installDesktop(vi.fn(async (request) => ({
      ...request, ok: true, shown: true, status: "shown",
    })));
    renderWithProviders(<ReminderNotificationService />);
    await waitFor(() => expect(resolveNative).toHaveBeenCalledTimes(1));
    const request = notify.mock.calls[0][0];
    expect(request).toMatchObject({ ...payload, caller_id: expect.any(String), id: "r", type: "reminder" });
    expect(resolveNative.mock.calls[0][0]).toMatchObject({ id: "r", ok: true,
      shown: true, status: "shown", caller_id: request.caller_id, agent_id: "a" });
    expect(resolveNative.mock.calls[0][1]).toMatchObject({ "X-Agent-Id": "a", "X-Workspace-Grant-Id": "" });
  });
  it.each(["attempt_id", "caller_id", "agent_id", "id"])("rejects a foreign %s show receipt", async (key) => {
    installDesktop(vi.fn(async (request) => ({ ...request, [key]: "foreign",
      ok: true, shown: true, status: "shown" })));
    renderWithProviders(<ReminderNotificationService />);
    await waitFor(() => expect(resolveNative).toHaveBeenCalledTimes(1));
    expect(resolveNative.mock.calls[0][0]).toMatchObject({ ok: false, shown: false, status: "unknown", agent_id: "a" });
  });
  it("keeps an IPC timeout unknown", async () => {
    installDesktop(vi.fn().mockRejectedValue(new Error("synthetic timeout")));
    renderWithProviders(<ReminderNotificationService />);
    await waitFor(() => expect(resolveNative).toHaveBeenCalledTimes(1));
    expect(resolveNative.mock.calls[0][0]).toMatchObject({ ok: false, shown: false, status: "unknown" });
  });
  it("does not show a claim returned after unmount", async () => {
    let release!: (value: unknown) => void;
    claimNative.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    const notify = installDesktop();
    const view = renderWithProviders(<ReminderNotificationService />);
    view.unmount();
    await act(async () => release(payload));
    expect(notify).not.toHaveBeenCalled();
  });
  it.each(["focus", "unmount"])("resolves a valid pre-IPC claim after %s as not submitted", async (transition) => {
    let release!: (value: unknown) => void;
    let caller = "";
    claimNative.mockImplementation((id: string) => {
      caller = id;
      return new Promise((resolve) => { release = resolve; });
    });
    const notify = installDesktop();
    const view = renderWithProviders(<ReminderNotificationService />);
    if (transition === "focus") vi.spyOn(document, "hasFocus").mockReturnValue(true);
    else view.unmount();
    await act(async () => release({ ...payload, caller_id: caller }));
    await waitFor(() => expect(resolveNative).toHaveBeenCalledTimes(1));
    expect(notify).not.toHaveBeenCalled();
    expect(resolveNative.mock.calls[0][0]).toMatchObject({
      ok: false, shown: false, status: "failed", reason: "desktop_not_submitted",
      agent_id: "a", caller_id: caller,
    });
    expect(resolveNative.mock.calls[0][1]["X-Agent-Id"]).toBe("a");
  });
  it("writes an in-flight receipt to the captured original agent after switching", async () => {
    let finish!: (value: unknown) => void;
    const notify = installDesktop(vi.fn(() => new Promise((resolve) => { finish = resolve; })));
    claimNative.mockImplementationOnce(async (caller: string) => ({ ...payload, caller_id: caller }))
      .mockResolvedValue(null);
    renderWithProviders(<ReminderNotificationService />);
    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
    act(() => useAgentStore.setState({ selectedAgent: "b" }));
    const request = notify.mock.calls[0][0];
    await act(async () => finish({ ...request, ok: true, shown: true, status: "shown" }));
    await waitFor(() => expect(resolveNative).toHaveBeenCalledTimes(1));
    expect(resolveNative.mock.calls[0][1]["X-Agent-Id"]).toBe("a");
  });
});
