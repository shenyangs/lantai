import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes, useParams } from "react-router-dom";
import { renderWithProviders } from "@/test/common_setup";
import OfficeWorkbench from "./index";

describe("OfficeWorkbench recovery states", () => {
  beforeEach(() => {
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: undefined,
    });
  });

  it("explains when the route is opened outside the desktop editor", async () => {
    renderWithProviders(<OfficeWorkbench />, {
      initialEntries: ["/office-workbench"],
    });
    expect(
      await screen.findByText("当前环境不支持本地 Office 工作台"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "返回原会话" }),
    ).toBeInTheDocument();
  });

  it("shows a recoverable empty state without creating or saving a file", async () => {
    const hide = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        officeWorkbench: {
          getState: vi.fn().mockResolvedValue(null),
          hide,
          layout: vi.fn().mockResolvedValue(undefined),
        },
      },
    });
    const { unmount } = renderWithProviders(<OfficeWorkbench />, {
      initialEntries: ["/office-workbench"],
    });
    expect(
      await screen.findByText("没有打开的 Office 文件"),
    ).toBeInTheDocument();
    expect(screen.getByText(/不会创建空白文件/)).toBeInTheDocument();
    unmount();
    expect(hide).toHaveBeenCalledOnce();
  });

  it("treats a resolved getState business failure as an error, not an empty workspace", async () => {
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
          layout: vi.fn().mockResolvedValue({ success: true }),
        },
      },
    });

    renderWithProviders(<OfficeWorkbench />, {
      initialEntries: ["/office-workbench"],
    });

    expect(await screen.findByText("编辑器状态不可用")).toBeInTheDocument();
    expect(
      screen.getByText(/Office 工作台请求来源不受信任/),
    ).toBeInTheDocument();
    expect(screen.queryByText("没有打开的 Office 文件")).toBeNull();
  });

  it("returns to the exact source chat after hiding the editor", async () => {
    const hide = vi.fn().mockResolvedValue({ success: true });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        officeWorkbench: {
          getState: vi.fn().mockResolvedValue({
            active: true,
            visible: true,
            workbench_id: "workbench-slides",
            kind: "slides",
            display_name: "report.pptx",
            source_chat_id: "chat-source-42",
            route: "/office-workbench",
          }),
          hide,
          layout: vi.fn().mockResolvedValue({ success: true }),
        },
      },
    });
    const ChatDestination = () => {
      const { chatId } = useParams();
      return <div>source:{chatId}</div>;
    };

    renderWithProviders(
      <Routes>
        <Route path="/office-workbench" element={<OfficeWorkbench />} />
        <Route path="/chat/:chatId" element={<ChatDestination />} />
      </Routes>,
      { initialEntries: ["/office-workbench"] },
    );

    expect(await screen.findByText("report.pptx")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /返回对话/ }));

    await waitFor(() => expect(hide).toHaveBeenCalled());
    expect(
      await screen.findByText("source:chat-source-42"),
    ).toBeInTheDocument();
  });

  it("stays in the workbench when hide resolves with success false", async () => {
    const hide = vi.fn().mockResolvedValue({
      success: false,
      error: "编辑器仍在处理保存请求",
      error_code: "office_workbench_busy",
    });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        officeWorkbench: {
          getState: vi.fn().mockResolvedValue({
            active: true,
            visible: true,
            workbench_id: "workbench-docs",
            kind: "docs",
            display_name: "draft.docx",
            source_chat_id: "chat-source-42",
            route: "/office-workbench",
          }),
          hide,
          layout: vi.fn().mockResolvedValue({ success: true }),
        },
      },
    });

    renderWithProviders(
      <Routes>
        <Route path="/office-workbench" element={<OfficeWorkbench />} />
        <Route path="/chat/:chatId" element={<div>已经离开工作台</div>} />
      </Routes>,
      { initialEntries: ["/office-workbench"] },
    );

    expect(await screen.findByText("draft.docx")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /返回对话/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "编辑器仍在处理保存请求",
    );
    expect(screen.queryByText("已经离开工作台")).toBeNull();
  });

  it("defers a zero-sized cold-start host until a usable layout is measured", async () => {
    const layout = vi.fn().mockResolvedValue({ success: true });
    const requestAnimationFrame = vi
      .spyOn(window, "requestAnimationFrame")
      .mockReturnValue(41);
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        officeWorkbench: {
          getState: vi.fn().mockResolvedValue({
            active: true,
            visible: false,
            workbench_id: "cold-start-docx",
            kind: "docs",
            display_name: "startup.docx",
            source_chat_id: "",
            route: "/office-workbench",
          }),
          hide: vi.fn().mockResolvedValue({ success: true }),
          layout,
        },
      },
    });

    renderWithProviders(<OfficeWorkbench />, {
      initialEntries: ["/office-workbench"],
    });

    expect(await screen.findByText("startup.docx")).toBeInTheDocument();
    await waitFor(() => expect(requestAnimationFrame).toHaveBeenCalled());
    expect(layout).not.toHaveBeenCalled();

    const host = screen
      .getByLabelText("本地 Office 工作台")
      .querySelector("div[class*='editorHost']") as HTMLDivElement;
    expect(host).not.toBeNull();
    vi.spyOn(host, "getBoundingClientRect").mockReturnValue({
      x: 80,
      y: 120,
      left: 80,
      top: 120,
      right: 980,
      bottom: 720,
      width: 900,
      height: 600,
      toJSON: () => ({}),
    });
    fireEvent(window, new Event("pageshow"));
    await waitFor(() =>
      expect(layout).toHaveBeenCalledWith({
        workbench_id: "cold-start-docx",
        bounds: { x: 80, y: 120, width: 900, height: 600 },
      }),
    );
    expect(requestAnimationFrame).toHaveBeenCalled();
  });

  it("surfaces a resolved layout failure without claiming the file was untouched", async () => {
    const layout = vi.fn().mockResolvedValue({
      success: false,
      error: "编辑器窗口布局失败",
      error_code: "office_workbench_window_unavailable",
    });
    vi.spyOn(window, "requestAnimationFrame").mockReturnValue(42);
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        officeWorkbench: {
          getState: vi.fn().mockResolvedValue({
            active: true,
            visible: false,
            workbench_id: "layout-failure-docx",
            kind: "docs",
            display_name: "layout.docx",
            source_chat_id: "",
            route: "/office-workbench",
          }),
          hide: vi.fn().mockResolvedValue({ success: true }),
          layout,
        },
      },
    });

    renderWithProviders(<OfficeWorkbench />, {
      initialEntries: ["/office-workbench"],
    });
    expect(await screen.findByText("layout.docx")).toBeInTheDocument();
    const host = screen
      .getByLabelText("本地 Office 工作台")
      .querySelector("div[class*='editorHost']") as HTMLDivElement;
    vi.spyOn(host, "getBoundingClientRect").mockReturnValue({
      x: 80,
      y: 120,
      left: 80,
      top: 120,
      right: 980,
      bottom: 720,
      width: 900,
      height: 600,
      toJSON: () => ({}),
    });

    fireEvent(window, new Event("pageshow"));

    expect(await screen.findByText("编辑器视图不可用")).toBeInTheDocument();
    expect(screen.getByText(/保存状态需要在原文件中核对/)).toBeInTheDocument();
    expect(screen.queryByText(/文件没有被保存或覆盖/)).toBeNull();
  });
});
