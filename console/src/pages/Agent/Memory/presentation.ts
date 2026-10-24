import type {
  LayeredMemorySnapshot,
  MemoryEngineStatus,
  UserMemoryEntry,
  UserMemorySnapshot,
  UserMemoryType,
} from "../../../api/types";

export interface MemoryImpressionCardView {
  lead: string;
  chips: string[];
  stats: string;
}

export interface MemoryOverviewHighlight {
  type: UserMemoryType;
  label: string;
  value: string;
}

export interface MemoryOverviewView {
  headline: string;
  narrative: string[];
  highlights: MemoryOverviewHighlight[];
  chips: string[];
}

export interface MemoryDisplayItem {
  content: string;
  sourceTitle: string;
  confidenceLabel: MemoryConfidenceLabel;
  confidenceValue: number;
}

export type MemoryConfidenceLabel =
  | "已确认"
  | "建议确认"
  | "较可靠"
  | "自动沉淀";

export interface MemoryEngineStatusRowView {
  key: string;
  label: string;
  status: string;
  detail: string;
  metric: string;
  metricLabel: string;
  tone: "ready" | "partial" | "disabled" | "error";
}

export interface MemoryEngineStatusView {
  overall: "ready" | "partial" | "disabled" | "error";
  overallLabel: string;
  summary: string;
  rows: MemoryEngineStatusRowView[];
}

export function pickPrimaryManualEntry(
  entries: UserMemoryEntry[],
  type: UserMemoryType,
) {
  return (
    [...entries]
      .filter(
        (entry) =>
          entry.type === type &&
          entry.status === "active" &&
          entry.source === "manual",
      )
      .sort(
        (a, b) =>
          new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      )[0] || null
  );
}

const DISPLAY_PREFIX_RE = /^(?:用户(?:的)?|默认规则[：:]?|长期规则[：:]?)\s*/;
const PROFILE_SECTION_PREFIX_RE =
  /^(?:用户(?:的)?(?:长期)?(?:偏好|画像|信息|背景|情况|记忆)|长期(?:印象|偏好)|个人使用画像)[：:]\s*/;
const LOCAL_PATH_RE =
  /(?:~|\/Users|\/private|\/var|\/tmp|\/Applications|\/Volumes)(?:\/[^\s，。；;、）)\]"']+)+/g;
const FILE_LIKE_RE =
  /[A-Za-z0-9_\-.+\u4e00-\u9fa5]+?\.(?:zip|dmg|pkg|app|pdf|docx|pptx|xlsx|md|json|log|py|ts|tsx|js|jsx|html|png|jpg|jpeg|webp|gif)\b/gi;
const OPAQUE_ID_RE = /^[0-9a-f]{8,}$/i;

const INTERNAL_PHRASES = [
  "已经下载到",
  "当前工作目录",
  "工作区状态",
  "下载到 /",
  "保存到 /",
  "backend.log",
  "node_modules",
  ".venv",
  "site-packages",
  "Library/Containers",
  "Application Support",
  "已运行",
  "已搜索",
  "已编辑",
  "执行命令",
];

const OPERATIONAL_TRACE_RE =
  /(已探索|已搜索|已运行|已编辑|运行了|执行了|调用了|读取了|写入了|复制到|下载到|保存到|stderr|stdout|exit code|backend\.log|构建产物|release\/|dist\/|node_modules|site-packages)/i;
const ASSISTANT_META_RE =
  /(沈行知.{0,24}(助手|助理|工作伙伴|身份名|角色背景|角色设定|职责|模块|人格)|(助手|助理|工作伙伴|身份名|角色背景|角色设定|职责|模块|人格).{0,24}沈行知|澜台中.{0,16}(行知|沈行知).{0,24}(用户|名字|身份|角色)|(行知|沈行知)是澜台的默认工作伙伴身份名|(行知|沈行知).{0,24}(默认工作伙伴|陪伴用户|判断、推进和交付任务))/;

const DISPLAY_RELEVANCE_RE: Record<UserMemoryType, RegExp> = {
  identity:
    /(用户|身份|角色|名字|称呼|背景|职业|项目|长期|住在|位于|已婚|家人|团队|公司|负责|从事|出生|成长)/,
  workflow:
    /(习惯|流程|工作方式|工作区|路径|构建|验证|同步|打包|复核|优先|处理|安装|导入|上传|文件|资料|技能|测试|发布|审查)/,
  style:
    /(偏好|喜欢|不喜欢|语气|风格|中文|英文|表达|措辞|简洁|详细|自然|克制|格式|标题|排版|口吻)/,
  instruction:
    /(要求|默认|规则|指令|不要|必须|需要|以后|每次|优先|禁止|记住|提醒|确认|直接|执行)/,
};

