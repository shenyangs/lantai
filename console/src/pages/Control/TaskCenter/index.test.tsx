import { afterEach, describe, expect, it, vi } from "vitest";
import { Modal } from "antd";
import { renderWithProviders } from "../../../test/common_setup";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TaskCenterPage from "./index";
import { selectRequestedRuntimeRun } from "./runtimeRunSelection";
import { runtimeApi } from "../../../api/modules/runtime";
import { capabilityApi } from "../../../api/modules/capability";

const { messageSuccess } = vi.hoisted(() => ({ messageSuccess: vi.fn() }));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: {
      success: messageSuccess,
      error: vi.fn(),
      warning: vi.fn(),
    },
  }),
}));

vi.mock("../../../api/modules/chat", () => ({
  chatApi: {
    listChats: vi.fn(async () => []),
  },
}));

vi.mock("../../../api/modules/cronjob", () => ({
  cronJobApi: {
    listCronJobs: vi.fn(async () => []),
    getCronJobState: vi.fn(async () => null),
  },
}));

vi.mock("../../../api/modules/heartbeat", () => ({
  heartbeatApi: {
    getHeartbeatConfig: vi.fn(async () => ({ enabled: true })),
    updateHeartbeatConfig: vi.fn(async () => ({})),
  },
}));

vi.mock("../../../api/modules/capability", () => ({
  capabilityApi: {
    listTaskRuns: vi.fn(async () => []),
  },
}));

vi.mock("../../../api/modules/runtime", () => ({
  runtimeApi: {
    listRuns: vi.fn(async () => ({
      total: 1,
      items: [
        {
          id: "run-1",
          display_id: "run-1",
          source: "cron",
          title: "日报整理",
          summary: "日报整理 · agent",
          status: "completed",
          started_at: "2026-05-22T08:00:00Z",
          ended_at: "2026-05-22T08:05:00Z",
          duration_ms: 300000,
          model: null,
          chat_id: "chat-run-1",
          session_id: "console:run-1",
          workspace_dir: "/tmp/topic-alpha",
          can_interrupt: false,
          can_retry: true,
          usage: {
            input_tokens: 10,
            output_tokens: 5,
            cache_tokens: 0,
            context_tokens: 100,
            cost_points: 0,
          },
          last_event_summary: "定时任务已结束",
          event_count: 2,
          debug_refs: { run_id: "run-1" },
        },
      ],
    })),
    getRunEvents: vi.fn(async () => ({ events: [] })),
    interruptRun: vi.fn(async () => ({ stopped: false, run_id: "run-1" })),
    retryRun: vi.fn(async () => ({ queued: false, run_id: "run-1" })),
    pauseRun: vi.fn(async () => ({ paused: true, run_id: "run-1" })),
    resumeRun: vi.fn(async () => ({ resumed: true, run_id: "run-1" })),
    archiveRun: vi.fn(async () => ({ archived: true, run_id: "run-1" })),
    exportRun: vi.fn(async () => ({
      exported: true,
      run_id: "run-1",
      package: {
        schema_version: 1,
        exported_at: "2026-07-17T00:00:00Z",
        exported_by: "task-center",
        export_reason: "Task Center 导出运行审计包",
        contract: { contract_id: "contract-run-1" },
        runtime_run: { run_id: "run-1" },
        trace_events: [],
        artifact_manifest: {
          schema_version: 1,
          manifest_hash: "sha256:manifest",
          artifacts: [],
        },
        retention_summary: {
          artifact_count: 0,
          retention_policies: {},
          contract_retention_days: 30,
          export_requires_approval: true,
          delete_artifacts_on_contract_delete: false,
        },
      },
    })),
    updateRunBudget: vi.fn(async () => ({
      changed: true,
      run_id: "run-1",
      revision: 2,
      budget: { max_steps: 20, max_seconds: 1800 },
    })),
    resolveRunEffect: vi.fn(async () => ({
      resolved: true,
      run_id: "run-1",
      effect_id: "effect-click-publish",
      effect_revision: 3,
      effect_status: "human_resolved",
    })),
    repairRunEffect: vi.fn(async () => ({
      repaired: true,
      run_id: "run-1",
      effect_id: "effect-click-publish",
      action: "rebuild_checkpoint",
      effect_revision: 3,
      effect_status: "committed",
    })),
    continueRun: vi.fn(async () => ({
      queued: false,
      reconnected: false,
      run_id: "run-1",
      chat_id: "chat-run-1",
      text: "续跑提示",
    })),
  },
}));

vi.mock("../../../api/modules/debug", () => ({
  debugApi: {
    getTurnEvents: vi.fn(async () => ({
      items: [
        {
          id: "evt-1",
          ts: "2026-05-22T08:00:00Z",
          chat_id: null,
          session_id: null,
          run_id: null,
          scope: "task",
          kind: "cron.job.started",
          status: "running",
          summary: "定时任务已开始",
          label: "定时开始",
          detail: "日报整理 · agent",
          workspace_dir: "/tmp/topic-alpha",
        },
        {
          id: "evt-2",
          ts: "2026-05-22T08:05:00Z",
          chat_id: null,
          session_id: null,
          run_id: null,
          scope: "task",
          kind: "heartbeat.run.settled",
          status: "finished",
          summary: "心跳已结束",
          label: "心跳结束",
          detail: "已完成 · main",
          workspace_dir: "/tmp/topic-alpha",
        },
        {
          id: "evt-3",
          ts: "2026-05-22T08:10:00Z",
          chat_id: null,
          session_id: null,
          run_id: null,
          scope: "task",
          kind: "heartbeat.run.settled",
          status: "finished",
          summary: "心跳任务已跳过",
          label: "心跳结束",
          detail: "已超时",
          workspace_dir: "/tmp/topic-beta",
        },
      ],
    })),
  },
}));

