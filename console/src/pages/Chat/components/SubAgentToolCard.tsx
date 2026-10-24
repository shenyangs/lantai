import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type CSSProperties,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronDown,
  Clipboard,
  Loader2,
  PanelRightOpen,
  Send,
  Users,
} from "lucide-react";
import type { AgentSummary } from "../../../api/types/agents";
import type {
  CollaborationBatchSnapshot,
  CollaborationMemberState,
  CollaborationMemberStatus,
} from "../../../api/types/chat";
import {
  collaborationSnapshotForCall,
  subscribeCollaborationCall,
} from "../collaborationEventStore";
import { BeverageAgentAvatar } from "../../../components/BeverageAgentAvatar";
import type { SubAgentInspectionTarget } from "./SubAgentInspectorPanel";
import styles from "./SubAgentToolCard.module.less";

interface RuntimeToolContent {
  data?: {
    name?: string;
    server_label?: string;
    arguments?: unknown;
    output?: unknown;
    error?: unknown;
    id?: string;
    call_id?: string;
    tool_call_id?: string;
    collaboration?: unknown;
  };
}

interface RuntimeToolData {
  status?: string;
  content?: RuntimeToolContent[];
}

interface SubAgentToolCardProps {
  data?: RuntimeToolData;
  agents?: AgentSummary[];
  onInspect?: (target: SubAgentInspectionTarget) => void;
}

type SubAgentStatus = "running" | "submitted" | "done" | "failed" | "pending";

interface AgentMeta {
  accent: string;
  bg: string;
  fallbackName: string;
  role: string;
  alias: string;
}

interface ResultMeta {
  taskId: string;
  sessionId: string;
  taskStatus: string;
}

const SUMMARY_LIMIT = 120;
const DETAILS_LIMIT = 24_000;

export const CORE_SUB_AGENT_TOOL_NAMES = new Set([
  "list_agents",
  "chat_with_agent",
  "collaborate_with_agents",
  "submit_to_agent",
  "check_agent_task",
]);

export const BUILTIN_TEAM_AGENT_IDS = [
  "Lantai_Coconut_Agent",
  "Lantai_Americano_Agent",
  "Lantai_Matcha_Agent",
  "Lantai_Soda_Agent",
  "Lantai_Water_Agent",
  "Lantai_Mocha_Agent",
  "Lantai_Orange_Agent",
  "Lantai_Latte_Agent",
  "Lantai_Espresso_Agent",
  "Lantai_Longjing_Agent",
  "Lantai_BlackCoffee_Agent",
  "Lantai_Puer_Agent",
  "Lantai_RockTea_Agent",
  "Lantai_MilkTea_Agent",
  "Lantai_BlackTea_Agent",
  "Lantai_Cocoa_Agent",
  "Lantai_BarleyTea_Agent",
  "Lantai_SoyMilk_Agent",
  "Lantai_ColdBrew_Agent",
  "Lantai_WhiteTea_Agent",
  "Lantai_WarmWater_Agent",
  "Lantai_ClearTea_Agent",
  "Lantai_Oolong_Agent",
];

