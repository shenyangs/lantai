import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  ApiRequestError,
  getSafeRequestErrorDiagnostic,
  request,
} from "./request";
import { parseErrorDetail } from "../utils/error";

// mock config so URL is predictable and token is empty by default
vi.mock("./config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
  getApiToken: vi.fn(() => ""),
  clearAuthToken: vi.fn(),
}));

vi.mock("./authHeaders", () => ({
  buildAuthHeaders: vi.fn(() => ({})),
}));

import { clearAuthToken } from "./config";
import { buildAuthHeaders } from "./authHeaders";

// Helper: create a mock Response
function mockFetch(
  status: number,
  body?: unknown,
  contentType = "application/json",
) {
  const responseBody =
    body !== undefined
      ? typeof body === "string"
        ? body
        : JSON.stringify(body)
      : "";

  global.fetch = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    statusText:
      status === 200 ? "OK" : status === 401 ? "Unauthorized" : "Error",
    headers: { get: () => contentType },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(responseBody),
  } as unknown as Response);
}

describe("request", () => {
  beforeEach(() => {
    vi.mocked(buildAuthHeaders).mockReturnValue({});
    Object.defineProperty(window, "location", {
      value: { pathname: "/chat", href: "" },
      writable: true,
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  // ---------------------------------------------------------------------------
  // Successful requests
  // ---------------------------------------------------------------------------

  it("GET request does not set Content-Type", async () => {
    mockFetch(200, { data: "ok" });
    await request("/models");
    const headers: Headers = (fetch as any).mock.calls[0][1].headers;
    expect(headers.has("Content-Type")).toBe(false);
  });

  it("POST request automatically adds Content-Type: application/json", async () => {
    mockFetch(200, { data: "ok" });
    await request("/models", { method: "POST", body: "{}" });
    const headers: Headers = (fetch as any).mock.calls[0][1].headers;
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("PUT request automatically adds Content-Type", async () => {
    mockFetch(200, { status: "ok" });
    await request("/models/active", { method: "PUT", body: "{}" });
    const headers: Headers = (fetch as any).mock.calls[0][1].headers;
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("caller-specified Content-Type is not overridden", async () => {
    mockFetch(200, { status: "ok" });
    await request("/upload", {
      method: "POST",
      headers: { "Content-Type": "multipart/form-data" },
    });
    const headers: Headers = (fetch as any).mock.calls[0][1].headers;
    expect(headers.get("Content-Type")).toBe("multipart/form-data");
  });

  it("correctly parses and returns JSON response", async () => {
    mockFetch(200, { id: 1, name: "test" });
    const result = await request<{ id: number; name: string }>("/models");
    expect(result).toEqual({ id: 1, name: "test" });
  });

  it("returns text for non-JSON Content-Type", async () => {
    mockFetch(200, "plain text response", "text/plain");
    const result = await request("/health");
    expect(result).toBe("plain text response");
  });

  it("204 response returns undefined", async () => {
    mockFetch(204, undefined, "");
    const result = await request("/models/active");
    expect(result).toBeUndefined();
  });

  // ---------------------------------------------------------------------------
  // Error handling
  // ---------------------------------------------------------------------------

  it("calls clearAuthToken and redirects to /login on 401", async () => {
    mockFetch(401);
    await expect(request("/models")).rejects.toThrow("Not authenticated");
    expect(clearAuthToken).toHaveBeenCalledOnce();
    expect(window.location.href).toBe("/login");
  });

  it("does not redirect again when already on /login for 401", async () => {
    window.location.pathname = "/login";
    window.location.href = "";
    mockFetch(401);
    await expect(request("/models")).rejects.toThrow("Not authenticated");
    expect(window.location.href).toBe("");
  });

  it.each([
    ["/", "#/chat?lantaiColdStart=1", true, "/login"],
    ["/", "", true, "/login"],
    ["/console", "#/console/chat", true, "/console/login"],
    ["/console/", "#/console/chat", true, "/console/login"],
    ["/", "#/chat", false, "/login"],
    ["/", "", false, "/login"],
    ["/index.html", "", false, "/login"],
    ["/console/", "", false, "/console/login"],
  ] as const)(
    "keeps the console document on 401 at %s with hash %s (desktop %s)",
    async (pathname, hash, isDesktop, loginRoute) => {
      const previousElectronAPI = window.electronAPI;
      Object.defineProperty(window, "electronAPI", {
        value: { isDesktop },
        configurable: true,
        writable: true,
      });
      window.location.pathname = pathname;
      window.location.hash = hash;
      try {
        mockFetch(401);
        await expect(request("/models")).rejects.toThrow("Not authenticated");
        expect(clearAuthToken).toHaveBeenCalledOnce();
        expect(window.location.hash).toBe(loginRoute);
        expect(window.location.href).toBe("");
        expect(window.location.pathname).toBe(pathname);
      } finally {
        window.electronAPI = previousElectronAPI;
      }
    },
  );

  it.each([
    ["/", "#/login?redirect=%2Fchat"],
    ["/console/", "#/console/login?redirect=%2Fconsole%2Fchat"],
  ])("preserves the active hash login route at %s on 401", async (pathname, hash) => {
    window.location.pathname = pathname;
    window.location.hash = hash;
    mockFetch(401);
    await expect(request("/models")).rejects.toThrow("Not authenticated");
    expect(window.location.hash).toBe(hash);
    expect(window.location.href).toBe("");
  });

  it("throws error with status code for non-401 errors", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      headers: { get: () => "application/json" },
      text: () => Promise.resolve("server exploded"),
    } as unknown as Response);

    await expect(request("/models")).rejects.toThrow("server exploded");
  });

  it("shows structured detail once while preserving its machine-readable code", async () => {
    mockFetch(403, {
      detail: {
        code: "workspace_grant_required",
        message: "请重新选择并授权工作区",
      },
    });

    const error = await request("/coding/files/tree").catch(
      (caught) => caught,
    );

    expect(error.message).toBe("请重新选择并授权工作区");
    expect(parseErrorDetail(error)?.code).toBe("workspace_grant_required");
  });

  it.each([
    [401, "authentication", "sign-in"],
    [422, "validation", "fix-input"],
    [429, "rate-limit", "wait-and-retry"],
  ] as const)(
    "keeps HTTP %s recovery semantics structured",
    async (status, kind, recoveryAction) => {
      mockFetch(status, {
        status,
        code: `error_${status}`,
        message: `failure ${status}`,
        details: {
          request_id: `request-${status}`,
          owner: "provider-settings",
          certainty: "not_applied",
          retry_hint: status === 429 ? "after_backoff" : "do_not_retry",
        },
      });

      const caught = await request("/models", {
        method: status === 422 ? "POST" : "GET",
      }).catch((error) => error);

      expect(caught).toBeInstanceOf(ApiRequestError);
      expect(caught).toMatchObject({
        httpStatus: status,
        code: `error_${status}`,
        kind,
        requestId: `request-${status}`,
        owner: "provider-settings",
        certainty: "not_applied",
        retryHint: status === 429 ? "after_backoff" : "do_not_retry",
        recoveryAction,
      });
    },
  );

  it("marks a mutation deadline as unknown and requires reconciliation without replay", async () => {
    vi.useFakeTimers();
    global.fetch = vi.fn((_url, init) => {
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("deadline", "AbortError"));
        });
      });
    }) as typeof fetch;

    const pending = request(
      "/tools/tool-1",
      { method: "PATCH", body: "{}" },
      { operation: "mutation", timeoutMs: 50 },
    ).catch((error) => error);
    await vi.advanceTimersByTimeAsync(50);
    const caught = await pending;
    vi.useRealTimers();

    expect(caught).toBeInstanceOf(ApiRequestError);
    expect(caught).toMatchObject({
      kind: "timeout",
      certainty: "unknown",
      retryHint: "reconcile_before_retry",
      recoveryAction: "check-result",
    });
  });

  it("keeps caller cancellation distinct from a deadline or a server-side cancel", async () => {
    const controller = new AbortController();
    global.fetch = vi.fn((_url, init) => {
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("caller cancelled", "AbortError"));
        });
      });
    }) as typeof fetch;

    const pending = request(
      "/models",
      { signal: controller.signal },
      { operation: "read", timeoutMs: 5000 },
    ).catch((error) => error);
    controller.abort();
    const caught = await pending;

    expect(caught).toBeInstanceOf(ApiRequestError);
    expect(caught).toMatchObject({
      kind: "cancelled",
      certainty: "unknown",
      recoveryAction: "none",
    });
  });

  it("copies only allowlisted request diagnostics", () => {
    const error = new ApiRequestError("token=secret raw server message", {
      httpStatus: 422,
      code: "invalid_provider",
      kind: "validation",
      requestId: "request-safe",
      owner: "provider-settings",
      certainty: "not_applied",
      retryHint: "fix_fields",
      recoveryAction: "fix-input",
      detail: {
        token: "secret",
        stack: "private stack",
        document: "private document",
      },
    });

    const diagnostic = getSafeRequestErrorDiagnostic(error);

    expect(diagnostic).toEqual({
      httpStatus: 422,
      code: "invalid_provider",
      kind: "validation",
      requestId: "request-safe",
      owner: "provider-settings",
      certainty: "not_applied",
      retryHint: "fix_fields",
      recoveryAction: "fix-input",
    });
    expect(JSON.stringify(diagnostic)).not.toContain("secret");
    expect(JSON.stringify(diagnostic)).not.toContain("private");
  });

  it("injects Authorization header when token is present", async () => {
    vi.mocked(buildAuthHeaders).mockReturnValue({
      Authorization: "Bearer test-token",
    });
    mockFetch(200, {});
    await request("/models");
    const headers: Headers = (fetch as any).mock.calls[0][1].headers;
    expect(headers.get("Authorization")).toBe("Bearer test-token");
  });

  it("request URL is correctly built by getApiUrl", async () => {
    mockFetch(200, {});
    await request("/models/active");
    expect(fetch).toHaveBeenCalledWith(
      "/api/models/active",
      expect.any(Object),
    );
  });
});
