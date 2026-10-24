import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  KdocsDocumentItem,
  KdocsDocumentListResponse,
} from "../../../../api/types/kdocs";
import KdocsPicker from "./KdocsPicker";

const mocks = vi.hoisted(() => ({
  listDocuments: vi.fn(),
}));

vi.mock("../../../../api/modules/kdocs", () => ({
  kdocsApi: { listDocuments: mocks.listDocuments },
}));

function item(
  fileId: string,
  name: string,
  overrides: Partial<KdocsDocumentItem> = {},
): KdocsDocumentItem {
  return {
    key: `drive-a:${fileId}`,
    drive_id: "drive-a",
    file_id: fileId,
    parent_id: "0",
    name,
    type: "docx",
    suffix: "docx",
    is_folder: false,
    size: 24,
    version: "1",
    content_hash: `hash-${fileId}`,
    link_id: "",
    original_url: "",
    location: "我的云文档",
    owner: null,
    modifier: null,
    shared: false,
    permissions: { can_read: true },
    ...overrides,
  };
}

function response(items: KdocsDocumentItem[]): KdocsDocumentListResponse {
  return {
    view: "recent",
    items,
    next_page_token: "",
    total: items.length,
    parent: { drive_id: "", file_id: "" },
  };
}

const first = item("file-a", "经营周报.docx");
const referenced = item("file-r", "已引用方案.docx");
const folder = item("folder-a", "资料库", {
  is_folder: true,
  type: "folder",
  suffix: "",
});
const nested = item("file-b", "预算表.xlsx", {
  parent_id: "folder-a",
  type: "xlsx",
  suffix: "xlsx",
});

describe("KDocs picker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.listDocuments.mockImplementation(
      async (params: { view: string }): Promise<KdocsDocumentListResponse> =>
        params.view === "folder"
          ? response([nested])
          : response([first, referenced, folder]),
    );
  });

  it("keeps selections across folders and excludes already referenced documents", async () => {
    const onConfirm = vi.fn();
    render(
      <KdocsPicker
        open
        referencedKeys={new Set([referenced.key])}
        onClose={vi.fn()}
        onConfirm={onConfirm}
        onPreview={vi.fn()}
      />,
    );

    const search = await screen.findByRole("textbox", {
      name: "搜索金山文档",
    });
    await waitFor(() => expect(document.activeElement).toBe(search));

    const locked = screen.getByRole("checkbox", {
      name: `已引用${referenced.name}`,
    });
    expect(locked).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "选择当前列表" }));
    expect(screen.getByText("已选 1 项")).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: `打开文件夹${folder.name}` }),
    );

    await screen.findByText(nested.name);
    expect(screen.getByText("已选 1 项")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("checkbox", { name: `选择${nested.name}` }),
    );
    expect(screen.getByText("已选 2 项")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "添加 (2)" }));
    expect(onConfirm).toHaveBeenCalledWith([first, nested]);
  });

  it("closes with Escape from inside the dialog", async () => {
    const onClose = vi.fn();
    render(
      <KdocsPicker
        open
        referencedKeys={new Set()}
        onClose={onClose}
        onConfirm={vi.fn()}
        onPreview={vi.fn()}
      />,
    );

    const search = await screen.findByRole("textbox", {
      name: "搜索金山文档",
    });
    fireEvent.keyDown(search, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
});