const TEAM_AGENT_META: Record<string, AgentMeta> = {
  Lantai_Americano_Agent: {
    accent: "#6F5B4C",
    bg: "#F2E9E1",
    fallbackName: "美式",
    role: "策略、判断、取舍与路线",
    alias: "Americano",
  },
  Lantai_Coconut_Agent: {
    accent: "#4B8A8B",
    bg: "#E4F2F0",
    fallbackName: "椰汁",
    role: "研究、信源、事实与证据",
    alias: "Coconut",
  },
  Lantai_Oolong_Agent: {
    accent: "#7B6F45",
    bg: "#F1EBD5",
    fallbackName: "乌龙",
    role: "公关、媒体、声明与危机沟通",
    alias: "Oolong",
  },
  Lantai_IcedTea_Agent: {
    accent: "#4C7CA8",
    bg: "#E5EEF7",
    fallbackName: "冰茶",
    role: "热点舆论、社会情绪",
    alias: "IcedTea",
  },
  Lantai_Sprite_Agent: {
    accent: "#4B9562",
    bg: "#E6F3EA",
    fallbackName: "雪碧",
    role: "媒体、KOL、发布节奏",
    alias: "Sprite",
  },
  Lantai_MilkTea_Agent: {
    accent: "#A36C5B",
    bg: "#F4E8E3",
    fallbackName: "奶茶",
    role: "市场、增长、渠道与品牌",
    alias: "MilkTea",
  },
  Lantai_Soda_Agent: {
    accent: "#5F79B7",
    bg: "#E8ECF8",
    fallbackName: "苏打",
    role: "数据、统计、指标与可视化",
    alias: "Soda",
  },
  Lantai_Matcha_Agent: {
    accent: "#668246",
    bg: "#EAF0DF",
    fallbackName: "抹茶",
    role: "写作、编辑、结构与统稿",
    alias: "Matcha",
  },
  Lantai_Cola_Agent: {
    accent: "#8E4F58",
    bg: "#F1E2E5",
    fallbackName: "可乐",
    role: "标题、slogan、短文案",
    alias: "Cola",
  },
  Lantai_Latte_Agent: {
    accent: "#92704F",
    bg: "#F1E8DC",
    fallbackName: "拿铁",
    role: "办公、文档、表格与演示",
    alias: "Latte",
  },
  Lantai_Mocha_Agent: {
    accent: "#7C5D70",
    bg: "#EFE4EC",
    fallbackName: "摩卡",
    role: "产品、体验、交互与原型",
    alias: "Mocha",
  },
  Lantai_Orange_Agent: {
    accent: "#B1663F",
    bg: "#F5E6DA",
    fallbackName: "橙汁",
    role: "视觉、图像、视频与创意制作",
    alias: "Orange",
  },
  Lantai_Longjing_Agent: {
    accent: "#4E7F6B",
    bg: "#E3F0EA",
    fallbackName: "龙井",
    role: "复核、反方、风险与验收",
    alias: "Longjing",
  },
  Lantai_Water_Agent: {
    accent: "#4C87A1",
    bg: "#E1EFF4",
    fallbackName: "白水",
    role: "代码、调试、测试与自动化",
    alias: "Water",
  },
  Lantai_Espresso_Agent: {
    accent: "#6B5144",
    bg: "#EEE5DF",
    fallbackName: "浓缩",
    role: "项目、依赖、执行与复盘",
    alias: "Espresso",
  },
  Lantai_BlackCoffee_Agent: {
    accent: "#564943",
    bg: "#ECE7E4",
    fallbackName: "黑咖",
    role: "财务、会计、预算与现金流",
    alias: "BlackCoffee",
  },
  Lantai_Puer_Agent: {
    accent: "#775A45",
    bg: "#F0E8E1",
    fallbackName: "普洱",
    role: "公司、行业、估值与投资风险",
    alias: "Puer",
  },
  Lantai_RockTea_Agent: {
    accent: "#6B6659",
    bg: "#ECEAE3",
    fallbackName: "岩茶",
    role: "法律、合同、权利义务与合规",
    alias: "RockTea",
  },
  Lantai_BlackTea_Agent: {
    accent: "#8B594E",
    bg: "#F1E6E2",
    fallbackName: "红茶",
    role: "销售、客户、提案与客户成功",
    alias: "BlackTea",
  },
  Lantai_Cocoa_Agent: {
    accent: "#74534C",
    bg: "#EFE5E2",
    fallbackName: "可可",
    role: "人才、招聘、绩效与组织",
    alias: "Cocoa",
  },
  Lantai_BarleyTea_Agent: {
    accent: "#817342",
    bg: "#F1EDDA",
    fallbackName: "麦茶",
    role: "经营、采购、库存与供应链",
    alias: "BarleyTea",
  },
  Lantai_SoyMilk_Agent: {
    accent: "#8A7659",
    bg: "#F2ECE3",
    fallbackName: "豆乳",
    role: "学习、课程、练习与反馈",
    alias: "SoyMilk",
  },
  Lantai_ColdBrew_Agent: {
    accent: "#466F82",
    bg: "#E3EDF1",
    fallbackName: "冷萃",
    role: "科学、工程、实验与复现",
    alias: "ColdBrew",
  },
  Lantai_WhiteTea_Agent: {
    accent: "#72806D",
    bg: "#EBF0E8",
    fallbackName: "白茶",
    role: "政策、公共事务与影响分析",
    alias: "WhiteTea",
  },
  Lantai_WarmWater_Agent: {
    accent: "#8B665F",
    bg: "#F2E8E5",
    fallbackName: "温水",
    role: "健康信息、循证解释与就诊准备",
    alias: "WarmWater",
  },
  Lantai_ClearTea_Agent: {
    accent: "#537A68",
    bg: "#E7EFEA",
    fallbackName: "清茶",
    role: "新闻、选题、采访与事实底稿",
    alias: "ClearTea",
  },
};

