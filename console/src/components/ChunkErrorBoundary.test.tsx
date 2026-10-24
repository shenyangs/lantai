import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ChunkErrorBoundary } from "./ChunkErrorBoundary";

describe("ChunkErrorBoundary", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("unavailable")));
    window.electronAPI = undefined;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("does not remount the failed route when public build identity cannot be read", async () => {
    let renderCount = 0;
    function BrokenRoute() {
      renderCount += 1;
      throw new Error("SECRET-DOCUMENT-CONTENT");
    }

    render(
      <ChunkErrorBoundary resetKey="/memory">
        <BrokenRoute />
      </ChunkErrorBoundary>,
    );

    expect(await screen.findByText("页面出现异常")).toBeVisible();
    const renderCountAfterFailure = renderCount;
    await waitFor(() => expect(fetch).toHaveBeenCalledOnce());

    expect(renderCount).toBe(renderCountAfterFailure);
    expect(screen.getByText(/界面显示中断不等于/)).toBeVisible();
    expect(document.body).not.toHaveTextContent("SECRET-DOCUMENT-CONTENT");
  });
});
