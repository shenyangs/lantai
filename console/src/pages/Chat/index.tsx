import {
  LantaiRuntimeWebUI,
  type ILantaiRuntimeWebUIRef,
  type ILantaiRuntimeWebUIOptions,
} from "@/vendor/lantaiChat";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ComponentProps,
  type DragEvent,
  type FC,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { Button, Modal, Popover, Result, Tooltip } from "antd";
import { useAppMessage } from "../../hooks/useAppMessage";
import {
  ApiOutlined,
  ArrowDownOutlined,
  ArrowUpOutlined,
  CloudOutlined,
  DatabaseOutlined,
  ExclamationCircleOutlined,
  LaptopOutlined,
} from "@ant-design/icons";
import {
  SparkCopyLine,
  SparkAttachmentLine,
  SparkHistoryLine,
  SparkMagicWandLine,
} from "@/vendor/lantaiIcons";
import {
  Bell,
  BookOpenText,
  Brain,
  Code2,
  FileText,
  GitBranch,
  Globe,
  Laptop,
  Minimize2,
  MonitorPlay,
  PanelRightOpen,
  Presentation,
  Search,
} from "lucide-react";
import { usePlugins } from "../../plugins/PluginContext";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";
import sessionApi from "./sessionApi";
import defaultConfig, { getDefaultConfig } from "./OptionsPanel/defaultConfig";
import {
  chatApi,
  chatUploadResponseToAttachmentSuccess,
} from "../../api/modules/chat";
import { capabilityApi } from "../../api/modules/capability";
import { workspaceApi } from "../../api/modules/workspace";
import { agentApi } from "../../api/modules/agent";
import { reminderApi } from "../../api/modules/reminder";
import { dailyBriefApi } from "../../api/modules/dailyBrief";
import { timeApi } from "../../api/modules/time";
import { runtimeApi } from "../../api/modules/runtime";
import { getApiUrl } from "../../api/config";
import { buildAuthHeaders } from "../../api/authHeaders";
import { providerApi } from "../../api/modules/provider";
import { toolsApi } from "../../api/modules/tools";
import type { ProviderInfo, ModelInfo } from "../../api/types";
import type {
  ChatRuntimeState,
  ChatSpec,
  ChatContextStats,
  RuntimeEventsResponse,
} from "../../api/types/chat";
import type { DailyBriefHomeSnapshot, TodayBrief } from "../../api/types";
import type {
  CollaborationDraftStageEvent,
  CollaborationDraftStreamEvent,
} from "../../api/types/capability";
import type { ILantaiRuntimeResponse } from "@/vendor/lantaiChatTypes";
import { useTheme } from "../../contexts/ThemeContext";
import { defaultDisplaySettings } from "../../theme/appearance";
import { useAgentStore } from "../../stores/agentStore";
import { useChatAnywhereInput } from "@/vendor/lantaiChat";
import styles from "./index.module.less";
import type { AgentSummary } from "../../api/types/agents";
import {
  setActiveTopicWorkspace,
  updateActiveTopicWorkspaceFromMeta,
} from "../../utils/topicWorkspace";
import {
  filterProductVisibleProviders,
  stripProductHiddenActiveModel,
} from "../../utils/providerVisibility";
import { IconButton } from "@/vendor/lantaiDesign";
import ChatHeaderTitle from "./components/ChatHeaderTitle";
import ChatSessionInitializer from "./components/ChatSessionInitializer";
import { createWelcomeEntryId } from "./welcomeGreetings/state";
import { useWelcomeGreeting } from "./welcomeGreetings/useWelcomeGreeting";
import KnowledgeSourceBanner from "./components/KnowledgeSourceBanner";
import WorkEditSessionBanner from "./components/WorkEditSessionBanner";
import ChatSessionDrawer from "./components/ChatSessionDrawer";
import PinnedSessionStrip from "./components/PinnedSessionStrip";
import DailyBriefHomeShelf from "./components/DailyBriefHomeShelf";
import DailyBriefDiscussionDraft from "./components/DailyBriefDiscussionDraft";
import {
  selectContinuationSuggestions,
  type ContinuationSuggestion,
} from "./continuationSuggestions";
import InlinePlanPanel from "../../components/PlanPanel/InlinePlanPanel";
import QuquResponseCard from "./components/QuquResponseCard";
import ToolCallCard from "./components/ToolCallCard";
import BrowserSessionCard, {
  type BrowserSessionTarget,
} from "./components/BrowserSessionCard";
import BrowserSessionPanel from "./components/BrowserSessionPanel";
import {
  MacUseTargetBanner,
  MacUseTargetDialog,
  type MacUseTargetSelection,
} from "./components/MacUsePicker";
import MacUseToolCard, { isMacUseToolName } from "./components/MacUseToolCard";
import ImageGenerationToolCard from "./components/ImageGenerationToolCard";
import VideoGenerationToolCard from "./components/VideoGenerationToolCard";
import TurnEventPreview from "./components/TurnEventPreview";
import RuntimeEventStreamBridge from "./components/RuntimeEventStreamBridge";
import { buildRuntimeEventSessionPatch } from "./runtimeEventStream";
import PermissionModeSelector from "./components/PermissionModeSelector";
import CapabilityMenuButton from "./components/CapabilityMenuButton";
import ChatArtifactPanel from "./components/ChatArtifactPanel";
import ArtifactEditTargetChip, {
  type ArtifactEditDraft,
} from "./components/ArtifactEditTargetChip";
import type { ArtifactEditTarget } from "./components/artifactExperience";
import {
  clearModelSetupInputDraft,
  clearStoredArtifactEditDraft,
  loadArtifactEditDraft,
  loadModelSetupInputDraft,
  sameComposerDraftOwner,
  saveArtifactEditDraft,
  saveModelSetupInputDraft,
  subscribeComposerContextDrafts,
  type ComposerContextDraft,
} from "./composerContextDrafts";
import { migrateArtifactWorkbenchState } from "./components/artifactWorkbenchState";
import PrivateEditorPanel from "./components/PrivateEditorPanel";
import KdocsComposerContext from "./components/Kdocs/KdocsComposerContext";
import KdocsPicker from "./components/Kdocs/KdocsPicker";
import KdocsMessageReferences from "./components/Kdocs/KdocsMessageReferences";
import KdocsWorkspacePanel from "./components/Kdocs/KdocsWorkspacePanel";
import type { KdocsDocumentItem } from "../../api/types/kdocs";
import { readKdocsDocumentSafely } from "./kdocsRead";
import {
  MAX_KDOCS_REFERENCES,
  documentToKdocsReference,
  kdocsReferenceStorageKey,
  kdocsReferenceToDocument,
  loadKdocsReferences,
  mergeKdocsReference,
  resolveKdocsSubmitDraft,
  saveKdocsReferences,
  snapshotKdocsReferencesForRequest,
  type KdocsReference,
} from "./kdocsReferences";
import { resolveChatRuntimeMessageSource } from "./chatRuntimeMode";
import { LANTAI_RESPONSE_CARD_CODE } from "./chatCardCodes";
import ScheduledTaskDraftCard, {
  isScheduledTaskToolName,
} from "./components/ScheduledTaskDraftCard";
import CustomizationDraftCard, {
  isCustomizationDraftToolName,
} from "./components/CustomizationDraftCard";
import SubAgentToolCard, {
  isSubAgentToolName,
} from "./components/SubAgentToolCard";
import SubAgentInspectorPanel, {
  type SubAgentInspectionTarget,
} from "./components/SubAgentInspectorPanel";
import { collaborationParentChatTarget } from "./collaborationEventStore";
import { resolveToolPresentation } from "./toolPresentation";
import { ApprovalCard } from "../../components/ApprovalCard/ApprovalCard";
import { commandsApi } from "../../api/modules/commands";
import { useApprovalContext } from "../../contexts/ApprovalContext";
import { planApi } from "../../api/modules/plan";
import { getAgentDisplayName } from "../../utils/agentDisplayName";
import {
  formatRoutingDetail,
  formatRoutingModelLabel,
  formatRoutingReason,
  normalizeRoutingSnapshot,
  publishRoutingSnapshot,
  routingSnapshotFromResponse,
  type ChatRoutingSnapshot,
} from "../../utils/routingDecision";
import {
  DEFAULT_CHAT_PERMISSION_PRESET,
  permissionRequestForPreset,
  persistChatPermissionPreset,
  readChatPermissionPreset,
  type ChatPermissionPreset,
} from "./permissionMode";
import { workMethodApi } from "../../api/modules/workMethod";
import { customizationApi } from "../../api/modules/customization";
import type { WorkMethod } from "../../api/types/workMethod";
import type { PrivateEditorAngleProposal } from "../../api/types/privateEditor";
import {
  DEFAULT_WORK_METHODS,
  persistWorkMethodId,
  readPersistedWorkMethodId,
  readWorkMethodId,
} from "./workMethod";

interface ApprovalMessageData {
  requestId: string;
  sessionId: string;
  rootSessionId?: string;
  userId?: string;
  agentId: string;
  toolName: string;
  severity: string;
  findingsCount: number;
  findingsSummary: string;
  toolParams: Record<string, unknown>;
  createdAt: number;
  timeoutSeconds: number | null;
  policyDecision?: Record<string, unknown> | null;
  approvalOptions?: string[];
  sessionRememberable?: boolean;
  rememberable?: boolean;
  permissionScope?: string | null;
  permissionGroupKey?: string | null;
  sourceRunId?: string | null;
  sourceAgentName?: string | null;
}

interface ApprovalGroupData extends ApprovalMessageData {
  requestIds: string[];
  requestCount: number;
}

function sameArtifactEditTarget(
  left: ArtifactEditTarget,
  right: ArtifactEditTarget,
): boolean {
  return (
    left.chat_id === right.chat_id &&
    left.asset_id === right.asset_id &&
    left.work_id === right.work_id &&
    left.edit_path === right.edit_path &&
    left.base_revision.version === right.base_revision.version &&
    left.base_revision.digest === right.base_revision.digest &&
    JSON.stringify(left.selection ?? null) ===
      JSON.stringify(right.selection ?? null)
  );
}

import {
  toDisplayUrl,
  copyText,
  extractCopyableText,
  buildModelError,
  normalizeContentUrls,
  extractUserMessageText,
  extractTextFromMessage,
  extractClipboardTextWithLayout,
  setTextareaValue,
  resolveStableChatId,
  stripAttachmentSubmitMarker,
  ATTACHMENT_ONLY_SUBMIT_MARKER,
  buildFailedChatResponse,
  buildFailedChatPayload,
  type CopyableResponse,
  type RuntimeLoadingBridgeApi,
} from "./utils";
import { resolveCollaborationDraftAnchor } from "./capabilityDraft";
import { consumeRetryDraft } from "./retryDraft";
import { usePreviewErrorBackfeed } from "./hooks/usePreviewErrorBackfeed";
import {
  TOKEN_USAGE_POPOVER_OFFSET,
  TOKEN_USAGE_POPOVER_PLACEMENT,
} from "./tokenUsagePopover";
import { buildPlanPrompt, isPlanCommand } from "./planPrompt";
import { createLatestRequestGate } from "../../utils/latestRequest";
import {
  buildAccountSetupPath,
  buildModelsSetupPath,
  consumeModelSetupDraftSnapshot,
  mergeRestoredModelSetupInput,
  persistModelSetupDraftSnapshot,
} from "../../utils/modelSetupFlow";
import {
  consumeDailyBriefReturnDraft,
  persistDailyBriefReturnDraft,
} from "./dailyBriefReturnDraft";

const CHAT_ATTACHMENT_MAX_MB = 150;
const CHAT_ATTACHMENT_MAX_BYTES = CHAT_ATTACHMENT_MAX_MB * 1024 * 1024;
const CHAT_ATTACHMENT_UPLOAD_TIMEOUT_MS = 3 * 60_000;
const FIRST_TURN_TRANSITION_MS = 320;

const COLLABORATION_DISTILLATION_STEPS = [
  { id: "collect", title: "读取会话范围" },
  { id: "sanitize", title: "过滤敏感与一次性信息" },
  { id: "distill", title: "提炼纠正与稳定方法" },
  { id: "classify", title: "判断沉淀形态" },
  { id: "draft", title: "生成可复核草稿" },
] as const;

type CollaborationProgressStep = {
  id: string;
  title: string;
  detail?: string;
  status: "pending" | "running" | "completed" | "failed";
};

function initialCollaborationProgressSteps(): CollaborationProgressStep[] {
  return COLLABORATION_DISTILLATION_STEPS.map((step, index) => ({
    ...step,
    status: index === 0 ? "running" : "pending",
  }));
}

function collaborationProgressResponse(
  operationId: string,
  steps: CollaborationProgressStep[],
  options: {
    status?: "running" | "completed" | "failed";
    title?: string;
    detail?: string;
    technicalReason?: string;
  } = {},
): ILantaiRuntimeResponse {
  const status = options.status || "running";
  const activeStep =
    [...steps].reverse().find((step) => step.status !== "pending") || steps[0];
  const now = Math.floor(Date.now() / 1000);
  return {
    id: `collaboration-distillation-${operationId}`,
    object: "response",
    status: status === "running" ? "in_progress" : "completed",
    created_at: now,
    completed_at: status === "running" ? null : now,
    output: [
      {
        id: `collaboration-distillation-status-${operationId}`,
        type: "message",
        role: "assistant",
        status: status === "running" ? "in_progress" : "completed",
        content: [{ type: "text", text: "" }],
        metadata: {
          system_action: {
            kind: "collaboration_distillation",
            status,
            show_avatar: true,
            title:
              options.title ||
              (status === "running"
                ? "沈行知正在沉淀这段协作"
                : "这段协作已沉淀为草稿"),
            detail: options.detail || activeStep?.detail || activeStep?.title,
            technical_reason: options.technicalReason,
            steps,
          },
        },
      },
    ],
    error: null,
    usage: null,
  };
}

interface SessionInfo {
  session_id?: string;
  user_id?: string;
  channel?: string;
}

interface CustomWindow extends Window {
  currentSessionId?: string;
  currentUserId?: string;
  currentChannel?: string;
}

declare const window: CustomWindow;

interface EmptyPromptSuggestion {
  label: string;
  prompt: string;
}

const RETIRED_IMAGE_GENERATION_SUGGESTION =
  /(?:生成|绘制|画)(?:一张|一个)?(?:图片|图像|图)|(?:generate|create|draw)\s+(?:an?\s+)?(?:image|picture|illustration)/iu;

export interface CodingChatContext {
  workspaceDir?: string | null;
  activePath?: string | null;
  openPaths?: string[];
  dirtyPaths?: string[];
  stalePaths?: string[];
}

export interface ChatPageProps {
  embedded?: boolean;
  codingMode?: boolean;
  codingWorkspaceDir?: string | null;
  codingWorkspaceGrantId?: string | null;
  codingContext?: CodingChatContext | null;
  codingStateSnapshot?: string | null;
  codingChatId?: string | null;
  codingSessionId?: string | null;
  onCodingChatResolved?: (chat: ChatSpec) => void;
}

export const DEFAULT_EMPTY_PROMPT_SUGGESTIONS: EmptyPromptSuggestion[] = [
  {
    label: "分析一份文档",
    prompt: "读取我接下来附上的文档，提炼关键结论、风险和下一步：",
  },
  {
    label: "制作可编辑演示",
    prompt: "把下面的材料制作成一份可编辑的演示文稿，并检查版式：",
  },
  {
    label: "研究并附来源",
    prompt: "研究下面的问题，核对多个可靠来源，并给出可点击的出处：",
  },
  {
    label: "让浏览器帮我",
    prompt: "打开浏览器，帮我完成这个任务：",
  },
  {
    label: "操作 Mac 应用",
    prompt: "请操作我选中的 Mac 应用完成这个任务：",
  },
  {
    label: "写一段代码",
    prompt: "帮我实现这个功能，并说明关键改动：",
  },
];

const EMPTY_PROMPT_ICONS = [
  FileText,
  Presentation,
  Search,
  Globe,
  Laptop,
  Code2,
] as const;

const CODING_CHAT_STORAGE_PREFIX = "lantai:coding-chat:";
const MAC_USE_TARGET_STORAGE_PREFIX = "lantai:mac-use-target:";

function hashText(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function macUseTargetStorageKey(agentId: string, sessionKey: string) {
  return `${MAC_USE_TARGET_STORAGE_PREFIX}${agentId}:${hashText(sessionKey)}`;
}

function readStoredMacUseTarget(
  agentId: string,
  sessionKey: string,
): MacUseTargetSelection | null {
  try {
    const parsed = JSON.parse(
      localStorage.getItem(macUseTargetStorageKey(agentId, sessionKey)) ||
        "null",
    ) as Partial<MacUseTargetSelection> | null;
    if (
      !parsed?.app_bundle_id ||
      !parsed.window_id ||
      !Number.isInteger(Number(parsed.process_id)) ||
      Number(parsed.process_id) <= 0 ||
      !Number.isFinite(Number(parsed.process_started_at)) ||
      Number(parsed.process_started_at) <= 0
    ) {
      return null;
    }
    if (!/^macsel_[0-9a-f]{48}$/.test(String(parsed.selection_receipt || ""))) {
      return null;
    }
    return {
      app_bundle_id: String(parsed.app_bundle_id).slice(0, 300),
      window_id: String(parsed.window_id).slice(0, 80),
      process_id: Number(parsed.process_id),
      process_started_at: Number(parsed.process_started_at),
      window_title: String(parsed.window_title || "").slice(0, 500),
      owner_name: String(parsed.owner_name || "").slice(0, 200),
      selection_receipt: String(parsed.selection_receipt).slice(0, 128),
    };
  } catch {
    return null;
  }
}

function storeMacUseTarget(
  agentId: string,
  sessionKey: string,
  target: MacUseTargetSelection | null,
) {
  try {
    const key = macUseTargetStorageKey(agentId, sessionKey);
    if (target) localStorage.setItem(key, JSON.stringify(target));
    else localStorage.removeItem(key);
  } catch {
    // Draft selection persistence is best effort.
  }
}

function buildCodingSessionId(agentId: string, workspaceDir?: string | null) {
  const key = `${agentId}:${workspaceDir || "__agent_workspace__"}`;
  return `console:coding:${hashText(key)}`;
}

function buildCodingStorageKey(agentId: string, workspaceDir?: string | null) {
  return `${CODING_CHAT_STORAGE_PREFIX}${agentId}:${hashText(
    workspaceDir || "__agent_workspace__",
  )}`;
}

function readStoredCodingChatId(agentId: string, workspaceDir?: string | null) {
  try {
    return localStorage.getItem(buildCodingStorageKey(agentId, workspaceDir));
  } catch {
    return null;
  }
}

function storeCodingChatId(
  agentId: string,
  workspaceDir: string | null | undefined,
  chatId: string | null,
) {
  try {
    const key = buildCodingStorageKey(agentId, workspaceDir);
    if (chatId) {
      localStorage.setItem(key, chatId);
    } else {
      localStorage.removeItem(key);
    }
  } catch {
    /* ignore storage failures */
  }
}

function matchesChatSelection(
  expectedChatId: string | null | undefined,
  sessionId: string | null | undefined,
  realId: string | null | undefined,
): boolean {
  const expected =
    typeof expectedChatId === "string" ? expectedChatId.trim() : "";
  if (!expected) return true;
  return [sessionId, realId]
    .map((value) => (typeof value === "string" ? value.trim() : ""))
    .some((value) => value === expected);
}

function sessionAliasSet(
  ...ids: Array<string | null | undefined>
): Set<string> {
  const aliases = new Set<string>();
  for (const id of ids) {
    const normalized = typeof id === "string" ? id.trim() : "";
    if (!normalized) continue;
    aliases.add(normalized);
    const realId = sessionApi.getRealIdForSession(normalized);
    if (realId) aliases.add(realId);
  }
  return aliases;
}

function approvalMatchesSessionAliases(
  approval: { root_session_id: string; session_id: string },
  aliases: ReadonlySet<string>,
): boolean {
  if (aliases.size === 0) return false;
  const approvalAliases = sessionAliasSet(
    approval.root_session_id,
    approval.session_id,
  );
  return [...approvalAliases].some((id) => aliases.has(id));
}

function buildCodingContextText(context?: CodingChatContext | null): string {
  if (!context) return "";
  const lines = [
    `workspace: ${context.workspaceDir || "active Lantai workspace"}`,
    `active_file: ${context.activePath || "(none)"}`,
  ];
  if (context.openPaths?.length) {
    lines.push(`open_tabs: ${context.openPaths.slice(0, 20).join(", ")}`);
  }
  if (context.dirtyPaths?.length) {
    lines.push(`dirty_drafts: ${context.dirtyPaths.slice(0, 20).join(", ")}`);
  }
  if (context.stalePaths?.length) {
    lines.push(`stale_on_disk: ${context.stalePaths.slice(0, 20).join(", ")}`);
  }
  return lines.join("\n");
}

export function normalizeEmptyPromptSuggestions(
  raw: unknown,
): EmptyPromptSuggestion[] {
  if (!Array.isArray(raw)) return [];

  return raw
    .map((item) => {
      if (typeof item === "string") {
        const prompt = item.trim();
        return prompt ? { label: prompt, prompt } : null;
      }
      if (!item || typeof item !== "object") return null;

      const record = item as Record<string, unknown>;
      const label = typeof record.label === "string" ? record.label.trim() : "";
      const prompt =
        typeof record.prompt === "string"
          ? record.prompt.trim()
          : typeof record.value === "string"
            ? record.value.trim()
            : label;

      if (!label || !prompt) return null;
      return { label, prompt };
    })
    .filter(
      (item): item is EmptyPromptSuggestion =>
        Boolean(item) &&
        !RETIRED_IMAGE_GENERATION_SUGGESTION.test(
          `${item?.label ?? ""} ${item?.prompt ?? ""}`,
        ),
    );
}

function renderResponseActionIcon(icon: ReactNode, label: string) {
  return (
    <Tooltip title={label} mouseEnterDelay={0.35}>
      <span className={styles.responseActionIcon} aria-label={label}>
        {icon}
      </span>
    </Tooltip>
  );
}

function withDefaultToolRenderer(
  toolRenderConfig: Record<string, FC<any>>,
  agents: AgentSummary[] = [],
  onInspectSubAgent?: (target: SubAgentInspectionTarget) => void,
  onInspectBrowserSession?: (target: BrowserSessionTarget) => void,
  onRequestMacUseTarget?: () => void,
): Record<string, FC<any>> {
  const SubAgentRenderer: FC<any> = (props) => (
    <SubAgentToolCard
      {...props}
      agents={agents}
      onInspect={onInspectSubAgent}
    />
  );
  const ScheduledTaskRenderer: FC<any> = (props) => (
    <ScheduledTaskDraftCard {...props} />
  );
  const CustomizationDraftRenderer: FC<any> = (props) => (
    <CustomizationDraftCard {...props} />
  );
  const BrowserRenderer: FC<any> = (props) => (
    <BrowserSessionCard
      {...props}
      autoOpen
      onInspect={onInspectBrowserSession}
    />
  );
  const MacUseRenderer: FC<any> = (props) => (
    <MacUseToolCard {...props} onRequestTarget={onRequestMacUseTarget} />
  );

  return new Proxy(toolRenderConfig, {
    get(target, property, receiver) {
      if (typeof property !== "string") {
        return Reflect.get(target, property, receiver);
      }

      const PolicyAwareRenderer: FC<any> = (props) => {
        const presentation = resolveToolPresentation(
          property,
          props?.data,
          agents,
        );
        const visibility = String(presentation?.visibility || "");
        const surface = String(presentation?.surface || "");

        if (visibility === "hidden" || surface === "debug") {
          return null;
        }
        if (surface === "subagent" || visibility === "prominent") {
          return <SubAgentRenderer {...props} />;
        }
        if (isScheduledTaskToolName(property)) {
          return <ScheduledTaskRenderer {...props} />;
        }
        if (isCustomizationDraftToolName(property)) {
          return <CustomizationDraftRenderer {...props} />;
        }
        return <ToolCallCard {...props} />;
      };

      if (isSubAgentToolName(property, agents)) {
        return SubAgentRenderer;
      }

      if (isScheduledTaskToolName(property)) {
        return ScheduledTaskRenderer;
      }

      if (isCustomizationDraftToolName(property)) {
        return CustomizationDraftRenderer;
      }

      if (isMacUseToolName(property)) {
        return MacUseRenderer;
      }

      if (
        ["browser_task", "browser_workbench", "browser_use"].includes(property)
      ) {
        return BrowserRenderer;
      }

      if (property === "generate_image") {
        return ImageGenerationToolCard;
      }

      if (property === "generate_video") {
        return VideoGenerationToolCard;
      }

      return Reflect.get(target, property, receiver) ?? PolicyAwareRenderer;
    },
  });
}

interface CommandSuggestion {
  command: string;
  value: string;
  description: string;
}

function messageRequestsHistoryClear(message: unknown): boolean {
  if (!message || typeof message !== "object") return false;
  const metadata = (message as Record<string, unknown>).metadata;
  if (!metadata || typeof metadata !== "object") return false;

  const meta = metadata as Record<string, unknown>;
  if (meta.clear_history === true) return true;

  const nested = meta.metadata;
  return (
    !!nested &&
    typeof nested === "object" &&
    (nested as Record<string, unknown>).clear_history === true
  );
}

function payloadRequestsHistoryClear(payload: unknown): boolean {
  if (!payload || typeof payload !== "object") return false;

  const record = payload as Record<string, unknown>;
  const candidates: unknown[] = [];

  if (record.object === "message") {
    candidates.push(record);
  }

  if (record.object === "response" && Array.isArray(record.output)) {
    candidates.push(...record.output);
  }

  return candidates.some(messageRequestsHistoryClear);
}

function payloadCompletesResponse(payload: unknown): boolean {
  if (!payload || typeof payload !== "object") return false;

  const record = payload as Record<string, unknown>;
  return record.object === "response" && record.status === "completed";
}

function payloadConversationOwnerIds(payload: unknown): string[] {
  if (!payload || typeof payload !== "object") return [];
  const record = payload as Record<string, unknown>;
  const metadata =
    record.metadata && typeof record.metadata === "object"
      ? (record.metadata as Record<string, unknown>)
      : {};
  return Array.from(
    new Set(
      [record.chat_id, record.session_id, metadata.chat_id, metadata.session_id]
        .map((value) => (typeof value === "string" ? value.trim() : ""))
        .filter(Boolean),
    ),
  );
}

function messageFromUnknownErrorPayload(
  payload: Record<string, unknown>,
): string {
  const error = payload.error;
  if (typeof error === "string" && error.trim()) return error.trim();
  if (error && typeof error === "object") {
    const message = (error as Record<string, unknown>).message;
    if (typeof message === "string" && message.trim()) return message.trim();
  }

  const detail = payload.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (detail && typeof detail === "object") {
    const message = (detail as Record<string, unknown>).message;
    if (typeof message === "string" && message.trim()) return message.trim();
  }

  const message = payload.message;
  if (typeof message === "string" && message.trim()) return message.trim();
  return "";
}

function codeFromUnknownErrorPayload(payload: Record<string, unknown>): string {
  for (const value of [payload.error, payload.detail]) {
    if (!value || typeof value !== "object") continue;
    const code = (value as Record<string, unknown>).code;
    if (typeof code === "string" && code.trim()) return code.trim();
  }
  return typeof payload.code === "string" ? payload.code.trim() : "";
}

function normalizeChatStreamPayload(
  payload: Record<string, unknown>,
): Record<string, unknown> {
  if (typeof payload.object === "string" && payload.object.trim()) {
    return payload;
  }
  const message = messageFromUnknownErrorPayload(payload);
  if (!message) return payload;
  return buildFailedChatPayload({
    errorCode: "Chat stream error",
    message,
  });
}

function renderSuggestionLabel(command: string, description: string) {
  return (
    <div className={styles.suggestionLabel}>
      <span className={styles.suggestionCommand}>{command}</span>
      <span className={styles.suggestionDescription}>{description}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DEFAULT_USER_ID = "default";
const DEFAULT_CHANNEL = "console";
const SKILL_INVOCATION_FALLBACK_PROMPT = "请根据当前消息和附件完成任务。";

function buildChatRequestError(error: unknown): Response {
  const isAbortError =
    typeof DOMException !== "undefined" &&
    error instanceof DOMException &&
    error.name === "AbortError";
  const message =
    error instanceof Error && error.message
      ? error.message
      : isAbortError
        ? "Request cancelled"
        : "Failed to send chat request";

  return buildFailedChatResponse({
    statusCode: isAbortError ? 499 : 500,
    errorCode: isAbortError ? "Request cancelled" : "Chat request failed",
    message,
  });
}

async function buildChatHttpErrorResponse(
  response: Response,
): Promise<Response> {
  const fallbackMessage =
    response.statusText || `Chat request failed with HTTP ${response.status}`;
  let message = fallbackMessage;
  let errorCode = `HTTP ${response.status}`;
  try {
    const text = (await response.text()).trim();
    if (text) {
      let payload: unknown = null;
      try {
        payload = JSON.parse(text);
      } catch {
        payload = null;
      }
      if (payload && typeof payload === "object") {
        message =
          messageFromUnknownErrorPayload(payload as Record<string, unknown>) ||
          fallbackMessage;
        errorCode =
          codeFromUnknownErrorPayload(payload as Record<string, unknown>) ||
          errorCode;
      } else {
        message = text;
      }
    }
  } catch {
    message = fallbackMessage;
  }

  return buildFailedChatResponse({
    statusCode: response.status,
    errorCode,
    message,
  });
}

interface ChatTokenUsageCall {
  providerId?: string;
  modelName?: string;
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number | null;
  cacheHitRate: number | null;
  costCny?: number;
  costPoints?: number;
}

interface ChatTokenUsageSnapshot {
  providerId?: string;
  modelName?: string;
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number | null;
  cacheHitRate: number | null;
  totalTokens: number;
  contextTokens: number;
  contextLimit: number | null;
  costCny?: number;
  costPoints?: number;
  calls: ChatTokenUsageCall[];
  updatedAt: number;
}

interface ChatContextSnapshot extends ChatContextStats {
  updatedAt: number;
}

function readUsageNumber(source: unknown, ...keys: string[]): number | null {
  if (!source || typeof source !== "object") return null;
  const record = source as Record<string, unknown>;
  for (const key of keys) {
    const raw = record[key];
    if (raw === undefined || raw === null || raw === "") continue;
    const value = Number(raw);
    if (Number.isFinite(value)) return value;
  }
  return null;
}

function normalizeRate(value: unknown): number | null {
  const rate = Number(value);
  if (!Number.isFinite(rate)) return null;
  if (rate > 1) return rate / 100;
  if (rate < 0) return 0;
  return rate;
}

function normalizeUsageCall(raw: unknown): ChatTokenUsageCall | null {
  if (!raw || typeof raw !== "object") return null;
  const inputTokens =
    readUsageNumber(raw, "input_tokens", "prompt_tokens") ?? 0;
  const outputTokens =
    readUsageNumber(raw, "output_tokens", "completion_tokens") ?? 0;
  const cachedTokens = readUsageNumber(
    raw,
    "cached_tokens",
    "cache_tokens",
    "cache_read_input_tokens",
    "cached_input_tokens",
  );
  const cacheHitRate =
    normalizeRate((raw as Record<string, unknown>).cache_hit_rate) ??
    (cachedTokens !== null && inputTokens > 0
      ? cachedTokens / inputTokens
      : null);
  if (inputTokens <= 0 && outputTokens <= 0 && cachedTokens === null) {
    return null;
  }
  return {
    providerId: String((raw as Record<string, unknown>).provider_id || ""),
    modelName: String((raw as Record<string, unknown>).model_name || ""),
    inputTokens,
    outputTokens,
    cachedTokens,
    cacheHitRate,
    costCny: readUsageNumber(raw, "cost_cny") ?? undefined,
    costPoints: readUsageNumber(raw, "cost_points") ?? undefined,
  };
}

function normalizeTokenUsage(
  raw: unknown,
  fallbackContextLimit?: number | null,
): ChatTokenUsageSnapshot | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const calls = Array.isArray(record.calls)
    ? record.calls
        .map((call) => normalizeUsageCall(call))
        .filter((call): call is ChatTokenUsageCall => Boolean(call))
    : [];
  const fallbackCall = normalizeUsageCall(raw);
  const normalizedCalls =
    calls.length > 0 ? calls : fallbackCall ? [fallbackCall] : [];
  if (normalizedCalls.length === 0) return null;

  const inputTokens =
    readUsageNumber(
      raw,
      "input_tokens",
      "prompt_tokens",
      "total_input_tokens",
    ) ?? normalizedCalls.reduce((sum, call) => sum + call.inputTokens, 0);
  const outputTokens =
    readUsageNumber(
      raw,
      "output_tokens",
      "completion_tokens",
      "total_output_tokens",
    ) ?? normalizedCalls.reduce((sum, call) => sum + call.outputTokens, 0);
  const cachedKnown =
    readUsageNumber(raw, "cached_tokens", "cache_tokens") !== null ||
    normalizedCalls.some((call) => call.cachedTokens !== null);
  const cachedTokens = cachedKnown
    ? (readUsageNumber(raw, "cached_tokens", "cache_tokens") ??
      normalizedCalls.reduce((sum, call) => sum + (call.cachedTokens ?? 0), 0))
    : null;
  const cacheHitRate =
    normalizeRate(record.cache_hit_rate) ??
    (cachedTokens !== null && inputTokens > 0
      ? cachedTokens / inputTokens
      : null);
  const lastCall = normalizedCalls[normalizedCalls.length - 1];
  const contextTokens =
    readUsageNumber(raw, "context_tokens", "current_context_tokens") ??
    lastCall?.inputTokens ??
    inputTokens;
  const contextLimit =
    readUsageNumber(
      raw,
      "context_limit",
      "context_window",
      "context_window_tokens",
      "max_context_tokens",
      "max_input_length",
    ) ??
    fallbackContextLimit ??
    null;

  return {
    providerId: String(record.provider_id || lastCall?.providerId || ""),
    modelName: String(record.model_name || lastCall?.modelName || ""),
    inputTokens,
    outputTokens,
    cachedTokens,
    cacheHitRate,
    totalTokens:
      readUsageNumber(raw, "total_tokens") ?? inputTokens + outputTokens,
    contextTokens,
    contextLimit,
    costCny: readUsageNumber(raw, "cost_cny") ?? undefined,
    costPoints: readUsageNumber(raw, "cost_points") ?? undefined,
    calls: normalizedCalls,
    updatedAt: Date.now(),
  };
}

function formatTokenInteger(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "?";
  }
  return Math.round(value).toLocaleString();
}

function formatTokenCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "?";
  }
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 10_000) return `${(value / 1_000).toFixed(1)}K`;
  return Math.round(value).toLocaleString();
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "?";
  }
  return `${Math.round(value * 100)}%`;
}

