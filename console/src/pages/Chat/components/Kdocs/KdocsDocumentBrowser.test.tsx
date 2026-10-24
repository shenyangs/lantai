import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type {
  KdocsDocumentItem,
  KdocsDocumentListResponse,
} from "../../../../api/types/kdocs";
import KdocsDocumentBrowser from "./KdocsDocumentBrowser";

const mocks = vi.hoisted(() => ({
  listDocuments: vi.fn(),
}));

vi.mock("../../../../api/modules/kdocs", () => ({
  kdocsApi: { listDocuments: mocks.listDocuments },
}));

function item(fileId: string, name: string): KdocsDocumentItem {
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
  };
}

function response(
  items: KdocsDocumentItem[],
  nextPageToken = "",
): KdocsDocumentListResponse {
  return {
    view: "recent",
    items,
    next_page_token: nextPageToken,
    total: items.length,
    parent: { drive_id: "", file_id: "" },
  };
}

describe("KDocs document browser", () => {
  it("keeps selection and document opening as sibling controls", async () => {
    mocks.listDocuments.mockResolvedValue(
      response([item("brief", "项目简报.docx")]),
    );

    render(<KdocsDocumentBrowser onOpen={vi.fn()} onToggle={vi.fn()} />);

    const checkbox = await screen.findByRole("checkbox", {
      name: "选择项目简报.docx",
    });
    const openButton = screen.getByRole("button", {
      name: /打开项目简报\.docx/,
    });

    expect(checkbox.closest('[role="button"]')).toBeNull();
    expect(openButton.contains(checkbox)).toBe(false);
  });

  it("drops a stale pagination response after the view changes", async () => {
    let resolvePage: (value: KdocsDocumentListResponse) => void = () =>
      undefined;
    const stalePage = new Promise<KdocsDocumentListResponse>((resolve) => {
      resolvePage = resolve;
    });
    mocks.listDocuments.mockImplementation(
      (params: { view: string; page_token?: string }) => {
        if (params.page_token === "next") return stalePage;
        if (params.view === "my")
          return Promise.resolve(response([item("my", "我的方案.docx")]));
        return Promise.resolve(
          response([item("recent", "最近周报.docx")], "next"),
        );
      },
    );

    render(<KdocsDocumentBrowser onOpen={vi.fn()} onItemsChange={vi.fn()} />);

    await screen.findByText("最近周报.docx");
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    fireEvent.click(screen.getByRole("button", { name: "我的文档" }));
    await screen.findByText("我的方案.docx");

    resolvePage(response([item("stale", "过期分页.docx")]));
    await waitFor(() => expect(screen.queryByText("过期分页.docx")).toBeNull());
    expect(screen.getByText("我的方案.docx")).toBeTruthy();
  });
});