describe("TaskCenterPage", () => {
  afterEach(() => {
    Modal.destroyAll();
  });
  it("selects the requested runtime run only when the drawer still needs it", () => {
    const run = {
      id: "run-1",
      display_id: "run-1",
      source: "chat",
      title: "测试运行",
      summary: "测试摘要",
      status: "completed",
      can_interrupt: false,
      can_retry: true,
      usage: {
        input_tokens: 0,
        output_tokens: 0,
        cache_tokens: 0,
        context_tokens: 0,
        cost_points: 0,
      },
      last_event_summary: "",
      event_count: 0,
      debug_refs: {},
    } as any;

    expect(
      selectRequestedRuntimeRun({
        requestedRunId: "run-1",
        runtimeRuns: [run],
        runtimeRunsAvailable: true,
        loading: false,
        runtimeDrawerOpen: false,
        selectedRunId: null,
      })?.id,
    ).toBe("run-1");

    expect(
      selectRequestedRuntimeRun({
        requestedRunId: "run-1",
        runtimeRuns: [run],
        runtimeRunsAvailable: true,
        loading: false,
        runtimeDrawerOpen: true,
        selectedRunId: "run-1",
      }),
    ).toBeNull();
  });

  it("renders recent task ledger events for cron and heartbeat", async () => {
    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    await waitFor(() => {
      expect(screen.getByText("自动运行动态")).toBeInTheDocument();
    });

    expect(screen.getByText("定时开始")).toBeInTheDocument();
    expect(screen.getAllByText("日报整理 · agent").length).toBeGreaterThan(0);
    expect(screen.getAllByText("心跳结束").length).toBeGreaterThan(0);
    expect(screen.getByText("已完成 · main")).toBeInTheDocument();
    expect(screen.getAllByText("topic-alpha").length).toBeGreaterThan(0);
    expect(screen.getAllByText("打开定时").length).toBeGreaterThan(0);
    expect(screen.getByText("去检查心跳")).toBeInTheDocument();
  });

  it("filters task events by heartbeat and cron", async () => {
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    await waitFor(() => {
      expect(screen.getByText("自动运行动态")).toBeInTheDocument();
    });

    await user.click(screen.getByText("仅心跳"));
    expect(screen.queryByText("定时开始")).not.toBeInTheDocument();
    expect(screen.getAllByText("心跳结束").length).toBe(2);

    await user.click(screen.getByText("仅定时"));
    expect(screen.getByText("定时开始")).toBeInTheDocument();
    expect(screen.queryByText("心跳结束")).not.toBeInTheDocument();
    expect(screen.getAllByText("打开定时").length).toBeGreaterThan(0);
  });

  it("filters task events to issues only", async () => {
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    await waitFor(() => {
      expect(screen.getByText("自动运行动态")).toBeInTheDocument();
    });

    await user.click(screen.getByText("仅异常"));

    expect(screen.queryByText("定时开始")).not.toBeInTheDocument();
    expect(screen.queryByText("已完成 · main")).not.toBeInTheDocument();
    expect(screen.getByText("已超时")).toBeInTheDocument();
    expect(screen.getByText("topic-beta")).toBeInTheDocument();
    expect(screen.getByText("去检查心跳")).toBeInTheDocument();
  });

  it("renders stalled runtime runs and filters them explicitly", async () => {
    window.sessionStorage.clear();
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 2,
      items: [
        {
          id: "run-stalled",
          display_id: "run-stalled",
          source: "chat",
          attempt_id: "run-stalled:attempt-1",
          title: "前端预览修复",
          summary: "本轮可能卡住",
          status: "stalled",
          authority_status: "stalled",
          transport_status: "stalled",
          revision: 5,
          started_at: "2026-05-22T08:00:00Z",
          ended_at: "2026-05-22T08:02:00Z",
          duration_ms: 120000,
          model: null,
          chat_id: "chat-stalled",
          session_id: "console:stalled",
          workspace_dir: "/tmp/topic-alpha",
          can_interrupt: false,
          can_retry: true,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "本轮可能卡住",
          event_count: 2,
          debug_refs: { latest_seq: 12 },
        },
        {
          id: "run-ok",
          display_id: "run-ok",
          source: "chat",
          title: "已完成运行",
          summary: "本轮已结束",
          status: "completed",
          can_interrupt: false,
          can_retry: true,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "本轮已结束",
          event_count: 1,
          debug_refs: {},
        },
      ],
    } as any);
    vi.mocked(runtimeApi.continueRun).mockResolvedValueOnce({
      queued: false,
      reconnected: false,
      run_id: "run-stalled",
      chat_id: "chat-stalled",
      session_id: "console:stalled",
      reason: "continue_requires_frontend_resubmit",
      latest_seq: 12,
      text: "请从预览卡住处继续",
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    expect(await screen.findByText("前端预览修复")).toBeInTheDocument();
    expect(screen.getByText("已卡住")).toBeInTheDocument();

    await user.click(screen.getByText("卡住"));

    expect(screen.getByText("前端预览修复")).toBeInTheDocument();
    expect(screen.queryByText("已完成运行")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "续跑" }));

    await waitFor(() => {
      expect(runtimeApi.continueRun).toHaveBeenCalledWith("run-stalled", {
        expected_attempt_id: "run-stalled:attempt-1",
        expected_revision: 5,
        chat_id: "chat-stalled",
        session_id: "console:stalled",
        after_seq: 12,
        stage_label: "本轮可能卡住",
        workspace_dir: "/tmp/topic-alpha",
      });
    });
    expect(
      window.sessionStorage.getItem("lantai:retry-draft:chat-stalled"),
    ).toBe("请从预览卡住处继续");
  });

  it("renders canonical runtime projection control fields", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "capability-store-run",
          display_id: "capabili",
          source: "capability",
          attempt_id: "capability-store-run:attempt-1",
          title: "Capability Runtime",
          summary: "等待人工确认",
          status: "running",
          authority_status: "waiting_approval",
          transport_status: "unknown",
          revision: 4,
          started_at: "2026-07-16T12:00:00Z",
          ended_at: null,
          duration_ms: null,
          model: null,
          chat_id: null,
          session_id: "capability-session",
          workspace_dir: "/tmp/topic-alpha",
          lease_owner: "capability-worker",
          lease_expires_at: "2026-07-16T12:05:00Z",
          heartbeat_at: "2026-07-16T12:00:30Z",
          release_edition: "official",
          current_step: {
            step_id: "approval-1",
            index: 2,
            kind: "approval",
            title: "等待人工确认",
          },
          budget: { max_steps: 12, max_seconds: 900 },
          checkpoint: {
            checkpoint_id: "checkpoint-1",
            seq: 8,
            summary: "draft created",
          },
          pending_approval: true,
          ambiguous_effect: false,
          evaluation_summary: { conclusion: "needs_revision" },
          artifact_count: 1,
          recovery: {
            can_takeover: true,
            can_resume: false,
            resume_requires_durable_lease: true,
          },
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          projection_source: "runtime_store",
          debug_refs: { run_id: "capability-store-run" },
        },
      ],
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    expect(
      await within(runtimeSection as HTMLElement).findByText(
        "Capability Runtime",
      ),
    ).toBeInTheDocument();
    expect(
      within(runtimeSection as HTMLElement).queryByText(
        "Attempt capability...mpt-1",
      ),
    ).not.toBeInTheDocument();
    expect(
      within(runtimeSection as HTMLElement).queryByText("权威 等待审批"),
    ).not.toBeInTheDocument();
    expect(
      within(runtimeSection as HTMLElement).queryByText("Step #3 等待人工确认"),
    ).not.toBeInTheDocument();

    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: /Capability Runtime/,
      }),
    );

    expect(await screen.findByText("Current Step")).toBeInTheDocument();
    expect(screen.getByText("Attempt capability...mpt-1")).toBeInTheDocument();
    expect(screen.getByText("权威 等待审批")).toBeInTheDocument();
    expect(screen.getAllByText("#3 等待人工确认").length).toBeGreaterThan(0);
    expect(screen.getByText("12 步 · 900 秒")).toBeInTheDocument();
    expect(screen.getByText("capability-worker")).toBeInTheDocument();
    expect(screen.getByText("draft created")).toBeInTheDocument();
    expect(screen.getByText("needs_revision")).toBeInTheDocument();
    expect(screen.getByText("可接管 · 需接管恢复")).toBeInTheDocument();
  });

  it("shows a contract-backed empty trace state for store-only runs", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "store-only-run",
          display_id: "store-on",
          source: "capability",
          attempt_id: "store-only-run:attempt-1",
          title: "Store Only Runtime",
          summary: "只有 RuntimeRun 合同",
          status: "running",
          authority_status: "running",
          transport_status: "unknown",
          revision: 2,
          started_at: "2026-07-16T12:00:00Z",
          ended_at: null,
          duration_ms: null,
          model: null,
          chat_id: null,
          session_id: "store-session",
          workspace_dir: "/tmp/topic-store-only",
          lease_owner: "worker",
          heartbeat_at: "2026-07-16T12:00:30Z",
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          projection_source: "runtime_store",
          debug_refs: { run_id: "store-only-run" },
        },
      ],
    } as any);
    vi.mocked(runtimeApi.getRunEvents).mockResolvedValueOnce({
      run_id: "store-only-run",
      attempt_id: "store-only-run:attempt-1",
      workspace_dir: "/tmp/topic-store-only",
      after_seq: 0,
      latest_seq: 0,
      events: [],
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();

    await user.click(
      await within(runtimeSection as HTMLElement).findByRole("button", {
        name: /Store Only Runtime/,
      }),
    );

    expect(
      (await screen.findAllByText("Store Only Runtime")).length,
    ).toBeGreaterThan(1);
    expect(
      screen.getByText("运行合同已保存，暂无事件记录"),
    ).toBeInTheDocument();
    expect(runtimeApi.getRunEvents).toHaveBeenCalledWith("store-only-run");
  });

  it("keeps legacy task runs out of the runtime console projection", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "canonical-run",
          display_id: "canonica",
          source: "capability",
          title: "Canonical Runtime Row",
          summary: "来自 RuntimeRun projection",
          status: "running",
          authority_status: "running",
          transport_status: "unknown",
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          debug_refs: {},
        },
      ],
    } as any);
    vi.mocked(capabilityApi.listTaskRuns).mockResolvedValueOnce([
      {
        id: "legacy-task-run",
        agent_id: "agent-default",
        source: "capability",
        title: "Legacy Failed TaskRun",
        user_intent: "旧 TaskRun 状态",
        status: "failed",
        started_at: "2026-07-16T11:00:00Z",
        steps: [],
        artifacts: [],
      },
    ] as any);

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    const taskRunSection = (
      await screen.findByRole("heading", { name: "可沉淀任务" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    expect(taskRunSection).not.toBeNull();

    expect(
      await within(runtimeSection as HTMLElement).findByText(
        "Canonical Runtime Row",
      ),
    ).toBeInTheDocument();
    expect(
      within(runtimeSection as HTMLElement).queryByText(
        "Legacy Failed TaskRun",
      ),
    ).not.toBeInTheDocument();
    expect(
      await within(taskRunSection as HTMLElement).findByText(
        "Legacy Failed TaskRun",
      ),
    ).toBeInTheDocument();
  });

  it("sends expected attempt and revision when interrupting a runtime run", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "interrupt-run",
          display_id: "interrup",
          source: "chat",
          attempt_id: "interrupt-run:attempt-2",
          title: "Interruptible Runtime",
          summary: "正在运行",
          status: "running",
          authority_status: "running",
          transport_status: "running",
          revision: 9,
          chat_id: "chat-interrupt",
          session_id: "console:interrupt",
          workspace_dir: "/tmp/topic-alpha",
          can_interrupt: true,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          debug_refs: {},
        },
      ],
    } as any);
    vi.mocked(runtimeApi.interruptRun).mockResolvedValueOnce({
      stopped: true,
      run_id: "interrupt-run",
      chat_id: "chat-interrupt",
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await within(runtimeSection as HTMLElement).findByText(
      "Interruptible Runtime",
    );
    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: "中断",
      }),
    );

    await waitFor(() => {
      expect(runtimeApi.interruptRun).toHaveBeenCalledWith("interrupt-run", {
        expected_attempt_id: "interrupt-run:attempt-2",
        expected_revision: 9,
        chat_id: "chat-interrupt",
        session_id: "console:interrupt",
        workspace_dir: "/tmp/topic-alpha",
      });
    });
  });

  it("recognizes an accepted cron interruption without claiming settlement", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "interrupt-run",
          display_id: "interrup",
          source: "cron",
          attempt_id: "interrupt-run:attempt-2",
          title: "Interruptible Runtime",
          summary: "正在运行",
          status: "running",
          authority_status: "running",
          transport_status: "running",
          revision: 9,
          chat_id: "chat-interrupt",
          session_id: "console:interrupt",
          workspace_dir: "/tmp/topic-alpha",
          can_interrupt: true,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          debug_refs: {},
        },
      ],
    } as any);
    vi.mocked(runtimeApi.interruptRun).mockResolvedValueOnce({
      stopped: false,
      accepted: true,
      run_id: "interrupt-run",
      chat_id: "chat-interrupt",
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await within(runtimeSection as HTMLElement).findByText(
      "Interruptible Runtime",
    );
    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: "中断",
      }),
    );

    await waitFor(() => {
      expect(messageSuccess).toHaveBeenCalledWith("已请求中断运行。");
    });
    await waitFor(() => {
      expect(runtimeApi.interruptRun).toHaveBeenCalledWith("interrupt-run", {
        expected_attempt_id: "interrupt-run:attempt-2",
        expected_revision: 9,
        chat_id: "chat-interrupt",
        session_id: "console:interrupt",
        workspace_dir: "/tmp/topic-alpha",
      });
    });
  });

  it("sends expected attempt and revision when archiving a terminal runtime run", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "archive-run",
          display_id: "archive",
          source: "chat",
          attempt_id: "archive-run:attempt-1",
          title: "Archived Candidate",
          summary: "本轮已结束",
          status: "completed",
          authority_status: "succeeded",
          transport_status: "unknown",
          revision: 8,
          chat_id: "chat-archive",
          session_id: "console:archive",
          workspace_dir: "/tmp/topic-alpha",
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          debug_refs: {},
        },
      ],
    } as any);
    vi.mocked(runtimeApi.archiveRun).mockResolvedValueOnce({
      archived: true,
      run_id: "archive-run",
      attempt_id: "archive-run:attempt-1",
      revision: 8,
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await within(runtimeSection as HTMLElement).findByText(
      "Archived Candidate",
    );
    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: "归档",
      }),
    );

    await waitFor(() => {
      expect(runtimeApi.archiveRun).toHaveBeenCalledWith("archive-run", {
        expected_attempt_id: "archive-run:attempt-1",
        expected_revision: 8,
        chat_id: "chat-archive",
        session_id: "console:archive",
        workspace_dir: "/tmp/topic-alpha",
      });
    });
  });

  it("exports a runtime run with approval and shows retention impact", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "export-run",
          display_id: "export",
          source: "capability",
          attempt_id: "export-run:attempt-1",
          title: "Exportable Runtime",
          summary: "可导出审计包",
          status: "completed",
          authority_status: "succeeded",
          transport_status: "unknown",
          revision: 12,
          chat_id: "chat-export",
          session_id: "console:export",
          workspace_dir: "/tmp/topic-export",
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 2,
          projection_source: "runtime_store",
          debug_refs: {},
        },
      ],
    } as any);
    vi.mocked(runtimeApi.getRunEvents).mockResolvedValueOnce({
      run_id: "export-run",
      attempt_id: "export-run:attempt-1",
      workspace_dir: "/tmp/topic-export",
      after_seq: 0,
      latest_seq: 2,
      events: [],
    } as any);
    vi.mocked(runtimeApi.exportRun).mockResolvedValueOnce({
      exported: true,
      run_id: "export-run",
      attempt_id: "export-run:attempt-1",
      revision: 12,
      workspace_dir: "/tmp/topic-export",
      export_approval: {
        approved: true,
        reason: "Task Center 导出运行审计包",
        exported_by: "task-center",
      },
      package: {
        schema_version: 1,
        exported_at: "2026-07-17T00:00:00Z",
        exported_by: "task-center",
        export_reason: "Task Center 导出运行审计包",
        contract: {
          contract_id: "contract-export",
          contract_hash: "sha256:contract-export",
        },
        runtime_run: { run_id: "export-run", status: "completed" },
        trace_events: [
          {
            event_id: "event-1",
            seq: 1,
            type: "runtime.started",
            source: "runtime",
            summary: "开始",
          },
          {
            event_id: "event-2",
            seq: 2,
            type: "runtime.completed",
            source: "runtime",
            summary: "完成",
          },
        ],
        artifact_manifest: {
          schema_version: 1,
          manifest_hash: "sha256:manifest-export",
          artifacts: [{ artifact_id: "artifact-1" }],
        },
        retention_summary: {
          artifact_count: 1,
          retention_policies: { "30d": 1 },
          contract_retention_days: 30,
          export_requires_approval: true,
          delete_artifacts_on_contract_delete: false,
        },
        version_info: { client: "task-center" },
      },
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await user.click(
      await within(runtimeSection as HTMLElement).findByRole("button", {
        name: /Exportable Runtime/,
      }),
    );
    await user.click(await screen.findByRole("button", { name: "导出审计包" }));

    await waitFor(() => {
      expect(runtimeApi.exportRun).toHaveBeenCalledWith("export-run", {
        expected_attempt_id: "export-run:attempt-1",
        expected_revision: 12,
        chat_id: "chat-export",
        session_id: "console:export",
        workspace_dir: "/tmp/topic-export",
        approved: true,
        reason: "Task Center 导出运行审计包",
        exported_by: "task-center",
        version_info: { client: "task-center" },
      });
    });
    expect(await screen.findByText("导出包已生成")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Trace 2 条 · 产物 1 个 · 合同保留 30 天 · 导出需审批 · 合同删除时保留产物",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/Contract contract-export/)).toBeInTheDocument();
  });

  it("renders deduplicated runtime notification advisories in run details", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "notify-run",
          display_id: "notify",
          source: "chat",
          attempt_id: "notify-run:attempt-1",
          title: "Notification Runtime",
          summary: "等待人工确认",
          status: "waiting_approval",
          authority_status: "waiting_approval",
          transport_status: "running",
          revision: 7,
          chat_id: "chat-notify",
          session_id: "console:notify",
          workspace_dir: "/tmp/topic-notify",
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "等待发布确认",
          event_count: 3,
          projection_source: "runtime_store",
          debug_refs: {},
        },
      ],
    } as any);
    vi.mocked(runtimeApi.getRunEvents).mockResolvedValueOnce({
      run_id: "notify-run",
      attempt_id: "notify-run:attempt-1",
      workspace_dir: "/tmp/topic-notify",
      after_seq: 0,
      latest_seq: 3,
      events: [
        {
          id: "notify-approval-old",
          seq: 1,
          type: "runtime.notification",
          status: "info",
          source: "system",
          payload: {
            notification_kind: "waiting_approval",
            title: "运行等待审批",
            message: "旧的审批提醒",
            dedupe_key: "runtime-notify:approval",
            source_status: "waiting_approval",
            contract_id: "contract-notify",
            action_label: "打开审批",
            export_recommended: false,
          },
          usage: null,
          created_at: "2026-07-17T08:00:00Z",
        },
        {
          id: "notify-approval-new",
          seq: 2,
          type: "runtime.notification",
          status: "info",
          source: "system",
          payload: {
            notification_kind: "waiting_approval",
            title: "运行等待审批",
            message: "等待发布确认",
            dedupe_key: "runtime-notify:approval",
            source_status: "waiting_approval",
            contract_id: "contract-notify",
            action_label: "打开审批",
            export_recommended: false,
          },
          usage: null,
          created_at: "2026-07-17T08:00:20Z",
        },
        {
          id: "notify-budget",
          seq: 3,
          type: "runtime.notification",
          status: "failed",
          source: "system",
          payload: {
            notification_kind: "budget_exhausted",
            title: "运行预算已耗尽",
            message: "运行预算或停止条件已触发。",
            dedupe_key: "runtime-notify:budget",
            source_status: "failed",
            contract_id: "contract-budget",
            export_recommended: true,
          },
          usage: null,
          created_at: "2026-07-17T08:01:00Z",
        },
      ],
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await user.click(
      await within(runtimeSection as HTMLElement).findByRole("button", {
        name: /Notification Runtime/,
      }),
    );

    expect(await screen.findByText("运行提醒")).toBeInTheDocument();
    expect(screen.getAllByText("运行等待审批")).toHaveLength(1);
    expect(screen.getByText("等待发布确认")).toBeInTheDocument();
    expect(screen.queryByText("旧的审批提醒")).not.toBeInTheDocument();
    expect(screen.getByText("运行预算已耗尽")).toBeInTheDocument();
    expect(screen.getByText(/建议导出审计包/)).toBeInTheDocument();
  });

  it("edits runtime budget with fenced contract context", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "budget-run",
          display_id: "budget",
          source: "capability",
          attempt_id: "budget-run:attempt-1",
          title: "Budget Runtime",
          summary: "运行预算即将耗尽",
          status: "running",
          authority_status: "running",
          transport_status: "unknown",
          revision: 6,
          chat_id: null,
          session_id: "budget-session",
          workspace_dir: "/tmp/topic-budget",
          budget: {
            max_steps: 12,
            max_seconds: 900,
            deadline_at: "2026-07-17T10:00:00.000Z",
          },
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          projection_source: "runtime_store",
          debug_refs: {},
        },
      ],
    } as any);
    vi.mocked(runtimeApi.updateRunBudget).mockResolvedValueOnce({
      changed: true,
      run_id: "budget-run",
      attempt_id: "budget-run:attempt-1",
      revision: 7,
      status: "running",
      budget: {
        max_steps: 18,
        max_seconds: 1200,
        max_tool_calls: 7,
        deadline_at: "2026-07-17T10:30:00.000Z",
      },
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await within(runtimeSection as HTMLElement).findByText("Budget Runtime");

    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: "编辑预算",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "调整运行预算",
    });
    await user.clear(within(dialog).getByLabelText("最大步数"));
    await user.type(within(dialog).getByLabelText("最大步数"), "18");
    await user.type(within(dialog).getByLabelText("最大工具调用"), "7");
    await user.clear(within(dialog).getByLabelText("最长秒数"));
    await user.type(within(dialog).getByLabelText("最长秒数"), "1200");
    await user.clear(within(dialog).getByLabelText("截止时间"));
    await user.type(
      within(dialog).getByLabelText("截止时间"),
      "2026-07-17T10:30:00.000Z",
    );
    await user.clear(within(dialog).getByLabelText("调整原因"));
    await user.type(
      within(dialog).getByLabelText("调整原因"),
      "人工批准继续验证",
    );
    await user.click(within(dialog).getByRole("button", { name: "保存预算" }));

    await waitFor(() => {
      expect(runtimeApi.updateRunBudget).toHaveBeenCalledWith("budget-run", {
        expected_attempt_id: "budget-run:attempt-1",
        expected_revision: 6,
        chat_id: null,
        session_id: "budget-session",
        workspace_dir: "/tmp/topic-budget",
        budget: {
          max_turns: null,
          max_steps: 18,
          max_seconds: 1200,
          max_tool_calls: 7,
          max_tokens: null,
          max_cost_points: null,
          max_artifact_bytes: null,
          deadline_at: "2026-07-17T10:30:00.000Z",
        },
        changed_by: "user",
        reason: "人工批准继续验证",
      });
    });
  });

  it("surfaces effect recovery state in the runtime details drawer", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "browser-ambiguous-run",
          display_id: "browser-",
          source: "browser",
          attempt_id: "browser-ambiguous-run:attempt-1",
          title: "Browser Runtime",
          summary: "浏览器动作可能已经执行",
          status: "running",
          authority_status: "running",
          transport_status: "unknown",
          revision: 6,
          started_at: "2026-07-16T12:00:00Z",
          chat_id: null,
          session_id: "browser-session",
          workspace_dir: "/tmp/topic-alpha",
          pending_approval: false,
          ambiguous_effect: true,
          recovery: {
            can_takeover: true,
            effect_requires_probe: true,
            effect_replay_forbidden: true,
            effect_checkpoint_rebuild_required: true,
            effects: [
              {
                effect_id: "effect-click-publish",
                tool_name: "browser.click",
                status: "running",
                revision: 1,
              },
            ],
          },
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          projection_source: "runtime_store",
          debug_refs: { run_id: "browser-ambiguous-run" },
        },
      ],
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: /Browser Runtime/,
      }),
    );

    expect(await screen.findByText(/副作用待判定/)).toBeInTheDocument();
    expect(
      screen.getByText("可接管 · 需探测 · 需修复 checkpoint · 禁止自动重放"),
    ).toBeInTheDocument();
    expect(screen.getByText("副作用处理")).toBeInTheDocument();
    expect(screen.getByText("browser.click")).toBeInTheDocument();
    expect(screen.getByText("执行中 · Rev 1")).toBeInTheDocument();
  });

  it("sends run and effect fences when resolving a runtime effect", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "browser-ambiguous-run",
          display_id: "browser-",
          source: "browser",
          attempt_id: "browser-ambiguous-run:attempt-1",
          title: "Browser Runtime",
          summary: "浏览器动作可能已经执行",
          status: "running",
          authority_status: "running",
          transport_status: "unknown",
          revision: 6,
          session_id: "browser-session",
          workspace_dir: "/tmp/topic-alpha",
          pending_approval: false,
          ambiguous_effect: true,
          recovery: {
            effect_requires_human: true,
            effects: [
              {
                effect_id: "effect-click-publish",
                tool_name: "browser.click",
                status: "ambiguous",
                revision: 2,
              },
            ],
          },
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          debug_refs: { run_id: "browser-ambiguous-run" },
        },
      ],
    } as any);
    vi.mocked(runtimeApi.resolveRunEffect).mockResolvedValueOnce({
      resolved: true,
      run_id: "browser-ambiguous-run",
      effect_id: "effect-click-publish",
      effect_revision: 3,
      effect_status: "human_resolved",
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: /Browser Runtime/,
      }),
    );
    await user.click(await screen.findByRole("button", { name: "人工已处理" }));
    await user.click(
      await screen.findByRole("button", { name: "确认并记录" }),
    );

    await waitFor(() => {
      expect(runtimeApi.resolveRunEffect).toHaveBeenCalledWith(
        "browser-ambiguous-run",
        "effect-click-publish",
        {
          expected_attempt_id: "browser-ambiguous-run:attempt-1",
          expected_revision: 6,
          chat_id: null,
          session_id: "browser-session",
          workspace_dir: "/tmp/topic-alpha",
          expected_effect_revision: 2,
          resolution: "human_resolved",
          reason: "用户确认该副作用已人工处理",
        },
      );
    });
  });

  it("refreshes the runtime drawer when an effect fence is stale", async () => {
    vi.mocked(runtimeApi.listRuns)
      .mockResolvedValueOnce({
        total: 1,
        items: [
          {
            id: "browser-effect-stale-run",
            display_id: "browser-",
            source: "browser",
            attempt_id: "browser-effect-stale-run:attempt-1",
            title: "Browser Runtime Stale Effect",
            summary: "副作用等待处理",
            status: "running",
            authority_status: "running",
            transport_status: "unknown",
            revision: 6,
            session_id: "browser-session",
            workspace_dir: "/tmp/topic-alpha",
            pending_approval: false,
            ambiguous_effect: true,
            recovery: {
              effect_requires_human: true,
              effects: [
                {
                  effect_id: "effect-click-publish",
                  tool_name: "browser.click",
                  status: "ambiguous",
                  revision: 2,
                },
              ],
            },
            can_interrupt: false,
            can_retry: false,
            usage: {
              input_tokens: 0,
              output_tokens: 0,
              cache_tokens: 0,
              context_tokens: 0,
              cost_points: 0,
            },
            last_event_summary: "",
            event_count: 0,
            debug_refs: { run_id: "browser-effect-stale-run" },
          },
        ],
      } as any)
      .mockResolvedValueOnce({
        total: 1,
        items: [
          {
            id: "browser-effect-stale-run",
            display_id: "browser-",
            source: "browser",
            attempt_id: "browser-effect-stale-run:attempt-1",
            title: "Browser Runtime Fresh Effect",
            summary: "副作用已由其他窗口处理",
            status: "running",
            authority_status: "running",
            transport_status: "unknown",
            revision: 7,
            session_id: "browser-session",
            workspace_dir: "/tmp/topic-alpha",
            pending_approval: false,
            ambiguous_effect: false,
            recovery: {
              effects: [
                {
                  effect_id: "effect-click-publish",
                  tool_name: "browser.click",
                  status: "human_resolved",
                  revision: 3,
                },
              ],
            },
            can_interrupt: false,
            can_retry: false,
            usage: {
              input_tokens: 0,
              output_tokens: 0,
              cache_tokens: 0,
              context_tokens: 0,
              cost_points: 0,
            },
            last_event_summary: "",
            event_count: 0,
            debug_refs: { run_id: "browser-effect-stale-run" },
          },
        ],
      } as any);
    vi.mocked(runtimeApi.resolveRunEffect).mockRejectedValueOnce(
      new Error(
        'Runtime effect changed - {"detail":{"code":"stale_runtime_effect"}}',
      ),
    );
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: /Browser Runtime Stale Effect/,
      }),
    );
    expect(await screen.findByText("待判定 · Rev 2")).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "人工已处理" }));
    await user.click(
      await screen.findByRole("button", { name: "确认并记录" }),
    );

    const drawer = await screen.findByRole("dialog");
    expect(
      await within(drawer).findByText("Browser Runtime Fresh Effect"),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("人工已处理 · Rev 3")).toBeInTheDocument();
    expect(
      within(drawer).queryByRole("button", { name: "人工已处理" }),
    ).not.toBeInTheDocument();
  });

  it("sends run and effect fences when repairing a committed runtime effect", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "browser-repair-run",
          display_id: "browser-",
          source: "browser",
          attempt_id: "browser-repair-run:attempt-1",
          title: "Browser Runtime",
          summary: "浏览器动作已经提交",
          status: "running",
          authority_status: "running",
          transport_status: "unknown",
          revision: 6,
          session_id: "browser-session",
          workspace_dir: "/tmp/topic-alpha",
          pending_approval: false,
          ambiguous_effect: false,
          recovery: {
            effect_checkpoint_rebuild_required: true,
            effects: [
              {
                effect_id: "effect-click-save",
                tool_name: "browser.click",
                status: "committed",
                revision: 2,
                checkpoint_rebuild_required: true,
              },
            ],
          },
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          debug_refs: { run_id: "browser-repair-run", latest_seq: 18 },
        },
      ],
    } as any);
    vi.mocked(runtimeApi.repairRunEffect).mockResolvedValueOnce({
      repaired: true,
      run_id: "browser-repair-run",
      effect_id: "effect-click-save",
      action: "rebuild_checkpoint",
      effect_revision: 2,
      effect_status: "committed",
      revision: 7,
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: /Browser Runtime/,
      }),
    );
    await user.click(
      await screen.findByRole("button", { name: "修复 checkpoint" }),
    );

    await waitFor(() => {
      expect(runtimeApi.repairRunEffect).toHaveBeenCalledWith(
        "browser-repair-run",
        "effect-click-save",
        {
          expected_attempt_id: "browser-repair-run:attempt-1",
          expected_revision: 6,
          chat_id: null,
          session_id: "browser-session",
          workspace_dir: "/tmp/topic-alpha",
          expected_effect_revision: 2,
          action: "rebuild_checkpoint",
          reason: "用户确认外部副作用已提交，补写本地 checkpoint",
          checkpoint_id: "effect-repair:effect-click-save",
          checkpoint_seq: 18,
          checkpoint_summary: "副作用已提交，checkpoint 已人工修复",
        },
      );
    });
  });

  it("sends expected attempt and revision when pausing a browser runtime run", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "browser-run",
          display_id: "browser-",
          source: "browser",
          attempt_id: "browser-run:attempt-1",
          title: "Browser Runtime",
          summary: "正在执行浏览器动作",
          status: "running",
          authority_status: "running",
          transport_status: "running",
          revision: 3,
          chat_id: null,
          session_id: "browser-session",
          workspace_dir: "/tmp/topic-alpha",
          recovery: { can_pause: true },
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          debug_refs: {},
        },
      ],
    } as any);
    vi.mocked(runtimeApi.pauseRun).mockResolvedValueOnce({
      paused: true,
      run_id: "browser-run",
      status: "paused",
      browser_status: "paused",
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await within(runtimeSection as HTMLElement).findByText("Browser Runtime");
    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: "暂停",
      }),
    );

    await waitFor(() => {
      expect(runtimeApi.pauseRun).toHaveBeenCalledWith("browser-run", {
        expected_attempt_id: "browser-run:attempt-1",
        expected_revision: 3,
        chat_id: null,
        session_id: "browser-session",
        workspace_dir: "/tmp/topic-alpha",
      });
    });
  });

  it("sends expected attempt and revision when resuming a browser runtime run", async () => {
    vi.mocked(runtimeApi.listRuns).mockResolvedValueOnce({
      total: 1,
      items: [
        {
          id: "browser-paused-run",
          display_id: "browser-",
          source: "browser",
          attempt_id: "browser-paused-run:attempt-1",
          title: "Paused Browser Runtime",
          summary: "等待用户继续",
          status: "stalled",
          authority_status: "paused",
          transport_status: "running",
          revision: 4,
          chat_id: null,
          session_id: "browser-session",
          workspace_dir: "/tmp/topic-alpha",
          recovery: { can_resume: true },
          can_interrupt: false,
          can_retry: false,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_tokens: 0,
            context_tokens: 0,
            cost_points: 0,
          },
          last_event_summary: "",
          event_count: 0,
          debug_refs: {},
        },
      ],
    } as any);
    vi.mocked(runtimeApi.resumeRun).mockResolvedValueOnce({
      resumed: true,
      run_id: "browser-paused-run",
      status: "running",
      browser_status: "queued",
    } as any);
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await within(runtimeSection as HTMLElement).findByText(
      "Paused Browser Runtime",
    );
    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: "恢复",
      }),
    );

    await waitFor(() => {
      expect(runtimeApi.resumeRun).toHaveBeenCalledWith("browser-paused-run", {
        expected_attempt_id: "browser-paused-run:attempt-1",
        expected_revision: 4,
        chat_id: null,
        session_id: "browser-session",
        workspace_dir: "/tmp/topic-alpha",
      });
    });
  });

  it("refreshes runtime runs instead of retrying when control is stale", async () => {
    vi.mocked(runtimeApi.listRuns)
      .mockResolvedValueOnce({
        total: 1,
        items: [
          {
            id: "stale-control-run",
            display_id: "stale-co",
            source: "chat",
            attempt_id: "stale-control-run:attempt-1",
            title: "Stale Runtime Row",
            summary: "旧状态",
            status: "running",
            authority_status: "running",
            transport_status: "running",
            revision: 1,
            chat_id: "chat-stale",
            session_id: "console:stale",
            workspace_dir: "/tmp/topic-alpha",
            can_interrupt: true,
            can_retry: false,
            usage: {
              input_tokens: 0,
              output_tokens: 0,
              cache_tokens: 0,
              context_tokens: 0,
              cost_points: 0,
            },
            last_event_summary: "",
            event_count: 0,
            debug_refs: {},
          },
        ],
      } as any)
      .mockResolvedValueOnce({
        total: 1,
        items: [
          {
            id: "fresh-control-run",
            display_id: "fresh-co",
            source: "chat",
            attempt_id: "fresh-control-run:attempt-2",
            title: "Fresh Runtime Row",
            summary: "新状态",
            status: "running",
            authority_status: "running",
            transport_status: "running",
            revision: 2,
            chat_id: "chat-stale",
            session_id: "console:stale",
            workspace_dir: "/tmp/topic-alpha",
            can_interrupt: true,
            can_retry: false,
            usage: {
              input_tokens: 0,
              output_tokens: 0,
              cache_tokens: 0,
              context_tokens: 0,
              cost_points: 0,
            },
            last_event_summary: "",
            event_count: 0,
            debug_refs: {},
          },
        ],
      } as any);
    vi.mocked(runtimeApi.interruptRun).mockRejectedValueOnce(
      new Error(
        'Control request does not match - {"detail":{"code":"stale_attempt"}}',
      ),
    );
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await within(runtimeSection as HTMLElement).findByText("Stale Runtime Row");
    await user.click(
      within(runtimeSection as HTMLElement).getByRole("button", {
        name: "中断",
      }),
    );

    expect(
      await within(runtimeSection as HTMLElement).findByText(
        "Fresh Runtime Row",
      ),
    ).toBeInTheDocument();
    expect(
      within(runtimeSection as HTMLElement).queryByText("Stale Runtime Row"),
    ).not.toBeInTheDocument();
  });

  it("refreshes an open runtime drawer after a stale control response", async () => {
    vi.mocked(runtimeApi.listRuns)
      .mockResolvedValueOnce({
        total: 1,
        items: [
          {
            id: "drawer-stale-run",
            display_id: "drawer-s",
            source: "chat",
            attempt_id: "drawer-stale-run:attempt-1",
            title: "Drawer Stale Runtime",
            summary: "旧详情",
            status: "running",
            authority_status: "running",
            transport_status: "running",
            revision: 1,
            chat_id: "chat-drawer-stale",
            session_id: "console:drawer-stale",
            workspace_dir: "/tmp/topic-alpha",
            can_interrupt: true,
            can_retry: false,
            usage: {
              input_tokens: 0,
              output_tokens: 0,
              cache_tokens: 0,
              context_tokens: 0,
              cost_points: 0,
            },
            last_event_summary: "",
            event_count: 0,
            debug_refs: {},
          },
        ],
      } as any)
      .mockResolvedValueOnce({
        total: 1,
        items: [
          {
            id: "drawer-stale-run",
            display_id: "drawer-s",
            source: "chat",
            attempt_id: "drawer-stale-run:attempt-2",
            title: "Drawer Fresh Runtime",
            summary: "新详情",
            status: "completed",
            authority_status: "succeeded",
            transport_status: "unknown",
            revision: 2,
            chat_id: "chat-drawer-stale",
            session_id: "console:drawer-stale",
            workspace_dir: "/tmp/topic-alpha",
            can_interrupt: false,
            can_retry: false,
            usage: {
              input_tokens: 0,
              output_tokens: 0,
              cache_tokens: 0,
              context_tokens: 0,
              cost_points: 0,
            },
            last_event_summary: "",
            event_count: 0,
            debug_refs: {},
          },
        ],
      } as any);
    vi.mocked(runtimeApi.interruptRun).mockRejectedValueOnce(
      new Error(
        'Control request does not match - {"detail":{"code":"stale_attempt"}}',
      ),
    );
    const user = userEvent.setup();

    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    const runtimeSection = (
      await screen.findByRole("heading", { name: "运行控制台" })
    ).closest("section");
    expect(runtimeSection).not.toBeNull();
    await user.click(
      await within(runtimeSection as HTMLElement).findByRole("button", {
        name: /Drawer Stale Runtime/,
      }),
    );
    expect(await screen.findByText("Rev 1")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "中断运行" }));

    const drawer = await screen.findByRole("dialog");
    expect(
      within(drawer).getByText("Drawer Fresh Runtime"),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("Rev 2")).toBeInTheDocument();
    expect(
      within(drawer).queryByRole("button", { name: "中断运行" }),
    ).not.toBeInTheDocument();
  });

  it("keeps two renderer instances fenced when one has a stale browser control snapshot", async () => {
    const staleRun = {
      id: "multi-window-browser-run",
      display_id: "multi-wi",
      source: "browser",
      attempt_id: "multi-window-browser-run:attempt-1",
      title: "Shared Browser Runtime",
      summary: "两个窗口都还看到旧状态",
      status: "running",
      authority_status: "running",
      transport_status: "running",
      revision: 1,
      chat_id: null,
      session_id: "browser-session",
      workspace_dir: "/tmp/topic-alpha",
      recovery: { can_pause: true },
      can_interrupt: false,
      can_retry: false,
      usage: {
        input_tokens: 0,
        output_tokens: 0,
        cache_tokens: 0,
        context_tokens: 0,
        cost_points: 0,
      },
      last_event_summary: "",
      event_count: 0,
      debug_refs: {},
    };
    const freshRun = {
      ...staleRun,
      title: "Fresh Browser Runtime",
      summary: "另一个窗口已经暂停",
      status: "stalled",
      authority_status: "paused",
      revision: 2,
      recovery: { can_resume: true },
    };
    vi.mocked(runtimeApi.listRuns)
      .mockResolvedValueOnce({ total: 1, items: [staleRun] } as any)
      .mockResolvedValueOnce({ total: 1, items: [staleRun] } as any)
      .mockResolvedValueOnce({ total: 1, items: [freshRun] } as any)
      .mockResolvedValueOnce({ total: 1, items: [freshRun] } as any);
    vi.mocked(runtimeApi.pauseRun)
      .mockResolvedValueOnce({
        paused: true,
        run_id: "multi-window-browser-run",
        status: "paused",
        revision: 2,
      } as any)
      .mockRejectedValueOnce(
        new Error(
          'Control request does not match - {"detail":{"code":"stale_attempt"}}',
        ),
      )
      .mockRejectedValueOnce(
        new Error(
          'Control request does not match - {"detail":{"code":"stale_attempt"}}',
        ),
      );
    const firstUser = userEvent.setup();
    const secondUser = userEvent.setup();

    const firstWindow = renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });
    const secondWindow = renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    await within(firstWindow.container).findByText("Shared Browser Runtime");
    await within(secondWindow.container).findByText("Shared Browser Runtime");

    await firstUser.click(
      within(firstWindow.container).getByRole("button", { name: "暂停" }),
    );
    await waitFor(() => {
      expect(runtimeApi.pauseRun).toHaveBeenCalledWith(
        "multi-window-browser-run",
        {
          expected_attempt_id: "multi-window-browser-run:attempt-1",
          expected_revision: 1,
          chat_id: null,
          session_id: "browser-session",
          workspace_dir: "/tmp/topic-alpha",
        },
      );
    });

    await secondUser.click(
      within(secondWindow.container).getByRole("button", { name: "暂停" }),
    );

    expect(
      await within(secondWindow.container).findByText("Fresh Browser Runtime"),
    ).toBeInTheDocument();
    expect(runtimeApi.pauseRun).toHaveBeenCalledWith(
      "multi-window-browser-run",
      {
        expected_attempt_id: "multi-window-browser-run:attempt-1",
        expected_revision: 1,
        chat_id: null,
        session_id: "browser-session",
        workspace_dir: "/tmp/topic-alpha",
      },
    );
    expect(
      within(secondWindow.container).queryByText("Shared Browser Runtime"),
    ).not.toBeInTheDocument();
  });

  it("renders sub-agent hierarchy metadata in runtime details", async () => {
    vi.mocked(runtimeApi.getRunEvents).mockResolvedValueOnce({
      run_id: "run-1",
      workspace_dir: "/tmp/topic-alpha",
      after_seq: 0,
      latest_seq: 2,
      events: [
        {
          id: "evt-parent",
          seq: 1,
          type: "turn.started",
          status: "running",
          source: "user",
          payload: {
            legacy_kind: "chat.turn.started",
            text_preview: "并行检查实现",
          },
          usage: null,
          created_at: "2026-05-22T08:00:00Z",
        },
        {
          id: "evt-child",
          seq: 2,
          type: "tool.completed",
          status: "completed",
          source: "tool",
          presentation: {
            visibility: "prominent",
            surface: "subagent",
            risk_level: "low",
            display_name: "chat_with_agent",
            summary: "子任务完成",
          },
          payload: {
            legacy_kind: "chat.tool_call.finished",
            phase: "subtask",
            parent_run_id: "run-1",
            agent_name: "白水",
            agent_role: "代码与自动化",
            delegation_reason: "并行检查实现",
            permission_profile: "workspace",
            current_tool: "chat_with_agent",
            result_summary: "已完成实现检查",
            sub_run: {
              parent_run_id: "run-1",
              agent_name: "白水",
              status: "completed",
            },
          },
          usage: null,
          created_at: "2026-05-22T08:02:00Z",
        },
      ],
    } as any);

    const user = userEvent.setup();
    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    await waitFor(() => {
      expect(screen.getByText("运行控制台")).toBeInTheDocument();
    });

    await user.click(screen.getAllByRole("button", { name: /日报整理/ })[0]);

    expect(await screen.findByText("子任务：白水")).toBeInTheDocument();
    expect(screen.getAllByText(/代码与自动化/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/并行检查实现/).length).toBeGreaterThan(0);
    expect(screen.getByText(/来源 白水/)).toBeInTheDocument();
    expect(screen.getByText(/父运行 run-1/)).toBeInTheDocument();
  });

  it("renders runtime failure diagnosis in run details", async () => {
    window.sessionStorage.clear();
    vi.mocked(runtimeApi.getRunEvents).mockResolvedValueOnce({
      run_id: "run-1",
      workspace_dir: "/tmp/topic-alpha",
      after_seq: 0,
      latest_seq: 2,
      events: [
        {
          id: "evt-start",
          seq: 1,
          type: "turn.started",
          status: "running",
          source: "user",
          payload: {
            legacy_kind: "chat.turn.started",
            text_preview: "修复前端预览",
          },
          usage: null,
          created_at: "2026-05-22T08:00:00Z",
        },
        {
          id: "evt-preview-fail",
          seq: 2,
          type: "tool.failed",
          status: "failed",
          source: "tool",
          payload: {
            legacy_kind: "chat.tool_call.finished",
            phase: "preview",
            trace_title: "预览错误：Vite",
            trace_detail: "net::ERR_CONNECTION_REFUSED localhost:5173",
            failure_diagnosis: {
              label: "#2 预览错误：Vite",
              category: "preview",
              summary: "前端预览或浏览器验证失败",
              next_action:
                "先确认 dev server 端口和页面控制台首个错误，再修复导致页面不可访问或空白的根因。",
              evidence: "net::ERR_CONNECTION_REFUSED localhost:5173",
            },
          },
          usage: null,
          created_at: "2026-05-22T08:02:00Z",
        },
      ],
    } as any);

    const user = userEvent.setup();
    renderWithProviders(<TaskCenterPage />, {
      initialEntries: ["/tasks"],
    });

    await waitFor(() => {
      expect(screen.getByText("运行控制台")).toBeInTheDocument();
    });

    await user.click(screen.getAllByRole("button", { name: /日报整理/ })[0]);

    expect(await screen.findAllByText("前端预览或浏览器验证失败")).toHaveLength(
      2,
    );
    expect(
      screen.getAllByText(/先确认 dev server 端口/).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/net::ERR_CONNECTION_REFUSED/).length,
    ).toBeGreaterThan(0);

    await user.click(
      screen.getAllByRole("button", { name: "生成修复提示" })[0],
    );

    expect(
      window.sessionStorage.getItem("lantai:retry-draft:chat-run-1"),
    ).toContain("前端预览失败了");
    expect(
      window.sessionStorage.getItem("lantai:retry-draft:chat-run-1"),
    ).toContain("run_id: run-1");
  });
});