function formatCacheTokens(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "未上报"
    : formatTokenCompact(value);
}

function formatCacheRate(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "未上报"
    : formatPercent(value);
}

function getContextPercent(
  contextTokens: number | null,
  contextLimit: number | null,
): number | null {
  if (
    contextTokens === null ||
    contextLimit === null ||
    !Number.isFinite(contextTokens) ||
    !Number.isFinite(contextLimit) ||
    contextLimit <= 0
  ) {
    return null;
  }
  return Math.max(0, Math.min(1, contextTokens / contextLimit));
}

function resolveContextUsagePair(
  usage: ChatTokenUsageSnapshot | null,
  contextStats: ChatContextSnapshot | null,
) {
  const usageIsNewer =
    usage?.contextLimit != null &&
    usage.contextLimit > 0 &&
    (!contextStats || usage.updatedAt >= contextStats.updatedAt);
  if (usageIsNewer && usage) {
    return {
      contextTokens: usage.contextTokens,
      contextLimit: usage.contextLimit,
      sourceLabel: "本轮模型用量",
    };
  }
  return {
    contextTokens:
      contextStats?.estimated_tokens ?? usage?.contextTokens ?? null,
    contextLimit:
      contextStats?.context_policy?.target_input_tokens ??
      contextStats?.max_input_length ??
      contextStats?.effective_window ??
      usage?.contextLimit ??
      null,
    sourceLabel: contextStats ? "本轮清单估算" : "本轮模型用量",
  };
}

function getContextStatus(percent: number | null): string {
  if (percent === null) return "待估算";
  if (percent >= 0.9) return "接近上限";
  if (percent >= 0.72) return "偏高";
  return "正常";
}

function getContextTone(
  percent: number | null,
): "neutral" | "ok" | "warn" | "danger" {
  if (percent === null) return "neutral";
  if (percent >= 0.9) return "danger";
  if (percent >= 0.72) return "warn";
  return "ok";
}

function getContextGuidance(percent: number | null): string {
  if (percent === null) return "等待有效窗口数据";
  if (percent >= 0.9) return "即将达到智能预算，可先压缩或从当前节点创建分支";
  if (percent >= 0.72) return "上下文偏高，可提前整理或把新方向放进分支";
  return "按当前模型、路由和预留空间动态计算";
}

const contextCategoryLabels: Record<string, string> = {
  system: "系统规则",
  tool_schema: "工具说明",
  request_envelope: "请求封装",
  current_message: "当前消息",
  attachment: "附件",
  history: "近期对话",
  summary: "压缩摘要",
  stable_memory: "稳定记忆",
  dynamic_memory: "动态记忆",
  wiki_evidence: "Wiki 证据",
  tool_result: "工具结果",
};

function getContextCountSourceLabel(source?: string): string {
  if (source === "provider") return "供应商实报校准";
  if (source === "manifest_estimate") return "本轮清单估算";
  return "历史状态估算";
}

function getCallLabel(call: ChatTokenUsageCall, index: number): string {
  return call.modelName || call.providerId || `模型调用 ${index + 1}`;
}

