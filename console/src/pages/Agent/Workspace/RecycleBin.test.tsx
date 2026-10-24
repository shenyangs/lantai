import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { Modal } from "antd";
import { renderWithProviders } from "../../../test/common_setup";
import RecycleBin from "./RecycleBin";
import { workspaceApi } from "@/api/modules/workspace";
import { readingApi } from "@/api/modules/reading";

vi.mock("@/api/modules/workspace", () => ({
  workspaceApi: {
    listWikiTrash: vi.fn(),
    restoreWikiTrashItem: vi.fn(),
    purgeWikiTrashItem: vi.fn(),
  },
}));
vi.mock("@/api/modules/reading", () => ({
  readingApi: {
    entries: vi.fn(),
    restoreEntry: vi.fn(),
    purgeEntry: vi.fn(),
  },
}));

beforeEach(() => {
  Modal.destroyAll();
  vi.resetAllMocks();
  vi.mocked(workspaceApi.listWikiTrash).mockResolvedValue([
    {
      id: "page-trash",
      kind: "page",
      title: "研究结论",
      original_path: "queries/result.md",
      archived_at: "2026-09-08T10:00:00Z",
      related_count: 0,
    },
  ]);
  vi.mocked(readingApi.entries).mockResolvedValue({
    items: [
      {
        id: "article-trash",
        url: "https://example.com/a",
        title: "稍后阅读",
        body_kind: "page",
        published: "",
        created_at: 1,
        fetched_at: null,
        is_read: true,
        starred: true,
        tags: [],
        archived_at: 1,
      },
    ],
    total: 1,
  });
});

it("shows all deleted content in one place and restores by original system", async () => {
  const changed = vi.fn();
  vi.mocked(workspaceApi.restoreWikiTrashItem).mockResolvedValue({
    kind: "page",
    item: {},
  });
  vi.mocked(readingApi.restoreEntry).mockResolvedValue({
    id: "article-trash",
    url: "https://example.com/a",
    title: "稍后阅读",
    body_kind: "page",
    published: "",
    created_at: 1,
    fetched_at: null,
    is_read: true,
    starred: true,
    tags: [],
  });
  renderWithProviders(<RecycleBin onRestore={changed} />);
  expect(await screen.findByText("研究结论")).toBeInTheDocument();
  expect(screen.getByText("稍后阅读")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "恢复 研究结论" }));
  await waitFor(() =>
    expect(workspaceApi.restoreWikiTrashItem).toHaveBeenCalledWith(
      "page-trash",
    ),
  );
  expect(changed).toHaveBeenCalled();
});

it("requires an explicit destructive confirmation before permanent deletion", async () => {
  vi.mocked(workspaceApi.purgeWikiTrashItem).mockResolvedValue({
    deleted: true,
    id: "page-trash",
  });
  renderWithProviders(<RecycleBin onRestore={vi.fn()} />);
  fireEvent.click(
    await screen.findByRole("button", { name: "彻底删除 研究结论" }),
  );
  expect(workspaceApi.purgeWikiTrashItem).not.toHaveBeenCalled();
  fireEvent.click(
    await screen.findByRole("button", { name: "彻底删除", exact: true }),
  );
  await waitFor(() =>
    expect(workspaceApi.purgeWikiTrashItem).toHaveBeenCalledWith("page-trash"),
  );
});