function trimSentence(text: string) {
  return text
    .trim()
    .replace(/\s+/g, " ")
    .replace(/[。；;]+$/g, "");
}

function basename(path: string) {
  const cleaned = path.replace(/[，。；;、）)\]"']+$/g, "");
  return cleaned.split("/").filter(Boolean).pop() || "本地文件";
}

function firstFileName(text: string) {
  const fileMatch = text.match(FILE_LIKE_RE)?.[0];
  if (fileMatch) return fileMatch;
  const pathMatch = text.match(LOCAL_PATH_RE)?.[0];
  if (pathMatch) return basename(pathMatch);
  return "";
}

function replaceLocalPaths(text: string) {
  return text.replace(LOCAL_PATH_RE, (path) => `「${basename(path)}」`);
}

function normalizeReadableText(text: string) {
  return replaceLocalPaths(text)
    .replace(/会话\s+[0-9a-f]{6,}/gi, "最近一轮聊天")
    .replace(/消息\s+[0-9a-f]{6,}/gi, "最近一条消息")
    .replace(/\s+/g, " ")
    .trim();
}

export function looksLikeInternalMemory(text: string) {
  const normalized = text.trim();
  if (!normalized) return false;
  LOCAL_PATH_RE.lastIndex = 0;
  const hasLocalPath = LOCAL_PATH_RE.test(normalized);
  LOCAL_PATH_RE.lastIndex = 0;
  if (hasLocalPath) return true;
  return INTERNAL_PHRASES.some((phrase) => normalized.includes(phrase));
}

export function looksLikeOperationalTrace(text: string) {
  return OPERATIONAL_TRACE_RE.test(text);
}

export function looksLikeAssistantMetaMemory(text: string) {
  return ASSISTANT_META_RE.test(text.trim());
}

function humanizeTechnicalMemory(text: string, type: UserMemoryType) {
  if (type === "workflow" && text.includes("memory.md / MEMORY.md")) {
    return "目录里已有 memory.md / MEMORY.md，可作为工作区记忆使用";
  }

  const fileName = firstFileName(text);
  if (fileName && /(上传|下载|导入|安装|预装|资料|文件|技能)/.test(text)) {
    if (type === "workflow") {
      return `处理本地资料时，会关注文件是否已落地并可继续使用`;
    }
    if (/skill|技能|wps365/i.test(text)) {
      return `用户曾上传「${fileName}」这类技能文件，希望澜台能完成安装和预装`;
    }
    return `用户曾上传「${fileName}」这类本地文件，希望澜台继续处理`;
  }

  if (/当前工作目录|工作区状态/.test(text)) {
    return "用户经常围绕当前工作区继续推进任务";
  }

  return replaceLocalPaths(text);
}

function polishDisplaySentence(text: string, type: UserMemoryType) {
  let normalized = text.trim().replace(PROFILE_SECTION_PREFIX_RE, "").trim();

  if (type === "instruction" && /^回答/.test(normalized)) {
    normalized = `回答时${normalized.replace(/^回答/, "")}`;
  }
  if (type === "instruction") {
    normalized = normalized.replace(/^(默认规则：|长期规则：)/, "").trim();
  }

  if (type === "style") {
    normalized = normalized.replace(/^偏好/, "用户偏好");
    normalized = normalized.replace(/^不喜欢/, "用户不喜欢");
  }

  if (type === "workflow") {
    normalized = normalized.replace(/^处理/, "用户希望处理");
  }

  return normalized || text;
}

function looksLikeOpaqueTitle(text: string) {
  const normalized = text.trim();
  if (!normalized) return true;
  if (OPAQUE_ID_RE.test(normalized)) return true;
  if (/^(会话|消息)\s+[0-9a-f]{6,}$/i.test(normalized)) return true;
  LOCAL_PATH_RE.lastIndex = 0;
  if (LOCAL_PATH_RE.test(normalized)) return true;
  return false;
}

function stripDisplayPrefix(text: string) {
  return text.replace(DISPLAY_PREFIX_RE, "").trim();
}

export function toDisplaySentence(
  text: string,
  type: UserMemoryType = "identity",
) {
  const raw = trimSentence(text);
  if (!raw) return "";

  const readable = looksLikeInternalMemory(raw)
    ? humanizeTechnicalMemory(raw, type)
    : replaceLocalPaths(raw);
  const normalized = trimSentence(polishDisplaySentence(readable, type));
  if (!normalized) return "";
  return normalized;
}

