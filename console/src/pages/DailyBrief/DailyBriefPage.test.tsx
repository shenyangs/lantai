import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { renderWithProviders } from "@/test/common_setup";
import type { BriefWorkspaceSnapshot } from "@/api/types/dailyBrief";
import DailyBriefPage from "./index";

const {
  getWorkspaceSnapshot,
  setInvitationHidden,
  activate,
  createGrant,
  revokeGrant,
  createSubscription,
  updateSubscription,
  createPreview,
  addFeedback,
  createDiscussionDraft,
  markDeliveryRead,
  getDeliveryReceipt,
  getEdition,
  getEditions,
  markEditionRead,
  getModelCapability,
  retryCycle,
  removeSavedItem,
  createWatchTopic,
  updateWatchTopic,
  configureWatchTopicForBrief,
  openExternalUrl,
} = vi.hoisted(() => ({
  getWorkspaceSnapshot: vi.fn(),
  setInvitationHidden: vi.fn(),
  activate: vi.fn(),
  createGrant: vi.fn(),
  revokeGrant: vi.fn(),
  createSubscription: vi.fn(),
  updateSubscription: vi.fn(),
  createPreview: vi.fn(),
  addFeedback: vi.fn(),
  createDiscussionDraft: vi.fn(),
  markDeliveryRead: vi.fn(),
  getDeliveryReceipt: vi.fn(),
  getEdition: vi.fn(),
  getEditions: vi.fn(),
  markEditionRead: vi.fn(),
  getModelCapability: vi.fn(),
  retryCycle: vi.fn(),
  removeSavedItem: vi.fn(),
  createWatchTopic: vi.fn(),
  updateWatchTopic: vi.fn(),
  configureWatchTopicForBrief: vi.fn(),
  openExternalUrl: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/api/modules/dailyBrief", () => ({
  dailyBriefApi: {
    getWorkspaceSnapshot,
    setInvitationHidden,
    activate,
    createGrant,
    revokeGrant,
    createSubscription,
    updateSubscription,
    createPreview,
    addFeedback,
    createDiscussionDraft,
    markDeliveryRead,
    getDeliveryReceipt,
    getEdition,
    getEditions,
    markEditionRead,
    getModelCapability,
    retryCycle,
    removeSavedItem,
  },
}));

vi.mock("@/api/modules/reminder", () => ({
  reminderApi: {
    createWatchTopic,
    updateWatchTopic,
    configureWatchTopicForBrief,
  },
}));

vi.mock("@/utils/openExternalUrl", () => ({ openExternalUrl }));

