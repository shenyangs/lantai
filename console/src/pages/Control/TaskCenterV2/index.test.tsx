import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { runtimeApi } from "../../../api/modules/runtime";
import type { RuntimeTaskBoardItem } from "../../../api/types/runtime";
import { renderWithProviders } from "../../../test/common_setup";
import TaskCenterV2Page from "./index";

vi.mock("../../../api/modules/runtime", () => ({
  runtimeApi: {
    getTaskBoard: vi.fn(),
    createTask: vi.fn(),
    startTask: vi.fn(),
    interruptRun: vi.fn(),
    continueRun: vi.fn(),
    grantRunAutonomy: vi.fn(),
    revokeRunAutonomy: vi.fn(),
    getRunEvents: vi.fn(async () => ({ events: [] })),
  },
}));

function task(
  id: string,
  lane: RuntimeTaskBoardItem["lane"],
): RuntimeTaskBoardItem {
  return {
    id,
    display_id: id.slice(0, 8),
    title: id === "task-pending" ? "发布检查" : "确认外部发布结果",
    summary: "任务摘要",
    source: "chat",
    lane,
    outcome_status: lane === "needs_user" ? "needs_input" : "queued",
    authority_status: lane === "pending" ? "queued" : "waiting_approval",
    transport_status: "unknown",
    delivery_status: "pending",
    is_history: false,
    pending_approval: lane === "needs_user",
    ambiguous_effect: false,
    next_action: lane === "needs_user" ? "确认是否继续" : null,
    agent_id: "default",
    chat_id: `chat-${id}`,
    session_id: `console:${id}`,
    workspace_dir: "/tmp/topic",
    attempt_id: `${id}:attempt-1`,
    revision: 0,
    budget: {},
    usage: {
      input_tokens: 0,
      output_tokens: 0,
      cache_tokens: 0,
      context_tokens: 0,
      cost_points: 0,
      tool_calls: 0,
      artifact_bytes: 0,
    },
    recovery: {},
    can_start: lane === "pending",
    can_interrupt: lane === "needs_user",
    can_retry: false,
    can_resume: false,
    can_open_chat: true,
    event_count: 0,
    last_event_summary: "",
    debug_refs: {},
  };
}

