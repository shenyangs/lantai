import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../../test/common_setup";
import { useAgentStore } from "../../../stores/agentStore";
import MemoryPage from "./index";
import { formatMemoryTime } from "./formatTime";

const workspaceMock = vi.hoisted(() => ({
  listUserMemory: vi.fn(),
  getLayeredMemory: vi.fn(),
  listMemorySuggestions: vi.fn(),
  getMemoryPreferences: vi.fn(),
  getLastMemoryRecall: vi.fn(),
  getRecentMemoryImport: vi.fn(),
  getMemoryImportPrompt: vi.fn(),
  previewMemoryImport: vi.fn(),
  commitMemoryImport: vi.fn(),
  undoMemoryImport: vi.fn(),
  exportMemoryPack: vi.fn(),
  adoptMemoryPack: vi.fn(),
  getMemoryClearPreview: vi.fn(),
  clearAllMemory: vi.fn(),
  getMemoryDreaming: vi.fn(),
  updateMemoryDreaming: vi.fn(),
  runMemoryDreaming: vi.fn(),
  rollbackMemoryDreaming: vi.fn(),
  getUserUnderstanding: vi.fn(),
  feedbackUserUnderstanding: vi.fn(),
  listProactiveOpportunities: vi.fn(),
  feedbackProactiveOpportunity: vi.fn(),
  updateMemoryPreferences: vi.fn(),
  confirmMemoryFact: vi.fn(),
  rejectMemoryFact: vi.fn(),
  addUserMemory: vi.fn(),
  updateUserMemory: vi.fn(),
  archiveUserMemory: vi.fn(),
  deleteUserMemory: vi.fn(),
}));

vi.mock("../../../api/modules/workspace", () => ({
  workspaceApi: workspaceMock,
}));

const emptyUserMemory = {
  entries: [],
  stats: {
    total: 0,
    auto: 0,
    manual: 0,
    conflict: 0,
    by_type: { identity: 0, workflow: 0, style: 0, instruction: 0 },
  },
  profile_summary: {
    summary: "",
    by_type: { identity: [], workflow: [], style: [], instruction: [] },
  },
};

const emptyLayeredMemory = {
  stats: {
    l0_conversations: 0,
    l1_atoms: 0,
    l2_scenes: 0,
    offload_entries: 0,
    system2_models: 0,
    open_intentions: 0,
  },
  atoms: [],
  scenes: [],
  persona: "",
  system2: { models: [], open_intentions: [], evolution_chains: [] },
  offload: [],
  storage_path: "",
  updated_at: "",
};

const emptyUnderstanding = {
  confirmed: [],
  hypotheses: [],
  open_loops: [],
  stats: { confirmed: 0, hypotheses: 0, open_loops: 0, retired: 0 },
};

const emptyDreaming = {
  enabled: false,
  schedule: "0 23 * * *",
  rem_enabled: true,
  min_score: 0.68,
  latest_run: null,
  runs: [],
  next_run_at: "",
  diary_path: "",
  diary_excerpt: "",
  safety: {
    confirmed_only_injection: true,
    assistant_and_tool_evidence_blocked: true,
    diary_is_promotion_source: false,
  },
};

function editableUserMemory(
  id: string,
  content: string,
  updatedAt = "2026-09-05T08:00:00Z",
) {
  return {
    entries: [
      {
        id,
        type: "style",
        content,
        source: "agent",
        source_kind: "chat",
        source_title: "讨论写作方式",
        status: "active",
        confirmed: true,
        confidence: 0.95,
        reason: "",
        evidence: "用户说以后都按这种方式写",
        created_at: "2026-09-01T00:00:00Z",
        updated_at: updatedAt,
      },
    ],
    stats: {
      total: 1,
      auto: 1,
      manual: 0,
      conflict: 0,
      by_type: { identity: 0, workflow: 0, style: 1, instruction: 0 },
    },
    profile_summary: { summary: "", by_type: {} },
  };
}