const snapshot: BriefWorkspaceSnapshot = {
  grants: [
    {
      id: "grant-1",
      revision: 1,
      status: "active",
      one_time: false,
      topic_ids: ["topic-1"],
      public_source_discovery: true,
      public_source_read: true,
      cloud_processing: "public_only",
      allowed_context_refs: [],
      search_provider: "approved-provider",
      model_provider: "approved-model",
      budget: {
        max_searches: 4,
        max_source_reads: 6,
        max_model_tokens: 4_000,
        max_cost_usd: 0.5,
      },
      granted_at: "2026-09-20T00:00:00Z",
      expires_at: null,
      revoked_at: null,
    },
  ],
  subscriptions: [],
  latest_edition: {
    id: "edition-1",
    revision: 2,
    cycle_id: "cycle-1",
    subscription_id: null,
    grant_id: "grant-1",
    grant_revision: 1,
    topic_revisions: { "topic-1": 1 },
    status: "published",
    outcome: "ready",
    local_date: "2026-09-20",
    timezone: "Asia/Shanghai",
    title: "一个值得继续判断的变化",
    summary: "这是经过查证的工作台内容。",
    coverage: [
      {
        topic_id: "topic-1",
        status: "complete",
        checked_at: "2026-09-20T07:58:00Z",
        query_count: 2,
        source_read_count: 1,
        note: "",
      },
    ],
    evidence: [
      {
        id: "evidence-1",
        canonical_url: "https://example.com/source",
        original_url: "https://example.com/source",
        origin_group: "example",
        source_kind: "primary",
        publisher: "源站",
        title: "原始资料",
        published_at: "2026-09-19T08:00:00Z",
        event_at: "2026-09-19T07:00:00Z",
        fetched_at: "2026-09-20T07:58:00Z",
        read_level: "primary_document",
        evidence_text: "测试证据摘要",
        content_hash: "0123456789abcdef",
        retracted: false,
      },
    ],
    items: [
      {
        id: "item-1",
        topic_id: "topic-1",
        title: "重点变化",
        fact: "原始资料明确记载了该变化。",
        relevance: "它可能影响用户正在做的产品决策。",
        analysis: "当前证据支持继续观察。",
        limitations: "只有一个主要来源。",
        optional_action: "可等待第二个来源后再决定。",
        evidence_ids: ["evidence-1"],
        private_dependency_refs: [],
        related_work_refs: [],
        explicit_work_association: false,
      },
    ],
    trends: [
      {
        id: "trend-1",
        topic_id: "topic-1",
        revision: 1,
        statement: "这个变化可能会延续。",
        support_evidence_ids: ["evidence-1"],
        counter_evidence_ids: [],
        falsifiers: ["后续公告撤回该变化"],
        uncertainty: "尚无第二来源。",
        supersedes_id: null,
        observed_at: "2026-09-20T07:58:00Z",
      },
    ],
    checked_started_at: "2026-09-20T07:55:00Z",
    checked_finished_at: "2026-09-20T07:58:00Z",
    published_at: "2026-09-20T08:00:00Z",
    supersedes_id: null,
    correction_note: "",
    content_hash: "fedcba9876543210",
  },
  history: [],
  topics: [
    {
      id: "topic-1",
      title: "公开产品变化",
      description: "",
      enabled: true,
      revision: 1,
      brief_status: "confirmed",
      brief_purpose: "跟踪与当前产品工作相关的变化",
      brief_scope_refs: [],
      brief_evidence_refs: [],
      brief_exclusions: [],
      brief_valid_until: null,
    },
  ],
  feature_flags: {
    homepage_entry: true,
    research: false,
    native_notification: false,
  },
  invitation_hidden: false,
};

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname + location.search}
    </output>
  );
}