const TEAM_AGENT_SKILL_ALIASES: Record<string, string> = {
  Lantai_Coconut_Agent: "coconut",
  Lantai_Americano_Agent: "americano",
  Lantai_Matcha_Agent: "matcha",
  Lantai_Soda_Agent: "soda",
  Lantai_Water_Agent: "water",
  Lantai_Mocha_Agent: "mocha",
  Lantai_Orange_Agent: "orange",
  Lantai_Latte_Agent: "latte",
  Lantai_Espresso_Agent: "espresso",
  Lantai_Longjing_Agent: "longjing",
  Lantai_BlackCoffee_Agent: "black_coffee",
  Lantai_Puer_Agent: "puer",
  Lantai_RockTea_Agent: "rock_tea",
  Lantai_MilkTea_Agent: "milk_tea",
  Lantai_BlackTea_Agent: "black_tea",
  Lantai_Cocoa_Agent: "cocoa",
  Lantai_BarleyTea_Agent: "barley_tea",
  Lantai_SoyMilk_Agent: "soy_milk",
  Lantai_ColdBrew_Agent: "cold_brew",
  Lantai_WhiteTea_Agent: "white_tea",
  Lantai_WarmWater_Agent: "warm_water",
  Lantai_ClearTea_Agent: "clear_tea",
  Lantai_Oolong_Agent: "oolong",
};

const DIRECT_AGENT_TOOL_ALIASES = new Set(
  BUILTIN_TEAM_AGENT_IDS.flatMap((id) => {
    const meta = TEAM_AGENT_META[id];
    return [
      normalizeToolKey(id),
      normalizeToolKey(meta.alias),
      normalizeToolKey(TEAM_AGENT_SKILL_ALIASES[id] || ""),
      normalizeToolKey(meta.fallbackName),
    ];
  }),
);

export function isSubAgentToolName(
  toolName: string,
  agents: AgentSummary[] = [],
): boolean {
  if (CORE_SUB_AGENT_TOOL_NAMES.has(toolName)) return true;

  const normalized = normalizeToolKey(toolName);
  if (DIRECT_AGENT_TOOL_ALIASES.has(normalized)) return true;

  return agents.some((agent) => normalizeToolKey(agent.id) === normalized);
}