async function openOnlyEditableMemory(
  user: ReturnType<typeof userEvent.setup>,
) {
  await user.click(await screen.findByRole("button", { name: "全部记忆" }));
  await user.click(screen.getByText("你的表达偏好").closest("button")!);
  await user.click(screen.getByRole("button", { name: "修改记忆" }));
  return screen.getByRole("textbox", { name: "记忆内容" });
}

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  useAgentStore.setState({ selectedAgent: "default" });
  workspaceMock.listUserMemory.mockResolvedValue(emptyUserMemory);
  workspaceMock.getLayeredMemory.mockResolvedValue(emptyLayeredMemory);
  workspaceMock.listMemorySuggestions.mockResolvedValue({
    facts: [],
    stats: {},
  });
  workspaceMock.getMemoryPreferences.mockResolvedValue({
    runtime_enabled: true,
    capture_enabled: true,
  });
  workspaceMock.getLastMemoryRecall.mockResolvedValue({
    query: "",
    updated_at: "",
    status: "unavailable",
    reason: "no_record",
    items: [],
  });
  workspaceMock.getRecentMemoryImport.mockResolvedValue({ batch: null });
  workspaceMock.getMemoryDreaming.mockResolvedValue(emptyDreaming);
  workspaceMock.getUserUnderstanding.mockResolvedValue(emptyUnderstanding);
  workspaceMock.listProactiveOpportunities.mockResolvedValue({
    opportunities: [],
    stats: {},
  });
  workspaceMock.updateMemoryPreferences.mockResolvedValue({
    runtime_enabled: false,
    capture_enabled: false,
  });
  workspaceMock.feedbackUserUnderstanding.mockResolvedValue({ updated: true });
  workspaceMock.feedbackProactiveOpportunity.mockResolvedValue({
    updated: true,
  });
  workspaceMock.confirmMemoryFact.mockResolvedValue({ confirmed: true });
  workspaceMock.rejectMemoryFact.mockResolvedValue({ rejected: true });
  workspaceMock.addUserMemory.mockResolvedValue({ decision: "new" });
  workspaceMock.updateUserMemory.mockResolvedValue({ updated: true });
  workspaceMock.archiveUserMemory.mockResolvedValue({ archived: true });
  workspaceMock.deleteUserMemory.mockResolvedValue({ deleted: true });
  workspaceMock.updateMemoryDreaming.mockResolvedValue({});
  workspaceMock.runMemoryDreaming.mockResolvedValue({
    run_id: "dream-1",
    status: "completed",
    trigger: "manual",
    shadow: false,
    counts: { reviewed: 4, eligible: 1, staged: 1, excluded: 3 },
    candidates: [],
  });
  workspaceMock.getMemoryImportPrompt.mockResolvedValue({
    prompt: "请整理适合长期保留的记忆。",
    max_characters: 50_000,
    max_candidates: 60,
    privacy_note: "只在本机解析。",
  });
  workspaceMock.previewMemoryImport.mockResolvedValue({
    batch_id: "import-1",
    provider: "ChatGPT",
    created_at: "2026-09-05T00:00:00Z",
    raw_stored: false,
    stats: { recommended: 1, review: 1, duplicate: 0, rejected: 0 },
    candidates: [
      {
        id: "recommended",
        type: "style",
        type_label: "表达偏好",
        content: "用户偏好自然、克制的中文表达。",
        bucket: "recommended",
        reason: "明确且长期有用",
        selectable: true,
        selected_by_default: true,
        confidence: 0.9,
        evidence: "",
        scope: "global",
        sensitivity: "normal",
      },
      {
        id: "review",
        type: "workflow",
        type_label: "做事方式",
        content: "用户可能长期使用一个项目路径。",
        bucket: "review",
        reason: "使用范围不明确",
        selectable: true,
        selected_by_default: false,
        confidence: 0.75,
        evidence: "",
        scope: "project",
        sensitivity: "normal",
      },
    ],
  });
  workspaceMock.commitMemoryImport.mockResolvedValue({
    batch_id: "import-1",
    provider: "ChatGPT",
    status: "committed",
    imported_count: 1,
    skipped_count: 0,
    imported: [],
    skipped: [],
    committed_at: "2026-09-05T00:01:00Z",
    can_undo: true,
  });
  workspaceMock.undoMemoryImport.mockResolvedValue({
    batch_id: "import-1",
    status: "undone",
    removed_count: 1,
    restored_count: 0,
    forgotten_fact_count: 1,
    undone_at: "2026-09-05T00:02:00Z",
  });
  workspaceMock.exportMemoryPack.mockResolvedValue({
    blob: new Blob(["memory"]),
    filename: "memory.lantai-memory.zip",
  });
  workspaceMock.adoptMemoryPack.mockResolvedValue({
    valid: true,
    schema_version: 1,
    source_agent_id: "source-agent",
    created_at: "2026-09-05T00:00:00Z",
    categories: ["root_memory", "user_memory"],
    file_count: 3,
    total_bytes: 2048,
    dry_run: true,
    adopted: false,
    would_replace: ["root_memory", "user_memory"],
    would_remove: ["daily_memory"],
    recovery_pack: "",
    warnings: [],
  });
  workspaceMock.getMemoryClearPreview.mockResolvedValue({
    counts: {
      structured_entries: 3,
      canonical_facts: 2,
      canonical_events: 4,
      layered_atoms: 5,
      layered_conversations: 6,
      scenes: 1,
      system2_models: 2,
      markdown_files: 2,
      import_batches: 0,
      dreaming_runs: 0,
      user_understanding_atoms: 0,
      proactive_opportunities: 0,
      backup_files: 3,
    },
    active_items: 25,
    backup_files: 3,
    total_items: 28,
    chat_history_preserved: true,
    runtime_cache_preserved: true,
    confirmation_text: "删除全部记忆",
  });
  workspaceMock.clearAllMemory.mockResolvedValue({
    cleared: true,
    cleared_counts: {},
    cleared_items: 28,
    backup_created: true,
    backup_dir: "/backup",
    chat_history_preserved: true,
    runtime_cache_preserved: true,
    preferences: { runtime_enabled: true, capture_enabled: true },
  });
});

