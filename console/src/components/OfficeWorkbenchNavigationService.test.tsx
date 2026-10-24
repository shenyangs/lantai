import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes, useNavigate } from "react-router-dom";
import { renderWithProviders } from "@/test/common_setup";
import OfficeWorkbenchNavigationService from "./OfficeWorkbenchNavigationService";

function OfficeRoute() {
  const navigate = useNavigate();
  return <button onClick={() => navigate("/works")}>打开作品</button>;
}

function TestRoutes() {
  return (
    <>
      <OfficeWorkbenchNavigationService />
      <Routes>
        <Route path="/office-workbench" element={<OfficeRoute />} />
        <Route path="/works" element={<div>作品页面</div>} />
      </Routes>
    </>
  );
}

describe("OfficeWorkbenchNavigationService", () => {
  beforeEach(() => {
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: undefined,
    });
  });

  it("releases the native editor when any app navigation leaves the workbench", async () => {
    const hide = vi.fn().mockResolvedValue({ success: true });
    let resolveState: ((state: OfficeWorkbenchState) => void) | undefined;
    const getState = vi.fn(
      () =>
        new Promise<OfficeWorkbenchState>((resolve) => {
          resolveState = resolve;
        }),
    );
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        officeWorkbench: {
          getState,
          hide,
          onPresent: vi.fn(() => vi.fn()),
        },
      },
    });

    renderWithProviders(<TestRoutes />, {
      initialEntries: ["/office-workbench"],
    });
    fireEvent.click(screen.getByRole("button", { name: "打开作品" }));

    expect(await screen.findByText("作品页面")).toBeInTheDocument();
    await waitFor(() => expect(hide).toHaveBeenCalled());
    resolveState?.({
      active: true,
      visible: true,
      workbench_id: "stale-workbench",
      kind: "slides",
      display_name: "report.pptx",
      source_chat_id: "chat-source",
      route: "/office-workbench",
    });

    await Promise.resolve();
    expect(screen.getByText("作品页面")).toBeInTheDocument();
  });

  it("still enters the workbench when a new native editor is presented", async () => {
    let present: ((state: OfficeWorkbenchState) => void) | undefined;
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        officeWorkbench: {
          getState: vi.fn().mockResolvedValue({
            active: false,
            visible: false,
            route: "/office-workbench",
          }),
          hide: vi.fn().mockResolvedValue({ success: true }),
          onPresent: vi.fn((listener) => {
            present = listener;
            return vi.fn();
          }),
        },
      },
    });

    renderWithProviders(<TestRoutes />, { initialEntries: ["/works"] });
    expect(await screen.findByText("作品页面")).toBeInTheDocument();

    act(() => {
      present?.({
        active: true,
        visible: false,
        workbench_id: "new-workbench",
        kind: "slides",
        display_name: "new-report.pptx",
        source_chat_id: "chat-source",
        route: "/office-workbench",
      });
    });

    expect(
      await screen.findByRole("button", { name: "打开作品" }),
    ).toBeInTheDocument();
  });

  it("recovers a cold-start editor whose presentation event arrived before React mounted", async () => {
    const hide = vi.fn().mockResolvedValue({ success: true });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        officeWorkbench: {
          getState: vi.fn().mockResolvedValue({
            active: true,
            visible: false,
            workbench_id: "cold-start-workbench",
            kind: "docs",
            display_name: "startup-report.docx",
            source_chat_id: "",
            route: "/office-workbench",
          }),
          hide,
          onPresent: vi.fn(() => vi.fn()),
        },
      },
    });

    renderWithProviders(<TestRoutes />, { initialEntries: ["/works"] });

    expect(
      await screen.findByRole("button", { name: "打开作品" }),
    ).toBeInTheDocument();
    expect(hide).not.toHaveBeenCalled();
  });

  it("does not enter the workbench when getState resolves with a business failure", async () => {
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        officeWorkbench: {
          getState: vi.fn().mockResolvedValue({
            success: false,
            error: "Office 工作台请求来源不受信任",
            error_code: "office_workbench_untrusted_sender",
          }),
          hide: vi.fn().mockResolvedValue({ success: true }),
          onPresent: vi.fn(() => vi.fn()),
        },
      },
    });

    renderWithProviders(<TestRoutes />, { initialEntries: ["/works"] });

    expect(await screen.findByText("作品页面")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        window.electronAPI?.officeWorkbench?.getState,
      ).toHaveBeenCalledOnce(),
    );
    expect(
      screen.queryByRole("button", { name: "打开作品" }),
    ).not.toBeInTheDocument();
  });
});
