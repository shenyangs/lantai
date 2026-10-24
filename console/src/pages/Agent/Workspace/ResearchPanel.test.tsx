import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import ChatSessionInitializer from "@/pages/Chat/components/ChatSessionInitializer";
import ResearchPanel from "./ResearchPanel";

const mock = vi.hoisted(() => ({
  sessionId: "local:research-draft",
  sessions: [] as { id: string }[],
  humanReview: vi.fn(),
  save: vi.fn(),
  read: vi.fn(),
  uploadFigure: vi.fn(),
  revise: vi.fn(),
  projectDetail: vi.fn(),
  report: vi.fn(),
  source: vi.fn(),
  select: vi.fn(),
  create: vi.fn(),
  drafts: new Map<string, string>(),
}));
const project = {
  project_id: "research-one", question: "Which evidence supports this conclusion?",
  revision: 1, policy: "local_sources", source_versions: [], stale_sources: [],
  stale_bibliography: [], bibliography: [], review_status: "not_reviewed", archived: false,
  budget: { reading_chars: 7000, review_calls: 2, review_seconds: 60 },
  budget_usage: { reading_chars: 6000, review_calls: 2, review_seconds: 12 },
  budget_remaining: { reading_chars: 1000, review_calls: 0, review_seconds: 48 },
  reading: { read_blocks: 0, total_blocks: 0 },
};
vi.mock("@/api/modules/research", () => ({ researchApi: {
  humanReview: mock.humanReview,
  save: mock.save,
  read: mock.read,
  uploadFigure: mock.uploadFigure,
  revise: mock.revise,
  projects: async () => ({ items: [project], pending_deletions: [] }),
  project: mock.projectDetail,
  report: mock.report,
  source: mock.source,
} }));
vi.mock("@/hooks/useAppMessage", () => ({ useAppMessage: () => ({ message: { success: vi.fn() } }) }));
vi.mock("./ResearchLibraryPanel", () => ({ default: () => null }));
vi.mock("./ResearchEstimate", () => ({ default: ({ project }: { project: { revision: number } }) => <section aria-label="运行前估算">修订估算 {project.revision}</section> }));
vi.mock("@/pages/Chat/sessionApi", () => ({ default: {
  createSession: mock.create,
  saveDraftText: (id: string, text: string) => mock.drafts.set(id, text),
} }));
vi.mock("@/vendor/lantaiChat", () => ({
  useChatAnywhereSessionsState: () => ({
    sessions: mock.sessions, currentSessionId: null, setCurrentSessionId: mock.select,
  }),
  useChatAnywhereSessions: () => ({ createSession: mock.create }),
}));

beforeEach(() => {
  mock.report.mockReset();
  mock.source.mockReset();
  mock.revise.mockReset().mockResolvedValue(project);
  mock.projectDetail.mockReset().mockResolvedValue({ project, report: null, review: null, sources: [], report_history: [] });
  mock.sessions = []; mock.drafts.clear(); mock.select.mockReset();
  mock.create.mockReset().mockImplementation(async () => {
    mock.sessions = [{ id: mock.sessionId }];
    return mock.sessions;
  });
});

it("preserves selected missing files and evidence levels when revising project sources", async () => {
  mock.projectDetail.mockResolvedValue({ project: { ...project, source_paths: ["old.txt"], source_levels: { "old.txt": "excerpt" } },
    report: null, review: null, sources: [], report_history: [] });
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.click(await screen.findByRole("button", { name: "修改资料" }));
  expect(screen.getByRole("combobox", { name: "研究资料" })).toBeTruthy();
  expect(screen.getByText("部分摘录")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "保存资料并创建修订" }));
  await waitFor(() => expect(mock.revise).toHaveBeenCalledWith("research-one", {
    expected_revision: 1, source_paths: ["old.txt"], source_levels: { "old.txt": "excerpt" }, idempotency_key: expect.any(String),
  }));
});

