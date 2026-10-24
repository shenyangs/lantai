import { describe, it, expect, vi, afterEach } from "vitest";
import { browserWorkbenchApi } from "./browserSession";
import { request } from "../request";

vi.mock("../request", () => ({ request: vi.fn() }));
vi.mock("../config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));
vi.mock("../authHeaders", () => ({
  buildAuthHeaders: vi.fn(() => ({ Authorization: "Bearer test-token" })),
}));

describe("browserWorkbenchApi task events", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("builds task event stream URLs with encoded cursor", () => {
    expect(browserWorkbenchApi.taskEventsStreamUrl("run/1", "evt 1")).toBe(
      "/api/browser-workbench/tasks/run%2F1/events/stream?after_id=evt%201",
    );
  });

  it("creates picker-backed browser file grants", async () => {
    vi.mocked(request).mockResolvedValueOnce({
      ok: true,
      file_grant_id: "grant-1",
      security_scope_ref: "browser-security-scope:test",
      security_scoped_bookmark: true,
      security_scope_bookmark_sha256: "sha256:bookmark",
      path: "/tmp/upload.png",
      profile_id: "default",
      run_id: "run-1",
      page_id: "tab-1",
      url: "https://example.com/upload",
      selector: "input[type=file]",
      expires_at: "2026-06-01T00:10:00+00:00",
    });

    await expect(
      browserWorkbenchApi.createFileGrant({
        path: "/tmp/upload.png",
        profile_id: "default",
        run_id: "run-1",
        page_id: "tab-1",
        url: "https://example.com/upload",
        selector: "input[type=file]",
        security_scoped_bookmark: true,
        security_scope_ref: "browser-security-scope:test",
        security_scope_bookmark_sha256: "sha256:bookmark",
      }),
    ).resolves.toMatchObject({ file_grant_id: "grant-1" });

    expect(request).toHaveBeenCalledWith("/browser-workbench/file-grants", {
      method: "POST",
      body: JSON.stringify({
        path: "/tmp/upload.png",
        profile_id: "default",
        run_id: "run-1",
        page_id: "tab-1",
        url: "https://example.com/upload",
        selector: "input[type=file]",
        security_scoped_bookmark: true,
        security_scope_ref: "browser-security-scope:test",
        security_scope_bookmark_sha256: "sha256:bookmark",
      }),
    });
  });

  it("streams task events with auth headers until a terminal status", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'id: evt-1\nevent: task_started\ndata: {"id":"evt-1","ts":"2026-06-01T00:00:00Z","run_id":"run/1","type":"task_started","status":"running_background","message":"started","action":"","observation_id":"","metadata":{}}\n\n',
          ),
        );
        controller.enqueue(
          new TextEncoder().encode(
            'id: evt-2\nevent: task_completed\ndata: {"id":"evt-2","ts":"2026-06-01T00:00:01Z","run_id":"run/1","type":"task_completed","status":"completed","message":"done","action":"","observation_id":"","metadata":{}}\n\n',
          ),
        );
        controller.close();
      },
    });
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body,
    } as unknown as Response);
    const onEvent = vi.fn();
    const onError = vi.fn();

    const unsubscribe = browserWorkbenchApi.subscribeTaskEvents(
      "run/1",
      onEvent,
      { afterId: "evt 0", onError },
    );

    await vi.waitFor(() => {
      expect(onEvent).toHaveBeenCalledTimes(2);
    });
    unsubscribe();

    expect(fetch).toHaveBeenCalledWith(
      "/api/browser-workbench/tasks/run%2F1/events/stream?timeout_seconds=60&after_id=evt+0",
      expect.objectContaining({
        headers: { Authorization: "Bearer test-token" },
        signal: expect.any(AbortSignal),
      }),
    );
    expect(onEvent).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        id: "evt-2",
        status: "completed",
      }),
    );
    expect(onError).not.toHaveBeenCalled();
  });
});
