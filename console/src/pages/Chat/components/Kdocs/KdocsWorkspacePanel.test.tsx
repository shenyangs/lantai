import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { KdocsDocumentItem } from "../../../../api/types/kdocs";
import type { KdocsReference } from "../../kdocsReferences";
import KdocsWorkspacePanel from "./KdocsWorkspacePanel";

const mocks = vi.hoisted(() => ({
  getDocumentInfo: vi.fn(),
  getDocumentLink: vi.fn(),
  openExternalUrl: vi.fn(),
  readKdocsDocumentSafely: vi.fn(),
}));

vi.mock("../../../../api/modules/kdocs", () => ({
  kdocsApi: {
    getDocumentInfo: mocks.getDocumentInfo,
    getDocumentLink: mocks.getDocumentLink,
  },
}));

vi.mock("../../../../utils/openExternalUrl", () => ({
  openExternalUrl: mocks.openExternalUrl,
}));

vi.mock("../../kdocsRead", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../kdocsRead")>();
  return {
    ...original,
    readKdocsDocumentSafely: mocks.readKdocsDocumentSafely,
  };
});

vi.mock("./KdocsDocumentBrowser", () => ({
  default: () => <div data-testid="document-browser" />,
}));

const document: KdocsDocumentItem = {
  key: "drive-a:file-a",
  drive_id: "drive-a",
  file_id: "file-a",
  parent_id: "0",
  name: "经营周报.docx",
  type: "docx",
  suffix: "docx",
  is_folder: false,
  size: 100,
  version: "7",
  content_hash: "hash-7",
  link_id: "",
  original_url: "https://www.kdocs.cn/l/weekly",
  location: "我的云文档 / 周报",
  owner: null,
  modifier: null,
  shared: false,
  permissions: { can_read: true, can_edit: true },
};

const storedReference: KdocsReference = {
  schema_version: 1,
  key: "drive-a:file-a:file",
  kind: "file",
  drive_id: "drive-a",
  file_id: "file-a",
  name_snapshot: document.name,
  path_snapshot: document.location,
  suffix: document.suffix,
  original_url: document.original_url,
  read_status: "read",
  read_snapshot: {
    version: "7",
    content_hash: "hash-7",
    read_at: "2026-09-20T08:00:00.000Z",
  },
  content_snapshot: {
    text: "# 发送时的周报\n\n[查看资料](https://example.com/source)",
    format: "markdown",
    truncated: false,
  },
};

function renderPanel(reference = storedReference) {
  return render(
    <KdocsWorkspacePanel
      open
      references={[reference]}
      requestedDocument={document}
      onRequestedDocumentHandled={vi.fn()}
      onClose={vi.fn()}
      onAddDocument={vi.fn()}
      onAddExcerpt={vi.fn()}
      onRemoveDocument={vi.fn()}
      onMarkDocumentUpdated={vi.fn()}
    />,
  );
}