it("opens the original citation after a source revision leaves only one historical report", async () => {
  const span = { source_version_id: "original-source", start: 7, end: 21, quote: "Original quote" };
  mock.projectDetail.mockResolvedValue({ project: { ...project, revision: 2 }, report: null, review: null, sources: [],
    report_history: [{ id: "old-report", revision: 1 }] });
  mock.report.mockResolvedValue({ id: "old-report", revision: 1, text: "Previous finding", claims: [{ start: 0, end: 16, evidence: [span] }] });
  mock.source.mockResolvedValue({ id: "original-source", path: "old.txt", text: "Before Original quote after", raw_hash: "oldhash123456789",
    snapshot_path: "snapshot.txt", coverage: "complete", issues: [], pages: [{ page: 1, start: 0, end: 27 }], blocks: [{ index: 0, page: 1, start: 0, end: 27 }] });
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.click(await screen.findByRole("button", { name: "版本 1" }));
  fireEvent.click(await screen.findByRole("button", { name: /查看对应版本原文/ }));
  await waitFor(() => expect(mock.source).toHaveBeenCalledWith("original-source"));
  await waitFor(() => expect(document.querySelector("mark")).toHaveTextContent("Original quote"));
});

it("replaces a selected source and drops its old evidence level from the revision", async () => {
  mock.projectDetail.mockResolvedValue({ project: { ...project, source_paths: ["old.txt"], source_levels: { "old.txt": "excerpt" } },
    report: null, review: null, sources: [], report_history: [] });
  const replacement = { path: "new.txt", filename: "Replacement", size: 10, created_time: "", modified_time: "", updated_at: 0,
    kind: "text", extension: ".txt", sha256: "new", original_path: "new.txt", linked_pages: [], status: "ready" };
  render(<MemoryRouter><ResearchPanel sources={[replacement]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.click(await screen.findByRole("button", { name: "修改资料" }));
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "研究资料" }));
  fireEvent.click(await screen.findByText("Replacement"));
  const oldOption = screen.getAllByText("old.txt").find((node) => node.closest(".ant-select-item-option"));
  expect(oldOption).toBeTruthy();
  fireEvent.click(oldOption!);
  fireEvent.click(screen.getByRole("button", { name: "保存资料并创建修订" }));
  await waitFor(() => expect(mock.revise).toHaveBeenCalledWith("research-one", {
    expected_revision: 1, source_paths: ["new.txt"], source_levels: { "new.txt": "fulltext" }, idempotency_key: expect.any(String),
  }));
});

it("prevents a source revision from discarding unsaved report prose", async () => {
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.change(await screen.findByRole("textbox", { name: "研究报告正文" }), { target: { value: "Unsaved work" } });
  expect(screen.getByRole("button", { name: "修改资料" })).toBeDisabled();
  expect(mock.revise).not.toHaveBeenCalled();
});

it("hands the research draft to the same session selected by the chat route", async () => {
  render(<MemoryRouter initialEntries={["/research"]}><Routes>
    <Route path="/research" element={<ResearchPanel sources={[]} selectedSources={[]} />} />
    <Route path="/chat/:id" element={<ChatSessionInitializer />} />
  </Routes></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.click(await screen.findByRole("button", { name: "到对话继续研究" }));
  await waitFor(() => expect(mock.select).toHaveBeenCalledWith(mock.sessionId));
  const selected = mock.select.mock.calls.at(-1)?.[0];
  expect(mock.drafts.get(selected)).toContain("research-one");
  expect(mock.drafts.get(selected)).toContain(project.question);
  expect(mock.create).toHaveBeenCalledTimes(1);
});


it("shows spent budget and creates a budget-only revision to resume research", async () => {
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  expect(await screen.findByText(/审核调用：2 \/ 2/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "调整预算" }));
  fireEvent.change(screen.getByRole("spinbutton", { name: "审核调用上限" }), { target: { value: "3" } });
  fireEvent.click(screen.getByRole("button", { name: "保存预算并保留进度" }));
  await waitFor(() => expect(mock.revise).toHaveBeenCalledWith("research-one", {
    expected_revision: 1, budget: { reading_chars: 7000, review_calls: 3, review_seconds: 60 },
    idempotency_key: expect.any(String),
  }));
});