export function TokenUsagePopoverContent({
  usage,
  contextStats,
  routing,
  contextWindowTarget,
  onSaveContextWindow,
  onPrepareCompact,
  onCreateBranch,
}: {
  usage: ChatTokenUsageSnapshot | null;
  contextStats: ChatContextSnapshot | null;
  routing: ChatRoutingSnapshot | null;
  contextWindowTarget?: {
    providerId: string;
    modelId: string;
    modelName: string;
  } | null;
  onSaveContextWindow?: (value: number) => Promise<void>;
  onPrepareCompact?: () => void;
  onCreateBranch?: () => void;
}) {
  const [editingContextWindow, setEditingContextWindow] = useState(false);
  const [contextWindowDraft, setContextWindowDraft] = useState("");
  const [confirmContextWindowOpen, setConfirmContextWindowOpen] =
    useState(false);
  const [savingContextWindow, setSavingContextWindow] = useState(false);

  if (!usage && !contextStats && !routing) {
    return (
      <div
        className={styles.tokenPopover}
        data-chat-scroll-chain="1"
        data-empty="1"
      >
        <div className={styles.tokenPopoverEmpty}>暂无本轮 Token 数据</div>
      </div>
    );
  }

  const { contextTokens, contextLimit, sourceLabel } = resolveContextUsagePair(
    usage,
    contextStats,
  );
  const contextPercent = getContextPercent(contextTokens, contextLimit);
  const contextPercentText =
    contextPercent === null ? "--" : `${(contextPercent * 100).toFixed(1)}%`;
  const contextStatus = getContextStatus(contextPercent);
  const contextTone = getContextTone(contextPercent);
  const contextGuidance = getContextGuidance(contextPercent);
  const parsedContextWindow = Number(contextWindowDraft);
  const contextWindowDraftValid =
    Number.isInteger(parsedContextWindow) &&
    parsedContextWindow >= 1_000 &&
    parsedContextWindow <= 10_000_000;
  const beginContextWindowEdit = () => {
    setContextWindowDraft(
      String(
        contextStats?.requested_window ??
          contextStats?.effective_window ??
          128 * 1024,
      ),
    );
    setEditingContextWindow(true);
  };
  const confirmContextWindowSave = async () => {
    if (
      !contextWindowDraftValid ||
      !onSaveContextWindow ||
      !contextWindowTarget
    ) {
      return;
    }
    setSavingContextWindow(true);
    try {
      await onSaveContextWindow(parsedContextWindow);
      setConfirmContextWindowOpen(false);
      setEditingContextWindow(false);
    } finally {
      setSavingContextWindow(false);
    }
  };

  return (
    <div className={styles.tokenPopover} data-chat-scroll-chain="1">
      <div className={styles.tokenPopoverHeader}>
        <div className={styles.tokenPopoverHeading}>
          <span>本轮上下文</span>
          <small>{sourceLabel}</small>
          <span className={styles.tokenContextStatus} data-tone={contextTone}>
            {contextStatus}
          </span>
        </div>
        <div className={styles.tokenContextHeadline}>
          <strong>{formatTokenCompact(contextTokens)}</strong>
          <span>/ {formatTokenCompact(contextLimit)} tokens</span>
          <em>{contextPercentText}</em>
        </div>
        <span
          className={styles.tokenContextBar}
          data-tone={contextTone}
          role="progressbar"
          aria-label="上下文占用"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={
            contextPercent === null
              ? undefined
              : Math.round(contextPercent * 100)
          }
        >
          <i
            style={
              {
                "--token-context-scale":
                  contextPercent === null ? 0 : contextPercent,
              } as CSSProperties
            }
          />
        </span>
        <p className={styles.tokenContextGuidance} data-tone={contextTone}>
          {contextGuidance}
        </p>
      </div>

      {contextPercent !== null &&
      contextPercent >= 0.72 &&
      (onPrepareCompact || onCreateBranch) ? (
        <section className={styles.tokenProtectionActions}>
          <div>
            <strong>上下文保护</strong>
            <span>先整理再继续，避免后续信息被动挤出</span>
          </div>
          <div>
            {onPrepareCompact ? (
              <Button
                type="text"
                size="small"
                icon={<Minimize2 size={13} />}
                onClick={onPrepareCompact}
              >
                准备压缩
              </Button>
            ) : null}
            {onCreateBranch ? (
              <Button
                type="text"
                size="small"
                icon={<GitBranch size={13} />}
                onClick={onCreateBranch}
              >
                创建分支
              </Button>
            ) : null}
          </div>
        </section>
      ) : null}

      {contextStats ? (
        <dl className={styles.tokenPrimaryMetrics}>
          <div>
            <dt>用户上限</dt>
            <dd>{formatTokenCompact(contextStats.requested_window)}</dd>
          </div>
          <div>
            <dt>模型硬上限</dt>
            <dd>
              {formatTokenCompact(
                contextStats.model_window ?? contextStats.effective_window,
              )}
            </dd>
          </div>
          <div>
            <dt>本轮可用输入</dt>
            <dd>
              {formatTokenCompact(
                contextStats.context_policy?.target_input_tokens ??
                  contextStats.max_input_length,
              )}
            </dd>
          </div>
        </dl>
      ) : null}

      {contextStats?.categories?.length ? (
        <section className={styles.tokenContextBreakdown}>
          <div className={styles.tokenSectionHeading}>
            <span>上下文都用在了哪里</span>
            <em>{getContextCountSourceLabel(contextStats.count_source)}</em>
          </div>
          <div className={styles.tokenCategoryGrid}>
            {contextStats.categories.map((category) => (
              <div key={category.key}>
                <span>
                  {contextCategoryLabels[category.key] || category.key}
                </span>
                <strong>{formatTokenCompact(category.estimated_tokens)}</strong>
                <small>{(category.share * 100).toFixed(1)}%</small>
              </div>
            ))}
          </div>
          {contextStats.compaction?.explanation ? (
            <p data-active={contextStats.compaction.active ? "1" : "0"}>
              {contextStats.compaction.explanation}
            </p>
          ) : null}
        </section>
      ) : null}

      {contextStats && contextWindowTarget && onSaveContextWindow ? (
        <section className={styles.tokenContextWindowEditor}>
          <div className={styles.tokenContextWindowEditorHeader}>
            <div>
              <strong>当前模型上下文</strong>
              <span>{contextWindowTarget.modelName}</span>
            </div>
            {!editingContextWindow ? (
              <Button type="text" size="small" onClick={beginContextWindowEdit}>
                修改用户上限
              </Button>
            ) : null}
          </div>
          {editingContextWindow ? (
            <div className={styles.tokenContextWindowEditorForm}>
              <label htmlFor="model-context-window-input">用户上下文上限</label>
              <div>
                <input
                  id="model-context-window-input"
                  type="number"
                  aria-label="用户上下文上限"
                  min={1_000}
                  max={10_000_000}
                  step={1_024}
                  value={contextWindowDraft}
                  onChange={(event) =>
                    setContextWindowDraft(event.target.value)
                  }
                />
                <span>tokens</span>
              </div>
              <p>仅保存到当前模型；模型或路由硬上限仍会自动约束。</p>
              <div className={styles.tokenContextWindowEditorActions}>
                <Button
                  type="text"
                  size="small"
                  onClick={() => setEditingContextWindow(false)}
                >
                  取消
                </Button>
                <Button
                  type="primary"
                  size="small"
                  disabled={!contextWindowDraftValid}
                  onClick={() => setConfirmContextWindowOpen(true)}
                >
                  保存上下文上限
                </Button>
              </div>
            </div>
          ) : null}
          <Modal
            open={confirmContextWindowOpen}
            title={`确认修改 ${contextWindowTarget.modelName} 的上下文上限`}
            okText="我了解风险，确认保存"
            cancelText="取消"
            confirmLoading={savingContextWindow}
            maskClosable={false}
            keyboard={!savingContextWindow}
            onCancel={() => {
              if (!savingContextWindow) setConfirmContextWindowOpen(false);
            }}
            onOk={() => void confirmContextWindowSave()}
          >
            <div className={styles.tokenContextWindowWarning}>
              <strong>这是高级设置，请确认你了解该模型和当前接入路由。</strong>
              <p>
                错误设置可能导致请求失败、内容截断、延迟或费用上升。设置高于模型或路由硬上限时，澜台仍会按真实上限收紧；设置过低则会减少可保留的对话内容。
              </p>
              <dl>
                <div>
                  <dt>保存范围</dt>
                  <dd>仅 {contextWindowTarget.modelName}</dd>
                </div>
                <div>
                  <dt>新的用户上限</dt>
                  <dd>{parsedContextWindow.toLocaleString()} tokens</dd>
                </div>
              </dl>
            </div>
          </Modal>
        </section>
      ) : null}

      {routing ? (
        <section className={styles.tokenRoutingPanel}>
          <div className={styles.tokenSectionHeading}>
            <span>模型路由</span>
            <em>
              {routing.previousRouteInherited ? "沿用上一轮" : "当前轮次"}
            </em>
          </div>
          <strong className={styles.tokenRoutingModel}>
            {formatRoutingModelLabel(routing)}
          </strong>
          {formatRoutingReason(routing) ? (
            <p>{formatRoutingReason(routing)}</p>
          ) : null}
          {routing.fallbackReason ? <p>{routing.fallbackReason}</p> : null}
        </section>
      ) : null}

      {contextStats || routing ? (
        <details className={styles.tokenDisclosure}>
          <summary>
            <span>技术明细</span>
            <small>消息、压缩与路由</small>
          </summary>
          <div className={styles.tokenDetailGrid}>
            {contextStats ? (
              <>
                <div>
                  <span>消息</span>
                  <strong>
                    {formatTokenCompact(contextStats.total_messages)}
                  </strong>
                </div>
                <div>
                  <span>消息 Token</span>
                  <strong>
                    {formatTokenCompact(contextStats.messages_tokens)}
                  </strong>
                </div>
                <div>
                  <span>压缩摘要</span>
                  <strong>
                    {formatTokenCompact(contextStats.compressed_summary_tokens)}
                  </strong>
                </div>
                <div>
                  <span>模型能力</span>
                  <strong>
                    {contextStats.context_profile_status === "verified"
                      ? "已验证"
                      : "待验证"}
                  </strong>
                </div>
                <div>
                  <span>输出与思考预留</span>
                  <strong>
                    {formatTokenCompact(
                      contextStats.context_policy?.output_reserve,
                    )}
                  </strong>
                </div>
                <div>
                  <span>安全空间</span>
                  <strong>
                    {formatTokenCompact(
                      contextStats.context_policy?.safety_margin,
                    )}
                  </strong>
                </div>
                <div>
                  <span>上下文模式</span>
                  <strong>
                    {contextStats.context_policy?.mode === "max"
                      ? "最大"
                      : contextStats.context_policy?.mode === "economy"
                        ? "经济"
                        : "智能"}
                  </strong>
                </div>
                <div>
                  <span>输入计数来源</span>
                  <strong>{usage ? "供应商实报" : "历史估算"}</strong>
                </div>
              </>
            ) : null}
            {routing?.mode ? (
              <div>
                <span>路由模式</span>
                <strong>{routing.mode}</strong>
              </div>
            ) : null}
            {routing?.complexity ? (
              <div>
                <span>复杂度</span>
                <strong>{routing.complexity}</strong>
              </div>
            ) : null}
            {routing?.estimatedContextTokens !== undefined ? (
              <div>
                <span>预计上下文</span>
                <strong>
                  {formatTokenCompact(routing.estimatedContextTokens)}
                </strong>
              </div>
            ) : null}
            {routing?.estimatedCostPoints !== undefined ? (
              <div>
                <span>预计点数</span>
                <strong>
                  {formatTokenCompact(routing.estimatedCostPoints)}
                </strong>
              </div>
            ) : null}
          </div>
        </details>
      ) : null}

      {usage ? (
        <details className={styles.tokenDisclosure}>
          <summary>
            <span>本轮消耗</span>
            <small>{usage.calls.length} 次模型调用</small>
          </summary>
          <div className={styles.tokenTableWrap}>
            <table className={styles.tokenTable}>
              <thead>
                <tr>
                  <th>调用方</th>
                  <th>↑输入</th>
                  <th>↧缓存</th>
                  <th>命中率</th>
                  <th>↓输出</th>
                </tr>
              </thead>
              <tbody>
                {usage.calls.map((call, index) => {
                  const label = getCallLabel(call, index);
                  return (
                    <tr
                      key={`${
                        call.modelName || call.providerId || "call"
                      }-${index}`}
                    >
                      <td>
                        <span className={styles.tokenCallCell} title={label}>
                          <span className={styles.tokenCallIndex}>
                            {index + 1}
                          </span>
                          <span className={styles.tokenCallName}>{label}</span>
                        </span>
                      </td>
                      <td>{formatTokenCompact(call.inputTokens)}</td>
                      <td>{formatCacheTokens(call.cachedTokens)}</td>
                      <td>{formatCacheRate(call.cacheHitRate)}</td>
                      <td>{formatTokenCompact(call.outputTokens)}</td>
                    </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr>
                  <td>合计</td>
                  <td>{formatTokenCompact(usage.inputTokens)}</td>
                  <td>{formatCacheTokens(usage.cachedTokens)}</td>
                  <td>{formatCacheRate(usage.cacheHitRate)}</td>
                  <td>{formatTokenCompact(usage.outputTokens)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
        </details>
      ) : (
        <div className={styles.tokenPopoverFootnote}>
          暂无本轮 Token 消耗明细
        </div>
      )}
    </div>
  );
}

function ChatTokenUsageMeter({
  usage,
  running,
  contextStats,
  routing,
  contextWindowTarget,
  onSaveContextWindow,
  onPrepareCompact,
  onCreateBranch,
  onOpenChange,
}: {
  usage: ChatTokenUsageSnapshot | null;
  running: boolean;
  contextStats: ChatContextSnapshot | null;
  routing: ChatRoutingSnapshot | null;
  contextWindowTarget?: {
    providerId: string;
    modelId: string;
    modelName: string;
  } | null;
  onSaveContextWindow?: (value: number) => Promise<void>;
  onPrepareCompact?: () => void;
  onCreateBranch?: () => void;
  onOpenChange?: (open: boolean) => void;
}) {
  const { contextTokens, contextLimit } = resolveContextUsagePair(
    usage,
    contextStats,
  );
  const contextPercent = getContextPercent(contextTokens, contextLimit);
  const progress = contextPercent === null ? 0 : contextPercent * 100;
  const label =
    contextPercent === null
      ? usage || contextStats
        ? formatTokenCompact(contextTokens)
        : running
          ? "..."
          : "--"
      : String(Math.round(progress));

  return (
    <Popover
      placement={TOKEN_USAGE_POPOVER_PLACEMENT}
      trigger={["hover", "click"]}
      arrow={false}
      autoAdjustOverflow={{ adjustX: 1, adjustY: 1 }}
      align={{ offset: TOKEN_USAGE_POPOVER_OFFSET }}
      rootClassName={styles.tokenPopoverOverlay}
      overlayClassName={styles.tokenPopoverOverlay}
      onOpenChange={onOpenChange}
      content={
        <TokenUsagePopoverContent
          usage={usage}
          contextStats={contextStats}
          routing={routing}
          contextWindowTarget={contextWindowTarget}
          onSaveContextWindow={onSaveContextWindow}
          onPrepareCompact={onPrepareCompact}
          onCreateBranch={onCreateBranch}
        />
      }
    >
      <button
        type="button"
        className={styles.tokenMeterButton}
        data-running={running ? "1" : "0"}
        data-chat-context-meter="1"
        style={
          {
            "--token-meter-progress": `${Math.min(100, progress)}%`,
          } as CSSProperties
        }
        aria-label="查看上下文占用与保护选项"
      >
        <span>{label}</span>
      </button>
    </Popover>
  );
}

function ResponseTokenSummary({
  usage,
  routing,
}: {
  usage: ChatTokenUsageSnapshot | null;
  routing: ChatRoutingSnapshot | null;
}) {
  const routingDetail = formatRoutingDetail(routing);
  return (
    <span className={styles.responseTokenSummary} data-chat-token-usage="1">
      {usage ? (
        <>
          <span>
            <ArrowUpOutlined />
            {formatTokenInteger(usage.inputTokens)}
          </span>
          <span>
            <ArrowDownOutlined />
            {formatTokenInteger(usage.outputTokens)}
          </span>
          {usage.cacheHitRate !== null ? (
            <span>
              <DatabaseOutlined />
              {formatPercent(usage.cacheHitRate)}
            </span>
          ) : null}
        </>
      ) : null}
      {routing ? (
        <span>
          <Brain size={13} strokeWidth={2} />
          <span title={routingDetail || undefined}>
            {formatRoutingModelLabel(routing)}
            {routing.previousRouteInherited ? " · 沿用" : ""}
          </span>
        </span>
      ) : null}
    </span>
  );
}

function normalizeOutgoingText(text: string): string {
  const stripped = stripAttachmentSubmitMarker(text);
  return stripped.trim().length === 0 ? "" : stripped;
}

function normalizeOutgoingContentPart(part: any): any {
  const normalized = normalizeContentUrls(part);
  if (normalized?.type === "text" && typeof normalized.text === "string") {
    return {
      ...normalized,
      text: normalizeOutgoingText(normalized.text),
    };
  }
  return normalized;
}

function normalizeOutgoingMessage(message: any): any {
  if (!message || typeof message !== "object") return message;

  if (typeof message.content === "string") {
    return {
      ...message,
      content: normalizeOutgoingText(message.content),
    };
  }

  if (Array.isArray(message.content)) {
    return {
      ...message,
      content: message.content
        .map(normalizeOutgoingContentPart)
        .filter(
          (part: any) =>
            !(
              part?.type === "text" &&
              typeof part.text === "string" &&
              part.text.trim().length === 0
            ),
        ),
    };
  }

  return message;
}

function buildSkillInvocationText(skillName: string, text: string): string {
  const userText = normalizeOutgoingText(text).trim();
  return `/[${skillName}] ${userText || SKILL_INVOCATION_FALLBACK_PROMPT}`;
}

function messageStartsWithSlashCommand(message: any): boolean {
  const content = message?.content;
  if (typeof content === "string") {
    return normalizeOutgoingText(content).trimStart().startsWith("/");
  }
  if (Array.isArray(content)) {
    const textPart = content.find(
      (part: any) => part?.type === "text" && typeof part.text === "string",
    );
    return textPart
      ? normalizeOutgoingText(textPart.text).trimStart().startsWith("/")
      : false;
  }
  return false;
}

function injectSkillInvocation(message: any, skillName: string | null): any {
  if (!skillName || !message || typeof message !== "object") return message;

  if (typeof message.content === "string") {
    return {
      ...message,
      content: buildSkillInvocationText(skillName, message.content),
    };
  }

  if (Array.isArray(message.content)) {
    let injected = false;
    const content = message.content.map((part: any) => {
      if (!injected && part?.type === "text" && typeof part.text === "string") {
        injected = true;
        return {
          ...part,
          text: buildSkillInvocationText(skillName, part.text),
        };
      }
      return part;
    });

    if (!injected) {
      content.unshift({
        type: "text",
        text: buildSkillInvocationText(skillName, ""),
      });
    }

    return {
      ...message,
      content,
    };
  }

  return message;
}

function hasFileDrag(event: DragEvent<HTMLElement>): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes("Files");
}

function listDraggedFiles(event: DragEvent<HTMLElement>): File[] {
  return Array.from(event.dataTransfer?.files ?? []).filter(Boolean);
}

// ---------------------------------------------------------------------------
// Custom hooks
// ---------------------------------------------------------------------------

/** Handle IME composition events to prevent premature Enter key submission. */
function useIMEComposition(isChatActive: () => boolean) {
  const isComposingRef = useRef(false);

  useEffect(() => {
    const handleCompositionStart = () => {
      if (!isChatActive()) return;
      isComposingRef.current = true;
    };

    const handleCompositionEnd = () => {
      if (!isChatActive()) return;
      // Small delay for Safari on macOS, which fires keydown after
      // compositionend within the same event loop tick.  Keep this as
      // short as possible so fast typists who hit Space+Enter in quick
      // succession are not blocked.
      setTimeout(() => {
        isComposingRef.current = false;
      }, 50);
    };

    const suppressImeEnter = (e: KeyboardEvent) => {
      if (!isChatActive()) return;
      const target = e.target as HTMLElement;
      if (target?.tagName === "TEXTAREA" && e.key === "Enter" && !e.shiftKey) {
        // e.isComposing is the standard flag; isComposingRef covers the
        // post-compositionend grace period needed by Safari.
        if (isComposingRef.current || (e as any).isComposing) {
          e.stopPropagation();
          e.stopImmediatePropagation();
          e.preventDefault();
          return false;
        }
      }
    };

    document.addEventListener("compositionstart", handleCompositionStart, true);
    document.addEventListener("compositionend", handleCompositionEnd, true);
    // Listen on both keydown (Safari) and keypress (legacy) in capture phase.
    document.addEventListener("keydown", suppressImeEnter, true);
    document.addEventListener("keypress", suppressImeEnter, true);

    return () => {
      document.removeEventListener(
        "compositionstart",
        handleCompositionStart,
        true,
      );
      document.removeEventListener(
        "compositionend",
        handleCompositionEnd,
        true,
      );
      document.removeEventListener("keydown", suppressImeEnter, true);
      document.removeEventListener("keypress", suppressImeEnter, true);
    };
  }, [isChatActive]);

  return isComposingRef;
}

type ActiveModelCapabilities = {
  activeModelKey: string | null;
  activeProviderId: string | null;
  activeModelId: string | null;
  activeModelName: string | null;
  supportsMultimodal: boolean;
  supportsImage: boolean;
  supportsVideo: boolean;
  supportsDeepThinking: boolean;
  deepThinkingFixed: boolean;
  reasoningCapabilityResolved: boolean;
};

const UNRESOLVED_ACTIVE_MODEL_CAPABILITIES: ActiveModelCapabilities = {
  activeModelKey: null,
  activeProviderId: null,
  activeModelId: null,
  activeModelName: null,
  supportsMultimodal: false,
  supportsImage: false,
  supportsVideo: false,
  supportsDeepThinking: false,
  deepThinkingFixed: false,
  reasoningCapabilityResolved: false,
};

/** Fetch and track multimodal capabilities for the active model. */
function useMultimodalCapabilities(
  refreshKey: number,
  locationPathname: string,
  isChatActive: () => boolean,
  selectedAgent: string,
) {
  const [multimodalCaps, setMultimodalCaps] = useState<ActiveModelCapabilities>(
    UNRESOLVED_ACTIVE_MODEL_CAPABILITIES,
  );
  const capabilityGenerationRef = useRef(0);

  const updateCapsIfChanged = useCallback((next: ActiveModelCapabilities) => {
    setMultimodalCaps((prev) =>
      prev.activeModelKey === next.activeModelKey &&
      prev.activeProviderId === next.activeProviderId &&
      prev.activeModelId === next.activeModelId &&
      prev.activeModelName === next.activeModelName &&
      prev.supportsMultimodal === next.supportsMultimodal &&
      prev.supportsImage === next.supportsImage &&
      prev.supportsVideo === next.supportsVideo &&
      prev.supportsDeepThinking === next.supportsDeepThinking &&
      prev.deepThinkingFixed === next.deepThinkingFixed &&
      prev.reasoningCapabilityResolved === next.reasoningCapabilityResolved
        ? prev
        : next,
    );
  }, []);

  const invalidateCapabilities = useCallback(() => {
    capabilityGenerationRef.current += 1;
    setMultimodalCaps((previous) =>
      previous.reasoningCapabilityResolved
        ? { ...previous, reasoningCapabilityResolved: false }
        : previous,
    );
  }, []);

  const fetchMultimodalCaps = useCallback(async () => {
    const generation = capabilityGenerationRef.current;
    const commitCaps = (next: ActiveModelCapabilities) => {
      if (capabilityGenerationRef.current === generation) {
        updateCapsIfChanged(next);
      }
    };
    const noCaps: ActiveModelCapabilities = {
      activeModelKey: null,
      activeProviderId: null,
      activeModelId: null,
      activeModelName: null,
      supportsMultimodal: false,
      supportsImage: false,
      supportsVideo: false,
      supportsDeepThinking: false,
      deepThinkingFixed: false,
      reasoningCapabilityResolved: true,
    };
    try {
      const [providers, activeModels] = await Promise.all([
        providerApi.listProviders(),
        providerApi.getActiveModels({
          scope: "effective",
          agent_id: selectedAgent,
        }),
      ]);
      const visibleActiveModels = stripProductHiddenActiveModel(activeModels);
      const activeProviderId = visibleActiveModels?.active_llm?.provider_id;
      const activeModelId = visibleActiveModels?.active_llm?.model;
      if (!activeProviderId || !activeModelId) {
        commitCaps(noCaps);
        return;
      }
      const provider = filterProductVisibleProviders(
        providers as ProviderInfo[],
      ).find((p) => p.id === activeProviderId);
      if (!provider) {
        commitCaps(noCaps);
        return;
      }
      const allModels: ModelInfo[] = [
        ...(provider.models ?? []),
        ...(provider.extra_models ?? []),
      ];
      const model = allModels.find((m) => m.id === activeModelId);
      const reasoning = await providerApi
        .getReasoningCapability(activeProviderId, activeModelId)
        .catch(() => null);
      commitCaps({
        activeModelKey: `${activeProviderId}\u0000${activeModelId}`,
        activeProviderId,
        activeModelId,
        activeModelName: model?.name || model?.id || activeModelId,
        supportsMultimodal: model?.supports_multimodal ?? false,
        supportsImage: model?.supports_image ?? false,
        supportsVideo: model?.supports_video ?? false,
        supportsDeepThinking: reasoning?.supported ?? false,
        deepThinkingFixed: reasoning?.fixed ?? false,
        reasoningCapabilityResolved: true,
      });
    } catch {
      commitCaps(noCaps);
    }
  }, [selectedAgent, updateCapsIfChanged]);

  // A different agent may resolve to a different provider/model. Invalidate
  // immediately so no request can inherit capabilities from the old agent.
  useEffect(() => {
    invalidateCapabilities();
  }, [selectedAgent, invalidateCapabilities]);

  // Route and ordinary refreshes keep the last confirmed capability while a
  // same-model refresh is in flight. This keeps transport state aligned with
  // the still-visible control and avoids duplicate mount requests.
  useEffect(() => {
    if (isChatActive()) {
      fetchMultimodalCaps();
    }
  }, [locationPathname, fetchMultimodalCaps, isChatActive, refreshKey]);

  // Listen for model-switched event from ModelSelector
  useEffect(() => {
    const handler = () => {
      invalidateCapabilities();
      fetchMultimodalCaps();
    };
    window.addEventListener("model-switched", handler);
    return () => window.removeEventListener("model-switched", handler);
  }, [fetchMultimodalCaps, invalidateCapabilities]);

  return multimodalCaps;
}

function useMessageHistoryNavigation(
  chatRef: React.RefObject<ILantaiRuntimeWebUIRef | null>,
  isChatActive: () => boolean,
  isComposingRef: React.RefObject<boolean>,
) {
  const historyIndexRef = useRef<number>(-1);
  const draftRef = useRef<string>("");

  /** Cached user messages to avoid re-computing on every keydown */
  const userMessagesCacheRef = useRef<string[]>([]);
  const cachedMessageCountRef = useRef<number>(0);

  const getUserMessagesWithText = useCallback((): string[] => {
    if (!chatRef.current?.messages?.getMessages) return [];

    const allMessages = chatRef.current.messages.getMessages();
    if (!Array.isArray(allMessages)) return [];

    const currentCount = allMessages.length;
    if (
      userMessagesCacheRef.current.length > 0 &&
      cachedMessageCountRef.current === currentCount
    ) {
      return userMessagesCacheRef.current;
    }

    const userMessages = allMessages
      .filter((msg) => msg.role === "user")
      .map((msg) => extractTextFromMessage(msg))
      .filter((text) => text.trim().length > 0);

    userMessagesCacheRef.current = userMessages;
    cachedMessageCountRef.current = currentCount;

    return userMessages;
  }, [chatRef]);

  interface MessageResult {
    index: number;
    text: string;
  }

  const findMessageInDirection = (
    messages: string[],
    startIndex: number,
    direction: 1 | -1,
  ): MessageResult | null => {
    const MAX_LOOKUP = 100;
    let lookupIndex = startIndex;
    let steps = 0;

    while (
      lookupIndex >= 0 &&
      lookupIndex < messages.length &&
      steps < MAX_LOOKUP
    ) {
      const messageText = messages[messages.length - 1 - lookupIndex];
      if (messageText) {
        return { index: lookupIndex, text: messageText };
      }
      lookupIndex += direction;
      steps += 1;
    }

    return null;
  };

  const isSuggestionPopupOpen = (textarea: HTMLTextAreaElement): boolean =>
    textarea.value.startsWith("/");

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!isChatActive()) return;

      const target = e.target as HTMLElement;
      const isChatSender =
        target?.tagName === "TEXTAREA" &&
        target?.closest('[class*="sender"]') !== null;

      if (!isChatSender) return;
      if (isComposingRef.current || (e as any).isComposing) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;

      const textarea = target as HTMLTextAreaElement;
      const hasSelection = textarea.selectionStart !== textarea.selectionEnd;
      if (hasSelection) return;

      const userMessages = getUserMessagesWithText();

      if (e.key === "ArrowUp") {
        if (isSuggestionPopupOpen(textarea)) return;

        const cursorPosition = textarea.selectionStart || 0;
        const textBeforeCursor = textarea.value.substring(0, cursorPosition);
        const lineBreaks = textBeforeCursor.split("\n").length - 1;
        if (lineBreaks > 0) return;

        if (userMessages.length === 0) return;

        if (historyIndexRef.current === -1) {
          draftRef.current = textarea.value;
        }

        const startIndex = historyIndexRef.current + 1;
        const messageText = findMessageInDirection(userMessages, startIndex, 1);

        if (messageText) {
          e.preventDefault();
          historyIndexRef.current = messageText.index;
          setTextareaValue(textarea, messageText.text);
        }
      } else if (e.key === "ArrowDown") {
        if (historyIndexRef.current < 0) return;

        const cursorPosition = textarea.selectionStart || 0;
        const textAfterCursor = textarea.value.substring(cursorPosition);
        if (textAfterCursor.includes("\n")) return;

        const startIndex = historyIndexRef.current - 1;
        const messageText = findMessageInDirection(
          userMessages,
          startIndex,
          -1,
        );

        if (messageText) {
          e.preventDefault();
          historyIndexRef.current = messageText.index;
          setTextareaValue(textarea, messageText.text);
        } else {
          e.preventDefault();
          historyIndexRef.current = -1;
          setTextareaValue(textarea, draftRef.current);
        }
      }
    };

    const handleFocus = (e: FocusEvent) => {
      const target = e.target as HTMLElement;
      const isChatSender =
        target?.tagName === "TEXTAREA" &&
        target?.closest('[class*="sender"]') !== null;

      if (isChatSender) {
        historyIndexRef.current = -1;
        draftRef.current = "";
      }
    };

    document.addEventListener("keydown", handleKeyDown, true);
    document.addEventListener("focusin", handleFocus, true);

    return () => {
      document.removeEventListener("keydown", handleKeyDown, true);
      document.removeEventListener("focusin", handleFocus, true);
    };
  }, [isChatActive, isComposingRef, getUserMessagesWithText]);
}

function RuntimeLoadingBridge({
  bridgeRef,
}: {
  bridgeRef: { current: RuntimeLoadingBridgeApi | null };
}) {
  const { setLoading, getLoading } = useChatAnywhereInput(
    (value) =>
      ({
        setLoading: value.setLoading,
        getLoading: value.getLoading,
      }) as RuntimeLoadingBridgeApi,
  );

  useEffect(() => {
    if (!setLoading || !getLoading) {
      bridgeRef.current = null;
      return;
    }

    bridgeRef.current = {
      setLoading,
      getLoading,
    };

    return () => {
      if (bridgeRef.current?.setLoading === setLoading) {
        bridgeRef.current = null;
      }
    };
  }, [getLoading, setLoading, bridgeRef]);

  return null;
}

