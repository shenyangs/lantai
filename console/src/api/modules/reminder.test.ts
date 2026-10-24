import { beforeEach, describe, expect, it, vi } from "vitest";
import { reminderApi } from "./reminder";

vi.mock("../request", () => ({ request: vi.fn() }));

import { request } from "../request";

describe("reminderApi", () => {
  beforeEach(() => {
    vi.mocked(request).mockResolvedValue({});
  });

  it("lists reminders without a status query", async () => {
    await reminderApi.listReminders();
    expect(request).toHaveBeenCalledWith("/reminders");
  });

  it("lists reminders with a status query", async () => {
    await reminderApi.listReminders("snoozed");
    expect(request).toHaveBeenCalledWith("/reminders?status=snoozed");
  });

  it("creates reminders with the expected path and method", async () => {
    await reminderApi.createReminder({ title: "准备周会" });
    expect(request).toHaveBeenCalledWith(
      "/reminders",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ title: "准备周会" }),
      }),
    );
  });

  it("snoozes reminders through the quiet action endpoint", async () => {
    await reminderApi.snoozeReminder("rem/1", "2026-05-28T10:00:00.000Z");
    expect(request).toHaveBeenCalledWith(
      "/reminders/rem%2F1/snooze",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ until: "2026-05-28T10:00:00.000Z" }),
      }),
    );
  });

  it("loads today's brief", async () => {
    await reminderApi.getTodayBrief();
    expect(request).toHaveBeenCalledWith("/reminders/brief/today");
  });

  it("updates watch topics", async () => {
    await reminderApi.updateWatchTopic("topic/1", { enabled: false });
    expect(request).toHaveBeenCalledWith(
      "/reminders/watch-topics/topic%2F1",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ enabled: false }),
      }),
    );
  });

  it("version-fences explicit WatchTopic brief confirmation", async () => {
    const payload = {
      expected_revision: 2,
      explicit_confirmation: true,
      status: "confirmed" as const,
      purpose: "跟踪公开产品变化",
      scope_refs: ["public:web"],
      evidence_refs: [],
      exclusions: ["private:ungranted"],
      valid_until: null,
    };

    await reminderApi.configureWatchTopicForBrief("topic/1", payload);

    expect(request).toHaveBeenCalledWith(
      "/reminders/watch-topics/topic%2F1/brief-use",
      { method: "POST", body: JSON.stringify(payload) },
      { operation: "mutation" },
    );
  });
});