describe("TaskCenterV2Page", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.clearAllMocks();
    vi.mocked(runtimeApi.getTaskBoard).mockResolvedValue({
      total: 2,
      visible_total: 2,
      generated_at: "2026-08-13T00:00:00Z",
      counts: {
        pending: 1,
        active: 0,
        needs_user: 1,
        done: 0,
        needs_action: 0,
        history: 0,
      },
      items: [
        task("task-pending", "pending"),
        task("task-input", "needs_user"),
      ],
    });
    vi.mocked(runtimeApi.startTask).mockResolvedValue({
      run_id: "task-pending",
      chat_id: "chat-task-pending",
      session_id: "console:task-pending",
      status: "queued",
      already_running: false,
      accepted: true,
    });
    vi.mocked(runtimeApi.grantRunAutonomy).mockResolvedValue({
      changed: true,
      run_id: "task-pending",
      autonomy_grant: null,
    });
    vi.mocked(runtimeApi.revokeRunAutonomy).mockResolvedValue({
      changed: true,
      run_id: "task-pending",
      autonomy_grant: null,
    });
  });

  it("renders the five-lane board and starts a queued task", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TaskCenterV2Page />, {
      initialEntries: ["/task-center"],
    });

    expect(await screen.findByRole("heading", { name: "任务" })).toBeVisible();
    expect(await screen.findByTestId("task-board")).toBeVisible();
    expect(screen.getByText("个任务在等你决定")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "开始" }));

    await waitFor(() => {
      expect(runtimeApi.startTask).toHaveBeenCalledWith("task-pending");
    });
  });

  it("opens a run deep link with an ordinary summary and keeps technical controls reachable", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TaskCenterV2Page />, {
      initialEntries: ["/task-center?run=task-pending"],
    });

    expect(
      await screen.findByText("任务已经保存，尚未开始执行。"),
    ).toBeVisible();
    expect(screen.getAllByText("结果待确认").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "高级控制" })).toBeVisible();
    await user.click(screen.getByText("技术信息"));
    expect(screen.getByText("Run ID")).toBeVisible();
    expect(screen.getByText("task-pending")).toBeVisible();
    expect(runtimeApi.getTaskBoard).toHaveBeenCalled();
  });

  it("shows a persisted deadline report as factual non-terminal progress", async () => {
    const paused = {
      ...task("task-deadline", "needs_action"),
      authority_status: "paused",
      can_resume: true,
      progress_report: {
        report_id: "report-deadline-1",
        kind: "deadline_snapshot" as const,
        generated_at: "2026-09-20T08:00:00Z",
        run_id: "task-deadline",
        attempt_id: "task-deadline:attempt-1",
        contract_revision: 5,
        status: "paused",
        terminal: false,
        objective: "核对发布证据",
        completed_milestones: [
          {
            milestone_id: "milestone-1",
            statement: "已生成候选产物",
            evidence_refs: ["artifact://candidate"],
          },
        ],
        acceptance_results: [],
        open_acceptance_items: ["确认候选产物可打开"],
        artifact_refs: ["artifact://candidate"],
        blocked_reasons: ["wait_reason:deadline"],
        user_decisions: ["extend_deadline_and_resume_or_stop"],
        effect_summary: {
          status_counts: { committed: 1 },
          unresolved_effect_ids: [],
          ambiguous_effect_ids: ["effect-delivery-1"],
        },
        cost_summary: {
          currency: "CNY" as const,
          limit_micro_cny: 10_000_000,
          reserved_micro_cny: 1_000_000,
          committed_micro_cny: 2_000_000,
          settled_count: 2,
          pending_count: 1,
          ambiguous_count: 0,
          overrun_count: 0,
        },
        active_seconds: 180,
        elapsed_seconds: 300,
        wait_seconds: null,
        unknown_facts: ["wait_seconds_not_recorded"],
        wait_reason: "deadline",
        narrative_status: "failed" as const,
        narrative_error: "provider unavailable",
      },
    };
    vi.mocked(runtimeApi.getTaskBoard).mockResolvedValue({
      total: 1,
      visible_total: 1,
      generated_at: "2026-09-20T08:00:01Z",
      counts: {
        pending: 0,
        active: 0,
        needs_user: 0,
        done: 0,
        needs_action: 1,
        history: 0,
      },
      items: [paused],
    });

    renderWithProviders(<TaskCenterV2Page />, {
      initialEntries: ["/task-center?run=task-deadline"],
    });

    expect(await screen.findByText("截至截止时间")).toBeVisible();
    expect(screen.getByText("仍可继续")).toBeVisible();
    expect(screen.getByText("已生成候选产物")).toBeVisible();
    expect(screen.getByText("确认候选产物可打开")).toBeVisible();
    expect(screen.getAllByText("artifact://candidate").length).toBeGreaterThan(
      0,
    );
    expect(screen.getByText(/已结算 ¥2\.000000/)).toBeVisible();
    expect(
      screen.getByText("文字说明生成失败，结构化事实仍然有效"),
    ).toBeVisible();
    expect(screen.getByText("wait_seconds_not_recorded")).toBeVisible();
  });

  it("grants only the explicit task, path, model, price, budget, and expiry scope", async () => {
    const user = userEvent.setup();
    const fillField = async (label: string, value: string) => {
      await user.click(screen.getByLabelText(label));
      await user.paste(value);
    };
    renderWithProviders(<TaskCenterV2Page />, {
      initialEntries: ["/task-center?run=task-pending"],
    });

    await user.click(
      await screen.findByRole("button", { name: "授权后台续跑" }),
    );
    await fillField("Provider ID", "provider-a");
    await fillField("Model ID", "model-a");
    await fillField("价格版本", "2026-09-20");
    await fillField("任务总上限（人民币元）", "10");
    await fillField("单次调用上限（人民币元）", "1");
    await user.click(
      screen.getByRole("checkbox", {
        name: /我已按上述价格版本核验/,
      }),
    );
    await user.click(screen.getByRole("button", { name: "确认授权" }));

    await waitFor(() => {
      expect(runtimeApi.grantRunAutonomy).toHaveBeenCalledWith(
        "task-pending",
        expect.objectContaining({
          approved: true,
          expected_attempt_id: "task-pending:attempt-1",
          expected_revision: 0,
          chat_id: "chat-task-pending",
          session_id: "console:task-pending",
          workspace_dir: "/tmp/topic",
          expires_at: expect.any(String),
          allowed_operations: [
            "automatic_resume",
            "model_call",
            "local_read",
            "local_write_new",
            "local_modify_existing",
          ],
          allowed_paths: ["/tmp/topic"],
          allowed_provider_ids: ["provider-a"],
          allowed_model_ids: ["model-a"],
          allow_network: false,
          allow_gui: false,
          allow_external_delivery: false,
          auto_resume_conditions: [
            "backend_restart",
            "app_reopen",
            "network_recovery",
          ],
          budget_currency: "CNY",
          max_cost_micro_cny: 10_000_000,
          model_price_bounds: [
            {
              provider_id: "provider-a",
              model_id: "model-a",
              pricing_version: "2026-09-20",
              max_dispatch_micro_cny: 1_000_000,
              upper_bound_proven: true,
            },
          ],
          reason: "用户在任务详情中明确授权后台续跑",
        }),
      );
    });
  });

  it("keeps an active grant visible and revocable from the same task", async () => {
    const active = {
      ...task("task-pending", "pending"),
      autonomy_grant: {
        grant_id: "grant-1",
        approved: true as const,
        issued_by: "user" as const,
        run_id: "task-pending",
        chat_id: "chat-task-pending",
        workspace_id: "/tmp/topic",
        target_task_revision: 0,
        issued_at: "2026-09-20T00:00:00Z",
        starts_at: "2026-09-20T00:00:00Z",
        expires_at: "2099-09-20T08:00:00Z",
        allowed_operations: ["automatic_resume" as const],
        allowed_paths: [],
        allowed_provider_ids: [],
        allowed_model_ids: [],
        allow_network: false,
        allow_gui: false,
        allow_external_delivery: false,
        auto_resume_conditions: ["app_reopen" as const],
        required_capabilities: [],
        preflight_checks: [
          {
            check_id: "workspace-access",
            capability: "workspace_access" as const,
            status: "passed" as const,
            checked_at: "2026-09-20T00:00:00Z",
            message: "工作区与授权路径已验证可用",
            evidence: { approved_path_count: 1 },
          },
        ],
        budget_currency: null,
        max_cost_micro_cny: null,
        model_price_bounds: [],
        revision: 1,
        revocation_generation: 0,
        revoked_at: null,
        revocation_reason: null,
      },
    };
    vi.mocked(runtimeApi.getTaskBoard).mockResolvedValue({
      total: 1,
      visible_total: 1,
      generated_at: "2026-09-20T00:00:00Z",
      counts: {
        pending: 1,
        active: 0,
        needs_user: 0,
        done: 0,
        needs_action: 0,
        history: 0,
      },
      items: [active],
    });
    const user = userEvent.setup();
    renderWithProviders(<TaskCenterV2Page />, {
      initialEntries: ["/task-center?run=task-pending"],
    });

    expect(await screen.findByText("工作区与授权路径已验证可用")).toBeVisible();
    await user.click(await screen.findByRole("button", { name: "撤销授权" }));
    const revokeButtons = await screen.findAllByRole("button", {
      name: "撤销授权",
    });
    await user.click(revokeButtons[revokeButtons.length - 1]);

    await waitFor(() => {
      expect(runtimeApi.revokeRunAutonomy).toHaveBeenCalledWith(
        "task-pending",
        {
          expected_attempt_id: "task-pending:attempt-1",
          expected_revision: 0,
          chat_id: "chat-task-pending",
          session_id: "console:task-pending",
          workspace_dir: "/tmp/topic",
          reason: "用户在任务详情中明确撤销后台续跑授权",
        },
      );
    });
  });

  it("shows a stop request as waiting and does not offer a second stop", async () => {
    const stopping = {
      ...task("task-stopping", "needs_action"),
      authority_status: "stop_requested",
      can_interrupt: true,
      next_action: "等待停止完成；若长时间未结束请接管",
    };
    vi.mocked(runtimeApi.getTaskBoard).mockResolvedValue({
      total: 1,
      visible_total: 1,
      generated_at: "2026-08-13T00:00:00Z",
      counts: {
        pending: 0,
        active: 0,
        needs_user: 0,
        done: 0,
        needs_action: 1,
        history: 0,
      },
      items: [stopping],
    });

    renderWithProviders(<TaskCenterV2Page />, {
      initialEntries: ["/task-center?run=task-stopping"],
    });

    expect(
      await screen.findByText("停止请求已经提交，正在等待运行安全结束。"),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "停止" })).toBeNull();
  });

  it("separates an event loading error from an empty timeline and can retry", async () => {
    const user = userEvent.setup();
    vi.mocked(runtimeApi.getRunEvents)
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({ events: [] });

    renderWithProviders(<TaskCenterV2Page />, {
      initialEntries: ["/task-center?run=task-pending"],
    });

    expect(await screen.findByText("运行记录暂时无法读取")).toBeVisible();
    expect(screen.queryByText("任务尚未产生运行事件")).toBeNull();
    await user.click(screen.getByRole("button", { name: "重试读取运行记录" }));
    await waitFor(() => {
      expect(runtimeApi.getRunEvents).toHaveBeenCalledTimes(2);
      expect(screen.getByText("任务尚未产生运行事件")).toBeVisible();
    });
  });

  it("loads every event page so the newest technical evidence is reachable", async () => {
    const firstPage = Array.from({ length: 160 }, (_, index) => ({
      id: `event-${index + 1}`,
      seq: index + 1,
      created_at: "2026-09-07T01:00:00Z",
      type: "message.completed",
      status: "completed",
      source: "assistant",
      session_id: "console:task-pending",
      turn_id: "task-pending",
      payload: { trace_title: `事件 ${index + 1}` },
    }));
    const latestEvent = {
      ...firstPage[0],
      id: "event-161",
      seq: 161,
      payload: { trace_title: "最新停止结果" },
    };
    vi.mocked(runtimeApi.getRunEvents)
      .mockResolvedValueOnce({
        run_id: "task-pending",
        after_seq: 0,
        latest_seq: 161,
        next_after_seq: 160,
        has_more: true,
        events: firstPage,
      })
      .mockResolvedValueOnce({
        run_id: "task-pending",
        after_seq: 160,
        latest_seq: 161,
        next_after_seq: 161,
        has_more: false,
        events: [firstPage[159], latestEvent],
      });

    renderWithProviders(<TaskCenterV2Page />, {
      initialEntries: ["/task-center?run=task-pending"],
    });

    expect(await screen.findByText("最新停止结果")).toBeVisible();
    expect(screen.getByText("161 条事件")).toBeVisible();
    expect(runtimeApi.getRunEvents).toHaveBeenNthCalledWith(
      1,
      "task-pending",
      0,
      { limit: 160, attemptId: "task-pending:attempt-1" },
    );
    expect(runtimeApi.getRunEvents).toHaveBeenNthCalledWith(
      2,
      "task-pending",
      160,
      { limit: 160, attemptId: "task-pending:attempt-1" },
    );
  });

  it("reports an invalid event cursor instead of pretending the record is complete", async () => {
    vi.mocked(runtimeApi.getRunEvents).mockResolvedValue({
      run_id: "task-pending",
      after_seq: 0,
      latest_seq: 161,
      next_after_seq: 0,
      has_more: true,
      events: [],
    });

    renderWithProviders(<TaskCenterV2Page />, {
      initialEntries: ["/task-center?run=task-pending"],
    });

    expect(
      await screen.findByText("运行记录分页位置没有前进，无法确认记录完整性。"),
    ).toBeVisible();
    expect(screen.queryByText("任务尚未产生运行事件")).toBeNull();
  });
});