export default function ChatPage({
  embedded = false,
  codingMode = false,
  codingWorkspaceDir = null,
  codingWorkspaceGrantId = null,
  codingContext = null,
  codingStateSnapshot = null,
  codingChatId = null,
  codingSessionId = null,
  onCodingChatResolved,
}: ChatPageProps = {}) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const modelSetupReturnTo = useMemo(
    () => {
      const params = new URLSearchParams(location.search);
      if (!params.has("briefDraft")) return `${location.pathname}${location.search}`;
      params.delete("briefDraft");
      const search = params.toString();
      return `${location.pathname}${search ? `?${search}` : ""}`;
    },
    [location.pathname, location.search],
  );
  const briefDraftId = new URLSearchParams(location.search).get("briefDraft");
  const { isDark, displaySettings: themeDisplaySettings } = useTheme();
  const displaySettings = themeDisplaySettings ?? defaultDisplaySettings;
  const routeChatId = useMemo(() => {
    const match = location.pathname.match(/^\/chat\/(.+)$/);
    return match?.[1];
  }, [location.pathname]);
  const {
    selectedAgent,
    agents = [],
    setLastChatId,
    getLastChatId,
  } = useAgentStore();
  const [embeddedChatId, setEmbeddedChatId] = useState<string | undefined>(
    () =>
      embedded && codingMode
        ? codingChatId ||
          readStoredCodingChatId(selectedAgent, codingWorkspaceDir) ||
          undefined
        : undefined,
  );
  const chatId = embedded ? embeddedChatId : routeChatId;
  const [welcomeEntryId, setWelcomeEntryId] = useState(createWelcomeEntryId);
  const [welcomeEligible, setWelcomeEligible] = useState(false);
  const welcomeSessionIdRef = useRef<string | null>(null);
  const privateEditorProjectId = useMemo(
    () =>
      embedded
        ? null
        : new URLSearchParams(location.search).get("privateEditorProject"),
    [embedded, location.search],
  );
  const [privateEditorPanelOpen, setPrivateEditorPanelOpen] = useState(
    Boolean(privateEditorProjectId),
  );
  useEffect(() => {
    setPrivateEditorPanelOpen(Boolean(privateEditorProjectId));
  }, [privateEditorProjectId]);
  useEffect(() => {
    if (!embedded && chatId) {
      setLastChatId(selectedAgent, chatId);
    }
  }, [chatId, embedded, selectedAgent, setLastChatId]);
  const activeCodingSessionId = useMemo(
    () =>
      codingMode || codingWorkspaceDir
        ? codingSessionId ||
          buildCodingSessionId(selectedAgent, codingWorkspaceDir)
        : "",
    [codingMode, codingSessionId, codingWorkspaceDir, selectedAgent],
  );
  const [showModelPrompt, setShowModelPrompt] = useState(false);
  const { toolRenderConfig } = usePlugins();
  const [refreshKey, setRefreshKey] = useState(0);
  const runtimeLoadingBridgeRef = useRef<RuntimeLoadingBridgeApi | null>(null);
  const { message, modal } = useAppMessage();
  const { approvals } = useApprovalContext();
  const [approvalRequests, setApprovalRequests] = useState<
    Map<string, ApprovalMessageData>
  >(new Map());
  const permissionSessionKey = chatId || activeCodingSessionId || "draft";
  const macUseDesktopAvailable =
    Boolean(window.electronAPI?.isDesktop) &&
    window.electronAPI?.platform === "darwin";
  const [permissionPreset, setPermissionPreset] =
    useState<ChatPermissionPreset>(() =>
      readChatPermissionPreset(selectedAgent, permissionSessionKey),
    );
  const permissionPresetRef = useRef<ChatPermissionPreset>(permissionPreset);
  const [workMethods, setWorkMethods] =
    useState<WorkMethod[]>(DEFAULT_WORK_METHODS);
  const [workMethodId, setWorkMethodId] = useState(() =>
    readWorkMethodId(selectedAgent, permissionSessionKey),
  );
  const workMethodIdRef = useRef(workMethodId);
  const [allowFullPermission, setAllowFullPermission] = useState<
    boolean | null
  >(null);
  const [planEnabled, setPlanEnabled] = useState(false);
  const [planDraftMode, setPlanDraftMode] = useState(false);
  const [goalDraftMode, setGoalDraftMode] = useState(false);
  const [maxContextTokens, setMaxContextTokens] = useState<number | null>(null);
  const [latestTokenUsage, setLatestTokenUsage] =
    useState<ChatTokenUsageSnapshot | null>(null);
  const [latestContextStats, setLatestContextStats] =
    useState<ChatContextSnapshot | null>(null);
  const [latestRouting, setLatestRouting] =
    useState<ChatRoutingSnapshot | null>(null);
  const [todayBrief, setTodayBrief] = useState<TodayBrief | null>(null);
  const [dailyBriefHome, setDailyBriefHome] =
    useState<DailyBriefHomeSnapshot | null>(null);
  const [continuationSuggestions, setContinuationSuggestions] = useState<
    ContinuationSuggestion[]
  >([]);
  const [draftingCapability, setDraftingCapability] = useState(false);
  const capabilityDraftAbortRef = useRef<AbortController | null>(null);
  const [tokenMeterRunning, setTokenMeterRunning] = useState(false);
  const [sessionDrawerOpen, setSessionDrawerOpen] = useState(false);
  const [artifactPanelOpen, setArtifactPanelOpen] = useState(false);
  const [artifactEditDraft, setArtifactEditDraft] =
    useState<ArtifactEditDraft | null>(null);
  const artifactEditDraftRef =
    useRef<ComposerContextDraft<ArtifactEditDraft> | null>(null);
  const composerDraftOwnerRef = useRef({
    agentId: selectedAgent,
    sessionId: permissionSessionKey,
  });
  composerDraftOwnerRef.current = {
    agentId: selectedAgent,
    sessionId: permissionSessionKey,
  };
  const [kdocsPanelOpen, setKdocsPanelOpen] = useState(false);
  const [kdocsPickerOpen, setKdocsPickerOpen] = useState(false);
  const [kdocsRequestedReference, setKdocsRequestedReference] =
    useState<KdocsReference | null>(null);
  const [kdocsRequestedDocument, setKdocsRequestedDocument] =
    useState<KdocsDocumentItem | null>(null);
  const [kdocsReferences, setKdocsReferences] = useState<KdocsReference[]>(() =>
    loadKdocsReferences(selectedAgent, permissionSessionKey),
  );
  const kdocsReferencesRef = useRef(kdocsReferences);
  const kdocsReferenceScope = kdocsReferenceStorageKey(
    selectedAgent,
    permissionSessionKey,
  );
  const kdocsReferenceScopeRef = useRef(kdocsReferenceScope);
  kdocsReferenceScopeRef.current = kdocsReferenceScope;
  const kdocsReferenceOwnerRef = useRef({
    agentId: selectedAgent,
    sessionId: permissionSessionKey,
    scope: kdocsReferenceScope,
  });
  kdocsReferenceOwnerRef.current = {
    agentId: selectedAgent,
    sessionId: permissionSessionKey,
    scope: kdocsReferenceScope,
  };
  const kdocsReadGenerationsRef = useRef(new Map<string, number>());
  const [artifactRequestedPath, setArtifactRequestedPath] = useState<
    string | null
  >(null);
  const [subAgentInspectionTarget, setSubAgentInspectionTarget] =
    useState<SubAgentInspectionTarget | null>(null);
  const [browserSessionTarget, setBrowserSessionTarget] =
    useState<BrowserSessionTarget | null>(null);
  const [macUseTarget, setMacUseTarget] =
    useState<MacUseTargetSelection | null>(() =>
      readStoredMacUseTarget(selectedAgent, permissionSessionKey),
    );
  const [macUsePickerOpen, setMacUsePickerOpen] = useState(false);
  const handleMacUseTargetChange = useCallback(
    (target: MacUseTargetSelection | null) => {
      setMacUseTarget(target);
      storeMacUseTarget(selectedAgent, permissionSessionKey, target);
    },
    [permissionSessionKey, selectedAgent],
  );
  const handleRequestMacUseTarget = useCallback(() => {
    if (!macUseDesktopAvailable || codingMode) return;
    setMacUsePickerOpen(true);
  }, [codingMode, macUseDesktopAvailable]);
  const commitKdocsReferences = useCallback(
    (update: (current: KdocsReference[]) => KdocsReference[]) => {
      const next = update(kdocsReferencesRef.current);
      kdocsReferencesRef.current = next;
      setKdocsReferences(next);
      saveKdocsReferences(selectedAgent, permissionSessionKey, next);
    },
    [permissionSessionKey, selectedAgent],
  );
  const handleAddKdocsDocument = useCallback(
    (
      document: KdocsDocumentItem,
      prepared?: KdocsReference,
      forceRead = false,
    ) => {
      const reference = prepared ?? documentToKdocsReference(document);
      const existing = kdocsReferencesRef.current.find(
        (item) => item.key === reference.key,
      );
      if (
        !existing &&
        kdocsReferencesRef.current.length >= MAX_KDOCS_REFERENCES
      ) {
        message.warning(
          `每条消息最多引用 ${MAX_KDOCS_REFERENCES} 个文档或片段`,
        );
        return;
      }
      if (prepared) {
        commitKdocsReferences((current) =>
          mergeKdocsReference(current, reference),
        );
        return;
      }
      if (
        !forceRead &&
        existing &&
        ["reading", "read", "partial"].includes(existing.read_status)
      ) {
        return;
      }
      commitKdocsReferences((current) =>
        mergeKdocsReference(current, {
          ...reference,
          read_status: "reading",
          error_message: undefined,
        }),
      );
      const readAgentId = selectedAgent;
      const readSessionId = permissionSessionKey;
      const readScope = kdocsReferenceScope;
      const generationKey = `${readScope}\u0000${reference.key}`;
      const generation =
        (kdocsReadGenerationsRef.current.get(generationKey) ?? 0) + 1;
      kdocsReadGenerationsRef.current.set(generationKey, generation);
      void readKdocsDocumentSafely(document).then((result) => {
        if (kdocsReadGenerationsRef.current.get(generationKey) !== generation) {
          return;
        }
        if (kdocsReferenceScopeRef.current !== readScope) {
          const stored = loadKdocsReferences(readAgentId, readSessionId);
          if (!stored.some((item) => item.key === reference.key)) return;
          saveKdocsReferences(
            readAgentId,
            readSessionId,
            mergeKdocsReference(stored, result),
          );
          return;
        }
        commitKdocsReferences((current) =>
          current.some((item) => item.key === reference.key)
            ? mergeKdocsReference(current, result)
            : current,
        );
      });
    },
    [
      commitKdocsReferences,
      kdocsReferenceScope,
      message,
      permissionSessionKey,
      selectedAgent,
    ],
  );
  const handleAddKdocsExcerpt = useCallback(
    (reference: KdocsReference) => {
      const existing = kdocsReferencesRef.current.some(
        (item) => item.key === reference.key,
      );
      if (
        !existing &&
        kdocsReferencesRef.current.length >= MAX_KDOCS_REFERENCES
      ) {
        message.warning(
          `每条消息最多引用 ${MAX_KDOCS_REFERENCES} 个文档或片段`,
        );
        return;
      }
      commitKdocsReferences((current) =>
        mergeKdocsReference(current, reference),
      );
      message.success("已引用选中内容");
    },
    [commitKdocsReferences, message],
  );
  const handleRemoveKdocsReference = useCallback(
    (key: string) => {
      const generationKey = `${kdocsReferenceScope}\u0000${key}`;
      kdocsReadGenerationsRef.current.set(
        generationKey,
        (kdocsReadGenerationsRef.current.get(generationKey) ?? 0) + 1,
      );
      commitKdocsReferences((current) =>
        current.filter((item) => item.key !== key),
      );
    },
    [commitKdocsReferences, kdocsReferenceScope],
  );
  const handleRemoveKdocsDocument = useCallback(
    (driveId: string, fileId: string) => {
      commitKdocsReferences((current) => {
        current
          .filter(
            (item) => item.drive_id === driveId && item.file_id === fileId,
          )
          .forEach((item) => {
            const generationKey = `${kdocsReferenceScope}\u0000${item.key}`;
            kdocsReadGenerationsRef.current.set(
              generationKey,
              (kdocsReadGenerationsRef.current.get(generationKey) ?? 0) + 1,
            );
          });
        return current.filter(
          (item) => item.drive_id !== driveId || item.file_id !== fileId,
        );
      });
    },
    [commitKdocsReferences, kdocsReferenceScope],
  );
  const handleMarkKdocsDocumentUpdated = useCallback(
    (document: KdocsDocumentItem) => {
      commitKdocsReferences((current) =>
        current.map((item) =>
          item.kind === "file" &&
          item.drive_id === document.drive_id &&
          item.file_id === document.file_id
            ? { ...item, read_status: "original_updated" }
            : item,
        ),
      );
    },
    [commitKdocsReferences],
  );
  const handleOpenKdocsDocument = useCallback(
    (document?: KdocsDocumentItem) => {
      setArtifactPanelOpen(false);
      setArtifactRequestedPath(null);
      setSubAgentInspectionTarget(null);
      setBrowserSessionTarget(null);
      setPrivateEditorPanelOpen(false);
      setKdocsRequestedReference(null);
      setKdocsRequestedDocument(document ?? null);
      setKdocsPanelOpen(true);
    },
    [],
  );
  const handleOpenKdocsReference = useCallback(
    (reference: KdocsReference) => {
      handleOpenKdocsDocument(kdocsReferenceToDocument(reference));
      setKdocsRequestedReference(reference);
    },
    [handleOpenKdocsDocument],
  );
  const handleRetryKdocsReference = useCallback(
    (reference: KdocsReference) => {
      if (reference.kind === "excerpt") {
        handleOpenKdocsReference(reference);
        return;
      }
      handleAddKdocsDocument(
        kdocsReferenceToDocument(reference),
        undefined,
        true,
      );
    },
    [handleAddKdocsDocument, handleOpenKdocsReference],
  );
  const kdocsReferencedDocumentKeys = useMemo(
    () =>
      new Set(
        kdocsReferences.map((item) => `${item.drive_id}:${item.file_id}`),
      ),
    [kdocsReferences],
  );
  const privateEditorPanelVisible = Boolean(
    privateEditorProjectId &&
    privateEditorPanelOpen &&
    !artifactPanelOpen &&
    !kdocsPanelOpen &&
    !subAgentInspectionTarget &&
    !browserSessionTarget,
  );
  const rightPanelOpen =
    artifactPanelOpen ||
    kdocsPanelOpen ||
    Boolean(subAgentInspectionTarget) ||
    Boolean(browserSessionTarget) ||
    privateEditorPanelVisible;
  const handleInspectSubAgent = useCallback(
    (target: SubAgentInspectionTarget) => {
      setArtifactPanelOpen(false);
      setArtifactRequestedPath(null);
      setKdocsPanelOpen(false);
      setKdocsRequestedDocument(null);
      setBrowserSessionTarget(null);
      setSubAgentInspectionTarget(target);
    },
    [],
  );
  const handleInspectBrowserSession = useCallback(
    (target: BrowserSessionTarget) => {
      setArtifactPanelOpen(false);
      setArtifactRequestedPath(null);
      setKdocsPanelOpen(false);
      setKdocsRequestedDocument(null);
      setSubAgentInspectionTarget(null);
      if (target.runId) {
        window.sessionStorage.removeItem(
          `lantai:browser-sidecar:dismissed:${target.runId}`,
        );
      }
      setBrowserSessionTarget({ ...target, ownerChatId: chatId || "draft" });
    },
    [chatId],
  );
  useEffect(() => {
    setBrowserSessionTarget((current) =>
      current?.ownerChatId && current.ownerChatId !== (chatId || "draft")
        ? null
        : current,
    );
  }, [chatId]);
  useEffect(() => {
    const runId =
      new URLSearchParams(location.search).get("browser_task") || "";
    if (!runId) return;
    handleInspectBrowserSession({
      runId,
      profileId: "default",
      url: "",
      instruction: "浏览器任务",
      task: null,
    });
  }, [handleInspectBrowserSession, location.search]);
  useEffect(
    () => () => {
      capabilityDraftAbortRef.current?.abort();
    },
    [],
  );
  const handleArtifactLinkClick = useCallback(
    (event: ReactMouseEvent<HTMLDivElement>) => {
      const target =
        event.target instanceof Element ? event.target.closest("a") : null;
      if (!(target instanceof HTMLAnchorElement)) return;
      const href = target.getAttribute("href")?.trim() || "";
      if (!href) return;
      const presentedAsFile = Boolean(target.closest(".lantai-file-link"));
      const looksLikeLocalFile =
        href.startsWith("file://") ||
        /^(?:\/(?:Users|private|tmp|var)\/|\.\.?\/).+\.[a-z0-9_-]+(?::(?:L)?\d+(?:-\d+)?)?$/i.test(
          href,
        );
      if (!presentedAsFile && !looksLikeLocalFile) return;
      event.preventDefault();
      event.stopPropagation();
      setSubAgentInspectionTarget(null);
      setBrowserSessionTarget(null);
      setKdocsPanelOpen(false);
      setKdocsRequestedDocument(null);
      setPrivateEditorPanelOpen(false);
      setArtifactRequestedPath(href);
      setArtifactPanelOpen(true);
    },
    [],
  );
  useEffect(() => {
    const restored = readChatPermissionPreset(
      selectedAgent,
      permissionSessionKey,
    );
    permissionPresetRef.current = restored;
    setPermissionPreset(restored);
  }, [permissionSessionKey, selectedAgent]);

  useEffect(() => {
    const restored = readWorkMethodId(selectedAgent, permissionSessionKey);
    workMethodIdRef.current = restored;
    setWorkMethodId(restored);
  }, [permissionSessionKey, selectedAgent]);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([
      workMethodApi.listWorkMethods().catch(() => DEFAULT_WORK_METHODS),
      customizationApi.getEffectiveState().catch(() => null),
    ]).then(([catalog, customization]) => {
      if (cancelled) return;
      const methods = catalog.length > 0 ? catalog : DEFAULT_WORK_METHODS;
      setWorkMethods(methods);

      // Standard is the model-native loop: the primary model chooses whether
      // to answer or call tools as it works. Other methods are only activated
      // by an explicit user selection, never by a per-message preflight model.
      const persistedMethodId = readPersistedWorkMethodId(
        selectedAgent,
        permissionSessionKey,
      );
      const customizedMethodId =
        typeof customization?.values?.["work_methods.default"] === "string"
          ? String(customization.values["work_methods.default"])
          : "standard";
      const preferredMethodId =
        persistedMethodId === "auto"
          ? "standard"
          : persistedMethodId || customizedMethodId;
      if (methods.some((method) => method.id === preferredMethodId)) {
        workMethodIdRef.current = preferredMethodId;
        setWorkMethodId(preferredMethodId);
      } else {
        workMethodIdRef.current = "standard";
        setWorkMethodId("standard");
      }
    });
    return () => {
      cancelled = true;
    };
  }, [permissionSessionKey, selectedAgent]);

  useEffect(() => {
    let cancelled = false;
    void toolsApi
      .listPermissionProfiles()
      .then((profiles) => {
        if (!cancelled) {
          setAllowFullPermission(
            profiles.some((profile) => profile.id === "full"),
          );
        }
      })
      .catch(() => {
        // The backend remains authoritative if this optional probe is offline.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (allowFullPermission !== false || permissionPreset !== "full") {
      return;
    }
    permissionPresetRef.current = DEFAULT_CHAT_PERMISSION_PRESET;
    setPermissionPreset(DEFAULT_CHAT_PERMISSION_PRESET);
    persistChatPermissionPreset(
      selectedAgent,
      [permissionSessionKey],
      DEFAULT_CHAT_PERMISSION_PRESET,
    );
  }, [
    allowFullPermission,
    permissionPreset,
    permissionSessionKey,
    selectedAgent,
  ]);

  const permissionChangeInFlightRef = useRef(false);
  const handlePermissionPresetChange = useCallback(
    async (nextPreset: ChatPermissionPreset) => {
      const effectivePreset =
        nextPreset === "full" && allowFullPermission === false
          ? DEFAULT_CHAT_PERMISSION_PRESET
          : nextPreset;
      if (
        effectivePreset === permissionPresetRef.current ||
        permissionChangeInFlightRef.current
      ) {
        return;
      }
      permissionChangeInFlightRef.current = true;

      const pendingApproval = approvalRequests.values().next().value as
        | ApprovalMessageData
        | undefined;
      const rootSessionId = String(
        pendingApproval?.rootSessionId ||
          window.currentSessionId ||
          activeCodingSessionId ||
          chatId ||
          "",
      ).trim();
      const userId = String(
        pendingApproval?.userId || window.currentUserId || DEFAULT_USER_ID,
      ).trim();
      const override = permissionRequestForPreset(effectivePreset);

      try {
        let expectedQueueRevision: string | undefined;
        let previewRequests: Array<{
          request_id: string;
          tool_name: string;
          severity: string;
        }> = [];
        if (effectivePreset === "full" && rootSessionId) {
          const preview = await commandsApi.previewSessionPermission({
            sessionId: rootSessionId,
            userId,
          });
          expectedQueueRevision = preview.queue_revision;
          previewRequests = preview.requests;
        }

        if (effectivePreset === "full") {
          const confirmed = await new Promise<boolean>((resolve) => {
            let decided = false;
            const decide = (value: boolean) => {
              if (decided) return;
              decided = true;
              resolve(value);
            };
            modal.confirm({
              title: "将当前对话切换为完全访问？",
              content: (
                <div style={{ display: "grid", gap: 8 }}>
                  <div>
                    仅影响当前对话。后续工具操作可能不再逐次询问，系统硬性限制仍会拦截。
                  </div>
                  {previewRequests.length > 0 ? (
                    <div>
                      <div>
                        确认后会立即放行以下 {previewRequests.length}{" "}
                        项待处理请求：
                      </div>
                      <ul style={{ margin: "6px 0 0", paddingLeft: 20 }}>
                        {previewRequests.map((item) => (
                          <li key={item.request_id}>
                            {item.tool_name}（{item.severity}）
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : (
                    <div>当前没有会被立即放行的待处理请求。</div>
                  )}
                </div>
              ),
              okText: "确认完全访问",
              cancelText: "保持现有权限",
              okButtonProps: { danger: true },
              autoFocusButton: "cancel",
              centered: true,
              onOk: () => decide(true),
              onCancel: () => decide(false),
              afterClose: () => decide(false),
            });
          });
          if (!confirmed) return;
        }

        let resolvedIds = new Set<string>();
        if (rootSessionId) {
          const response = await commandsApi.updateSessionPermission({
            sessionId: rootSessionId,
            userId,
            permissionMode: override.permission_mode,
            permissionProfile: override.permission_profile,
            expectedQueueRevision,
          });
          resolvedIds = new Set(response.resolved_request_ids || []);
        }

        permissionPresetRef.current = effectivePreset;
        setPermissionPreset(effectivePreset);
        persistChatPermissionPreset(
          selectedAgent,
          [
            permissionSessionKey,
            chatId,
            activeCodingSessionId,
            window.currentSessionId,
          ],
          effectivePreset,
        );

        if (resolvedIds.size > 0) {
          setApprovalRequests((previous) => {
            const next = new Map(previous);
            resolvedIds.forEach((requestId) => next.delete(requestId));
            return next;
          });
        }
      } catch (error) {
        console.error("Failed to update the active session permission:", error);
        const status = Number(
          (error as { httpStatus?: number } | null)?.httpStatus || 0,
        );
        message.warning(
          status === 409
            ? "待处理的审批已变化，本次未切换权限，请重新核对"
            : "当前任务权限未能即时刷新，本次未切换",
        );
      } finally {
        permissionChangeInFlightRef.current = false;
      }
    },
    [
      activeCodingSessionId,
      allowFullPermission,
      approvalRequests,
      chatId,
      message,
      modal,
      permissionSessionKey,
      selectedAgent,
    ],
  );

  const handleWorkMethodChange = useCallback(
    (nextMethodId: string) => {
      workMethodIdRef.current = nextMethodId;
      setWorkMethodId(nextMethodId);
      persistWorkMethodId(
        selectedAgent,
        [
          permissionSessionKey,
          chatId,
          activeCodingSessionId,
          window.currentSessionId,
        ],
        nextMethodId,
      );
    },
    [activeCodingSessionId, chatId, permissionSessionKey, selectedAgent],
  );
  const welcomeDescriptionVariants = useMemo(() => {
    const raw = t("chat.descriptionVariants", { returnObjects: true });
    if (Array.isArray(raw)) {
      const variants = raw.filter(
        (item): item is string =>
          typeof item === "string" && item.trim().length > 0,
      );
      if (variants.length > 0) {
        return variants;
      }
    }
    return [t("chat.description")];
  }, [i18n.language, t]);
  const welcomeDescription = useMemo(() => {
    const index = Math.floor(Math.random() * welcomeDescriptionVariants.length);
    return welcomeDescriptionVariants[index] ?? t("chat.description");
  }, [welcomeDescriptionVariants, t]);
  const localizedWelcomeGreeting = t("chat.emptyWelcomeGreeting", {
    defaultValue: "今天想推进什么？",
  });
  const localizedWelcomeDescription = t("chat.emptyWelcomeDescription", {
    defaultValue: welcomeDescription,
  });
  const emptyWelcome = useWelcomeGreeting({
    agentId: selectedAgent,
    displaySettings,
    enabled: welcomeEligible,
    entryId: welcomeEntryId,
    fallbackTitle: localizedWelcomeGreeting,
    fallbackSubtitle: localizedWelcomeDescription,
    locale: i18n.language,
  });
  const emptyWelcomeGreeting = emptyWelcome.title;
  const emptyWelcomeDescription = emptyWelcome.subtitle;
  const emptyPromptSuggestions = useMemo(() => {
    const raw = t("chat.emptyPrompts", { returnObjects: true });
    const suggestions = normalizeEmptyPromptSuggestions(raw);
    return suggestions.length > 0
      ? suggestions
      : DEFAULT_EMPTY_PROMPT_SUGGESTIONS;
  }, [i18n.language, t]);
  const RuntimeResponseCard = useCallback(
    (props: ComponentProps<typeof QuquResponseCard>) => (
      <>
        <QuquResponseCard
          {...props}
          showThinking={displaySettings.showThinking}
          showToolCalls={displaySettings.showToolCalls}
          progressInComposer={!codingMode}
        />
        {props.isLast ? <TurnEventPreview variant="inline" /> : null}
      </>
    ),
    [displaySettings.showThinking, displaySettings.showToolCalls, codingMode],
  );

  useEffect(() => {
    if (!embedded || !codingMode) return;
    let cancelled = false;
    const selectedCodingChatId =
      typeof codingChatId === "string" && codingChatId.trim()
        ? codingChatId.trim()
        : null;
    if (selectedCodingChatId) {
      sessionApi.preferredChatId = selectedCodingChatId;
      setEmbeddedChatId(selectedCodingChatId);
      storeCodingChatId(
        selectedAgent,
        codingWorkspaceDir,
        selectedCodingChatId,
      );
      if (activeCodingSessionId) {
        sessionApi.rememberSessionAlias(
          activeCodingSessionId,
          selectedCodingChatId,
        );
      }
    }
    const stored = readStoredCodingChatId(selectedAgent, codingWorkspaceDir);
    if (!selectedCodingChatId && stored) {
      sessionApi.preferredChatId = stored;
      setEmbeddedChatId(stored);
    } else if (!selectedCodingChatId) {
      setEmbeddedChatId(undefined);
    }

    chatApi
      .ensureTopicWorkspace({
        session_id: activeCodingSessionId,
        chat_id: selectedCodingChatId || undefined,
        user_id: DEFAULT_USER_ID,
        channel: DEFAULT_CHANNEL,
        name: "新会话",
        manual_workspace_dir: codingWorkspaceGrantId
          ? undefined
          : codingWorkspaceDir || undefined,
        coding_mode: true,
        coding_workspace_dir: codingWorkspaceGrantId
          ? undefined
          : codingWorkspaceDir || undefined,
        workspace_grant_id: codingWorkspaceGrantId || undefined,
      })
      .then((workspace) => {
        if (cancelled) return;
        const shouldAdoptWorkspaceChat = matchesChatSelection(
          selectedCodingChatId,
          workspace.chat.id,
          workspace.chat.id,
        );
        const sessionAliases = shouldAdoptWorkspaceChat
          ? [activeCodingSessionId, selectedCodingChatId, stored]
          : [activeCodingSessionId, stored];
        sessionApi.resolveEnsuredSession(
          sessionAliases,
          workspace.chat.id,
          workspace.chat.session_id,
        );
        sessionApi.rememberSessionAlias(
          activeCodingSessionId,
          workspace.chat.id,
          workspace.chat.session_id,
          ...sessionAliases,
        );
        if (shouldAdoptWorkspaceChat) {
          setActiveTopicWorkspace({
            agentId: selectedAgent,
            sessionId: workspace.chat.session_id,
            chatId: workspace.chat.id,
            topicWorkspaceDir: workspace.topic_workspace_dir,
            manualWorkspaceDir: workspace.manual_workspace_dir || null,
            effectiveWorkspaceDir:
              workspace.project_workspace_dir ||
              workspace.effective_workspace_dir,
            projectWorkspaceDir:
              workspace.project_workspace_dir || codingWorkspaceDir,
            workspaceGrantId:
              workspace.workspace_grant_id || codingWorkspaceGrantId || null,
            workspaceAccessMode: workspace.workspace_access_mode || null,
            workspaceTrustState: workspace.workspace_trust_state || null,
          });
          sessionApi.preferredChatId = workspace.chat.id;
          setEmbeddedChatId(workspace.chat.id);
          storeCodingChatId(
            selectedAgent,
            codingWorkspaceDir,
            workspace.chat.id,
          );
        } else {
          sessionApi.preferredChatId = selectedCodingChatId;
        }
        onCodingChatResolved?.(workspace.chat);
        setRefreshKey((value) => value + 1);
      })
      .catch((error) => {
        console.warn("Failed to prepare Coding chat:", error);
      });

    return () => {
      cancelled = true;
    };
  }, [
    activeCodingSessionId,
    codingChatId,
    codingMode,
    codingWorkspaceDir,
    codingWorkspaceGrantId,
    embedded,
    onCodingChatResolved,
    selectedAgent,
  ]);

  useEffect(() => {
    let cancelled = false;
    planApi
      .getPlanConfig()
      .then((cfg) => {
        if (!cancelled) setPlanEnabled(cfg.enabled);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [selectedAgent]);

  const handleEnsurePlanEnabled = useCallback(async () => {
    const config = await planApi.updatePlanConfig({ enabled: true });
    if (!config.enabled) {
      throw new Error("计划能力未能启用，请稍后重试。");
    }
    setPlanEnabled(true);
  }, []);

  useEffect(() => {
    let cancelled = false;
    reminderApi
      .getTodayBrief()
      .then((brief) => {
        if (!cancelled) setTodayBrief(brief);
      })
      .catch(() => {
        if (!cancelled) setTodayBrief(null);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedAgent]);

  useEffect(() => {
    let cancelled = false;
    dailyBriefApi
      .getHomeSnapshot()
      .then((snapshot) => {
        if (!cancelled) setDailyBriefHome(snapshot);
      })
      .catch(() => {
        if (!cancelled) setDailyBriefHome(null);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedAgent]);

  useEffect(() => {
    let cancelled = false;
    agentApi
      .getAgentRunningConfig()
      .then((cfg) => {
        if (!cancelled) {
          setMaxContextTokens(
            Number.isFinite(cfg.max_input_length) ? cfg.max_input_length : null,
          );
        }
      })
      .catch(() => {
        if (!cancelled) setMaxContextTokens(null);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedAgent]);

  const isChatActiveRef = useRef(false);
  isChatActiveRef.current =
    embedded ||
    location.pathname === "/" ||
    location.pathname.startsWith("/chat");

  const isChatActive = useCallback(() => isChatActiveRef.current, []);

  // Consume approvals from Context and filter by current session.
  // Uses a serialized key to avoid creating a new Map (and triggering
  // re-renders of the entire Chat tree) when the filtered result is identical.
  const prevApprovalKeyRef = useRef("");

  useEffect(() => {
    const currentAliases = sessionAliasSet(window.currentSessionId, chatId);
    const sessionApprovals = approvals.filter((approval) =>
      approvalMatchesSessionAliases(approval, currentAliases),
    );

    // Build a stable key from the filtered request IDs so we can skip
    // the Map rebuild when nothing changed (avoids re-render every 2.5s poll).
    const approvalKey = sessionApprovals
      .map((a) => a.request_id)
      .sort()
      .join(",");

    if (approvalKey === prevApprovalKeyRef.current) return;
    prevApprovalKeyRef.current = approvalKey;

    const newMap = new Map<string, ApprovalMessageData>();
    for (const approval of sessionApprovals) {
      newMap.set(approval.request_id, {
        requestId: approval.request_id,
        sessionId: approval.session_id,
        rootSessionId: approval.root_session_id,
        userId: approval.user_id,
        agentId: approval.agent_id,
        toolName: approval.tool_name,
        severity: approval.severity,
        findingsCount: approval.findings_count,
        findingsSummary: approval.findings_summary,
        toolParams: approval.tool_params,
        createdAt: approval.created_at,
        timeoutSeconds: approval.timeout_seconds,
        policyDecision: approval.policy_decision,
        approvalOptions: approval.approval_options,
        sessionRememberable: approval.session_rememberable,
        rememberable: approval.rememberable,
        permissionScope: approval.permission_scope,
        permissionGroupKey: approval.permission_group_key,
        sourceRunId: approval.source_run_id,
        sourceAgentName: approval.source_agent_name,
      });
    }

    setApprovalRequests(newMap);
  }, [approvals, chatId]);

  const handleApprove = useCallback(
    async (
      requestId: string,
      scope: "once" | "session" | "workspace" = "once",
    ) => {
      const request = approvalRequests.get(requestId);
      if (!request) return;

      // The server authorizes this request against the pending approval's
      // original root session. A route ID or a newly resolved alias can differ
      // from it while a first turn is being promoted to a real chat.
      const rootSessionId =
        request.rootSessionId || window.currentSessionId || chatId || "";
      const approvalUserId =
        request.userId || window.currentUserId || DEFAULT_USER_ID;

      try {
        const cardElement = document.querySelector(
          `[data-approval-id="${requestId}"]`,
        );
        if (cardElement) {
          cardElement.classList.add(styles.approvalStackItemExit);
        }

        const response = await commandsApi.sendApprovalCommand(
          "approve",
          requestId,
          rootSessionId,
          undefined,
          scope,
          approvalUserId,
        );
        message.success(t("approval.approved"));

        // Delay removal to let exit animation complete
        setTimeout(() => {
          setApprovalRequests((prev) => {
            const next = new Map(prev);
            next.delete(requestId);
            for (const resolvedId of response.resolved_request_ids || []) {
              next.delete(resolvedId);
            }
            return next;
          });
        }, 300);
      } catch (error) {
        message.error(t("approval.approveFailed"));
        console.error("Failed to approve:", error);
        throw error;
      }
    },
    [approvalRequests, chatId, t, message],
  );

  const handleDeny = useCallback(
    async (requestId: string) => {
      const request = approvalRequests.get(requestId);
      if (!request) return;

      // The pending record is the authority for the root session. Its
      // sessionId can be a sub-agent session, so do not submit that value.
      const rootSessionId =
        request.rootSessionId || window.currentSessionId || chatId || "";
      const approvalUserId =
        request.userId || window.currentUserId || DEFAULT_USER_ID;

      try {
        // Add exit animation class
        const cardElement = document.querySelector(
          `[data-approval-id="${requestId}"]`,
        );
        if (cardElement) {
          cardElement.classList.add(styles.approvalStackItemExit);
        }

        await commandsApi.sendApprovalCommand(
          "deny",
          requestId,
          rootSessionId,
          undefined,
          undefined,
          approvalUserId,
        );
        message.success(t("approval.denied"));

        // Delay removal to let animation complete
        // Backend will remove from pending list, next poll will update UI
        setTimeout(() => {
          setApprovalRequests((prev) => {
            const next = new Map(prev);
            next.delete(requestId);
            return next;
          });
        }, 300); // Match animation duration
      } catch (error) {
        message.error(t("approval.denyFailed"));
        console.error("Failed to deny:", error);
        throw error;
      }
    },
    [approvalRequests, chatId, t, message],
  );

  const approvalGroups = useMemo(() => {
    const groups = new Map<string, ApprovalGroupData>();
    Array.from(approvalRequests.values()).forEach((request) => {
      const policy = request.policyDecision || {};
      const rule =
        String(policy.matched_rule_id || "").trim() ||
        (Array.isArray(policy.matched_rules)
          ? policy.matched_rules.join(",")
          : "") ||
        (Array.isArray(policy.matched_rule_ids)
          ? policy.matched_rule_ids.join(",")
          : "");
      const key =
        request.permissionGroupKey ||
        [
          request.toolName,
          request.severity,
          rule,
          request.rootSessionId || "",
        ].join("|");
      const existing = groups.get(key);
      if (existing) {
        existing.requestIds.push(request.requestId);
        existing.requestCount += 1;
        existing.findingsCount += request.findingsCount;
        return;
      }
      groups.set(key, {
        ...request,
        requestIds: [request.requestId],
        requestCount: 1,
      });
    });
    return Array.from(groups.values());
  }, [approvalRequests]);

  // Use custom hooks for better separation of concerns
  const isComposingRef = useIMEComposition(isChatActive);
  const multimodalCaps = useMultimodalCapabilities(
    refreshKey,
    location.pathname,
    isChatActive,
    selectedAgent,
  );

  const lastSessionIdRef = useRef<string | null>(null);
  const chatIdRef = useRef(chatId);
  const navigateRef = useRef(navigate);
  const chatStageRef = useRef<HTMLDivElement>(null);
  const chatRef = useRef<ILantaiRuntimeWebUIRef>(null);
  const dragDepthRef = useRef(0);
  const firstTurnTransitionTimerRef = useRef<number | null>(null);
  const pendingClearHistoryRef = useRef<string[] | null>(null);
  const latestRuntimeControlRef = useRef<{
    chatId: string;
    runId: string;
    runtimeState: ChatRuntimeState;
  } | null>(null);
  const selectedSkillNameRef = useRef<string | null>(null);
  const pendingModelSetupInputRef = useRef<ComposerContextDraft<
    Array<Record<string, unknown>>
  > | null>(null);
  const deepThinkingEnabledRef = useRef(false);
  const deepThinkingSupportedRef = useRef(false);
  const lastDeepThinkingModelKeyRef = useRef<string | null>(null);
  const contextStatsGateRef = useRef(createLatestRequestGate());
  const runtimeUsageGateRef = useRef(createLatestRequestGate());
  const [fileDragActive, setFileDragActive] = useState(false);
  const [isEmptyConversation, setIsEmptyConversation] = useState(!chatId);
  const [isFirstTurnTransitioning, setIsFirstTurnTransitioning] =
    useState(false);
  const [deepThinkingEnabled, setDeepThinkingEnabled] = useState(false);
  const [selectedSkillName, setSelectedSkillName] = useState<string | null>(
    null,
  );

  useEffect(() => {
    if (embedded || codingMode || !isEmptyConversation) {
      setContinuationSuggestions([]);
      return;
    }
    let cancelled = false;
    void Promise.allSettled([
      capabilityApi.listTaskRuns(),
      timeApi.getToday(),
    ]).then(([runsResult, todayResult]) => {
      if (cancelled) return;
      const runs = runsResult.status === "fulfilled" ? runsResult.value : [];
      const today =
        todayResult.status === "fulfilled" ? todayResult.value : undefined;
      setContinuationSuggestions(selectContinuationSuggestions(runs, today));
    });
    return () => {
      cancelled = true;
    };
  }, [codingMode, embedded, isEmptyConversation, selectedAgent]);

  useEffect(() => {
    latestRuntimeControlRef.current = null;
  }, [chatId, selectedAgent]);

  const runtimeMessageSource = useMemo(
    () =>
      resolveChatRuntimeMessageSource(
        typeof window === "undefined" ? null : window.localStorage,
      ),
    [],
  );

  // The sender can become interactive in the same paint that exposes the
  // active thinking control. Keep the click-path ref in a layout effect so a
  // first interaction cannot observe the previous value.
  useLayoutEffect(() => {
    deepThinkingEnabledRef.current = deepThinkingEnabled;
  }, [deepThinkingEnabled]);

  useLayoutEffect(() => {
    deepThinkingSupportedRef.current = multimodalCaps.supportsDeepThinking;
  }, [multimodalCaps.supportsDeepThinking]);

  // LantaiRuntimeWebUI may still be finishing work that began from an older
  // options render when the model capability probe resolves.  Keep the
  // transport getter stable and read the latest committed control state from
  // refs, so an old submit/fetch closure cannot serialize a different choice
  // from the button the user can currently see.
  const snapshotDeepThinkingRequest = useCallback(() => {
    // A retained vendor submit callback can outlive the ChatPage render that
    // created it.  In that case its refs belong to the retired render while
    // the current control is already visible.  The rendered admission marker
    // is therefore the final authority for a user-triggered submission.
    const visibleControl =
      typeof document === "undefined"
        ? null
        : document.querySelector<HTMLButtonElement>(
            'button[data-chat-deep-thinking="1"]',
          );
    if (visibleControl && !visibleControl.disabled) {
      return {
        enabled: visibleControl.getAttribute("aria-pressed") === "true",
        effort: "high",
      };
    }
    if (!deepThinkingSupportedRef.current) return undefined;
    return {
      enabled: deepThinkingEnabledRef.current,
      effort: "high",
    };
  }, []);

  useLayoutEffect(() => {
    if (!multimodalCaps.reasoningCapabilityResolved) {
      return;
    }
    // A model switch adopts the product default (thinking on whenever the
    // selected model supports it). Capability refreshes for the same model
    // deliberately preserve the user's manual choice.
    const modelChanged =
      multimodalCaps.activeModelKey !== lastDeepThinkingModelKeyRef.current;
    lastDeepThinkingModelKeyRef.current = multimodalCaps.activeModelKey;
    if (!multimodalCaps.supportsDeepThinking) {
      deepThinkingEnabledRef.current = false;
      setDeepThinkingEnabled(false);
    } else if (modelChanged || multimodalCaps.deepThinkingFixed) {
      deepThinkingEnabledRef.current = true;
      setDeepThinkingEnabled(true);
    }
  }, [
    multimodalCaps.activeModelKey,
    multimodalCaps.reasoningCapabilityResolved,
    multimodalCaps.supportsDeepThinking,
    multimodalCaps.deepThinkingFixed,
  ]);

  useEffect(() => {
    selectedSkillNameRef.current = selectedSkillName;
  }, [selectedSkillName]);

  useEffect(() => {
    selectedSkillNameRef.current = null;
    setSelectedSkillName(null);
    setArtifactPanelOpen(false);
    const draftOwner = {
      agentId: selectedAgent,
      sessionId: permissionSessionKey,
    };
    const restoredArtifactEditDraft = loadArtifactEditDraft(draftOwner);
    artifactEditDraftRef.current = restoredArtifactEditDraft;
    setArtifactEditDraft(restoredArtifactEditDraft?.value || null);
    pendingModelSetupInputRef.current = loadModelSetupInputDraft(draftOwner);
    setKdocsPanelOpen(false);
    setKdocsPickerOpen(false);
    setKdocsRequestedDocument(null);
    setSubAgentInspectionTarget(null);
    setArtifactRequestedPath(null);
    const storedKdocsReferences = loadKdocsReferences(
      selectedAgent,
      permissionSessionKey,
    );
    kdocsReferencesRef.current = storedKdocsReferences;
    setKdocsReferences(storedKdocsReferences);
    setMacUseTarget(
      readStoredMacUseTarget(selectedAgent, permissionSessionKey),
    );
    return subscribeComposerContextDrafts(draftOwner, (kind) => {
      if (!sameComposerDraftOwner(draftOwner, composerDraftOwnerRef.current))
        return;
      if (kind === "artifact-edit") {
        const stored = loadArtifactEditDraft(draftOwner);
        artifactEditDraftRef.current = stored;
        setArtifactEditDraft(stored?.value || null);
      } else {
        pendingModelSetupInputRef.current =
          loadModelSetupInputDraft(draftOwner);
      }
    });
  }, [chatId, permissionSessionKey, selectedAgent]);

  const getChatTextarea = useCallback(() => {
    return chatStageRef.current?.querySelector(
      "textarea",
    ) as HTMLTextAreaElement | null;
  }, []);

  const clearArtifactEditDraft = useCallback(() => {
    clearStoredArtifactEditDraft(composerDraftOwnerRef.current);
    artifactEditDraftRef.current = null;
    setArtifactEditDraft(null);
  }, []);

  const handlePrepareArtifactEdit = useCallback(
    (draft: ArtifactEditDraft) => {
      const targetChatId = draft.target.chat_id;
      const draftOwner = {
        agentId: selectedAgent,
        sessionId: permissionSessionKey,
      };
      if (
        targetChatId !== chatIdRef.current ||
        !sameComposerDraftOwner(draftOwner, composerDraftOwnerRef.current)
      )
        return;
      artifactEditDraftRef.current = saveArtifactEditDraft(draftOwner, draft);
      setArtifactEditDraft(draft);
      setArtifactPanelOpen(false);
      window.requestAnimationFrame(() => {
        if (
          targetChatId !== chatIdRef.current ||
          !sameComposerDraftOwner(draftOwner, composerDraftOwnerRef.current)
        )
          return;
        const textarea = getChatTextarea();
        if (!textarea) return;
        if (!textarea.value.trim()) {
          const prompt = "请按选中的目标修改这个成果。";
          setTextareaValue(textarea, prompt);
          sessionApi.saveDraftText(targetChatId, prompt);
        }
        textarea.focus();
      });
    },
    [getChatTextarea, permissionSessionKey, selectedAgent],
  );

  useEffect(() => {
    const draftOwner = {
      agentId: selectedAgent,
      sessionId: permissionSessionKey,
    };
    pendingModelSetupInputRef.current = loadModelSetupInputDraft(draftOwner);
    const draft = consumeModelSetupDraftSnapshot(modelSetupReturnTo);
    if (!draft) return;
    if (
      draft.taskContext?.agentId &&
      draft.taskContext.agentId !== selectedAgent
    ) {
      persistModelSetupDraftSnapshot(modelSetupReturnTo, draft);
      return;
    }
    pendingModelSetupInputRef.current = draft.submittedInput.length
      ? saveModelSetupInputDraft(draftOwner, draft.submittedInput)
      : null;
    if (!draft.submittedInput.length) clearModelSetupInputDraft(draftOwner);
    if (draft.composerText)
      sessionApi.saveDraftText(permissionSessionKey, draft.composerText);
    if (
      !draft.taskContext?.agentId ||
      draft.taskContext.agentId === selectedAgent
    ) {
      if (draft.taskContext?.selectedSkillName) {
        selectedSkillNameRef.current = draft.taskContext.selectedSkillName;
        setSelectedSkillName(draft.taskContext.selectedSkillName);
      }
      if (draft.taskContext?.deepThinking) {
        deepThinkingEnabledRef.current = draft.taskContext.deepThinking.enabled;
        setDeepThinkingEnabled(draft.taskContext.deepThinking.enabled);
      }
    }

    let attempts = 0;
    let timer = 0;
    const restore = () => {
      const textarea = getChatTextarea();
      if (textarea) {
        if (!textarea.value.trim()) {
          setTextareaValue(textarea, draft.composerText);
        }
        return;
      }
      attempts += 1;
      if (attempts < 12) {
        timer = window.setTimeout(restore, 50);
      } else {
        persistModelSetupDraftSnapshot(modelSetupReturnTo, draft);
      }
    };
    restore();

    return () => window.clearTimeout(timer);
  }, [
    getChatTextarea,
    modelSetupReturnTo,
    permissionSessionKey,
    selectedAgent,
  ]);

  useEffect(() => {
    const draft = consumeDailyBriefReturnDraft({
      returnTo: modelSetupReturnTo,
      agentId: selectedAgent,
      sessionKey: permissionSessionKey,
    });
    if (!draft) return;

    let attempts = 0;
    let timer = 0;
    const restore = () => {
      const textarea = getChatTextarea();
      if (textarea) {
        if (!textarea.value.trim()) {
          setTextareaValue(textarea, draft.composerText);
        }
        return;
      }
      attempts += 1;
      if (attempts < 12) {
        timer = window.setTimeout(restore, 50);
      } else {
        persistDailyBriefReturnDraft(draft);
      }
    };
    restore();

    return () => window.clearTimeout(timer);
  }, [
    getChatTextarea,
    modelSetupReturnTo,
    permissionSessionKey,
    selectedAgent,
  ]);

  const handleOpenDailyBrief = useCallback(() => {
    persistDailyBriefReturnDraft({
      returnTo: modelSetupReturnTo,
      agentId: selectedAgent,
      sessionKey: permissionSessionKey,
      composerText: getChatTextarea()?.value ?? "",
    });
    navigate(dailyBriefHome?.card.route || "/daily-brief");
  }, [
    dailyBriefHome?.card.route,
    getChatTextarea,
    modelSetupReturnTo,
    navigate,
    permissionSessionKey,
    selectedAgent,
  ]);

  const closeBriefDraft = useCallback(() => {
    const params = new URLSearchParams(location.search);
    params.delete("briefDraft");
    navigate(`${location.pathname}${params.size ? `?${params}` : ""}`, { replace: true });
  }, [location.pathname, location.search, navigate]);
  const insertBriefDraft = useCallback((text: string) => {
    const textarea = getChatTextarea();
    if (!textarea) return false;
    setTextareaValue(textarea, textarea.value.trim() ? `${textarea.value}\n\n${text}` : text);
    textarea.focus();
    return true;
  }, [getChatTextarea]);

  const previewBackfeed = usePreviewErrorBackfeed({
    chatId,
    getTextarea: getChatTextarea,
    onInfo: (text) => message.info(text),
    onWarning: (text) => message.warning(text),
  });

  useEffect(() => {
    const handlePaste = (event: ClipboardEvent) => {
      if (!isChatActive()) return;

      const target = event.target;
      if (!(target instanceof HTMLTextAreaElement)) return;
      if (!target.closest('[class*="sender"]')) return;
      if (!event.clipboardData) return;
      if (event.clipboardData.files?.length) return;

      for (
        let index = 0;
        index < event.clipboardData.items.length;
        index += 1
      ) {
        if (event.clipboardData.items[index]?.kind === "file") return;
      }

      const plain = event.clipboardData.getData("text/plain") || "";
      const text = extractClipboardTextWithLayout(event.clipboardData);
      if (!text || text === plain || !text.includes("\n")) return;

      event.preventDefault();
      const start = target.selectionStart ?? target.value.length;
      const end = target.selectionEnd ?? start;
      const nextValue =
        target.value.slice(0, start) + text + target.value.slice(end);
      const caret = start + text.length;
      setTextareaValue(target, nextValue, caret, caret);
    };

    document.addEventListener("paste", handlePaste, true);
    return () => document.removeEventListener("paste", handlePaste, true);
  }, [isChatActive]);

  useEffect(() => {
    if (!chatId) return;
    const retryDraft = consumeRetryDraft(chatId);
    if (!retryDraft) return;

    let attempts = 0;
    let timer: number | null = null;
    const applyDraft = () => {
      attempts += 1;
      const textarea = getChatTextarea();
      if (!textarea) {
        if (attempts < 12) {
          timer = window.setTimeout(applyDraft, 100);
        }
        return;
      }
      setTextareaValue(textarea, retryDraft);
      textarea.focus();
      // A retry prompt is only a draft until the user presses Send. Recording
      // it as an accepted message creates a phantom user bubble on hydration.
      sessionApi.saveDraftText(chatId, retryDraft);
    };

    applyDraft();
    return () => {
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [chatId, getChatTextarea]);

  const hasAttachmentCards = useCallback(() => {
    return Boolean(
      chatStageRef.current?.querySelector('[class*="attachment-list-card"]'),
    );
  }, []);

  const syncEmptyConversationState = useCallback(() => {
    const root = chatStageRef.current;
    if (!root) return;

    setIsEmptyConversation(
      Boolean(
        root.querySelector(
          '.lantai-chat-anywhere-message-list-welcome:not([data-lantai-welcome-exiting="1"]), [class*="message-list-welcome"]:not([data-lantai-welcome-exiting="1"])',
        ),
      ),
    );
  }, []);

  const startFirstTurnTransition = useCallback(() => {
    const root = chatStageRef.current;
    if (!root || root.dataset.emptyConversation !== "true") return;

    if (firstTurnTransitionTimerRef.current !== null) {
      window.clearTimeout(firstTurnTransitionTimerRef.current);
    }
    setIsFirstTurnTransitioning(true);
    setIsEmptyConversation(false);

    const reduceMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    firstTurnTransitionTimerRef.current = window.setTimeout(
      () => {
        firstTurnTransitionTimerRef.current = null;
        setIsFirstTurnTransitioning(false);
        syncEmptyConversationState();
      },
      reduceMotion ? 1 : FIRST_TURN_TRANSITION_MS,
    );
  }, [syncEmptyConversationState]);

  useEffect(
    () => () => {
      if (firstTurnTransitionTimerRef.current !== null) {
        window.clearTimeout(firstTurnTransitionTimerRef.current);
      }
    },
    [],
  );

  useEffect(() => {
    if (!chatId) {
      if (firstTurnTransitionTimerRef.current !== null) {
        window.clearTimeout(firstTurnTransitionTimerRef.current);
        firstTurnTransitionTimerRef.current = null;
      }
      setIsFirstTurnTransitioning(false);
      setIsEmptyConversation(true);
      return;
    }

    let cancelled = false;
    let frame = 0;
    let timer = 0;

    const sync = () => {
      if (!cancelled) {
        syncEmptyConversationState();
      }
    };

    frame = window.requestAnimationFrame(sync);
    timer = window.setTimeout(sync, 120);

    return () => {
      cancelled = true;
      if (frame) {
        window.cancelAnimationFrame(frame);
      }
      if (timer) {
        window.clearTimeout(timer);
      }
    };
  }, [chatId, refreshKey, syncEmptyConversationState]);

  useEffect(() => {
    contextStatsGateRef.current.invalidate();
    runtimeUsageGateRef.current.invalidate();
    setLatestTokenUsage(null);
    setLatestContextStats(null);
    setLatestRouting(null);
    publishRoutingSnapshot(null);
  }, [chatId, refreshKey, selectedAgent]);

  const resolveActiveChatId = useCallback(() => {
    const sessionId = window.currentSessionId || "";
    return resolveStableChatId(
      chatIdRef.current,
      sessionApi.getRealIdForSession(sessionId),
      lastSessionIdRef.current,
      sessionId,
    );
  }, []);

  const refreshContextStats = useCallback(async () => {
    const activeChatId = resolveActiveChatId();
    if (!activeChatId) {
      contextStatsGateRef.current.invalidate();
      setLatestContextStats(null);
      setLatestRouting(null);
      return;
    }
    const ownsState = contextStatsGateRef.current.begin();
    try {
      const stats = await chatApi.getChatContextStats(activeChatId);
      if (!ownsState() || resolveActiveChatId() !== activeChatId) {
        return;
      }
      setLatestContextStats({
        ...stats,
        updatedAt: Date.now(),
      });
    } catch (error) {
      if (!ownsState()) return;
      console.warn("Failed to refresh context stats:", error);
    }
  }, [resolveActiveChatId]);

  const contextWindowTarget = useMemo(() => {
    if (!multimodalCaps.activeProviderId || !multimodalCaps.activeModelId) {
      return null;
    }
    return {
      providerId: multimodalCaps.activeProviderId,
      modelId: multimodalCaps.activeModelId,
      modelName: multimodalCaps.activeModelName || multimodalCaps.activeModelId,
    };
  }, [
    multimodalCaps.activeModelId,
    multimodalCaps.activeModelName,
    multimodalCaps.activeProviderId,
  ]);

  const saveActiveModelContextWindow = useCallback(
    async (value: number) => {
      if (!contextWindowTarget) {
        throw new Error("当前模型尚未就绪，请稍后重试。");
      }
      try {
        await providerApi.configureModel(
          contextWindowTarget.providerId,
          contextWindowTarget.modelId,
          { user_context_window: value },
        );
        setLatestContextStats((previous) =>
          previous
            ? {
                ...previous,
                requested_window: value,
                effective_window: previous.model_window
                  ? Math.min(value, previous.model_window)
                  : Math.min(value, previous.effective_window ?? value),
                updatedAt: Date.now(),
              }
            : previous,
        );
        await refreshContextStats();
        window.dispatchEvent(
          new CustomEvent("model-context-window-updated", {
            detail: {
              ...contextWindowTarget,
              userContextWindow: value,
            },
          }),
        );
        message.success(`已保存 ${contextWindowTarget.modelName} 的上下文上限`);
      } catch (error) {
        message.error(
          error instanceof Error
            ? error.message
            : "上下文上限保存失败，请稍后重试。",
        );
        throw error;
      }
    },
    [contextWindowTarget, message, refreshContextStats],
  );

  const applyRuntimeEventsSnapshot = useCallback(
    (response: RuntimeEventsResponse) => {
      const activeChatId = resolveActiveChatId();
      const responseChatId = String(response.chat_id || "").trim();
      if (responseChatId && activeChatId && responseChatId !== activeChatId) {
        return;
      }
      if (responseChatId && responseChatId === activeChatId) {
        const previousControl = latestRuntimeControlRef.current;
        const runtimePatch = buildRuntimeEventSessionPatch(
          response,
          previousControl?.chatId === responseChatId
            ? previousControl.runtimeState
            : null,
        );
        const runId = String(runtimePatch.runtimeState.run_id || "").trim();
        latestRuntimeControlRef.current = runId
          ? {
              chatId: responseChatId,
              runId,
              runtimeState: runtimePatch.runtimeState,
            }
          : null;
      }
      const eventWithUsage = [...response.events]
        .reverse()
        .find((event) => event.usage);
      const routingEvent = [...response.events]
        .reverse()
        .find(
          (event) =>
            event.type === "model.routing_decision" ||
            event.payload?.legacy_kind === "chat.model.routing_decision",
        );
      const routing = normalizeRoutingSnapshot(routingEvent?.payload);
      if (routing) {
        setLatestRouting(routing);
        publishRoutingSnapshot(routing);
      } else if (response.after_seq === 0) {
        setLatestRouting(null);
        publishRoutingSnapshot(null);
      }
      const usage = normalizeTokenUsage(
        eventWithUsage?.usage,
        maxContextTokens,
      );
      if (usage) {
        setLatestTokenUsage(usage);
      }
    },
    [maxContextTokens, resolveActiveChatId],
  );

  const requestStopForActiveChat = useCallback(
    async (requestedSessionId?: string) => {
      const candidateSessionId = String(requestedSessionId || "").trim();
      const currentSessionId = String(window.currentSessionId || "").trim();
      const resolvedChatId = resolveStableChatId(
        chatIdRef.current,
        sessionApi.getRealIdForSession(candidateSessionId),
        sessionApi.getRealIdForSession(currentSessionId),
        lastSessionIdRef.current,
        candidateSessionId,
        currentSessionId,
      );
      if (!resolvedChatId) return { stopped: false };

      const runtimeControl = latestRuntimeControlRef.current;
      const runId =
        runtimeControl?.chatId === resolvedChatId
          ? runtimeControl.runId
          : undefined;
      // Runtime events and the presence strip are the authoritative view of
      // the active execution. Stop that same run instead of fencing the
      // legacy chat endpoint with an event turn id that may name a different
      // attempt. Chats without a runtime ledger keep the compatibility path.
      const stopRequest = runId
        ? runtimeApi.interruptRun(runId)
        : chatApi.stopChat(resolvedChatId);
      const macUseSessionIds = [
        resolvedChatId,
        candidateSessionId,
        currentSessionId,
        lastSessionIdRef.current,
      ].filter(
        (value, index, values): value is string =>
          Boolean(value) && values.indexOf(value) === index,
      );
      const macUseStops = macUseSessionIds.map((sessionId) =>
        window.electronAPI?.stopMacUse?.(sessionId),
      );
      try {
        const [response, macStopResults] = await Promise.all([
          stopRequest,
          Promise.allSettled(macUseStops),
        ]);
        const macUseStopped = macStopResults.some(
          (result) =>
            result.status === "fulfilled" &&
            result.value?.ok &&
            Boolean(
              (
                result.value.data?.session as
                  | { was_active?: boolean }
                  | undefined
              )?.was_active,
            ),
        );
        const stopped = Boolean(response?.stopped || macUseStopped);
        return { ...response, stopped };
      } catch (err) {
        console.error("Failed to stop chat:", err);
        message.error("停止当前回复失败。");
        throw err;
      }
    },
    [message],
  );

  const refreshRuntimeUsage = useCallback(async () => {
    const activeChatId = resolveActiveChatId();
    if (!activeChatId) {
      runtimeUsageGateRef.current.invalidate();
      setLatestRouting(null);
      return;
    }
    const ownsState = runtimeUsageGateRef.current.begin();
    try {
      const response = await chatApi.getChatEvents(activeChatId);
      if (!ownsState() || resolveActiveChatId() !== activeChatId) {
        return;
      }
      applyRuntimeEventsSnapshot(response);
    } catch (error) {
      if (!ownsState()) return;
      console.warn("Failed to refresh runtime usage:", error);
    }
  }, [applyRuntimeEventsSnapshot, resolveActiveChatId]);

  useEffect(() => {
    void refreshContextStats();
    void refreshRuntimeUsage();
    return () => {
      contextStatsGateRef.current.invalidate();
      runtimeUsageGateRef.current.invalidate();
    };
  }, [chatId, refreshKey, refreshContextStats, refreshRuntimeUsage]);

  useEffect(() => {
    const root = chatStageRef.current;
    if (!root) return;

    let frame = 0;
    const queueSync = () => {
      if (frame) {
        window.cancelAnimationFrame(frame);
      }
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        syncEmptyConversationState();
      });
    };

    queueSync();
    const observer = new MutationObserver(queueSync);
    observer.observe(root, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: [
        "class",
        "style",
        "hidden",
        "data-lantai-welcome-exiting",
      ],
    });

    return () => {
      observer.disconnect();
      if (frame) {
        window.cancelAnimationFrame(frame);
      }
    };
  }, [location.pathname, refreshKey, syncEmptyConversationState]);

  const ensureAttachmentOnlySubmitMarker = useCallback(() => {
    const textarea = getChatTextarea();
    if (!textarea) return;

    const cleanValue = stripAttachmentSubmitMarker(textarea.value || "");
    if (cleanValue.trim().length > 0) return;
    setTextareaValue(textarea, ATTACHMENT_ONLY_SUBMIT_MARKER);
  }, [getChatTextarea]);

  const clearAttachmentOnlySubmitMarkerIfDetached = useCallback(() => {
    if (hasAttachmentCards()) return;

    const textarea = getChatTextarea();
    if (!textarea) return;

    const value = textarea.value || "";
    if (!value.includes(ATTACHMENT_ONLY_SUBMIT_MARKER)) return;
    setTextareaValue(textarea, stripAttachmentSubmitMarker(value));
  }, [getChatTextarea, hasAttachmentCards]);

  const dispatchDroppedFilesToUpload = useCallback((files: File[]): boolean => {
    const root = chatStageRef.current;
    if (!root || files.length === 0) return false;

    const uploadInputs = Array.from(
      root.querySelectorAll<HTMLInputElement>(
        '[class*="chat-anywhere-input"] input[type="file"], input[type="file"]',
      ),
    );
    const uploadInput =
      uploadInputs.find((input) => !input.disabled && input.multiple) ??
      uploadInputs.find((input) => !input.disabled);
    if (!uploadInput || typeof DataTransfer !== "function") return false;

    try {
      const transfer = new DataTransfer();
      files.forEach((file) => transfer.items.add(file));
      try {
        uploadInput.files = transfer.files;
      } catch {
        Object.defineProperty(uploadInput, "files", {
          value: transfer.files,
          configurable: true,
        });
      }
      uploadInput.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    } catch (error) {
      console.error("Failed to dispatch dropped files:", error);
      return false;
    }
  }, []);

  const handleDragEnter = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!hasFileDrag(event)) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current += 1;
    setFileDragActive(true);
  }, []);

  const handleDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!hasFileDrag(event)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const handleDragLeave = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!hasFileDrag(event)) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) {
      setFileDragActive(false);
    }
  }, []);

  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      if (!hasFileDrag(event)) return;
      event.preventDefault();
      event.stopPropagation();
      dragDepthRef.current = 0;
      setFileDragActive(false);

      const files = listDraggedFiles(event);
      if (files.length === 0) return;

      if (!dispatchDroppedFilesToUpload(files)) {
        message.error(t("chat.attachments.dropFailed"));
      }
    },
    [dispatchDroppedFilesToUpload, message, t],
  );

  const handleEmptyPromptClick = useCallback(
    (prompt: string) => {
      const textarea = getChatTextarea();
      if (!textarea) return;

      const currentValue = stripAttachmentSubmitMarker(
        textarea.value || "",
      ).trim();
      const nextValue = currentValue ? `${currentValue}\n${prompt}` : prompt;
      setTextareaValue(textarea, nextValue);
      textarea.focus();
    },
    [getChatTextarea],
  );

  const prepareComposerText = useCallback(
    (
      prompt: string,
      notice: string,
      options: { requireEmpty?: boolean } = {},
    ) => {
      const textarea = getChatTextarea();
      if (!textarea) {
        message.warning("没有找到当前输入框，请回到聊天输入区后再试。");
        return false;
      }
      const current = stripAttachmentSubmitMarker(textarea.value || "").trim();
      if (options.requireEmpty && current) {
        message.info("输入框里还有未发送内容，先处理后再准备压缩。");
        textarea.focus();
        return false;
      }
      const next = current ? `${current}\n\n${prompt}` : prompt;
      setTextareaValue(textarea, next);
      textarea.focus();
      message.success(notice);
      return true;
    },
    [getChatTextarea, message],
  );

  const handlePrepareContextCompact = useCallback(() => {
    prepareComposerText("/compact", "已把压缩指令放入输入框，确认后发送。", {
      requireEmpty: true,
    });
  }, [prepareComposerText]);

  const handleCreateContextBranch = useCallback(() => {
    if (!chatRef.current?.branch.createFromLatestUserMessage()) {
      message.info("当前没有可创建分支的用户消息，或这一轮仍在运行。");
    }
  }, [message]);

  const handleRequestReplan = useCallback(() => {
    prepareComposerText(
      "请根据当前进展重新检查执行计划：保留已完成内容，说明需要调整的步骤、风险和新的完成条件。先更新计划，不要立即执行新增步骤。",
      "已把调整计划的要求放入输入框，确认后发送。",
    );
  }, [prepareComposerText]);

  const handleRequestValidation = useCallback(() => {
    prepareComposerText(
      "请给当前计划补充明确的验收步骤：列出需要运行的测试、需要人工检查的界面，以及最终可判断完成的结果。先更新计划，不要立即执行。",
      "已把验收要求放入输入框，确认后发送。",
    );
  }, [prepareComposerText]);

  const handleRequestPrivateEditorAngle = useCallback(() => {
    prepareComposerText(
      "请只基于当前作品已经授权的材料，找一个真正不同的新切口。如果材料没有新价值，可以直接返回空结果；不要联网、跨项目或创建定时任务。",
      "已把新切口请求放入输入框，确认后发送。",
    );
  }, [prepareComposerText]);

  const handleDevelopPrivateEditorAngle = useCallback(
    (proposal: PrivateEditorAngleProposal) => {
      prepareComposerText(
        [
          `请继续发展已记录的新切口（提案 ${proposal.id}）：${proposal.key_idea}`,
          `依据：${proposal.source_refs.join("；")}。`,
          `最小下一步：${proposal.next_action}`,
          "只在当前作品受管理的工作副本中形成有来源的新草稿或候选；不要直接修改或覆盖当前稿。完成后明确说明当前稿未变，等待我决定。",
        ].join("\n"),
        "已把这个切口的发展要求放入输入框，确认后发送。",
      );
    },
    [prepareComposerText],
  );

  const handleCreateCapabilityDraft = useCallback(
    async (response: unknown) => {
      if (tokenMeterRunning) {
        message.info("请等本轮回复完成后，再沉淀这段协作。");
        return;
      }
      if (draftingCapability) {
        message.info("沈行知正在沉淀上一段协作。");
        return;
      }
      const activeChatId = resolveActiveChatId();
      if (!activeChatId) {
        message.warning("先完成一轮对话，再沉淀这段协作。");
        return;
      }
      const anchor = resolveCollaborationDraftAnchor(response);
      if (!anchor) {
        message.warning("这条历史回复缺少轮次标识，无法安全判断沉淀截止位置。");
        return;
      }

      const operationId = globalThis.crypto?.randomUUID?.() || `${Date.now()}`;
      const progressMessageId = `collaboration-distillation-${operationId}`;
      const operationOwnerIds = Array.from(
        new Set(
          [activeChatId, chatIdRef.current]
            .map((value) => String(value || "").trim())
            .filter(Boolean),
        ),
      );
      let progressSteps = initialCollaborationProgressSteps();
      const updateProgress = (
        options?: Parameters<typeof collaborationProgressResponse>[2],
      ) => {
        chatRef.current?.messages.upsertTransientAssistantResponse(
          operationOwnerIds,
          progressMessageId,
          collaborationProgressResponse(operationId, progressSteps, options),
        );
      };
      const applyStageEvent = (event: CollaborationDraftStageEvent) => {
        progressSteps = progressSteps.map((step) =>
          step.id === event.step
            ? {
                ...step,
                title: event.title || step.title,
                detail: event.detail,
                status: event.status,
              }
            : step,
        );
        updateProgress({ status: "running", detail: event.detail });
      };

      setDraftingCapability(true);
      capabilityDraftAbortRef.current?.abort();
      const controller = new AbortController();
      capabilityDraftAbortRef.current = controller;
      updateProgress();
      try {
        const draft = await capabilityApi.streamCollaborationDraft(
          {
            chat_id: activeChatId,
            anchor_run_id: anchor.runId,
            anchor_response_id: anchor.responseId,
          },
          {
            signal: controller.signal,
            onEvent: (event: CollaborationDraftStreamEvent) => {
              if (event.type === "stage") applyStageEvent(event);
            },
          },
        );
        progressSteps = progressSteps.map((step) => ({
          ...step,
          status: "completed",
        }));
        const scope = draft.scope;
        updateProgress({
          status: "completed",
          title:
            draft.primary_asset === "both"
              ? "已生成 Skill + 能力草稿"
              : "已生成 Skill 草稿",
          detail: scope
            ? `已吸收截至所点回复的 ${scope.turn_count} 轮协作，现在进入保存前复核。`
            : "草稿已完成，现在进入保存前复核。",
        });
        await new Promise((resolve) => window.setTimeout(resolve, 420));
        navigate("/capabilities", { state: { collaborationDraft: draft } });
        message.success("协作草稿已生成，保存前请先复核。");
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError")
          return;
        const reason =
          error instanceof Error ? error.message : "生成协作草稿失败";
        progressSteps = progressSteps.map((step) =>
          step.status === "running" ? { ...step, status: "failed" } : step,
        );
        updateProgress({
          status: "failed",
          title: "这次沉淀没有完成",
          detail: "会话本身没有被修改，可以稍后重试。",
          technicalReason: reason,
        });
        message.error(reason);
      } finally {
        if (capabilityDraftAbortRef.current === controller) {
          capabilityDraftAbortRef.current = null;
        }
        setDraftingCapability(false);
      }
    },
    [
      draftingCapability,
      message,
      navigate,
      resolveActiveChatId,
      tokenMeterRunning,
    ],
  );

  useEffect(() => {
    const root = chatStageRef.current;
    if (!root) return;

    const observer = new MutationObserver(() => {
      clearAttachmentOnlySubmitMarkerIfDetached();
    });
    observer.observe(root, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [clearAttachmentOnlySubmitMarkerIfDetached]);

  const handlePickSkill = useCallback(
    (skillName: string) => {
      setSelectedSkillName(skillName);
      getChatTextarea()?.focus();
    },
    [getChatTextarea],
  );

  const handleClearSkill = useCallback(() => {
    setSelectedSkillName(null);
    getChatTextarea()?.focus();
  }, [getChatTextarea]);

  useMessageHistoryNavigation(chatRef, isChatActive, isComposingRef);

  chatIdRef.current = chatId;
  navigateRef.current = navigate;

  const scheduleHistoryClear = useCallback(() => {
    queueMicrotask(() => {
      const ownerSessionIds = pendingClearHistoryRef.current;
      if (!ownerSessionIds) return;
      pendingClearHistoryRef.current = null;
      chatRef.current?.messages.removeAllMessages(ownerSessionIds);
    });
  }, []);

  // Tell sessionApi which session to put first in getSessionList, so the library's
  // useMount auto-selects the correct session without an extra getSession round-trip.
  if (chatId && sessionApi.preferredChatId !== chatId) {
    sessionApi.preferredChatId = chatId;
  }

  // Register session API event callbacks for URL synchronization

  useEffect(() => {
    sessionApi.onSessionIdResolved = (tempId, realId) => {
      if (!isChatActiveRef.current) return;
      migrateArtifactWorkbenchState(tempId, realId);
      if (embedded) {
        if (
          codingMode &&
          codingChatId &&
          !matchesChatSelection(codingChatId, tempId, realId)
        ) {
          return;
        }
        lastSessionIdRef.current = realId;
        setEmbeddedChatId(realId);
        if (codingMode) {
          storeCodingChatId(selectedAgent, codingWorkspaceDir, realId);
        }
        return;
      }
      if (welcomeSessionIdRef.current === tempId) {
        welcomeSessionIdRef.current = realId;
      }
      // Update URL when realId is resolved, regardless of current chatId
      // (chatId may be undefined if URL was cleared in onSessionCreated)
      lastSessionIdRef.current = realId;
      setLastChatId(selectedAgent, realId);
      navigateRef.current(`/chat/${realId}`, { replace: true });
    };

    sessionApi.onSessionRemoved = (removedId) => {
      if (!isChatActiveRef.current) return;
      if (welcomeSessionIdRef.current === removedId) {
        welcomeSessionIdRef.current = null;
        setWelcomeEligible(false);
      }
      // Clear URL when current session is removed
      // Check if removed session matches current session (by realId or sessionId)
      const currentRealId = sessionApi.getRealIdForSession(
        chatIdRef.current || "",
      );
      if (chatIdRef.current === removedId || currentRealId === removedId) {
        lastSessionIdRef.current = null;
        if (embedded) {
          setEmbeddedChatId(undefined);
          if (codingMode) {
            storeCodingChatId(selectedAgent, codingWorkspaceDir, null);
          }
          return;
        }
        navigateRef.current("/chat", { replace: true });
        setLastChatId(selectedAgent, "");
      }
    };

    sessionApi.onSessionSelected = (
      sessionId: string | null | undefined,
      realId: string | null,
    ) => {
      if (!isChatActiveRef.current) return;
      // Update URL when session is selected and different from current
      const targetId = realId || sessionId;
      if (!targetId) return;

      if (embedded) {
        if (
          codingMode &&
          codingChatId &&
          !matchesChatSelection(codingChatId, sessionId, realId)
        ) {
          return;
        }
        if (targetId !== lastSessionIdRef.current) {
          lastSessionIdRef.current = targetId;
          setEmbeddedChatId(targetId);
          if (codingMode) {
            storeCodingChatId(selectedAgent, codingWorkspaceDir, targetId);
          }
        }
        return;
      }

      setWelcomeEligible(welcomeSessionIdRef.current === targetId);

      if (targetId !== lastSessionIdRef.current) {
        lastSessionIdRef.current = targetId;
        setLastChatId(selectedAgent, targetId);
        navigateRef.current(`/chat/${targetId}`, { replace: true });
      }
    };

    sessionApi.onSessionCreated = (sessionId) => {
      if (!isChatActiveRef.current) return;
      if (embedded) {
        lastSessionIdRef.current = sessionId;
        setEmbeddedChatId(sessionId);
        if (codingMode) {
          storeCodingChatId(selectedAgent, codingWorkspaceDir, sessionId);
        }
        return;
      }
      welcomeSessionIdRef.current = sessionId;
      setWelcomeEligible(true);
      setWelcomeEntryId(createWelcomeEntryId());
      // A local session is already a stable draft owner. Give it a route
      // immediately so leaving and returning to chat cannot fall back to the
      // previous conversation before the first message resolves a backend ID.
      lastSessionIdRef.current = sessionId;
      setLastChatId(selectedAgent, sessionId);
      navigateRef.current(`/chat/${sessionId}`, { replace: true });
    };

    return () => {
      sessionApi.onSessionIdResolved = null;
      sessionApi.onSessionRemoved = null;
      sessionApi.onSessionSelected = null;
      sessionApi.onSessionCreated = null;
    };
  }, [
    codingChatId,
    codingMode,
    codingWorkspaceDir,
    embedded,
    selectedAgent,
    setLastChatId,
  ]);

  // Setup multimodal capabilities tracking via custom hook

  // Refresh chat when selectedAgent changes, preserving last active chat per agent
  const prevSelectedAgentRef = useRef(selectedAgent);
  const selectedAgentRef = useRef(selectedAgent);
  selectedAgentRef.current = selectedAgent;
  useEffect(() => {
    sessionApi.activateAgent?.(selectedAgent);
    if (embedded) {
      prevSelectedAgentRef.current = selectedAgent;
      return;
    }
    const prevAgent = prevSelectedAgentRef.current;
    if (prevAgent !== selectedAgent && prevAgent !== undefined) {
      welcomeSessionIdRef.current = null;
      setWelcomeEligible(false);
      // Save current chat ID for the agent we're leaving
      const currentChatId =
        chatIdRef.current || lastSessionIdRef.current || undefined;
      if (currentChatId && prevAgent) {
        setLastChatId(prevAgent, currentChatId);
      }

      // Restore last chat ID for the agent we're switching to
      const restored = getLastChatId(selectedAgent);
      if (restored) {
        navigateRef.current(`/chat/${restored}`, { replace: true });
        sessionApi.preferredChatId = restored;
      } else {
        navigateRef.current("/chat", { replace: true });
      }
      lastSessionIdRef.current = null;

      setRefreshKey((prev) => prev + 1);
    }
    prevSelectedAgentRef.current = selectedAgent;
  }, [embedded, selectedAgent, setLastChatId, getLastChatId]);

  const copyResponse = useCallback(
    async (response: CopyableResponse) => {
      try {
        const answerText = extractCopyableText(response);
        if (!answerText) return;
        await copyText(answerText);
        message.success(t("common.copied"));
      } catch {
        message.error(t("common.copyFailed"));
      }
    },
    [message, t],
  );

  const saveResponseToWiki = useCallback(
    async (response: CopyableResponse) => {
      const content = extractCopyableText(response).trim();
      if (!content) {
        message.warning(t("workspace.noSaveableContent"));
        return;
      }
      const firstLine =
        content
          .split(/\r?\n/)
          .map((line) => line.replace(/^#+\s*/, "").trim())
          .find(Boolean) || t("workspace.savedChatAnswer");
      const title = firstLine.slice(0, 80);
      try {
        await workspaceApi.saveQueryToWiki(title, content);
        message.success(t("workspace.savedToWiki"));
      } catch (error) {
        console.error("Failed to save response to wiki", error);
        message.error(t("workspace.saveToWikiFailed"));
      }
    },
    [message, t],
  );

  const confirmSaveResponseToWiki = useCallback(
    (response: CopyableResponse) => {
      const content = extractCopyableText(response).trim();
      if (!content) {
        message.warning(t("workspace.noSaveableContent"));
        return;
      }

      modal.confirm({
        title: t("workspace.saveToWikiConfirmTitle"),
        content: t("workspace.saveToWikiConfirmDesc"),
        icon: <DatabaseOutlined />,
        okText: t("workspace.confirmSaveToWiki"),
        cancelText: t("common.cancel"),
        autoFocusButton: "cancel",
        centered: true,
        onOk: () => saveResponseToWiki(response),
      });
    },
    [message, modal, saveResponseToWiki, t],
  );

  const chatRequestAdmissionTailRef = useRef<Promise<void>>(Promise.resolve());

  const customFetch = useCallback(
    async (data: {
      input?: Array<Record<string, unknown>>;
      biz_params?: Record<string, unknown>;
      signal?: AbortSignal;
    }): Promise<Response> => {
      const { input: submittedInput = [], biz_params } = data;
      const isControlInput = biz_params?.operation === "steer";
      const requestDraftOwner = {
        agentId: selectedAgent,
        sessionId: permissionSessionKey,
      };
      const memoryInputDraft = pendingModelSetupInputRef.current;
      const submittedModelSetupInputDraft = !isControlInput
        ? loadModelSetupInputDraft(requestDraftOwner) ||
          (memoryInputDraft &&
          !memoryInputDraft.persisted &&
          sameComposerDraftOwner(memoryInputDraft, requestDraftOwner)
            ? memoryInputDraft
            : null)
        : null;
      const input = submittedModelSetupInputDraft
        ? mergeRestoredModelSetupInput(
            submittedInput,
            submittedModelSetupInputDraft.value,
          )
        : submittedInput;
      const pendingModelSetupMessage = [...input]
        .reverse()
        .find((message: any) => message.role === "user");
      const pendingModelSetupDraft = pendingModelSetupMessage
        ? extractUserMessageText(pendingModelSetupMessage).trim()
        : "";
      const submittedDeepThinking = (() => {
        if (isControlInput) return undefined;
        const value = biz_params?.deep_thinking;
        if (value && typeof value === "object" && !Array.isArray(value)) {
          const record = value as Record<string, unknown>;
          return {
            enabled: record.enabled === true,
            effort:
              typeof record.effort === "string" && record.effort.trim()
                ? record.effort
                : "high",
          };
        }
        return snapshotDeepThinkingRequest();
      })();
      const persistPendingModelSetup = () => {
        persistModelSetupDraftSnapshot(modelSetupReturnTo, {
          composerText: pendingModelSetupDraft,
          submittedInput: input,
          taskContext: {
            agentId: selectedAgent,
            ...(selectedSkillNameRef.current
              ? { selectedSkillName: selectedSkillNameRef.current }
              : {}),
            ...(submittedDeepThinking
              ? { deepThinking: submittedDeepThinking }
              : {}),
          },
        });
      };
      // The request owner is captured before the first await. Navigation and
      // agent changes are allowed while the provider/workspace probes run,
      // but they must never retarget this submission to the newly visible
      // conversation.
      const requestRouteChatId = chatIdRef.current;
      const requestAgentId = selectedAgent;
      const requestKdocsOwner = { ...kdocsReferenceOwnerRef.current };
      const submittedArtifactEditTarget = (() => {
        const meta = biz_params?.meta;
        if (!meta || typeof meta !== "object" || Array.isArray(meta)) {
          return null;
        }
        const target = (meta as Record<string, unknown>).artifact_edit_target;
        return target && typeof target === "object" && !Array.isArray(target)
          ? (target as ArtifactEditTarget)
          : null;
      })();
      const memoryArtifactDraft = artifactEditDraftRef.current;
      const activeArtifactDraft =
        loadArtifactEditDraft(requestDraftOwner) ||
        (memoryArtifactDraft &&
        !memoryArtifactDraft.persisted &&
        sameComposerDraftOwner(memoryArtifactDraft, requestDraftOwner)
          ? memoryArtifactDraft
          : null);
      const submittedArtifactDraft =
        submittedArtifactEditTarget &&
        activeArtifactDraft &&
        sameArtifactEditTarget(
          activeArtifactDraft.value.target,
          submittedArtifactEditTarget,
        )
          ? activeArtifactDraft
          : null;
      const submittedKdocsReferenceKeys = new Set(
        (() => {
          const meta = biz_params?.meta;
          if (!meta || typeof meta !== "object" || Array.isArray(meta)) {
            return [];
          }
          const references = (meta as Record<string, unknown>).kdocs_references;
          if (!Array.isArray(references)) return [];
          return references
            .map((reference) =>
              reference &&
              typeof reference === "object" &&
              !Array.isArray(reference) &&
              typeof (reference as Record<string, unknown>).key === "string"
                ? String((reference as Record<string, unknown>).key)
                : "",
            )
            .filter(Boolean);
        })(),
      );
      const previousAdmission = chatRequestAdmissionTailRef.current;
      let releaseAdmissionPromise: (() => void) | undefined;
      chatRequestAdmissionTailRef.current = new Promise<void>((resolve) => {
        releaseAdmissionPromise = resolve;
      });
      let admissionReleased = false;
      const releaseAdmission = () => {
        if (admissionReleased) return;
        admissionReleased = true;
        releaseAdmissionPromise?.();
      };
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        ...buildAuthHeaders(),
      };

      if (!isControlInput) {
        try {
          const activeModels = await providerApi.getActiveModels({
            scope: "effective",
            agent_id: selectedAgent,
          });
          const visibleActiveModels =
            stripProductHiddenActiveModel(activeModels);
          if (
            !visibleActiveModels?.active_llm?.provider_id ||
            !visibleActiveModels?.active_llm?.model
          ) {
            persistPendingModelSetup();
            setShowModelPrompt(true);
            setTokenMeterRunning(false);
            releaseAdmission();
            return buildModelError();
          }
        } catch {
          persistPendingModelSetup();
          setShowModelPrompt(true);
          setTokenMeterRunning(false);
          releaseAdmission();
          return buildModelError();
        }
      }

      if (!isControlInput) {
        setTokenMeterRunning(true);
      }
      const stagedUserMessageIds = new Set<string>();

      try {
        const session: SessionInfo = input[input.length - 1]?.session || {};
        const lastInput = input.slice(-1);
        const lastMsg = lastInput[0];
        const skillNameForRequest = isControlInput
          ? null
          : selectedSkillNameRef.current;
        const normalizedLastMsg = lastMsg?.content
          ? normalizeOutgoingMessage(lastMsg)
          : lastMsg;
        const shouldInjectSkill =
          !!skillNameForRequest &&
          !messageStartsWithSlashCommand(normalizedLastMsg);
        const rewrittenInput = lastMsg?.content
          ? [
              injectSkillInvocation(
                normalizedLastMsg,
                shouldInjectSkill ? skillNameForRequest : null,
              ),
            ]
          : lastInput;
        if (!isControlInput && skillNameForRequest) {
          selectedSkillNameRef.current = null;
          setSelectedSkillName(null);
        }
        const requestCodingSessionId =
          !isControlInput && (codingMode || codingWorkspaceDir)
            ? activeCodingSessionId
            : "";
        const codingContextText =
          !isControlInput && (codingMode || codingWorkspaceDir)
            ? buildCodingContextText({
                ...codingContext,
                workspaceDir:
                  codingContext?.workspaceDir || codingWorkspaceDir || null,
              })
            : "";
        const clientTurnId =
          typeof crypto !== "undefined" && crypto.randomUUID
            ? `client-turn-${crypto.randomUUID()}`
            : `client-turn-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
        const controlMeta =
          biz_params?.meta &&
          typeof biz_params.meta === "object" &&
          !Array.isArray(biz_params.meta)
            ? biz_params.meta
            : undefined;
        const requestBizParams = isControlInput
          ? {
              operation: "steer",
              ...(controlMeta ? { meta: controlMeta } : {}),
            }
          : biz_params;

        const requestBody = {
          input: rewrittenInput,
          // The submitted message envelope is the authoritative conversation
          // identity. Window globals are a compatibility projection for older
          // integrations and can lag behind a fast session switch.
          session_id: requestCodingSessionId || session?.session_id || "",
          user_id: session?.user_id || DEFAULT_USER_ID,
          channel: session?.channel || DEFAULT_CHANNEL,
          stream: true,
          ...requestBizParams,
          // Freeze this at customFetch admission, before provider/workspace
          // preparation can yield.  The vendor submit boundary normally
          // supplies the same immutable value in biz_params; the render-state
          // fallback keeps the visible control and transport atomic even if a
          // caller omits that optional extension.  Place it after biz_params
          // so undefined/stale caller data cannot erase the admitted choice.
          ...(submittedDeepThinking
            ? { deep_thinking: submittedDeepThinking }
            : {}),
          ...(!isControlInput
            ? permissionRequestForPreset(permissionPresetRef.current)
            : {}),
        };
        const userText = rewrittenInput
          .filter((m: any) => m.role === "user")
          .map(extractUserMessageText)
          .join("\n")
          .trim();
        const requestBodyRecord = requestBody as Record<string, any>;
        const existingRequestMeta =
          requestBodyRecord.meta &&
          typeof requestBodyRecord.meta === "object" &&
          !Array.isArray(requestBodyRecord.meta)
            ? (requestBodyRecord.meta as Record<string, unknown>)
            : {};
        requestBodyRecord.meta = {
          ...existingRequestMeta,
          client_turn_id:
            typeof existingRequestMeta.client_turn_id === "string" &&
            existingRequestMeta.client_turn_id.trim()
              ? existingRequestMeta.client_turn_id
              : clientTurnId,
          ...(!isControlInput
            ? { work_method_id: workMethodIdRef.current }
            : {}),
        };
        if (!isControlInput && (codingMode || codingWorkspaceDir)) {
          Object.assign(requestBody, {
            coding_mode: true,
            coding_context: codingContextText,
            coding_state_snapshot: codingStateSnapshot || undefined,
            manual_workspace_dir: codingWorkspaceGrantId
              ? undefined
              : codingWorkspaceDir || undefined,
            workspace_dir: codingWorkspaceGrantId
              ? undefined
              : codingWorkspaceDir || undefined,
            coding_workspace_dir: codingWorkspaceGrantId
              ? undefined
              : codingWorkspaceDir || undefined,
            workspace_grant_id: codingWorkspaceGrantId || undefined,
          });
        }

        const cachePendingUserMessage = (
          ids: Array<string | null | undefined>,
        ) => {
          if (!isControlInput && userText) {
            ids.forEach((id) => {
              const normalized = String(id || "").trim();
              if (normalized) stagedUserMessageIds.add(normalized);
            });
            sessionApi.stageLastUserMessage(ids, userText);
          }
        };
        cachePendingUserMessage([
          requestBody.session_id,
          session?.session_id,
          requestRouteChatId,
        ]);

        let ensuredChatId: string | null = null;
        if (!isControlInput) {
          try {
            const preEnsureSessionId = requestBody.session_id;
            const workspace = await chatApi.ensureTopicWorkspace({
              chat_id:
                sessionApi.getRealIdForSession(preEnsureSessionId) ||
                requestRouteChatId ||
                undefined,
              session_id: preEnsureSessionId,
              user_id: requestBody.user_id,
              channel: requestBody.channel,
              name: "新会话",
              manual_workspace_dir: codingWorkspaceGrantId
                ? undefined
                : codingWorkspaceDir || undefined,
              coding_mode: codingMode || undefined,
              coding_workspace_dir: codingWorkspaceGrantId
                ? undefined
                : codingWorkspaceDir || undefined,
              workspace_grant_id: codingWorkspaceGrantId || undefined,
            });
            ensuredChatId = workspace.chat.id;
            const ensuredSessionId =
              workspace.chat.session_id || preEnsureSessionId;
            const ensuredUserId = workspace.chat.user_id || requestBody.user_id;
            const ensuredChannel =
              workspace.chat.channel || requestBody.channel;
            sessionApi.resolveEnsuredSession(
              [preEnsureSessionId, session?.session_id, requestRouteChatId],
              workspace.chat.id,
              ensuredSessionId,
            );
            const currentRouteChatId = chatIdRef.current;
            const workspaceOwnerIds = new Set(
              [
                requestRouteChatId,
                preEnsureSessionId,
                session?.session_id,
                workspace.chat.id,
                ensuredSessionId,
              ]
                .map((value) => String(value || "").trim())
                .filter(Boolean),
            );
            const ownsVisibleWorkspace =
              selectedAgentRef.current === requestAgentId &&
              (!currentRouteChatId ||
                workspaceOwnerIds.has(currentRouteChatId));
            if (
              ownsVisibleWorkspace &&
              (workspace.workspace_grant_id || codingWorkspaceGrantId)
            ) {
              setActiveTopicWorkspace({
                agentId: selectedAgent,
                sessionId: ensuredSessionId,
                chatId: workspace.chat.id,
                topicWorkspaceDir: workspace.topic_workspace_dir,
                manualWorkspaceDir: workspace.manual_workspace_dir || null,
                effectiveWorkspaceDir:
                  workspace.project_workspace_dir ||
                  workspace.effective_workspace_dir,
                projectWorkspaceDir:
                  workspace.project_workspace_dir || codingWorkspaceDir,
                workspaceGrantId:
                  workspace.workspace_grant_id ||
                  codingWorkspaceGrantId ||
                  null,
                workspaceAccessMode: workspace.workspace_access_mode || null,
                workspaceTrustState: workspace.workspace_trust_state || null,
              });
            } else if (ownsVisibleWorkspace) {
              updateActiveTopicWorkspaceFromMeta(
                selectedAgent,
                ensuredSessionId,
                workspace.chat.meta || {},
                workspace.chat.id,
              );
            }
            Object.assign(requestBody, {
              session_id: ensuredSessionId,
              user_id: ensuredUserId,
              channel: ensuredChannel,
              workspace_dir: workspace.effective_workspace_dir,
              topic_workspace_dir: workspace.topic_workspace_dir,
              manual_workspace_dir: workspace.manual_workspace_dir || undefined,
              workspace_grant_id:
                workspace.workspace_grant_id ||
                codingWorkspaceGrantId ||
                undefined,
            });
            sessionApi.rememberSessionAlias(
              preEnsureSessionId,
              workspace.chat.id,
              ensuredSessionId,
              requestRouteChatId,
            );
            if (ensuredSessionId !== preEnsureSessionId) {
              sessionApi.rememberSessionAlias(
                ensuredSessionId,
                workspace.chat.id,
                ensuredSessionId,
                requestRouteChatId,
              );
            }
            if (codingMode || codingWorkspaceDir) {
              onCodingChatResolved?.(workspace.chat);
              storeCodingChatId(
                selectedAgent,
                codingWorkspaceDir,
                workspace.chat.id,
              );
            }
            cachePendingUserMessage([
              workspace.chat.id,
              workspace.chat.session_id,
              requestBody.session_id,
              session?.session_id,
              requestRouteChatId,
            ]);
            persistChatPermissionPreset(
              selectedAgent,
              [
                workspace.chat.id,
                workspace.chat.session_id,
                requestBody.session_id,
                session?.session_id,
                requestRouteChatId,
              ],
              permissionPresetRef.current,
            );
            persistWorkMethodId(
              selectedAgent,
              [
                workspace.chat.id,
                workspace.chat.session_id,
                requestBody.session_id,
                session?.session_id,
                requestRouteChatId,
              ],
              workMethodIdRef.current,
            );
          } catch (error) {
            console.warn("Failed to ensure topic workspace:", error);
          }
        }

        const backendChatId =
          ensuredChatId ??
          sessionApi.getRealIdForSession(requestBody.session_id) ??
          requestRouteChatId ??
          requestBody.session_id;
        if (backendChatId) {
          cachePendingUserMessage([
            backendChatId,
            requestBody.session_id,
            session?.session_id,
            requestRouteChatId,
          ]);
        }

        if (backendChatId) {
          requestBodyRecord.chat_id = backendChatId;
        }

        // Headers for the active stream arrive immediately, so serialising
        // only request admission does not block streaming. It does preserve
        // the user's Enter order when two steer POSTs race through workspace
        // preparation and would otherwise be appended to the Run Inbox in
        // reverse order.
        await previousAdmission;
        let response: Response;
        try {
          response = await fetch(getApiUrl("/console/chat"), {
            method: "POST",
            headers,
            body: JSON.stringify(requestBody),
            signal: data.signal,
          });
        } finally {
          releaseAdmission();
        }

        if (!response.ok) {
          sessionApi.discardPendingUserMessage(
            Array.from(stagedUserMessageIds),
          );
          if (!isControlInput) {
            setTokenMeterRunning(false);
            runtimeLoadingBridgeRef.current?.setLoading?.(false);
          }
          return await buildChatHttpErrorResponse(response);
        }

        if (submittedKdocsReferenceKeys.size > 0) {
          if (kdocsReferenceScopeRef.current === requestKdocsOwner.scope) {
            const remaining = kdocsReferencesRef.current.filter(
              (reference) => !submittedKdocsReferenceKeys.has(reference.key),
            );
            kdocsReferencesRef.current = remaining;
            setKdocsReferences(remaining);
            saveKdocsReferences(
              requestKdocsOwner.agentId,
              requestKdocsOwner.sessionId,
              remaining,
            );
          } else {
            const stored = loadKdocsReferences(
              requestKdocsOwner.agentId,
              requestKdocsOwner.sessionId,
            );
            saveKdocsReferences(
              requestKdocsOwner.agentId,
              requestKdocsOwner.sessionId,
              stored.filter(
                (reference) => !submittedKdocsReferenceKeys.has(reference.key),
              ),
            );
          }
        }

        if (submittedArtifactDraft) {
          clearStoredArtifactEditDraft(
            requestDraftOwner,
            submittedArtifactDraft.revision,
          );
          if (
            sameComposerDraftOwner(
              requestDraftOwner,
              composerDraftOwnerRef.current,
            ) &&
            artifactEditDraftRef.current?.revision ===
              submittedArtifactDraft.revision
          ) {
            artifactEditDraftRef.current = null;
            setArtifactEditDraft(null);
          }
        }
        if (submittedModelSetupInputDraft) {
          clearModelSetupInputDraft(
            requestDraftOwner,
            submittedModelSetupInputDraft.revision,
          );
          if (
            sameComposerDraftOwner(
              requestDraftOwner,
              composerDraftOwnerRef.current,
            ) &&
            pendingModelSetupInputRef.current?.revision ===
              submittedModelSetupInputDraft.revision
          ) {
            pendingModelSetupInputRef.current = null;
          }
        }

        if (!isControlInput && userText) {
          const acceptedClientTurnId = String(
            requestBodyRecord.meta?.client_turn_id || clientTurnId,
          ).trim();
          sessionApi.acceptLastUserMessage(
            Array.from(stagedUserMessageIds),
            userText,
            acceptedClientTurnId,
          );

          // A welcome belongs only to the untouched draft entry. Once its
          // first user turn is accepted, retire that ownership permanently so
          // switching away and back cannot make an existing conversation look
          // like a fresh chat or schedule another welcome preparation.
          const welcomeOwnerId = welcomeSessionIdRef.current;
          if (welcomeOwnerId && stagedUserMessageIds.has(welcomeOwnerId)) {
            welcomeSessionIdRef.current = null;
            setWelcomeEligible(false);
          }
        }

        return response;
      } catch (error) {
        releaseAdmission();
        sessionApi.discardPendingUserMessage(Array.from(stagedUserMessageIds));
        if (!data.signal?.aborted) {
          console.error("Chat request failed before completion:", error);
        }
        if (!isControlInput) {
          setTokenMeterRunning(false);
          runtimeLoadingBridgeRef.current?.setLoading?.(false);
        }
        return buildChatRequestError(error);
      }
    },
    [
      activeCodingSessionId,
      codingContext,
      codingMode,
      codingStateSnapshot,
      codingWorkspaceDir,
      codingWorkspaceGrantId,
      modelSetupReturnTo,
      permissionSessionKey,
      onCodingChatResolved,
      selectedAgent,
      snapshotDeepThinkingRequest,
    ],
  );

  const handleFileUpload = useCallback(
    async (options: {
      file: File;
      onSuccess: (body: {
        url?: string;
        thumbUrl?: string;
        canonicalPath?: string;
        storedName?: string;
        digest?: string;
        immutableVersion?: number;
        uploadReceipt?: string;
      }) => void;
      onError?: (e: Error) => void;
      onProgress?: (e: { percent?: number }) => void;
    }) => {
      const { file, onSuccess, onError, onProgress } = options;
      try {
        const isMediaAttachment =
          file.type.startsWith("image/") ||
          file.type.startsWith("video/") ||
          file.type.startsWith("audio/");
        if (isMediaAttachment && !multimodalCaps.supportsMultimodal) {
          message.warning(t("chat.attachments.multimodalWarning"));
        } else if (
          file.type.startsWith("video/") &&
          multimodalCaps.supportsImage &&
          !multimodalCaps.supportsVideo
        ) {
          message.warning(t("chat.attachments.imageOnlyWarning"));
        }
        const sizeMb = file.size / 1024 / 1024;
        const isWithinLimit = file.size <= CHAT_ATTACHMENT_MAX_BYTES;

        if (!isWithinLimit) {
          message.error(
            t("chat.attachments.fileSizeExceeded", {
              limit: CHAT_ATTACHMENT_MAX_MB,
              size: sizeMb.toFixed(2),
            }),
          );
          onError?.(new Error(`File size exceeds ${CHAT_ATTACHMENT_MAX_MB}MB`));
          return;
        }

        const controller = new AbortController();
        const timeoutId = window.setTimeout(() => {
          controller.abort(
            new DOMException("附件上传超时，请重试", "TimeoutError"),
          );
        }, CHAT_ATTACHMENT_UPLOAD_TIMEOUT_MS);
        try {
          const res = await chatApi.uploadFile(file, {
            signal: controller.signal,
          });
          onProgress?.({ percent: 100 });
          onSuccess(chatUploadResponseToAttachmentSuccess(res));
          ensureAttachmentOnlySubmitMarker();
        } finally {
          window.clearTimeout(timeoutId);
        }
      } catch (e) {
        const error =
          e instanceof Error
            ? e
            : new Error(String(e || "附件上传失败，请重试"));
        if (error.name === "TimeoutError" || error.name === "AbortError") {
          onError?.(new Error("附件上传超时，请重试"));
          return;
        }
        onError?.(error);
      }
    },
    [ensureAttachmentOnlySubmitMarker, message, multimodalCaps, t],
  );

  const options = useMemo(() => {
    const i18nConfig = getDefaultConfig(t);
    const commandSuggestions: CommandSuggestion[] = [
      {
        command: "/clear",
        value: "clear",
        description: t("chat.commands.clear.description"),
      },
      {
        command: "/compact",
        value: "compact",
        description: t("chat.commands.compact.description"),
      },
      {
        command: "/mission",
        value: "mission",
        description: t("chat.commands.mission.description"),
      },
      {
        command: "/skills",
        value: "skills",
        description: t("chat.commands.skills.description"),
      },
    ];
    if (planEnabled) {
      commandSuggestions.push({
        command: "/plan",
        value: "plan ",
        description: t("chat.commands.plan.description"),
      });
    }

    const tokenUsageMeter = (
      <ChatTokenUsageMeter
        usage={latestTokenUsage}
        running={tokenMeterRunning}
        contextStats={latestContextStats}
        routing={latestRouting}
        contextWindowTarget={contextWindowTarget}
        onSaveContextWindow={saveActiveModelContextWindow}
        onPrepareCompact={handlePrepareContextCompact}
        onCreateBranch={handleCreateContextBranch}
        onOpenChange={(open) => {
          if (open) {
            void refreshContextStats();
            void refreshRuntimeUsage();
          }
        }}
      />
    );

    const senderPrefix = (
      <>
        <span className={styles.senderCapabilityGroup}>
          <CapabilityMenuButton
            selectedAgent={selectedAgent}
            workMethods={workMethods}
            workMethodId={workMethodId}
            onWorkMethodChange={handleWorkMethodChange}
            skillsEnabled={!codingMode}
            selectedSkillName={selectedSkillName}
            onPickSkill={handlePickSkill}
            onClearSkill={handleClearSkill}
            onManageSkills={() => navigate("/skills")}
            planEnabled={planEnabled}
            planDraftMode={planDraftMode}
            onEnsurePlanEnabled={handleEnsurePlanEnabled}
            onPlanDraftModeChange={setPlanDraftMode}
            goalDraftMode={goalDraftMode}
            onGoalDraftModeChange={setGoalDraftMode}
            onManageConnectors={() => navigate("/mcp")}
          />
          <PermissionModeSelector
            value={permissionPreset}
            allowFullAccess={allowFullPermission !== false}
            onChange={handlePermissionPresetChange}
            onOpenCustom={() => navigate("/permissions")}
          />
          <Tooltip title="添加金山云文档">
            <IconButton
              aria-label="添加金山云文档"
              bordered={false}
              className={styles.senderIconButton}
              icon={<BookOpenText size={16} strokeWidth={1.9} />}
              onClick={() => setKdocsPickerOpen(true)}
            />
          </Tooltip>
          <Tooltip
            title={t(
              !multimodalCaps.supportsDeepThinking
                ? "chat.deepThinking.tooltipUnavailable"
                : deepThinkingEnabled
                  ? "chat.deepThinking.tooltipOn"
                  : "chat.deepThinking.tooltipOff",
            )}
          >
            <span className={styles.deepThinkingButtonShell}>
              <IconButton
                aria-label={t("chat.deepThinking.label")}
                aria-pressed={deepThinkingEnabled}
                data-chat-deep-thinking="1"
                bordered={false}
                className={`${styles.deepThinkingButton} ${
                  deepThinkingEnabled ? styles.deepThinkingButtonActive : ""
                }`}
                disabled={
                  !multimodalCaps.supportsDeepThinking ||
                  multimodalCaps.deepThinkingFixed
                }
                icon={<Brain size={16} strokeWidth={2} />}
                onClick={() => {
                  if (!multimodalCaps.supportsDeepThinking) return;
                  const nextValue = !deepThinkingEnabledRef.current;
                  deepThinkingEnabledRef.current = nextValue;
                  setDeepThinkingEnabled(nextValue);
                }}
              />
            </span>
          </Tooltip>
        </span>
        <span className={styles.senderToolbarDivider} aria-hidden="true" />
      </>
    );

    const handleBeforeSubmit = async () => {
      if (isComposingRef.current) return false;

      const textarea = getChatTextarea();
      const value = textarea?.value ?? "";
      let cleanValue = stripAttachmentSubmitMarker(value).trim();
      if (
        value.includes(ATTACHMENT_ONLY_SUBMIT_MARKER) &&
        cleanValue.length === 0 &&
        !hasAttachmentCards()
      ) {
        if (textarea) {
          setTextareaValue(textarea, "");
        }
        return false;
      }

      const kdocsSubmit = resolveKdocsSubmitDraft(
        cleanValue,
        kdocsReferencesRef.current,
      );
      if (kdocsSubmit.blocked) {
        message.info("正在读取引用文档，完成后即可发送");
        return false;
      }

      if (kdocsSubmit.value !== cleanValue && textarea) {
        setTextareaValue(textarea, kdocsSubmit.value);
      }
      cleanValue = kdocsSubmit.value;

      if (
        cleanValue.length > 0 ||
        hasAttachmentCards() ||
        kdocsReferencesRef.current.length > 0
      ) {
        startFirstTurnTransition();
      }

      if (planEnabled && cleanValue.length > 0 && !isPlanCommand(cleanValue)) {
        const applyPlanPrompt = () => {
          if (textarea) {
            setTextareaValue(textarea, buildPlanPrompt(cleanValue));
          }
        };

        if (goalDraftMode) {
          applyPlanPrompt();
          setGoalDraftMode(false);
          message.info("已切换为目标模式，先建立可跟踪目标，再持续推进。");
          return true;
        }

        if (planDraftMode) {
          applyPlanPrompt();
          setPlanDraftMode(false);
          message.info("已切换为计划模式，先生成执行方案。");
          return true;
        }
      }

      return true;
    };

    return {
      ...i18nConfig,
      theme: {
        ...defaultConfig.theme,
        darkMode: isDark,
        leftHeader: {
          ...defaultConfig.theme.leftHeader,
        },
        rightHeader: codingMode ? (
          <div className={styles.codingChatHeader}>
            <RuntimeEventStreamBridge
              chatId={chatId}
              refreshKey={refreshKey}
              onUpdate={applyRuntimeEventsSnapshot}
            />
            <ChatSessionInitializer />
            <RuntimeLoadingBridge bridgeRef={runtimeLoadingBridgeRef} />
          </div>
        ) : (
          <div className={styles.chatHeaderControls}>
            <div className={styles.chatHeaderContext}>
              <ChatHeaderTitle />
              <PinnedSessionStrip
                onOpenAll={() => setSessionDrawerOpen(true)}
              />
              <RuntimeEventStreamBridge
                chatId={chatId}
                refreshKey={refreshKey}
                onUpdate={applyRuntimeEventsSnapshot}
              />
            </div>
            <div className={styles.chatHeaderActions}>
              <Tooltip title={t("chat.chatHistoryTooltip")}>
                <IconButton
                  bordered={false}
                  icon={<SparkHistoryLine />}
                  data-testid="chat-history-button"
                  aria-label={t("chat.chatHistoryTooltip")}
                  onClick={() => setSessionDrawerOpen(true)}
                />
              </Tooltip>
              <TurnEventPreview />
              <ChatSessionInitializer />
              <RuntimeLoadingBridge bridgeRef={runtimeLoadingBridgeRef} />
              <Tooltip title={kdocsPanelOpen ? "收起金山文档" : "浏览金山文档"}>
                <button
                  type="button"
                  className={styles.artifactHeaderButton}
                  aria-label={kdocsPanelOpen ? "收起金山文档" : "浏览金山文档"}
                  data-testid="chat-kdocs-panel-toggle"
                  data-active={kdocsPanelOpen ? "true" : "false"}
                  onClick={() => {
                    setKdocsPanelOpen((value) => {
                      const next = !value;
                      if (next) {
                        setArtifactPanelOpen(false);
                        setArtifactRequestedPath(null);
                        setSubAgentInspectionTarget(null);
                        setBrowserSessionTarget(null);
                        setPrivateEditorPanelOpen(false);
                      }
                      return next;
                    });
                  }}
                >
                  <BookOpenText
                    size={16}
                    strokeWidth={1.9}
                    aria-hidden="true"
                  />
                  <span>文档</span>
                </button>
              </Tooltip>
              <Tooltip
                title={artifactPanelOpen ? "收起任务产物" : "查看任务产物"}
              >
                <button
                  type="button"
                  className={styles.artifactHeaderButton}
                  aria-label={
                    artifactPanelOpen ? "收起任务产物" : "查看任务产物"
                  }
                  data-testid="chat-artifact-panel-toggle"
                  data-active={artifactPanelOpen ? "true" : "false"}
                  onClick={() => {
                    setArtifactPanelOpen((value) => {
                      const next = !value;
                      if (next) {
                        setKdocsPanelOpen(false);
                        setKdocsRequestedDocument(null);
                        setSubAgentInspectionTarget(null);
                        setBrowserSessionTarget(null);
                        setPrivateEditorPanelOpen(false);
                      }
                      return next;
                    });
                  }}
                >
                  <PanelRightOpen size={16} aria-hidden="true" />
                  <span>成果</span>
                </button>
              </Tooltip>
            </div>
            <ChatSessionDrawer
              open={sessionDrawerOpen}
              onClose={() => setSessionDrawerOpen(false)}
            />
          </div>
        ),
      },
      welcome: {
        ...i18nConfig.welcome,
        greeting: emptyWelcomeGreeting,
        description: emptyWelcomeDescription,
        prompts: [],
        nick: getAgentDisplayName(
          agents.find((a) => a.id === selectedAgent) ?? {
            id: selectedAgent,
            name: "沈行知",
          },
          t,
        ),
        avatar: "./lantai.png",
      },
      sender: {
        ...(i18nConfig as any)?.sender,
        outsideContext: codingMode ? null : (
          <TurnEventPreview variant="presence" />
        ),
        context: codingMode ? null : (
          <>
            <ArtifactEditTargetChip
              draft={artifactEditDraft}
              onClear={clearArtifactEditDraft}
            />
            <KdocsComposerContext
              references={kdocsReferences}
              getTextarea={getChatTextarea}
              setTextareaText={setTextareaValue}
              isComposingRef={isComposingRef}
              onAddDocument={handleAddKdocsDocument}
              onOpenReference={handleOpenKdocsReference}
              onRemoveReference={handleRemoveKdocsReference}
              onRetryReference={handleRetryKdocsReference}
            />
            <MacUseTargetBanner
              target={macUseDesktopAvailable ? macUseTarget : null}
              onClear={() => handleMacUseTargetChange(null)}
            />
            <KnowledgeSourceBanner />
            <WorkEditSessionBanner />
            {briefDraftId && <DailyBriefDiscussionDraft key={`${selectedAgent}:${briefDraftId}`}
              draftId={briefDraftId} onInsert={insertBriefDraft} onClose={closeBriefDraft} />}
          </>
        ),
        beforeSubmit: handleBeforeSubmit,
        allowSpeech: false,
        prefix: senderPrefix,
        beforeAttachments: null,
        afterAttachments: tokenUsageMeter,
        attachments: {
          multiple: true,
          trigger: function (props: any) {
            return (
              <Tooltip
                title={t("chat.attachments.tooltip", {
                  limit: CHAT_ATTACHMENT_MAX_MB,
                })}
              >
                <IconButton
                  aria-label={t("chat.attachments.tooltip", {
                    limit: CHAT_ATTACHMENT_MAX_MB,
                  })}
                  className={styles.senderIconButton}
                  disabled={props?.disabled}
                  icon={<SparkAttachmentLine />}
                  bordered={false}
                />
              </Tooltip>
            );
          },
          customRequest: handleFileUpload,
        },
        placeholder: codingMode
          ? "描述要改的代码或任务... (/ commands)"
          : t("chat.inputPlaceholder"),
        ariaLabel: codingMode ? "输入代码修改或任务" : "输入消息",
        suggestions: commandSuggestions.map((item) => ({
          label: renderSuggestionLabel(item.command, item.description),
          value: item.value,
          command: item.command,
          description: item.description,
        })),
      },
      session: {
        multiple: true,
        hideBuiltInSessionList: true,
        initialSessionId: chatId,
        initialSessionStrategy: "first",
        api: sessionApi,
      },
      cards: {
        [LANTAI_RESPONSE_CARD_CODE]: RuntimeResponseCard,
      },
      api: {
        ...defaultConfig.api,
        messageSource: runtimeMessageSource,
        // Freeze controls from the same React render that the user can see.
        // customFetch performs asynchronous workspace/model preparation, so
        // reading mutable refs only at transport time can serialize a newer
        // state than the one the user submitted. The vendor submit boundary
        // places this immutable snapshot in biz_params; its values remain
        // authoritative when customFetch assembles the final request.
        getRequestBizParams: () => {
          const deepThinking = snapshotDeepThinkingRequest();
          const params: Record<string, unknown> = {};
          if (deepThinking) params.deep_thinking = deepThinking;
          const meta: Record<string, unknown> = {};
          if (macUseDesktopAvailable && macUseTarget) {
            meta.mac_use_target = macUseTarget;
          }
          if (kdocsReferencesRef.current.length > 0) {
            meta.kdocs_references = snapshotKdocsReferencesForRequest(
              kdocsReferencesRef.current,
            );
          }
          const draftOwner = composerDraftOwnerRef.current;
          const memoryDraft = artifactEditDraftRef.current;
          const ownedArtifactDraft =
            loadArtifactEditDraft(draftOwner) ||
            (memoryDraft &&
            !memoryDraft.persisted &&
            sameComposerDraftOwner(memoryDraft, draftOwner)
              ? memoryDraft
              : null);
          if (ownedArtifactDraft) {
            meta.artifact_edit_target = ownedArtifactDraft.value.target;
          }
          if (Object.keys(meta).length > 0) {
            params.meta = meta;
          }
          return params;
        },
        getControlInputStatus: async (data: {
          session_id: string;
          chat_id?: string;
          command_id: string;
        }) => {
          const query = new URLSearchParams({
            session_id: data.session_id,
            command_id: data.command_id,
          });
          if (data.chat_id) query.set("chat_id", data.chat_id);
          const response = await fetch(
            getApiUrl(`/console/chat/control-input?${query.toString()}`),
            {
              method: "GET",
              headers: buildAuthHeaders(),
            },
          );
          if (response.status === 404) return null;
          if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
          }
          return response.json();
        },
        cancelControlInput: async (data: {
          session_id: string;
          chat_id?: string;
          command_id: string;
        }) => {
          const query = new URLSearchParams({
            session_id: data.session_id,
            command_id: data.command_id,
          });
          if (data.chat_id) query.set("chat_id", data.chat_id);
          const response = await fetch(
            getApiUrl(`/console/chat/control-input/cancel?${query.toString()}`),
            {
              method: "POST",
              headers: buildAuthHeaders(),
            },
          );
          const payload = (await response.json().catch(() => null)) as Record<
            string,
            unknown
          > | null;
          if (!response.ok) {
            const error = new Error(`HTTP ${response.status}`) as Error & {
              detail?: Record<string, unknown>;
            };
            const detail = payload?.detail;
            if (detail && typeof detail === "object") {
              error.detail = detail as Record<string, unknown>;
            }
            throw error;
          }
          return payload || {};
        },
        fetch: customFetch,
        responseParser: (chunk: string) => {
          const payload = normalizeChatStreamPayload(
            JSON.parse(chunk) as Record<string, unknown>,
          );
          const usage = normalizeTokenUsage(payload.usage, maxContextTokens);
          if (usage) {
            setLatestTokenUsage(usage);
          }

          if (payloadRequestsHistoryClear(payload)) {
            const ownerSessionIds = payloadConversationOwnerIds(payload);
            // History clear is destructive. If the stream cannot prove which
            // conversation owns the command, fail closed instead of clearing
            // whichever chat happens to be visible after navigation.
            pendingClearHistoryRef.current =
              ownerSessionIds.length > 0 ? ownerSessionIds : null;
            if (payloadCompletesResponse(payload)) {
              scheduleHistoryClear();
            }
          }
          if (
            payload.object === "response" &&
            (payload.status === "completed" || payload.status === "failed")
          ) {
            setTokenMeterRunning(false);
            void refreshContextStats();
          }
          return payload as any;
        },
        replaceMediaURL: (url: string) => {
          return toDisplayUrl(url);
        },
        cancel(data: { session_id: string }) {
          return requestStopForActiveChat(data.session_id);
        },
        async reconnect(data: {
          session_id: string;
          user_id?: string;
          channel?: string;
          signal?: AbortSignal;
        }) {
          const headers: Record<string, string> = {
            "Content-Type": "application/json",
            ...buildAuthHeaders(),
          };

          return fetch(getApiUrl("/console/chat"), {
            method: "POST",
            headers,
            body: JSON.stringify({
              reconnect: true,
              session_id:
                (codingMode || codingWorkspaceDir
                  ? activeCodingSessionId
                  : "") || data.session_id,
              user_id: data.user_id || DEFAULT_USER_ID,
              channel: data.channel || DEFAULT_CHANNEL,
              coding_mode: codingMode || undefined,
              coding_context:
                codingMode || codingWorkspaceDir
                  ? buildCodingContextText({
                      ...codingContext,
                      workspaceDir:
                        codingContext?.workspaceDir ||
                        codingWorkspaceDir ||
                        null,
                    })
                  : undefined,
              coding_state_snapshot: codingStateSnapshot || undefined,
              manual_workspace_dir: codingWorkspaceGrantId
                ? undefined
                : codingWorkspaceDir || undefined,
              workspace_dir: codingWorkspaceGrantId
                ? undefined
                : codingWorkspaceDir || undefined,
              coding_workspace_dir: codingWorkspaceGrantId
                ? undefined
                : codingWorkspaceDir || undefined,
              workspace_grant_id: codingWorkspaceGrantId || undefined,
              meta: {
                work_method_id: workMethodIdRef.current,
              },
              ...permissionRequestForPreset(permissionPresetRef.current),
            }),
            signal: data.signal,
          });
        },
      },
      renderMessageReferences: (
        message: Parameters<typeof KdocsMessageReferences>[0]["message"],
      ) => (
        <KdocsMessageReferences
          message={message}
          onOpen={handleOpenKdocsReference}
        />
      ),
      customToolRenderConfig: withDefaultToolRenderer(
        toolRenderConfig,
        agents,
        handleInspectSubAgent,
        handleInspectBrowserSession,
        macUseDesktopAvailable && !codingMode
          ? handleRequestMacUseTarget
          : undefined,
      ),
      actions: {
        list: [
          {
            icon: renderResponseActionIcon(
              <SparkCopyLine />,
              t("chat.actionCopyResponse"),
            ),
            onClick: ({ data }: { data: CopyableResponse }) => {
              void copyResponse(data);
            },
          },
          {
            icon: renderResponseActionIcon(
              <DatabaseOutlined />,
              t("workspace.saveToWiki"),
            ),
            onClick: ({ data }: { data: CopyableResponse }) => {
              confirmSaveResponseToWiki(data);
            },
          },
          {
            icon: renderResponseActionIcon(
              <SparkMagicWandLine />,
              "将此前协作沉淀为 Skill",
            ),
            onClick: ({ data }: { data: unknown }) => {
              void handleCreateCapabilityDraft(data);
            },
          },
          {
            render: ({ data }: { data: any }) => {
              const responseData = data?.data ?? data;
              const usage = normalizeTokenUsage(
                responseData?.usage,
                maxContextTokens,
              );
              const routing = routingSnapshotFromResponse(responseData);
              // Historical cards must keep their own model provenance. Using
              // the page's current routing here rewrites every old reply with
              // whichever model the user selected most recently and makes
              // separate conversations look merged.
              if (!usage && !routing) return <></>;
              return <ResponseTokenSummary usage={usage} routing={routing} />;
            },
          },
        ],
        replace: true,
      },
    } as unknown as ILantaiRuntimeWebUIOptions;
  }, [
    customFetch,
    briefDraftId,
    insertBriefDraft,
    closeBriefDraft,
    artifactEditDraft,
    clearArtifactEditDraft,
    activeCodingSessionId,
    chatId,
    embedded,
    codingContext,
    codingMode,
    codingStateSnapshot,
    codingWorkspaceDir,
    codingWorkspaceGrantId,
    copyResponse,
    confirmSaveResponseToWiki,
    handleFileUpload,
    t,
    isDark,
    latestTokenUsage,
    latestContextStats,
    latestRouting,
    tokenMeterRunning,
    draftingCapability,
    maxContextTokens,
    multimodalCaps,
    deepThinkingEnabled,
    toolRenderConfig,
    scheduleHistoryClear,
    planEnabled,
    selectedAgent,
    selectedSkillName,
    startFirstTurnTransition,
    agents,
    getChatTextarea,
    hasAttachmentCards,
    handlePickSkill,
    handleClearSkill,
    handlePrepareContextCompact,
    handleCreateContextBranch,
    handleCreateCapabilityDraft,
    applyRuntimeEventsSnapshot,
    requestStopForActiveChat,
    refreshContextStats,
    refreshRuntimeUsage,
    emptyWelcomeDescription,
    emptyWelcomeGreeting,
    planDraftMode,
    goalDraftMode,
    permissionPreset,
    macUseTarget,
    macUseDesktopAvailable,
    handleMacUseTargetChange,
    allowFullPermission,
    handlePermissionPresetChange,
    handleEnsurePlanEnabled,
    handleWorkMethodChange,
    workMethodId,
    workMethods,
    message,
    runtimeMessageSource,
    RuntimeResponseCard,
    sessionDrawerOpen,
    artifactPanelOpen,
    kdocsPanelOpen,
    kdocsReferences,
    handleAddKdocsDocument,
    handleOpenKdocsReference,
    handleRemoveKdocsReference,
    handleRetryKdocsReference,
    handleInspectSubAgent,
    handleInspectBrowserSession,
    handleRequestMacUseTarget,
    navigate,
  ]);

  return (
    <div
      className={styles.chatShell}
      data-artifact-open={rightPanelOpen ? "true" : "false"}
    >
      {privateEditorProjectId &&
      !privateEditorPanelOpen &&
      !artifactPanelOpen &&
      !kdocsPanelOpen &&
      !subAgentInspectionTarget &&
      !browserSessionTarget ? (
        <button
          type="button"
          className={styles.privateEditorReopen}
          onClick={() => setPrivateEditorPanelOpen(true)}
          aria-label="打开私人编辑作品面板"
        >
          <PanelRightOpen size={16} />
          <span>作品</span>
        </button>
      ) : null}
      <div
        ref={chatStageRef}
        className={styles.chatStage}
        data-embedded={embedded ? "true" : "false"}
        data-coding-mode={codingMode ? "true" : "false"}
        data-artifact-panel={rightPanelOpen ? "true" : "false"}
        data-empty-conversation={isEmptyConversation ? "true" : "false"}
        data-first-turn-transition={isFirstTurnTransitioning ? "true" : "false"}
        data-testid="chat-stage"
        onClickCapture={handleArtifactLinkClick}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        <div className={styles.chatMessagesArea}>
          <LantaiRuntimeWebUI
            ref={chatRef}
            key={refreshKey}
            options={options}
          />
        </div>

        {planEnabled ? (
          <div className={styles.inlinePlanMount} aria-live="polite">
            <InlinePlanPanel
              active={planEnabled}
              onRequestReplan={handleRequestReplan}
              onRequestValidation={handleRequestValidation}
            />
          </div>
        ) : null}

        {(isEmptyConversation || isFirstTurnTransitioning) && !embedded ? (
          <DailyBriefHomeShelf
            snapshot={dailyBriefHome}
            continuationItems={continuationSuggestions}
            onOpenBrief={handleOpenDailyBrief}
            onOpenContinuation={(item) => navigate(item.actionPath)}
          />
        ) : null}

        {(isEmptyConversation || isFirstTurnTransitioning) && !embedded ? (
          todayBrief && todayBrief.high_value_count > 0 ? (
            <button
              type="button"
              className={styles.dailyBriefStrip}
              aria-hidden={isFirstTurnTransitioning ? true : undefined}
              tabIndex={isFirstTurnTransitioning ? -1 : undefined}
              onClick={() => navigate("/reminders")}
            >
              <Bell size={15} />
              <span>今日有 {todayBrief.high_value_count} 条高价值提醒</span>
              <strong>查看</strong>
            </button>
          ) : null
        ) : null}

        {(isEmptyConversation || isFirstTurnTransitioning) && !embedded ? (
          <div
            className={styles.emptyPromptChips}
            role="group"
            aria-hidden={isFirstTurnTransitioning ? true : undefined}
            aria-label={t("chat.emptyPromptSuggestionsLabel", {
              defaultValue: "新会话推荐词",
            })}
          >
            {emptyPromptSuggestions.map((suggestion, index) => {
              const Icon = EMPTY_PROMPT_ICONS[index] ?? FileText;
              return (
                <button
                  key={`${suggestion.label}:${suggestion.prompt}`}
                  type="button"
                  className={styles.emptyPromptChip}
                  tabIndex={isFirstTurnTransitioning ? -1 : undefined}
                  onClick={() => handleEmptyPromptClick(suggestion.prompt)}
                >
                  <Icon size={16} strokeWidth={1.8} aria-hidden="true" />
                  <span>{suggestion.label}</span>
                </button>
              );
            })}
          </div>
        ) : null}

        {fileDragActive ? (
          <div className={styles.dropOverlay} aria-live="polite">
            <div className={styles.dropOverlayPanel}>
              <SparkAttachmentLine />
              <span>{t("chat.attachments.dropOverlay")}</span>
            </div>
          </div>
        ) : null}

        {previewBackfeed.draft ? (
          <div className={styles.previewFixStrip} aria-live="polite">
            <MonitorPlay size={16} />
            <span>
              捕获预览错误：{previewBackfeed.draft.summary || "等待回填修复"}
            </span>
            <Button
              size="small"
              type="text"
              onClick={() => previewBackfeed.applyDraft()}
            >
              回填修复
            </Button>
            <Button
              size="small"
              type="text"
              onClick={previewBackfeed.dismissDraft}
            >
              忽略
            </Button>
          </div>
        ) : null}

        {/* Render approval requests as one aggregated overlay stack */}
        {approvalGroups.length > 0 ? (
          <aside
            className={styles.approvalStack}
            data-chat-scroll-chain="1"
            aria-label="待处理的安全审批"
            aria-live="polite"
          >
            {approvalGroups.map((request, index) => (
              <div
                key={request.requestId}
                className={styles.approvalStackItem}
                data-approval-id={request.requestId}
              >
                <ApprovalCard
                  requestId={request.requestId}
                  requestCount={request.requestCount}
                  toolName={request.toolName}
                  severity={request.severity}
                  findingsCount={request.findingsCount}
                  findingsSummary={request.findingsSummary}
                  toolParams={request.toolParams}
                  createdAt={request.createdAt}
                  timeoutSeconds={request.timeoutSeconds}
                  sessionId={request.sessionId}
                  rootSessionId={request.rootSessionId}
                  policyDecision={request.policyDecision}
                  approvalOptions={request.approvalOptions}
                  sessionRememberable={request.sessionRememberable}
                  rememberable={request.rememberable}
                  permissionScope={request.permissionScope}
                  sourceRunId={request.sourceRunId}
                  sourceAgentName={request.sourceAgentName}
                  keyboardActive={index === approvalGroups.length - 1}
                  onApprove={async (_requestId, scope) => {
                    const requestIds =
                      scope === "session"
                        ? request.requestIds.slice(0, 1)
                        : request.requestIds;
                    for (const id of requestIds) {
                      await handleApprove(id, scope);
                    }
                  }}
                  onDeny={async () => {
                    for (const id of request.requestIds) {
                      await handleDeny(id);
                    }
                  }}
                  onViewRules={() => navigate("/permissions")}
                  onCancel={() => {
                    requestStopForActiveChat(window.currentSessionId);
                  }}
                />
              </div>
            ))}
          </aside>
        ) : null}

        <Modal
          open={showModelPrompt}
          closable={false}
          footer={null}
          width={620}
          styles={{
            content: isDark
              ? {
                  background: "#1f1f1f",
                  boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
                }
              : undefined,
          }}
        >
          <Result
            icon={<ExclamationCircleOutlined style={{ color: "#faad14" }} />}
            title={
              <span
                style={{ color: isDark ? "rgba(255,255,255,0.88)" : undefined }}
              >
                {t("modelConfig.promptTitle")}
              </span>
            }
            subTitle={
              <div
                className={styles.firstRunGuide}
                style={{ color: isDark ? "rgba(255,255,255,0.55)" : undefined }}
              >
                <p>{t("modelConfig.promptMessage")}</p>
                <ol>
                  <li data-current="true">
                    <span>1</span>
                    <strong>{t("modelConfig.firstRunConnect")}</strong>
                  </li>
                  <li>
                    <span>2</span>
                    <strong>{t("modelConfig.firstRunTask")}</strong>
                  </li>
                  <li>
                    <span>3</span>
                    <strong>{t("modelConfig.firstRunResult")}</strong>
                  </li>
                </ol>
                <div className={styles.modelAccessChoices}>
                  <button
                    type="button"
                    data-recommended="true"
                    onClick={() => {
                      setShowModelPrompt(false);
                      navigate(buildAccountSetupPath(modelSetupReturnTo));
                    }}
                  >
                    <CloudOutlined aria-hidden="true" />
                    <span>
                      <strong>{t("modelConfig.watchaChoice")}</strong>
                      <small>{t("modelConfig.watchaChoiceDescription")}</small>
                    </span>
                    <em>{t("modelConfig.recommended")}</em>
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setShowModelPrompt(false);
                      navigate(buildModelsSetupPath("own", modelSetupReturnTo));
                    }}
                  >
                    <ApiOutlined aria-hidden="true" />
                    <span>
                      <strong>{t("modelConfig.ownApiChoice")}</strong>
                      <small>{t("modelConfig.ownApiChoiceDescription")}</small>
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setShowModelPrompt(false);
                      navigate(
                        buildModelsSetupPath("local", modelSetupReturnTo),
                      );
                    }}
                  >
                    <LaptopOutlined aria-hidden="true" />
                    <span>
                      <strong>{t("modelConfig.localChoice")}</strong>
                      <small>{t("modelConfig.localChoiceDescription")}</small>
                    </span>
                  </button>
                </div>
                <small>{t("modelConfig.firstRunRecovery")}</small>
              </div>
            }
            extra={[
              <Button key="skip" onClick={() => setShowModelPrompt(false)}>
                {t("modelConfig.skipButton")}
              </Button>,
            ]}
          />
        </Modal>
      </div>

      {privateEditorPanelVisible && privateEditorProjectId ? (
        <PrivateEditorPanel
          projectId={privateEditorProjectId}
          onRequestNewAngle={handleRequestPrivateEditorAngle}
          onDevelopAngle={handleDevelopPrivateEditorAngle}
          onClose={() => setPrivateEditorPanelOpen(false)}
        />
      ) : null}

      <KdocsWorkspacePanel
        key={kdocsReferenceScope}
        requestedReference={kdocsRequestedReference}
        open={kdocsPanelOpen}
        references={kdocsReferences}
        requestedDocument={kdocsRequestedDocument}
        onRequestedDocumentHandled={() => setKdocsRequestedDocument(null)}
        onClose={() => {
          setKdocsPanelOpen(false);
          setKdocsRequestedDocument(null);
        }}
        onAddDocument={handleAddKdocsDocument}
        onAddExcerpt={handleAddKdocsExcerpt}
        onRemoveDocument={handleRemoveKdocsDocument}
        onMarkDocumentUpdated={handleMarkKdocsDocumentUpdated}
      />

      <KdocsPicker
        open={kdocsPickerOpen}
        referencedKeys={kdocsReferencedDocumentKeys}
        onClose={() => setKdocsPickerOpen(false)}
        onConfirm={(documents) => {
          documents.forEach((document) => handleAddKdocsDocument(document));
          if (documents.length > 0) {
            message.success(`已添加 ${documents.length} 个金山文档`);
          }
        }}
        onPreview={(document) => {
          setKdocsPickerOpen(false);
          handleOpenKdocsDocument(document);
        }}
      />

      <MacUseTargetDialog
        open={macUsePickerOpen}
        value={macUseTarget}
        onChange={handleMacUseTargetChange}
        onClose={() => setMacUsePickerOpen(false)}
      />

      <ChatArtifactPanel
        open={artifactPanelOpen}
        chatId={chatId}
        requestedPath={artifactRequestedPath}
        onClose={() => setArtifactPanelOpen(false)}
        onPrepareArtifactEdit={handlePrepareArtifactEdit}
      />
      <BrowserSessionPanel
        target={browserSessionTarget}
        onClose={() => {
          if (browserSessionTarget?.runId) {
            window.sessionStorage.setItem(
              `lantai:browser-sidecar:dismissed:${browserSessionTarget.runId}`,
              "1",
            );
          }
          setBrowserSessionTarget(null);
        }}
      />
      {subAgentInspectionTarget ? (
        <SubAgentInspectorPanel
          target={subAgentInspectionTarget}
          onClose={() => setSubAgentInspectionTarget(null)}
          onTakeover={() => {
            const parentChatId = collaborationParentChatTarget(
              subAgentInspectionTarget.roomSnapshot,
            );
            if (parentChatId) {
              lastSessionIdRef.current = parentChatId;
              sessionApi.preferredChatId = parentChatId;
              if (embedded) {
                setEmbeddedChatId(parentChatId);
              } else {
                setLastChatId(selectedAgent, parentChatId);
                navigate(`/chat/${parentChatId}`);
              }
            }
            setSubAgentInspectionTarget(null);
            window.setTimeout(() => getChatTextarea()?.focus(), 50);
          }}
          onStop={() => {
            const target = subAgentInspectionTarget;
            if (target?.kind === "member" && target.taskId && target.agentId) {
              void runtimeApi
                .cancelAgentTask(target.taskId, target.agentId)
                .then(() =>
                  message.success(`已停止${target.agentName}的当前任务`),
                )
                .catch((error) => {
                  console.error("Failed to stop collaboration member:", error);
                  message.error("停止该协作者失败");
                });
              return;
            }
            requestStopForActiveChat(window.currentSessionId);
          }}
          onOpenArtifacts={() => {
            setSubAgentInspectionTarget(null);
            setKdocsPanelOpen(false);
            setKdocsRequestedDocument(null);
            setArtifactPanelOpen(true);
          }}
        />
      ) : null}
    </div>
  );
}
