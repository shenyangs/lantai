import { describe, expect, it, vi } from "vitest";
import { runtimeApi } from "./runtime";

vi.mock("../request", () => ({ request: vi.fn() }));

import { request } from "../request";

describe("runtimeApi", () => {
  it("gets the task board with stable filters", async () => {
    vi.mocked(request).mockResolvedValue({
      total: 0,
      visible_total: 0,
      generated_at: "2026-08-13T00:00:00Z",
      counts: {},
      items: [],
    });

    await runtimeApi.getTaskBoard({
      view: "board",
      source: "chat",
      lane: "needs_user",
      query: "release check",
      limit: 80,
    });

    expect(request).toHaveBeenCalledWith(
      "/runtime/task-board?view=board&source=chat&lane=needs_user&query=release+check&limit=80",
    );
  });

  it("creates and starts persistent runtime tasks", async () => {
    vi.mocked(request).mockResolvedValue({
      run_id: "task-1",
      chat_id: "task-chat-1",
      session_id: "console:task:1",
      accepted: true,
    });

    await runtimeApi.createTask({
      objective: "整理发布检查清单",
      client_task_id: "client-task-1",
      auto_start: false,
    });
    await runtimeApi.startTask("task/1");

    expect(request).toHaveBeenCalledWith("/runtime/runs", {
      method: "POST",
      body: JSON.stringify({
        objective: "整理发布检查清单",
        client_task_id: "client-task-1",
        auto_start: false,
      }),
    });
    expect(request).toHaveBeenCalledWith("/runtime/runs/task%2F1/start", {
      method: "POST",
      body: JSON.stringify({}),
    });
  });

  it("gets paginated run events with optional attempt filtering", async () => {
    vi.mocked(request).mockResolvedValue({
      run_id: "run/1",
      attempt_id: "attempt/1",
      after_seq: 8,
      latest_seq: 10,
      limit: 2,
      next_after_seq: 10,
      has_more: true,
      events: [],
    });

    await runtimeApi.getRunEvents("run/1", 8, {
      limit: 2,
      attemptId: "attempt/1",
    });

    expect(request).toHaveBeenCalledWith(
      "/runtime/runs/run%2F1/events?after_seq=8&limit=2&attempt_id=attempt%2F1",
    );
  });

  it("binds child run discovery and replay to the selected child agent", async () => {
    vi.mocked(request).mockResolvedValue({ total: 0, items: [] });

    await runtimeApi.listRuns({
      source: "chat",
      sessionId: "host:to:water:attempt-1",
      agentId: "Lantai_Water_Agent",
      limit: 20,
    });
    await runtimeApi.getRunEvents("child/run", 0, {
      agentId: "Lantai_Water_Agent",
      limit: 400,
    });

    expect(request).toHaveBeenCalledWith(
      "/runtime/runs?source=chat&session_id=host%3Ato%3Awater%3Aattempt-1&limit=20",
      { headers: { "X-Agent-Id": "Lantai_Water_Agent" } },
    );
    expect(request).toHaveBeenCalledWith(
      "/runtime/runs/child%2Frun/events?after_seq=0&limit=400",
      { headers: { "X-Agent-Id": "Lantai_Water_Agent" } },
    );
  });

  it("cancels the selected child task through its agent-scoped endpoint", async () => {
    vi.mocked(request).mockResolvedValue({
      task_id: "child/task",
      status: "cancelled",
    });

    await runtimeApi.cancelAgentTask("child/task", "Lantai_Water_Agent");

    expect(request).toHaveBeenCalledWith(
      "/agent/process/task/child%2Ftask/cancel",
      {
        method: "POST",
        body: JSON.stringify({}),
        headers: { "X-Agent-Id": "Lantai_Water_Agent" },
      },
    );
  });

  it("posts continuation context to the runtime continue endpoint", async () => {
    vi.mocked(request).mockResolvedValue({
      run_id: "run-1",
      reconnected: false,
      text: "continue",
    });

    await runtimeApi.continueRun("run/1", {
      mode: "takeover",
      chat_id: "chat-1",
      session_id: "console:chat-1",
      after_seq: 8,
      stage_label: "构建项目",
      workspace_dir: "/tmp/work",
    });

    expect(request).toHaveBeenCalledWith("/runtime/runs/run%2F1/continue", {
      method: "POST",
      body: JSON.stringify({
        mode: "takeover",
        chat_id: "chat-1",
        session_id: "console:chat-1",
        after_seq: 8,
        stage_label: "构建项目",
        workspace_dir: "/tmp/work",
      }),
    });
  });

  it("posts archive fence context to the runtime archive endpoint", async () => {
    vi.mocked(request).mockResolvedValue({
      run_id: "run-1",
      archived: true,
    });

    await runtimeApi.archiveRun("run/1", {
      expected_attempt_id: "attempt-1",
      expected_revision: 6,
      workspace_dir: "/tmp/work",
    });

    expect(request).toHaveBeenCalledWith("/runtime/runs/run%2F1/archive", {
      method: "POST",
      body: JSON.stringify({
        expected_attempt_id: "attempt-1",
        expected_revision: 6,
        workspace_dir: "/tmp/work",
      }),
    });
  });

  it("posts explicit approval to the runtime export endpoint", async () => {
    vi.mocked(request).mockResolvedValue({
      run_id: "run-1",
      exported: true,
      package: {
        schema_version: 1,
        trace_events: [],
        artifact_manifest: {
          schema_version: 1,
          manifest_hash: "sha256:0",
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
    });

    await runtimeApi.exportRun("run/1", {
      approved: true,
      reason: "Task Center 导出运行审计包",
      exported_by: "task-center",
      expected_attempt_id: "attempt-1",
      expected_revision: 6,
      workspace_dir: "/tmp/work",
      version_info: { client: "task-center" },
    });

    expect(request).toHaveBeenCalledWith("/runtime/runs/run%2F1/export", {
      method: "POST",
      body: JSON.stringify({
        approved: true,
        reason: "Task Center 导出运行审计包",
        exported_by: "task-center",
        expected_attempt_id: "attempt-1",
        expected_revision: 6,
        workspace_dir: "/tmp/work",
        version_info: { client: "task-center" },
      }),
    });
  });

  it("posts fenced budget updates to the runtime budget endpoint", async () => {
    vi.mocked(request).mockResolvedValue({
      run_id: "run-1",
      changed: true,
      revision: 7,
      budget: { max_steps: 20, max_seconds: 1800 },
    });

    await runtimeApi.updateRunBudget("run/1", {
      expected_attempt_id: "attempt-1",
      expected_revision: 6,
      workspace_dir: "/tmp/work",
      budget: { max_steps: 20, max_seconds: 1800 },
      changed_by: "user",
      reason: "Task Center 手动增加运行预算",
    });

    expect(request).toHaveBeenCalledWith("/runtime/runs/run%2F1/budget", {
      method: "POST",
      body: JSON.stringify({
        expected_attempt_id: "attempt-1",
        expected_revision: 6,
        workspace_dir: "/tmp/work",
        budget: { max_steps: 20, max_seconds: 1800 },
        changed_by: "user",
        reason: "Task Center 手动增加运行预算",
      }),
    });
  });

  it("posts task-scoped autonomy grants and revocations", async () => {
    vi.mocked(request).mockResolvedValue({
      run_id: "run-1",
      changed: true,
      autonomy_grant: null,
    });

    await runtimeApi.grantRunAutonomy("run/1", {
      approved: true,
      expected_attempt_id: "attempt-1",
      expected_revision: 6,
      chat_id: "chat-1",
      workspace_dir: "/tmp/work",
      expires_at: "2026-09-21T00:00:00Z",
      allowed_operations: ["automatic_resume", "local_read"],
      allowed_paths: ["/tmp/work"],
      auto_resume_conditions: ["app_reopen"],
      reason: "User approved this bounded task scope.",
    });
    await runtimeApi.revokeRunAutonomy("run/1", {
      expected_attempt_id: "attempt-1",
      expected_revision: 7,
      chat_id: "chat-1",
      workspace_dir: "/tmp/work",
      reason: "User revoked unattended execution.",
    });

    expect(request).toHaveBeenCalledWith(
      "/runtime/runs/run%2F1/autonomy-grant",
      {
        method: "POST",
        body: JSON.stringify({
          approved: true,
          expected_attempt_id: "attempt-1",
          expected_revision: 6,
          chat_id: "chat-1",
          workspace_dir: "/tmp/work",
          expires_at: "2026-09-21T00:00:00Z",
          allowed_operations: ["automatic_resume", "local_read"],
          allowed_paths: ["/tmp/work"],
          auto_resume_conditions: ["app_reopen"],
          reason: "User approved this bounded task scope.",
        }),
      },
    );
    expect(request).toHaveBeenCalledWith(
      "/runtime/runs/run%2F1/autonomy-grant/revoke",
      {
        method: "POST",
        body: JSON.stringify({
          expected_attempt_id: "attempt-1",
          expected_revision: 7,
          chat_id: "chat-1",
          workspace_dir: "/tmp/work",
          reason: "User revoked unattended execution.",
        }),
      },
    );
  });

  it("posts effect resolution fence context to the runtime effect endpoint", async () => {
    vi.mocked(request).mockResolvedValue({
      run_id: "run-1",
      effect_id: "effect/1",
      resolved: true,
      effect_revision: 3,
      effect_status: "human_resolved",
    });

    await runtimeApi.resolveRunEffect("run/1", "effect/1", {
      expected_attempt_id: "attempt-1",
      expected_revision: 6,
      expected_effect_revision: 1,
      workspace_dir: "/tmp/work",
      resolution: "human_resolved",
      reason: "用户确认外部动作已完成",
    });

    expect(request).toHaveBeenCalledWith(
      "/runtime/runs/run%2F1/effects/effect%2F1/resolve",
      {
        method: "POST",
        body: JSON.stringify({
          expected_attempt_id: "attempt-1",
          expected_revision: 6,
          expected_effect_revision: 1,
          workspace_dir: "/tmp/work",
          resolution: "human_resolved",
          reason: "用户确认外部动作已完成",
        }),
      },
    );
  });

  it("posts effect repair fence context to the runtime effect repair endpoint", async () => {
    vi.mocked(request).mockResolvedValue({
      run_id: "run-1",
      effect_id: "effect/1",
      repaired: true,
      action: "rebuild_checkpoint",
      effect_revision: 3,
      effect_status: "committed",
    });

    await runtimeApi.repairRunEffect("run/1", "effect/1", {
      expected_attempt_id: "attempt-1",
      expected_revision: 6,
      expected_effect_revision: 3,
      workspace_dir: "/tmp/work",
      action: "rebuild_checkpoint",
      checkpoint_id: "checkpoint-1",
      checkpoint_seq: 12,
      checkpoint_summary: "外部动作已提交",
    });

    expect(request).toHaveBeenCalledWith(
      "/runtime/runs/run%2F1/effects/effect%2F1/repair",
      {
        method: "POST",
        body: JSON.stringify({
          expected_attempt_id: "attempt-1",
          expected_revision: 6,
          expected_effect_revision: 3,
          workspace_dir: "/tmp/work",
          action: "rebuild_checkpoint",
          checkpoint_id: "checkpoint-1",
          checkpoint_seq: 12,
          checkpoint_summary: "外部动作已提交",
        }),
      },
    );
  });

  it("sends explicit approval to the runtime delete endpoint", async () => {
    vi.mocked(request).mockResolvedValue({
      run_id: "run-1",
      deleted: true,
    });

    await runtimeApi.deleteRun("run/1", {
      approved: true,
      reason: "User requested cleanup",
      delete_artifacts: true,
      expected_attempt_id: "attempt-1",
      expected_revision: 6,
      workspace_dir: "/tmp/work",
    });

    expect(request).toHaveBeenCalledWith("/runtime/runs/run%2F1", {
      method: "DELETE",
      body: JSON.stringify({
        approved: true,
        reason: "User requested cleanup",
        delete_artifacts: true,
        expected_attempt_id: "attempt-1",
        expected_revision: 6,
        workspace_dir: "/tmp/work",
      }),
    });
  });

  it("posts local preview inspection requests", async () => {
    vi.mocked(request).mockResolvedValue({
      url: "http://localhost:5173/",
      fetch_url: "http://localhost:5173/",
      ok: false,
      summary: "本地预览不可达。",
    });

    await runtimeApi.inspectPreview({
      url: "http://localhost:5173/",
      chat_id: "chat-1",
      run_id: "run-1",
      record_event: true,
    });

    expect(request).toHaveBeenCalledWith("/runtime/preview/inspect", {
      method: "POST",
      body: JSON.stringify({
        url: "http://localhost:5173/",
        chat_id: "chat-1",
        run_id: "run-1",
        record_event: true,
      }),
    });
  });
});