export function toUserFacingSourceTitle(entry: UserMemoryEntry) {
  const explicitTitle = normalizeReadableText(String(entry.source_title || ""));
  if (explicitTitle && !looksLikeOpaqueTitle(explicitTitle)) {
    return explicitTitle;
  }
  if (entry.source_kind === "chat") return "最近一轮聊天";
  if (entry.source_kind === "file") return "最近处理的文件";
  if (entry.source_kind === "import") return "其他 AI 迁移";
  if (entry.source_kind === "system") return "系统同步";
  if (entry.source_kind === "manual" || entry.source === "manual") {
    return "你手动补充的记忆";
  }
  if (entry.source_kind === "agent" || entry.source === "agent") {
    return "智能体自动沉淀";
  }
  return "自动沉淀";
}

export function toDisplayEvidence(
  text: string,
  type: UserMemoryType = "identity",
) {
  const readable = normalizeReadableText(text || "");
  if (!readable) return "";
  if (looksLikeInternalMemory(readable)) {
    return toDisplaySentence(readable, type);
  }
  return readable.length > 220
    ? `${readable.slice(0, 220).trim()}...`
    : readable;
}

export function toConfidenceLabel(
  entry: UserMemoryEntry,
): MemoryConfidenceLabel {
  if (entry.source === "manual") return "已确认";
  if (entry.status === "conflict") return "建议确认";
  if (entry.confidence < 0.72) return "建议确认";
  if (entry.confidence >= 0.88) return "较可靠";
  return "自动沉淀";
}

function hasTypeRelevance(text: string, type: UserMemoryType) {
  return DISPLAY_RELEVANCE_RE[type].test(text);
}

export function isUserFacingMemoryEntry(entry: UserMemoryEntry) {
  const raw = trimSentence(entry.content);
  if (!raw) return false;
  if (looksLikeAssistantMetaMemory(raw)) return false;
  if (entry.source === "manual") return true;
  if (entry.memory_layer === "trace") return false;
  if (entry.memory_layer === "offload") return false;

  const display = toDisplaySentence(raw, entry.type);
  if (!display) return false;

  const internal = looksLikeInternalMemory(raw);
  const operational = looksLikeOperationalTrace(raw);

  if (internal || operational) {
    if (entry.type !== "workflow") return false;
    return hasTypeRelevance(display, "workflow");
  }

  if (entry.confidence < 0.45) return false;
  return hasTypeRelevance(display, entry.type) || entry.status === "conflict";
}

export function toDisplayChip(text: string, type: UserMemoryType) {
  let normalized = stripDisplayPrefix(toDisplaySentence(text, type));
  normalized = normalized.replace(/^当前工作目录是/, "工作目录");
  normalized = normalized.replace(
    /^处理上传文件时，文件会先落到/,
    "上传文件落地",
  );
  normalized = normalized.replace(/^不喜欢用/, "不喜欢");
  normalized = normalized.replace(/^希望/, "希望");
  normalized = normalized.replace(/^偏好/, "偏好");
  normalized = normalized.replace(/^以后/, "以后");
  normalized = normalized.replace(/^每次/, "每次");

  if (type === "workflow" && normalized.includes("memory.md / MEMORY.md")) {
    normalized = "目录已有 memory.md / MEMORY.md";
  }

  if (normalized.length > 22) {
    normalized = `${normalized.slice(0, 22).trim()}...`;
  }
  return normalized;
}

function addressUserForReview(text: string) {
  return text.replace(/^用户/, "你").replace(/^默认规则：/, "");
}

export function toReviewDisplaySentence(
  text: string,
  type: UserMemoryType = "identity",
) {
  const base = toDisplaySentence(text, type);
  if (!base) return "";
  return `澜台准备记住：${addressUserForReview(base)}`;
}

function scoreDisplayCandidate(
  text: string,
  type: UserMemoryType,
  source: "summary" | "entry",
  confidence = 0.82,
) {
  let score = source === "entry" ? 10 : 0;
  const raw = text.trim();
  const readable = toDisplaySentence(raw, type);
  if (!readable) return -999;

  if (confidence >= 0.9) score += 12;
  else if (confidence >= 0.78) score += 7;
  else if (confidence < 0.72) score -= 12;

  if (raw.includes("用户") || raw.includes("默认规则")) score += 4;
  if (readable.length >= 8 && readable.length <= 90) score += 8;
  if (readable.length > 140) score -= 12;
  if (looksLikeInternalMemory(raw)) score -= 34;
  if (looksLikeOperationalTrace(raw)) score -= 28;
  if (!hasTypeRelevance(readable, type)) score -= 18;

  if (
    type === "identity" &&
    /(身份|角色|名字|用户是|负责|从事|背景)/.test(readable)
  ) {
    score += 12;
  }
  if (looksLikeAssistantMetaMemory(raw)) score -= 60;
  if (
    type === "workflow" &&
    /(习惯|流程|工作区|验证|构建|同步|优先|处理)/.test(readable)
  ) {
    score += 8;
  }
  if (type === "style" && /(偏好|喜欢|不喜欢|语气|风格|中文)/.test(readable)) {
    score += 8;
  }
  if (
    type === "instruction" &&
    /(要求|默认|不要|必须|需要|以后|每次|规则)/.test(readable)
  ) {
    score += 8;
  }

  return score;
}

