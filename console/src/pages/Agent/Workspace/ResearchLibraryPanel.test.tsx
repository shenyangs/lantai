import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import ResearchLibraryPanel from "./ResearchLibraryPanel";

const mock = vi.hoisted(() => ({ organize: vi.fn(), note: vi.fn(), noteSource: vi.fn(), search: vi.fn(), queryPreviews: vi.fn(), decideQuery: vi.fn() }));
const work = { work_id: "work-one", csl: { title: "Evidence study" }, versions: [], origins: [],
  annotations: [{ id: "annotation-one", text: "My thought", annotation_type: "annotation", origin: { provider: "zotero-local", external_anchor: { page_label: "12", attachment_version: 3, quote: "quoted passage" } } }], source_versions: [], quality_issues: [],
  organization: { revision: 2, tags: ["Methods"], collections: ["Review"] } };
vi.mock("@/api/modules/research", () => ({ researchApi: {
  queryPreviews: mock.queryPreviews, decideQuery: mock.decideQuery, search: mock.search, library: async () => ({ items: [work] }), organize: mock.organize, note: mock.note, noteSource: mock.noteSource, work: async () => work,
} }));

it("filters collections and saves classification against its current revision", async () => {
  mock.organize.mockResolvedValue({ ...work, organization: { revision: 3, tags: ["Causal"], collections: ["Review"] } });
  render(<ResearchLibraryPanel detail={null} onProjectChange={() => {}} />);
  await screen.findByRole("button", { name: "Evidence study" });
  fireEvent.change(screen.getByRole("textbox", { name: "筛选集合" }), { target: { value: "Unrelated" } });
  expect(screen.queryByRole("button", { name: "Evidence study" })).toBeNull();
  fireEvent.change(screen.getByRole("textbox", { name: "筛选集合" }), { target: { value: "Review" } });
  fireEvent.click(screen.getByRole("button", { name: "Evidence study" }));
  expect(screen.getByText("Zotero 第 12 页 · 附件版本 3 · 尚未与本地全文核对")).toBeTruthy();
  expect(screen.getByText("quoted passage")).toBeTruthy();
  fireEvent.change(screen.getByRole("textbox", { name: "文献标签" }), { target: { value: "Causal" } });
  fireEvent.click(screen.getByRole("button", { name: "保存分类" }));
  await waitFor(() => expect(mock.organize).toHaveBeenCalledWith("work-one", {
    expected_revision: 2, tags: ["Causal"], collections: ["Review"],
  }));
});


it("saves a user note and imports it separately", async () => {
  mock.note.mockResolvedValue({ id: "new-note" });
  mock.noteSource.mockResolvedValue({});
  const refresh = vi.fn();
  const detail = { project: { project_id: "p", revision: 3, question: "Why?", policy: "local_sources" },
    sources: [] } as unknown as import("@/api/modules/research").ResearchDetail;
  render(<ResearchLibraryPanel detail={detail} onProjectChange={refresh} />);
  fireEvent.click(await screen.findByRole("button", { name: "Evidence study" }));
  fireEvent.change(screen.getByRole("textbox", { name: "文献笔记" }), { target: { value: "User thought" } });
  fireEvent.click(screen.getByRole("button", { name: "保存笔记" }));
  await waitFor(() => expect(mock.note).toHaveBeenCalledWith("work-one", "User thought", undefined));
  fireEvent.click(screen.getByRole("button", { name: "作为笔记加入当前项目" }));
  await waitFor(() => expect(mock.noteSource).toHaveBeenCalledWith("annotation-one", detail.project));
  await waitFor(() => expect(refresh).toHaveBeenCalled());
});


it("shows original cache retrieval and retry information", async () => {
  mock.search.mockResolvedValue({ id: "search-one", status: "partial", papers: [], sources: {
    crossref: { status: "ok", cache: "hit", negative_cache: true, retrieved_at: "2026-10-13T12:00:00Z" },
    openalex: { status: "rate_limited", retry_at: 1791909000 },
  } });
  const detail = { project: { project_id: "p", revision: 3, policy: "public_search" }, sources: [] } as unknown as import("@/api/modules/research").ResearchDetail;
  render(<ResearchLibraryPanel detail={detail} onProjectChange={() => {}} />);
  fireEvent.change(screen.getByRole("textbox", { name: "文献检索词或 DOI" }), { target: { value: "query" } });
  fireEvent.click(screen.getByRole("button", { name: /检\s*索/ }));
  expect(await screen.findByText(/crossref：缓存中的零命中/)).toBeTruthy();
  expect(screen.getByText(/下次可请求时间/)).toBeTruthy();
});


it("requires a separate exact-query confirmation before sending", async () => {
  const item = { id: "preview-one", query: "private topic", revision: 3, limit: 8, status: "pending", requests: { crossref: { endpoint: "https://api.crossref.org/works", params: { "query.bibliographic": "private topic", rows: 8 }, version: "v2" } } };
  mock.queryPreviews.mockResolvedValue({ items: [item] });
  mock.decideQuery.mockResolvedValue({ id: "result-one", status: "ok", papers: [], sources: {} });
  const detail = { project: { project_id: "p", revision: 3, policy: "public_search", query_preview_required: true }, sources: [] } as unknown as import("@/api/modules/research").ResearchDetail;
  render(<ResearchLibraryPanel detail={detail} onProjectChange={() => {}} />);
  await screen.findByText("private topic");
  expect(mock.decideQuery).not.toHaveBeenCalled();
  expect(screen.getByText(/query.bibliographic/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "确认并检索" }));
  await waitFor(() => expect(mock.decideQuery).toHaveBeenCalledWith("preview-one", true));
});

it("sends title and author separately only in the explicit mode", async () => {
  mock.search.mockClear();
  mock.search.mockResolvedValue({ id: "search-fields", status: "ok", papers: [], sources: {} });
  const detail = { project: { project_id: "p", revision: 3, policy: "public_search" }, sources: [] } as unknown as import("@/api/modules/research").ResearchDetail;
  render(<ResearchLibraryPanel detail={detail} onProjectChange={() => {}} />);
  await screen.findByRole("button", { name: "Evidence study" });
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "文献检索方式" }));
  fireEvent.click(screen.getByText("题名与作者"));
  fireEvent.change(screen.getByRole("textbox", { name: "文献题名" }), { target: { value: "A trial" } });
  fireEvent.change(screen.getByRole("textbox", { name: "文献作者" }), { target: { value: "Janet L. Peacock" } });
  fireEvent.click(screen.getByRole("button", { name: /检\s*索/ }));
  await waitFor(() => expect(mock.search).toHaveBeenLastCalledWith(detail.project, "A trial", { query_mode: "title_author", author: "Janet L. Peacock" }));
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "文献检索方式" }));
  fireEvent.click(screen.getByText("关键词或 DOI"));
  expect(screen.queryByRole("textbox", { name: "文献作者" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /检\s*索/ }));
  await waitFor(() => expect(mock.search).toHaveBeenLastCalledWith(detail.project, "A trial"));
});
