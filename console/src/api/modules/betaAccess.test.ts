import { beforeEach, describe, expect, it, vi } from "vitest";

const { request } = vi.hoisted(() => ({ request: vi.fn() }));

vi.mock("../request", () => ({ request }));

import { betaAccessApi } from "./betaAccess";

describe("betaAccessApi", () => {
  beforeEach(() => vi.clearAllMocks());

  it("passes a custom invite to the local trusted backend without transforming it", async () => {
    request.mockResolvedValue({ status: "active" });

    await betaAccessApi.activate({
      code: "朋友 请坐",
      request_id: "stable-request",
    });

    expect(request).toHaveBeenCalledWith("/beta-access/activate", {
      method: "POST",
      body: JSON.stringify({
        code: "朋友 请坐",
        request_id: "stable-request",
      }),
    });
  });

  it("recovers a committed issuance by opaque operation id", async () => {
    request.mockResolvedValue({ status: "active" });

    await betaAccessApi.recover("operation-1");

    expect(request).toHaveBeenCalledWith("/beta-access/recover", {
      method: "POST",
      body: JSON.stringify({ operation_id: "operation-1" }),
    });
  });

  it("passes the scoped recovery token for anonymous issuance recovery", async () => {
    request.mockResolvedValue({ status: "active" });

    await betaAccessApi.recover("operation-2", "recovery-token-2");

    expect(request).toHaveBeenCalledWith("/beta-access/recover", {
      method: "POST",
      body: JSON.stringify({
        operation_id: "operation-2",
        recovery_token: "recovery-token-2",
      }),
    });
  });
});