export function buildMemoryImpressionView(
  type: UserMemoryType,
  summaryItems: string[],
  entries: UserMemoryEntry[],
) {
  const activeEntries = entries.filter((entry) => entry.status === "active");
  const sourceCounts = {
    manual: activeEntries.filter((entry) => entry.source === "manual").length,
    auto: activeEntries.filter((entry) => entry.source !== "manual").length,
  };

  const candidates = [
    ...activeEntries.map((entry) => ({
      raw: entry.content,
      text: toDisplaySentence(entry.content, type),
      score: scoreDisplayCandidate(
        entry.content,
        type,
        "entry",
        entry.confidence,
      ),
    })),
    ...summaryItems.map((item) => ({
      raw: item,
      text: toDisplaySentence(item, type),
      score: scoreDisplayCandidate(item, type, "summary"),
    })),
  ]
    .filter((item) => item.text && item.score > -20)
    .sort((a, b) => b.score - a.score);

  const orderedTexts = Array.from(new Set(candidates.map((item) => item.text)));

  const lead = orderedTexts[0] || "";
  const chips = Array.from(
    new Set(
      orderedTexts
        .slice(1)
        .map((item) => toDisplayChip(item, type))
        .filter(Boolean),
    ),
  ).slice(0, 4);

  const statsParts: string[] = [];
  if (sourceCounts.manual > 0) {
    statsParts.push(`${sourceCounts.manual} 条手动补充`);
  }
  if (sourceCounts.auto > 0) {
    statsParts.push(`${sourceCounts.auto} 条澜台整理`);
  }

  return {
    lead,
    chips,
    stats: statsParts.join(" · ") || "暂无稳定记忆",
  } satisfies MemoryImpressionCardView;
}

export function buildMemoryOverviewView(
  views: Record<UserMemoryType, MemoryImpressionCardView>,
  labels: Record<UserMemoryType, string>,
) {
  const headline =
    views.identity.lead ||
    views.style.lead ||
    views.workflow.lead ||
    views.instruction.lead ||
    "还在形成更稳定的用户画像";

  const narrative = [
    views.style.lead,
    views.workflow.lead,
    views.instruction.lead,
  ].filter((item) => item && item !== headline);

  const highlights = (Object.keys(views) as UserMemoryType[])
    .map((type) => {
      const view = views[type];
      const value =
        view.chips[0] || toDisplayChip(view.lead, type) || view.stats;
      return value
        ? {
            type,
            label: labels[type],
            value,
          }
        : null;
    })
    .filter((item): item is MemoryOverviewHighlight => Boolean(item));

  const chips = Array.from(
    new Set(
      (Object.keys(views) as UserMemoryType[]).flatMap(
        (type) => views[type].chips,
      ),
    ),
  ).slice(0, 8);

  return {
    headline,
    narrative,
    highlights,
    chips,
  } satisfies MemoryOverviewView;
}

function toneFromStatus(status: string) {
  if (/(异常|错误|失败)/.test(status)) return "error" as const;
  if (/(未配置|部分|等待|未启用|不可用|降级)/.test(status)) return "partial" as const;
  if (/(禁用)/.test(status)) return "disabled" as const;
  return "ready" as const;
}

function backendLabel(value?: string) {
  if (!value) return "";
  const normalized = value.trim().toLowerCase();
  if (normalized === "chroma") return "Chroma";
  if (normalized === "local") return "本地 ONNX";
  if (normalized === "sqlite-exact-cosine") return "SQLite 精确向量";
  if (normalized === "openai") return "OpenAI 兼容";
  if (normalized === "onnx") return "本地 ONNX";
  return value;
}

function overallLabel(overall: MemoryEngineStatus["overall"]) {
  if (overall === "ready") return "已就绪";
  if (overall === "partial") return "部分可用";
  if (overall === "disabled") return "未启用";
  return "异常";
}