describe("MemoryPage partial-load ownership", () => {
  it("keeps core memory usable when an optional panel fails and retries only the optional data", async () => {
    workspaceMock.listProactiveOpportunities
      .mockRejectedValueOnce(new Error("opportunities offline"))
      .mockResolvedValueOnce({
        opportunities: [],
        stats: {},
      });
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });

    expect(await screen.findByText("澜台现在怎样理解你")).toBeInTheDocument();
    expect(screen.queryByText("记忆没有加载完成")).not.toBeInTheDocument();
    expect(screen.getByText("部分辅助信息没有更新")).toBeInTheDocument();
    expect(screen.getByText(/\u4e3b\u52a8\u5173\u6ce8/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重试辅助信息" }));

    await waitFor(() =>
      expect(workspaceMock.listProactiveOpportunities).toHaveBeenCalledTimes(2),
    );
    await waitFor(() =>
      expect(
        screen.queryByText("部分辅助信息没有更新"),
      ).not.toBeInTheDocument(),
    );
    expect(workspaceMock.listUserMemory).toHaveBeenCalledTimes(1);
  });

  it("does not let an old agent optional response leak into the new agent page", async () => {
    let resolveOldOpportunities: ((value: unknown) => void) | undefined;
    const oldOpportunities = new Promise((resolve) => {
      resolveOldOpportunities = resolve;
    });
    workspaceMock.listProactiveOpportunities
      .mockReturnValueOnce(oldOpportunities)
      .mockResolvedValueOnce({
        opportunities: [
          {
            id: "new-agent-item",
            kind: "follow_up",
            status: "queued",
            content: "新 Agent 的关注项",
            score: 0.9,
            source_ids: [],
            created_at: "2026-09-19T00:00:00Z",
            updated_at: "2026-09-19T00:00:00Z",
            expires_at: "",
            cooldown_until: "",
            ignored_hits: 0,
            presented_hits: 0,
          },
        ],
        stats: {},
      });
    const rendered = renderWithProviders(<MemoryPage />, {
      initialEntries: ["/memory"],
    });

    await screen.findByText("澜台现在怎样理解你");
    act(() => useAgentStore.getState().setSelectedAgent("agent-b"));

    expect(await screen.findByText("新 Agent 的关注项")).toBeInTheDocument();
    await act(async () => {
      resolveOldOpportunities?.({
        opportunities: [
          {
            id: "old-agent-item",
            kind: "follow_up",
            status: "queued",
            content: "旧 Agent 的关注项",
            score: 0.9,
            source_ids: [],
            created_at: "2026-09-19T00:00:00Z",
            updated_at: "2026-09-19T00:00:00Z",
            expires_at: "",
            cooldown_until: "",
            ignored_hits: 0,
            presented_hits: 0,
          },
        ],
        stats: {},
      });
      await oldOpportunities;
    });

    expect(screen.queryByText("旧 Agent 的关注项")).not.toBeInTheDocument();
    expect(screen.getByText("新 Agent 的关注项")).toBeInTheDocument();
    rendered.unmount();
  });
});

