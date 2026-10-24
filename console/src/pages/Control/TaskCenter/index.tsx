import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Alert,
  Button,
  Collapse,
  Drawer,
  Empty,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Spin,
} from "antd";
import {
  CalendarOutlined,
  ClockCircleOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  Bot,
  FileSpreadsheet,
  FileText,
  Presentation,
  Sparkles,
} from "lucide-react";
import dayjs from "dayjs";
import { useNavigate, useSearchParams } from "react-router-dom";
import { chatApi } from "../../../api/modules/chat";
import { cronJobApi } from "../../../api/modules/cronjob";
import { capabilityApi } from "../../../api/modules/capability";
import {
  debugApi,
  type DebugTurnEventRecord,
} from "../../../api/modules/debug";
import { heartbeatApi } from "../../../api/modules/heartbeat";
import { runtimeApi } from "../../../api/modules/runtime";
import { officeDocumentApi } from "../../../api/modules/officeDocument";
import type {
  ChatSpec,
  CronJobSpecInput,
  CronJobSpecOutput,
  OfficeTaskTemplate,
  RuntimeFailureDiagnosis,
  TaskRun,
} from "../../../api/types";
import type {
  RuntimeEffectRepairAction,
  RuntimeEffectRepairRequest,
  RuntimeEffectResolution,
  RuntimeEffectResolutionRequest,
  RuntimeEvent,
  RuntimeBudget,
  RuntimeRunControlRequest,
  RuntimeRunExportResponse,
  RuntimeRunSource,
  RuntimeRunStatus,
  RuntimeRunSummary,
} from "../../../api/types/runtime";
import type { HeartbeatConfig } from "../../../api/types/heartbeat";
import {
  CapabilityCard,
  CapabilityChip,
  CapabilityPage,
} from "../../../components/CapabilityLayout";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useAgentStore } from "../../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";
import { selectRequestedRuntimeRun } from "./runtimeRunSelection";
import { saveRetryDraft } from "../../Chat/retryDraft";
import {
  formatRoutingDetail,
  normalizeRoutingSnapshot,
} from "../../../utils/routingDecision";
import {
  buildRuntimeRepairPrompt,
  previewHarnessReportFromPayload,
  runtimeFailureDiagnosisFromPayload,
  runtimeFailureDiagnosisTone,
} from "../../../utils/runtimeFailureDiagnosis";
import { parseErrorDetail } from "../../../utils/error";
import RuntimeRunBoard from "./RuntimeRunBoard";
import CollaborationRoom from "./CollaborationRoom";
import {
  deriveCollaborationParticipants,
  deriveHostedCollaborationProjection,
} from "./collaborationProjection";
import styles from "./index.module.less";

interface PendingTurnMeta {
  text?: string;
  status?: string;
  created_at?: string;
  run_id?: string;
  last_turn_run_id?: string;
}

interface CronJobStateView {
  next_run_at?: string | null;
  last_run_at?: string | null;
  last_status?: string | null;
  last_error?: string | null;
}

interface UnifiedActivityItem {
  id: string;
  type: "chat" | "cron";
  title: string;
  summary: string;
  status: string;
  timestamp: number;
  actionLabel: string;
  actionHref: string;
}

type TaskEventFilter = "all" | "heartbeat" | "cron";
type TaskEventStatusFilter = "all" | "issues";
type RuntimeSourceFilter = "all" | RuntimeRunSource;
type RuntimeStatusFilter = "all" | RuntimeRunStatus;
type RuntimeViewMode = "list" | "board";
type RuntimeBudgetNumberKey =
  | "max_turns"
  | "max_steps"
  | "max_tool_calls"
  | "max_seconds"
  | "max_tokens"
  | "max_cost_points"
  | "max_artifact_bytes";

interface RuntimeBudgetEditorState {
  run: RuntimeRunSummary;
  budget: RuntimeBudget;
  reason: string;
}

const OFFICE_TEMPLATE_ICONS: Record<string, ReactNode> = {
  "weekly-report-docx": <FileText size={18} />,
  "sales-table-xlsx": <FileSpreadsheet size={18} />,
  "briefing-deck-pptx": <Presentation size={18} />,
};

function officeTemplateCron(templateId: string): string {
  if (templateId === "weekly-report-docx") return "0 9 * * fri";
  if (templateId === "sales-table-xlsx") return "0 18 * * fri";
  return "0 9 * * mon";
}

function buildOfficeCronJobSpec(
  template: OfficeTaskTemplate,
): CronJobSpecInput {
  const sessionId = `office-template:${template.id}`;
  const prompt = `${template.prompt}\n\n这是一个定时 Office 文档任务。请在每次运行时读取当前工作区资料，生成或更新对应 Office 文件，并在最终回复中给出文件路径、预览检查结果和下一步修改建议。`;
  return {
    name: template.title,
    enabled: true,
    schedule: {
      type: "cron",
      cron: officeTemplateCron(template.id),
      timezone: "Asia/Shanghai",
    },
    task_type: "agent",
    request: {
      input: [
        {
          role: "user",
          content: [{ type: "text", text: prompt }],
        },
      ],
      user_id: "default",
      session_id: sessionId,
    },
    dispatch: {
      type: "channel",
      channel: "console",
      target: {
        user_id: "default",
        session_id: sessionId,
      },
      mode: "final",
      meta: {
        source: "office_task_template",
        template_id: template.id,
      },
    },
    runtime: {
      share_session: true,
      max_concurrency: 1,
      timeout_seconds: 900,
      misfire_grace_seconds: 300,
    },
    meta: {
      created_from: "office_task_template",
      template_id: template.id,
    },
  };
}

function isTaskLedgerEvent(item: DebugTurnEventRecord) {
  return (
    item.scope === "task" ||
    item.kind.startsWith("cron.") ||
    item.kind.startsWith("heartbeat.")
  );
}

function workspaceLabel(value?: string | null) {
  const normalized = String(value || "").trim();
  if (!normalized) return "默认工作区";
  const parts = normalized.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || normalized;
}

function getTaskEventHref(item: DebugTurnEventRecord) {
  return item.kind.startsWith("heartbeat.") ? "/heartbeat" : "/cron-jobs";
}

function matchesTaskEventFilter(
  item: DebugTurnEventRecord,
  filter: TaskEventFilter,
) {
  if (filter === "all") return true;
  if (filter === "heartbeat") return item.kind.startsWith("heartbeat.");
  return item.kind.startsWith("cron.");
}

function isTaskEventIssue(item: DebugTurnEventRecord) {
  if (item.status === "interrupted") return true;
  const combined =
    `${item.summary} ${item.detail} ${item.status}`.toLowerCase();
  return /(失败|超时|取消|中断|error|timeout|cancelled|interrupted)/.test(
    combined,
  );
}

function matchesTaskEventStatusFilter(
  item: DebugTurnEventRecord,
  filter: TaskEventStatusFilter,
) {
  if (filter === "all") return true;
  return isTaskEventIssue(item);
}

function getTaskEventActionLabel(item: DebugTurnEventRecord) {
  const isHeartbeat = item.kind.startsWith("heartbeat.");
  const issue = isTaskEventIssue(item);
  if (isHeartbeat) {
    return issue ? "去检查心跳" : "打开心跳";
  }
  return issue ? "去检查定时" : "打开定时";
}

const RECOMMENDED_HEARTBEAT_CONFIG: HeartbeatConfig = {
  enabled: true,
  every: "6h",
  target: "main",
  activeHours: {
    start: "08:00",
    end: "22:00",
  },
};

function parsePendingTurn(chat: ChatSpec): PendingTurnMeta | null {
  const raw = chat.meta?.pending_turn;
  if (!raw || typeof raw !== "object") return null;
  return raw as PendingTurnMeta;
}

function normalizeCronStatus(status?: string | null) {
  const normalized = String(status || "")
    .trim()
    .toLowerCase();
  if (!normalized) return "idle";
  return normalized;
}

function formatTime(value?: string | null) {
  if (!value) return "暂无记录";
  const parsed = dayjs(value);
  if (!parsed.isValid()) return "暂无记录";
  return parsed.format("MM/DD HH:mm");
}

function formatRelativeTime(value?: string | null) {
  if (!value) return "暂无记录";
  const parsed = dayjs(value);
  if (!parsed.isValid()) return "暂无记录";
  const diffMinutes = Math.max(0, dayjs().diff(parsed, "minute"));
  if (diffMinutes < 1) return "刚刚";
  if (diffMinutes < 60) return `${diffMinutes} 分钟前`;
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours} 小时前`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays} 天前`;
}

function textPreview(value: string, maxLength = 72) {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, maxLength).trim()}...`;
}

function formatCompactNumber(value?: number) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric) || numeric <= 0) return "0";
  if (numeric >= 10000) return `${(numeric / 1000).toFixed(1)}K`;
  return Math.round(numeric).toLocaleString();
}

function formatCompactBytes(value?: number) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric) || numeric <= 0) return "0B";
  if (numeric >= 1024 * 1024)
    return `${(numeric / (1024 * 1024)).toFixed(1)}MB`;
  if (numeric >= 1024) return `${(numeric / 1024).toFixed(1)}KB`;
  return `${Math.round(numeric)}B`;
}

function formatDuration(ms?: number | null) {
  if (!ms) return "暂无";
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function formatRuntimeUsage(run: RuntimeRunSummary) {
  const usage = run.usage || {};
  const tokenTotal =
    Number(usage.input_tokens || 0) + Number(usage.output_tokens || 0);
  const parts: string[] = [];
  if (tokenTotal > 0) parts.push(`Token ${formatCompactNumber(tokenTotal)}`);
  if (Number(usage.cache_tokens || 0) > 0) {
    parts.push(`缓存 ${formatCompactNumber(usage.cache_tokens)}`);
  }
  if (Number(usage.context_tokens || 0) > 0) {
    parts.push(`上下文 ${formatCompactNumber(usage.context_tokens)}`);
  }
  if (Number(usage.cost_points || 0) > 0) {
    parts.push(`用量点 ${formatCompactNumber(usage.cost_points)}`);
  }
  if (Number(usage.tool_calls || 0) > 0) {
    parts.push(`工具 ${formatCompactNumber(usage.tool_calls)}`);
  }
  if (Number(usage.artifact_bytes || 0) > 0) {
    parts.push(`产物 ${formatCompactBytes(usage.artifact_bytes)}`);
  }
  return parts.join(" · ") || "暂无用量";
}

function runtimeSourceLabel(source: string) {
  return (
    {
      chat: "聊天",
      tool: "工具",
      approval: "审批",
      mission: "任务",
      browser: "浏览器",
      capability: "能力",
      pipeline: "流水线",
      cron: "定时",
      reminder: "提醒",
      heartbeat: "心跳",
      image: "已退役图片任务",
      skills: "技能",
      system: "系统",
    }[source] || source
  );
}

function runtimeStatusLabel(status: string) {
  return (
    {
      running: "运行中",
      completed: "已完成",
      failed: "失败",
      interrupted: "已中断",
      stalled: "已卡住",
      idle: "空闲",
      unknown: "未知",
    }[status] || status
  );
}

function runtimeAuthorityStatusLabel(status?: string | null) {
  const normalized = String(status || "").trim();
  if (!normalized) return "未知";
  return (
    {
      queued: "排队",
      claimed: "已认领",
      running: "运行中",
      waiting_approval: "等待审批",
      paused: "已暂停",
      stop_requested: "停止中",
      succeeded: "已成功",
      failed: "失败",
      cancelled: "已取消",
      stalled: "已卡住",
    }[normalized] || normalized
  );
}

function runtimeShortId(value?: string | null) {
  const text = String(value || "").trim();
  if (!text) return "暂无";
  if (text.length <= 18) return text;
  return `${text.slice(0, 10)}...${text.slice(-5)}`;
}

function runtimeRecordText(
  record: Record<string, unknown> | null | undefined,
  key: string,
) {
  const value = record?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function runtimeRecordNumber(
  record: Record<string, unknown> | null | undefined,
  key: string,
) {
  const value = record?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function runtimeStepLabel(step?: Record<string, unknown> | null) {
  if (!step) return "暂无";
  const title = runtimeRecordText(step, "title");
  const kind = runtimeRecordText(step, "kind");
  const index = runtimeRecordNumber(step, "index");
  const prefix = index !== null ? `#${index + 1}` : "";
  return [prefix, title || kind || "当前步骤"].filter(Boolean).join(" ");
}

