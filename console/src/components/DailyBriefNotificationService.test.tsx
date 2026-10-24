import { act, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes, useLocation } from "react-router-dom";
import { renderWithProviders } from "@/test/common_setup";
import DailyBriefNotificationService from "./DailyBriefNotificationService";

const { claimNextNativeDelivery, resolveNativeDelivery } = vi.hoisted(() => ({
  claimNextNativeDelivery: vi.fn(),
  resolveNativeDelivery: vi.fn(),
}));

vi.mock("@/api/modules/dailyBrief", () => ({
  dailyBriefApi: { claimNextNativeDelivery, resolveNativeDelivery },
}));

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname + location.search}
    </output>
  );
}

describe("DailyBriefNotificationService", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: undefined,
    });
  });

  it("does not claim deliveries outside the trusted desktop shell", async () => {
    renderWithProviders(<DailyBriefNotificationService />);
    await Promise.resolve();
    expect(claimNextNativeDelivery).not.toHaveBeenCalled();
  });

  it("does not claim a delivery while the desktop window is focused", async () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        notifySystemBehavior: vi.fn(),
        onDailyBriefOpen: vi.fn(() => vi.fn()),
      },
    });

    renderWithProviders(<DailyBriefNotificationService />);
    await Promise.resolve();

    expect(claimNextNativeDelivery).not.toHaveBeenCalled();
  });

  it("keeps a duplicate native outcome unknown instead of claiming delivery", async () => {
    const notifySystemBehavior = vi.fn().mockResolvedValue({
      ok: true,
      shown: false,
      reason: "duplicate",
    });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        notifySystemBehavior,
        onDailyBriefOpen: vi.fn(() => vi.fn()),
      },
    });
    claimNextNativeDelivery.mockResolvedValue({
      receipt_id: "delivery-unknown",
      edition_id: "edition-1",
      edition_revision: 1,
      title: "澜台简报",
      body: "有一份新的简报可以阅读",
      open_route: "/daily-brief",
    });

    renderWithProviders(<DailyBriefNotificationService />);

    await waitFor(() => expect(notifySystemBehavior).toHaveBeenCalledTimes(1));
    expect(resolveNativeDelivery).not.toHaveBeenCalled();
  });

  it("records a definitive native refusal as failed", async () => {
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        notifySystemBehavior: vi.fn().mockResolvedValue({
          ok: true,
          shown: false,
          reason: "unsupported",
        }),
        onDailyBriefOpen: vi.fn(() => vi.fn()),
      },
    });
    claimNextNativeDelivery.mockResolvedValue({
      receipt_id: "delivery-failed",
      edition_id: "edition-1",
      edition_revision: 1,
      title: "澜台简报",
      body: "有一份新的简报可以阅读",
      open_route: "/daily-brief",
    });
    resolveNativeDelivery.mockResolvedValue({ state: "failed" });

    renderWithProviders(<DailyBriefNotificationService />);

    await waitFor(() => {
      expect(resolveNativeDelivery).toHaveBeenCalledWith("delivery-failed", {
        state: "failed",
        error: "unsupported",
      });
    });
  });

  it("preserves a native confirmation timeout as unknown", async () => {
    Object.defineProperty(window, "electronAPI", { configurable: true, value: {
      notifySystemBehavior: vi.fn().mockResolvedValue({
        ok: false, shown: false, status: "unknown", reason: "confirmation_timeout",
      }), onDailyBriefOpen: vi.fn(() => vi.fn()),
    } });
    claimNextNativeDelivery.mockResolvedValue({ receipt_id: "unconfirmed", title: "Synthetic", body: "" });
    renderWithProviders(<DailyBriefNotificationService />);
    await waitFor(() => expect(claimNextNativeDelivery).toHaveBeenCalledTimes(1));
    await act(async () => { await Promise.resolve(); });
    expect(resolveNativeDelivery).not.toHaveBeenCalled();
  });

  it("records submitted only after Electron confirms it showed the notification", async () => {
    const listeners: Array<(payload: { receiptId: string }) => void> = [];
    const notifySystemBehavior = vi.fn().mockResolvedValue({ ok: true, shown: true });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        notifySystemBehavior,
        onDailyBriefOpen: vi.fn((listener) => {
          listeners.push(listener);
          return vi.fn();
        }),
      },
    });
    claimNextNativeDelivery
      .mockResolvedValueOnce({
        receipt_id: "delivery-1",
        edition_id: "edition-1",
        edition_revision: 1,
        title: "澜台简报",
        body: "有一份新的简报可以阅读",
        open_route: "/daily-brief",
      })
      .mockResolvedValue(null);
    resolveNativeDelivery.mockResolvedValue({ state: "submitted" });

    renderWithProviders(
      <>
        <DailyBriefNotificationService />
        <Routes>
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </>,
      { initialEntries: ["/chat"] },
    );

    await waitFor(() => {
      expect(resolveNativeDelivery).toHaveBeenCalledWith("delivery-1", {
        state: "submitted",
        adapter_message_id: "electron-local",
        error: "",
      });
    });
    act(() => listeners[0]?.({ receiptId: "delivery-1" }));
    expect(document.querySelector('[data-testid="location"]')).toHaveTextContent(
      "/daily-brief?delivery=delivery-1",
    );
  });
});