describe("MemoryPage rebuilt information architecture", () => {
  it("formats memory timestamps in the Chinese 24-hour convention", () => {
    const localTime = new Date(2026, 8, 8, 23, 0, 0).toISOString();
    const formatted = formatMemoryTime(localTime);

    expect(formatted).toContain("23:00");
    expect(formatted).not.toMatch(/AM|PM/i);
  });

  it("opens on useful content and never turns an unknown trace into zero", async () => {
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });

    expect(await screen.findByText("澜台现在怎样理解你")).toBeInTheDocument();
    expect(
      screen.getByText(/项目资料和第三方内容不会自动当成你本人/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "记忆页面" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "概览" })).toHaveAttribute(
      "data-active",
      "true",
    );
    expect(
      screen.getByText("暂无可追溯的检索记录，不把未知显示成 0"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/调用了 0 条/)).not.toBeInTheDocument();
  });

  it("restores an unsaved memory edit after an internal route unmount", async () => {
    workspaceMock.listUserMemory.mockResolvedValue(
      editableUserMemory(
        "route-draft-memory",
        "用户偏好自然、专业的中文表达。",
      ),
    );
    const user = userEvent.setup();
    const firstRender = renderWithProviders(<MemoryPage />, {
      initialEntries: ["/memory"],
    });
    const editor = await openOnlyEditableMemory(user);
    await user.clear(editor);
    await user.type(editor, "用户偏好自然、专业、克制的中文表达。");
    firstRender.unmount();

    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });
    const restoredEditor = await openOnlyEditableMemory(user);

    expect(restoredEditor).toHaveValue("用户偏好自然、专业、克制的中文表达。");
    expect(
      screen.getByText("已恢复你离开此页面前未保存的修改"),
    ).toBeInTheDocument();
  });

  it("does not claim a draft owned by another agent or an older revision", async () => {
    workspaceMock.listUserMemory.mockResolvedValue(
      editableUserMemory("owned-draft-memory", "用户偏好自然的中文表达。"),
    );
    const user = userEvent.setup();
    const firstRender = renderWithProviders(<MemoryPage />, {
      initialEntries: ["/memory"],
    });
    const editor = await openOnlyEditableMemory(user);
    await user.clear(editor);
    await user.type(editor, "默认 Agent 的未保存修改。");
    firstRender.unmount();

    act(() => useAgentStore.getState().setSelectedAgent("agent-b"));
    const secondRender = renderWithProviders(<MemoryPage />, {
      initialEntries: ["/memory"],
    });
    expect(await openOnlyEditableMemory(user)).toHaveValue(
      "用户偏好自然的中文表达。",
    );
    expect(
      screen.queryByText("已恢复你离开此页面前未保存的修改"),
    ).not.toBeInTheDocument();
    secondRender.unmount();

    act(() => useAgentStore.getState().setSelectedAgent("default"));
    workspaceMock.listUserMemory.mockResolvedValue(
      editableUserMemory(
        "owned-draft-memory",
        "服务端已经更新的内容。",
        "2026-09-19T08:00:00Z",
      ),
    );
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });

    expect(await openOnlyEditableMemory(user)).toHaveValue(
      "服务端已经更新的内容。",
    );
    expect(
      screen.queryByText("已恢复你离开此页面前未保存的修改"),
    ).not.toBeInTheDocument();
  });

  it("builds data-derived topics and opens details in the native content area", async () => {
    workspaceMock.listUserMemory.mockResolvedValue({
      entries: [
        {
          id: "style-1",
          type: "style",
          content: "用户的长期偏好：偏好自然、专业、克制的中文表达。",
          source: "agent",
          source_kind: "chat",
          source_title: "讨论写作方式",
          status: "active",
          confirmed: true,
          confidence: 0.95,
          reason: "",
          evidence: "用户说以后都按这种方式写",
          created_at: "2026-09-01T00:00:00Z",
          updated_at: "2026-09-05T08:00:00Z",
        },
      ],
      stats: {
        total: 1,
        auto: 1,
        manual: 0,
        conflict: 0,
        by_type: { identity: 0, workflow: 0, style: 1, instruction: 0 },
      },
      profile_summary: { summary: "", by_type: {} },
    });
    workspaceMock.getLayeredMemory.mockResolvedValue({
      ...emptyLayeredMemory,
      scenes: [
        {
          scene_name: "novel-submission",
          title: "万应之城投稿",
          path: "/memory/scenes/novel.md",
          atom_count: 3,
          updated_at: "2026-09-05T09:00:00Z",
          sample: ["小说已进入投稿准备阶段。", "需要保留最终稿位置。"],
        },
      ],
      system2: {
        models: [
          {
            key: "decision-style",
            title: "决策方式",
            summary: "比较选项后，希望获得明确推荐和理由。",
            sample: ["不希望无限扩展候选项。"],
            evidence_count: 4,
            updated_at: "2026-09-05T07:00:00Z",
          },
        ],
        open_intentions: [],
        evolution_chains: [],
      },
    });
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });

    expect(
      (await screen.findAllByText("用户偏好自然、专业、克制的中文表达")).length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText("万应之城投稿").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "全部记忆" }));
    expect(
      screen.getByRole("heading", { name: "全部记忆" }),
    ).toBeInTheDocument();
    await user.click(screen.getAllByText("万应之城投稿")[0].closest("button")!);
    expect(
      screen.getByRole("heading", { name: "万应之城投稿" }),
    ).toBeInTheDocument();
    expect(screen.getByText("需要保留最终稿位置。")).toBeInTheDocument();
    expect(screen.getByText(/项目 · 由相关记忆整理/)).toBeInTheDocument();
    expect(screen.queryByText(/6 条相关依据/)).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "返回全部记忆" }));
    await user.click(screen.getByRole("button", { name: "概览" }));
    const changesSection = screen
      .getByRole("heading", { name: "最近变化" })
      .closest("section")!;
    const topicsSection = screen
      .getByRole("heading", { name: "主题与项目" })
      .closest("section")!;
    expect(
      changesSection.compareDocumentPosition(topicsSection) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("shows review items independently and lets the user defer without changing memory", async () => {
    workspaceMock.listMemorySuggestions.mockResolvedValue({
      facts: [
        {
          id: "fact-1",
          content: "以后方案都需要明确推荐。",
          memory_type: "instruction",
          status: "pending",
          event_ids: ["event-1"],
          confidence: 0.8,
          scope: "global",
          scope_id: "",
          subject: "user",
          utility_score: 0.9,
          decision_reason: "",
          sensitivity: "normal",
          session_ids: ["s1"],
          occurrence_count: 2,
          legacy_entry_ids: [],
          atom_ids: [],
          created_at: "2026-09-05T00:00:00Z",
          updated_at: "2026-09-05T00:00:00Z",
        },
      ],
      stats: {},
    });
    workspaceMock.getUserUnderstanding.mockResolvedValue({
      ...emptyUnderstanding,
      hypotheses: [
        {
          id: "guess-1",
          kind: "decision_pattern",
          statement: "你可能更在意真实体感而非评分。",
          status: "working_hypothesis",
          confidence: 0.82,
          evidence_ids: ["eval:1"],
          scope: "global",
          scope_id: "",
          sensitivity: "normal",
          expires_at: "",
          created_at: "2026-09-05T00:00:00Z",
          updated_at: "2026-09-05T00:00:00Z",
        },
      ],
      stats: { confirmed: 0, hypotheses: 1, open_loops: 0, retired: 0 },
    });
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });

    await user.click(
      await screen.findByRole("button", { name: "2 项需要核对" }),
    );
    expect(
      screen.getByText(
        "这些选择彼此独立。可以只处理一项，也可以全部留到以后。",
      ),
    ).toBeInTheDocument();
    const first = screen
      .getByText("以后方案都需要明确推荐。")
      .closest("article")!;
    await user.click(within(first).getByRole("button", { name: "以后再说" }));
    expect(
      screen.getByText("你可能更在意真实体感而非评分。"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "1 项需要核对" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "返回记忆" }));
    await user.click(screen.getByRole("button", { name: "1 项需要核对" }));
    expect(
      screen.queryByText("以后方案都需要明确推荐。"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("你可能更在意真实体感而非评分。"),
    ).toBeInTheDocument();
    expect(workspaceMock.confirmMemoryFact).not.toHaveBeenCalled();
    expect(workspaceMock.rejectMemoryFact).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "不是这样" }));
    await waitFor(() =>
      expect(workspaceMock.feedbackUserUnderstanding).toHaveBeenCalledWith(
        "guess-1",
        "corrected",
      ),
    );
  });

  it("labels a real retrieval trace without claiming model causality", async () => {
    workspaceMock.getLastMemoryRecall.mockResolvedValue({
      query: "帮我判断这个传播方案",
      updated_at: "2026-09-05T08:00:00Z",
      status: "ready",
      reason: "",
      items: [
        {
          source: "native-memory",
          citation: "memory-ledger/facts.json#fact-1",
          content: "用户负责品牌公关工作。",
          score: 0.9,
        },
      ],
    });
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });
    const summary = await screen.findByRole("button", {
      name: /最近一次记忆检索记录/,
    });
    expect(summary).toHaveTextContent("检索到 1 条相关记忆");
    expect(summary).not.toHaveTextContent("模型使用了");
    await user.click(summary);
    expect(screen.getByText("用户负责品牌公关工作。")).toBeInTheDocument();
    expect(
      screen.queryByText("这里记录的是最近一次检索，不代表模型内部全部因果。"),
    ).not.toBeInTheDocument();
  });

  it("provides an executable correction path for an aggregated topic", async () => {
    workspaceMock.getLayeredMemory.mockResolvedValue({
      ...emptyLayeredMemory,
      scenes: [
        {
          scene_name: "novel",
          title: "万应之城投稿",
          path: "/memory/novel.md",
          atom_count: 2,
          updated_at: "2026-09-05T09:00:00Z",
          sample: ["小说还在初稿阶段。"],
        },
      ],
    });
    workspaceMock.addUserMemory.mockImplementation(async () => {
      workspaceMock.listUserMemory.mockResolvedValue({
        ...emptyUserMemory,
        entries: [
          {
            id: "correction-1",
            type: "workflow",
            content: "小说已经完成定稿。",
            source: "manual",
            source_kind: "manual",
            status: "active",
            confirmed: true,
            confidence: 1,
            reason: "用户纠正主题:project:novel|万应之城投稿",
            created_at: "2026-09-05T10:00:00Z",
            updated_at: "2026-09-05T10:00:00Z",
          },
        ],
        stats: {
          total: 1,
          auto: 0,
          manual: 1,
          conflict: 0,
          by_type: { identity: 0, workflow: 1, style: 0, instruction: 0 },
        },
      });
      return { decision: "new" };
    });
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });
    await user.click(await screen.findByRole("button", { name: "全部记忆" }));
    await user.click(screen.getByText("万应之城投稿").closest("button")!);
    await user.click(screen.getByRole("button", { name: "纠正这项理解" }));
    await user.type(
      screen.getByRole("textbox", { name: "正确的情况" }),
      "小说已经完成定稿。",
    );
    await user.click(screen.getByRole("button", { name: "提交正确说法" }));
    await waitFor(() =>
      expect(workspaceMock.addUserMemory).toHaveBeenCalledWith(
        "小说已经完成定稿。",
        "workflow",
        "用户纠正主题:project:novel|万应之城投稿",
      ),
    );
    expect(await screen.findByText("小说已经完成定稿。")).toBeInTheDocument();
    expect(screen.queryByText("小说还在初稿阶段。")).not.toBeInTheDocument();
  });

  it.each([
    ["user_profile", "用户画像", "identity"],
    ["communication_model", "表达偏好", "style"],
    ["standing_rules", "长期规则", "instruction"],
  ] as const)(
    "writes a %s topic correction with its real %s memory type",
    async (key, title, expectedType) => {
      workspaceMock.getLayeredMemory.mockResolvedValue({
        ...emptyLayeredMemory,
        system2: {
          models: [{ key, title, summary: `${title}的旧理解。` }],
          open_intentions: [],
          evolution_chains: [],
        },
      });
      const user = userEvent.setup();
      renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });

      await user.click(await screen.findByRole("button", { name: "全部记忆" }));
      await user.click(screen.getByText(title).closest("button")!);
      await user.click(screen.getByRole("button", { name: "纠正这项理解" }));
      await user.type(
        screen.getByRole("textbox", { name: "正确的情况" }),
        `${title}的新理解。`,
      );
      await user.click(screen.getByRole("button", { name: "提交正确说法" }));

      await waitFor(() =>
        expect(workspaceMock.addUserMemory).toHaveBeenCalledWith(
          `${title}的新理解。`,
          expectedType,
          `用户纠正主题:theme:${key}|${title}`,
        ),
      );
    },
  );

  it("keeps open loops out of forced review while allowing the user to end one", async () => {
    workspaceMock.getUserUnderstanding.mockResolvedValue({
      ...emptyUnderstanding,
      open_loops: [
        {
          id: "open-1",
          kind: "open_loop",
          statement: "还要完成记忆系统验收。",
          status: "confirmed",
          confidence: 1,
          evidence_ids: ["task:memory"],
          scope: "global",
          scope_id: "",
          sensitivity: "normal",
          expires_at: "",
          created_at: "2026-09-05T00:00:00Z",
          updated_at: "2026-09-05T00:00:00Z",
        },
      ],
      stats: { confirmed: 0, hypotheses: 0, open_loops: 1, retired: 0 },
    });
    workspaceMock.feedbackUserUnderstanding.mockImplementation(async () => {
      workspaceMock.getUserUnderstanding.mockResolvedValue(emptyUnderstanding);
      return { updated: true };
    });
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });

    expect(
      (await screen.findAllByText("还要完成记忆系统验收。")).length,
    ).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: /项需要核对/ }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "这件事结束了" }));
    await waitFor(() =>
      expect(workspaceMock.feedbackUserUnderstanding).toHaveBeenCalledWith(
        "open-1",
        "corrected",
      ),
    );
    await waitFor(() =>
      expect(screen.queryAllByText("还要完成记忆系统验收。")).toHaveLength(0),
    );
  });
});

