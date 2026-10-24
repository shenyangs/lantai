import { beforeEach, describe, expect, it, vi } from "vitest";

const { request } = vi.hoisted(() => ({ request: vi.fn() }));

vi.mock("../request", () => ({ request }));

import { cloudAccountApi } from "./cloudAccount";

describe("cloudAccountApi", () => {
  beforeEach(() => vi.clearAllMocks());

  it("starts Watcha login through the local backend", async () => {
    request.mockResolvedValue({ attempt_id: "attempt" });
    await cloudAccountApi.startWatcha();
    expect(request).toHaveBeenCalledWith("/cloud-account/watcha/start", {
      method: "POST",
    });
  });

  it("polls with only the local attempt id", async () => {
    request.mockResolvedValue({ status: "pending" });
    await cloudAccountApi.pollWatcha("local-attempt");
    expect(request).toHaveBeenCalledWith("/cloud-account/watcha/poll", {
      method: "POST",
      body: JSON.stringify({ attempt_id: "local-attempt" }),
    });
  });

  it("cancels only the active local attempt", async () => {
    request.mockResolvedValue({ cancelled: true });
    await cloudAccountApi.cancelWatcha("local-attempt");
    expect(request).toHaveBeenCalledWith("/cloud-account/watcha/cancel", {
      method: "POST",
      body: JSON.stringify({ attempt_id: "local-attempt" }),
    });
  });
});