describe("DailyBriefPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    getModelCapability.mockResolvedValue({
      ready: true,
      code: "ready",
      message: "模型可用",
    });
    markEditionRead.mockResolvedValue(undefined);
    markDeliveryRead.mockResolvedValue({ id: "delivery-1", state: "read" });
    openExternalUrl.mockResolvedValue(undefined);
    getDeliveryReceipt.mockResolvedValue({
      id: "delivery-1",
      edition_id: "edition-1",
      edition_revision: 2,
    });
    getEdition.mockResolvedValue(snapshot.latest_edition);
  });

  it("shows topics-only activation and an actionable failed attempt", async () => {
    getWorkspaceSnapshot.mockResolvedValue({
      ...snapshot,
      latest_edition: null,
      activation_state: "topics_only",
      cycles: [
        {
          id: "failed-1",
          status: "failed",
          stage: "failed",
          local_date: "2026-10-03",
          message: "上次生成被中断，请明确重试。",
          retryable: true,
          usage: {
            max_searches: 1,
            max_source_reads: 1,
            max_model_tokens: 200,
            max_cost_usd: 0,
          },
        },
      ],
    });
    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    expect(
      await screen.findByText("关注方向已确认，尚未开启简报"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("上次生成被中断，请明确重试。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试本次生成" })).toBeEnabled();
    expect(retryCycle).not.toHaveBeenCalled();
  });

  it("refreshes published results on focus without starting research", async () => {
    getWorkspaceSnapshot
      .mockResolvedValueOnce({ ...snapshot, latest_edition: null, cycles: [] })
      .mockResolvedValue(snapshot);
    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await screen.findByRole("button", { name: "开始设置" });
    fireEvent.focus(window);
    expect(await screen.findByText("重点变化")).toBeInTheDocument();
    expect(activate).not.toHaveBeenCalled();
  });

  it("preserves a newer history choice while refresh resolves an older edition", async () => {
    const user = userEvent.setup();
    const older = {
      ...snapshot.latest_edition!,
      id: "older",
      local_date: "2026-09-19",
      title: "较早的一期",
    };
    const newerChoice = {
      ...snapshot.latest_edition!,
      id: "choice",
      local_date: "2026-09-18",
      title: "新选择的一期",
    };
    let resolveOlder!: (value: typeof older) => void;
    getWorkspaceSnapshot
      .mockResolvedValueOnce({ ...snapshot, history: [older, newerChoice] })
      .mockResolvedValue({ ...snapshot, history: [newerChoice] });
    getEdition.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveOlder = resolve;
        }),
    );
    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(
      await screen.findByRole("button", { name: /2026年9月19日/ }),
    );
    fireEvent.focus(window);
    await waitFor(() => expect(getEdition).toHaveBeenCalledWith("older", 2));
    await user.click(screen.getByRole("button", { name: /2026年9月18日/ }));
    await act(async () => {
      resolveOlder(older);
    });
    expect(
      screen.getByRole("heading", { name: "新选择的一期" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "较早的一期" }),
    ).not.toBeInTheDocument();
  });

  it("restarts history pagination when a new publication shifts the page boundary", async () => {
    const user = userEvent.setup();
    const editions = Array.from({ length: 5 }, (_, index) => ({
      ...snapshot.latest_edition!,
      id: `history-${index}`,
      title: `历史第 ${index} 期`,
      local_date: `2026-09-${String(20 - index).padStart(2, "0")}`,
    }));
    getEdition.mockImplementation((id: string) =>
      Promise.resolve(editions.find((entry) => entry.id === id)),
    );
    getWorkspaceSnapshot
      .mockResolvedValueOnce({
        ...snapshot,
        history: editions.slice(1, 3),
        history_total: 4,
      })
      .mockResolvedValue({
        ...snapshot,
        history: editions.slice(0, 2),
        history_total: 5,
      });
    getEditions
      .mockResolvedValueOnce(editions.slice(3))
      .mockResolvedValueOnce(editions.slice(2));
    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(
      await screen.findByRole("button", { name: "加载更早的简报" }),
    );
    await screen.findByRole("button", { name: /2026年9月16日/ });
    await user.click(screen.getByRole("button", { name: /2026年9月16日/ }));
    fireEvent.focus(window);
    await waitFor(() => expect(getWorkspaceSnapshot).toHaveBeenCalledTimes(2));
    await user.click(
      await screen.findByRole("button", { name: "加载更早的简报" }),
    );
    expect(getEditions).toHaveBeenLastCalledWith(50, 2);
    expect(
      await screen.findByRole("button", { name: /2026年9月18日/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "加载更早的简报" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "历史第 4 期" }),
    ).toBeInTheDocument();
  });

  it("ignores a history page that returns after its pagination snapshot was replaced", async () => {
    const user = userEvent.setup();
    const edition = snapshot.latest_edition!;
    let resolveOldPage!: (value: (typeof edition)[]) => void;
    getWorkspaceSnapshot
      .mockResolvedValueOnce({
        ...snapshot,
        history: [edition],
        history_total: 3,
      })
      .mockResolvedValue({ ...snapshot, history: [edition], history_total: 4 });
    getEditions.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOldPage = resolve;
        }),
    );
    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(
      await screen.findByRole("button", { name: "加载更早的简报" }),
    );
    fireEvent.focus(window);
    await waitFor(() => expect(getWorkspaceSnapshot).toHaveBeenCalledTimes(2));
    await act(async () => {
      resolveOldPage([{ ...edition, id: "stale", local_date: "2026-09-01" }]);
    });
    expect(
      screen.queryByRole("button", { name: /2026年9月1日/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "加载更早的简报" }),
    ).toBeEnabled();
  });

  it("preserves the last saved selection when requests resolve out of order", async () => {
    const user = userEvent.setup();
    const older = {
      ...snapshot.latest_edition!,
      id: "older",
      title: "较早的收藏",
    };
    const newer = {
      ...snapshot.latest_edition!,
      id: "newer",
      title: "最后选择的收藏",
    };
    let resolveOlder!: (value: typeof older) => void;
    getWorkspaceSnapshot.mockResolvedValue({
      ...snapshot,
      saved_items: [
        {
          id: "saved-old",
          edition_id: "older",
          edition_revision: 2,
          item_id: "item-1",
          saved_at: "2026-09-18",
        },
        {
          id: "saved-new",
          edition_id: "newer",
          edition_revision: 2,
          item_id: "item-1",
          saved_at: "2026-09-19",
        },
      ],
    });
    getEdition.mockImplementation((id: string) =>
      id === "older"
        ? new Promise((resolve) => {
            resolveOlder = resolve;
          })
        : Promise.resolve(newer),
    );
    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(
      await screen.findByRole("button", { name: /查看 2026年9月18日 的收藏/ }),
    );
    await user.click(
      screen.getByRole("button", { name: /查看 2026年9月19日 的收藏/ }),
    );
    await screen.findByRole("heading", { name: "最后选择的收藏" });
    await act(async () => {
      resolveOlder(older);
    });
    expect(
      screen.getByRole("heading", { name: "最后选择的收藏" }),
    ).toBeInTheDocument();
  });

  it("opens the exact notification revision before marking it read", async () => {
    getWorkspaceSnapshot.mockResolvedValue(snapshot);
    getDeliveryReceipt.mockResolvedValue({
      id: "old",
      edition_id: "previous",
      edition_revision: 3,
    });
    getEdition.mockResolvedValue({
      ...snapshot.latest_edition,
      id: "previous",
      revision: 3,
      title: "通知对应的旧一期",
    });
    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief?delivery=old"],
    });
    expect(await screen.findByText("通知对应的旧一期")).toBeInTheDocument();
    expect(getEdition).toHaveBeenCalledWith("previous", 3);
    expect(markDeliveryRead).toHaveBeenCalledWith("old");
  });

  it("reuses an activation action after a lost response", async () => {
    const user = userEvent.setup();
    getWorkspaceSnapshot.mockResolvedValue({
      ...snapshot,
      feature_flags: { ...snapshot.feature_flags, research: true },
    });
    activate.mockRejectedValue(new Error("连接中断，请重试"));
    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));
    await user.click(
      screen.getByRole("checkbox", { name: /允许澜台搜索和读取公开来源/ }),
    );
    await user.click(screen.getByRole("button", { name: "先试读一次" }));
    await screen.findByText("连接中断，请重试");
    await user.click(screen.getByRole("button", { name: "先试读一次" }));
    expect(activate.mock.calls[0][0].action_id).toBe(
      activate.mock.calls[1][0].action_id,
    );
  });

  it("loads one existing snapshot without starting research or mutations", async () => {
    getWorkspaceSnapshot.mockResolvedValue(snapshot);

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });

    expect(
      await screen.findByRole("main", { name: "澜台简报工作台" }),
    ).toBeInTheDocument();
    expect(screen.getByText("重点变化")).toBeInTheDocument();
    expect(screen.getByText("趋势与反证")).toBeInTheDocument();
    expect(getWorkspaceSnapshot).toHaveBeenCalledTimes(1);
    expect(addFeedback).not.toHaveBeenCalled();
    expect(createDiscussionDraft).not.toHaveBeenCalled();
    expect(markDeliveryRead).not.toHaveBeenCalled();
  });

  it("introduces automatic discovery without making topic selection the product", async () => {
    getWorkspaceSnapshot.mockResolvedValue({
      ...snapshot,
      latest_edition: null,
      history: [],
    });

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });

    expect(
      await screen.findByText(/从当前有效的记忆与已确认方向中寻找线索/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "开始设置" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/先选择公开主题/)).not.toBeInTheDocument();
    expect(getWorkspaceSnapshot).toHaveBeenCalledTimes(1);
    expect(activate).not.toHaveBeenCalled();
  });

  it("marks only the receipt opened from a native notification as read", async () => {
    getWorkspaceSnapshot.mockResolvedValue(snapshot);
    markDeliveryRead.mockResolvedValue({ id: "delivery-1", state: "read" });

    renderWithProviders(
      <>
        <DailyBriefPage />
        <LocationProbe />
      </>,
      { initialEntries: ["/daily-brief?delivery=delivery-1"] },
    );

    await waitFor(() => {
      expect(markDeliveryRead).toHaveBeenCalledWith("delivery-1");
    });
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/daily-brief");
    });
  });

  it("opens authorization settings without producing reads or mutations", async () => {
    const user = userEvent.setup();
    getWorkspaceSnapshot.mockResolvedValue(snapshot);

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });

    await user.click(await screen.findByRole("button", { name: "简报设置" }));

    expect(
      screen.getByRole("dialog", { name: "简报设置" }),
    ).toBeInTheDocument();
    expect(getWorkspaceSnapshot).toHaveBeenCalledTimes(1);
    expect(createGrant).not.toHaveBeenCalled();
    expect(createPreview).not.toHaveBeenCalled();
    expect(createSubscription).not.toHaveBeenCalled();
    expect(configureWatchTopicForBrief).not.toHaveBeenCalled();
  });

  it("mounts settings outside the isolated workspace and removes the overlay on close", async () => {
    const user = userEvent.setup();
    getWorkspaceSnapshot.mockResolvedValue(snapshot);
    const { container } = renderWithProviders(
      <div style={{ isolation: "isolate" }}>
        <DailyBriefPage />
      </div>,
      { initialEntries: ["/daily-brief"] },
    );
    await user.click(await screen.findByRole("button", { name: "简报设置" }));
    const dialog = screen.getByRole("dialog", { name: "简报设置" });
    expect(container).not.toContainElement(dialog);
    expect(dialog.parentElement?.parentElement).toBe(document.body);
    await user.click(screen.getByRole("button", { name: "关闭简报设置" }));
    expect(
      screen.queryByRole("dialog", { name: "简报设置" }),
    ).not.toBeInTheDocument();
  });

  it("confirms a WatchTopic only after the explicit topic action", async () => {
    const user = userEvent.setup();
    const unconfigured = {
      ...snapshot,
      topics: [
        {
          ...snapshot.topics[0],
          brief_status: "unconfigured" as const,
          brief_purpose: "",
        },
      ],
    };
    getWorkspaceSnapshot.mockResolvedValue(unconfigured);
    configureWatchTopicForBrief.mockResolvedValue({
      ...unconfigured.topics[0],
      brief_status: "confirmed",
      revision: 2,
    });

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));
    expect(configureWatchTopicForBrief).not.toHaveBeenCalled();

    expect(
      screen.queryByRole("button", { name: "明确确认用于简报" }),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "调整关注方向（可选）" }),
    );

    await user.click(screen.getByRole("button", { name: "明确确认用于简报" }));

    await waitFor(() => {
      expect(configureWatchTopicForBrief).toHaveBeenCalledWith("topic-1", {
        expected_revision: 1,
        explicit_confirmation: true,
        status: "confirmed",
        purpose: expect.stringContaining("公开产品变化"),
        scope_refs: ["public:web", "public:primary-documents"],
        evidence_refs: [],
        exclusions: ["private:ungranted", "unverified:rumor"],
        valid_until: null,
      });
    });
    expect(createGrant).not.toHaveBeenCalled();
  });

  it("requires explicit quota consent before using a configured cloud model", async () => {
    const user = userEvent.setup();
    getWorkspaceSnapshot.mockResolvedValue({
      ...snapshot,
      feature_flags: { ...snapshot.feature_flags, research: true },
    });
    getModelCapability.mockResolvedValue({
      ready: true,
      code: "ready",
      model_label: "minimax-cn/MiniMax-M3",
      message: "将使用 minimax-cn/MiniMax-M3，会消耗该模型的额度或产生 API 费用。",
    });
    activate.mockResolvedValue({ cycle: { id: "configured-preview" } });
    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));

    expect(
      screen.getByText(/简报支持你在「设置 → 模型 → 回答后的后台整理」选定的模型/),
    ).toBeInTheDocument();
    expect(await screen.findByText(/将使用 minimax-cn\/MiniMax-M3/)).toBeInTheDocument();
    const consent = screen.getByRole("checkbox", {
      name: /允许澜台搜索和读取公开来源.*消耗该模型的额度或产生 API 费用/,
    });
    expect(consent).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "先试读一次" }));
    expect(activate).not.toHaveBeenCalled();
    await user.click(consent);
    await user.click(screen.getByRole("button", { name: "先试读一次" }));
    await waitFor(() => expect(activate).toHaveBeenCalledWith(expect.objectContaining({
      explicit_consent: true,
      configured_model_consent: true,
      mode: "preview",
    })));
  });

  it("keeps consent and allows retry after an actionable model failure", async () => {
    const user = userEvent.setup();
    getWorkspaceSnapshot.mockResolvedValue({
      ...snapshot,
      feature_flags: { ...snapshot.feature_flags, research: true },
    });
    activate.mockRejectedValue(
      new Error("请在设置中配置本地或免费模型后重试。"),
    );
    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));
    const consent = screen.getByRole("checkbox", {
      name: /允许澜台搜索和读取公开来源/,
    });
    await user.click(consent);
    await user.click(screen.getByRole("button", { name: "先试读一次" }));

    expect(
      await screen.findByText("请在设置中配置本地或免费模型后重试。"),
    ).toBeInTheDocument();
    expect(consent).toBeChecked();
    expect(screen.getByRole("button", { name: "先试读一次" })).toBeEnabled();
    activate.mockResolvedValue({ cycle: { id: "cycle-retry" } });
    await user.click(screen.getByRole("button", { name: "先试读一次" }));
    expect(
      await screen.findByText("一次试读已排队；开始和完成状态会在工作台更新。"),
    ).toBeInTheDocument();
    expect(activate).toHaveBeenCalledTimes(2);
    expect(createSubscription).not.toHaveBeenCalled();
  });

  it("creates a one-time grant and preview without creating a subscription", async () => {
    const user = userEvent.setup();
    const enabledSnapshot = {
      ...snapshot,
      feature_flags: { ...snapshot.feature_flags, research: true },
    };
    getWorkspaceSnapshot.mockResolvedValue(enabledSnapshot);
    activate.mockResolvedValue({ cycle: { id: "cycle-preview" } });

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));
    await user.click(
      screen.getByRole("checkbox", {
        name: /允许澜台搜索和读取公开来源/,
      }),
    );
    await user.click(
      screen.getByRole("checkbox", {
        name: /允许把选中的非敏感记忆线索片段交给已配置的辅助模型/,
      }),
    );
    await user.click(screen.getByRole("button", { name: "先试读一次" }));

    await waitFor(() => {
      expect(activate).toHaveBeenCalledWith(
        expect.objectContaining({
          explicit_consent: true,
          mode: "preview",
          topic_ids: ["topic-1"],
          use_memory_clues: true,
          memory_context_processing_consent: true,
          timezone: expect.any(String),
          home_visible: true,
          native_notifications: false,
        }),
      );
    });
    expect(createSubscription).not.toHaveBeenCalled();
  });

  it("keeps optional topic controls collapsed in the simple setup", async () => {
    const user = userEvent.setup();
    const enabledSnapshot = {
      ...snapshot,
      feature_flags: { ...snapshot.feature_flags, research: true },
    };
    getWorkspaceSnapshot.mockResolvedValue(enabledSnapshot);

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));

    expect(
      screen.getByText("让澜台每天替你留意值得关注的变化"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "开启每日简报" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("公开产品变化")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "调整关注方向（可选）" }),
    );
    expect(screen.getByText("公开产品变化")).toBeInTheDocument();
  });

  it("keeps provider, model, token and search quotas out of the simple setup", async () => {
    const user = userEvent.setup();
    const enabledSnapshot = {
      ...snapshot,
      feature_flags: { ...snapshot.feature_flags, research: true },
    };
    getWorkspaceSnapshot.mockResolvedValue(enabledSnapshot);

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));
    expect(
      screen.getByText("让澜台每天替你留意值得关注的变化"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("搜索提供商")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("模型提供商")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("搜索次数上限")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("模型 tokens 上限")).not.toBeInTheDocument();
    expect(screen.queryByText(/Bing RSS/)).not.toBeInTheDocument();
    expect(createGrant).not.toHaveBeenCalled();
    expect(createPreview).not.toHaveBeenCalled();
  });

  it("does not queue research while the research gate remains off", async () => {
    const user = userEvent.setup();
    getWorkspaceSnapshot.mockResolvedValue({
      ...snapshot,
      feature_flags: { ...snapshot.feature_flags, research: false },
    });

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));
    expect(screen.getByText("当前构建尚未开放研究门禁")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开启每日简报" })).toBeDisabled();
    expect(createGrant).not.toHaveBeenCalled();
    expect(createPreview).not.toHaveBeenCalled();
  });

  it("creates a daily subscription with notifications off by default", async () => {
    const user = userEvent.setup();
    const enabledSnapshot = {
      ...snapshot,
      feature_flags: { ...snapshot.feature_flags, research: true },
    };
    getWorkspaceSnapshot.mockResolvedValue(enabledSnapshot);
    activate.mockResolvedValue({ subscription: { id: "subscription-1" } });

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));
    await user.click(
      screen.getByRole("checkbox", {
        name: /允许澜台搜索和读取公开来源/,
      }),
    );
    await user.click(
      screen.getByRole("checkbox", {
        name: /允许把选中的非敏感记忆线索片段交给已配置的辅助模型/,
      }),
    );
    await user.click(screen.getByRole("button", { name: "开启每日简报" }));

    await waitFor(() => {
      expect(activate).toHaveBeenCalledWith(
        expect.objectContaining({
          explicit_consent: true,
          mode: "daily",
          topic_ids: ["topic-1"],
          use_memory_clues: true,
          memory_context_processing_consent: true,
          timezone: expect.any(String),
          home_visible: true,
          native_notifications: false,
        }),
      );
    });
    expect(createPreview).not.toHaveBeenCalled();
  });

  it("includes the current revision and an action id when pausing a subscription", async () => {
    const user = userEvent.setup();
    const subscription = {
      id: "subscription-1",
      revision: 3,
      status: "active" as const,
      grant_id: "grant-1",
      grant_revision: 1,
      topic_revisions: { "topic-1": 1 },
      timezone: "Asia/Shanghai",
      local_time: "08:00:00",
      schedule_revision: 1,
      home_visible: true,
      native_notifications: false,
      quiet_hours_start: null,
      quiet_hours_end: null,
      last_successful_check_at: null,
      last_successful_local_date: null,
      created_at: "2026-09-20T00:00:00Z",
      updated_at: "2026-09-20T00:00:00Z",
    };
    const configuredSnapshot = {
      ...snapshot,
      subscriptions: [subscription],
    };
    getWorkspaceSnapshot.mockResolvedValue(configuredSnapshot);
    updateSubscription.mockResolvedValue({
      ...subscription,
      revision: 4,
      status: "paused",
    });

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));
    await user.click(screen.getByRole("button", { name: "暂停" }));

    await waitFor(() => {
      expect(updateSubscription).toHaveBeenCalledWith("subscription-1", {
        status: "paused",
        expected_revision: 3,
        action_id: expect.stringMatching(/^brief-subscription-update:/),
      });
    });
  });

  it("changes the homepage invitation only after the explicit hide action", async () => {
    const user = userEvent.setup();
    getWorkspaceSnapshot.mockResolvedValue(snapshot);
    setInvitationHidden.mockResolvedValue(undefined);

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });
    await user.click(await screen.findByRole("button", { name: "简报设置" }));
    expect(setInvitationHidden).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "更多设置" }));
    await user.click(screen.getByRole("button", { name: "隐藏首页邀请" }));

    await waitFor(() => {
      expect(setInvitationHidden).toHaveBeenCalledWith(true);
    });
  });

  it("prepares a BriefContextRef draft without sending a message", async () => {
    const user = userEvent.setup();
    getWorkspaceSnapshot.mockResolvedValue(snapshot);
    createDiscussionDraft.mockResolvedValue({ id: "draft-1" });

    renderWithProviders(
      <>
        <DailyBriefPage />
        <LocationProbe />
      </>,
      {
        initialEntries: ["/daily-brief"],
      },
    );

    await user.click(await screen.findByRole("button", { name: /继续讨论/ }));
    await user.click(screen.getByRole("button", { name: "准备讨论草稿" }));

    await waitFor(() => {
      expect(createDiscussionDraft).toHaveBeenCalledWith("edition-1", {
        edition_revision: 2,
        item_id: "item-1",
        text: "我想继续讨论：重点变化",
        action_id: expect.stringMatching(/^brief-discussion:/),
      });
    });
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/chat?briefDraft=draft-1",
    );
  });

  it("opens source links through the existing guarded external-link adapter", async () => {
    const user = userEvent.setup();
    getWorkspaceSnapshot.mockResolvedValue(snapshot);

    renderWithProviders(<DailyBriefPage />, {
      initialEntries: ["/daily-brief"],
    });

    const sourceLinks = await screen.findAllByRole("link", { name: /源站/ });
    await user.click(sourceLinks[0]);

    expect(openExternalUrl).toHaveBeenCalledWith("https://example.com/source");
    expect(addFeedback).not.toHaveBeenCalled();
    expect(createDiscussionDraft).not.toHaveBeenCalled();
  });

  it("returns through the existing in-app history entry", async () => {
    const user = userEvent.setup();
    getWorkspaceSnapshot.mockResolvedValue(snapshot);

    renderWithProviders(
      <>
        <DailyBriefPage />
        <LocationProbe />
      </>,
      { initialEntries: ["/chat/session-one", "/daily-brief"] },
    );

    await user.click(await screen.findByRole("button", { name: "返回" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/chat/session-one",
    );
  });
});