it("shows model budget consumption and server remaining values even after exceeding a limit", async () => {
  mock.projectDetail.mockResolvedValue({ project: { ...project,
    budget: { ...project.budget, model_calls: 40, model_tokens: 1000000, model_seconds: 1200 },
    budget_usage: { ...project.budget_usage, model_calls: 39, model_tokens: 1010543, model_seconds: 1198.2 },
    budget_remaining: { ...project.budget_remaining, model_calls: 1, model_tokens: 0, model_seconds: 1.8 },
  }, report: null, review: null, sources: [], report_history: [] });
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  expect(await screen.findByText("模型调用：39 / 40 次 · 剩余 1 次")).toBeTruthy();
  expect(screen.getByText("已报告 token：1010543 / 1000000 · 剩余 0")).toBeTruthy();
  expect(screen.getByText("模型调用耗时：1199 / 1200 秒 · 剩余 1 秒")).toBeTruthy();
  expect(mock.revise).not.toHaveBeenCalled();
});

it("keeps absent model budget values unknown for older project records", async () => {
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  expect(await screen.findByText("模型调用：未知 / 未知 次 · 剩余 未知 次")).toBeTruthy();
  expect(screen.getByText("已报告 token：未知 / 未知 · 剩余 未知")).toBeTruthy();
  expect(screen.getByText("模型调用耗时：未知 / 未知 秒 · 剩余 未知 秒")).toBeTruthy();
});

it("edits versioned subquestions and outline without losing the research question", async () => {
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.click(await screen.findByRole("button", { name: "修改问题" }));
  fireEvent.change(screen.getByRole("textbox", { name: "研究子问题" }), { target: { value: "What was measured?\nWhat remains uncertain?" } });
  fireEvent.change(screen.getByRole("textbox", { name: "报告大纲" }), { target: { value: "Findings\nLimitations" } });
  fireEvent.click(screen.getByRole("button", { name: "创建新修订" }));
  await waitFor(() => expect(mock.revise).toHaveBeenCalledWith("research-one", {
    expected_revision: 1, question: project.question,
    subquestions: ["What was measured?", "What remains uncertain?"], outline: ["Findings", "Limitations"],
    idempotency_key: expect.any(String),
  }));
});


it("shows disclosed evidence conflicts and independent critical review without hiding negative relations", async () => {
  const claim = { start: 0, end: 7, text: "Finding", assessment: "contradicts", reason: "Trials differ",
    evidence_indices: [0], importance: "critical", disposition: "disclosed", disclosure: { start: 0, end: 7, text: "Limited generalization" } };
  mock.projectDetail.mockResolvedValue({ project, report: null, sources: [], report_history: [], review: {
    id: "review", status: "reviewed", report_hash: "1234567890abcdef", coverage: [[0, 7]], issues: [],
    identity: { provider: "fixture", model: "fixture" }, claims: [claim],
    critical_review: { required_claims: 1, checkpoint_ids: ["part"], claims: [{ ...claim,
      assessment: "partial_support", disposition: "unresolved", disclosure: undefined, reason: "A component is missing" }] },
  } });
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  expect(await screen.findByText("反证")).toBeTruthy();
  expect(screen.getByText("已在正文披露")).toBeTruthy();
  expect(screen.getByText("正文披露：Limited generalization")).toBeTruthy();
  expect(screen.getByText("重要论点独立复核")).toBeTruthy();
  expect(screen.getByText("部分支持")).toBeTruthy();
  expect(screen.getByText("待处理")).toBeTruthy();
});