describe("KDocs workspace reader", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.openExternalUrl.mockResolvedValue(undefined);
  });

  it("reopens the stored message snapshot without silently reading a newer version", async () => {
    renderPanel();

    expect(
      await screen.findByRole("heading", { name: "发送时的周报" }),
    ).toBeTruthy();
    expect(mocks.readKdocsDocumentSafely).not.toHaveBeenCalled();
  });

  it("explains a formatting-limited snapshot as usable content", async () => {
    renderPanel({
      ...storedReference,
      read_status: "partial",
      error_message:
        "For fine-grained OTL ops or full formatting, use otl.block_query",
    });

    expect(
      await screen.findByText(
        "正文已经可以使用；部分块级结构、精细排版或嵌入内容未展开。需要完整呈现时，请查看在线原文。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/otl\.block_query/)).not.toBeInTheDocument();
  });

  it("routes links in rendered document content through safe external navigation", async () => {
    renderPanel();

    fireEvent.click(await screen.findByRole("link", { name: "查看资料" }));

    await waitFor(() =>
      expect(mocks.openExternalUrl).toHaveBeenCalledWith(
        "https://example.com/source",
      ),
    );
  });

  it("keeps comparing online versions with the stored read snapshot", async () => {
    const onMarkDocumentUpdated = vi.fn();
    mocks.getDocumentInfo.mockResolvedValue({
      ...document,
      version: "8",
      content_hash: "hash-8",
    });
    render(
      <KdocsWorkspacePanel
        open
        references={[storedReference]}
        requestedDocument={document}
        onRequestedDocumentHandled={vi.fn()}
        onClose={vi.fn()}
        onAddDocument={vi.fn()}
        onAddExcerpt={vi.fn()}
        onRemoveDocument={vi.fn()}
        onMarkDocumentUpdated={onMarkDocumentUpdated}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "检查更新" }));
    expect(await screen.findByText("在线原文有新版本")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "检查更新" }));

    await waitFor(() => expect(onMarkDocumentUpdated).toHaveBeenCalledTimes(2));
  });

  it("refreshes reading without changing draft references", async () => {
    const onAddDocument = vi.fn();
    mocks.getDocumentInfo.mockResolvedValue({ ...document, version: "8" });
    mocks.readKdocsDocumentSafely.mockResolvedValue({
      ...storedReference,
      content_snapshot: { text: "新版", format: "markdown", truncated: false },
    });
    render(
      <KdocsWorkspacePanel
        open
        references={[storedReference]}
        requestedDocument={document}
        onRequestedDocumentHandled={vi.fn()}
        onClose={vi.fn()}
        onAddDocument={onAddDocument}
        onAddExcerpt={vi.fn()}
        onRemoveDocument={vi.fn()}
        onMarkDocumentUpdated={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "检查更新" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /重新读取|读取最新版/ }),
    );
    await screen.findByText("新版");
    expect(onAddDocument).not.toHaveBeenCalled();
  });

  it("does not claim unchanged when the source has no version information", async () => {
    mocks.getDocumentInfo.mockResolvedValue({
      ...document,
      version: "",
      content_hash: "",
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "检查更新" }));
    expect(
      await screen.findByText(/来源未提供版本信息，暂时无法确认是否更新/),
    ).toBeInTheDocument();
    expect(screen.queryByText("已核对，原文未变化")).toBeNull();
  });

  it("preserves the existing snapshot when refreshing fails", async () => {
    mocks.getDocumentInfo.mockResolvedValue({ ...document, version: "8" });
    mocks.readKdocsDocumentSafely.mockResolvedValue({
      ...storedReference,
      read_status: "failed",
      error_message: "暂时无法读取",
      content_snapshot: undefined,
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "检查更新" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /重新读取|读取最新版/ }),
    );
    expect(await screen.findByText("暂时无法读取")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "发送时的周报" }),
    ).toBeInTheDocument();
  });

  it("preserves search when expanding reading", async () => {
    renderPanel();
    fireEvent.change(await screen.findByRole("textbox", { name: "文内搜索" }), {
      target: { value: "周报" },
    });
    fireEvent.click(screen.getByRole("button", { name: "展开阅读" }));
    expect(screen.getByRole("textbox", { name: "文内搜索" })).toHaveValue(
      "周报",
    );
    expect(screen.getByRole("status")).toHaveTextContent("1 / 1");
    fireEvent.click(screen.getByRole("button", { name: "收起阅读" }));
    expect(screen.getByRole("textbox", { name: "文内搜索" })).toHaveValue(
      "周报",
    );
  });

  it("treats the narrow workspace as a dismissible focus boundary", async () => {
    const originalResizeObserver = globalThis.ResizeObserver;
    class NarrowHostResizeObserver implements ResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe(target: Element) {
        this.callback(
          [
            {
              target,
              contentRect: { width: 900 } as DOMRectReadOnly,
            } as ResizeObserverEntry,
          ],
          this,
        );
      }
      unobserve() {}
      disconnect() {}
    }
    globalThis.ResizeObserver = NarrowHostResizeObserver;

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            打开云文档
          </button>
          <KdocsWorkspacePanel
            open={open}
            references={[]}
            requestedDocument={null}
            onRequestedDocumentHandled={vi.fn()}
            onClose={() => setOpen(false)}
            onAddDocument={vi.fn()}
            onAddExcerpt={vi.fn()}
            onRemoveDocument={vi.fn()}
            onMarkDocumentUpdated={vi.fn()}
          />
        </>
      );
    }

    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "打开云文档" });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = await screen.findByRole("dialog", { name: "金山文档" });
    expect(dialog).toHaveAttribute("data-layout", "overlay");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "关闭金山文档" }),
      ).toHaveFocus(),
    );

    fireEvent.keyDown(globalThis.document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(trigger).toHaveFocus();

    globalThis.ResizeObserver = originalResizeObserver;
  });

  it("closes the split workspace with Escape and restores its trigger", async () => {
    const originalResizeObserver = globalThis.ResizeObserver;
    class WideHostResizeObserver implements ResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe(target: Element) {
        this.callback(
          [
            {
              target,
              contentRect: { width: 1400 } as DOMRectReadOnly,
            } as ResizeObserverEntry,
          ],
          this,
        );
      }
      unobserve() {}
      disconnect() {}
    }
    globalThis.ResizeObserver = WideHostResizeObserver;

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            打开云文档
          </button>
          <KdocsWorkspacePanel
            open={open}
            references={[]}
            requestedDocument={null}
            onRequestedDocumentHandled={vi.fn()}
            onClose={() => setOpen(false)}
            onAddDocument={vi.fn()}
            onAddExcerpt={vi.fn()}
            onRemoveDocument={vi.fn()}
            onMarkDocumentUpdated={vi.fn()}
          />
        </>
      );
    }

    try {
      render(<Harness />);
      const trigger = screen.getByRole("button", { name: "打开云文档" });
      trigger.focus();
      fireEvent.click(trigger);

      const panel = await screen.findByLabelText("金山文档");
      expect(panel).toHaveAttribute("data-layout", "split");
      expect(panel).not.toHaveAttribute("aria-modal");

      fireEvent.keyDown(globalThis.document, { key: "Escape" });
      await waitFor(() =>
        expect(screen.queryByLabelText("金山文档")).toBeNull(),
      );
      expect(trigger).toHaveFocus();
    } finally {
      globalThis.ResizeObserver = originalResizeObserver;
    }
  });
});