function runtimeBudgetLabel(budget?: Record<string, unknown> | null) {
  if (!budget || Object.keys(budget).length === 0) return "未设置";
  const parts: string[] = [];
  const maxSteps = runtimeRecordNumber(budget, "max_steps");
  const maxTurns = runtimeRecordNumber(budget, "max_turns");
  const maxToolCalls = runtimeRecordNumber(budget, "max_tool_calls");
  const maxSeconds = runtimeRecordNumber(budget, "max_seconds");
  const maxTokens = runtimeRecordNumber(budget, "max_tokens");
  const maxArtifactBytes = runtimeRecordNumber(budget, "max_artifact_bytes");
  if (maxSteps !== null) parts.push(`${maxSteps} 步`);
  if (maxTurns !== null) parts.push(`${maxTurns} 轮`);
  if (maxToolCalls !== null) parts.push(`${maxToolCalls} 工具`);
  if (maxSeconds !== null) parts.push(`${Math.round(maxSeconds)} 秒`);
  if (maxTokens !== null)
    parts.push(`${formatCompactNumber(maxTokens)} tokens`);
  if (maxArtifactBytes !== null) {
    parts.push(`${formatCompactBytes(maxArtifactBytes)} 产物`);
  }
  return parts.join(" · ") || "已设置";
}

function runtimeEditableBudget(
  budget?: Record<string, unknown> | null,
): RuntimeBudget {
  return {
    max_turns: runtimeRecordNumber(budget, "max_turns"),
    max_steps: runtimeRecordNumber(budget, "max_steps"),
    max_tool_calls: runtimeRecordNumber(budget, "max_tool_calls"),
    max_seconds: runtimeRecordNumber(budget, "max_seconds"),
    max_tokens: runtimeRecordNumber(budget, "max_tokens"),
    max_cost_points: runtimeRecordNumber(budget, "max_cost_points"),
    max_artifact_bytes: runtimeRecordNumber(budget, "max_artifact_bytes"),
    deadline_at: runtimeRecordText(budget, "deadline_at") || null,
  };
}

function normalizeBudgetNumber(value: number | string | null) {
  if (value === null || value === "") return null;
  const numberValue = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}

function normalizeBudgetDeadline(value: string | null | undefined) {
  const text = String(value || "").trim();
  if (!text) return null;
  const parsed = dayjs(text);
  return parsed.isValid() ? parsed.toISOString() : text;
}

function runtimeCheckpointLabel(checkpoint?: Record<string, unknown> | null) {
  if (!checkpoint) return "暂无";
  return (
    runtimeRecordText(checkpoint, "summary") ||
    runtimeRecordText(checkpoint, "checkpoint_id") ||
    "已有 checkpoint"
  );
}

function runtimeEvaluationLabel(summary?: Record<string, unknown> | null) {
  if (!summary) return "暂无";
  return (
    runtimeRecordText(summary, "conclusion") ||
    runtimeRecordText(summary, "summary") ||
    "已有 evaluator"
  );
}

function runtimeRecoveryLabel(recovery?: Record<string, unknown> | null) {
  if (!recovery) return "暂无";
  const parts: string[] = [];
  if (recovery.can_pause) parts.push("可暂停");
  if (recovery.can_resume) parts.push("可原地恢复");
  if (recovery.can_takeover) parts.push("可接管");
  if (recovery.resume_requires_durable_lease && !recovery.can_resume) {
    parts.push("需接管恢复");
  }
  if (recovery.can_retry) parts.push("可重试");
  if (recovery.can_interrupt) parts.push("可中断");
  if (recovery.effect_requires_probe) parts.push("需探测");
  if (recovery.effect_requires_human) parts.push("需人工判定");
  if (recovery.effect_checkpoint_rebuild_required) {
    parts.push("需修复 checkpoint");
  }
  if (recovery.effect_terminal_rewrite_required) parts.push("需补终态");
  if (recovery.effect_replay_forbidden) parts.push("禁止自动重放");
  return parts.join(" · ") || "只读";
}

function runtimeRecoveryEffects(run: RuntimeRunSummary | null) {
  const raw = run?.recovery?.effects;
  if (!Array.isArray(raw)) return [];
  return raw.filter(
    (item): item is Record<string, unknown> =>
      Boolean(item) && typeof item === "object" && !Array.isArray(item),
  );
}

function runtimeEffectStatusLabel(status: string) {
  return (
    {
      intent: "已登记",
      waiting_approval: "等待审批",
      approved: "已批准",
      blocked: "已阻止",
      running: "执行中",
      committed: "已提交",
      failed: "失败",
      ambiguous: "待判定",
      probed_committed: "探测确认已执行",
      safe_to_retry: "可安全重试",
      compensated: "已补偿",
      human_resolved: "人工已处理",
      cancelled: "已取消",
    }[status] || status
  );
}

type RuntimeNotificationAlertType = "success" | "info" | "warning" | "error";

function runtimeNotificationKey(event: RuntimeEvent) {
  const payload = event.payload || {};
  return (
    runtimeRecordText(payload, "dedupe_key") ||
    event.item_id ||
    runtimeRecordText(payload, "notification_id") ||
    event.id
  );
}

function runtimeNotificationEvents(events: RuntimeEvent[]) {
  const latestByKey = new Map<string, RuntimeEvent>();
  for (const event of events) {
    if (event.type !== "runtime.notification") continue;
    const key = runtimeNotificationKey(event);
    const current = latestByKey.get(key);
    if (!current || event.seq >= current.seq) {
      latestByKey.set(key, event);
    }
  }
  return [...latestByKey.values()].sort(
    (first, second) => first.seq - second.seq,
  );
}

function runtimeNotificationTitle(event: RuntimeEvent) {
  return runtimeRecordText(event.payload, "title") || "运行提醒";
}

function runtimeNotificationMessage(event: RuntimeEvent) {
  return (
    runtimeRecordText(event.payload, "message") ||
    runtimeRecordText(event.payload, "summary") ||
    runtimeRecordText(event.payload, "detail") ||
    runtimeNotificationTitle(event)
  );
}

function runtimeNotificationAlertType(
  event: RuntimeEvent,
): RuntimeNotificationAlertType {
  const kind = runtimeRecordText(event.payload, "notification_kind");
  if (kind === "final_complete") return "success";
  if (kind === "budget_exhausted") return "error";
  if (kind === "waiting_approval" || kind === "ambiguous_effect") {
    return "warning";
  }
  return event.status === "failed" ? "error" : "info";
}

function runtimeNotificationMeta(event: RuntimeEvent) {
  const payload = event.payload || {};
  const parts: string[] = [];
  const sourceStatus = runtimeRecordText(payload, "source_status");
  const contractId = runtimeRecordText(payload, "contract_id");
  const actionLabel = runtimeRecordText(payload, "action_label");
  if (sourceStatus) {
    parts.push(`状态 ${runtimeAuthorityStatusLabel(sourceStatus)}`);
  }
  if (contractId) parts.push(`Contract ${runtimeShortId(contractId)}`);
  if (actionLabel) parts.push(actionLabel);
  if (payload.export_recommended === true) parts.push("建议导出审计包");
  return parts.join(" · ");
}

function runtimeEffectResolutionReason(resolution: RuntimeEffectResolution) {
  return (
    {
      mark_ambiguous: "用户从 Task Center 标记该副作用待判定",
      probe_committed: "用户确认已探测外部状态且副作用已执行",
      safe_to_retry: "用户确认该副作用可安全重试",
      compensated: "用户确认该副作用已完成补偿",
      human_resolved: "用户确认该副作用已人工处理",
    }[resolution] || `用户处理副作用：${resolution}`
  );
}

function runtimeEffectResolutionBody(
  run: RuntimeRunSummary,
  effect: Record<string, unknown>,
  resolution: RuntimeEffectResolution,
): RuntimeEffectResolutionRequest {
  return {
    ...runtimeRunControlRequest(run),
    expected_effect_revision: runtimeRecordNumber(effect, "revision"),
    resolution,
    reason: runtimeEffectResolutionReason(resolution),
  };
}

function runtimeEffectRepairReason(action: RuntimeEffectRepairAction) {
  return (
    {
      rebuild_checkpoint: "用户确认外部副作用已提交，补写本地 checkpoint",
      rewrite_terminal: "用户确认 checkpoint 已修复，补写运行终态",
    }[action] || `用户修复副作用状态：${action}`
  );
}

function runtimeEffectRepairBody(
  run: RuntimeRunSummary,
  effect: Record<string, unknown>,
  action: RuntimeEffectRepairAction,
): RuntimeEffectRepairRequest {
  const effectId = runtimeRecordText(effect, "effect_id") || "effect";
  const body: RuntimeEffectRepairRequest = {
    ...runtimeRunControlRequest(run),
    action,
    expected_effect_revision: runtimeRecordNumber(effect, "revision"),
    reason: runtimeEffectRepairReason(action),
  };
  if (action === "rebuild_checkpoint") {
    body.checkpoint_id = `effect-repair:${effectId}`;
    body.checkpoint_seq = runtimeRunLatestSeq(run);
    body.checkpoint_summary = "副作用已提交，checkpoint 已人工修复";
  }
  if (action === "rewrite_terminal") {
    body.terminal_status = "succeeded";
  }
  return body;
}

function runtimeRecoveryFlag(
  run: RuntimeRunSummary | null,
  key: string,
): boolean {
  return Boolean(run?.recovery?.[key]);
}

function runtimeEmptyEventsDescription(run: RuntimeRunSummary | null) {
  if (run?.projection_source === "runtime_store") {
    return "运行合同已保存，暂无事件记录";
  }
  return "暂无运行事件";
}

function runtimeEventTitle(event: RuntimeEvent) {
  const payload = event.payload || {};
  const phase = String(payload.phase || "");
  if (payload.trace_title) return String(payload.trace_title);
  if (phase === "command" && payload.command) {
    const command = String(payload.command);
    return `命令执行：${command.split(/\s+/)[0] || "shell"}`;
  }
  if (phase === "file" || Array.isArray(payload.files_changed)) {
    return "文件变更";
  }
  if (phase === "subtask" && payload.sub_run) {
    const subRun = payload.sub_run as Record<string, unknown>;
    return `子任务：${String(subRun.agent_name || subRun.tool_name || "Agent")}`;
  }
  return String(payload.label || payload.summary || event.type || "事件");
}

