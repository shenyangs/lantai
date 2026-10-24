import { describe, expect, it } from "vitest";
import {
  buildIncompleteResponseContinuationPrompt,
  buildRuntimeContinuationPrompt,
  getRuntimeStallState,
  latestRuntimePreviewEvent,
  runtimeStageLabel,
} from "./runtimeStall";
import type {
  ChatRuntimeState,
  ChatTurnEventPreview,
} from "../../api/types/chat";

const NOW = Date.parse("2026-05-29T08:02:00Z");

function runningState(
  events: ChatRuntimeState["events_preview"],
): ChatRuntimeState {
  return {
    status: "running",
    run_id: "run-1",
    updated_at: "2026-05-29T08:00:00Z",
    events_preview: events,
  };
}

describe("runtimeStall", () => {
  it("builds a result-focused prompt for an ended but incomplete turn", () => {
    const prompt = buildIncompleteResponseContinuationPrompt({
      runId: "run-incomplete",
      workspaceName: "topic-workspace",
      workspaceDir: "/tmp/topic-workspace",
    });

    expect(prompt).toContain("可能没有形成完整回答");
    expect(prompt).toContain("只是在聊天或提问");
    expect(prompt).toContain("不要制造文件或制品");
    expect(prompt).not.toContain("似乎中途停住");
  });

  it("picks the latest event by sequence and timestamp", () => {
    const latest = latestRuntimePreviewEvent(
      runningState([
        {
          id: "evt-old",
          seq: 1,
          ts: "2026-05-29T08:00:00Z",
          kind: "chat.turn.started",
          status: "running",
          label: "开始运行",
          detail: "",
        },
      ]),
      {
        events: [
          {
            id: "evt-new",
            seq: 2,
            ts: "2026-05-29T08:01:00Z",
            kind: "chat.tool_call.started",
            status: "running",
            label: "工具调用",
            detail: "npm run build",
          },
        ],
      } as ChatTurnEventPreview,
    );

    expect(latest?.id).toBe("evt-new");
  });

  it("classifies healthy, stale, and stalled running states", () => {
    expect(
      getRuntimeStallState(
        runningState([
          {
            id: "evt-healthy",
            seq: 1,
            ts: "2026-05-29T08:01:30Z",
            kind: "chat.tool_call.started",
            status: "running",
            label: "工具调用",
            detail: "npm run dev",
          },
        ]),
        null,
        NOW,
      ).level,
    ).toBe("healthy");

    expect(
      getRuntimeStallState(
        runningState([
          {
            id: "evt-stale",
            seq: 1,
            ts: "2026-05-29T08:00:50Z",
            kind: "chat.tool_call.started",
            status: "running",
            label: "工具调用",
            detail: "npm run dev",
          },
        ]),
        null,
        NOW,
      ).level,
    ).toBe("stale");

    const stalled = getRuntimeStallState(
      runningState([
        {
          id: "evt-stalled",
          seq: 1,
          ts: "2026-05-29T07:56:00Z",
          kind: "chat.tool_call.started",
          status: "running",
          label: "工具调用",
          detail: "npm run dev",
          payload: { phase: "preview", trace_title: "启动预览：npm" },
        },
      ]),
      null,
      NOW,
    );
    expect(stalled.level).toBe("stalled");
    expect(stalled.summary).toBe("长时间没有新进展 · 6m");
    expect(stalled.stageLabel).toBe("启动预览：npm");
  });

  it("keeps transport health separate from stalled business progress", () => {
    const active = getRuntimeStallState(
      {
        ...runningState([
          {
            id: "evt-model-started",
            seq: 4,
            ts: "2026-05-29T07:52:00Z",
            kind: "chat.message.started",
            status: "running",
            label: "生成回复",
            detail: "",
          },
        ]),
        transport_status: "connected",
        updated_at: "2026-05-29T08:01:50Z",
      },
      null,
      NOW,
    );

    expect(active.level).toBe("stalled");
    expect(active.transportAlive).toBe(true);
    expect(active.summary).toBe("连接正常 · 10m 无业务进展");
  });

  it("does not let an expired connected marker hide a real stall", () => {
    const expired = getRuntimeStallState(
      {
        ...runningState([
          {
            id: "evt-model-started",
            seq: 4,
            ts: "2026-05-29T07:52:00Z",
            kind: "chat.message.started",
            status: "running",
            label: "生成回复",
            detail: "",
          },
        ]),
        transport_status: "connected",
        updated_at: "2026-05-29T07:52:10Z",
      },
      null,
      NOW,
    );

    expect(expired.level).toBe("stalled");
    expect(expired.transportAlive).toBe(false);
    expect(expired.summary).toBe("长时间没有新进展 · 10m");
  });

  it("names a confirmed transport interruption instead of guessing", () => {
    const interrupted = getRuntimeStallState(
      {
        ...runningState([
          {
            id: "evt-last-progress",
            seq: 2,
            ts: "2026-05-29T07:56:00Z",
            kind: "chat.tool_call.started",
            status: "running",
            label: "工具调用",
            detail: "render",
          },
        ]),
        transport_status: "closed",
      },
      null,
      NOW,
    );

    expect(interrupted.level).toBe("stalled");
    expect(interrupted.transportAlive).toBe(false);
    expect(interrupted.summary).toBe("连接已中断 · 6m");
  });

  it("does not treat transport keepalives as substantive task progress", () => {
    const stalled = getRuntimeStallState(
      runningState([
        {
          id: "evt-turn-started",
          seq: 1,
          ts: "2026-05-29T07:58:00Z",
          kind: "chat.turn.started",
          status: "running",
          label: "开始运行",
          detail: "文本：做一个网页游戏",
        },
        {
          id: "evt-transport-keepalive",
          seq: 32,
          ts: "2026-05-29T08:02:00Z",
          kind: "chat.system.event",
          type: "runtime.progress",
          status: "running",
          label: "运行心跳",
          detail: "Runtime is active.",
          payload: { structured_keepalive: true },
        },
        {
          id: "evt-usage-diagnostic",
          seq: 33,
          ts: "2026-05-29T08:02:00Z",
          kind: "chat.system.event",
          type: "runtime.warning",
          status: "info",
          label: "系统事件",
          detail: "model usage",
          payload: { kind: "model_usage" },
        },
      ]),
      null,
      NOW,
    );

    expect(stalled.level).toBe("stale");
    expect(stalled.lastEvent?.id).toBe("evt-turn-started");
    expect(stalled.summary).toBe("连接正常 · 4m 未收到模型输出");
  });

  it("does not report the initial turn receipt as substantive progress", () => {
    const receiptOnly = getRuntimeStallState(
      runningState([
        {
          id: "evt-turn-started",
          seq: 1,
          ts: "2026-05-29T08:01:30Z",
          kind: "chat.turn.started",
          status: "running",
          label: "开始运行",
          detail: "文本：没啥事",
        },
      ]),
      null,
      NOW,
    );

    expect(receiptOnly.waitingForFirstOutput).toBe(true);
    expect(receiptOnly.hasSubstantiveProgress).toBe(false);
  });

  it("treats a running snapshot with no event yet as waiting for first output", () => {
    const waiting = getRuntimeStallState(
      {
        status: "running",
        run_id: "run-before-first-event",
        started_at: "2026-05-29T08:01:50Z",
        updated_at: "2026-05-29T08:01:50Z",
        events_preview: [],
      },
      null,
      NOW,
    );

    expect(waiting.level).toBe("healthy");
    expect(waiting.waitingForFirstOutput).toBe(true);
    expect(waiting.hasSubstantiveProgress).toBe(false);
  });

  it.each([
    ["legacy message kind", "chat.message.delta", undefined],
    ["native message type", "message.delta", "message.delta"],
    ["legacy tool kind", "chat.tool_call.started", undefined],
    ["native tool type", "tool.call.started", "tool.call.started"],
  ])(
    "reports a %s as substantive progress after the initial receipt",
    (_label, kind, type) => {
      const progressed = getRuntimeStallState(
        runningState([
          {
            id: "evt-turn-started",
            seq: 1,
            ts: "2026-05-29T08:01:20Z",
            kind: "chat.turn.started",
            status: "running",
            label: "开始运行",
            detail: "文本：整理方案",
          },
          {
            id: "evt-progress",
            seq: 2,
            ts: "2026-05-29T08:01:30Z",
            kind,
            type,
            status: "running",
            label: "继续处理",
            detail: "已有真实输出",
          },
        ]),
        null,
        NOW,
      );

      expect(progressed.waitingForFirstOutput).toBe(false);
      expect(progressed.hasSubstantiveProgress).toBe(true);
    },
  );

  it("recognizes a native turn.started row as waiting for first output", () => {
    const receiptOnly = getRuntimeStallState(
      runningState([
        {
          id: "evt-native-turn-started",
          seq: 1,
          ts: "2026-05-29T08:01:30Z",
          kind: "turn.started",
          type: "turn.started",
          status: "running",
          label: "开始运行",
          detail: "已收到请求",
        },
      ]),
      null,
      NOW,
    );

    expect(receiptOnly.waitingForFirstOutput).toBe(true);
    expect(receiptOnly.hasSubstantiveProgress).toBe(false);
  });

  it("returns idle for terminal runtime state", () => {
    expect(
      getRuntimeStallState(
        {
          status: "completed",
          events_preview: [],
        },
        null,
        NOW,
      ).level,
    ).toBe("idle");
  });

  it("keeps backend-stalled runtime state visible as stalled", () => {
    const stalled = getRuntimeStallState(
      {
        status: "stalled",
        summary: "本轮可能卡住",
        updated_at: "2026-05-29T08:00:00Z",
        events_preview: [
          {
            id: "evt-stalled",
            seq: 3,
            ts: "2026-05-29T08:00:00Z",
            kind: "chat.system.event",
            type: "turn.stalled",
            status: "stalled",
            label: "系统事件",
            detail: "后端没有活动任务",
            payload: {
              phase: "failure",
              trace_title: "本轮可能卡住",
            },
          },
        ],
      },
      null,
      NOW,
    );

    expect(stalled.level).toBe("stalled");
    expect(stalled.isRunning).toBe(false);
    expect(stalled.summary).toBe("本轮可能卡住");
    expect(stalled.stageLabel).toBe("本轮可能卡住");
  });

  it("builds continuation prompt with stage and last event detail", () => {
    const stall = getRuntimeStallState(
      runningState([
        {
          id: "evt-stalled",
          seq: 1,
          ts: "2026-05-29T08:00:00Z",
          kind: "chat.tool_call.started",
          status: "running",
          label: "工具调用",
          detail: "npm run build",
          payload: {
            phase: "build",
            trace_title: "构建项目：npm",
            trace_detail: "npm run build",
          },
        },
      ]),
      null,
      NOW,
    );

    const prompt = buildRuntimeContinuationPrompt({
      stall,
      runId: "run-1",
      workspaceName: "topic-workspace",
      workspaceDir: "/tmp/topic-workspace",
    });

    expect(prompt).toContain("不要重复已经完成的步骤");
    expect(prompt).toContain("普通问答直接回答");
    expect(prompt).toContain("- run_id: run-1");
    expect(prompt).toContain("- 停住阶段: 构建项目：npm");
    expect(prompt).toContain("- 最后事件详情: npm run build");
  });

  it("uses readable fallback stage labels", () => {
    expect(
      runtimeStageLabel({
        id: "evt",
        ts: "2026-05-29T08:00:00Z",
        kind: "chat.tool_call.started",
        status: "running",
        label: "工具调用",
        detail: "",
      }),
    ).toBe("工具调用");
  });
});
