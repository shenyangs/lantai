import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../test/common_setup";
import type { WikiSearchHit } from "../../../api/types";
import WorkspacePage from "./index";

const mock = vi.hoisted(() => ({
  handleSearch: vi.fn(),
  handleResolveReview: vi.fn(),
  handleRunIngest: vi.fn(),
  handleFileClick: vi.fn(),
  fetchFiles: vi.fn(),
  uploadWikiSource: vi.fn(),
  importWikiUrl: vi.fn(),
  loadWikiSourceContent: vi.fn(),
  updateWikiSource: vi.fn(),
  createWikiCollection: vi.fn(),
  archiveWikiSource: vi.fn(),
  listArchivedWikiSources: vi.fn(),
  restoreArchivedWikiSource: vi.fn(),
  selectedAgent: "default",
  deleteWikiPage: vi.fn(),
  listWikiTrash: vi.fn(),
  restoreWikiTrashItem: vi.fn(),
  purgeWikiTrashItem: vi.fn(),
  searchResults: null as { query: string; hits: WikiSearchHit[] } | null,
}));

vi.mock("../../../stores/agentStore", () => ({
  useAgentStore: () => ({ selectedAgent: mock.selectedAgent }),
}));

const updated = Date.parse("2026-08-12T10:00:00Z");

vi.mock("../../../api/modules/workspace", () => ({
  workspaceApi: {
    uploadWikiSource: mock.uploadWikiSource,
    importWikiUrl: mock.importWikiUrl,
    loadWikiSourceContent: mock.loadWikiSourceContent,
    updateWikiSource: mock.updateWikiSource,
    createWikiCollection: mock.createWikiCollection,
    archiveWikiSource: mock.archiveWikiSource,
    listArchivedWikiSources: mock.listArchivedWikiSources,
    restoreArchivedWikiSource: mock.restoreArchivedWikiSource,
    deleteWikiPage: mock.deleteWikiPage,
    listWikiTrash: mock.listWikiTrash,
    restoreWikiTrashItem: mock.restoreWikiTrashItem,
    purgeWikiTrashItem: mock.purgeWikiTrashItem,
  },
}));

vi.mock("./components", () => ({
  FileEditor: ({ selectedFile }: { selectedFile: { title: string } }) => (
    <div data-testid="file-editor">{selectedFile.title}</div>
  ),
  useAgentsData: () => ({
    files: [
      {
        filename: "queries/poem.md",
        path: ".wiki/pages/queries/poem.md",
        size: 100,
        created_time: "2026-08-12T10:00:00Z",
        modified_time: "2026-08-12T10:00:00Z",
        updated_at: updated,
        title: "北京现代诗",
        summary: "从对话中整理出的研究笔记",
        links: [],
        backlinks: [],
        sources: [],
        page_type: "query",
        category: "queries",
      },
    ],
    sources: [
      {
        filename: "research.pdf",
        display_title:
          mock.selectedAgent === "agent-a"
            ? "A 同名资料"
            : mock.selectedAgent === "agent-b"
              ? "B 同名资料"
              : undefined,
        path: ".wiki/raw/sources/files/research.pdf",
        size: 1024,
        created_time: "2026-08-12T09:00:00Z",
        modified_time: "2026-08-12T09:00:00Z",
        updated_at: updated - 3600,
        kind: "binary",
        extension: ".pdf",
        sha256: "hash",
        original_path: "research.pdf",
        linked_pages: ["sources/research.md"],
        status: "ready",
        extraction_status: "text_extracted",
        extracted_chars: 420,
      },
      {
        filename: "budget.xlsx",
        display_title: "媒体预算",
        path: ".wiki/raw/sources/files/budget.xlsx",
        size: 2048,
        created_time: "2026-08-11T09:00:00Z",
        modified_time: "2026-08-11T09:00:00Z",
        updated_at: updated - 7200,
        kind: "office",
        extension: ".xlsx",
        sha256: "hash-2",
        original_path: "budget.xlsx",
        linked_pages: ["sources/budget.md"],
        status: "ready",
        extraction_status: "text_extracted",
        extracted_chars: 680,
      },
    ],
    collections: [],
    graph: { nodes: [], edges: [] },
    stats: {},
    queueSummary: {
      pending: 0,
      running: 0,
      finished: 1,
      failed: 0,
      cancelled: 0,
      total: 1,
      latest: [],
    },
    lint: { issues: [], stats: {} },
    insights: { insights: [], stats: {} },
    review: {
      items: [
        {
          id: "review-1",
          status: "pending",
          action: "deep-ingest-review",
          title: "确认整理结果",
          message: "请确认是否继续整理。",
          targets: [],
          created_at: "2026-08-12T10:00:00Z",
        },
      ],
      stats: { pending: 1, total: 1 },
    },
    schema: { pages: [], fields: [], stats: {} },
    purpose: {
      filename: "purpose.md",
      path: ".wiki/purpose.md",
      size: 0,
      created_time: "2026-08-12T10:00:00Z",
      modified_time: "2026-08-12T10:00:00Z",
      updated_at: updated,
      content: "# 知识库目的",
    },
    searchResults: mock.searchResults,
    searchLoading: false,
    selectedFile: null,
    fileContent: "",
    loading: false,
    deleting: false,
    workspacePath: "/tmp/workspace/.wiki",
    hasChanges: false,
    purposeContent: "# 知识库目的",
    purposeHasChanges: false,
    setFileContent: vi.fn(),
    setPurposeContent: vi.fn(),
    fetchFiles: mock.fetchFiles,
    handleSearch: mock.handleSearch,
    handleRunIngest: mock.handleRunIngest,
    handleResolveReview: mock.handleResolveReview,
    handleFileClick: mock.handleFileClick,
    handleSave: vi.fn(),
    handleReset: vi.fn(),
    handleDelete: vi.fn(),
    handlePurposeSave: vi.fn(),
    handlePurposeReset: vi.fn(),
  }),
}));

