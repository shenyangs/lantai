import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/common_setup";
import { RuntimeFileCard } from "./StructuredContent";

const { mockAddWork } = vi.hoisted(() => ({ mockAddWork: vi.fn() }));

vi.mock("../../../api/modules/works", () => ({
  worksApi: { add: mockAddWork },
}));

describe("RuntimeFileCard", () => {
  beforeEach(() => {
    mockAddWork.mockReset();
    mockAddWork.mockResolvedValue({ id: "work-1" });
    window.location.hash = "#/chat/chat-source-1";
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      openOfficeEditor: vi.fn().mockResolvedValue({ success: true }),
      openDocxEditor: vi.fn().mockResolvedValue({ success: true }),
      openPath: vi.fn().mockResolvedValue({ success: true }),
      revealPath: vi.fn().mockResolvedValue({ success: true }),
      openWith: vi.fn().mockResolvedValue({ success: true }),
    };
  });

  it("keeps the local source for native open-with", async () => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(
      <RuntimeFileCard
        url="/api/files/preview/tmp/report.md"
        sourceUrl="file:///tmp/report.md"
        name="市场策略分析.md"
        size={4096}
      />,
    );

    expect(
      screen.getByRole("button", {
        name: "在澜台中编辑：市场策略分析.md",
      }),
    ).toBeInTheDocument();

    const card = container.querySelector('[data-lantai-artifact="file"]');
    expect(card).not.toBeNull();
    fireEvent.contextMenu(card as Element);
    await user.click(await screen.findByText("选择应用打开…"));

    expect(window.electronAPI?.openWith).toHaveBeenCalledWith("/tmp/report.md");
  });

  it("lets the user keep a generated file in Works without a blocking prompt", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <RuntimeFileCard
        url="/api/files/preview/tmp/game.html"
        sourceUrl="file:///tmp/game.html"
        name="俄罗斯方块.html"
      />,
    );

    await user.click(screen.getByRole("button", { name: "加入作品" }));

    expect(mockAddWork).toHaveBeenCalledWith({
      path: "/tmp/game.html",
      title: "俄罗斯方块",
      source_chat_id: "chat-source-1",
    });
    expect(screen.getByRole("button", { name: "已加入作品" })).toBeDisabled();
  });

  it("opens a local DOCX deliverable in the Lantai editor", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <RuntimeFileCard
        url="/api/files/preview/tmp/report.docx"
        sourceUrl="file:///tmp/report.docx"
        name="市场策略分析.docx"
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "在澜台中编辑：市场策略分析.docx" }),
    );

    expect(window.electronAPI?.openOfficeEditor).toHaveBeenCalledWith(
      "/tmp/report.docx",
      { source_chat_id: "chat-source-1", agent_id: "default" },
    );
    expect(window.electronAPI?.openPath).not.toHaveBeenCalled();
  });

  it("uses the Lantai editor from the DOCX action icon", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <RuntimeFileCard
        url="/api/files/preview/tmp/report.docx"
        sourceUrl="file:///tmp/report.docx"
        name="市场策略分析.docx"
      />,
    );

    await user.click(screen.getByRole("button", { name: "在澜台中编辑" }));

    expect(window.electronAPI?.openOfficeEditor).toHaveBeenCalledWith(
      "/tmp/report.docx",
      { source_chat_id: "chat-source-1", agent_id: "default" },
    );
    expect(window.electronAPI?.openPath).not.toHaveBeenCalled();
  });

  it("opens a local presentation in the Lantai editor on primary click", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <RuntimeFileCard
        url="/api/files/preview/tmp/deck.pptx"
        sourceUrl="file:///tmp/deck.pptx"
        name="季度复盘.pptx"
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "在澜台中编辑：季度复盘.pptx" }),
    );

    expect(window.electronAPI?.openOfficeEditor).toHaveBeenCalledWith(
      "/tmp/deck.pptx",
      { source_chat_id: "chat-source-1", agent_id: "default" },
    );
    expect(window.electronAPI?.openPath).not.toHaveBeenCalled();
  });

  it("saves a local Agent deliverable through the native file copy bridge", async () => {
    const user = userEvent.setup();
    const saveFileAs = vi.fn().mockResolvedValue({
      success: true,
      path: "/Users/test/Downloads/季度复盘.pptx",
      size_bytes: 4096,
    });
    (window.electronAPI as any).saveFileAs = saveFileAs;
    renderWithProviders(
      <RuntimeFileCard
        url="/api/files/preview/tmp/deck.pptx"
        sourceUrl="file:///tmp/deck.pptx"
        name="季度复盘.pptx"
        size={4096}
      />,
    );

    await user.click(screen.getByLabelText("下载文件"));

    await waitFor(() =>
      expect(saveFileAs).toHaveBeenCalledWith({
        sourcePath: "/tmp/deck.pptx",
        filename: "季度复盘.pptx",
      }),
    );
  });
});
