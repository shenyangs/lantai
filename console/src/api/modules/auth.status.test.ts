import { afterEach, describe, expect, it, vi } from "vitest";
import { authApi } from "./auth";
vi.mock("../config", () => ({ getApiUrl: (path: string) => "/api" + path }));
describe("auth status failure identity", () => {
  it("forwards an optional AbortSignal without dropping caller cancellation", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ enabled: true, has_users: true }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await authApi.getStatus(controller.signal);
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/status", {
      signal: controller.signal,
    });
  });
  afterEach(() => vi.unstubAllGlobals());
  it("retains HTTP 503 for credential recovery", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503 }),
    );
    const error = await authApi.getStatus().catch((value) => value);
    expect(error).toBeInstanceOf(Error);
    expect(error.status).toBe(503);
  });
  it("retains other HTTP errors and the existing default message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 500 }),
    );
    const error = await authApi.getStatus().catch((value) => value);
    expect(error).toMatchObject({
      status: 500,
      message: "Failed to check auth status",
    });
  });
  it("preserves the original transport failure", async () => {
    const error = new TypeError("Fixture offline");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(error));
    await expect(authApi.getStatus()).rejects.toBe(error);
  });
});