vi.mock("./ReadingPanel", () => ({
  default: () => <div>阅读分区</div>,
}));
vi.mock("./ChannelDiscovery", () => ({
  default: () => <div>频道分区</div>,
}));
vi.mock("./RecycleBin", () => ({
  default: () => <div>统一回收站</div>,
}));

describe("Knowledge workspace", () => {
  beforeEach(() => {
    Modal.destroyAll();
    vi.clearAllMocks();
    mock.uploadWikiSource.mockReset();
    mock.importWikiUrl.mockReset();
    mock.selectedAgent = "default";
    mock.searchResults = null;
    window.sessionStorage.clear();
    mock.loadWikiSourceContent.mockResolvedValue({
      path: ".wiki/raw/sources/files/research.pdf",
      title: "research.pdf",
      kind: "binary",
      content: "Research body",
      content_status: "text_extracted",
      message: "",
      page_count: 2,
      linked_pages: [],
    });
    mock.listArchivedWikiSources.mockResolvedValue([
      {
        id: "archive-1",
        source_path: ".wiki/raw/sources/files/old.pdf",
        display_title: "已移出的旧报告",
        archived_at: "2026-08-10T09:00:00Z",
        archived_pages: [],
      },
    ]);
    mock.restoreArchivedWikiSource.mockResolvedValue({
      filename: "old.pdf",
      display_title: "已移出的旧报告",
      path: ".wiki/raw/sources/files/old.pdf",
      modified_time: "2026-08-10T09:00:00Z",
    });
    mock.uploadWikiSource.mockResolvedValue({
      imported: [{ path: ".wiki/raw/sources/imported.txt" }],
      skipped: [],
      pages: [],
    });
    mock.importWikiUrl.mockResolvedValue({
      imported: [{ path: ".wiki/raw/sources/web/imported.md" }],
      skipped: [],
      pages: [],
    });
  });

  it("presents one task-oriented workspace instead of internal tabs", () => {
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    expect(
      screen.getByRole("button", { name: "全部内容" }),
    ).toBeInTheDocument();
    expect(screen.getByText("北京现代诗")).toBeInTheDocument();
    expect(screen.getByText("research.pdf")).toBeInTheDocument();
    expect(screen.queryByText("图谱")).not.toBeInTheDocument();
    expect(screen.queryByText("结构")).not.toBeInTheDocument();
    expect(screen.queryByText("检查")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "资料关系说明" }),
    ).toHaveTextContent("资料、知识、记忆和作品各自独立");
    expect(
      screen.getByRole("navigation", { name: "知识库分区" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "知识库" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "知识库设置" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "资料" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("button", { name: "阅读" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "频道" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "回收站" })).toBeInTheDocument();
  });

  it("uses one add entry for files and webpages", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    await user.click(screen.getByRole("button", { name: /添加资料/ }));

    expect(
      screen.getByRole("dialog", { name: "添加资料" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "上传文件" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "添加网页" }));
    expect(
      screen.getByRole("button", { name: "添加网页" }),
    ).toBeInTheDocument();
  });

  it("imports every file selected in one batch", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });
    await user.click(screen.getByRole("button", { name: /添加资料/ }));
    const input =
      document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();

    fireEvent.change(input!, {
      target: {
        files: [
          new File(["alpha"], "alpha.txt", { type: "text/plain" }),
          new File(["beta"], "beta.txt", { type: "text/plain" }),
        ],
      },
    });

    await waitFor(() => expect(mock.uploadWikiSource).toHaveBeenCalledTimes(2));
    expect(mock.uploadWikiSource.mock.calls.map(([file]) => file.name)).toEqual(
      ["alpha.txt", "beta.txt"],
    );
  });

  it("does not continue a multi-file import in a newly selected agent", async () => {
    let resolveFirstUpload: (value: {
      imported: { path: string }[];
      skipped: never[];
      pages: never[];
    }) => void = () => {};
    const firstUpload = new Promise<{
      imported: { path: string }[];
      skipped: never[];
      pages: never[];
    }>((resolve) => {
      resolveFirstUpload = resolve;
    });
    mock.selectedAgent = "agent-a";
    mock.uploadWikiSource.mockReturnValue(firstUpload);
    const user = userEvent.setup();
    const view = renderWithProviders(<WorkspacePage />, {
      initialEntries: ["/workspace"],
    });
    await user.click(screen.getByRole("button", { name: /添加资料/ }));
    const input =
      document.querySelector<HTMLInputElement>('input[type="file"]');
    fireEvent.change(input!, {
      target: {
        files: [
          new File(["alpha"], "alpha.txt", { type: "text/plain" }),
          new File(["beta"], "beta.txt", { type: "text/plain" }),
        ],
      },
    });
    await waitFor(() => expect(mock.uploadWikiSource).toHaveBeenCalledTimes(1));

    mock.selectedAgent = "agent-b";
    view.rerender(<WorkspacePage />);
    await act(async () => {
      resolveFirstUpload({
        imported: [{ path: ".wiki/raw/sources/alpha.txt" }],
        skipped: [],
        pages: [],
      });
      await firstUpload;
    });

    expect(mock.uploadWikiSource).toHaveBeenCalledTimes(1);
    expect(mock.fetchFiles).not.toHaveBeenCalled();
  });

  it("imports multiple web addresses from one paste", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });
    await user.click(screen.getByRole("button", { name: /添加资料/ }));
    await user.click(screen.getByRole("tab", { name: "添加网页" }));
    fireEvent.change(
      screen.getByPlaceholderText(/https:\/\/example\.com\/article/),
      {
        target: {
          value: "https://example.com/alpha\nhttps://example.com/beta",
        },
      },
    );
    await user.click(screen.getByRole("button", { name: "添加网页" }));

    await waitFor(() => expect(mock.importWikiUrl).toHaveBeenCalledTimes(2));
    expect(mock.importWikiUrl.mock.calls.map(([url]) => url)).toEqual([
      "https://example.com/alpha",
      "https://example.com/beta",
    ]);
  });

  it("keeps failed web addresses and retries only those addresses", async () => {
    const user = userEvent.setup();
    mock.importWikiUrl
      .mockResolvedValueOnce({
        imported: [{ path: ".wiki/raw/sources/web/alpha.md" }],
        skipped: [],
        pages: ["sources/alpha.md"],
      })
      .mockRejectedValueOnce(new Error("beta 网页无法读取"))
      .mockResolvedValueOnce({
        imported: [{ path: ".wiki/raw/sources/web/beta.md" }],
        skipped: [],
        pages: ["sources/beta.md"],
      });
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });
    await user.click(screen.getByRole("button", { name: /添加资料/ }));
    await user.click(screen.getByRole("tab", { name: "添加网页" }));
    const input = screen.getByPlaceholderText(
      /https:\/\/example\.com\/article/,
    );
    fireEvent.change(input, {
      target: {
        value: "https://example.com/alpha\nhttps://example.com/beta",
      },
    });
    await user.click(screen.getByRole("button", { name: "添加网页" }));

    const dialog = screen.getByRole("dialog");
    expect(
      await within(dialog).findByText("已接收，等待整理"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("读取失败")).toBeInTheDocument();
    expect(within(dialog).getByText("beta 网页无法读取")).toBeInTheDocument();
    expect(input).toHaveValue("https://example.com/beta");

    await user.click(screen.getByRole("button", { name: "仅重试失败项（1）" }));
    await waitFor(() => expect(mock.importWikiUrl).toHaveBeenCalledTimes(3));
    expect(mock.importWikiUrl.mock.calls[2]?.[0]).toBe(
      "https://example.com/beta",
    );
    expect(screen.queryByText("读取失败")).toBeNull();
    expect(input).toHaveValue("");
  });

  it("does not offer a blind retry when a web import result is unknown", async () => {
    const user = userEvent.setup();
    mock.importWikiUrl.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });
    await user.click(screen.getByRole("button", { name: /添加资料/ }));
    await user.click(screen.getByRole("tab", { name: "添加网页" }));
    const input = screen.getByPlaceholderText(
      /https:\/\/example\.com\/article/,
    );
    fireEvent.change(input, {
      target: { value: "https://example.com/uncertain" },
    });
    await user.click(screen.getByRole("button", { name: "添加网页" }));

    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByText("结果待核实")).toBeInTheDocument();
    expect(within(dialog).getByText(/不能确定/)).toBeInTheDocument();
    expect(input).toHaveValue("https://example.com/uncertain");
    expect(
      within(dialog).queryByRole("button", { name: /仅重试失败项/ }),
    ).toBeNull();
  });

  it("keeps each failed file available for a failed-only retry", async () => {
    const user = userEvent.setup();
    mock.uploadWikiSource
      .mockResolvedValueOnce({
        imported: [{ path: ".wiki/raw/sources/alpha.txt" }],
        skipped: [],
        pages: ["sources/alpha.md"],
      })
      .mockRejectedValueOnce(new Error("文件内容无法解析"))
      .mockResolvedValueOnce({
        imported: [{ path: ".wiki/raw/sources/beta.txt" }],
        skipped: [],
        pages: ["sources/beta.md"],
      });
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });
    await user.click(screen.getByRole("button", { name: /添加资料/ }));
    const input =
      document.querySelector<HTMLInputElement>('input[type="file"]');
    fireEvent.change(input!, {
      target: {
        files: [
          new File(["alpha"], "alpha.txt", { type: "text/plain" }),
          new File(["beta"], "beta.txt", { type: "text/plain" }),
        ],
      },
    });

    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByText("alpha.txt")).toBeInTheDocument();
    expect(within(dialog).getByText("beta.txt")).toBeInTheDocument();
    expect(within(dialog).getByText("文件内容无法解析")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "仅重试失败项（1）" }));

    await waitFor(() => expect(mock.uploadWikiSource).toHaveBeenCalledTimes(3));
    expect(mock.uploadWikiSource.mock.calls[2]?.[0].name).toBe("beta.txt");
    expect(mock.uploadWikiSource.mock.calls[0]?.[0].name).toBe("alpha.txt");
    expect(mock.uploadWikiSource.mock.calls[1]?.[0].name).toBe("beta.txt");
  });

  it("labels knowledge and source counts with a unified selection scope", () => {
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    expect(
      screen.getAllByRole("button", { name: /原始资料/ }).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText(/1 条知识笔记 · 2 份原始资料 · 3 项可用/),
    ).toBeVisible();
    expect(
      screen.getByRole("checkbox", {
        name: "选择当前内容",
      }),
    ).toBeInTheDocument();
  });

  it("states that resolving a reminder does not mutate knowledge", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    await user.click(screen.getByRole("button", { name: "需要处理，1 项" }));
    expect(
      screen.getByText(/只关闭这条提醒，不会自动修改你的知识内容/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "标记已处理" }));
    expect(mock.handleResolveReview).toHaveBeenCalledWith(
      "review-1",
      "resolved",
    );
  });

  it("keeps search visible as a primary action", () => {
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    const search = screen.getByPlaceholderText("搜索标题或正文");
    fireEvent.change(search, { target: { value: "现代诗" } });
    fireEvent.keyDown(search, { key: "Enter", code: "Enter" });

    expect(mock.handleSearch).toHaveBeenCalledWith("现代诗");
  });

  it("starts a new chat with the selected knowledge source attached", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    await user.click(screen.getByRole("button", { name: "research.pdf" }));
    await user.click(screen.getByRole("button", { name: "用这份资料提问" }));

    expect(
      JSON.parse(
        window.sessionStorage.getItem("lantai:create-new-chat-context") || "{}",
      ),
    ).toEqual({
      knowledgeSource: {
        path: ".wiki/raw/sources/files/research.pdf",
        filename: "research.pdf",
        kind: "binary",
      },
      knowledgeSources: [
        {
          path: ".wiki/raw/sources/files/research.pdf",
          filename: "research.pdf",
          kind: "binary",
        },
      ],
      knowledgeScopeMode: "exact",
    });
  });

  it("starts an exact-scope chat with multiple selected sources", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    await user.click(
      screen.getByRole("checkbox", { name: "选择 research.pdf" }),
    );
    await user.click(screen.getByRole("checkbox", { name: "选择 媒体预算" }));
    await user.click(screen.getByRole("button", { name: "用 2 份资料提问" }));

    const context = JSON.parse(
      window.sessionStorage.getItem("lantai:create-new-chat-context") || "{}",
    );
    expect(context.knowledgeScopeMode).toBe("exact");
    expect(context.knowledgeSources).toEqual([
      {
        path: ".wiki/raw/sources/files/research.pdf",
        filename: "research.pdf",
        kind: "binary",
      },
      {
        path: ".wiki/raw/sources/files/budget.xlsx",
        filename: "媒体预算",
        kind: "office",
      },
    ]);
    expect(context.knowledgeSource).toBeUndefined();
  });

  it("opens a citation link at the matching source excerpt", async () => {
    renderWithProviders(<WorkspacePage />, {
      initialEntries: [
        "/workspace?source=.wiki%2Fraw%2Fsources%2Ffiles%2Fresearch.pdf&start=0&end=8&rev=hash",
      ],
    });

    await waitFor(() =>
      expect(mock.loadWikiSourceContent).toHaveBeenCalledWith(
        ".wiki/raw/sources/files/research.pdf",
      ),
    );
    await waitFor(() =>
      expect(document.querySelector("mark")?.textContent).toBe("Research"),
    );
  });

  it("opens a search excerpt by Unicode position and checks its source version", async () => {
    const user = userEvent.setup();
    mock.searchResults = {
      query: "Research",
      hits: [{
        kind: "source",
        filename: "research.pdf",
        title: "可追溯检索结果",
        path: ".wiki/raw/sources/files/research.pdf",
        snippet: "Research",
        score: 10,
        source_revision: "2026-08-12T10:00:00",
        source_text_hash: "indexed-hash",
        source_start: 1,
        source_end: 9,
        position_unit: "unicode_codepoint",
      }],
    };
    mock.loadWikiSourceContent.mockResolvedValue({
      path: ".wiki/raw/sources/files/research.pdf",
      title: "research.pdf",
      kind: "office",
      content: "😀Research body",
      content_status: "text_extracted",
      message: "",
      page_count: 1,
      linked_pages: [],
      source_excerpt: "Research",
    });
    renderWithProviders(<WorkspacePage />);
    await user.type(screen.getByRole("searchbox", { name: "搜索知识库" }), "Research");
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: /可追溯检索结果/ }));
    expect(mock.loadWikiSourceContent).toHaveBeenCalledWith(
      ".wiki/raw/sources/files/research.pdf",
      expect.objectContaining({ source_text_hash: "indexed-hash", source_start: 1, source_end: 9 }),
    );
    expect(await screen.findByText("Research", { selector: "mark" })).toBeVisible();
  });

  it("shows a stale search citation without highlighting the new version", async () => {
    const user = userEvent.setup();
    mock.searchResults = {
      query: "Research",
      hits: [{
        kind: "source", filename: "research.pdf", title: "旧检索结果",
        path: ".wiki/raw/sources/files/research.pdf", snippet: "Research", score: 10,
        source_revision: "old", source_text_hash: "old-hash",
        source_start: 0, source_end: 8,
      }],
    };
    mock.loadWikiSourceContent.mockResolvedValueOnce({
      path: ".wiki/raw/sources/files/research.pdf", title: "research.pdf",
      kind: "office", content: "", content_status: "version_unavailable",
      message: "资料已更新", page_count: 1, linked_pages: [],
    });
    renderWithProviders(<WorkspacePage />);
    await user.type(screen.getByRole("searchbox", { name: "搜索知识库" }), "Research");
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: /旧检索结果/ }));
    expect(await screen.findByText("引用的资料版本已不可核对")).toBeVisible();
    expect(document.querySelector("mark")).toBeNull();
    await user.click(screen.getByRole("button", { name: "查看最新版" }));
    expect(mock.loadWikiSourceContent).toHaveBeenLastCalledWith(
      ".wiki/raw/sources/files/research.pdf",
    );
  });

  it("does not reinterpret an agent citation as another agent's same-path source", async () => {
    mock.selectedAgent = "agent-a";
    const view = renderWithProviders(<WorkspacePage />, {
      initialEntries: [
        "/workspace?source=.wiki%2Fraw%2Fsources%2Ffiles%2Fresearch.pdf&start=0&end=8&rev=hash",
      ],
    });
    await waitFor(() =>
      expect(mock.loadWikiSourceContent).toHaveBeenCalledTimes(1),
    );

    mock.selectedAgent = "agent-b";
    view.rerender(<WorkspacePage />);

    await waitFor(() => {
      expect(
        screen.getByRole("checkbox", { name: "选择 B 同名资料" }),
      ).not.toBeChecked();
      expect(screen.queryByLabelText("资料标题")).toBeNull();
    });
    expect(mock.loadWikiSourceContent).toHaveBeenCalledTimes(1);
  });

  it("opens the unified recycle bin as a primary library section", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    await user.click(screen.getByRole("button", { name: "回收站" }));
    expect(screen.getByText("统一回收站")).toBeInTheDocument();
  });

  it("offers recoverable deletion directly on every knowledge row", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    await user.click(screen.getByRole("button", { name: "删除 北京现代诗" }));
    expect(mock.deleteWikiPage).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "移到回收站" }));
    await waitFor(() =>
      expect(mock.deleteWikiPage).toHaveBeenCalledWith("queries/poem.md"),
    );
    Modal.destroyAll();
  });

  it("explains that removing a source does not delete the original file", async () => {
    let confirmation: { content?: unknown } | null = null;
    const confirmSpy = vi
      .spyOn(Modal, "confirm")
      .mockImplementation((config) => {
        confirmation = config;
        return { destroy: vi.fn(), update: vi.fn() } as never;
      });
    const user = userEvent.setup();
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    await user.click(screen.getByRole("button", { name: "research.pdf" }));
    await user.click(
      screen.getByRole("button", { name: "将当前资料移到知识库回收站" }),
    );

    expect(String(confirmation?.content)).toContain(
      "你电脑上的原文件不会被修改",
    );
    expect(String(confirmation?.content)).toContain(
      "知识库副本会保留在本地回收目录",
    );
    confirmSpy.mockRestore();
  });

  it("separates a source read failure from an empty document and retries", async () => {
    const user = userEvent.setup();
    mock.loadWikiSourceContent
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({
        path: ".wiki/raw/sources/files/research.pdf",
        title: "research.pdf",
        kind: "binary",
        content: "Research body",
        content_status: "text_extracted",
        message: "",
        page_count: 2,
        linked_pages: [],
      });
    renderWithProviders(<WorkspacePage />, { initialEntries: ["/workspace"] });

    await user.click(screen.getByRole("button", { name: "research.pdf" }));
    expect(await screen.findByText("资料正文读取失败")).toBeVisible();
    expect(screen.queryByText("暂时没有可读正文")).toBeNull();
    await user.click(screen.getByRole("button", { name: "重试读取正文" }));
    expect(await screen.findByText("Research body")).toBeVisible();
  });

  it("clears selected sources and ignores a late body response after switching agents", async () => {
    const user = userEvent.setup();
    let resolveAgentA: (value: {
      path: string;
      title: string;
      kind: string;
      content: string;
      content_status: string;
      message: string;
      page_count: number;
      linked_pages: string[];
    }) => void = () => {};
    const agentABody = new Promise<{
      path: string;
      title: string;
      kind: string;
      content: string;
      content_status: string;
      message: string;
      page_count: number;
      linked_pages: string[];
    }>((resolve) => {
      resolveAgentA = resolve;
    });
    mock.selectedAgent = "agent-a";
    mock.loadWikiSourceContent.mockImplementation(() => agentABody);

    const view = renderWithProviders(<WorkspacePage />, {
      initialEntries: ["/workspace"],
    });
    await user.click(screen.getByRole("checkbox", { name: "选择 A 同名资料" }));
    await user.click(screen.getByRole("button", { name: "A 同名资料" }));

    mock.selectedAgent = "agent-b";
    mock.loadWikiSourceContent.mockResolvedValue({
      path: ".wiki/raw/sources/files/research.pdf",
      title: "B 同名资料",
      kind: "binary",
      content: "B 正文",
      content_status: "text_extracted",
      message: "",
      page_count: 1,
      linked_pages: [],
    });
    view.rerender(<WorkspacePage />);

    expect(
      await screen.findByRole("checkbox", { name: "选择 B 同名资料" }),
    ).not.toBeChecked();
    expect(screen.queryByText("A 同名资料")).toBeNull();
    await act(async () => {
      resolveAgentA({
        path: ".wiki/raw/sources/files/research.pdf",
        title: "A 同名资料",
        kind: "binary",
        content: "A 的迟到正文",
        content_status: "text_extracted",
        message: "",
        page_count: 1,
        linked_pages: [],
      });
      await agentABody;
    });
    expect(screen.queryByText("A 的迟到正文")).toBeNull();
  });

  it("closes an old source archive confirmation when the agent changes", async () => {
    const user = userEvent.setup();
    let confirmation: { onOk?: () => void | Promise<void> } | null = null;
    const confirmSpy = vi
      .spyOn(Modal, "confirm")
      .mockImplementation((config) => {
        confirmation = config;
        return { destroy: vi.fn(), update: vi.fn() } as never;
      });
    const destroySpy = vi
      .spyOn(Modal, "destroyAll")
      .mockImplementation(() => undefined);
    mock.selectedAgent = "agent-a";
    const view = renderWithProviders(<WorkspacePage />, {
      initialEntries: ["/workspace"],
    });
    await user.click(screen.getByRole("button", { name: "A 同名资料" }));
    await user.click(
      screen.getByRole("button", { name: "将当前资料移到知识库回收站" }),
    );
    expect(confirmation).not.toBeNull();

    mock.selectedAgent = "agent-b";
    view.rerender(<WorkspacePage />);

    await waitFor(() => {
      expect(destroySpy).toHaveBeenCalled();
    });
    await act(async () => {
      await confirmation?.onOk?.();
    });
    expect(mock.archiveWikiSource).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
    destroySpy.mockRestore();
  });

  it("does not restore an old source title after refresh finishes in another agent", async () => {
    let resolveRefresh: () => void = () => {};
    const refresh = new Promise<void>((resolve) => {
      resolveRefresh = resolve;
    });
    mock.selectedAgent = "agent-a";
    mock.updateWikiSource.mockResolvedValue({});
    mock.fetchFiles.mockReturnValue(refresh);
    const user = userEvent.setup();
    const view = renderWithProviders(<WorkspacePage />, {
      initialEntries: ["/workspace"],
    });
    await user.click(screen.getByRole("button", { name: "A 同名资料" }));
    const titleInput = screen.getByLabelText("资料标题");
    await user.clear(titleInput);
    await user.type(titleInput, "A 更新标题");
    await user.click(screen.getByRole("button", { name: /保存标题/ }));
    await waitFor(() => expect(mock.fetchFiles).toHaveBeenCalledTimes(1));

    mock.selectedAgent = "agent-b";
    view.rerender(<WorkspacePage />);
    await act(async () => {
      resolveRefresh();
      await refresh;
    });

    expect(screen.queryByText("A 更新标题")).toBeNull();
    expect(
      screen.getByRole("checkbox", { name: "选择 B 同名资料" }),
    ).not.toBeChecked();
  });
});