describe("MemoryPage low-frequency controls", () => {
  it("keeps settings out of the primary navigation and explains pause semantics", async () => {
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });

    const primary = await screen.findByRole("navigation", { name: "记忆页面" });
    expect(within(primary).queryByText("设置")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "记忆设置" }));
    expect(
      screen.getByRole("heading", { name: "记忆设置" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "跨对话记忆" }));
    await waitFor(() =>
      expect(workspaceMock.updateMemoryPreferences).toHaveBeenCalledWith({
        runtime_enabled: false,
        capture_enabled: false,
      }),
    );
    expect(
      screen.getByText("暂停后不会读取或新增，已有内容仍保留。"),
    ).toBeInTheDocument();
  });

  it("makes automatic organization show results instead of internal tuning", async () => {
    const completedRun = {
      run_id: "dream-1",
      status: "completed",
      trigger: "manual",
      shadow: false,
      completed_at: "2026-09-05T03:00:00Z",
      counts: { reviewed: 4, eligible: 1, staged: 1, excluded: 3 },
      candidates: [
        {
          fact_id: "fact-1",
          content: "用户希望正式方案给出明确推荐。",
          memory_type: "instruction",
          status: "pending",
          scope: "global",
          subject: "user",
          sensitivity: "normal",
          score: 0.9,
          signals: {},
          occurrence_count: 2,
          session_count: 2,
          evidence_count: 2,
          grounded_evidence_count: 2,
          grounded: true,
          eligible: true,
          staged: true,
          reason: "多次明确表达",
          source_event_ids: ["e1", "e2"],
        },
      ],
    };
    workspaceMock.getMemoryDreaming
      .mockResolvedValueOnce({
        ...emptyDreaming,
        latest_run: completedRun,
        runs: [completedRun],
      })
      .mockResolvedValue({
        ...emptyDreaming,
        latest_run: completedRun,
        runs: [completedRun],
      });
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });

    await user.click(await screen.findByRole("button", { name: "自动整理" }));
    expect(
      screen.getByRole("heading", { name: "自动整理" }),
    ).toBeInTheDocument();
    expect(screen.getByText("发现 1 条需要核对的理解")).toBeInTheDocument();
    expect(
      screen.getByText("用户希望正式方案给出明确推荐。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Light|REM|Deep|记忆标准/),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "现在整理" }));
    await waitFor(() =>
      expect(workspaceMock.runMemoryDreaming).toHaveBeenCalledWith({
        shadow: false,
      }),
    );
  });

  it("previews imported memories and only imports selected items", async () => {
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });
    await user.click(await screen.findByRole("button", { name: "记忆设置" }));
    await user.click(screen.getByRole("button", { name: "从其他 AI 导入" }));
    expect(await screen.findByText("先预览，后导入")).toBeInTheDocument();
    await user.type(
      screen.getByRole("textbox", { name: "其他 AI 返回的记忆" }),
      "长期偏好清单",
    );
    await user.click(screen.getByRole("button", { name: "分析并预览" }));
    expect(
      await screen.findByText("用户偏好自然、克制的中文表达。"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "用户偏好自然、克制的中文表达。" }),
    ).toBeChecked();
    await user.click(screen.getByRole("button", { name: "导入选中的 1 条" }));
    await waitFor(() =>
      expect(workspaceMock.commitMemoryImport).toHaveBeenCalledWith(
        "import-1",
        ["recommended"],
      ),
    );
    expect(await screen.findByText("记忆导入完成")).toBeInTheDocument();
  });

  it("requires an exact phrase before deleting every memory", async () => {
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });
    await user.click(await screen.findByRole("button", { name: "记忆设置" }));
    await user.click(screen.getByRole("button", { name: "删除所有记忆" }));
    expect(
      await screen.findByText("将清除约 25 项长期记忆"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/聊天记录、文件、知识库和技能不会删除/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "删除前创建一份本地恢复备份" }),
    ).toBeChecked();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "确认删除所有记忆" }),
      ).toBeDisabled(),
    );
    const confirm = screen.getByRole("button", { name: "确认删除所有记忆" });
    fireEvent.change(
      screen.getByRole("textbox", { name: "输入删除确认文字" }),
      {
        target: { value: "删除全部记忆" },
      },
    );
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() =>
      expect(workspaceMock.clearAllMemory).toHaveBeenCalledWith({
        confirmation: "删除全部记忆",
        create_backup: true,
      }),
    );
  });

  it("keeps recovery controls for archived and recently imported memories", async () => {
    workspaceMock.listUserMemory.mockResolvedValue({
      ...emptyUserMemory,
      entries: [
        {
          id: "archived-1",
          type: "style",
          content: "用户曾偏好旧的写作方式。",
          source: "agent",
          status: "archived",
          confidence: 0.9,
          reason: "",
          created_at: "2026-09-01T00:00:00Z",
          updated_at: "2026-09-02T00:00:00Z",
          archived_at: "2026-09-02T00:00:00Z",
        },
      ],
    });
    workspaceMock.getRecentMemoryImport.mockResolvedValue({
      batch: {
        batch_id: "import-1",
        provider: "Claude",
        status: "committed",
        imported_count: 1,
        committed_at: "2026-09-05T00:01:00Z",
        undone_at: "",
        can_undo: true,
      },
    });
    const user = userEvent.setup();
    renderWithProviders(<MemoryPage />, { initialEntries: ["/memory"] });
    await user.click(await screen.findByRole("button", { name: "记忆设置" }));

    await user.click(screen.getByRole("button", { name: "撤销最近导入" }));
    await user.click(await screen.findByRole("button", { name: "撤销导入" }));
    await waitFor(() =>
      expect(workspaceMock.undoMemoryImport).toHaveBeenCalledWith("import-1"),
    );

    await user.click(
      screen.getByRole("button", { name: /已停止使用的记忆（1）/ }),
    );
    await user.click(screen.getByRole("button", { name: "恢复使用" }));
    await waitFor(() =>
      expect(workspaceMock.updateUserMemory).toHaveBeenCalledWith(
        "archived-1",
        {
          status: "active",
        },
      ),
    );
  });
});