function normalizeToolKey(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function tryParseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (!trimmed || !/^[{["]/.test(trimmed)) return value;
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function stringifyValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function normalizeId(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.trim().replace(/^["']|["']$/g, "");
}

function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function truncate(value: string, limit = SUMMARY_LIMIT): string {
  const text = normalizeWhitespace(value);
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1)}…`;
}

function getToolName(data?: RuntimeToolData): string {
  return data?.content?.[0]?.data?.name || "sub_agent";
}

function getToolCallId(data?: RuntimeToolData): string {
  const tool = data?.content?.[0]?.data;
  return normalizeId(tool?.call_id || tool?.tool_call_id || tool?.id);
}

function getToolArguments(data?: RuntimeToolData): Record<string, unknown> {
  const args = tryParseJson(data?.content?.[0]?.data?.arguments);
  return args && typeof args === "object" && !Array.isArray(args)
    ? (args as Record<string, unknown>)
    : {};
}

function unwrapText(value: unknown): string {
  const parsed = tryParseJson(value);
  if (typeof parsed === "string") return parsed;
  if (typeof parsed === "number" || typeof parsed === "boolean") {
    return String(parsed);
  }
  if (Array.isArray(parsed)) {
    return parsed
      .map((item) => {
        if (!item || typeof item !== "object") return unwrapText(item);
        const record = item as Record<string, unknown>;
        if (typeof record.text === "string") return record.text;
        if (record.content !== undefined) return unwrapText(record.content);
        if (record.output !== undefined) return unwrapText(record.output);
        return stringifyValue(record);
      })
      .filter(Boolean)
      .join("\n\n");
  }
  if (parsed && typeof parsed === "object") {
    const record = parsed as Record<string, unknown>;
    for (const key of ["text", "output", "result", "content", "message"]) {
      if (record[key] !== undefined) return unwrapText(record[key]);
    }
  }
  return stringifyValue(parsed);
}

function getOutputText(data?: RuntimeToolData): string {
  return unwrapText(data?.content?.[1]?.data?.output).trim();
}

function parseResultMeta(text: string): ResultMeta {
  return {
    taskId: text.match(/^\[TASK_ID:\s*([^\]]+)\]/m)?.[1]?.trim() || "",
    sessionId: text.match(/^\[SESSION:\s*([^\]]+)\]/m)?.[1]?.trim() || "",
    taskStatus: text.match(/^\[STATUS:\s*([^\]]+)\]/m)?.[1]?.trim() || "",
  };
}

function stripResultMeta(text: string): string {
  return text
    .replace(/^\[(?:SESSION|TASK_ID|STATUS):[^\]]+\]\s*$/gm, "")
    .replace(/^Task completed\.\s*$/gim, "")
    .replace(/^Task submitted successfully\.\s*$/gim, "")
    .replace(/^Check status with:\s*check_agent_task\([^)]+\)\s*$/gim, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function sanitizeSubAgentPublicText(text: string): string {
  return text
    .replace(
      /(?:[A-Za-z]:)?[^\s`"']*skills[\\/][^\s`"']+[\\/]SKILL\.md/gi,
      "专业方法",
    )
    .replace(/\bSKILL\.md\b/gi, "专业方法")
    .replace(/\bread_skill\b/gi, "载入专业方法")
    .replace(/\bskill_name\b/gi, "专业能力标识")
    .replace(/\bskills?\b/gi, "专业方法");
}

function trimDetailText(text: string): string {
  if (text.length <= DETAILS_LIMIT) return text;
  return `${text.slice(0, DETAILS_LIMIT)}\n\n...已截断预览，复制可获取当前完整内容。`;
}

function parseTargetFromSession(sessionId: string): string {
  return sessionId.match(/:to:([^:]+)/)?.[1] || "";
}

function resolveDirectAgentTool(toolName: string): string {
  const normalized = normalizeToolKey(toolName);
  return (
    BUILTIN_TEAM_AGENT_IDS.find((id) => {
      const meta = TEAM_AGENT_META[id];
      return [
        normalizeToolKey(id),
        normalizeToolKey(meta.alias),
        normalizeToolKey(TEAM_AGENT_SKILL_ALIASES[id] || ""),
        normalizeToolKey(meta.fallbackName),
      ].includes(normalized);
    }) || ""
  );
}

function resolveTargetAgentId(
  toolName: string,
  args: Record<string, unknown>,
  resultMeta: ResultMeta,
): string {
  return (
    normalizeId(args.to_agent) ||
    parseTargetFromSession(resultMeta.sessionId) ||
    resolveDirectAgentTool(toolName)
  );
}

function findAgent(
  agents: AgentSummary[],
  agentId: string,
): AgentSummary | undefined {
  if (!agentId) return undefined;
  return agents.find(
    (agent) =>
      agent.id === agentId ||
      normalizeToolKey(agent.id) === normalizeToolKey(agentId),
  );
}

function getAgentMeta(agentId: string): AgentMeta {
  return (
    TEAM_AGENT_META[agentId] || {
      accent: "#5D7891",
      bg: "#E7EEF4",
      fallbackName: agentId || "协作进度",
      role: "多智能体协作",
      alias: agentId || "Collaboration",
    }
  );
}

function getAgentName(agent?: AgentSummary, meta?: AgentMeta): string {
  return agent?.name || meta?.fallbackName || "协作进度";
}

function getStatus(
  data: RuntimeToolData | undefined,
  text: string,
): SubAgentStatus {
  const runtimeStatus = String(data?.status || "").toLowerCase();
  const error = data?.content?.[1]?.data?.error;
  const lowerText = text.toLowerCase();
  const taskStatus = parseResultMeta(text).taskStatus.toLowerCase();

  if (runtimeStatus.includes("progress") || runtimeStatus.includes("running")) {
    return "running";
  }
  if (runtimeStatus.includes("fail") || runtimeStatus.includes("error")) {
    return "failed";
  }
  if (error || /^error:/im.test(text) || lowerText.includes("task failed")) {
    return "failed";
  }
  if (!text) return "pending";
  if (["running", "pending", "submitted"].includes(taskStatus)) {
    return taskStatus === "submitted" ? "submitted" : "running";
  }
  if (lowerText.includes("task submitted successfully")) return "submitted";
  return "done";
}

function getStatusLabel(status: SubAgentStatus, toolName: string): string {
  if (toolName === "list_agents") return "团队名单";
  switch (status) {
    case "running":
      return "工作中";
    case "submitted":
      return "已派发";
    case "failed":
      return "失败";
    case "pending":
      return "等待中";
    default:
      return "已完成";
  }
}

function getStatusIcon(status: SubAgentStatus) {
  if (status === "running")
    return <Loader2 className={styles.spin} size={14} />;
  if (status === "failed") return <AlertCircle size={14} />;
  if (status === "submitted") return <Send size={14} />;
  if (status === "done") return <CheckCircle2 size={14} />;
  return <Users size={14} />;
}

function getTaskText(toolName: string, args: Record<string, unknown>): string {
  if (typeof args.text === "string" && args.text.trim()) {
    return sanitizeSubAgentPublicText(args.text.trim());
  }
  if (toolName === "check_agent_task") {
    return "同步协作任务的最新进度";
  }
  if (toolName === "list_agents") return "协作团队已准备就绪";
  return "";
}

function buildFallbackResult(toolName: string, status: SubAgentStatus): string {
  if (toolName === "submit_to_agent" && status === "submitted") {
    return "任务已经交给协作者，完成后会回到当前对话。";
  }
  if (status === "running") return "协作者正在处理。";
  if (status === "pending") return "等待工具返回结果。";
  return "暂无结果。";
}

function getModeLabel(toolName: string): string {
  if (toolName === "submit_to_agent") return "异步协作";
  if (toolName === "check_agent_task") return "进度同步";
  if (toolName === "chat_with_agent") return "即时协作";
  if (resolveDirectAgentTool(toolName)) return "专业协作";
  return "团队协作";
}

function buildActivityLine(
  toolName: string,
  status: SubAgentStatus,
  resultMeta: ResultMeta,
): string {
  if (status === "running") {
    if (toolName === "check_agent_task") return "正在同步协作进度";
    if (resultMeta.taskStatus) return `当前状态：${resultMeta.taskStatus}`;
    return "协作者正在处理";
  }
  if (status === "submitted") return "任务已派发，等待协作者处理";
  if (status === "pending") return "等待协作工具返回结果";
  if (status === "failed") return "协作调用失败，结果区保留错误信息";
  return "协作者已返回结果";
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard?.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };

  return (
    <button
      type="button"
      className={styles.iconButton}
      onClick={handleCopy}
      aria-label="复制协作结果"
      disabled={!text}
    >
      {copied ? <Check size={14} /> : <Clipboard size={14} />}
    </button>
  );
}

interface BatchResultRow {
  member_id?: string;
  agent_id?: string;
  session_id?: string;
  run_id?: string;
  child_run_id?: string;
  status?: string;
  result?: string;
  error?: string;
}

function batchResultRows(data?: RuntimeToolData): BatchResultRow[] {
  const parsed = tryParseJson(getOutputText(data));
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];
  const results = (parsed as Record<string, unknown>).results;
  return Array.isArray(results)
    ? results.filter(
        (result): result is BatchResultRow =>
          Boolean(result) && typeof result === "object",
      )
    : [];
}

function initialBatchSnapshot(
  args: Record<string, unknown>,
  agents: AgentSummary[],
  data?: RuntimeToolData,
): CollaborationBatchSnapshot | undefined {
  const tasks = Array.isArray(args.tasks) ? args.tasks : [];
  const runtimeStatus = String(data?.status || "").toLowerCase();
  const resultByAgent = new Map(
    batchResultRows(data)
      .filter((result) => typeof result.agent_id === "string")
      .map((result) => [result.agent_id as string, result]),
  );
  const isActive = ["progress", "running", "pending", "submitted"].some(
    (status) => runtimeStatus.includes(status),
  );
  // Arguments are only a proposal.  Once a call has failed, they cannot prove
  // that the hosted coordinator created a room or started any member.  Keep a
  // first-frame preview while active, and retain legacy completed batches only
  // when they contain real member results.
  if (!isActive && resultByAgent.size === 0) return undefined;
  const members = tasks.flatMap((task, index) => {
    if (!task || typeof task !== "object") return [];
    const record = task as Record<string, unknown>;
    const agentId = normalizeId(record.to_agent);
    if (!agentId) return [];
    const agent = findAgent(agents, agentId);
    const meta = getAgentMeta(agent?.id || agentId);
    const result = resultByAgent.get(agentId);
    const resultStatus = String(result?.status || "").toLowerCase();
    let status: CollaborationMemberStatus = "queued";
    if (resultStatus === "completed") status = "completed";
    else if (resultStatus === "failed") status = "failed";
    else if (resultStatus === "cancelled") status = "cancelled";
    else if (runtimeStatus.includes("progress")) status = "running";
    return [
      {
        member_id: `member-${index + 1}`,
        agent_id: agentId,
        display_name: getAgentName(agent, meta),
        role: agent?.description || meta.role,
        label: normalizeId(record.label) || "协作任务",
        status,
        attempt: status === "queued" ? 0 : 1,
        duration_ms: 0,
        error_code: null,
      } satisfies CollaborationMemberState,
    ];
  });
  if (members.length === 0) return undefined;
  return {
    batch_id: getToolCallId(data) || "pending-collaboration",
    revision: 0,
    status: isActive ? "running" : "completed",
    protocol: normalizeId(args.protocol) || "parallel_consultation",
    goal: normalizeId(args.goal),
    why_parallel: normalizeId(args.why_parallel),
    acceptance_criteria: Array.isArray(args.acceptance_criteria)
      ? args.acceptance_criteria.map(normalizeId).filter(Boolean)
      : [],
    members,
  };
}

function batchStatusLabel(snapshot: CollaborationBatchSnapshot): string {
  if (snapshot.status === "failed") return "协作失败";
  if (snapshot.status === "partial") return "部分完成";
  if (snapshot.status === "cancelled") return "已取消";
  const phaseLabels: Record<string, string> = {
    preparing: "准备中",
    staffing: "正在组队",
    working: "协作中",
    reconciling: "统一结果",
    verifying: "验证中",
    delivering: "准备交付",
    closing: "正在关房",
    closed: "已关房",
  };
  if (snapshot.phase && phaseLabels[snapshot.phase]) {
    return phaseLabels[snapshot.phase];
  }
  switch (snapshot.status) {
    case "completed":
      return "已完成";
    default:
      return "并行处理中";
  }
}

function batchHostTitle(
  snapshot: CollaborationBatchSnapshot,
  hostName: string,
): string {
  switch (snapshot.status) {
    case "completed":
      if (snapshot.closure?.deliverable_ready) return `${hostName}已完成交付`;
      if (snapshot.closure?.synthesis_ready)
        return `${hostName}已回收结果，待统一交付`;
      return `${hostName}已结束协作`;
    case "partial":
      return `${hostName}已结束协作`;
    case "failed":
      return "协作未形成可验收结果";
    case "cancelled":
      return "协作已取消";
    default:
      return `${hostName}正在主持协作`;
  }
}

function memberStatusLabel(member: CollaborationMemberState): string {
  if (member.status === "waiting_resource") return "等待独占资源";
  if (member.review_status === "running") return "正在交叉复核";
  if (member.review_status === "unavailable") {
    return "首轮已保留 · 复核未完成";
  }
  if (member.review_status === "completed") return "已完成交叉复核";
  switch (member.status) {
    case "running":
      return "工作中";
    case "retrying":
      return "正在重试";
    case "completed":
      return "结果已回收";
    case "failed":
      return "失败";
    case "cancelled":
      return "已取消";
    default:
      return member.depends_on?.length ? "等待上游交付" : "等待中";
  }
}

function memberStatusIcon(status: CollaborationMemberStatus) {
  if (
    status === "running" ||
    status === "retrying" ||
    status === "waiting_resource"
  ) {
    return <Loader2 className={styles.spin} size={13} aria-hidden="true" />;
  }
  if (status === "completed") {
    return <CheckCircle2 size={13} aria-hidden="true" />;
  }
  if (status === "failed" || status === "cancelled") {
    return <AlertCircle size={13} aria-hidden="true" />;
  }
  return <Users size={13} aria-hidden="true" />;
}

function CollaborationBatchCard({
  data,
  agents,
  onInspect,
}: {
  data?: RuntimeToolData;
  agents: AgentSummary[];
  onInspect?: (target: SubAgentInspectionTarget) => void;
}) {
  const args = getToolArguments(data);
  const callId = getToolCallId(data);
  const subscribe = useCallback(
    (listener: () => void) => subscribeCollaborationCall(callId, listener),
    [callId],
  );
  const getSnapshot = useCallback(
    () => collaborationSnapshotForCall(callId),
    [callId],
  );
  const liveSnapshot = useSyncExternalStore(
    subscribe,
    getSnapshot,
    getSnapshot,
  );
  const fallbackSnapshot = useMemo(
    () => initialBatchSnapshot(args, agents, data),
    [agents, args, data],
  );
  const snapshot = liveSnapshot || fallbackSnapshot;
  const results = batchResultRows(data);
  const resultByMember = new Map(
    results.map((result) => [
      result.member_id || result.agent_id || "",
      result,
    ]),
  );
  if (!snapshot) return null;
  const terminalStatuses = new Set(["completed", "failed", "cancelled"]);
  const completedCount = snapshot.members.filter((member) =>
    terminalStatuses.has(member.status),
  ).length;
  const isTerminal =
    snapshot.phase === "closed" ||
    ["completed", "partial", "failed", "cancelled"].includes(snapshot.status);
  const hostName = snapshot.host?.display_name || "沈行知";
  const roomTarget: SubAgentInspectionTarget = {
    id: `${callId || snapshot.batch_id}:room`,
    agentId: snapshot.host?.agent_id || "default",
    agentName: hostName,
    role: "主持、综合与最终交付",
    task: snapshot.goal || normalizeId(args.goal) || "完成本次协作",
    status: snapshot.status,
    result: "",
    toolName: "collaborate_with_agents",
    toolCallId: callId || snapshot.batch_id,
    kind: "room",
    roomSnapshot: snapshot,
  };

  return (
    <section
      className={styles.batchPanel}
      data-chat-tool-call="1"
      aria-label={`本次协作，共 ${snapshot.members.length} 位`}
    >
      <button
        type="button"
        className={styles.batchHeader}
        onClick={() => onInspect?.(roomTarget)}
        disabled={!onInspect}
        aria-label="查看协作室"
      >
        <span className={styles.teamIcon} aria-hidden="true">
          <Users size={16} />
        </span>
        <span className={styles.batchHeading}>
          <small>
            {snapshot.protocol === "roundtable"
              ? "协作 · 圆桌研判"
              : "协作 · 并行分工"}
          </small>
          <strong>这个问题准备怎么回答？</strong>
          <span className={styles.batchGoal}>
            {snapshot.goal || normalizeId(args.goal) || "完成本次协作"}
          </span>
          {snapshot.why_parallel ? (
            <em className={styles.batchWhy}>{snapshot.why_parallel}</em>
          ) : null}
        </span>
        <span
          className={`${styles.batchStatus} ${styles[`batchStatus_${snapshot.status}`] || ""}`}
          aria-live="polite"
        >
          {snapshot.status === "running" && (
            <Loader2 className={styles.spin} size={14} aria-hidden="true" />
          )}
          {batchStatusLabel(snapshot)}
        </span>
        {onInspect ? (
          <PanelRightOpen
            size={16}
            className={styles.roomOpenIcon}
            aria-hidden="true"
          />
        ) : null}
      </button>

      <div className={styles.batchSummary}>
        <strong>{batchHostTitle(snapshot, hostName)}</strong>
        <span>
          {snapshot.quiet_summary ||
            (!isTerminal
              ? `已选 ${snapshot.members.length} 位协作者 · 直接发新消息可调整方向`
              : `${completedCount}/${snapshot.members.length} 位已进入终态`)}
        </span>
      </div>

      <div className={styles.batchMembers}>
        {snapshot.members.map((member) => {
          const agent = findAgent(agents, member.agent_id);
          const meta = getAgentMeta(agent?.id || member.agent_id);
          const name =
            member.display_name || getAgentName(agent, meta) || member.agent_id;
          const result =
            resultByMember.get(member.member_id) ||
            resultByMember.get(member.agent_id);
          const resultText = sanitizeSubAgentPublicText(
            String(
              member.result ||
                result?.result ||
                member.error_message ||
                result?.error ||
                "",
            ).trim(),
          );
          return (
            <div
              key={member.member_id}
              className={`${styles.batchMember} ${styles[`member_${member.status}`] || ""}`}
              style={
                {
                  "--agent-accent": meta.accent,
                  "--agent-bg": meta.bg,
                } as CSSProperties
              }
            >
              <button
                type="button"
                className={styles.batchMemberButton}
                onClick={() => {
                  if (onInspect) {
                    onInspect({
                      id: `${callId || snapshot.batch_id}:${member.member_id}`,
                      agentId: member.agent_id,
                      agentName: name,
                      role: member.role || agent?.description || meta.role,
                      task: member.label,
                      status: member.status,
                      result: resultText,
                      toolName: "collaborate_with_agents",
                      toolCallId: callId || snapshot.batch_id,
                      kind: "member",
                      memberId: member.member_id,
                      sessionId: member.session_id || result?.session_id,
                      taskId: member.task_id || undefined,
                      runId:
                        member.run_id || result?.run_id || result?.child_run_id,
                      chatId: member.chat_id || undefined,
                    });
                  }
                }}
                aria-label={
                  onInspect
                    ? `查看${name}的协作详情`
                    : `${name}：${memberStatusLabel(member)}`
                }
                disabled={!onInspect}
              >
                <span className={styles.batchAvatar} aria-hidden="true">
                  <BeverageAgentAvatar agentId={member.agent_id} size={34} />
                </span>
                <span className={styles.batchMemberIdentity}>
                  <span>
                    <strong>{name}</strong>
                    <small>
                      {sanitizeSubAgentPublicText(
                        member.role || agent?.description || meta.role,
                      )}
                    </small>
                  </span>
                  <span className={styles.batchTaskLabel}>
                    {sanitizeSubAgentPublicText(member.label)}
                  </span>
                </span>
                <span className={styles.batchMemberStatus}>
                  {memberStatusIcon(member.status)}
                  {memberStatusLabel(member)}
                  {member.attempt > 1 && <small>第 {member.attempt} 次</small>}
                </span>
                {onInspect ? (
                  <PanelRightOpen
                    size={16}
                    className={styles.chevron}
                    aria-hidden="true"
                  />
                ) : null}
              </button>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function RosterCard({
  data,
  agents = [],
}: {
  data?: RuntimeToolData;
  agents?: AgentSummary[];
}) {
  const outputText = getOutputText(data);
  const output = tryParseJson(outputText);
  const outputAgents =
    output && typeof output === "object" && !Array.isArray(output)
      ? (output as Record<string, unknown>).agents
      : output;
  const outputCount = Array.isArray(outputAgents) ? outputAgents.length : 0;
  const enabledCount =
    agents.filter((agent) => agent.id !== "default" && agent.enabled !== false)
      .length || outputCount;

  return (
    <div className={`${styles.subAgentCard} ${styles.rosterCard}`}>
      <div className={styles.headerButton}>
        <span className={styles.teamIcon} aria-hidden="true">
          <Users size={16} />
        </span>
        <span className={styles.headerMain}>
          <span className={styles.titleRow}>
            <span className={styles.agentName}>协作团队已就绪</span>
            <span className={styles.statusBadge}>可用</span>
          </span>
          <span className={styles.taskLine}>
            {enabledCount
              ? `${enabledCount} 位专业协作者可按需调用`
              : "可按需调用专业协作者"}
          </span>
        </span>
      </div>
    </div>
  );
}

export default function SubAgentToolCard({
  data,
  agents = [],
  onInspect,
}: SubAgentToolCardProps) {
  const toolName = getToolName(data);
  const args = getToolArguments(data);
  const outputText = getOutputText(data);
  const resultMeta = parseResultMeta(outputText);
  const status = getStatus(data, outputText);
  const targetAgentId = resolveTargetAgentId(toolName, args, resultMeta);
  const agent = findAgent(agents, targetAgentId);
  const meta = getAgentMeta(agent?.id || targetAgentId);
  const agentName = getAgentName(agent, meta);
  const taskText = getTaskText(toolName, args);
  const cleanedResult = sanitizeSubAgentPublicText(
    stripResultMeta(outputText) || buildFallbackResult(toolName, status),
  );
  const terminal = status === "done" || status === "failed";
  const displayResult = terminal
    ? cleanedResult
    : buildFallbackResult(toolName, status);
  const activityLine = buildActivityLine(toolName, status, resultMeta);
  const defaultExpanded = false;
  const [expanded, setExpanded] = useState(defaultExpanded);

  useEffect(() => {
    setExpanded(defaultExpanded);
  }, [defaultExpanded, resultMeta.taskId, status, targetAgentId, toolName]);

  if (toolName === "list_agents") {
    return <RosterCard data={data} agents={agents} />;
  }
  if (toolName === "collaborate_with_agents") {
    return (
      <CollaborationBatchCard
        data={data}
        agents={agents}
        onInspect={onInspect}
      />
    );
  }
  if (toolName === "check_agent_task" && !terminal) {
    return null;
  }

  const statusClass =
    status === "running"
      ? styles.running
      : status === "failed"
        ? styles.failed
        : status === "submitted"
          ? styles.submitted
          : status === "pending"
            ? styles.pending
            : styles.done;
  const subtitle = truncate(taskText || activityLine);
  const inspectionTarget: SubAgentInspectionTarget = {
    id:
      getToolCallId(data) ||
      resultMeta.taskId ||
      resultMeta.sessionId ||
      `${toolName}:${targetAgentId || agentName}`,
    agentId: targetAgentId,
    agentName,
    role: agent?.description || meta.role,
    task: taskText,
    status,
    result: terminal ? displayResult : "",
    toolName,
    toolCallId: getToolCallId(data),
    sessionId: resultMeta.sessionId,
    taskId: resultMeta.taskId,
  };

  return (
    <div
      className={`${styles.subAgentCard} ${statusClass}`}
      data-chat-tool-call="1"
      style={
        {
          "--agent-accent": meta.accent,
          "--agent-bg": meta.bg,
        } as CSSProperties
      }
    >
      <button
        type="button"
        className={styles.headerButton}
        onClick={() => {
          if (onInspect) {
            onInspect(inspectionTarget);
            return;
          }
          setExpanded((value) => !value);
        }}
        aria-expanded={onInspect ? undefined : expanded}
        aria-label={
          onInspect
            ? `查看${agentName}的协作详情`
            : `${expanded ? "收起" : "展开"}协作任务 ${agentName}`
        }
      >
        <span className={styles.avatarWrap}>
          <span className={styles.avatar}>
            <BeverageAgentAvatar agentId={targetAgentId} size={34} />
          </span>
          <span className={styles.presenceDot} aria-hidden="true" />
        </span>
        <span className={styles.headerMain}>
          <span className={styles.titleRow}>
            <span className={styles.agentName}>{agentName}</span>
            <span className={styles.roleText}>
              {agent?.description || meta.role}
            </span>
            <span className={styles.statusBadge}>
              {getStatusIcon(status)}
              {getStatusLabel(status, toolName)}
            </span>
          </span>
          <span className={styles.taskLine}>
            {getModeLabel(toolName)} · {subtitle || "协作任务"}
          </span>
        </span>
        {onInspect ? (
          <PanelRightOpen
            size={17}
            className={styles.chevron}
            aria-hidden="true"
          />
        ) : (
          <ChevronDown
            size={18}
            className={`${styles.chevron} ${expanded ? styles.chevronOpen : ""}`}
            aria-hidden="true"
          />
        )}
      </button>

      {!onInspect && expanded && (
        <div className={styles.details}>
          <section className={styles.detailSection}>
            <div className={styles.sectionLabel}>当前状态</div>
            <div className={styles.activityLine}>{activityLine}</div>
          </section>

          {taskText && (
            <section className={styles.detailSection}>
              <div className={styles.sectionLabel}>任务</div>
              <div className={styles.promptPreview}>
                {truncate(taskText, 260)}
              </div>
            </section>
          )}

          {terminal && (
            <section className={styles.detailSection}>
              <div className={styles.sectionHeader}>
                <span className={styles.sectionLabel}>结果</span>
                <CopyButton text={displayResult} />
              </div>
              <div className={styles.resultBox}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {trimDetailText(displayResult)}
                </ReactMarkdown>
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