function runtimePayloadText(
  payload: Record<string, unknown>,
  ...keys: string[]
) {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function runtimeSubRunPayload(event: RuntimeEvent) {
  const payload = event.payload || {};
  return payload.sub_run && typeof payload.sub_run === "object"
    ? (payload.sub_run as Record<string, unknown>)
    : {};
}

function runtimeSubtaskDetail(event: RuntimeEvent) {
  const payload = event.payload || {};
  const subRun = runtimeSubRunPayload(event);
  if (
    payload.phase !== "subtask" &&
    event.presentation?.surface !== "subagent" &&
    !Object.keys(subRun).length
  ) {
    return "";
  }
  const parts = [
    runtimePayloadText(payload, "agent_role") ||
      runtimePayloadText(subRun, "agent_role"),
    runtimePayloadText(payload, "delegation_reason") ||
      runtimePayloadText(subRun, "delegation_reason"),
    (() => {
      const value =
        runtimePayloadText(payload, "permission_profile") ||
        runtimePayloadText(subRun, "permission_profile");
      return value ? `权限 ${value}` : "";
    })(),
    (() => {
      const value =
        runtimePayloadText(payload, "inherited_context_summary") ||
        runtimePayloadText(subRun, "inherited_context_summary");
      return value ? `上下文 ${value}` : "";
    })(),
    (() => {
      const value =
        runtimePayloadText(payload, "current_tool") ||
        runtimePayloadText(subRun, "current_tool");
      return value ? `当前工具 ${value}` : "";
    })(),
    (() => {
      const value =
        runtimePayloadText(payload, "failure_reason") ||
        runtimePayloadText(subRun, "failure_reason");
      return value ? `失败 ${value}` : "";
    })(),
    (() => {
      const value =
        runtimePayloadText(payload, "result_summary") ||
        runtimePayloadText(subRun, "result_summary");
      return value ? `结果 ${value}` : "";
    })(),
  ].filter(Boolean);
  const duration =
    typeof payload.duration_ms === "number"
      ? formatDuration(payload.duration_ms)
      : typeof subRun.duration_ms === "number"
        ? formatDuration(subRun.duration_ms)
        : "";
  if (duration) parts.push(duration);
  return parts.join(" · ");
}

function runtimeEventSourceLine(event: RuntimeEvent) {
  const payload = event.payload || {};
  const subRun = runtimeSubRunPayload(event);
  const sourceAgent =
    runtimePayloadText(payload, "agent_name", "source_agent_name") ||
    runtimePayloadText(subRun, "agent_name");
  const parentRunId =
    runtimePayloadText(payload, "parent_run_id", "source_run_id") ||
    runtimePayloadText(subRun, "parent_run_id");
  const parts = [
    sourceAgent ? `来源 ${sourceAgent}` : "",
    parentRunId ? `父运行 ${parentRunId.slice(0, 8)}` : "",
  ].filter(Boolean);
  return parts.join(" · ");
}

function runtimeEventFailureDiagnosis(event: RuntimeEvent) {
  return runtimeFailureDiagnosisFromPayload(event.payload);
}

function runtimeEventDetail(event: RuntimeEvent) {
  const payload = event.payload || {};
  const subtaskDetail = runtimeSubtaskDetail(event);
  if (subtaskDetail) return subtaskDetail;
  if (payload.trace_detail) {
    const duration =
      typeof payload.duration_ms === "number"
        ? ` · ${formatDuration(payload.duration_ms)}`
        : "";
    return `${String(payload.trace_detail)}${duration}`;
  }
  if (payload.command) {
    const parts = [
      String(payload.command),
      payload.cwd ? `cwd ${String(payload.cwd)}` : "",
      payload.exit_code !== undefined
        ? `退出码 ${String(payload.exit_code)}`
        : "",
      payload.output_summary ? String(payload.output_summary) : "",
    ].filter(Boolean);
    return parts.join(" · ");
  }
  if (Array.isArray(payload.files_changed)) {
    return payload.files_changed
      .map((item) => {
        if (!item || typeof item !== "object") return String(item);
        const change = item as Record<string, unknown>;
        const lineSummary = [
          Number(change.added_lines || 0) ? `+${change.added_lines}` : "",
          Number(change.deleted_lines || 0) ? `-${change.deleted_lines}` : "",
        ].filter(Boolean);
        return [
          change.path ? String(change.path) : "文件",
          lineSummary.join(" / "),
          change.summary ? String(change.summary) : "",
        ]
          .filter(Boolean)
          .join(" · ");
      })
      .join("\n");
  }
  if (
    event.type === "model.routing_decision" ||
    payload.legacy_kind === "chat.model.routing_decision"
  ) {
    return (
      formatRoutingDetail(normalizeRoutingSnapshot(payload)) ||
      "已记录本轮模型选择"
    );
  }
  return String(payload.detail || payload.summary || event.type || "");
}

type RuntimeEventPhase =
  | "request"
  | "model"
  | "command"
  | "file"
  | "tool"
  | "approval"
  | "subtask"
  | "artifact"
  | "result"
  | "failure";

const RUNTIME_PHASE_LABELS: Record<RuntimeEventPhase, string> = {
  request: "用户请求",
  model: "模型 / 思考",
  command: "命令执行",
  file: "文件变更",
  tool: "工具调用",
  approval: "审批",
  subtask: "子任务",
  artifact: "产物",
  result: "结果",
  failure: "失败原因",
};

const RUNTIME_PHASE_ORDER: RuntimeEventPhase[] = [
  "request",
  "model",
  "command",
  "file",
  "tool",
  "approval",
  "subtask",
  "artifact",
  "result",
  "failure",
];

function runtimeEventPhase(event: RuntimeEvent): RuntimeEventPhase {
  const legacyKind = String(event.payload?.legacy_kind || "");
  const explicitPhase = String(event.payload?.phase || "").toLowerCase();
  const type = String(event.type || "");
  const status = String(event.status || "").toLowerCase();
  if (explicitPhase === "command" || explicitPhase === "search")
    return "command";
  if (explicitPhase === "file") return "file";
  if (explicitPhase === "subtask") return "subtask";
  if (explicitPhase === "approval") return "approval";
  if (explicitPhase === "artifact") return "artifact";
  if (explicitPhase === "result") return "result";
  if (explicitPhase === "failure") return "failure";
  if (
    status === "failed" ||
    status === "stalled" ||
    type.includes("failed") ||
    type.includes("stalled") ||
    legacyKind === "chat.error"
  ) {
    return "failure";
  }
  if (
    type === "model.routing_decision" ||
    legacyKind === "chat.model.routing_decision"
  ) {
    return "model";
  }
  if (legacyKind === "chat.turn.started" || event.source === "user") {
    return "request";
  }
  if (type.startsWith("approval.") || legacyKind.startsWith("chat.approval.")) {
    return "approval";
  }
  if (Array.isArray(event.payload?.files_changed)) return "file";
  if (event.payload?.command) return "command";
  if (event.presentation?.surface === "subagent") return "subtask";
  if (type.startsWith("tool.") || legacyKind.startsWith("chat.tool")) {
    return "tool";
  }
  if (legacyKind.includes("artifact") || legacyKind.startsWith("image.")) {
    return "artifact";
  }
  if (
    type.includes("completed") ||
    legacyKind.includes("settled") ||
    legacyKind === "chat.final_answer"
  ) {
    return "result";
  }
  return "model";
}

function groupRuntimeEvents(events: RuntimeEvent[]) {
  const groups = new Map<RuntimeEventPhase, RuntimeEvent[]>();
  events.forEach((event) => {
    const phase = runtimeEventPhase(event);
    groups.set(phase, [...(groups.get(phase) || []), event]);
  });
  return RUNTIME_PHASE_ORDER.map((phase) => ({
    phase,
    events: groups.get(phase) || [],
  })).filter((group) => group.events.length > 0);
}

function runtimeRunChatId(run: RuntimeRunSummary | null): string | null {
  const raw = run?.chat_id;
  return typeof raw === "string" && raw.trim() ? raw.trim() : null;
}

function runtimeRunLatestSeq(run: RuntimeRunSummary): number {
  const value = run.debug_refs?.latest_seq;
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function runtimeRunStageLabel(run: RuntimeRunSummary): string {
  return (
    run.summary ||
    run.last_event_summary ||
    run.title ||
    runtimeStatusLabel(run.status)
  );
}

function runtimeRunControlRequest(
  run: RuntimeRunSummary,
): RuntimeRunControlRequest {
  return {
    expected_attempt_id: run.attempt_id ?? null,
    expected_revision: run.revision ?? null,
    chat_id: run.chat_id ?? null,
    session_id: run.session_id ?? null,
    workspace_dir: run.workspace_dir ?? null,
  };
}

function runtimeExportArtifactCount(
  result: RuntimeRunExportResponse | null,
): number {
  const retentionCount = result?.package.retention_summary?.artifact_count;
  if (typeof retentionCount === "number" && Number.isFinite(retentionCount)) {
    return retentionCount;
  }
  return result?.package.artifact_manifest?.artifacts?.length || 0;
}

function runtimeExportTraceCount(result: RuntimeRunExportResponse | null) {
  return result?.package.trace_events?.length || 0;
}

function runtimeExportRetentionLabel(
  result: RuntimeRunExportResponse | null,
): string {
  if (!result) return "";
  const retention = result.package.retention_summary;
  const days = retention?.contract_retention_days;
  const deletePolicy = retention?.delete_artifacts_on_contract_delete
    ? "合同删除时删除产物"
    : "合同删除时保留产物";
  const approvalPolicy = retention?.export_requires_approval
    ? "导出需审批"
    : "导出无需审批";
  return [
    `Trace ${runtimeExportTraceCount(result)} 条`,
    `产物 ${runtimeExportArtifactCount(result)} 个`,
    typeof days === "number" && Number.isFinite(days)
      ? `合同保留 ${days} 天`
      : "合同保留策略未知",
    approvalPolicy,
    deletePolicy,
  ].join(" · ");
}

function runtimeExportDetailLabel(result: RuntimeRunExportResponse | null) {
  if (!result) return "";
  const contractId = result.package.contract?.contract_id;
  const manifestHash = result.package.artifact_manifest?.manifest_hash;
  return [
    typeof contractId === "string" && contractId.trim()
      ? `Contract ${runtimeShortId(contractId)}`
      : null,
    typeof manifestHash === "string" && manifestHash.trim()
      ? `Manifest ${runtimeShortId(manifestHash)}`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function isStaleRuntimeAttemptError(error: unknown) {
  const code = parseErrorDetail(error)?.code;
  return code === "stale_attempt" || code === "stale_runtime_effect";
}

function isStalledRuntimeRun(run: RuntimeRunSummary | null): boolean {
  return String(run?.status || "").toLowerCase() === "stalled";
}

function runtimeAuthorityStatus(run: RuntimeRunSummary | null): string {
  return String(run?.authority_status || run?.status || "")
    .trim()
    .toLowerCase();
}

function canPauseRuntimeRun(run: RuntimeRunSummary | null): boolean {
  const authorityStatus = runtimeAuthorityStatus(run);
  return (
    run?.source === "browser" &&
    runtimeRecoveryFlag(run, "can_pause") &&
    ["running", "waiting_approval"].includes(authorityStatus)
  );
}

function canResumeRuntimeRun(run: RuntimeRunSummary | null): boolean {
  return (
    run?.source === "browser" &&
    runtimeRecoveryFlag(run, "can_resume") &&
    runtimeAuthorityStatus(run) === "paused"
  );
}

function canArchiveRuntimeRun(run: RuntimeRunSummary | null): boolean {
  const status = runtimeAuthorityStatus(run);
  return [
    "completed",
    "succeeded",
    "failed",
    "interrupted",
    "cancelled",
    "canceled",
    "stalled",
  ].includes(status);
}

function canUpdateRuntimeBudget(run: RuntimeRunSummary | null): boolean {
  const status = runtimeAuthorityStatus(run);
  return [
    "queued",
    "claimed",
    "running",
    "waiting_approval",
    "paused",
    "stop_requested",
  ].includes(status);
}

function getChatSummary(chat: ChatSpec) {
  const pendingTurn = parsePendingTurn(chat);
  if (pendingTurn?.text) return textPreview(pendingTurn.text);
  if (chat.pinned) return "已置顶会话";
  return "等待新的输入";
}

function getChatActivity(chat: ChatSpec): UnifiedActivityItem | null {
  const pendingTurn = parsePendingTurn(chat);
  const sourceTime =
    pendingTurn?.created_at || chat.updated_at || chat.created_at;
  const parsed = sourceTime ? dayjs(sourceTime) : null;
  if (!parsed?.isValid()) return null;
  return {
    id: `chat-${chat.id}`,
    type: "chat",
    title: chat.name || "新会话",
    summary: getChatSummary(chat),
    status:
      chat.status === "running" ? "running" : pendingTurn?.status || "idle",
    timestamp: parsed.valueOf(),
    actionLabel: "打开会话",
    actionHref: `/chat/${chat.id}`,
  };
}

function getCronActivity(
  job: CronJobSpecOutput,
  state: CronJobStateView | null,
): UnifiedActivityItem | null {
  const timeValue = state?.last_run_at || state?.next_run_at || "";
  const parsed = timeValue ? dayjs(timeValue) : null;
  if (!parsed?.isValid()) return null;
  const status = normalizeCronStatus(state?.last_status);
  return {
    id: `cron-${job.id}`,
    type: "cron",
    title: job.name,
    summary:
      job.task_type === "text"
        ? textPreview(job.text || "")
        : job.task_type === "capability"
          ? "能力任务"
          : "智能体任务",
    status,
    timestamp: parsed.valueOf(),
    actionLabel: "查看定时",
    actionHref: "/cron-jobs",
  };
}

export default function TaskCenterPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { message } = useAppMessage();
  const { selectedAgent } = useAgentStore();
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [heartbeatSaving, setHeartbeatSaving] = useState(false);
  const [heartbeatEnabled, setHeartbeatEnabled] = useState(false);
  const [chats, setChats] = useState<ChatSpec[]>([]);
  const [cronJobs, setCronJobs] = useState<CronJobSpecOutput[]>([]);
  const [cronStates, setCronStates] = useState<
    Record<string, CronJobStateView>
  >({});
  const [taskEvents, setTaskEvents] = useState<DebugTurnEventRecord[]>([]);
  const [taskRuns, setTaskRuns] = useState<TaskRun[]>([]);
  const [officeTemplates, setOfficeTemplates] = useState<OfficeTaskTemplate[]>(
    [],
  );
  const [runtimeRuns, setRuntimeRuns] = useState<RuntimeRunSummary[]>([]);
  const [runtimeEvents, setRuntimeEvents] = useState<RuntimeEvent[]>([]);
  const [selectedRun, setSelectedRun] = useState<RuntimeRunSummary | null>(
    null,
  );
  const [runtimeDrawerOpen, setRuntimeDrawerOpen] = useState(false);
  const [runtimeEventsLoading, setRuntimeEventsLoading] = useState(false);
  const [runtimeExportingId, setRuntimeExportingId] = useState<string | null>(
    null,
  );
  const [runtimeExportResult, setRuntimeExportResult] =
    useState<RuntimeRunExportResponse | null>(null);
  const [budgetEditor, setBudgetEditor] =
    useState<RuntimeBudgetEditorState | null>(null);
  const [budgetSaving, setBudgetSaving] = useState(false);
  const [taskEventsAvailable, setTaskEventsAvailable] = useState(true);
  const [runtimeRunsAvailable, setRuntimeRunsAvailable] = useState(true);
  const [taskEventFilter, setTaskEventFilter] =
    useState<TaskEventFilter>("all");
  const [taskEventStatusFilter, setTaskEventStatusFilter] =
    useState<TaskEventStatusFilter>("all");
  const [runtimeSourceFilter, setRuntimeSourceFilter] =
    useState<RuntimeSourceFilter>("all");
  const [runtimeStatusFilter, setRuntimeStatusFilter] =
    useState<RuntimeStatusFilter>("all");
  const [runtimeViewMode, setRuntimeViewMode] =
    useState<RuntimeViewMode>("list");
  const dataLoadGateRef = useRef(createLatestRequestGate());
  const runtimeDetailGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());
  const requestedRunId = String(searchParams.get("run") || "").trim();
  const updateRunQuery = useCallback(
    (runId?: string | null) => {
      const next = new URLSearchParams(searchParams);
      if (runId) {
        next.set("run", runId);
      } else {
        next.delete("run");
      }
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const loadData = useCallback(async () => {
    const ownsState = dataLoadGateRef.current.begin();
    setRefreshing(true);
    try {
      const [
        chatList,
        jobs,
        heartbeatConfig,
        turnEventsResult,
        taskRunsResult,
        runtimeRunsResult,
        officeTemplatesResult,
      ] = await Promise.all([
        chatApi.listChats(),
        cronJobApi.listCronJobs(),
        heartbeatApi.getHeartbeatConfig().catch(() => null),
        debugApi.getTurnEvents(24).catch(() => null),
        capabilityApi.listTaskRuns().catch(() => []),
        runtimeApi.listRuns({ limit: 32 }).catch(() => null),
        officeDocumentApi.listOfficeTaskTemplates().catch(() => null),
      ]);
      if (!ownsState()) return;
      setChats(Array.isArray(chatList) ? chatList : []);
      setCronJobs(Array.isArray(jobs) ? jobs : []);
      setTaskRuns(Array.isArray(taskRunsResult) ? taskRunsResult : []);
      setOfficeTemplates(officeTemplatesResult?.templates || []);
      setHeartbeatEnabled(Boolean(heartbeatConfig?.enabled));

      if (runtimeRunsResult?.items) {
        const nextRuntimeRuns = runtimeRunsResult.items;
        setRuntimeRuns(nextRuntimeRuns);
        setSelectedRun((current) => {
          if (!current) return current;
          return (
            nextRuntimeRuns.find((run) => run.id === current.id) || current
          );
        });
        setRuntimeRunsAvailable(true);
      } else {
        setRuntimeRuns([]);
        setRuntimeRunsAvailable(false);
      }

      if (turnEventsResult?.items) {
        setTaskEvents(turnEventsResult.items.filter(isTaskLedgerEvent));
        setTaskEventsAvailable(true);
      } else {
        setTaskEvents([]);
        setTaskEventsAvailable(false);
      }

      const statePairs = await Promise.all(
        (Array.isArray(jobs) ? jobs : []).map(async (job) => {
          try {
            const state = (await cronJobApi.getCronJobState(
              job.id,
            )) as CronJobStateView;
            return [job.id, state] as const;
          } catch {
            return [job.id, null] as const;
          }
        }),
      );
      if (!ownsState()) return;
      setCronStates(
        Object.fromEntries(
          statePairs.filter(
            (item): item is readonly [string, CronJobStateView] =>
              Boolean(item[1]),
          ),
        ),
      );
    } finally {
      if (ownsState()) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  const enableRecommendedHeartbeat = useCallback(async () => {
    const ownsState = mutationScopeRef.current.capture();
    setHeartbeatSaving(true);
    try {
      await heartbeatApi.updateHeartbeatConfig(RECOMMENDED_HEARTBEAT_CONFIG);
      if (!ownsState()) return;
      setHeartbeatEnabled(true);
      message.success("已启用推荐心跳配置。");
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to enable heartbeat from task center:", error);
      message.error("启用心跳失败");
    } finally {
      if (ownsState()) setHeartbeatSaving(false);
    }
  }, [message]);

  const handleStaleRuntimeControl = useCallback(
    async (error: unknown) => {
      if (!isStaleRuntimeAttemptError(error)) return false;
      message.warning("运行状态已变化，已刷新最新记录。");
      await loadData();
      return true;
    },
    [loadData, message],
  );

  const interruptChatTurn = useCallback(
    async (chat: ChatSpec) => {
      const ownsState = mutationScopeRef.current.capture();
      const pendingTurn = parsePendingTurn(chat);
      const turnId =
        pendingTurn?.run_id || pendingTurn?.last_turn_run_id || chat.id;
      try {
        await chatApi.interruptTurn(chat.id, turnId);
        if (!ownsState()) return;
        message.success("已请求中断当前运行。");
        await loadData();
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to interrupt chat turn:", error);
        message.error("中断失败");
      }
    },
    [loadData, message],
  );

  const retryChatTurn = useCallback(
    async (chat: ChatSpec) => {
      const ownsState = mutationScopeRef.current.capture();
      const pendingTurn = parsePendingTurn(chat);
      const turnId =
        pendingTurn?.run_id || pendingTurn?.last_turn_run_id || chat.id;
      try {
        const result = await chatApi.retryTurn(chat.id, turnId);
        if (!ownsState()) return;
        if (result.text) {
          saveRetryDraft(chat.id, result.text);
          message.success("已准备好重试内容，打开会话后可重新发送。");
        } else {
          message.warning("当前任务没有可自动重试的输入。");
        }
        navigate(`/chat/${chat.id}`);
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to retry chat turn:", error);
        message.error("重试失败");
      }
    },
    [message, navigate],
  );

  const openRuntimeRun = useCallback(
    async (run: RuntimeRunSummary, syncQuery = true) => {
      const ownsState = runtimeDetailGateRef.current.begin();
      if (syncQuery) {
        updateRunQuery(run.id);
      }
      setSelectedRun(run);
      setRuntimeExportResult((current) =>
        current?.run_id === run.id ? current : null,
      );
      setRuntimeDrawerOpen(true);
      setRuntimeEventsLoading(true);
      setRuntimeEvents([]);
      try {
        const response = await runtimeApi.getRunEvents(run.id);
        if (ownsState()) setRuntimeEvents(response.events || []);
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to load runtime run events:", error);
        message.error("运行详情加载失败");
      } finally {
        if (ownsState()) setRuntimeEventsLoading(false);
      }
    },
    [message, updateRunQuery],
  );

  const interruptRuntimeRun = useCallback(
    async (run: RuntimeRunSummary) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        const result = await runtimeApi.interruptRun(
          run.id,
          runtimeRunControlRequest(run),
        );
        if (!ownsState()) return;
        if (result.stopped || result.accepted) {
          message.success("已请求中断运行。");
        } else {
          message.warning("未找到可中断的活动运行。");
        }
        await loadData();
      } catch (error) {
        if (!ownsState()) return;
        if (await handleStaleRuntimeControl(error)) return;
        console.error("Failed to interrupt runtime run:", error);
        message.error("中断失败");
      }
    },
    [handleStaleRuntimeControl, loadData, message],
  );

  const pauseRuntimeRun = useCallback(
    async (run: RuntimeRunSummary) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        const result = await runtimeApi.pauseRun(
          run.id,
          runtimeRunControlRequest(run),
        );
        if (!ownsState()) return;
        if (result.paused) {
          message.success("已暂停浏览器运行。");
        } else {
          message.warning("当前浏览器运行没有进入暂停状态。");
        }
        await loadData();
      } catch (error) {
        if (!ownsState()) return;
        if (await handleStaleRuntimeControl(error)) return;
        console.error("Failed to pause runtime run:", error);
        message.error("暂停失败");
      }
    },
    [handleStaleRuntimeControl, loadData, message],
  );

  const resumeRuntimeRun = useCallback(
    async (run: RuntimeRunSummary) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        const result = await runtimeApi.resumeRun(
          run.id,
          runtimeRunControlRequest(run),
        );
        if (!ownsState()) return;
        if (result.resumed) {
          message.success("已恢复浏览器运行。");
        } else {
          message.warning("当前浏览器运行没有进入恢复状态。");
        }
        await loadData();
      } catch (error) {
        if (!ownsState()) return;
        if (await handleStaleRuntimeControl(error)) return;
        console.error("Failed to resume runtime run:", error);
        message.error("恢复失败");
      }
    },
    [handleStaleRuntimeControl, loadData, message],
  );

  const resolveRuntimeEffect = useCallback(
    async (
      run: RuntimeRunSummary,
      effect: Record<string, unknown>,
      resolution: RuntimeEffectResolution,
    ) => {
      const ownsState = mutationScopeRef.current.capture();
      const effectId = runtimeRecordText(effect, "effect_id");
      if (!effectId) {
        message.warning("这条副作用记录缺少 effect_id，暂时不能处理。");
        return;
      }
      if (resolution !== "mark_ambiguous") {
        const toolName = runtimeRecordText(effect, "tool_name") || "外部操作";
        const currentStatus =
          runtimeEffectStatusLabel(runtimeRecordText(effect, "status")) ||
          "未知";
        const confirmed = await new Promise<boolean>((resolve) => {
          Modal.confirm({
            transitionName: "",
            maskTransitionName: "",
            title:
              resolution === "safe_to_retry"
                ? "确认允许再次执行这个外部操作？"
                : resolution === "human_resolved"
                  ? "确认该外部操作已经人工处理？"
                  : "确认外部操作的实际结果？",
            content: (
              <div style={{ display: "grid", gap: 8 }}>
                <div>
                  操作：<strong>{toolName}</strong>
                </div>
                <div>
                  记录：<code>{runtimeShortId(effectId)}</code> · 当前状态：
                  {currentStatus}
                </div>
                <div>
                  {resolution === "safe_to_retry"
                    ? "只有在你已经核对外部系统、确认上一次没有成功时才能继续。误判可能导致重复发送、重复发布或重复写入。"
                    : resolution === "human_resolved"
                      ? "确认后系统会把它视为已处理，不会再主动恢复这一步。"
                      : "请先到对应外部系统核对实际结果，再确认本地记录。"}
                </div>
              </div>
            ),
            okText:
              resolution === "safe_to_retry"
                ? "我已核对，允许重试"
                : "确认并记录",
            cancelText: "返回核对",
            okButtonProps:
              resolution === "safe_to_retry" ? { danger: true } : undefined,
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!confirmed) return;
      }
      try {
        const result = await runtimeApi.resolveRunEffect(
          run.id,
          effectId,
          runtimeEffectResolutionBody(run, effect, resolution),
        );
        if (!ownsState()) return;
        if (result.resolved) {
          message.success("副作用处理状态已记录。");
        } else {
          message.warning("副作用处理状态没有变化。");
        }
        await loadData();
      } catch (error) {
        if (!ownsState()) return;
        if (await handleStaleRuntimeControl(error)) return;
        console.error("Failed to resolve runtime effect:", error);
        message.error("副作用处理失败");
      }
    },
    [handleStaleRuntimeControl, loadData, message],
  );

  const repairRuntimeEffect = useCallback(
    async (
      run: RuntimeRunSummary,
      effect: Record<string, unknown>,
      action: RuntimeEffectRepairAction,
    ) => {
      const ownsState = mutationScopeRef.current.capture();
      const effectId = runtimeRecordText(effect, "effect_id");
      if (!effectId) {
        message.warning("这条副作用记录缺少 effect_id，暂时不能修复。");
        return;
      }
      try {
        const result = await runtimeApi.repairRunEffect(
          run.id,
          effectId,
          runtimeEffectRepairBody(run, effect, action),
        );
        if (!ownsState()) return;
        if (result.repaired) {
          message.success("运行修复状态已记录。");
        } else {
          message.warning("运行修复状态没有变化。");
        }
        await loadData();
      } catch (error) {
        if (!ownsState()) return;
        if (await handleStaleRuntimeControl(error)) return;
        console.error("Failed to repair runtime effect:", error);
        message.error("运行修复失败");
      }
    },
    [handleStaleRuntimeControl, loadData, message],
  );

  const archiveRuntimeRun = useCallback(
    async (run: RuntimeRunSummary) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        const result = await runtimeApi.archiveRun(
          run.id,
          runtimeRunControlRequest(run),
        );
        if (!ownsState()) return;
        if (result.archived) {
          message.success("运行归档已记录。");
        } else {
          message.warning("运行归档状态没有变化。");
        }
        await loadData();
      } catch (error) {
        if (!ownsState()) return;
        if (await handleStaleRuntimeControl(error)) return;
        console.error("Failed to archive runtime run:", error);
        message.error("归档失败");
      }
    },
    [handleStaleRuntimeControl, loadData, message],
  );

  const exportRuntimeRun = useCallback(
    async (run: RuntimeRunSummary) => {
      const ownsState = mutationScopeRef.current.capture();
      setRuntimeExportingId(run.id);
      setRuntimeExportResult(null);
      try {
        const result = await runtimeApi.exportRun(run.id, {
          ...runtimeRunControlRequest(run),
          approved: true,
          reason: "Task Center 导出运行审计包",
          exported_by: "task-center",
          version_info: { client: "task-center" },
        });
        if (!ownsState()) return;
        setRuntimeExportResult(result);
        message.success("运行导出包已生成。");
      } catch (error) {
        if (!ownsState()) return;
        if (await handleStaleRuntimeControl(error)) return;
        console.error("Failed to export runtime run:", error);
        message.error("导出失败");
      } finally {
        if (ownsState()) setRuntimeExportingId(null);
      }
    },
    [handleStaleRuntimeControl, message],
  );

  const openRuntimeBudgetEditor = useCallback((run: RuntimeRunSummary) => {
    setBudgetEditor({
      run,
      budget: runtimeEditableBudget(run.budget),
      reason: "人工调整运行预算",
    });
  }, []);

  const updateRuntimeBudgetDraft = useCallback(
    (key: RuntimeBudgetNumberKey, value: number | string | null) => {
      setBudgetEditor((current) =>
        current
          ? {
              ...current,
              budget: {
                ...current.budget,
                [key]: normalizeBudgetNumber(value),
              },
            }
          : current,
      );
    },
    [],
  );

  const saveRuntimeBudget = useCallback(async () => {
    if (!budgetEditor) return;
    const ownsState = mutationScopeRef.current.capture();
    const reason = budgetEditor.reason.trim();
    if (!reason) {
      message.warning("请填写调整原因。");
      return;
    }
    const run = budgetEditor.run;
    const budget: RuntimeBudget = {
      ...budgetEditor.budget,
      deadline_at: normalizeBudgetDeadline(budgetEditor.budget.deadline_at),
    };
    setBudgetSaving(true);
    try {
      const result = await runtimeApi.updateRunBudget(run.id, {
        ...runtimeRunControlRequest(run),
        budget,
        changed_by: "user",
        reason,
      });
      if (!ownsState()) return;
      if (result.changed) {
        message.success("运行预算已更新。");
        setSelectedRun((current) =>
          current?.id === run.id
            ? {
                ...current,
                revision: result.revision ?? current.revision,
                status: (result.status as RuntimeRunStatus) ?? current.status,
                authority_status: result.status ?? current.authority_status,
                budget: result.budget as Record<string, unknown>,
              }
            : current,
        );
      } else {
        message.warning("运行预算没有变化。");
      }
      setBudgetEditor(null);
      await loadData();
    } catch (error) {
      if (!ownsState()) return;
      if (await handleStaleRuntimeControl(error)) {
        setBudgetEditor(null);
        return;
      }
      console.error("Failed to update runtime budget:", error);
      message.error("保存预算失败");
    } finally {
      if (ownsState()) setBudgetSaving(false);
    }
  }, [budgetEditor, handleStaleRuntimeControl, loadData, message]);

  const retryRuntimeRun = useCallback(
    async (run: RuntimeRunSummary) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        const result = await runtimeApi.retryRun(
          run.id,
          runtimeRunControlRequest(run),
        );
        if (!ownsState()) return;
        if (result.chat_id && result.text) {
          saveRetryDraft(result.chat_id, result.text);
        }
        if (result.chat_id) {
          navigate(`/chat/${result.chat_id}`);
        }
        if (result.text) {
          message.success("已准备好重试内容，打开会话后可重新发送。");
        } else {
          message.warning("当前运行没有可自动重试的输入。");
        }
      } catch (error) {
        if (!ownsState()) return;
        if (await handleStaleRuntimeControl(error)) return;
        console.error("Failed to retry runtime run:", error);
        message.error("重试失败");
      }
    },
    [handleStaleRuntimeControl, message, navigate],
  );

  const continueRuntimeRun = useCallback(
    async (run: RuntimeRunSummary) => {
      const ownsState = mutationScopeRef.current.capture();
      const chatId = runtimeRunChatId(run);
      if (!chatId) {
        message.warning("这个卡住的运行暂时没有可续跑的会话。");
        return;
      }
      try {
        const result = await runtimeApi.continueRun(run.id, {
          ...runtimeRunControlRequest(run),
          chat_id: chatId,
          session_id: run.session_id ?? null,
          after_seq: runtimeRunLatestSeq(run),
          stage_label: runtimeRunStageLabel(run),
          workspace_dir: run.workspace_dir ?? null,
        });
        if (!ownsState()) return;
        const targetChatId = result.chat_id || chatId;
        if (result.reconnected) {
          message.success("后端任务仍在运行，已重新接上运行态。");
          navigate(`/chat/${encodeURIComponent(targetChatId)}`);
          return;
        }
        if (result.reason === "ambiguous_effect_requires_resolution") {
          message.warning("请先在任务详情中处理未决工具副作用，再续跑。");
          return;
        }
        if (result.text) {
          saveRetryDraft(targetChatId, result.text);
          message.success("已准备好续跑内容，打开会话后可继续发送。");
        } else {
          message.warning("当前运行没有生成可续跑的内容。");
        }
        navigate(`/chat/${encodeURIComponent(targetChatId)}`);
      } catch (error) {
        if (!ownsState()) return;
        if (await handleStaleRuntimeControl(error)) return;
        console.error("Failed to continue runtime run:", error);
        message.error("续跑失败");
      }
    },
    [handleStaleRuntimeControl, message, navigate],
  );

  const repairRuntimeRun = useCallback(
    async (
      run: RuntimeRunSummary,
      diagnosis: RuntimeFailureDiagnosis,
      event?: RuntimeEvent,
    ) => {
      const targetChatId = runtimeRunChatId(run);
      const repairText = buildRuntimeRepairPrompt({
        diagnosis,
        runId: run.id,
        stageLabel: event
          ? runtimeEventTitle(event)
          : runtimeRunStageLabel(run),
        eventDetail: event ? runtimeEventDetail(event) : run.last_event_summary,
        workspaceDir: run.workspace_dir ?? null,
        previewHarness: previewHarnessReportFromPayload(event?.payload),
      });

      if (targetChatId) {
        saveRetryDraft(targetChatId, repairText);
        message.success("已生成修复提示，打开会话后可发送。");
        navigate(`/chat/${encodeURIComponent(targetChatId)}`);
        return;
      }

      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(repairText);
          message.warning("当前运行没有关联会话，已复制修复提示。");
          return;
        } catch (error) {
          console.error("Failed to copy runtime repair prompt:", error);
        }
      }
      message.warning("当前运行没有关联会话，暂时无法自动回填修复提示。");
    },
    [message, navigate],
  );

  const startOfficeTemplate = useCallback(
    async (template: OfficeTaskTemplate) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        const chat = await chatApi.createChat({
          name: template.title,
          channel: "console",
        });
        if (!ownsState()) return;
        saveRetryDraft(chat.id, template.prompt);
        message.success("已创建 Office 文档任务草稿。");
        navigate(`/chat/${chat.id}`);
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to create Office template chat:", error);
        message.error("创建 Office 文档任务失败");
      }
    },
    [message, navigate],
  );

  const createOfficeScheduledTask = useCallback(
    (template: OfficeTaskTemplate) => {
      const ownsState = mutationScopeRef.current.capture();
      Modal.confirm({
        title: `设为定时：${template.title}`,
        content:
          "会创建一个已启用的默认定时任务，可在“定时”页面继续调整时间、投递方式和执行内容。",
        okText: "创建定时",
        cancelText: "取消",
        onOk: async () => {
          if (!ownsState()) return;
          try {
            await cronJobApi.createCronJob(buildOfficeCronJobSpec(template));
            if (!ownsState()) return;
            message.success("已创建 Office 定时任务");
            await loadData();
          } catch (error) {
            if (!ownsState()) return;
            console.error("Failed to create Office scheduled task:", error);
            message.error(
              error instanceof Error
                ? error.message
                : "创建 Office 定时任务失败",
            );
          }
        },
      });
    },
    [loadData, message],
  );

  useEffect(() => {
    dataLoadGateRef.current.invalidate();
    runtimeDetailGateRef.current.invalidate();
    setLoading(true);
    setRefreshing(false);
    setHeartbeatSaving(false);
    setHeartbeatEnabled(false);
    setChats([]);
    setCronJobs([]);
    setCronStates({});
    setTaskEvents([]);
    setTaskRuns([]);
    setOfficeTemplates([]);
    setRuntimeRuns([]);
    setRuntimeEvents([]);
    setSelectedRun(null);
    setRuntimeDrawerOpen(false);
    setRuntimeEventsLoading(false);
    setRuntimeExportingId(null);
    setRuntimeExportResult(null);
    setBudgetEditor(null);
    setBudgetSaving(false);
    void loadData();
    return () => {
      dataLoadGateRef.current.invalidate();
      runtimeDetailGateRef.current.invalidate();
      mutationScopeRef.current.invalidate();
    };
  }, [loadData, selectedAgent]);

  useEffect(() => {
    const matched = selectRequestedRuntimeRun({
      requestedRunId,
      runtimeRuns,
      runtimeRunsAvailable,
      loading,
      runtimeDrawerOpen,
      selectedRunId: selectedRun?.id,
    });
    if (matched) {
      void openRuntimeRun(matched, false);
    }
  }, [
    loading,
    openRuntimeRun,
    requestedRunId,
    runtimeDrawerOpen,
    runtimeRuns,
    runtimeRunsAvailable,
    selectedRun?.id,
  ]);

  const runningChats = useMemo(
    () =>
      chats
        .filter((chat) => chat.status === "running" || parsePendingTurn(chat))
        .sort(
          (a, b) =>
            dayjs(b.updated_at || b.created_at || 0).valueOf() -
            dayjs(a.updated_at || a.created_at || 0).valueOf(),
        ),
    [chats],
  );

  const recentActivities = useMemo(() => {
    const items: UnifiedActivityItem[] = [];
    runningChats.forEach((chat) => {
      const activity = getChatActivity(chat);
      if (activity) items.push(activity);
    });
    cronJobs.forEach((job) => {
      const activity = getCronActivity(job, cronStates[job.id] || null);
      if (activity) items.push(activity);
    });
    return items.sort((a, b) => b.timestamp - a.timestamp).slice(0, 8);
  }, [cronJobs, cronStates, runningChats]);

  const enabledCronJobs = useMemo(
    () => cronJobs.filter((job) => job.enabled),
    [cronJobs],
  );

  const recentTaskEvents = useMemo(
    () =>
      taskEvents
        .filter((item) => matchesTaskEventFilter(item, taskEventFilter))
        .filter((item) =>
          matchesTaskEventStatusFilter(item, taskEventStatusFilter),
        )
        .slice(0, 6),
    [taskEventFilter, taskEventStatusFilter, taskEvents],
  );

  const filteredRuntimeRuns = useMemo(
    () =>
      runtimeRuns
        .filter(
          (run) =>
            runtimeSourceFilter === "all" || run.source === runtimeSourceFilter,
        )
        .filter(
          (run) =>
            runtimeStatusFilter === "all" || run.status === runtimeStatusFilter,
        ),
    [runtimeRuns, runtimeSourceFilter, runtimeStatusFilter],
  );
  const visibleRuntimeRuns =
    runtimeViewMode === "board"
      ? filteredRuntimeRuns.slice(0, 50)
      : filteredRuntimeRuns.slice(0, 10);

  const runtimeTimelineGroups = useMemo(
    () => groupRuntimeEvents(runtimeEvents),
    [runtimeEvents],
  );
  const runtimeNotifications = useMemo(
    () => runtimeNotificationEvents(runtimeEvents),
    [runtimeEvents],
  );
  const collaborationParticipants = useMemo(
    () =>
      selectedRun
        ? deriveCollaborationParticipants(runtimeEvents, selectedRun.id)
        : [],
    [runtimeEvents, selectedRun],
  );
  const hostedCollaboration = useMemo(
    () => deriveHostedCollaborationProjection(runtimeEvents),
    [runtimeEvents],
  );
  const runtimeFailureDiagnoses = useMemo(
    () =>
      runtimeEvents
        .map(runtimeEventFailureDiagnosis)
        .filter((item): item is RuntimeFailureDiagnosis => Boolean(item)),
    [runtimeEvents],
  );
  const primaryRuntimeDiagnosis =
    runtimeFailureDiagnoses[runtimeFailureDiagnoses.length - 1] || null;

  const copyRuntimeDebug = useCallback(async () => {
    if (!selectedRun) return;
    try {
      await navigator.clipboard.writeText(
        JSON.stringify(
          {
            run: selectedRun,
            events: runtimeEvents,
          },
          null,
          2,
        ),
      );
      message.success("已复制调试信息");
    } catch (error) {
      console.error("Failed to copy runtime debug info:", error);
      message.error("复制失败");
    }
  }, [message, runtimeEvents, selectedRun]);

  const renderStatusTone = (status: string) => {
    if (
      ["dispatched", "acked", "running", "processing", "pending"].includes(
        status,
      )
    ) {
      return "info";
    }
    if (["succeeded", "completed", "success", "finished"].includes(status)) {
      return "success";
    }
    if (
      [
        "failed",
        "error",
        "cancelled",
        "interrupted",
        "timed_out",
        "stalled",
        "blocked",
      ].includes(status)
    ) {
      return "danger";
    }
    return "muted";
  };

  const canGenerateDraftFromTaskRun = (taskRun: TaskRun) =>
    ![
      "dispatched",
      "acked",
      "running",
      "blocked",
      "stalled",
      "timed_out",
    ].includes(taskRun.status);

  const evaluationLabel = (taskRun: TaskRun) => {
    const conclusion = taskRun.evaluation_summary?.conclusion;
    if (conclusion === "pass") return "PASS";
    if (conclusion === "blocked") return "BLOCKED";
    if (conclusion === "needs_revision") return "NEEDS_REVISION";
    return null;
  };

  return (
    <CapabilityPage
      section="工作"
      title="任务中心"
      description="把聊天执行、自动运行和后台任务集中在一个视图里，方便查看最近活动和当前状态。"
      actions={
        <Button
          type="default"
          icon={<ReloadOutlined spin={refreshing} />}
          onClick={() => void loadData()}
        >
          刷新
        </Button>
      }
      stats={[
        { label: "最近活动", value: recentActivities.length },
        {
          label: "运行记录",
          value: runtimeRunsAvailable ? runtimeRuns.length : "未接入",
          tone: runtimeRunsAvailable ? "default" : "muted",
        },
        { label: "进行中聊天", value: runningChats.length, tone: "info" },
        { label: "启用定时", value: enabledCronJobs.length, tone: "success" },
        { label: "任务记录", value: taskRuns.length, tone: "info" },
      ]}
      className={styles.taskCenterPage}
    >
      {loading ? (
        <div className={styles.loadingState}>
          <Spin />
        </div>
      ) : (
        <div className={styles.pageBody}>
          {runningChats.length > 0 && !heartbeatEnabled ? (
            <Alert
              showIcon
              type="warning"
              className={styles.heartbeatPrompt}
              message={`当前有 ${runningChats.length} 个进行中的聊天任务`}
              description="建议开启心跳，让系统每隔一段时间在后台轻量跟进一次。推荐配置是 6 小时一次、静默运行、仅在白天生效。"
              action={
                <div className={styles.heartbeatPromptActions}>
                  <Button
                    type="primary"
                    loading={heartbeatSaving}
                    onClick={() => void enableRecommendedHeartbeat()}
                  >
                    一键开启
                  </Button>
                  <Button type="default" onClick={() => navigate("/heartbeat")}>
                    去详细配置
                  </Button>
                </div>
              }
            />
          ) : null}

          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <div>
                <h2>Office 自动化</h2>
                <p>把常见文档交付变成可复用任务：周报、销售表和汇报 PPT。</p>
              </div>
              <Button
                type="default"
                onClick={() => navigate("/office-documents")}
              >
                Office 文档能力
              </Button>
            </div>
            {officeTemplates.length === 0 ? (
              <Empty description="当前没有 Office 文档模板" />
            ) : (
              <div className={styles.cardGrid}>
                {officeTemplates.map((template) => (
                  <CapabilityCard
                    key={template.id}
                    icon={
                      OFFICE_TEMPLATE_ICONS[template.id] || (
                        <FileText size={18} />
                      )
                    }
                    title={template.title}
                    subtitle="Office 文档任务"
                    status="可创建"
                    statusTone="info"
                    description={template.description}
                    chips={
                      <>
                        <CapabilityChip tone="default">
                          Word / Excel / PPT
                        </CapabilityChip>
                        <CapabilityChip tone="muted">可定时运行</CapabilityChip>
                      </>
                    }
                    footer={
                      <div className={styles.cardActions}>
                        <Button
                          type="text"
                          size="small"
                          onClick={() => void startOfficeTemplate(template)}
                        >
                          创建草稿
                        </Button>
                        <Button
                          type="text"
                          size="small"
                          onClick={() => createOfficeScheduledTask(template)}
                        >
                          设为定时
                        </Button>
                      </div>
                    }
                  />
                ))}
              </div>
            )}
          </section>

          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <div>
                <h2>运行控制台</h2>
                <p>按一次运行聚合聊天、工具、审批、定时和心跳事件。</p>
              </div>
              <div className={styles.taskEventFilters}>
                <Segmented
                  size="middle"
                  value={runtimeViewMode}
                  onChange={(value) =>
                    setRuntimeViewMode(value as RuntimeViewMode)
                  }
                  options={[
                    { label: "列表", value: "list" },
                    { label: "看板", value: "board" },
                  ]}
                  className={styles.taskEventFilter}
                  aria-label="运行记录视图"
                />
                <Segmented
                  size="middle"
                  value={runtimeSourceFilter}
                  onChange={(value) =>
                    setRuntimeSourceFilter(value as RuntimeSourceFilter)
                  }
                  options={[
                    { label: "全部", value: "all" },
                    { label: "聊天", value: "chat" },
                    { label: "能力", value: "capability" },
                    { label: "流水线", value: "pipeline" },
                    { label: "浏览器", value: "browser" },
                    { label: "定时", value: "cron" },
                    { label: "提醒", value: "reminder" },
                    { label: "心跳", value: "heartbeat" },
                    { label: "技能", value: "skills" },
                  ]}
                  className={styles.taskEventFilter}
                />
                <Segmented
                  size="middle"
                  value={runtimeStatusFilter}
                  onChange={(value) =>
                    setRuntimeStatusFilter(value as RuntimeStatusFilter)
                  }
                  options={[
                    { label: "全部状态", value: "all" },
                    { label: "运行中", value: "running" },
                    { label: "已完成", value: "completed" },
                    { label: "异常", value: "failed" },
                    { label: "卡住", value: "stalled" },
                    { label: "中断", value: "interrupted" },
                  ]}
                  className={styles.taskEventFilter}
                />
              </div>
            </div>
            {!runtimeRunsAvailable ? (
              <Empty description="当前环境暂未返回运行记录" />
            ) : visibleRuntimeRuns.length === 0 ? (
              <Empty description="当前筛选下没有运行记录" />
            ) : runtimeViewMode === "board" ? (
              <RuntimeRunBoard
                runs={visibleRuntimeRuns}
                onOpen={(run) => void openRuntimeRun(run)}
              />
            ) : (
              <div className={styles.runtimeList}>
                {visibleRuntimeRuns.map((run) => (
                  <div key={run.id} className={styles.runtimeRunItem}>
                    <button
                      type="button"
                      className={styles.runtimeRunMain}
                      onClick={() => void openRuntimeRun(run)}
                    >
                      <span className={styles.runtimeRunTitle}>
                        <strong>{run.title || "未命名运行"}</strong>
                        <CapabilityChip tone="muted">
                          {runtimeSourceLabel(run.source)}
                        </CapabilityChip>
                      </span>
                      <span className={styles.runtimeRunSummary}>
                        {run.summary || run.last_event_summary || "暂无摘要"}
                      </span>
                    </button>
                    <div className={styles.runtimeRunMeta}>
                      <CapabilityChip tone={renderStatusTone(run.status)}>
                        {runtimeStatusLabel(run.status)}
                      </CapabilityChip>
                      <span>{formatTime(run.started_at)}</span>
                      <span>{formatDuration(run.duration_ms)}</span>
                    </div>
                    <div className={styles.runtimeRunActions}>
                      <Button
                        type="text"
                        size="small"
                        onClick={() => void openRuntimeRun(run)}
                      >
                        详情
                      </Button>
                      {canPauseRuntimeRun(run) ? (
                        <Button
                          type="text"
                          size="small"
                          onClick={() => void pauseRuntimeRun(run)}
                        >
                          暂停
                        </Button>
                      ) : null}
                      {canResumeRuntimeRun(run) ? (
                        <Button
                          type="text"
                          size="small"
                          onClick={() => void resumeRuntimeRun(run)}
                        >
                          恢复
                        </Button>
                      ) : null}
                      {run.can_interrupt ? (
                        <Button
                          type="text"
                          size="small"
                          danger
                          onClick={() => void interruptRuntimeRun(run)}
                        >
                          中断
                        </Button>
                      ) : isStalledRuntimeRun(run) ? (
                        <Button
                          type="text"
                          size="small"
                          onClick={() => void continueRuntimeRun(run)}
                        >
                          续跑
                        </Button>
                      ) : run.can_retry ? (
                        <Button
                          type="text"
                          size="small"
                          onClick={() => void retryRuntimeRun(run)}
                        >
                          重试
                        </Button>
                      ) : null}
                      {canArchiveRuntimeRun(run) ? (
                        <Button
                          type="text"
                          size="small"
                          onClick={() => void archiveRuntimeRun(run)}
                        >
                          归档
                        </Button>
                      ) : null}
                      {canUpdateRuntimeBudget(run) ? (
                        <Button
                          type="text"
                          size="small"
                          onClick={() => openRuntimeBudgetEditor(run)}
                        >
                          编辑预算
                        </Button>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <div>
                <h2>最近活动</h2>
                <p>跨聊天和自动任务的统一时间线。</p>
              </div>
            </div>
            {recentActivities.length === 0 ? (
              <Empty description="还没有可展示的任务活动" />
            ) : (
              <div className={styles.activityList}>
                {recentActivities.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={styles.activityItem}
                    onClick={() => navigate(item.actionHref)}
                  >
                    <span className={styles.activityIcon} data-type={item.type}>
                      {item.type === "chat" ? (
                        <Bot size={16} />
                      ) : (
                        <CalendarOutlined />
                      )}
                    </span>
                    <span className={styles.activityCopy}>
                      <strong>{item.title}</strong>
                      <span>{item.summary}</span>
                    </span>
                    <span className={styles.activityMeta}>
                      <CapabilityChip tone={renderStatusTone(item.status)}>
                        {item.status}
                      </CapabilityChip>
                      <em>{dayjs(item.timestamp).format("MM/DD HH:mm")}</em>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </section>

          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <div>
                <h2>进行中聊天</h2>
                <p>优先显示正在执行或刚刚发起的会话。</p>
              </div>
              <Button type="default" onClick={() => navigate("/chat")}>
                打开聊天
              </Button>
            </div>
            {runningChats.length === 0 ? (
              <Empty description="当前没有进行中的聊天任务" />
            ) : (
              <div className={styles.cardGrid}>
                {runningChats.slice(0, 6).map((chat) => {
                  const pendingTurn = parsePendingTurn(chat);
                  return (
                    <CapabilityCard
                      key={chat.id}
                      icon={<Bot size={18} />}
                      title={chat.name || "新会话"}
                      subtitle={`${chat.channel || "console"} · ${formatTime(
                        chat.updated_at || chat.created_at,
                      )}`}
                      status={
                        chat.status === "running"
                          ? "运行中"
                          : pendingTurn?.status === "stalled"
                            ? "已卡住"
                            : "待处理"
                      }
                      statusTone={renderStatusTone(
                        chat.status === "running"
                          ? "running"
                          : pendingTurn?.status || "idle",
                      )}
                      description={getChatSummary(chat)}
                      chips={
                        <>
                          <CapabilityChip tone="muted">
                            最近更新 {formatRelativeTime(chat.updated_at)}
                          </CapabilityChip>
                          {pendingTurn?.created_at ? (
                            <CapabilityChip tone="default">
                              发起于 {formatTime(pendingTurn.created_at)}
                            </CapabilityChip>
                          ) : null}
                        </>
                      }
                      footer={
                        <div className={styles.cardActions}>
                          <Button
                            type="text"
                            size="small"
                            onClick={() => navigate(`/chat/${chat.id}`)}
                          >
                            打开会话
                          </Button>
                          {chat.status === "running" ? (
                            <Button
                              type="text"
                              size="small"
                              danger
                              onClick={() => void interruptChatTurn(chat)}
                            >
                              中断
                            </Button>
                          ) : (
                            <Button
                              type="text"
                              size="small"
                              onClick={() => void retryChatTurn(chat)}
                            >
                              重试
                            </Button>
                          )}
                        </div>
                      }
                    />
                  );
                })}
              </div>
            )}
          </section>

          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <div>
                <h2>自动任务</h2>
                <p>查看定时任务是否启用、上次执行结果和下次运行时间。</p>
              </div>
              <Button type="default" onClick={() => navigate("/cron-jobs")}>
                打开定时
              </Button>
            </div>
            {cronJobs.length === 0 ? (
              <Empty description="还没有定时任务" />
            ) : (
              <div className={styles.cardGrid}>
                {cronJobs.slice(0, 6).map((job) => {
                  const state = cronStates[job.id];
                  const cronStatus = normalizeCronStatus(state?.last_status);
                  return (
                    <CapabilityCard
                      key={job.id}
                      icon={<ClockCircleOutlined />}
                      title={job.name}
                      subtitle={
                        job.task_type === "text"
                          ? "固定文本"
                          : job.task_type === "capability"
                            ? "能力任务"
                            : "智能体任务"
                      }
                      status={job.enabled ? "已启用" : "已暂停"}
                      statusTone={job.enabled ? "success" : "muted"}
                      description={
                        state?.last_error
                          ? textPreview(state.last_error, 96)
                          : job.task_type === "text"
                            ? textPreview(job.text || "")
                            : job.task_type === "capability"
                              ? "按计划运行已启用的能力"
                              : "按计划触发并投递到指定会话"
                      }
                      chips={
                        <>
                          <CapabilityChip tone={renderStatusTone(cronStatus)}>
                            最近结果 {cronStatus}
                          </CapabilityChip>
                          <CapabilityChip tone="default">
                            下次 {formatTime(state?.next_run_at)}
                          </CapabilityChip>
                          <CapabilityChip tone="muted">
                            上次 {formatTime(state?.last_run_at)}
                          </CapabilityChip>
                        </>
                      }
                      footer={
                        <Button
                          type="text"
                          size="small"
                          onClick={() => navigate("/cron-jobs")}
                        >
                          查看详情
                        </Button>
                      }
                    />
                  );
                })}
              </div>
            )}
          </section>

          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <div>
                <h2>可沉淀任务</h2>
                <p>从已完成任务生成能力草稿，进入编辑器复核后再启用。</p>
              </div>
              <Button type="default" onClick={() => navigate("/capabilities")}>
                打开能力中心
              </Button>
            </div>
            {taskRuns.length === 0 ? (
              <Empty description="还没有可沉淀的任务记录" />
            ) : (
              <div className={styles.activityList}>
                {taskRuns.slice(0, 6).map((taskRun) => (
                  <button
                    key={taskRun.id}
                    type="button"
                    className={styles.activityItem}
                    disabled={!canGenerateDraftFromTaskRun(taskRun)}
                    onClick={() =>
                      navigate(
                        `/capabilities?taskRun=${encodeURIComponent(taskRun.id)}`,
                      )
                    }
                  >
                    <span
                      className={styles.activityIcon}
                      data-type={taskRun.source === "image" ? "image" : "chat"}
                    >
                      {taskRun.source === "cron" ? (
                        <CalendarOutlined />
                      ) : taskRun.source === "image" ? (
                        <Sparkles size={16} />
                      ) : (
                        <Bot size={16} />
                      )}
                    </span>
                    <span className={styles.activityCopy}>
                      <strong>{taskRun.title}</strong>
                      <span>
                        {textPreview(taskRun.user_intent || taskRun.source)}
                      </span>
                    </span>
                    <span className={styles.activityMeta}>
                      <CapabilityChip tone={renderStatusTone(taskRun.status)}>
                        {taskRun.status}
                      </CapabilityChip>
                      {evaluationLabel(taskRun) ? (
                        <CapabilityChip
                          tone={
                            taskRun.evaluation_summary?.conclusion === "pass"
                              ? "success"
                              : taskRun.evaluation_summary?.conclusion ===
                                  "blocked"
                                ? "danger"
                                : "warning"
                          }
                        >
                          {evaluationLabel(taskRun)}
                        </CapabilityChip>
                      ) : null}
                      <em>{formatTime(taskRun.started_at)}</em>
                      <strong className={styles.activityAction}>
                        生成能力
                      </strong>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </section>

          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <div>
                <h2>自动运行动态</h2>
                <p>直接来自运行事件账本，能看见定时和心跳刚刚做了什么。</p>
              </div>
              <div className={styles.taskEventFilters}>
                <Segmented
                  size="middle"
                  value={taskEventFilter}
                  onChange={(value) =>
                    setTaskEventFilter(value as TaskEventFilter)
                  }
                  options={[
                    { label: "全部", value: "all" },
                    { label: "仅心跳", value: "heartbeat" },
                    { label: "仅定时", value: "cron" },
                  ]}
                  className={styles.taskEventFilter}
                />
                <Segmented
                  size="middle"
                  value={taskEventStatusFilter}
                  onChange={(value) =>
                    setTaskEventStatusFilter(value as TaskEventStatusFilter)
                  }
                  options={[
                    { label: "全部状态", value: "all" },
                    { label: "仅异常", value: "issues" },
                  ]}
                  className={styles.taskEventFilter}
                />
              </div>
            </div>
            {!taskEventsAvailable ? (
              <Empty description="当前环境暂未返回自动运行事件" />
            ) : recentTaskEvents.length === 0 ? (
              <Empty description="当前筛选下没有自动运行动态" />
            ) : (
              <div className={styles.activityList}>
                {recentTaskEvents.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={styles.activityItem}
                    onClick={() => navigate(getTaskEventHref(item))}
                  >
                    <span className={styles.activityIcon} data-type="cron">
                      {item.kind.startsWith("heartbeat.") ? (
                        <ClockCircleOutlined />
                      ) : (
                        <CalendarOutlined />
                      )}
                    </span>
                    <span className={styles.activityCopy}>
                      <strong>{item.label}</strong>
                      <span>{item.detail}</span>
                    </span>
                    <span className={styles.activityMeta}>
                      <CapabilityChip tone={renderStatusTone(item.status)}>
                        {item.status}
                      </CapabilityChip>
                      <em>{formatRelativeTime(item.ts)}</em>
                      <small className={styles.activitySubmeta}>
                        {workspaceLabel(item.workspace_dir)}
                      </small>
                      <strong className={styles.activityAction}>
                        {getTaskEventActionLabel(item)}
                      </strong>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </section>
        </div>
      )}
      <Modal
        title="调整运行预算"
        open={Boolean(budgetEditor)}
        okText="保存预算"
        cancelText="取消"
        confirmLoading={budgetSaving}
        destroyOnHidden
        onOk={() => void saveRuntimeBudget()}
        onCancel={() => setBudgetEditor(null)}
      >
        {budgetEditor ? (
          <div className={styles.budgetEditorBody}>
            <div className={styles.budgetEditorGrid}>
              <label>
                <span>最大轮数</span>
                <InputNumber
                  aria-label="最大轮数"
                  min={1}
                  value={budgetEditor.budget.max_turns ?? null}
                  onChange={(value) =>
                    updateRuntimeBudgetDraft("max_turns", value)
                  }
                />
              </label>
              <label>
                <span>最大步数</span>
                <InputNumber
                  aria-label="最大步数"
                  min={1}
                  value={budgetEditor.budget.max_steps ?? null}
                  onChange={(value) =>
                    updateRuntimeBudgetDraft("max_steps", value)
                  }
                />
              </label>
              <label>
                <span>最大工具调用</span>
                <InputNumber
                  aria-label="最大工具调用"
                  min={1}
                  value={budgetEditor.budget.max_tool_calls ?? null}
                  onChange={(value) =>
                    updateRuntimeBudgetDraft("max_tool_calls", value)
                  }
                />
              </label>
              <label>
                <span>最长秒数</span>
                <InputNumber
                  aria-label="最长秒数"
                  min={1}
                  value={budgetEditor.budget.max_seconds ?? null}
                  onChange={(value) =>
                    updateRuntimeBudgetDraft("max_seconds", value)
                  }
                />
              </label>
              <label>
                <span>最大 tokens</span>
                <InputNumber
                  aria-label="最大 tokens"
                  min={1}
                  value={budgetEditor.budget.max_tokens ?? null}
                  onChange={(value) =>
                    updateRuntimeBudgetDraft("max_tokens", value)
                  }
                />
              </label>
              <label>
                <span>最大成本点数</span>
                <InputNumber
                  aria-label="最大成本点数"
                  min={0}
                  value={budgetEditor.budget.max_cost_points ?? null}
                  onChange={(value) =>
                    updateRuntimeBudgetDraft("max_cost_points", value)
                  }
                />
              </label>
              <label>
                <span>最大产物字节</span>
                <InputNumber
                  aria-label="最大产物字节"
                  min={1}
                  value={budgetEditor.budget.max_artifact_bytes ?? null}
                  onChange={(value) =>
                    updateRuntimeBudgetDraft("max_artifact_bytes", value)
                  }
                />
              </label>
              <label>
                <span>截止时间</span>
                <Input
                  aria-label="截止时间"
                  value={budgetEditor.budget.deadline_at ?? ""}
                  placeholder="2026-07-17T10:30:00.000Z"
                  onChange={(event) =>
                    setBudgetEditor((current) =>
                      current
                        ? {
                            ...current,
                            budget: {
                              ...current.budget,
                              deadline_at: event.target.value,
                            },
                          }
                        : current,
                    )
                  }
                />
              </label>
            </div>
            <label className={styles.budgetEditorReason}>
              <span>调整原因</span>
              <Input.TextArea
                aria-label="调整原因"
                rows={3}
                value={budgetEditor.reason}
                onChange={(event) =>
                  setBudgetEditor((current) =>
                    current
                      ? { ...current, reason: event.target.value }
                      : current,
                  )
                }
              />
            </label>
          </div>
        ) : null}
      </Modal>
      <Drawer
        title={selectedRun?.title || "运行详情"}
        open={runtimeDrawerOpen}
        width={560}
        onClose={() => {
          setRuntimeDrawerOpen(false);
          setSelectedRun(null);
          updateRunQuery(null);
        }}
      >
        {selectedRun ? (
          <div className={styles.runtimeDrawer}>
            <div className={styles.runtimeDrawerSummary}>
              <CapabilityChip tone={renderStatusTone(selectedRun.status)}>
                {runtimeStatusLabel(selectedRun.status)}
              </CapabilityChip>
              <span>{runtimeSourceLabel(selectedRun.source)}</span>
              <span>
                Attempt{" "}
                {runtimeShortId(selectedRun.attempt_id || selectedRun.id)}
              </span>
              <span>
                权威{" "}
                {runtimeAuthorityStatusLabel(
                  selectedRun.authority_status || selectedRun.status,
                )}
              </span>
              {selectedRun.revision !== null &&
              selectedRun.revision !== undefined ? (
                <span>Rev {selectedRun.revision}</span>
              ) : null}
              <span>{formatRuntimeUsage(selectedRun)}</span>
            </div>
            <div className={styles.runtimeDrawerActions}>
              {runtimeRunChatId(selectedRun) ? (
                <Button
                  type="default"
                  size="small"
                  onClick={() =>
                    navigate(
                      `/chat/${encodeURIComponent(runtimeRunChatId(selectedRun) || "")}`,
                    )
                  }
                >
                  打开会话
                </Button>
              ) : null}
              <Button
                type="default"
                size="small"
                onClick={() => void openRuntimeRun(selectedRun, false)}
              >
                刷新详情
              </Button>
              <Button
                type="default"
                size="small"
                loading={runtimeExportingId === selectedRun.id}
                onClick={() => void exportRuntimeRun(selectedRun)}
              >
                导出审计包
              </Button>
              {canPauseRuntimeRun(selectedRun) ? (
                <Button
                  type="default"
                  size="small"
                  onClick={() => void pauseRuntimeRun(selectedRun)}
                >
                  暂停运行
                </Button>
              ) : null}
              {canResumeRuntimeRun(selectedRun) ? (
                <Button
                  type="default"
                  size="small"
                  onClick={() => void resumeRuntimeRun(selectedRun)}
                >
                  恢复运行
                </Button>
              ) : null}
              {selectedRun.can_interrupt ? (
                <Button
                  type="default"
                  size="small"
                  danger
                  onClick={() => void interruptRuntimeRun(selectedRun)}
                >
                  中断运行
                </Button>
              ) : isStalledRuntimeRun(selectedRun) ? (
                <Button
                  type="default"
                  size="small"
                  onClick={() => void continueRuntimeRun(selectedRun)}
                >
                  续跑这轮
                </Button>
              ) : selectedRun.can_retry ? (
                <Button
                  type="default"
                  size="small"
                  onClick={() => void retryRuntimeRun(selectedRun)}
                >
                  重试这轮
                </Button>
              ) : null}
              {canArchiveRuntimeRun(selectedRun) ? (
                <Button
                  type="default"
                  size="small"
                  onClick={() => void archiveRuntimeRun(selectedRun)}
                >
                  归档运行
                </Button>
              ) : null}
              {canUpdateRuntimeBudget(selectedRun) ? (
                <Button
                  type="default"
                  size="small"
                  onClick={() => openRuntimeBudgetEditor(selectedRun)}
                >
                  编辑预算
                </Button>
              ) : null}
            </div>
            {runtimeExportResult?.run_id === selectedRun.id ? (
              <Alert
                type="success"
                showIcon
                message="导出包已生成"
                description={
                  <div className={styles.runtimeExportSummary}>
                    <span>
                      {runtimeExportRetentionLabel(runtimeExportResult)}
                    </span>
                    {runtimeExportDetailLabel(runtimeExportResult) ? (
                      <em>{runtimeExportDetailLabel(runtimeExportResult)}</em>
                    ) : null}
                  </div>
                }
              />
            ) : null}
            {runtimeNotifications.length ? (
              <div className={styles.runtimeNotificationPanel}>
                <div className={styles.runtimeNotificationHeader}>
                  <strong>运行提醒</strong>
                  <span>{runtimeNotifications.length} 条</span>
                </div>
                <div className={styles.runtimeNotificationList}>
                  {runtimeNotifications.map((event) => {
                    const meta = runtimeNotificationMeta(event);
                    return (
                      <Alert
                        key={runtimeNotificationKey(event)}
                        type={runtimeNotificationAlertType(event)}
                        showIcon
                        message={runtimeNotificationTitle(event)}
                        description={
                          <div className={styles.runtimeNotificationBody}>
                            <span>{runtimeNotificationMessage(event)}</span>
                            {meta ? <em>{meta}</em> : null}
                          </div>
                        }
                      />
                    );
                  })}
                </div>
              </div>
            ) : null}
            {collaborationParticipants.length || hostedCollaboration ? (
              <CollaborationRoom
                parentRun={selectedRun}
                participants={collaborationParticipants}
                hostedSnapshot={hostedCollaboration}
                availableRuns={runtimeRuns}
                onOpenRun={(run) => void openRuntimeRun(run, false)}
                onShowBoard={() => {
                  setRuntimeViewMode("board");
                  setRuntimeDrawerOpen(false);
                  setSelectedRun(null);
                  updateRunQuery(null);
                }}
              />
            ) : null}
            <div className={styles.runtimeSnapshotGrid}>
              <div>
                <span>Transport</span>
                <strong>
                  {selectedRun.transport_status
                    ? runtimeStatusLabel(selectedRun.transport_status)
                    : "未知"}
                </strong>
              </div>
              <div>
                <span>Lease</span>
                <strong>{selectedRun.lease_owner || "暂无"}</strong>
              </div>
              <div>
                <span>Heartbeat</span>
                <strong>{formatTime(selectedRun.heartbeat_at)}</strong>
              </div>
              <div>
                <span>Current Step</span>
                <strong>{runtimeStepLabel(selectedRun.current_step)}</strong>
              </div>
              <div>
                <span>Budget</span>
                <strong>{runtimeBudgetLabel(selectedRun.budget)}</strong>
              </div>
              <div>
                <span>Checkpoint</span>
                <strong>
                  {runtimeCheckpointLabel(selectedRun.checkpoint)}
                </strong>
              </div>
              <div>
                <span>Pending</span>
                <strong>
                  {selectedRun.pending_approval ? "等待审批" : "无待处理审批"}
                  {selectedRun.ambiguous_effect ? " · 副作用待判定" : ""}
                </strong>
              </div>
              <div>
                <span>Artifacts</span>
                <strong>{Number(selectedRun.artifact_count || 0)} 个</strong>
              </div>
              <div>
                <span>Evaluator</span>
                <strong>
                  {runtimeEvaluationLabel(selectedRun.evaluation_summary)}
                </strong>
              </div>
              <div>
                <span>Recovery</span>
                <strong>{runtimeRecoveryLabel(selectedRun.recovery)}</strong>
              </div>
            </div>
            {runtimeRecoveryEffects(selectedRun).length ? (
              <div className={styles.runtimeEffectPanel}>
                <div className={styles.runtimeEffectPanelHeader}>
                  <strong>副作用处理</strong>
                  <span>
                    {runtimeRecoveryEffects(selectedRun).length} 条记录
                  </span>
                </div>
                {runtimeRecoveryEffects(selectedRun).map((effect, index) => {
                  const effectId =
                    runtimeRecordText(effect, "effect_id") ||
                    `effect-${index + 1}`;
                  const status =
                    runtimeRecordText(effect, "status") || "unknown";
                  const revision = runtimeRecordNumber(effect, "revision");
                  const toolName = runtimeRecordText(effect, "tool_name");
                  const canMarkAmbiguous = status === "running";
                  const canResolve =
                    status === "running" ||
                    status === "ambiguous" ||
                    status === "failed";
                  const needsCheckpointRepair = Boolean(
                    effect.checkpoint_rebuild_required,
                  );
                  const needsTerminalRepair = Boolean(
                    effect.terminal_rewrite_required,
                  );
                  return (
                    <div
                      className={styles.runtimeEffectItem}
                      key={`${effectId}-${revision ?? index}`}
                    >
                      <div className={styles.runtimeEffectMeta}>
                        <span>{toolName || "外部副作用"}</span>
                        <strong>{runtimeShortId(effectId)}</strong>
                        <em>
                          {runtimeEffectStatusLabel(status)}
                          {revision !== null ? ` · Rev ${revision}` : ""}
                        </em>
                      </div>
                      <div className={styles.runtimeEffectActions}>
                        {canMarkAmbiguous ? (
                          <Button
                            size="small"
                            type="default"
                            onClick={() =>
                              void resolveRuntimeEffect(
                                selectedRun,
                                effect,
                                "mark_ambiguous",
                              )
                            }
                          >
                            标记待判定
                          </Button>
                        ) : null}
                        {canResolve ? (
                          <>
                            <Button
                              size="small"
                              type="default"
                              onClick={() =>
                                void resolveRuntimeEffect(
                                  selectedRun,
                                  effect,
                                  "probe_committed",
                                )
                              }
                            >
                              探测确认
                            </Button>
                            <Button
                              size="small"
                              type="default"
                              onClick={() =>
                                void resolveRuntimeEffect(
                                  selectedRun,
                                  effect,
                                  "human_resolved",
                                )
                              }
                            >
                              人工已处理
                            </Button>
                            <Button
                              size="small"
                              type="default"
                              onClick={() =>
                                void resolveRuntimeEffect(
                                  selectedRun,
                                  effect,
                                  "safe_to_retry",
                                )
                              }
                            >
                              标记可重试
                            </Button>
                          </>
                        ) : null}
                        {needsCheckpointRepair ? (
                          <Button
                            size="small"
                            type="default"
                            onClick={() =>
                              void repairRuntimeEffect(
                                selectedRun,
                                effect,
                                "rebuild_checkpoint",
                              )
                            }
                          >
                            修复 checkpoint
                          </Button>
                        ) : null}
                        {needsTerminalRepair ? (
                          <Button
                            size="small"
                            type="default"
                            onClick={() =>
                              void repairRuntimeEffect(
                                selectedRun,
                                effect,
                                "rewrite_terminal",
                              )
                            }
                          >
                            补写终态
                          </Button>
                        ) : null}
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : null}
            {primaryRuntimeDiagnosis ? (
              <div
                className={styles.runtimeDiagnosisPanel}
                data-tone={runtimeFailureDiagnosisTone(primaryRuntimeDiagnosis)}
              >
                <strong>{primaryRuntimeDiagnosis.summary}</strong>
                <span>{primaryRuntimeDiagnosis.next_action}</span>
                {primaryRuntimeDiagnosis.evidence ? (
                  <em>证据：{primaryRuntimeDiagnosis.evidence}</em>
                ) : null}
                <Button
                  size="small"
                  type="default"
                  className={styles.runtimeDiagnosisAction}
                  onClick={() =>
                    selectedRun
                      ? void repairRuntimeRun(
                          selectedRun,
                          primaryRuntimeDiagnosis,
                        )
                      : undefined
                  }
                >
                  生成修复提示
                </Button>
              </div>
            ) : null}
            <div className={styles.runtimeDebug}>
              <Collapse
                ghost
                items={[
                  {
                    key: "debug",
                    label: "调试信息",
                    children: (
                      <div className={styles.runtimeDebugBody}>
                        <Button
                          size="small"
                          onClick={() => void copyRuntimeDebug()}
                        >
                          复制调试信息
                        </Button>
                        <pre>
                          {JSON.stringify(
                            {
                              display_id: selectedRun.display_id,
                              attempt_id: selectedRun.attempt_id,
                              authority_status: selectedRun.authority_status,
                              transport_status: selectedRun.transport_status,
                              revision: selectedRun.revision,
                              projection_source: selectedRun.projection_source,
                              chat_id: selectedRun.chat_id,
                              session_id: selectedRun.session_id,
                              workspace_dir: selectedRun.workspace_dir,
                              lease_owner: selectedRun.lease_owner,
                              heartbeat_at: selectedRun.heartbeat_at,
                              current_step: selectedRun.current_step,
                              checkpoint: selectedRun.checkpoint,
                              debug_refs: selectedRun.debug_refs || {},
                            },
                            null,
                            2,
                          )}
                        </pre>
                      </div>
                    ),
                  },
                ]}
              />
            </div>
            <div className={styles.runtimeTimeline}>
              {runtimeEventsLoading ? (
                <div className={styles.loadingState}>
                  <Spin />
                </div>
              ) : runtimeEvents.length === 0 ? (
                <Empty
                  description={runtimeEmptyEventsDescription(selectedRun)}
                />
              ) : (
                runtimeTimelineGroups.map((group) => (
                  <section
                    key={group.phase}
                    className={styles.runtimeTimelineGroup}
                  >
                    <div className={styles.runtimeTimelineGroupTitle}>
                      <strong>{RUNTIME_PHASE_LABELS[group.phase]}</strong>
                      <span>{group.events.length} 条</span>
                    </div>
                    <div className={styles.runtimeTimelineGroupItems}>
                      {group.events.map((event) => {
                        const sourceLine = runtimeEventSourceLine(event);
                        const diagnosis = runtimeEventFailureDiagnosis(event);
                        return (
                          <div
                            key={event.id}
                            className={styles.runtimeTimelineItem}
                          >
                            <span className={styles.runtimeTimelineDot} />
                            <div className={styles.runtimeTimelineBody}>
                              <strong>{runtimeEventTitle(event)}</strong>
                              <p>{runtimeEventDetail(event)}</p>
                              {sourceLine ? (
                                <em className={styles.runtimeTimelineSubmeta}>
                                  {sourceLine}
                                </em>
                              ) : null}
                              {diagnosis ? (
                                <div
                                  className={styles.runtimeTimelineDiagnosis}
                                  data-tone={runtimeFailureDiagnosisTone(
                                    diagnosis,
                                  )}
                                >
                                  <strong>{diagnosis.summary}</strong>
                                  <span>{diagnosis.next_action}</span>
                                  {diagnosis.evidence ? (
                                    <em>证据：{diagnosis.evidence}</em>
                                  ) : null}
                                  <button
                                    type="button"
                                    className={
                                      styles.runtimeTimelineDiagnosisAction
                                    }
                                    onClick={() =>
                                      selectedRun
                                        ? void repairRuntimeRun(
                                            selectedRun,
                                            diagnosis,
                                            event,
                                          )
                                        : undefined
                                    }
                                  >
                                    生成修复提示
                                  </button>
                                </div>
                              ) : null}
                              <span>
                                {formatTime(event.created_at)} · {event.type}
                              </span>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </section>
                ))
              )}
            </div>
          </div>
        ) : null}
      </Drawer>
    </CapabilityPage>
  );
}
