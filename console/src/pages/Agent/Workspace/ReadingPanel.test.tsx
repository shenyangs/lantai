import { screen, fireEvent, waitFor, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../test/common_setup";
import ReadingPanel from "./ReadingPanel";
import { readingApi, type ReadingEntry } from "@/api/modules/reading";
import { CREATE_NEW_CHAT_CONTEXT_KEY } from "@/pages/Chat/chatSessionEvents";

vi.mock("@/api/modules/reading", () => ({
  readingApi: {
    feeds: vi.fn(),
    entries: vi.fn(),
    entry: vi.fn(),
    update: vi.fn(),
    fetch: vi.fn(),
    knowledge: vi.fn(),
    subscribe: vi.fn(),
    save: vi.fn(),
    refresh: vi.fn(),
    updateFeed: vi.fn(),
    unsubscribe: vi.fn(),
    archiveEntries: vi.fn(),
  },
}));
const article: ReadingEntry = {
  id: "a",
  url: "https://example.com/a",
  title: "阅读与研究",
  body: "正文证据",
  excerpt: "正文证据",
  body_kind: "summary",
  published: "",
  created_at: 1,
  fetched_at: null,
  is_read: false,
  starred: false,
  tags: [],
};

describe("Reading library", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    sessionStorage.clear();
    vi.mocked(readingApi.feeds).mockResolvedValue([]);
    vi.mocked(readingApi.entries).mockResolvedValue({
      items: [article],
      total: 1,
    });
    vi.mocked(readingApi.entry).mockResolvedValue(article);
  });
  it("brings the reader into view when navigation narrows a wide window", async () => {
    const scrollIntoView = vi.fn();
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    const panel = screen.getByRole("region", { name: "阅读与订阅" });
    Object.defineProperty(panel, "clientWidth", { value: 994 });
    screen.getByRole("article").scrollIntoView = scrollIntoView;
    fireEvent.click(await screen.findByRole("button", { name: "阅读与研究" }));
    await screen.findByRole("heading", { name: "阅读与研究" });
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" });
  });

  it("keeps the wide three-column reader in place when an article opens", async () => {
    const scrollIntoView = vi.fn();
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    const panel = screen.getByRole("region", { name: "阅读与订阅" });
    Object.defineProperty(panel, "clientWidth", { value: 1400 });
    screen.getByRole("article").scrollIntoView = scrollIntoView;
    fireEvent.click(await screen.findByRole("button", { name: "阅读与研究" }));
    await screen.findByRole("heading", { name: "阅读与研究" });
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
  it("reads without implicit model calls or knowledge ingestion and keeps failed fetch content", async () => {
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "阅读与研究" }));
    await screen.findByRole("heading", { name: "阅读与研究" });
    expect(readingApi.knowledge).not.toHaveBeenCalled();
    expect(readingApi.update).not.toHaveBeenCalled();
    vi.mocked(readingApi.fetch).mockRejectedValue(new Error("正文读取失败"));
    fireEvent.click(screen.getByRole("button", { name: "读取全文" }));
    await screen.findByText("正文读取失败");
    expect(screen.getByRole("article")).toHaveTextContent("正文证据");
  });
  it("does not navigate to discussion when knowledge ingestion fails", async () => {
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "阅读与研究" }));
    await screen.findByRole("heading", { name: "阅读与研究" });
    vi.mocked(readingApi.knowledge).mockRejectedValue(new Error("入库失败"));
    fireEvent.click(screen.getByRole("button", { name: "入库并讨论" }));
    await screen.findByText(/已入库 0\/1 篇.*入库失败/);
    expect(sessionStorage.getItem(CREATE_NEW_CHAT_CONTEXT_KEY)).toBeNull();
  });
  it("ignores old article requests after another article is selected", async () => {
    const second = { ...article, id: "b", title: "第二篇", body: "第二篇正文" };
    vi.mocked(readingApi.entries).mockResolvedValue({
      items: [article, second],
      total: 2,
    });
    let resolveFirst!: (entry: ReadingEntry) => void;
    vi.mocked(readingApi.entry).mockImplementation((id) =>
      id === "a"
        ? new Promise((resolve) => {
            resolveFirst = resolve;
          })
        : Promise.resolve(second),
    );
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "阅读与研究" }));
    fireEvent.click(screen.getByRole("button", { name: "第二篇" }));
    await screen.findByRole("heading", { name: "第二篇" });
    resolveFirst(article);
    await waitFor(() =>
      expect(screen.getByRole("article")).toHaveTextContent("第二篇正文"),
    );
    expect(screen.getByRole("article")).not.toHaveTextContent("正文证据");
  });
  it("does not let a late failed list overwrite a newer successful filter", async () => {
    let rejectFirst!: (reason: Error) => void;
    vi.mocked(readingApi.entries).mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectFirst = reject;
        }),
    );
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "未读", exact: true }));
    await screen.findByRole("button", { name: "阅读与研究" });
    await act(async () => rejectFirst(new Error("旧请求失败")));
    expect(screen.queryByText("旧请求失败")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "阅读与研究" }),
    ).toBeInTheDocument();
  });
  it("only saves a subscription folder on explicit save, and supports cancel", async () => {
    vi.mocked(readingApi.feeds).mockResolvedValue([
      {
        id: "feed",
        url: "https://example.com/rss",
        title: "周刊",
        folder: "旧分组",
        paused: false,
        checked_at: null,
        error: "",
        unread: 1,
      },
    ]);
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /周刊/ }));
    const field = await screen.findByLabelText("订阅分组");
    fireEvent.change(field, { target: { value: "新分组" } });
    fireEvent.blur(field);
    expect(readingApi.updateFeed).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "取消修改" }));
    expect(field).toHaveValue("旧分组");
    fireEvent.change(field, { target: { value: "新分组" } });
    fireEvent.click(screen.getByRole("button", { name: "保存分组" }));
    await waitFor(() =>
      expect(readingApi.updateFeed).toHaveBeenCalledWith("feed", {
        folder: "新分组",
      }),
    );
  });
  it("reports confirmed partial ingestion and leaves discussion unopened", async () => {
    const second = { ...article, id: "b", title: "第二篇" };
    vi.mocked(readingApi.entries).mockResolvedValue({
      items: [article, second],
      total: 2,
    });
    vi.mocked(readingApi.knowledge)
      .mockResolvedValueOnce({
        imported: [
          {
            filename: "first.md",
            path: ".wiki/raw/first.md",
            size: 20,
            created_time: "",
            modified_time: "",
            updated_at: 0,
            kind: "markdown",
            extension: ".md",
            sha256: "hash",
            original_path: "first.md",
            linked_pages: [],
            status: "ready",
          },
        ],
        skipped: [],
        pages: [],
      })
      .mockRejectedValueOnce(new Error("第二篇读取失败"));
    const changed = vi.fn();
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={changed} onOpenChannels={vi.fn()} />,
    );
    fireEvent.click(
      await screen.findByRole("checkbox", { name: "选择 阅读与研究" }),
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 第二篇" }));
    fireEvent.click(screen.getByRole("button", { name: /入库并讨论（2）/ }));
    await screen.findByText(/已入库 1\/2 篇.*first.md.*第二篇读取失败/);
    expect(changed).toHaveBeenCalledOnce();
    expect(sessionStorage.getItem(CREATE_NEW_CHAT_CONTEXT_KEY)).toBeNull();
  });
  it("hands a successfully imported source to the existing exact-scope chat flow", async () => {
    vi.mocked(readingApi.knowledge).mockResolvedValue({
      imported: [
        {
          filename: "reading.md",
          display_title: "阅读与研究",
          path: ".wiki/raw/reading.md",
          size: 20,
          created_time: "",
          modified_time: "",
          updated_at: 0,
          kind: "markdown",
          extension: ".md",
          sha256: "hash",
          original_path: "reading.md",
          linked_pages: [],
          status: "ready",
        },
      ],
      skipped: [],
      pages: [],
    });
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "阅读与研究" }));
    await screen.findByRole("heading", { name: "阅读与研究" });
    fireEvent.click(
      screen.getByRole("button", { name: "入库并讨论", exact: true }),
    );
    await waitFor(() =>
      expect(
        sessionStorage.getItem(CREATE_NEW_CHAT_CONTEXT_KEY),
      ).not.toBeNull(),
    );
    const handoff = JSON.parse(
      sessionStorage.getItem(CREATE_NEW_CHAT_CONTEXT_KEY)!,
    );
    expect(handoff.knowledgeScopeMode).toBe("exact");
    expect(handoff.knowledgeSources).toEqual([
      {
        path: ".wiki/raw/reading.md",
        filename: "阅读与研究",
        kind: "markdown",
      },
    ]);
  });
  it("clearing search restores the full list", async () => {
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    await screen.findByRole("button", { name: "阅读与研究" });
    const input = screen.getByLabelText("搜索文章和标签");
    fireEvent.change(input, { target: { value: "研究" } });
    fireEvent.click(screen.getByRole("button", { name: "search" }));
    await waitFor(() =>
      expect(readingApi.entries).toHaveBeenLastCalledWith(
        expect.objectContaining({ q: "研究" }),
      ),
    );
    fireEvent.change(input, { target: { value: "" } });
    await waitFor(() =>
      expect(readingApi.entries).toHaveBeenLastCalledWith(
        expect.objectContaining({ q: "" }),
      ),
    );
  });
  it("keeps article deletion visible from the list and moves it to trash", async () => {
    vi.mocked(readingApi.archiveEntries).mockResolvedValue({ archived: 1 });
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "删除 阅读与研究" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "移到回收站" }));
    await waitFor(() =>
      expect(readingApi.archiveEntries).toHaveBeenCalledWith(["a"]),
    );
  });
  it("offers a clear return from the mobile reading state", async () => {
    renderWithProviders(
      <ReadingPanel onKnowledgeChange={vi.fn()} onOpenChannels={vi.fn()} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "阅读与研究" }));
    await screen.findByRole("heading", { name: "阅读与研究" });
    fireEvent.click(screen.getByRole("button", { name: "返回文章列表" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("heading", { name: "阅读与研究" }),
      ).not.toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: "阅读与研究" }),
    ).toBeInTheDocument();
  });
});