it("records a version-bound independent user opinion without claiming model approval", async () => {
  mock.projectDetail.mockResolvedValue({ project: { ...project, report_id: "report-one", review_id: null },
    report: { id: "report-one", text: "A finding.", claims: [] }, review: null, sources: [], report_history: [], human_reviews: [] });
  mock.humanReview.mockResolvedValue({});
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.change(await screen.findByRole("textbox", { name: "人工复核意见" }), { target: { value: "Clarify the population." } });
  fireEvent.change(screen.getByRole("textbox", { name: "对应正文原句（可选）" }), { target: { value: "A finding." } });
  fireEvent.click(screen.getByRole("button", { name: "记录独立意见" }));
  await waitFor(() => expect(mock.humanReview).toHaveBeenCalledWith("research-one", {
    revision: 1, report_id: "report-one", expected_review_id: null, text: "Clarify the population.",
    verdict: "question", anchor: { text: "A finding." }, idempotency_key: expect.any(String),
  }));
  expect(screen.getByText(/不覆盖模型核验/)).toBeTruthy();
});

it("distinguishes missing usage from free calls and shows configuration versions", async () => {
  mock.projectDetail.mockResolvedValue({ project: { ...project, inference: {
    calls: 2, known_tokens: 15, unknown_token_calls: 1, known_cost_cny: null,
    unknown_cost_calls: 2, seconds: 3, surfaces: ["research_review"],
    configurations: [{ version: "0123456789abcdef", provider: "fixture", model: "reviewer", generation: { temperature: 0.2 } }],
  } }, report: null, review: null, sources: [], report_history: [] });
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  expect(await screen.findByText(/用量未知：1 次 · 费用未知：2 次 · 已知费用：未知/)).toBeTruthy();
  expect(screen.getByText(/配置 0123456789ab · fixture \/ reviewer/)).toBeTruthy();
});

it("keeps reports and exports accessible with local and remote embedding configurations", async () => {
  mock.projectDetail.mockResolvedValue({ project: { ...project, report_id: "report-one", inference: {
    calls: 3, known_tokens: 15, unknown_token_calls: 1, known_cost_cny: null,
    unknown_cost_calls: 3, seconds: 3, surfaces: ["embedding", "main_chat"],
    configurations: [
      { version: "local1234567890", provider: "local-onnx", model: "local-vector", dimensions: 384, max_length: 512, fingerprint_hash: "private-hash" },
      { version: "remote1234567890", provider: "openai-compatible", model: "remote-vector", dimensions: 1024, use_dimensions: false },
      { version: "chat1234567890", provider: "fixture", model: "chat-model", generation: {} },
    ],
  } }, report: { id: "report-one", text: "A finding.", claims: [] }, review: null, sources: [], report_history: [] });
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  expect(await screen.findByRole("textbox", { name: "研究报告正文" })).toHaveValue("A finding.");
  expect(screen.getByRole("button", { name: "导出报告" })).toBeEnabled();
  expect(screen.getByText(/local-onnx \/ local-vector/)).toHaveTextContent("向量维度：384");
  expect(screen.getByText(/local-onnx \/ local-vector/)).toHaveTextContent("最大长度：512");
  expect(screen.getByText(/openai-compatible \/ remote-vector/)).toHaveTextContent("发送维度参数：否");
  expect(screen.getByText(/fixture \/ chat-model/)).toHaveTextContent("参数由当前 Provider 配置决定");
  expect(screen.queryByText(/private-hash/)).not.toBeInTheDocument();
});

it("uploads a project figure into the draft without saving a report version", async () => {
  mock.uploadFigure.mockResolvedValue({ id: "figure-123", sha256: "abc" });
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  const input = await screen.findByLabelText("添加报告图片");
  fireEvent.change(input, { target: { files: [new File(["image"], "chart.png", { type: "image/png" })] } });
  await waitFor(() => expect(mock.uploadFigure).toHaveBeenCalledWith("research-one", 1, "aW1hZ2U="));
  await waitFor(() => expect((screen.getByRole("textbox", { name: "研究报告正文" }) as HTMLTextAreaElement).value).toContain("![chart.png](research-figure:figure-123)"));
  expect(screen.getByRole("button", { name: "保存新版本" })).toBeEnabled();
});