export function buildMemoryEngineStatusView(
  snapshot: UserMemorySnapshot,
  layered: LayeredMemorySnapshot,
) {
  const engineStatus = layered.engine_status;
  const structured = engineStatus?.structured_memory;
  const layeredMemory = engineStatus?.layered_memory;
  const vectorIndex = engineStatus?.vector_index;
  const embedding = engineStatus?.embedding;
  const localWorker = engineStatus?.local_worker;
  const pendingCount =
    structured?.pending_entries ?? snapshot.stats.conflict ?? 0;
  const visibleCount = structured?.visible_entries ?? snapshot.stats.total ?? 0;
  const totalCount = structured?.total_entries ?? snapshot.entries.length ?? 0;
  const l0Count = layeredMemory?.l0 ?? layered.stats.l0_conversations;
  const l1Count = layeredMemory?.l1 ?? layered.stats.l1_atoms;
  const l2Count = layeredMemory?.l2 ?? layered.stats.l2_scenes;
  const system2Count =
    layeredMemory?.system2 ?? (layered.stats.system2_models || 0);
  const activeLayerCount = [l0Count, l1Count, l2Count, system2Count].filter(
    (count) => count > 0,
  ).length;

  const rows: MemoryEngineStatusRowView[] = [
    {
      key: "structured",
      label: "结构化记忆",
      status: structured?.status || "等待状态",
      detail: `可见 ${visibleCount} 条 · 待确认 ${pendingCount} 条 · 总计 ${totalCount} 条`,
      metric: String(totalCount),
      metricLabel: "条记忆",
      tone: toneFromStatus(structured?.status || "等待状态"),
    },
    {
      key: "layered",
      label: "分层记忆",
      status: engineStatus ? "已沉淀" : "等待状态",
      detail: `L0 ${l0Count} · L1 ${l1Count} · L2 ${l2Count} · System2 ${system2Count}`,
      metric: String(activeLayerCount),
      metricLabel: "层有内容",
      tone: engineStatus ? "ready" : "partial",
    },
    {
      key: "vector",
      label: "向量检索",
      status: vectorIndex?.status || "未配置",
      detail: vectorIndex?.enabled
        ? `${backendLabel(vectorIndex.backend)} · ${vectorIndex.records || 0} 条 · ${
            vectorIndex.dimensions || "默认"
          } 维 · ${vectorIndex.sources || 0} 个来源${
            vectorIndex.stale_sources ? ` · ${vectorIndex.stale_sources} 个待迁移` : ""
          } · 写入/查询/删除探测通过`
        : vectorIndex?.configured
          ? `语义召回探测失败，已降级为关键词检索${
              vectorIndex.error ? ` · ${vectorIndex.error}` : ""
            }`
          : "未启用语义召回，当前仍可使用结构化与分层记忆",
      metric: vectorIndex?.enabled ? String(vectorIndex.records || 0) : "—",
      metricLabel: vectorIndex?.enabled ? "条语义索引" : "语义索引",
      tone: toneFromStatus(vectorIndex?.status || "未配置"),
    },
    {
      key: "embedding",
      label: "Embedding 引擎",
      status: embedding?.status || "未配置",
      detail: embedding?.enabled
        ? `${backendLabel(embedding.provider)} · ${embedding.model || "默认模型"}${
            embedding.dimensions ? ` · ${embedding.dimensions} 维` : ""
          }`
        : embedding?.configured
          ? `Embedding 模型探测失败${embedding.error ? ` · ${embedding.error}` : ""}`
          : "暂未配置 Embedding 模型",
      metric:
        embedding?.enabled && embedding.dimensions
          ? String(embedding.dimensions)
          : "—",
      metricLabel: embedding?.enabled ? "维向量" : "向量模型",
      tone: toneFromStatus(embedding?.status || "未配置"),
    },
  ];

  if (localWorker?.enabled) {
    rows.push({
      key: "local-worker",
      label: "本地 Worker",
      status: localWorker.status || "已启用",
      detail: localWorker.model || "本地模型已启用",
      metric: "本地",
      metricLabel: "后台理解",
      tone: toneFromStatus(localWorker.status || "已启用"),
    });
  }

  return {
    overall: engineStatus?.overall || "partial",
    overallLabel: overallLabel(engineStatus?.overall || "partial"),
    summary: !engineStatus
      ? "正在读取本机记忆引擎状态。"
      : pendingCount
        ? `记忆主体可用，当前有 ${pendingCount} 条待确认记忆。`
        : "记忆主体可用，当前没有待确认项目。",
    rows,
  } satisfies MemoryEngineStatusView;
}