it("rejects unsupported image files before sending them", async () => {
  mock.uploadFigure.mockClear();
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.change(await screen.findByLabelText("添加报告图片"), { target: { files: [new File(["<svg/>"], "chart.svg", { type: "image/svg+xml" })] } });
  expect(await screen.findByText(/请选择不超过 6 MiB 的 PNG 或 JPEG 图片/)).toBeTruthy();
  expect(mock.uploadFigure).not.toHaveBeenCalled();
});

it("preserves typing performed while an image upload is pending", async () => {
  let resolveUpload!: (value: { id: string }) => void;
  mock.uploadFigure.mockReset().mockImplementation(() => new Promise((resolve) => { resolveUpload = resolve; }));
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.change(await screen.findByLabelText("添加报告图片"), { target: { files: [new File(["image"], "chart.png", { type: "image/png" })] } });
  await waitFor(() => expect(mock.uploadFigure).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByRole("textbox", { name: "研究报告正文" }), { target: { value: "Newly typed finding." } });
  resolveUpload({ id: "figure-late" });
  await waitFor(() => expect(screen.getByRole("textbox", { name: "研究报告正文" })).toHaveValue("Newly typed finding.\n\n![chart.png](research-figure:figure-late)\n"));
});

it("does not append a late upload to a reloaded report", async () => {
  let resolveUpload!: (value: { id: string }) => void;
  mock.uploadFigure.mockReset().mockImplementation(() => new Promise((resolve) => { resolveUpload = resolve; }));
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.change(await screen.findByLabelText("添加报告图片"), { target: { files: [new File(["image"], "chart.png", { type: "image/png" })] } });
  await waitFor(() => expect(mock.uploadFigure).toHaveBeenCalledTimes(1));
  mock.projectDetail.mockResolvedValue({ project, report: { id: "new-report", text: "Reloaded report", claims: [] }, review: null, sources: [], report_history: [] });
  fireEvent.click(screen.getByRole("button", { name: /Which evidence/ }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "研究报告正文" })).toHaveValue("Reloaded report"));
  resolveUpload({ id: "figure-late" });
  await waitFor(() => expect(screen.getByLabelText("添加报告图片")).toBeEnabled());
  expect(screen.getByRole("textbox", { name: "研究报告正文" })).toHaveValue("Reloaded report");
});

it("requires an explicit click to re-extract scans with local OCR", async () => {
  mock.revise.mockReset().mockResolvedValue(project);
  mock.projectDetail.mockResolvedValue({ project, report: null, review: null, report_history: [], sources: [{
    id: "scan", path: "scan.pdf", pages: [{ page: 1, issue: "no_text_or_scanned_page" }], blocks: [], coverage: "partial",
  }] });
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  expect(mock.revise).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole("button", { name: "本地 OCR 重新提取" }));
  await waitFor(() => expect(mock.revise).toHaveBeenCalledWith("research-one", {
    expected_revision: 1, local_ocr: true, idempotency_key: expect.any(String),
  }));
});

it("keeps chronological version numbers when newer reports are added", async () => {
  const first = { id: "first", text: "Original report body", claims: [] };
  const second = { id: "second", text: "Revised report body", claims: [] };
  const third = { id: "third", text: "Newest report body", claims: [] };
  const detail = (reports: typeof first[]) => ({
    project, report: reports[0], review: null, sources: [], report_history: reports,
  });
  mock.projectDetail.mockResolvedValue(detail([second, first]));
  mock.report.mockImplementation(async (id: string) => [first, second, third].find((r) => r.id === id));
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  fireEvent.click(await screen.findByRole("button", { name: "版本 1" }));
  await waitFor(() => expect(mock.report).toHaveBeenLastCalledWith("first"));
  expect(await screen.findByText(first.text)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  mock.projectDetail.mockResolvedValue(detail([third, second, first]));
  fireEvent.click(screen.getByRole("button", { name: /Which evidence/ }));
  await screen.findByRole("button", { name: "版本 3" });
  fireEvent.click(screen.getByRole("button", { name: "版本 1" }));
  await waitFor(() => expect(mock.report).toHaveBeenLastCalledWith("first"));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "版本 2" }));
  await waitFor(() => expect(mock.report).toHaveBeenLastCalledWith("second"));
  expect(await screen.findByText(second.text)).toBeTruthy();
});


it("saves a user-selected citation with Unicode offsets without editing the reviewed report", async () => {
  const text = "😀相同 结论 相同";
  const source = { id: "s1", path: "paper.txt", raw_hash: "hash", pages: [], issues: [], coverage: "complete", blocks: [{ index: 0, start: 20, end: 27, page: 2 }] };
  mock.projectDetail.mockResolvedValue({ project, report: { id: "r1", text, claims: [] }, review: null, sources: [source], report_history: [] });
  mock.read.mockResolvedValue({ blocks: [{ index: 0, start: 20, end: 27, page: 2, text: "😀前文证据原文" }], total_blocks: 1 });
  mock.save.mockReset().mockResolvedValue({});
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  const body = await screen.findByRole("textbox", { name: "研究报告正文" });
  act(() => body.focus()); fireEvent.mouseUp(body);
  fireEvent.select(body, { target: { selectionStart: 8, selectionEnd: 10 } });
  fireEvent.click(screen.getByRole("button", { name: "为选中文字添加引用" }));
  const excerpt = await screen.findByRole("textbox", { name: "选择原文证据" });
  act(() => excerpt.focus()); fireEvent.mouseUp(excerpt);
  fireEvent.select(excerpt, { target: { selectionStart: 4, selectionEnd: 8 } });
  fireEvent.click(screen.getByRole("button", { name: "添加引用到草稿" }));
  expect(mock.save).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "导出报告" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "保存新版本" }));
  await waitFor(() => expect(mock.save).toHaveBeenCalledWith("research-one", {
    revision: 1, text, expected_report_id: "r1", idempotency_key: expect.any(String),
    claims: [{ start: 7, end: 9, evidence: [{ source_version_id: "s1", start: 23, end: 27, quote: "证据原文" }] }],
  }));
  expect(mock.read).toHaveBeenCalledWith("research-one", 1, "s1", 0);
});

it("keeps a draft intact and cannot attach evidence when the reading budget is exhausted", async () => {
  mock.projectDetail.mockResolvedValue({ project, report: { id: "r1", text: "原结论", claims: [] }, review: null,
    sources: [{ id: "s1", path: "paper.txt", raw_hash: "hash", pages: [], issues: [], coverage: "complete", blocks: [{ index: 0, start: 0, end: 3 }] }], report_history: [] });
  mock.read.mockRejectedValue(new Error("阅读额度已用尽"));
  mock.save.mockReset();
  render(<MemoryRouter><ResearchPanel sources={[]} selectedSources={[]} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Which evidence/ }));
  const body = await screen.findByRole("textbox", { name: "研究报告正文" });
  fireEvent.change(body, { target: { value: "修改后的结论" } });
  act(() => body.focus()); fireEvent.mouseUp(body); fireEvent.select(body, { target: { selectionStart: 0, selectionEnd: 6 } });
  fireEvent.click(screen.getByRole("button", { name: "为选中文字添加引用" }));
  expect(await screen.findByText("阅读额度已用尽")).toBeTruthy();
  expect(screen.getByRole("button", { name: "添加引用到草稿" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(body).toHaveValue("修改后的结论");
  expect(mock.save).not.toHaveBeenCalled();
});
